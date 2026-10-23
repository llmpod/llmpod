"""登录限速（login_throttle）与用户名枚举时序抹平单测。

Redis 用内存桩（get/incr/expire/delete），经 ``llmpod.db.redis._redis_client``
注入；限流参数用临场 Settings 对象，不依赖全局配置。
"""

from __future__ import annotations

import asyncio
import pytest

from llmpod.config.settings import Settings
from llmpod.db import redis as db_redis
from llmpod.services import login_throttle_service as throttle


class ThrottleRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def decr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) - 1)
        return int(self.store[key])

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


@pytest.fixture
def redis(monkeypatch) -> ThrottleRedis:
    fake = ThrottleRedis()
    monkeypatch.setattr(db_redis, "_redis_client", fake)
    return fake


def _settings(**kw) -> Settings:
    base = dict(login_fail_limit=3, login_ip_fail_limit=5, login_fail_window_seconds=900)
    base.update(kw)
    return Settings(
        **{
            "login_fail_limit": base["login_fail_limit"],
            "login_ip_fail_limit": base["login_ip_fail_limit"],
            "login_fail_window_seconds": base["login_fail_window_seconds"],
        }
    )


class TestThrottle:
    async def test_under_limit_passes(self, redis):
        await throttle.check("alice", "1.2.3.4", _settings())

    async def test_user_key_over_limit_rejected(self, redis):
        s = _settings()
        for _ in range(3):
            await throttle.record_failure("alice", "1.2.3.4", s)
        with pytest.raises(throttle.LoginThrottled):
            await throttle.check("alice", "9.9.9.9", s)  # 换 IP 也拒（用户名键超限）

    async def test_ip_key_over_limit_rejected(self, redis):
        s = _settings()
        for _ in range(5):
            await throttle.record_failure("bob", "1.2.3.4", s)
        with pytest.raises(throttle.LoginThrottled):
            await throttle.check("carol", "1.2.3.4", s)  # 换用户名也拒（IP 键超限）

    async def test_record_sets_ttl_on_first_write_only(self, redis):
        s = _settings()
        await throttle.record_failure("alice", "1.2.3.4", s)
        await throttle.record_failure("alice", "1.2.3.4", s)
        assert redis.ttls["llmpod:loginfail:user:alice"] == 900  # 不重复续期

    async def test_success_does_not_clear_shared_ip_failures(self, redis):
        s = _settings()
        await throttle.record_failure("alice", "1.2.3.4", s)
        await throttle.clear("alice", "1.2.3.4")
        await throttle.check("alice", "1.2.3.4", s)
        assert redis.store["llmpod:loginfail:ip:1.2.3.4"] == "1"

    async def test_parallel_attempts_cannot_all_pass_before_failure_recorded(self, redis):
        settings = _settings(login_fail_limit=3)
        permits = await asyncio.gather(
            *(throttle.admit("alice", "1.2.3.4", settings) for _ in range(20)),
            return_exceptions=True,
        )
        admitted = [p for p in permits if isinstance(p, throttle.AttemptPermit)]
        assert len(admitted) == 3
        assert sum(isinstance(p, throttle.LoginThrottled) for p in permits) == 17
        for permit in admitted:
            await permit.release()

    async def test_redis_unavailable_fail_open(self, monkeypatch):
        """Redis 未初始化（get_redis 抛 ConfigError）→ 检查放行、计数跳过。"""
        await throttle.check("alice", "1.2.3.4", _settings())
        await throttle.record_failure("alice", "1.2.3.4", _settings())
        await throttle.clear("alice", "1.2.3.4")


class TestTimingPad:
    async def test_unknown_user_verifies_dummy_hash(self, monkeypatch):
        """用户不存在也跑一次 bcrypt（时序抹平）——verify_password 被调用。"""

        import llmpod.services.auth_service as auth_service

        calls: list[str] = []

        def _fake_verify(password, pw_hash):
            calls.append(password)
            return False

        async def _to_thread(fn, *a):
            return fn(*a)

        monkeypatch.setattr(auth_service, "verify_password", _fake_verify)
        monkeypatch.setattr(auth_service, "asyncio", type("A", (), {"to_thread": staticmethod(_to_thread)}))

        from llmpod.core.exceptions import AuthError

        with pytest.raises(AuthError) as ei:
            await auth_service.authenticate(_NoExecDb(), "nobody", "pw", None)
        assert ei.value.error_code.value == "INVALID_CREDENTIALS"
        assert calls == ["pw"]  # 哑哈希路径确实执行了 bcrypt


class _NoExecDb:
    """authenticate 前半段只需要 execute 返回空结果集。"""

    async def execute(self, stmt):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()
