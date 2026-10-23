"""API 网关限流与配额：请求管道第 3/4 级。

- 限流：固定窗口计数器（60s），RPM 请求前 INCR 即检；TPM 请求前查已用、
  响应后按真实 usage 回填（record_usage）。超限 → RateLimitError（429）。
- 配额：三档 Redis 计数器——Key 总量 / Key×模型 / 模型全局；请求前
  「已用 + 预估」判定（配置态按需启用档位；模型全局档暂无配置入口，仅计数），
  响应后 settle 真实 usage。
  错误体注明超限维度与余量（区别于限流的瞬时窗口）。

Redis key 归网关自治（命名前缀 llmpod:rl:* / llmpod:quota:*，不与其他端共享）。
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("llmpod.proxyserver.limits")

_WINDOW_SECONDS = 60


class RateLimitError(Exception):
    """限流触发（路由层转 429，瞬时窗口语义）。"""


class QuotaExceededError(Exception):
    """配额超限（路由层转 429，错误体注明维度）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ReservedUsage:
    """TPM/配额预占 guard：先占位后判定，结算（settle_usage）或失败（release）时归还。

    预占挡「先 GET 判再回填」的并发窗口——并发请求各自读到同一旧值全部放行。
    占位计入 (key, amount) 清单；settle 时结算侧按真实 usage 全量加回，这里把
    占位减掉（净额 = 真实用量）；失败路径全额归还。幂等仅限当前进程内的 guard。
    """

    def __init__(self, request_id: str | None = None):
        # 请求编号随 guard 走：结算失败日志靠它定位计量行/冻结单；None 仅供内部直调
        self.request_id = request_id
        self.entries: list[tuple[str, int]] = []
        self._done = False
        self._lock = asyncio.Lock()

    def add(self, key: str, amount: int) -> None:
        if amount > 0:
            self.entries.append((key, amount))

    @property
    def reserved_any(self) -> bool:
        return bool(self.entries)

    async def settle(self, redis) -> None:
        """结算侧归还占位（真实用量由 QuotaCounters.settle / record_usage 全量加）。"""
        await self._give_back(redis)

    async def release(self, redis) -> None:
        """失败路径全额归还（与 settle 同一动作，语义别名）。"""
        await self._give_back(redis)

    async def _give_back(self, redis) -> None:
        async with self._lock:
            if self._done:
                return
            remaining = []
            for key, amount in self.entries:
                try:
                    await redis.incrby(key, -amount)
                except Exception:
                    remaining.append((key, amount))
                    log.exception("reserved usage give-back failed (key=%s amount=%s)", key, amount)
            self.entries = remaining
            self._done = not remaining
            # Redis 故障不能覆盖原始 429/402，更不能中断流式 finally 里的扣费。
            # 未归还的条目保留，外层 guard 后续 close/release 仍可重试。


def _window_key(now: float) -> int:
    """固定窗口编号：当前分钟。"""
    return int(now // _WINDOW_SECONDS)


def _bump_degraded() -> None:
    """递增降级指标（延迟导入防环；metrics 初始化失败不致命）。"""
    from llmpod.apps.proxyserver import metrics as proxy_metrics

    proxy_metrics.redis_degraded_total.inc()


class RateLimiter:
    def __init__(self, redis, clock=time.time):
        self._redis = redis
        self._clock = clock

    async def check(
        self,
        *,
        key_id: int,
        rpm_limit: int | None,
        tpm_limit: int | None,
        est_tokens: int = 0,
        reserve: ReservedUsage | None = None,
    ) -> None:
        """请求前检查；被拒（429）请求不消耗窗口额度。

        Redis 熔断降级：breaker open 或调用抛错且 ``redis_fail_mode=open`` 时放行
        （限流是软闸，Redis 故障不该把网关打成全量 500；资金闸在 PG 不受影响）；
        ``redis_fail_mode=closed`` 维持硬失败上抛。业务拒绝（RateLimitError）
        不是故障，不参与熔断计数。

        每个计数器都原子占位再判定：INCR/INCRBY 与判定之间没有读旧值的窗口，
        并发请求各自拿到递增后的值，超限即拒——不存在「先 GET 判再 INCR」
        的非原子窗口（并发下全部读到同一旧值、整分钟硬限被绕过）。被拒请求
        随即回滚自己的占位（RPM DECR / TPM INCRBY 负值），计数只反映真实
        放行的请求；TPM 先占 est_tokens，真实用量在 settle 时按 delta 归还
        占位（record_usage 全量加 → 净额 = 真实用量）。

        TPM 排在 RPM 占位之前：被 TPM 拒的请求不占 RPM 名额；反之 RPM 拒时
        需回滚已占的 TPM 位（下面的分支处理）。
        """
        from llmpod.apps.proxyserver.redis_guard import get_breaker
        from llmpod.config.settings import settings

        breaker = get_breaker()
        if not breaker.allow():
            _bump_degraded()
            return
        try:
            await self._check_strict(
                key_id=key_id, rpm_limit=rpm_limit, tpm_limit=tpm_limit, est_tokens=est_tokens, reserve=reserve
            )
            breaker.record_success()
        except RateLimitError:
            raise
        except Exception:
            breaker.record_failure()
            if settings.redis_fail_mode != "open":
                raise
            _bump_degraded()

    async def _check_strict(
        self,
        *,
        key_id: int,
        rpm_limit: int | None,
        tpm_limit: int | None,
        est_tokens: int = 0,
        reserve: ReservedUsage | None = None,
    ) -> None:
        window = _window_key(self._clock())
        if tpm_limit is not None:
            tpm_key = f"llmpod:rl:{key_id}:{window}:tpm"
            used = await self._redis.incrby(tpm_key, est_tokens)
            # 两判据：占位后超限（est>0 时先于真实用量挡住），或占位前已到限
            # （est=0 的空请求也维持「到限即拒」的旧语义）
            if used > tpm_limit or used - est_tokens >= tpm_limit:
                await self._redis.incrby(tpm_key, -est_tokens)
                raise RateLimitError(f"token rate limit exceeded: {used}/{tpm_limit} tok/min")
            if reserve is not None:
                reserve.add(tpm_key, est_tokens)
            # 新建的键统一设 TTL（est=0 的 INCRBY 0 也会建键，不设过期会按
            # 用户×分钟永久残留）
            if used == est_tokens:
                await self._redis.expire(tpm_key, _WINDOW_SECONDS * 2)
        if rpm_limit is not None:
            rpm_key = f"llmpod:rl:{key_id}:{window}:rpm"
            count = await self._redis.incr(rpm_key)
            if count > rpm_limit:
                await self._redis.decr(rpm_key)
                # RPM 拒：回滚本函数先前占下的 TPM 位，被拒请求不消耗任何档位
                if reserve is not None and reserve.entries:
                    await reserve.release(self._redis)
                raise RateLimitError(f"rate limit exceeded: {count - 1}/{rpm_limit} req/min")
            if count == 1:
                await self._redis.expire(rpm_key, _WINDOW_SECONDS)

    async def record_usage(self, *, key_id: int, tokens: int, reserved: ReservedUsage | None = None) -> None:
        """响应后按真实 usage 回填当前窗口 TPM 计数（window 内嵌 key，跨窗口自动归零）。

        ``reserved`` 是请求期占的 TPM 位：全量加真实用量后归还占位，净额正确。
        窗口翻滚的边界（占在 W、结在 W+1）会让 W 多计一份占位、W+1 少计一份
        delta——TPM 只是限流判据（非计费），且 60s TTL 后 W 自然消失，可接受。
        """
        if tokens <= 0:
            return
        window = _window_key(self._clock())
        tpm_key = f"llmpod:rl:{key_id}:{window}:tpm"
        await self._redis.incrby(tpm_key, tokens)
        await self._redis.expire(tpm_key, _WINDOW_SECONDS * 2)
        if reserved is not None:
            await reserved.settle(self._redis)


def quota_key(key_id: int) -> str:
    """Key 总量档计数器键（reset_budget 删计数器与 precheck/settle 共用）。"""
    return f"llmpod:quota:key:{key_id}"


def quota_keymodel_key(key_id: int, model: str) -> str:
    """Key×模型档计数器键。"""
    return f"llmpod:quota:keymodel:{key_id}:{model}"


def quota_keymodel_prefix(key_id: int) -> str:
    """Key×模型档的 scan 前缀（重置时批量发现该 Key 的所有分桶键）。"""
    return f"llmpod:quota:keymodel:{key_id}:*"


class QuotaCounters:
    """三档配额计数器：执行权威来源 = Redis。"""

    def __init__(self, redis):
        self._redis = redis

    async def precheck(
        self,
        *,
        key_id: int,
        model: str,
        estimated_tokens: int,
        total_token_quota: int | None,
        model_token_quota: int | None,
        model_global_quota: int | None,
        reserve: ReservedUsage | None = None,
    ) -> None:
        """请求前「已用 + 预估」逐档占位判定；未配置的档位跳过。超限 raise。

        单个计数器的 INCRBY 是原子的；多个档位之间没有事务，后续档位拒绝时
        尽力回滚已占的档位。结算按真实用量全量加后归还占位，净额 = 真实用量；
        进程崩溃或 Redis 故障时不保证跨档位回滚及请求级幂等。

        模型全局档（model_global_quota）当前无配置入口（调用方恒传 None），
        预检不生效；``settle`` 对该档**仍持续计数**——是平台护栏的观测数据，
        将来放开配置时直接可用，不是死代码。
        """
        checks = [
            (quota_key(key_id), total_token_quota, "key total"),
            (quota_keymodel_key(key_id, model), model_token_quota, f"model {model}"),
            (f"llmpod:quota:model:{model}", model_global_quota, f"model {model} global"),
        ]
        taken: list[str] = []
        initial_entries = len(reserve.entries) if reserve is not None else 0
        try:
            for key, limit, dimension in checks:
                if limit is None:
                    continue
                used = await self._redis.incrby(key, estimated_tokens)
                taken.append(key)
                if reserve is not None:
                    reserve.add(key, estimated_tokens)
                if used > limit:
                    # 展示用已用值回到占位前（INCRBY 返回值含本次预估，再拼
                    # “+ est” 会把预估双计，报错数字虚高一截）
                    raise QuotaExceededError(
                        f"quota exceeded ({dimension}): used {used - estimated_tokens} + est {estimated_tokens} > {limit}"
                    )
        except Exception:
            # 被拒（或 Redis 中途故障）：回滚本函数已占下的位，被拒请求不消耗额度
            failed_rollbacks: list[tuple[str, int]] = []
            for key in taken:
                try:
                    await self._redis.incrby(key, -estimated_tokens)
                except Exception:
                    log.exception("quota reserve rollback failed (key=%s)", key)
                    failed_rollbacks.append((key, estimated_tokens))
            if reserve is not None:
                reserve.entries[initial_entries:] = failed_rollbacks
            raise

    async def settle(self, *, key_id: int, model: str, tokens: int, reserved: ReservedUsage | None = None) -> None:
        """响应后按真实 usage 落账三档计数（模型全局档始终计数，平台护栏）。

        全量加真实用量后归还请求期占位（净额 = 真实用量）。
        """
        if tokens <= 0:
            if reserved is not None:
                await reserved.settle(self._redis)
            return
        await self._redis.incrby(quota_key(key_id), tokens)
        await self._redis.incrby(quota_keymodel_key(key_id, model), tokens)
        await self._redis.incrby(f"llmpod:quota:model:{model}", tokens)
        if reserved is not None:
            await reserved.settle(self._redis)


async def rebuild_quota_counters(db, redis) -> dict[str, int]:
    """Redis 计数器丢失后从 PG 调用日志（request_log）聚合重建三档配额计数。

    全量拉行 + Python 聚合（单机规模调用日志可承受；FakeSession 兼容）。
    返回 {"keys": 写入的计数器键数, "rows": 聚合行数}。

    只「就高不回填」：request_log 是 fire-and-forget，PG 聚合值恒 ≤ 存活中的
    Redis 计数器——若 Redis 计数器仍存活（marker 被误删/驱逐），直接用 max 保留
    更高的一方，避免重建把配额调低、开 bypass 窗口。

    Key 维度两档按「净增量」聚合：聚合值减去该 Key 的 ``quota_baselines``
    （reset_budget 定格的累计用量分桶）。没有这层扣除，已重置的 Key 会在重建
    中被全量调用日志回填、重置被静默撤销。模型全局档是跨 Key 平台护栏，从不随单
    Key 重置，恒全量聚合。
    """
    from sqlalchemy import select

    from llmpod.models import ApiKey, RequestLog

    # 聚合只需三列：整实体拉取会把 error_message/error_detail 等 Text 列全部
    # 实例化（调用日志百万行时启动重建直接吃内存）
    rows = (await db.execute(select(RequestLog.api_key_id, RequestLog.model_alias, RequestLog.total_tokens))).all()
    # 只取 id + 基线两列（api_key 全表整行会带上密文等宽列；启动重建路径白拉）
    key_rows = (await db.execute(select(ApiKey.id, ApiKey.quota_baselines))).all()
    baselines: dict[int, dict[str, int]] = {key_id: dict(b or {}) for key_id, b in key_rows}
    by_key: dict[int, int] = {}
    by_key_model: dict[tuple[int, str], int] = {}
    by_model: dict[str, int] = {}
    for api_key_id, model_alias, total_tokens in rows:
        if total_tokens:
            by_key[api_key_id] = by_key.get(api_key_id, 0) + total_tokens
            by_key_model[(api_key_id, model_alias)] = by_key_model.get((api_key_id, model_alias), 0) + total_tokens
            by_model[model_alias] = by_model.get(model_alias, 0) + total_tokens

    written = 0
    for key_id, total in by_key.items():
        net = total - sum(baselines.get(key_id, {}).values())
        if net > 0 and await _set_if_higher(redis, quota_key(key_id), net):
            written += 1
    for (key_id, model), total in by_key_model.items():
        net = total - baselines.get(key_id, {}).get(model, 0)
        if net > 0 and await _set_if_higher(redis, quota_keymodel_key(key_id, model), net):
            written += 1
    for model, total in by_model.items():
        if await _set_if_higher(redis, f"llmpod:quota:model:{model}", total):
            written += 1
    return {"keys": written, "rows": len(rows)}


async def _set_if_higher(redis, key: str, value: int) -> bool:
    """仅在计数器缺失或现存值更低时写入（避免用较旧的 PG 聚合覆盖存活计数器）。

    返回是否写入。
    """
    existing = await redis.get(key)
    if existing is not None and int(existing) >= value:
        return False
    await redis.set(key, value)
    return True
