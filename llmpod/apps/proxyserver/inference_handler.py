"""API 网关请求生命周期：准入 + 转发 + usage 落账 + 计费记账。

路由层只声明 operation 与客户端工厂，其余（鉴权 → 授权 → 限流 → 余额预扣 → 配额 →
选择器 → 协议转发 → 整形 → usage 落账 → 结算/退款 → 记账）在此统一编排。协议相关的
只有三处，且都经 operation 推导：入站鉴权头形态（x-api-key / Bearer 等价接受）、
拒绝响应的错误信封（Anthropic 客户端解析不了 OpenAI 形状）、转发携带的客户端协商头
（anthropic-version / anthropic-beta，见 protocol.base 的 PASSTHROUGH_HEADERS）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from llmpod.apps.proxyserver.authn import ApiKeyContext, authenticate
from llmpod.apps.proxyserver.authz import AuthorizationError, authorize
from llmpod.apps.proxyserver.billing import HeldReservation, InsufficientBalance, KeyBudgetExceeded
from llmpod.apps.proxyserver.limits import (
    QuotaCounters,
    QuotaExceededError,
    RateLimitError,
    RateLimiter,
    ReservedUsage,
)
from llmpod.apps.proxyserver.usage_cache import normalize_anthropic_usage
from llmpod.apps.proxyserver.pricing import (
    _extract_max_tokens,
    compute_cost,
    estimate_cost,
    estimate_prompt_tokens,
    estimate_text_tokens,
)
from llmpod.apps.proxyserver.protocol import ADAPTERS, ProtocolAdapter, collect_passthrough_headers
from llmpod.apps.proxyserver import metrics as proxy_metrics
from llmpod.apps.proxyserver.request_logger import RequestLogRecord
from llmpod.apps.proxyserver.routing import compute_boundary_hashes, route_reason
from llmpod.apps.proxyserver.snapshot import MaintenanceState, RouteEntry, RouteTarget
from llmpod.config.enums import AffinityFailoverPolicy
from llmpod.config.settings import settings

log = logging.getLogger("llmpod.proxyserver")


class HandledError(Exception):
    """管道已归一为标准错误体（携带响应，路由层直接返回）。

    ``reject_reason`` 非空表示该错误属于「准入拒绝」（未触达上游）：
    收口处据此打拒绝指标点（rejects_total 按原因聚合）。拒绝不写
    request_log（行缺模型/Key/用量，且 401 可刷库）；``meta`` 携带拒绝
    时刻已知的部分维度（Key/租户/模型名，选路前部分字段为空）。
    """

    def __init__(self, response: JSONResponse, *, reject_reason: str | None = None, meta: dict | None = None):
        self.response = response
        self.reject_reason = reject_reason
        self.meta = meta or {}


def openai_error(status: int, message: str, err_type: str, code: str) -> JSONResponse:
    """OpenAI 错误体（引擎身份不泄露）。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


# Anthropic 错误体的 type 取值由 HTTP 状态推导（Anthropic 无 code 字段，只有 type）。
# 只列本管道真的会产生的状态：准入链的 400/401/402/403/404/429/503 与上游归一后的
# 429/504/502（见 _upstream_status）。未命中的状态回落到 api_error——客户端按状态码
# 选异常类，type 只影响展示文案。
_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


def anthropic_error(status: int, message: str) -> JSONResponse:
    """Anthropic 错误体：``{"type":"error","error":{"type","message"}}``。

    Anthropic SDK 只认这个信封——返回 OpenAI 形状的 ``{"error":{...}}`` 时
    客户端解析不出 message，报错会在 SDK 层退化成无信息量的通用异常。
    """
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": _ANTHROPIC_ERROR_TYPES.get(status, "api_error"), "message": message},
        },
    )


def error_response(protocol: str, status: int, message: str, err_type: str, code: str) -> JSONResponse:
    """按协议出错误体：anthropic 走 Anthropic 信封，其余走 OpenAI 信封。

    两种信封承载的信息不同（Anthropic 无 code，type 由状态推导），故 anthropic
    分支忽略 err_type/code——调用方仍照常传，保持单一调用形态。
    """
    if protocol == "anthropic":
        return anthropic_error(status, message)
    return openai_error(status, message, err_type, code)


def upstream_error_response(e: Exception, protocol: str = "openai") -> JSONResponse:
    """上游异常归一：429 透传、超时 504、其余 502 bad_gateway。

    状态判定只走 :func:`_upstream_status`（与错误落账同源），避免两处
    isinstance 规则各自演化出现「落账 504、响应 502」的漂移。
    """
    status = _upstream_status(e)
    if status == 429:
        return error_response(protocol, 429, "upstream rate limited", "rate_limit_error", "engine_rate_limited")
    if status == 504:
        return error_response(protocol, 504, "upstream timeout", "api_error", "upstream_timeout")
    return error_response(protocol, 502, "upstream error", "api_error", "bad_gateway")


def _upstream_status(e: Exception) -> int:
    """上游失败的对外状态码（429 透传/超时 504/其余 502）；错误落账与响应归一共用。"""
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
        return 429
    return 504 if isinstance(e, httpx.TimeoutException) else 502


def _upstream_error_message(e: Exception) -> str:
    """上游失败原因（不暴露端点 URL，仅状态/类型，供 request_log.error_message）。"""
    if isinstance(e, httpx.HTTPStatusError):
        return f"upstream HTTP {e.response.status_code}"
    return f"upstream {type(e).__name__}"


def _upstream_error_detail(e: Exception) -> str | None:
    """上游错误原文，供 request_log.error_detail（仅管理员视图返回）。

    管理员有全系统可见权（拓扑/端点/key 本就可见），原文直存保真排障——只做
    500 字符截断防超长错误体撑爆列。取不到原文（连接层异常）返回 None。
    """
    if not isinstance(e, httpx.HTTPStatusError):
        return None
    try:
        body = e.response.text
    except Exception:
        return None
    if not body:
        return None
    if len(body) > 500:
        body = body[:500] + "…[truncated]"
    return body


def _failure_counts_toward_cooldown(e: Exception, candidate_count: int) -> bool:
    """失败是否计入端点熔断；failover 循环与流式中断收尾共用同一口径。

    端点级 = 真连接类故障（引擎死/网络断），即便只有一个候选也熔断——否则每个请求
    都会再挂满整个超时才失败，流量持续灌进黑洞。两类不算端点的锅，唯一候选不熔断
    （否则本该秒回的请求也被连坐 60s 503）：
    - ``UpstreamDeadlineExceeded``：我们自己的墙钟预算耗尽（长 prefill 首字节慢）；
    - ``httpx.PoolTimeout``：本进程连接池饱和（自伤）。
    多候选时请求级错误（HTTP 4xx/5xx）也冷却（慢/挤的端点让路给其他候选）。
    """
    self_inflicted = isinstance(e, (UpstreamDeadlineExceeded, httpx.PoolTimeout))
    endpoint_level = isinstance(e, httpx.RequestError) and not self_inflicted
    return endpoint_level or candidate_count > 1


def _protocol_candidates(entry: RouteEntry, adapter: ProtocolAdapter) -> tuple[RouteTarget, ...]:
    """该路由下与本次操作同协议的候选端点（协议内过滤，不做协议转换）。

    选路与失败分类共用同一份候选集——候选数是「本次实际可选范围」的度量，用未过滤
    的全部绑定会让混合协议路由（同一条路由同时挂 openai 与 anthropic 端点）下的
    「唯一候选不熔断」判定翻转。
    """
    return tuple(t for t in entry.targets if t.protocol == adapter.protocol)


def _note_endpoint_success(runtime, target: RouteTarget, hashes: list[str] | None, scope: str) -> None:
    """端点本轮已证健康：清零失败计数 + 亲和写/改绑。

    只在「确有产出」后调用（非流式 forward 返回完整响应 / 流式生成器走完）：选中即写
    会把失败端点固定；每次成功都重写也顺带刷新 TTL——会话活着就一直绑定，空闲到期
    自然解除。
    """
    runtime.selector.report_success(target.endpoint_id)
    runtime.selector.bind_affinity(hashes, target.endpoint_id, scope=scope)


def _inbound_candidates(request: Request, protocol: str) -> list[str]:
    """从请求头取明文 Key 候选：``x-api-key`` 与 ``Authorization: Bearer`` 等价接受。

    两种形态对应两套客户端：Anthropic SDK 只发 ``x-api-key``（不发 Authorization），
    OpenAI SDK 只发 Bearer。有些客户端/中间件两个头都带（比如网关注入了自己的
    x-api-key），其中一个是陈旧值——按头形态定优先级会让「多带一个头」反而把
    原本能过的请求打成 401，故两个候选都取出、任一校验通过即放行。按协议把
    常见形态排前，让绝大多数请求第一个候选就命中，不为叠加头的少数派多付一次
    鉴权往返。
    """
    api_key = request.headers.get("x-api-key")
    auth = request.headers.get("Authorization", "")
    bearer = auth[len("Bearer ") :] if auth.startswith("Bearer ") else None

    if protocol == "anthropic":
        preferred, other = api_key, bearer
    else:
        preferred, other = bearer, api_key
    out: list[str] = []
    if preferred:
        out.append(preferred)
    if other and other != preferred:
        out.append(other)
    return out


async def authenticate_http(request: Request, db, redis, *, protocol: str = "openai") -> ApiKeyContext:
    """校验入站 Key；缺失/无效 raise HandledError(401)。"""
    candidates = _inbound_candidates(request, protocol)
    if not candidates:
        raise HandledError(
            error_response(protocol, 401, "missing api key", "invalid_request_error", "invalid_api_key"),
            reject_reason="invalid_key",
        )
    for plaintext in candidates:
        ctx = await authenticate(db, redis, plaintext, client_ip=request.client.host if request.client else None)
        if ctx is not None:
            return ctx
    raise HandledError(
        error_response(protocol, 401, "invalid api key", "invalid_request_error", "invalid_api_key"),
        reject_reason="invalid_key",
    )


async def admit_inference_request(
    runtime, request: Request, db, redis, *, operation: str, payload: dict, request_id: str
) -> tuple[ApiKeyContext, RouteEntry, HeldReservation | None, ReservedUsage]:
    """准入：鉴权 → 授权 → 限流 → 配额 → 余额预扣；异常归一为标准错误体。

    顺序约定：配额在预扣之前——预扣之后的任何拒绝路径都要走退款，能前置拒绝的
    就不碰钱。预扣是最后一级，准入通过即「一定会有转发尝试或显式退款」。
    返回值带 hold guard（钱）与 ReservedUsage guard（TPM/配额占位）：结算与
    归还语义见各 guard；占位后的任何失败路径统一归还，不留计数给被拒请求。
    """
    # 拒绝响应按请求协议出信封（Anthropic 客户端解析不了 OpenAI 形状）
    protocol = ADAPTERS[operation].protocol
    ctx = await authenticate_http(request, db, redis, protocol=protocol)

    # 维护模式：租户 Key 全拒（平台直发 Key tenant_id=None 放行）。拒绝即未触达
    # 上游，走 reject 计数、不落 request_log。判定口径抽自 maintenance_reject，
    # 与 /v1/models 共用（改策略只改一处）。
    snapshot = await runtime.get_or_build(db)
    maintenance_rejected = maintenance_reject(snapshot.maintenance, ctx, protocol=protocol)
    if maintenance_rejected is not None:
        raise HandledError(
            maintenance_rejected,
            reject_reason="maintenance",
            meta={"key_id": ctx.key_id, "tenant_id": ctx.tenant_id},
        )

    model_name = payload.get("model")
    if not model_name:
        raise HandledError(
            error_response(protocol, 400, "missing model", "invalid_request_error", "missing_model"),
            reject_reason="missing_model",
            meta={"key_id": ctx.key_id, "tenant_id": ctx.tenant_id},
        )
    # 快照命中直接用；快照还没轮到的名字（新增路由/刚绑成员）回源点查，
    # 发布后立即可用，不必等下一个轮询周期
    entry = await runtime.resolve(db, model_name)
    if entry is None:
        raise HandledError(
            error_response(
                protocol, 404, f"model {model_name!r} not found", "invalid_request_error", "model_not_found"
            ),
            reject_reason="model_not_found",
            meta={"key_id": ctx.key_id, "tenant_id": ctx.tenant_id, "model_alias": model_name},
        )
    # 准入各拒绝出口共用的打点维度（模型名已知后统一带 alias）
    reject_meta = {"key_id": ctx.key_id, "tenant_id": ctx.tenant_id, "model_alias": model_name}
    try:
        await authorize(runtime, db, ctx, entry)
    except AuthorizationError as e:
        code = (
            "model_not_allowed"
            if "not allowed" in e.reason
            else ("model_not_published" if "not published" in e.reason else "permission_denied")
        )
        raise HandledError(
            error_response(protocol, 403, e.reason, "permission_error", code),
            reject_reason=code,
            meta=reject_meta,
        )

    # 输入 token 预估计算一次，限流占位 / 配额判定 / 预扣估算共用（CJK 等按 1 token 高估）
    est_prompt = estimate_prompt_tokens(payload, operation)
    # 输出 token 预留：客户端用 max_tokens 限定输出上限时一并预留，与 settle 记全量
    # （input+output）口径对齐；未限定则输出不可预知，仅按输入预留（估值的固有上限）
    est_output = _extract_max_tokens(payload) or 0

    # TPM/配额分别以 Redis INCRBY 占位后判定；各计数器原子，多维度间尽力回滚。
    # 占位后的失败（限流/配额被拒在各自内部回滚；余额不足/预扣故障走 except 统一
    # 归还）都不消耗档位；结算侧 settle_usage 归还占位（净额 = 真实用量）。
    reserved = ReservedUsage(request_id=request_id)
    try:
        try:
            await RateLimiter(redis).check(
                key_id=ctx.key_id,
                rpm_limit=ctx.rpm_limit,
                tpm_limit=ctx.tpm_limit,
                est_tokens=est_prompt,
                reserve=reserved,
            )
        except RateLimitError as e:
            raise HandledError(
                error_response(protocol, 429, str(e), "rate_limit_error", "rate_limit_exceeded"),
                reject_reason="rate_limited",
                meta=reject_meta,
            )
        if ctx.total_token_quota is not None or model_name in ctx.model_token_quotas:
            try:
                await QuotaCounters(redis).precheck(
                    key_id=ctx.key_id,
                    model=model_name,
                    estimated_tokens=est_prompt + est_output,
                    total_token_quota=ctx.total_token_quota,
                    model_token_quota=ctx.model_token_quotas.get(model_name),
                    model_global_quota=None,
                    reserve=reserved,
                )
            except QuotaExceededError as e:
                raise HandledError(
                    error_response(protocol, 429, e.reason, "rate_limit_error", "insufficient_quota"),
                    reject_reason="quota",
                    meta=reject_meta,
                )
    except BaseException:
        await reserved.release(redis)
        raise

    # 余额预扣（钱）：准入最后一级；平台 Key 也出 guard（鉴权快照有上限才进
    # Key 预算冻结，不触钱包），租户 Key 零成本（未配定价）返回 None guard
    est_cost = estimate_cost(entry, operation, payload, est_prompt=est_prompt)
    held: HeldReservation | None = None
    try:
        if runtime.billing is not None:
            try:
                held = await runtime.billing.hold(ctx, entry, est_cost, request_id=request_id)
            except (InsufficientBalance, KeyBudgetExceeded) as e:
                raise HandledError(
                    error_response(protocol, 402, str(e), "insufficient_quota", "insufficient_quota"),
                    reject_reason="insufficient_balance",
                    meta=reject_meta,
                )
            except Exception as e:
                log.exception("billing hold failed (key=%s)", ctx.key_id)
                raise HandledError(
                    error_response(
                        protocol, 503, "billing temporarily unavailable", "server_error", "billing_unavailable"
                    ),
                    reject_reason="billing_unavailable",
                    meta=reject_meta,
                ) from e
    except BaseException:
        await reserved.release(redis)
        raise
    return ctx, entry, held, reserved


async def settle_usage(
    redis,
    ctx: ApiKeyContext,
    model: str,
    usage: dict | None,
    reserved: ReservedUsage | None = None,
) -> None:
    """usage 落账：TPM 回填 + 三档配额计数（失败不阻断已产出响应）。

    ``reserved`` 是准入期的 TPM/配额占位：全量加真实用量后归还占位（净额 =
    真实用量）；usage 缺失（未触达上游即终局）也要归还，占位不能悬空。
    """
    if not usage:
        if reserved is not None:
            await reserved.release(redis)
        return
    total = usage.get("total_tokens")
    if total is None:
        # 与 usage_totals 的口径差异是有意的：此处 input-first——anthropic 规范化后
        # prompt_tokens 含缓存读写、input_tokens 是未缓存部分，TPM/配额按未缓存
        # 输入计；落账/指标的 usage_totals 走 prompt-first（展示与计量用全量）
        inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        total = inp + out
    if total <= 0:
        if reserved is not None:
            await reserved.release(redis)
        return
    try:
        await RateLimiter(redis).record_usage(key_id=ctx.key_id, tokens=total)
        await QuotaCounters(redis).settle(key_id=ctx.key_id, model=model, tokens=total)
        if reserved is not None:
            await reserved.settle(redis)
    except Exception:
        log.critical(
            "usage settle failed (request_id=%s key=%s model=%s tokens=%s)",
            reserved.request_id if reserved is not None else None,
            ctx.key_id,
            model,
            total,
            exc_info=True,
        )


def spawn_log(runtime, record: RequestLogRecord) -> None:
    """fire-and-forget 计量行落账（先块后行，块失败计量不丢）。

    task 存入 runtime 集合防 GC（无强引用的 task 可能被中途回收）；
    回调里自移除并吞掉异常（记账失败只留日志）。
    """
    if runtime.logger is None:
        return
    task = asyncio.create_task(runtime.logger.log(record, db=runtime.log_db))
    runtime.log_tasks.add(task)
    task.add_done_callback(runtime.log_tasks.discard)


def _elapsed_ms(start: float) -> int:
    """单调时钟起点的端到端耗时（毫秒），钳非负（时钟回调兜底）。"""
    return max(0, int((time.monotonic() - start) * 1000))


def _is_streaming_response(result) -> bool:
    """转发结果是否为流式响应（StreamingResponse 返回 ≠ 流已结束）。"""
    from starlette.responses import StreamingResponse

    return isinstance(result, StreamingResponse)


def _attach_stream_close(
    response, held, reserved, redis, operation: str, request_id: str, stream_exited: dict, on_exit=None
) -> None:
    """给流式响应挂「结束/丢弃必调」的资金兜底。

    原理：给该响应实例挂单实例子类，覆写 ``__call__``（ASGI 入口，必被 await——
    无论生成器是否被消费，starlette 调它时 finally 必执行）。生成器正常收尾/
    断连时 _stream_body 的 finally 已 settle + exit_request，close() 为 no-op
    （settle/release 幂等）、exit_request 靠 stream_exited 标记跳过（否则 gauge
    变负）；生成器被丢弃未消费时 settle/release 都没发生，close() 在此解冻 +
    补那次唯一的 exit_request。幂等不双调账。
    ``on_exit``：流真正结束/丢弃时回调（在途请求登记解除等收尾）。
    注意必须走单实例子类而非改 StreamingResponse 类属性——那是全局共享类，
    类级 patch 会污染所有后续流式响应。
    """
    original_call = type(response).__call__

    class _StreamCloseGuard(type(response)):
        async def __call__(self, scope, receive, send):
            try:
                await original_call(self, scope, receive, send)
            finally:

                async def finalize():
                    # ASGI 断连可在 send() 处取消响应任务；此时 Starlette 未必
                    # 主动关闭 body_iterator。先关闭使生成器 finally 以已产出量结算。
                    try:
                        await self.body_iterator.aclose()
                    except Exception:
                        log.exception("stream iterator close failed (request_id=%s)", request_id)
                    if held is not None:
                        try:
                            await held.close(request_id=request_id, meta={"operation": operation})
                        except Exception:
                            log.exception("stream billing close failed (request_id=%s)", request_id)
                    if reserved is not None:
                        try:
                            await reserved.release(redis)
                        except Exception:
                            log.exception("stream reserved usage release failed (request_id=%s)", request_id)
                    if not stream_exited["exited"]:
                        stream_exited["exited"] = True
                        proxy_metrics.exit_request()
                    if on_exit is not None:
                        on_exit()

                task = asyncio.create_task(finalize())
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise

    response.__class__ = _StreamCloseGuard


def _billing_meta(target: RouteTarget | None, operation: str, protocol: str, usage: dict | None) -> dict:
    meta: dict = {"operation": operation, "protocol": protocol}
    if target is not None:
        meta["served_model"] = target.served_model
    if usage:
        meta["prompt_tokens"] = usage.get("prompt_tokens") or usage.get("input_tokens")
        meta["completion_tokens"] = usage.get("completion_tokens") or usage.get("output_tokens")
    return meta


async def _settle_billing(held, entry, target, adapter, usage, operation, request_id) -> None:
    """结算（多退少补）；失败不阻断已产出响应，保留冻结供对账。"""
    if held is None:
        return
    actual_cost = compute_cost(entry, operation, usage)
    meta = _billing_meta(target, operation, adapter.protocol, usage)
    try:
        await held.settle(actual_cost, request_id=request_id, meta=meta)
    except Exception:
        log.critical(
            "billing settle failed; frozen reservation needs reconciliation (request_id=%s key=%s tenant=%s route=%s actual=%s)",
            request_id,
            held._ctx.key_id,
            held._ctx.tenant_id,
            entry.name,
            actual_cost,
            exc_info=True,
        )
        # 上游已产出：不能在数据库结算异常时把全部预扣免费退回。


async def _release_if_unsettled(
    held, reserved, redis, entry, operation, adapter, request_id, *, streaming=False
) -> None:
    """guard 未闭环则全额解冻 + 归还 TPM/配额占位（未预扣/已结算为 no-op）；失败不阻断错误响应。"""
    if reserved is not None and not streaming:
        try:
            await reserved.release(redis)
        except Exception:
            log.exception("reserved usage release failed (route=%s)", entry.name)
    if held is None:
        return
    try:
        await held.release(
            request_id=request_id,
            meta=_billing_meta(None, operation, adapter.protocol, None),
        )
    except Exception:
        log.exception("billing release failed (route=%s)", entry.name)


def build_record(
    ctx: ApiKeyContext | None,
    entry: RouteEntry | None,
    target: RouteTarget | None,
    operation: str,
    protocol: str,
    *,
    status: str = "success",
    http_status: int | None = 200,
    usage: dict | None = None,
    usage_estimated: bool = False,
    request_payload=None,
    response_payload=None,
    error_message: str | None = None,
    error_detail: str | None = None,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
    finish_reason: str | None = None,
    request_id: str | None = None,
    retry_count: int = 0,
    api_key_id: int | None = None,
    tenant_id: int | None = None,
    model_alias: str | None = None,
    route_reason: str | None = None,
) -> RequestLogRecord:
    """组装一次调用的记账输入（费用经 compute_cost 统一计价）。

    ctx/entry 允许 None（准入拒绝路径：401 无 Key 上下文、选路前无 entry），
    此时 api_key_id/tenant_id/model_alias 走显式入参（拒绝时刻已知的部分值）。
    """
    cost = compute_cost(entry, operation, usage) if entry is not None else None
    return RequestLogRecord(
        request_id=request_id or uuid.uuid4().hex,
        api_key_id=(ctx.key_id if ctx is not None else api_key_id),
        tenant_id=(ctx.tenant_id if ctx is not None else tenant_id),
        model_alias=(entry.name if entry is not None else (model_alias or "")),
        served_model=target.served_model if target else None,
        operation=operation,
        protocol=protocol,
        engine=target.engine_type if target else None,
        upstream_type=target.upstream_type if target else None,
        provider_name=target.provider_name if target else None,
        endpoint_id=target.endpoint_id if target else None,
        route_reason=route_reason,
        retry_count=retry_count,
        usage=usage,
        usage_estimated=usage_estimated,
        cost=cost,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        finish_reason=finish_reason,
        status=status,
        http_status=http_status,
        request_payload=request_payload,
        response_payload=response_payload,
        error_message=error_message,
        error_detail=error_detail,
    )


class _BodyTooLargeError(Exception):
    """请求体超过 proxy_max_body_bytes（读 body 阶段抛给上层出 413）。"""

    def __init__(self, cap: int):
        super().__init__(f"request body exceeds {cap} bytes")
        self.cap = cap


async def _read_json_bounded(request: Request) -> dict:
    """流式读取请求体并解析 JSON，累计字节超上限即中断。

    Content-Length 只在头存在时预判（提前拒，省得白读）；chunked 传输没有
    该头，靠读取途中的累计上限兜底。
    """
    cap = settings.proxy_max_body_bytes
    buf = bytearray()
    async for chunk in request.stream():
        received = len(buf) + len(chunk)
        if received > cap:
            raise _BodyTooLargeError(cap)
        buf.extend(chunk)
    return json.loads(bytes(buf))


def maintenance_reject(
    maintenance: MaintenanceState, ctx: ApiKeyContext, *, protocol: str = "openai"
) -> JSONResponse | None:
    """维护模式下租户 Key 的拒绝响应（单一判定口径，推理管道与 /v1/models 共用）。

    - 未开维护 / 平台直发 Key（tenant_id=None）→ None（放行）；
    - 命中 → 503 错误体（信封按调用方协议），message 为维护文案。
    """
    if not maintenance.enabled or ctx.tenant_id is None:
        return None
    return error_response(protocol, 503, maintenance.message, "service_unavailable", "maintenance")


def reject_response(
    meta: dict | None,
    operation: str,
    response: JSONResponse,
    reject_reason: str,
) -> JSONResponse:
    """拒绝出口：打拒绝指标点，返回既定错误响应。

    拒绝（401/400/404/403/429/402/503）**不写 request_log**——拒绝发生在选路
    之前，行缺模型/Key/用量等关键字段，硬塞进调用日志列表只会污染数据；且无
    鉴权的 401 可被外部无限刷，落库等于给攻击者一个免费写放大通道。拒绝的
    观测走两条路：指标（llmpod_proxy_rejects_total，按原因/维度聚合，中央
    scraper 收敛为 5min 一批）与 API 网关访问日志（uvicorn/nginx access log）。
    request_log 只记「真实触达上游」的请求（success/error/interrupted）。
    """
    meta = meta or {}
    proxy_metrics.record_reject(reject_reason, {**meta_to_labels(meta), "operation": operation})
    return response


def meta_to_labels(meta: dict) -> dict:
    """拒绝 meta（key_id/tenant_id/model_alias）→ 指标标签（选路后维度留空）。"""
    return {
        "tenant_id": meta.get("tenant_id"),
        "api_key_id": meta.get("key_id"),
        "model_alias": meta.get("model_alias"),
        "endpoint_id": None,
        "deployment_id": None,
        "engine": None,
        "upstream_type": None,
    }


async def handle_inference_request(runtime, request: Request, db, redis, *, operation: str, get_client):
    """推理请求对外入口：生成请求编号，出口统一盖请求 ID 响应头。

    所有 /v1/* 推理路由的成功 / 流式 / 错误信封 / 准入拒绝 / 请求体非法即拒
    响应都从这里返回，编号在进入管道前生成，人人有份。终端用户报障时贴这个
    号，平台即可反查调用日志、钱包流水与冻结单；拒绝路径不落计量行，客户端
    手里的号是它与访问日志之间唯一的对账线索。

    头名按客户端协议跟随官方惯例（同一值并排）：OpenAI 系认 ``x-request-id``，
    Anthropic 系认 ``request-id``（官方无 x- 前缀，SDK 报错提示让用户抓的就是
    它）——两边用户各自按官方习惯都能拿到号。
    """
    request_id = uuid.uuid4().hex
    response = await _handle_inference_request(
        runtime, request, db, redis, operation=operation, get_client=get_client, request_id=request_id
    )
    response.headers["x-request-id"] = request_id
    if operation.startswith("anthropic."):
        # 值与内部 request_id 严格同值（不加官方样式的 req_ 前缀）：报障贴来的号
        # 必须能原样命中计量行/钱包流水/冻结单，格式像不像官方是次要的
        response.headers["request-id"] = request_id
    return response


async def _handle_inference_request(
    runtime, request: Request, db, redis, *, operation: str, get_client, request_id: str
):
    """处理一次推理请求：准入 → 协议过滤 → 选择器 → failover 转发 → 整形 → 结算/记账。

    资金不变量由 hold guard 承担：admit 之后的一切异常路径（含 forward 抛出
    upstream_errors 之外的意外异常）统一走 finally 释放未结算的预扣——不依赖
    各出口各自记得调退款。正常产出响应（含流式）由结算多退少补闭环。
    """
    adapter: ProtocolAdapter = ADAPTERS[operation]
    # 请求体上限先于解析与鉴权：无上限时匿名请求即可让服务全量缓冲超大 body
    # （内存放大 + 事件循环阻塞）。Content-Length 头缺失/异常按无上限走流式累计。
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_len = int(content_length)
        except ValueError:
            declared_len = -1
        if declared_len > settings.proxy_max_body_bytes:
            return reject_response(
                None,
                operation,
                error_response(
                    adapter.protocol,
                    413,
                    f"request body too large (max {settings.proxy_max_body_bytes} bytes)",
                    "invalid_request_error",
                    "body_too_large",
                ),
                "body_too_large",
            )
    try:
        body = await _read_json_bounded(request)
    except (_BodyTooLargeError, ValueError) as e:
        detail = "request body too large" if isinstance(e, _BodyTooLargeError) else "invalid json body"
        code = "body_too_large" if isinstance(e, _BodyTooLargeError) else "invalid_json"
        return reject_response(
            None,
            operation,
            error_response(
                adapter.protocol,
                413 if isinstance(e, _BodyTooLargeError) else 400,
                detail,
                "invalid_request_error",
                code,
            ),
            code,
        )
    if not isinstance(body, dict):
        return reject_response(
            None,
            operation,
            error_response(
                adapter.protocol, 400, "request body must be a json object", "invalid_request_error", "invalid_body"
            ),
            "invalid_body",
        )
    start = time.monotonic()
    proxy_metrics.enter_request()
    _forward_result = None
    # 在途请求登记（hold reaper 的跳过集）：崩溃恢复后对悬空冻结做 TTL 回收时，
    # 本进程仍在处理的请求不受影响（其 hold 可能超 TTL 但仍活跃）
    runtime.active_request_ids.add(request_id)
    try:
        try:
            ctx, entry, held, reserved = await admit_inference_request(
                runtime, request, db, redis, operation=operation, payload=body, request_id=request_id
            )
        except HandledError as e:
            if e.reject_reason is None:
                return e.response
            return reject_response(e.meta, operation, e.response, e.reject_reason)
        try:
            _forward_result = await _forward_with_failover(
                runtime,
                redis,
                ctx,
                entry,
                held,
                reserved,
                adapter,
                body,
                operation,
                request_id,
                start,
                get_client,
                collect_passthrough_headers(request.headers),
            )
            return _forward_result
        finally:
            # 未结算（转发抛意外异常 / respond 抛错）→ 兜底退款 + 归还占位；流式已
            # mark_streaming，release 为 no-op（结算由生成器 finally 闭环）
            await _release_if_unsettled(
                held,
                reserved,
                redis,
                entry,
                operation,
                adapter,
                request_id,
                streaming=_is_streaming_response(_forward_result),
            )
    finally:
        # 非流式在此释放在途计数 + 解除 hold-reaper 跳过登记；流式两者都已移交
        # _StreamCloseGuard 的 finalize（StreamingResponse 对象返回≠流结束，生成器
        # 被丢弃时只有它跑——若在此提前解除，超 TTL 的长流会被 reaper 误回收）
        if not _is_streaming_response(_forward_result):
            runtime.active_request_ids.discard(request_id)
            proxy_metrics.exit_request()
        else:
            # 流式资金兜底：生成器被丢弃未消费时（客户端断连在首帧前 / 中间件丢响应），
            # 其 finally 的 settle/release 都不会执行，mark_streaming 又挡住了 release，
            # 冻结余额会悬空到重启。给响应挂 on-close 钩子：无论流是否被消费，
            # 结束/丢弃时 close() 都会跑——已闭环则 no-op，未闭环则解冻，不双调账。
            _attach_stream_close(
                _forward_result,
                held,
                reserved,
                redis,
                operation,
                request_id,
                stream_exited=getattr(_forward_result, "stream_exited", {"exited": True}),
                on_exit=lambda: runtime.active_request_ids.discard(request_id),
            )


def _upstream_deadline_seconds(payload: dict) -> float:
    """本次转发的上游墙钟上限（秒）。

    流式：``forward()`` 经 ``eager_stream`` 在**首个事件**返回，故等待它 = 等首字节，
    按 ``upstream_ttfb_timeout`` 收敛——引擎「活着但不回包」（GPU 卡死/死锁）时快速失败，
    而不是挂到 read 空闲上限；首字之后的长间隔由 read_timeout 兜。
    非流式：响应只在生成完毕才回包，首字节与整体不可区分，只能整体限时
    （``upstream_nonstream_timeout``）。
    """
    return settings.upstream_ttfb_timeout if payload.get("stream") else settings.upstream_nonstream_timeout


class UpstreamDeadlineExceeded(httpx.ReadTimeout):
    """平台侧墙钟预算耗尽（:func:`_upstream_deadline_seconds`），非上游 socket 读超时。

    长上下文 prefill、慢首字节这类「本请求超出我们的预算」不是端点死了——
    端点熔断不按端点级计（见 _forward_with_failover 的分类）。"""


async def _forward_bounded(
    adapter, client, target, *, alias_name: str, payload: dict, timeout: float, passthrough_headers: dict | None = None
):
    """带墙钟上限的转发（见 :func:`_upstream_deadline_seconds`）。

    超时归一成 ``UpstreamDeadlineExceeded``（``httpx.ReadTimeout`` 子类）：走既有的
    upstream_errors → 504 upstream_timeout、失败计量落账三件套，不必给上层再加一条
    异常分支；子类标记让熔断分类能区分「我们的预算耗尽」与「上游真读超时」。
    """
    try:
        return await asyncio.wait_for(
            adapter.forward(
                client, target, alias_name=alias_name, payload=payload, passthrough_headers=passthrough_headers
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise UpstreamDeadlineExceeded(
            f"upstream no response within {timeout:.0f}s (stream={bool(payload.get('stream'))})"
        ) from e


async def _forward_with_failover(
    runtime,
    redis,
    ctx,
    entry,
    held,
    reserved,
    adapter,
    body,
    operation,
    request_id,
    start,
    get_client,
    passthrough_headers,
):
    """协议过滤 → 选择器 → failover 循环 → 整形 → 结算/记账（guard 由调用方兜底）。"""
    # 协议内过滤（不做协议转换）：/v1/chat/completions 只选 openai、/v1/messages 只选 anthropic
    candidates = _protocol_candidates(entry, adapter)
    if not candidates:
        return reject_response(
            {"key_id": ctx.key_id, "tenant_id": ctx.tenant_id, "model_alias": entry.name},
            operation,
            error_response(
                adapter.protocol,
                503,
                f"no available endpoint for {entry.name!r} ({adapter.protocol})",
                "service_unavailable",
                "no_available_endpoint",
            ),
            "no_endpoint",
        )

    # 会话前缀边界（内容派生；无会话语义的操作返回 None）+ 亲和作用域（路由名 + 协议，
    # 避免两条路由的端点池在同一个前缀 hash 上互相串味）。亲和关着时消费侧都不看，
    # 不白付哈希开销
    hashes = compute_boundary_hashes(operation, body) if runtime.selector.affinity_enabled else None
    scope = f"{entry.name}:{adapter.protocol}"

    deadline = _upstream_deadline_seconds(body)
    max_retries = entry.failover_max_retries if entry.failover_enabled else 0
    # strict（缓存优先）：亲和没有给出可用目标时选择器直接返回 None（快速失败），
    # 不甩到别的端点；亲和命中的请求失败后也不再换端（见循环内 break）
    strict = settings.affinity_failover_policy == AffinityFailoverPolicy.STRICT.value
    selection = runtime.selector.select(candidates, hashes=hashes, scope=scope, strict=strict)
    attempts = 1 + max_retries

    tried: frozenset[int] = frozenset()
    last_error: Exception | None = None
    last_target: RouteTarget | None = None
    last_reason: str | None = None
    for attempt in range(attempts):
        if selection is None:
            break
        target = selection.target
        # 本次尝试的选路出处：触发时机看是不是第 2 次起（上游已失败过），机制看亲和是否命中
        reason = route_reason(retried=attempt > 0, via_affinity=selection.via_affinity)
        tried = tried | {target.endpoint_id}
        client = get_client(target.api_base, target.api_key)
        try:
            result = await _forward_bounded(
                adapter,
                client,
                target,
                alias_name=entry.name,
                payload=body,
                timeout=deadline,
                passthrough_headers=passthrough_headers,
            )
        except adapter.upstream_errors as e:
            runtime.selector.report_failure(
                target.endpoint_id, cooldown=_failure_counts_toward_cooldown(e, len(candidates))
            )
            log.warning("upstream error (endpoint=%s op=%s): %r", target.endpoint_id, operation, e)
            last_error = e
            last_target = target  # 失败落账需记录最后失败的端点（与 last_error 同源）
            last_reason = reason
            # strict（缓存优先）且本次目标来自亲和：不换端，快速失败，不把会话甩到
            # 别的上游。rebind 与冷会话（无缓存可保）的换端仍带 affinity：刚失败的
            # 那个已被 exclude 挡掉，若同一会话还有绑到其他端点的较短边界，那端至少
            # 缓存着一段前缀，比随机换一个更好
            if strict and selection.via_affinity:
                break
            selection = runtime.selector.select(candidates, exclude=tried, hashes=hashes, scope=scope)
            continue
        # 记成功与写亲和延迟到「确有产出」之后：非流式 forward 返回即有完整响应；
        # 流式 forward 只拿到响应头 + 首事件，真正的收尾（完整消费 / 断流）在生成器
        # finally（见 _stream_body）——那里成功才记、断流计失败。此处先记会把「吐完
        # 首帧即死」的半死端点固定给会话且失败计数被反复清零、永不熔断。
        if result.stream is None:
            _note_endpoint_success(runtime, target, hashes, scope)
        return await respond(
            runtime,
            redis,
            ctx,
            entry,
            held,
            reserved,
            target,
            adapter,
            result,
            body,
            operation,
            request_id,
            start,
            retry_count=len(tried) - 1,
            route_reason=reason,
            hashes=hashes,
            scope=scope,
        )

    if last_error is None:
        # 全程没取到目标（候选全冷却 / 全被本轮排除）：从未触达上游，走 rejected 落账
        return reject_response(
            {"key_id": ctx.key_id, "tenant_id": ctx.tenant_id, "model_alias": entry.name},
            operation,
            error_response(
                adapter.protocol,
                503,
                f"no available endpoint for {entry.name!r}",
                "service_unavailable",
                "no_available_endpoint",
            ),
            "no_endpoint",
        )

    # failover 耗尽：真实到过上游的失败也落账（记录最终流向 + 重试次数），
    # 计量行不因上游失败而缺失。
    # 关落账的协议（records_logs=False，如 count_tokens）同样跳过，与成功路径口径一致。
    if adapter.records_logs:
        spawn_log(
            runtime,
            build_record(
                ctx,
                entry,
                last_target,
                operation,
                adapter.protocol,
                status="error",
                http_status=_upstream_status(last_error),
                request_payload=body,
                error_message=_upstream_error_message(last_error),
                error_detail=_upstream_error_detail(last_error),
                latency_ms=_elapsed_ms(start),
                request_id=request_id,
                retry_count=len(tried) - 1,
                route_reason=last_reason,
            ),
        )
    proxy_metrics.record_outcome(
        proxy_metrics.outcome_labels(ctx, entry, last_target, operation),
        status="error",
        usage=None,
        latency_ms=_elapsed_ms(start),
        retry_count=len(tried) - 1,
    )
    return upstream_error_response(last_error, adapter.protocol)


async def respond(
    runtime,
    redis,
    ctx,
    entry,
    held,
    reserved,
    target,
    adapter,
    result,
    body,
    operation,
    request_id,
    start,
    *,
    retry_count: int,
    route_reason: str,
    hashes: list[str] | None = None,
    scope: str = "",
):
    """收尾：流式逐帧转发 + 结束后落账，非流式整形返回 + 落账。

    ``hashes`` / ``scope`` 只为流式路径传递：成功记账（report_success + 亲和改绑）
    延迟到生成器收尾（_stream_body 的 finally）——成功判定标准是「流走完了」，
    不是「响应头拿到了」。
    """
    if result.stream is not None:
        # 结算责任移交流式生成器（其 finally 必结算）：外层 finally 不再兜底退款，
        # 否则生成器被消费前 release 全额退、消费后 settle 再调账 = 双重调账
        if held is not None:
            held.mark_streaming()
        # 在途计数释放的共享标记：_stream_body 的 finally 置 True（正常收尾/断连），
        # 挂到响应实例上供 _attach_stream_close 读取——生成器被丢弃未消费时标记仍为
        # False，guard 才补那次 exit_request（否则正常路径会 double dec，gauge 变负）。
        stream_exited = {"exited": False}
        response = StreamingResponse(
            _stream_body(
                runtime,
                redis,
                ctx,
                entry,
                held,
                reserved,
                target,
                adapter,
                result,
                body,
                operation,
                request_id,
                start,
                retry_count=retry_count,
                route_reason=route_reason,
                hashes=hashes,
                scope=scope,
                stream_exited=stream_exited,
            ),
            media_type="text/event-stream",
        )
        response.stream_exited = stream_exited
        return response
    await asyncio.gather(
        settle_usage(redis, ctx, entry.name, result.usage, reserved),
        _settle_billing(held, entry, target, adapter, result.usage, operation, request_id),
    )
    labels = proxy_metrics.outcome_labels(ctx, entry, target, operation)
    if adapter.records_logs:
        spawn_log(
            runtime,
            build_record(
                ctx,
                entry,
                target,
                operation,
                adapter.protocol,
                usage=result.usage,
                request_payload=body,
                response_payload=result.body,
                finish_reason=result.finish_reason,
                latency_ms=_elapsed_ms(start),
                request_id=request_id,
                retry_count=retry_count,
                route_reason=route_reason,
            ),
        )
    proxy_metrics.record_outcome(
        labels,
        status="success",
        usage=result.usage,
        latency_ms=_elapsed_ms(start),
        retry_count=retry_count,
    )
    return JSONResponse(result.body)


def _complete_stream_usage(
    usage: dict | None, billable_parts: list[str], body: dict, operation: str, status: str
) -> tuple[dict, bool]:
    """补全流式用量，返回 (usage, 是否使用估算)；中断时部分上报值仅是下界。"""
    reported = (usage or {}).get("completion_tokens")
    if reported is None:
        reported = (usage or {}).get("output_tokens")
    # 估算只在这几类情形会被采用（成功且上游已报非零 completion 时必然弃用）——
    # 按需拼接扫描，长流不再为弃用值白付一次 O(输出) 的 join
    need_estimate = reported is None or reported == 0 or status != "success"
    estimated = estimate_text_tokens("".join(billable_parts)) if need_estimate else 0
    result = normalize_anthropic_usage(usage) if operation == "anthropic.messages" else dict(usage or {})
    result = result or {}
    used_estimate = False
    if "prompt_tokens" not in result and "input_tokens" not in result:
        result["prompt_tokens"] = estimate_prompt_tokens(body, operation)
        used_estimate = True
    if reported is None or (estimated > reported and (status != "success" or reported == 0)):
        result["completion_tokens"] = max(reported or 0, estimated)
        result.pop("output_tokens", None)
        used_estimate = True
    result["total_tokens"] = (result.get("prompt_tokens") or result.get("input_tokens") or 0) + (
        result.get("completion_tokens") or result.get("output_tokens") or 0
    )
    return result, used_estimate


async def _stream_body(
    runtime,
    redis,
    ctx,
    entry,
    held,
    reserved,
    target,
    adapter,
    result,
    body,
    operation,
    request_id,
    start,
    *,
    retry_count: int,
    route_reason: str,
    hashes: list[str] | None = None,
    scope: str = "",
    stream_exited: dict,
):
    """流式：逐帧转发，累加 usage/文本；结束或中断（客户端断连/上游断流）都落账结算。

    try/finally 保证：无论正常收尾、客户端断连（GeneratorExit）还是上游异常，
    结算与计量行都执行——中断时 usage 只含已收到部分，结算按已产出退差价，
    计量行记 interrupted 状态。生成器被丢弃未消费时 try/finally 不执行，
    资金由 guard.close() 的兜底解冻兜底（见 _release_if_unsettled）。

    端点健康与亲和的记账也在此收口：流走完才算成功（report_success + 亲和改绑，
    与非流式「forward 返回即成功」不同，forward 在这里只证明响应头 + 首事件）；
    上游断流在 except 分支即按失败计（分类口径同 failover 循环）——半死端点
    （吐完首帧即死）若被记成功，失败计数会反复清零、永不熔断，亲和还把它固定给会话。
    """
    usage: dict | None = None
    parts: list[str] = []
    billable_parts: list[str] = []
    status = "success"
    error_message: str | None = None
    ttft: int | None = None
    finish_reason: str | None = None
    try:
        async for ev in result.stream:
            if ttft is None:
                # 首帧即上游首帧（eager_stream 已在 forward 内拉出第一事件）→ TTFT
                ttft = _elapsed_ms(start)
                proxy_metrics.record_ttft(
                    proxy_metrics.outcome_labels(ctx, entry, target, operation),
                    ttft,
                )
            finish_reason = ev.finish_reason or finish_reason  # 收尾帧才带值，取最后一个
            if ev.usage:
                usage = {**(usage or {}), **ev.usage}
            if ev.text:
                parts.append(ev.text)
                billable_parts.append(ev.text)
            if ev.billable_text:
                billable_parts.append(ev.billable_text)
            yield ev.wire
            if ev.upstream_error:
                status = "error"
                error_message = ev.upstream_error
                runtime.selector.report_failure(target.endpoint_id, cooldown=True)
                break
        # Anthropic 的 message_start 已带占位 output_tokens=1；若没有带 stop_reason
        # 的 message_delta，正常迭代结束也可能只是上游提前 EOF，仍须按已收到内容估算。
        if status == "success" and operation == "anthropic.messages" and finish_reason is None:
            status = "interrupted"
    except (GeneratorExit, asyncio.CancelledError):
        status = "interrupted"
        raise
    except Exception as e:
        status = "error"
        error_message = f"stream failed: {type(e).__name__}"
        runtime.selector.report_failure(
            target.endpoint_id,
            cooldown=_failure_counts_toward_cooldown(e, len(_protocol_candidates(entry, adapter))),
        )
        log.warning("stream died mid-flight (endpoint=%s op=%s): %r", target.endpoint_id, operation, e)
        raise
    finally:
        # async for 不会自动关闭嵌套生成器；断连时必须一路关闭到 httpx.stream。
        close = getattr(result.stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                log.exception("upstream stream close failed (request_id=%s)", request_id)
        # 流式在途计数在此释放（而非 handle_inference_request 外层 finally——那在
        # StreamingResponse 对象返回时就跑，长流全程不算在途，gauge 失真）。
        # 置标记告知 guard 已释放过（guard 的 finally 见标记则不再 dec）。
        stream_exited["exited"] = True
        proxy_metrics.exit_request()
        if status == "success":
            _note_endpoint_success(runtime, target, hashes, scope)
        # usage 缺失（客户端读完即断 / 上游收尾 chunk 丢失）时按已交付内容估算：
        # 否则 compute_cost 按 None→0 结算，白嫖整段输出。计量行的 interrupted
        # 状态即「token 为估算值」的标记，可事后对账。已有部分上报（如 anthropic
        # message_start 只带 input）则只补缺的半边。上游在产出前报错（error 且无
        # 任何已交付内容）时保持 usage=None——没有可归属的生成用量。
        usage_estimated = False
        if status != "error" or usage is not None or billable_parts:
            usage, usage_estimated = _complete_stream_usage(usage, billable_parts, body, operation, status)
        await asyncio.gather(
            settle_usage(redis, ctx, entry.name, usage, reserved),
            _settle_billing(held, entry, target, adapter, usage, operation, request_id),
        )
        labels = proxy_metrics.outcome_labels(ctx, entry, target, operation)
        if adapter.records_logs:
            spawn_log(
                runtime,
                build_record(
                    ctx,
                    entry,
                    target,
                    operation,
                    adapter.protocol,
                    status=status,
                    http_status=200 if status == "success" else None,
                    usage=usage,
                    usage_estimated=usage_estimated,
                    request_payload=body,
                    response_payload={"model": entry.name, "role": "assistant", "content": "".join(parts)},
                    error_message=error_message,
                    latency_ms=_elapsed_ms(start),
                    ttft_ms=ttft,
                    finish_reason=finish_reason,
                    request_id=request_id,
                    retry_count=retry_count,
                    route_reason=route_reason,
                ),
            )
        proxy_metrics.record_outcome(
            labels,
            status=status,
            usage=usage,
            latency_ms=_elapsed_ms(start),
            retry_count=retry_count,
            stream=True,
        )
