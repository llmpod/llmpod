"""ApiKey 签发与生命周期管理（管理端 service）。

写入侧约束：
- 鉴权用 SHA-256 哈希（不可逆）；明文另以可逆加密列持久存储，供管理端随时回显；
- 租户 Key 的 allowed_models ⊆ 该租户可用集（published − active 封禁）（保存即校验）；
  平台 Key（tenant_id=None）跳过租户封禁校验。

失效协议：禁用/启用/删除/变更写 PG 后管理端 DEL 对应 Redis 缓存 key——
key 格式为平台契约（管理端只删不写），DEL 失败由 TTL 兜底。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.config.constants import APIKEY_CACHE_PREFIX
from llmpod.services.tenant_scope import platform_domain_clause, platform_scope_requested
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.core.secrets import encrypt
from llmpod.models import ApiKey, Route
from llmpod.services.model_block_service import active_blocked_route_ids

log = logging.getLogger("llmpod.services.api_key_service")

_PLAINTEXT_PREFIX = "sk-"

# 缓存 TTL 兜底：DEL 失败时的最终一致窗口
APIKEY_CACHE_TTL_SECONDS = 300
# 正常 Key 的回源通行标记不随 300s 快照过期；否则共用出口 IP 的攻击者
# 可以耗尽 miss 额度，迫使真实 Key 在快照过期后也被当作未知 Key 拒绝。
KNOWN_APIKEY_PREFIX = "llmpod:apikey-known:"


class _UnsetType:
    """「未传该参数」哨兵：与显式 ``None``（清空）区分，只用作三态更新参数的默认值。

    具名类型（而非裸 ``object()``）是为了 ``int | None | _UnsetType`` 这类标注可读、日志可辨。
    """

    def __repr__(self) -> str:
        return "UNSET"


#: 三态更新哨兵单例：可空列的参数默认值（未传=不改，显式 None=清空）
UNSET = _UnsetType()


def tri_state(body: BaseModel, field: str) -> Any:
    """请求体三态取值：字段未出现在 body → UNSET（不改）；显式 null → None（清空）；有值 → 原值。

    先取值再判是否出现：字段名写错在 ``getattr`` 处即 AttributeError 响亮失败，而非静默
    退化成 UNSET（「清空无效」bug 的形状）。前提：请求模型不得带会回填字段的 validator，
    否则 model_fields_set 失真。
    """
    value = getattr(body, field)
    return value if field in body.model_fields_set else UNSET


def _generate_plaintext() -> str:
    return _PLAINTEXT_PREFIX + secrets.token_urlsafe(32)


def _hash_plaintext(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def _validate_allowed_models(db: AsyncSession, tenant_id: int | None, allowed_models: list[str]) -> None:
    """写入时强制校验（网关只点查不校验，完整性在此收口）。

    空列表 = 全部模型可用（网关跳过白名单校验），直接返回；否则两层校验拆开：
    别名存在性对所有 Key 生效（allowed_models 引用的名字必须真实存在，否则网关
    运行期点查永远 fail）；封禁排除仅租户 Key 校验（名字已对该租户封禁则拒绝）。
    """
    if not allowed_models:
        return

    routes: list[Route] = list((await db.execute(select(Route).where(Route.name.in_(allowed_models)))).scalars())
    unknown = set(allowed_models) - {r.name for r in routes}
    if unknown:
        raise LLMPodValidationError(f"未知模型: {sorted(unknown)}")

    if tenant_id is None:
        return

    # 封禁集经 model_block_service 单源取（与租户模型路由/看板同口径），不在此复写语义
    blocked_route_ids = await active_blocked_route_ids(db, tenant_id)
    blocked = sorted(r.name for r in routes if r.id in blocked_route_ids)
    if blocked:
        raise LLMPodValidationError(f"模型已对该租户封禁: {blocked}")


async def create_api_key(
    db: AsyncSession,
    redis=None,
    *,
    tenant_id: int | None,
    name: str,
    allowed_models: list[str],
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    total_token_quota: int | None = None,
    model_token_quotas: dict[str, int] | None = None,
    budget_limit: Decimal | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """签发 API Key，返回 (ORM 实例, 明文)。明文可逆加密落库（随时回显）。

    ``allowed_models`` 空列表 = 全部模型可用（不落白名单，网关跳过该层校验；
    租户 Key 仍受 TenantModelBlock 封禁兜底）。
    """
    await _validate_allowed_models(db, tenant_id, allowed_models)

    plaintext = _generate_plaintext()
    key = ApiKey(
        key_hash=_hash_plaintext(plaintext),
        key_plaintext_encrypted=encrypt(plaintext),
        tenant_id=tenant_id,
        name=name,
        allowed_models=allowed_models,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        total_token_quota=total_token_quota,
        model_token_quotas=model_token_quotas or {},
        budget_limit=budget_limit,
        budget_spent=Decimal("0"),
        budget_frozen=Decimal("0"),
        expires_at=expires_at,
        status="active",
    )
    db.add(key)
    await db.flush()
    if redis is not None:
        await redis.set(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}", "1")
    return key, plaintext


async def list_api_keys(
    db: AsyncSession,
    *,
    tenant_id: int | None,
    name: str | None = None,
    status: str | None = None,
    exclude_tenant_scope: bool = False,
) -> list[ApiKey]:
    """Key 列表（筛选：显示名模糊 / 状态）。

    ``tenant_id`` 非空=租户作用域（租户路由必传，跨租户不可见），精确只看该租户；
    ``exclude_tenant_scope=True``（admin 路由不传/传 0 时置位）则平台域：
    ``tenant_id IS NULL OR tenant_id = 0``——直发 Key 恒为 NULL，0 臂与审计日志的
    平台域哨兵口径一致（存量行若真有 0 也不至于凭空消失）。
    签发侧已用 ``ge=1`` 拦 0（0 只可能是历史残留）。不传开关 = 不按租户过滤
    （租户端用法，本就带具体 tenant_id）。按 id 倒序。
    """
    conds = []
    if platform_scope_requested(tenant_id, exclude_tenant_scope=exclude_tenant_scope):
        conds.append(platform_domain_clause(ApiKey.tenant_id))
    elif tenant_id is not None:
        conds.append(ApiKey.tenant_id == tenant_id)
    if name:
        conds.append(ApiKey.name.ilike(f"%{name}%"))
    if status:
        conds.append(ApiKey.status == status)
    stmt = select(ApiKey)
    if conds:
        stmt = stmt.where(*conds)
    return list((await db.execute(stmt.order_by(ApiKey.id.desc()))).scalars())


async def ensure_active_platform_key(db: AsyncSession, *, name: str) -> ApiKey:
    """确保存在一条 active 平台 Key（tenant_id=None）：缺失则创建、已禁用则重新启用。

    管理端「在线调试」转发用它做免选 Key 的直发凭证；只 flush 不 commit（提交由调用方
    request 生命周期负责，与 create/禁用/启用/delete/update 一致）。返回 ORM，明文经 ``key.plaintext``。
    """
    key = (await db.execute(select(ApiKey).where(ApiKey.tenant_id.is_(None), ApiKey.name == name))).scalar_one_or_none()
    if key is None:
        key, _plaintext = await create_api_key(db, tenant_id=None, name=name, allowed_models=[])
        return key
    if key.status != "active":
        key.status = "active"
        await db.flush()
    return key


async def set_api_key_status(db: AsyncSession, redis=None, *, key_id: int, status: str) -> ApiKey:
    """翻转 Key 状态（active ↔ disabled）：PG 更新 + DEL 缓存（失效协议）。幂等：已是目标态直接返回。"""
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise NotFound(f"ApiKey 不存在: id={key_id}")

    if key.status != status:
        key.status = status
        await db.flush()
        if redis is not None:
            await redis.delete(f"{APIKEY_CACHE_PREFIX}{key.key_hash}")
            if status == "active":
                await redis.set(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}", "1")
            else:
                await redis.delete(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}")
    return key


async def disable_api_key(db: AsyncSession, redis=None, *, key_id: int) -> ApiKey:
    """禁用 Key：置 status=disabled（保留记录，可随时重新启用）+ DEL 缓存。"""
    return await set_api_key_status(db, redis, key_id=key_id, status="disabled")


async def enable_api_key(db: AsyncSession, redis=None, *, key_id: int) -> ApiKey:
    """启用 Key：置 status=active + DEL 缓存。"""
    return await set_api_key_status(db, redis, key_id=key_id, status="active")


async def delete_api_key(db: AsyncSession, redis=None, *, key_id: int) -> None:
    """删除 Key：DEL 缓存后彻底移除记录（不可恢复，区别于 disable 的保留记录）。"""
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise NotFound(f"ApiKey 不存在: id={key_id}")

    if redis is not None:
        await redis.delete(f"{APIKEY_CACHE_PREFIX}{key.key_hash}")
        await redis.delete(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}")
    await db.delete(key)


async def update_api_key(
    db: AsyncSession,
    redis=None,
    *,
    key_id: int,
    name: str | None = None,
    allowed_models: list[str] | None = None,
    rpm_limit: int | None | _UnsetType = UNSET,
    tpm_limit: int | None | _UnsetType = UNSET,
    total_token_quota: int | None | _UnsetType = UNSET,
    model_token_quotas: dict[str, int] | None = None,
    budget_limit: Decimal | None | _UnsetType = UNSET,
    expires_at: datetime | None | _UnsetType = UNSET,
) -> ApiKey:
    """调整 Key 配置（限流/配额/授权等）：变更后 DEL 缓存。

    三态语义（仅对可空列 rpm_limit/tpm_limit/total_token_quota/budget_limit/expires_at）：
    参数缺省 ``UNSET`` = 不修改（保持既有调用方「不传即不改」的语义）；显式传 ``None`` =
    清空为「不限 / 永不过期」；传值 = 覆写。
    ``name`` / ``allowed_models`` / ``model_token_quotas`` 是 NOT NULL 列，仍为 ``None`` =
    不修改（传 null 无处可落，前端恒传合法值；allowed_models 的清空信号是显式空列表）。
    """
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise NotFound(f"ApiKey 不存在: id={key_id}")

    if allowed_models is not None:
        await _validate_allowed_models(db, key.tenant_id, allowed_models)
        key.allowed_models = allowed_models
    if name is not None:
        key.name = name
    if rpm_limit is not UNSET:
        key.rpm_limit = rpm_limit
    if tpm_limit is not UNSET:
        key.tpm_limit = tpm_limit
    if total_token_quota is not UNSET:
        key.total_token_quota = total_token_quota
    if model_token_quotas is not None:
        key.model_token_quotas = model_token_quotas
    if budget_limit is not UNSET:
        # 「上限 ≥ 已消费 + 在途冻结」是网关预算冻结闸门的硬前提（租户与平台
        # 直发同一条谓词）：违反会让下一次预扣永远失败。清空（None）= 不限：
        # 无下限可校验（budget_spent 累计用量保留，只 reset_budget 才清零）。
        if budget_limit is not None:
            spent = key.budget_spent or Decimal("0")
            frozen = key.budget_frozen or Decimal("0")
            if budget_limit < spent + frozen:
                raise LLMPodValidationError(
                    f"预算上限({budget_limit})不能小于已消费额({spent})与在途冻结({frozen})之和"
                )
        key.budget_limit = budget_limit
    if expires_at is not UNSET:
        key.expires_at = expires_at
    await db.flush()

    if redis is not None:
        await redis.delete(f"{APIKEY_CACHE_PREFIX}{key.key_hash}")
    return key


async def reset_budget(db: AsyncSession, redis, *, key_id: int) -> ApiKey:
    """重置 Key 的消费/用量累计：``budget_spent`` 清 0 + 配额 Redis 计数器清零。

    只清「按 Key 维度」的两档（key 总量 / key×模型）；模型全局档
    （``llmpod:quota:model:*``）是跨 Key 的平台护栏，不随单 Key 重置动。
    RPM/TPM 是 60s 瞬时窗口，自然滚动，无需清。redis 为 None（测试/降级）时
    只清 DB 侧。

    同时定格**配额重置基线**（当前各模型累计用量，按模型分桶）：request_log
    调用日志行不删，网关启动的 rebuild_quota_counters 从调用日志全量聚合后按基线
    扣成净增量——没有基线，重置会在重建（marker 过期/Redis 清库后首次启动）
    的「就高回填」中被静默撤销。
    """
    from llmpod.models import RequestLog

    key = await db.get(ApiKey, key_id)
    if key is None:
        raise NotFound(f"ApiKey 不存在: id={key_id}")
    key.budget_spent = Decimal("0")

    # 只取两列（别名 + token 数）在 Python 聚合，不 materialize 整行——调用日志行
    # 从不清理，按 Key 累积后整行拉取会把 error_message/error_detail 等 Text
    # 列全部实例化进内存
    rows = (
        await db.execute(select(RequestLog.model_alias, RequestLog.total_tokens).where(RequestLog.api_key_id == key_id))
    ).all()
    baselines: dict[str, int] = {}
    for model_alias, total_tokens in rows:
        if total_tokens:
            baselines[model_alias] = baselines.get(model_alias, 0) + total_tokens
    key.quota_baselines = baselines
    await db.flush()
    if redis is not None:
        # 计数器键格式归网关 limits 单一所有，管理端只借构造函数，不抄布局
        from llmpod.apps.proxyserver.limits import quota_key, quota_keymodel_prefix

        keys = [quota_key(key_id)]
        try:
            async for k in redis.scan_iter(match=quota_keymodel_prefix(key_id)):
                keys.append(k)
            await redis.delete(*keys)
        except Exception as e:
            # 持久配额计数器可能保留旧值并误拒后续请求；记录日志供重试/排查。
            log.warning("reset_budget 清配额计数器失败 key=%s: %r", key_id, e)
    return key


def cache_payload(key: ApiKey) -> dict:
    """缓存 value JSON（跨面契约）：鉴权所需全部信息，只含哈希不含明文。

    budget_limit 序列化为字符串（Decimal 不能直接进 JSON，str 恰是 Decimal 的无损
    往返表示）；网关用它判「平台 Key 预扣要不要进 DB 冻结」，写侧变更走 DEL 失效
    协议（TTL 300s 兜底），与 rpm_limit/status 同口径。
    """
    return {
        "key_id": key.id,
        "tenant_id": key.tenant_id,
        "name": key.name,
        "allowed_models": key.allowed_models,
        "rpm_limit": key.rpm_limit,
        "tpm_limit": key.tpm_limit,
        "total_token_quota": key.total_token_quota,
        "model_token_quotas": key.model_token_quotas,
        "budget_limit": str(key.budget_limit) if key.budget_limit is not None else None,
        "status": key.status,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
    }


async def warm_apikey_cache(db: AsyncSession, redis) -> int:
    """全量预热：PG 全部 Key（含 disabled）写入 Redis，带 TTL 兜底。

    网关启动时调用；disabled 也预热，使网关在缓存命中路径即可拒绝已禁用 Key。
    管理端变更后的即时性由 DEL 失效协议保证，本函数不承担失效职责。
    """
    keys: list[ApiKey] = list((await db.execute(select(ApiKey))).scalars())
    # 每 Key 两条命令入同一条 pipeline 一次刷出（逐 Key await 是 2N 次串行往返，
    # 阻塞网关 lifespan 就绪）；命令语义与逐条发完全一致
    pipe = redis.pipeline(transaction=False)
    for key in keys:
        pipe.set(
            f"{APIKEY_CACHE_PREFIX}{key.key_hash}",
            json.dumps(cache_payload(key), ensure_ascii=False),
            ex=APIKEY_CACHE_TTL_SECONDS,
        )
        if key.status == "active":
            pipe.set(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}", "1")
        else:
            pipe.delete(f"{KNOWN_APIKEY_PREFIX}{key.key_hash}")
    if keys:
        await pipe.execute()
    return len(keys)
