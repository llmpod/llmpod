"""调用日志查询 service（计量行列表 + 单条查询）。

admin 与 tenant 路由共用；tenant 侧调用必须传 ``tenant_id`` 做作用域强制
（admin 侧不传 tenant_id 时由 ``exclude_tenant_scope`` 决定：只看平台记录）。
列表只碰 PG 计量列；详情读 BodyStore 块由路由层做（body_dir 是 webserver 侧依赖）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import NotFound
from llmpod.services.tenant_scope import platform_domain_clause, platform_scope_requested
from llmpod.models import RequestLog
from llmpod.utils.time import as_utc

# 时间倒序 + id tie-break：命中 idx_request_log_tenant_created（tenant 前导热路径），
# 同刻多行时 id 保持稳定的新旧序（纯 id 倒序会退化为全量排序）
RECENCY_ORDER = (RequestLog.created_at.desc(), RequestLog.id.desc())


async def list_request_logs(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    tenant_id: int | None = None,
    api_key_id: int | None = None,
    model: str | None = None,
    status: str | None = None,
    conv_prefix_hash: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    exclude_tenant_scope: bool = False,
) -> tuple[list[RequestLog], int]:
    """计量行分页列表（筛选：租户/Key/模型/状态/会话指纹/时间区间）。返回 (当页行, 总数)。

    ``exclude_tenant_scope=True``（admin 路由不传/传 0 时置位）：``tenant_id`` 未指定时
    返回整个平台域（``tenant_id IS NULL OR tenant_id = 0``——直发 Key 计量行恒为 NULL，
    0 臂与审计日志的平台域哨兵口径一致）；显式传具体租户则精确等值。
    租户端查询不传该开关（本就强制本租户）。
    """
    conds = []
    if platform_scope_requested(tenant_id, exclude_tenant_scope=exclude_tenant_scope):
        conds.append(platform_domain_clause(RequestLog.tenant_id))
    elif tenant_id is not None:
        conds.append(RequestLog.tenant_id == tenant_id)
    if api_key_id is not None:
        conds.append(RequestLog.api_key_id == api_key_id)
    if model is not None:
        conds.append(RequestLog.model_alias == model)
    if status is not None:
        conds.append(RequestLog.status == status)
    if conv_prefix_hash is not None:
        conds.append(RequestLog.conv_prefix_hash == conv_prefix_hash)
    since = as_utc(since)
    if since is not None:
        conds.append(RequestLog.created_at >= since)
    until = as_utc(until)
    if until is not None:
        conds.append(RequestLog.created_at <= until)

    base = select(RequestLog)
    if conds:
        base = base.where(*conds)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = list(
        (await db.execute(base.order_by(*RECENCY_ORDER).offset((page - 1) * page_size).limit(page_size))).scalars()
    )
    return rows, total


async def get_request_log(db: AsyncSession, *, request_id: str, tenant_id: int | None = None) -> RequestLog:
    """按 request_id 取单条计量行；``tenant_id`` 非空时强制作用域（跨租户视同不存在）。"""
    conds = [RequestLog.request_id == request_id]
    if tenant_id is not None:
        conds.append(RequestLog.tenant_id == tenant_id)
    row = (await db.execute(select(RequestLog).where(*conds))).scalar_one_or_none()
    if row is None:
        raise NotFound(f"调用日志不存在: {request_id}")
    return row


def get_body_store():
    """webserver 侧 BodyStore 读句柄（与 API 网关同卷部署，compose 共享 body_dir）。"""
    from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
    from llmpod.config.settings import settings

    return LocalFileBodyStore(root=Path(settings.body_dir))


def decode_body_block(store, block_hash: str | None):
    """按块哈希还原调用 body（JSON 解析；坏块降级为 _raw 文本，缺失返回 None）。

    管理端与租户端的调用详情路由共用；块 GC 已删/写失败（snapshot_disabled）时
    还原值为 None，由响应层表现为 body 缺失。
    """
    if not block_hash:
        return None
    raw = store.get(block_hash)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw.decode(errors="replace")}
