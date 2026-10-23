"""API 网关鉴权（AuthN）：请求管道第 1 级 + Redis 缓存读侧。

流程：明文 → SHA-256 → Redis 全量缓存查询（miss 回源 PG 点查并回填）→
状态（active）与过期（expires_at）判定。禁用即时性由管理端 DEL 失效协议保证，
TTL 300s 兜底最终一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.config.constants import APIKEY_CACHE_PREFIX
from llmpod.services.api_key_service import APIKEY_CACHE_TTL_SECONDS, KNOWN_APIKEY_PREFIX, cache_payload

from llmpod.models import ApiKey

# 未知 Key 负缓存 TTL（秒）：比正常缓存短得多——新签发的 Key 若撞上刚写下的
# 负缓存（同一 hash 不可能：签发即入库，查必命中），唯一影响是「曾试探过的不
# 存在 Key」再试时不再打 PG，语义无损。
_NEGATIVE_TTL_SECONDS = 60
log = logging.getLogger("llmpod.proxyserver.authn")


def _is_negative_cache(raw: str | None) -> bool:
    """空串哨兵 = 负缓存（PG 查无此 Key）。"""
    return raw == ""


@dataclass(frozen=True)
class ApiKeyContext:
    """鉴权通过后的 Key 上下文（管道后续级消费）。"""

    key_id: int
    tenant_id: int | None
    allowed_models: frozenset[str]
    rpm_limit: int | None
    tpm_limit: int | None
    total_token_quota: int | None
    model_token_quotas: dict[str, int]
    key_name: str = ""
    # Key 预算上限（元，None=不限）：鉴权快照值。平台 Key 的预扣闸门据此跳过
    # 「无上限」的 DB 冻结（省每请求一次行锁写）；进了闸门后的真实判定仍以
    # PG 行上的 budget_limit 为准（新鲜度与管理端 DEL 失效协议同口径）。
    budget_limit: Decimal | None = None

    @property
    def platform_budget_gated(self) -> bool:
        """平台 Key 且快照设了预算上限：预扣/解冻/结算占位释放都按它决定是否进 DB
        （无上限 = 预扣级零 DB 写，结算也免空写）。判定收在这一处，避免三处直读漂移。"""
        return self.tenant_id is None and self.budget_limit is not None


def _is_expired(expires_at_iso: str | None) -> bool:
    if not expires_at_iso:
        return False
    expires = datetime.fromisoformat(expires_at_iso)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def _context_from_payload(payload: dict) -> ApiKeyContext | None:
    """缓存/回源共用的有效性判定：非 active（禁用）或已过期 → None。"""
    if payload.get("status") != "active":
        return None
    if _is_expired(payload.get("expires_at")):
        return None
    raw_limit = payload.get("budget_limit")
    return ApiKeyContext(
        key_id=payload["key_id"],
        tenant_id=payload["tenant_id"],
        allowed_models=frozenset(payload["allowed_models"]),
        rpm_limit=payload["rpm_limit"],
        tpm_limit=payload["tpm_limit"],
        total_token_quota=payload["total_token_quota"],
        model_token_quotas=payload["model_token_quotas"],
        key_name=payload.get("name", ""),
        budget_limit=Decimal(raw_limit) if raw_limit is not None else None,
    )


async def authenticate(
    db: AsyncSession, redis, plaintext: str, *, client_ip: str | None = None
) -> ApiKeyContext | None:
    """校验明文 Key；无效（未知/禁用/过期）返回 None。缓存 miss 回源回填。

    未知 Key（PG 查无）写 60s 负缓存：未鉴权攻击者拿海量随机 Key 逐个打
    Redis-miss → PG 点查是免费放大通道；负缓存把回源频率压到每键每分钟一次。
    命中负缓存与「禁用/过期」一样返回 None（对调用方无差别）。

    Redis 熔断降级：breaker open（或调用抛错）时跳过全部缓存读写与 miss-limiter，
    直接 PG 点查——鉴权**不 fail-open**，降级只丢缓存层不丢校验；回源后的回填
    同样跳过（Redis 不可用时写不进去，别在热路径空转重试）。
    """
    from llmpod.apps.proxyserver.redis_guard import _BYPASS, guarded_redis_get

    if not plaintext.startswith("sk-") or len(plaintext) > 128:
        return None
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    cache_key = f"{APIKEY_CACHE_PREFIX}{key_hash}"

    try:
        raw = await guarded_redis_get(redis, cache_key)
    except Exception:
        # Redis 异常（熔断尚未 open 的抖动窗口）：与 open 同处置——跳过缓存直查
        # PG，鉴权不 fail-open；失败计数已在 guard 内记录，这里只负责降级
        raw = _BYPASS
    if raw is _BYPASS:
        pass  # 熔断 open：跳缓存直查 PG（下方回源路径）
    else:
        if raw is not None:
            if _is_negative_cache(raw):
                return None
            return _context_from_payload(json.loads(raw))

        # 每个未命中 Key 都不同也无法无限放大 PG：按真实 TCP 来源限回源次数。
        try:
            known = await redis.get(f"{KNOWN_APIKEY_PREFIX}{key_hash}")
        except Exception:
            known = None  # miss-limiter 不可用不阻断回源
        if client_ip and not known:
            try:
                miss_key = f"llmpod:apikey-miss:{client_ip}:{int(time.time() // 60)}"
                misses = await redis.incr(miss_key)
                if misses == 1:
                    await redis.expire(miss_key, 120)
                if misses > 60:
                    return None
            except Exception:
                log.warning("API Key miss limiter unavailable", exc_info=True)

    # miss → PG 点查（FakeSession 语义：无 where 裸查后 Python 过滤由调用方测试桩保证）
    row = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))).scalar_one_or_none()
    if row is None:
        if raw is not _BYPASS:
            try:
                await redis.set(cache_key, "", ex=_NEGATIVE_TTL_SECONDS)
            except Exception:
                pass  # 负缓存写失败不影响判定（下次 miss 再试回源）
        return None

    payload = cache_payload(row)
    if raw is not _BYPASS:
        try:
            await redis.set(cache_key, json.dumps(payload, ensure_ascii=False), ex=APIKEY_CACHE_TTL_SECONDS)
            if row.status == "active":
                await redis.set(f"{KNOWN_APIKEY_PREFIX}{key_hash}", "1")
        except Exception:
            log.warning("API Key 缓存回填失败（不影响本次判定）", exc_info=True)
    return _context_from_payload(payload)
