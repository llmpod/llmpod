"""登录暴力破解限速（Redis 计数，用户名与来源 IP 双键）。

检查在 ``authenticate`` 之前：任一键的失败计数达到上限即拒绝本次登录（不进
bcrypt，响应更快也更省 CPU）。失败计数在凭据校验失败后递增，成功登录
仅清用户名键；IP 键保留，用于防止共享来源连续撞库。窗口到期自然复位。

Redis 不可用（未初始化/异常）时全程放行——限速是防爆破加固，不能反过来
把登录可用性绑在 Redis 健康上（会话本身已依赖 Redis，此处失败时登录也进
不来，语义一致）。
"""

from __future__ import annotations

import logging

from llmpod.config.settings import Settings

log = logging.getLogger("llmpod.services.login_throttle")

_USER_PREFIX = "llmpod:loginfail:user:"
_IP_PREFIX = "llmpod:loginfail:ip:"
_ACTIVE_PREFIX = "llmpod:loginactive:"


class LoginThrottled(Exception):
    """失败次数超限，本次登录被拒绝（调用方映射为 AuthError）。"""


class AttemptPermit:
    """并发登录尝试的 Redis 占位，认证及失败计数落账后释放。"""

    def __init__(self, redis=None, keys=None):
        self.redis = redis
        self.keys = list(keys or ())
        self.released = False

    async def release(self):
        if self.released:
            return
        self.released = True
        if self.redis is not None:
            for key in self.keys:
                try:
                    await self.redis.decr(key)
                except Exception:
                    log.exception("login attempt permit release failed")


async def admit(username: str, ip: str | None, settings: Settings) -> AttemptPermit:
    """原子占并发位后检查历史失败；并发爆破不能全部穿过旧计数。"""
    permit = AttemptPermit()
    try:
        redis = _client()
        checks = [(f"{_ACTIVE_PREFIX}user:{username}", settings.login_fail_limit)]
        if ip:
            checks.append((f"{_ACTIVE_PREFIX}ip:{ip}", settings.login_ip_fail_limit))
        permit.redis = redis
        for key, limit in checks:
            count = await redis.incr(key)
            permit.keys.append(key)
            if count == 1:
                await redis.expire(key, settings.login_fail_window_seconds)
            if count > limit:
                raise LoginThrottled()
        await check(username, ip, settings)
        return permit
    except LoginThrottled:
        await permit.release()
        raise
    except Exception:
        await permit.release()
        log.debug("login attempt counters unavailable, skip")
        return AttemptPermit()


def _client():
    from llmpod.db import get_redis

    return get_redis()


async def check(username: str, ip: str | None, settings: Settings) -> None:
    """登录前检查：任一键超限抛 LoginThrottled。"""
    try:
        redis = _client()
        user_count = int(await redis.get(f"{_USER_PREFIX}{username}") or 0)
    except Exception:
        return
    if user_count >= settings.login_fail_limit:
        raise LoginThrottled()
    if ip:
        try:
            ip_count = int(await redis.get(f"{_IP_PREFIX}{ip}") or 0)
        except Exception:
            return
        if ip_count >= settings.login_ip_fail_limit:
            raise LoginThrottled()


async def record_failure(username: str, ip: str | None, settings: Settings) -> None:
    """凭据校验失败后计数（带窗口 TTL，首次写入时设置）。"""
    try:
        redis = _client()
        for key in (f"{_USER_PREFIX}{username}", f"{_IP_PREFIX}{ip}" if ip else None):
            if not key:
                continue
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, settings.login_fail_window_seconds)
    except Exception:
        log.debug("login throttle counters unavailable, skip")


async def clear(username: str, ip: str | None) -> None:
    """成功仅清用户名失败计数；不能让一个有效账号清空同 IP 的攻击计数。"""
    try:
        redis = _client()
        await redis.delete(f"{_USER_PREFIX}{username}")
    except Exception:
        log.debug("login throttle counters unavailable, skip clear")
