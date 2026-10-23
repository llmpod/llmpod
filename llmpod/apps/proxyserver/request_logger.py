"""管道末尾异步记账（写入路径）。

流程：截断 → put 请求/响应两块（gzip 内容寻址）→ 写 PG 计量行。
原文按需排障口径直接落块（含用户嵌在 body 里的自家凭据），不做脱敏。
容错：块写失败 → 计量行仍落库（refs 置空 + 告警）——计量不丢；
行写失败 → 块留着无害（无引用块由 GC 兜底）。
调用方以 fire-and-forget task 执行（不阻塞已产出响应）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from llmpod.apps.proxyserver.bodystore import BodyStore
from llmpod.apps.proxyserver.routing import compute_conv_prefix_hash
from llmpod.apps.proxyserver.usage_cache import extract_cached_tokens, usage_totals
from llmpod.core.encoding import canonical_json, lenient_utf8
from llmpod.models import RequestLog
from llmpod.models.request_log import PROVIDER_NAME_MAX_CHARS

log = logging.getLogger("llmpod.proxyserver.logger")

DEFAULT_MAX_FIELD_BYTES = 32 * 1024
_MAX_LIST_ITEMS = 100  # 数组最多保留条数（防 token_ids 等大数组撑爆 BodyStore）
_TRUNCATE_SUFFIX = "…[truncated]"


def _clip_upstream_name(name: str | None) -> str | None:
    """按列宽截断上游展示名（部署名/端点名 → request_log.provider_name）。

    展示名取自端点 ``name``（本地=部署名、外部=登记名，上限 255），而列只有
    ``PROVIDER_NAME_MAX_CHARS``。不截断的话超长 INSERT 会被 PG 拒绝，而落账是
    fire-and-forget（异常只告警不抛）——结果是**整条调用日志与计量数据被静默丢掉**。
    宁可显示名截断，不可丢计量。按 Python 码点切片（切片本身钳制越界，短名原样返回），
    不会切出半个字符；列放宽到 255 后可删掉本函数（需带迁移）。
    """
    return name if name is None else name[:PROVIDER_NAME_MAX_CHARS]


@dataclass
class _PreparedRow:
    """log() 的备料结果（_prepare 在线程池执行）：块引用 + 用量四项 + 亲和哈希。"""

    request_ref: str | None
    response_ref: str | None
    prefix_hash: str | None
    truncated: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int | None


@dataclass
class RequestLogRecord:
    """一次调用的完整记账输入（管道各处收集，末尾一次落账）。"""

    api_key_id: int
    model_alias: str
    operation: str
    status: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: int | None = None
    served_model: str | None = None
    protocol: str | None = None
    engine: str | None = None
    upstream_type: str | None = None
    # 上游展示名（快照自端点 name：本地=部署名，外部=端点名）。字段名沿用 provider_name
    # 是对齐 request_log 列名与前端 JSON 契约，语义已不是「供应商」；超列宽由
    # _clip_upstream_name 在写库处截断
    provider_name: str | None = None
    endpoint_id: int | None = None
    # 选路上游的原因（affinity / weighted / failover_affinity / failover_weighted）：
    # endpoint_id 只说落到哪，这一列说为什么落到那——排查「会话怎么被甩到别的供应商」
    # 靠它一眼分开缓存优先与负载均衡，见 routing.selector.route_reason
    route_reason: str | None = None
    retry_count: int = 0
    usage: dict | None = None
    usage_estimated: bool = False
    cost: Decimal | None = None
    latency_ms: int | None = None
    # 首帧延迟：仅流式有值（非流式拿不到「首帧」这个时点），吐字速度的分母靠它
    ttft_ms: int | None = None
    # 上游结束原因原样存（openai finish_reason / anthropic stop_reason）：截断率
    # 的判定靠它，不用 truncated（那是日志快照是否截断 body，两码事）
    finish_reason: str | None = None
    http_status: int | None = None
    request_payload: dict | None = None
    response_payload: dict | None = None
    error_message: str | None = None
    error_detail: str | None = None


def _truncate_strings(obj: Any, max_bytes: int) -> tuple[Any, bool]:
    """递归截断超长字段：字符串按 max_bytes 截断，数组按条数上限截断（超限标 truncated）。"""
    truncated = False

    def walk(node):
        nonlocal truncated
        if isinstance(node, str):
            raw = lenient_utf8(node)  # 坏字符（孤立代理项）在此不炸，见 core.encoding
            if len(raw) <= max_bytes:
                return node
            truncated = True
            return raw[:max_bytes].decode(errors="ignore") + _TRUNCATE_SUFFIX
        if isinstance(node, list):
            if len(node) > _MAX_LIST_ITEMS:
                truncated = True
                kept = [walk(x) for x in node[:_MAX_LIST_ITEMS]]
                kept.append(f"…[{len(node) - _MAX_LIST_ITEMS} more items]")
                return kept
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(obj), truncated


class RequestLogger:
    """异步记账器（BodyStore 块 + PG 计量行；db_factory 生产用，db 直传测试用）。"""

    def __init__(self, db_factory, store: BodyStore, *, max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES):
        self._db_factory = db_factory
        self._store = store
        self._max_field_bytes = max_field_bytes

    async def log(self, record: RequestLogRecord, db=None) -> None:
        """落账一次调用；块失败降级、行失败告警，均不抛（fire-and-forget 语义）。

        截断/规范化/哈希/gzip 写盘都是 CPU+磁盘活，多 MB 的多模态请求体在事件
        循环上同步跑会卡住所有在途流式响应——整段备料挪 to_thread，回到循环后
        只剩 PG 行写入。
        """
        prepared = await asyncio.to_thread(self._prepare, record)

        row = RequestLog(
            request_id=record.request_id,
            tenant_id=record.tenant_id,
            api_key_id=record.api_key_id,
            model_alias=record.model_alias,
            served_model=record.served_model,
            operation=record.operation,
            protocol=record.protocol,
            engine=record.engine,
            upstream_type=record.upstream_type,
            provider_name=_clip_upstream_name(record.provider_name),
            endpoint_id=record.endpoint_id,
            route_reason=record.route_reason,
            retry_count=record.retry_count,
            prompt_tokens=prepared.prompt_tokens,
            completion_tokens=prepared.completion_tokens,
            total_tokens=prepared.total_tokens,
            usage_estimated=record.usage_estimated,
            cached_tokens=prepared.cached_tokens,
            cost=record.cost,
            latency_ms=record.latency_ms,
            ttft_ms=record.ttft_ms,
            finish_reason=record.finish_reason,
            status=record.status,
            http_status=record.http_status,
            request_ref=prepared.request_ref,
            response_ref=prepared.response_ref,
            conv_prefix_hash=prepared.prefix_hash,
            truncated=prepared.truncated,
            snapshot_disabled=prepared.request_ref is None and prepared.response_ref is None,
            error_message=record.error_message,
            error_detail=record.error_detail,
        )
        try:
            if db is not None:
                db.add(row)
                await db.flush()
                await db.commit()
            elif self._db_factory is not None:
                # async with 保证成功提交/异常回滚都走 __aexit__，不泄漏 session/连接
                async with self._db_factory() as session:
                    session.add(row)
                    await session.flush()
                    # commit 由 get_db_context 的 __aexit__ 负责（成功提交、异常回滚）
            else:
                log.error("request_log drop: no db session (request_id=%s)", record.request_id)
        except Exception:  # noqa: BLE001 — 行写失败块留着无害
            log.exception("request_log insert failed (request_id=%s)", record.request_id)

    def _prepare(self, record: RequestLogRecord) -> _PreparedRow:
        """截断 → 块写 → 用量/亲和哈希抽取（同步；log 已挪 to_thread，勿在循环内直调）。"""
        req_obj, req_trunc = _truncate_strings(record.request_payload, self._max_field_bytes)
        resp_obj, resp_trunc = _truncate_strings(record.response_payload, self._max_field_bytes)

        request_ref = response_ref = None
        try:
            if req_obj is not None:
                request_ref = self._store.put(lenient_utf8(canonical_json(req_obj)))
            if resp_obj is not None:
                response_ref = self._store.put(lenient_utf8(canonical_json(resp_obj)))
        except Exception:  # noqa: BLE001 — 块写失败不丢计量
            log.exception("body store put failed (request_id=%s)", record.request_id)

        prompt, completion, total = usage_totals(record.usage)

        prefix_hash = None
        if record.request_payload is not None:
            prefix_hash = compute_conv_prefix_hash(record.operation, record.request_payload)

        return _PreparedRow(
            request_ref=request_ref,
            response_ref=response_ref,
            prefix_hash=prefix_hash,
            truncated=req_trunc or resp_trunc,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=extract_cached_tokens(record.usage),
        )
