"""rebuild 的失效语义：快照重建 = 现在生效。

锁定两个不被 TTL 拖尾的行为：回源旁路的旧结论随重建作废（已删路由立即 404）；
维护态绕过 settings 进程 TTL 缓存直读（开关随下一个轮询周期生效）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from _fakes import FakeSession

from llmpod.apps.proxyserver.app import ProxyRuntime
from llmpod.models import ModelEndpoint, Route, RouteBinding, SystemConfig

_NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _seeded_db() -> FakeSession:
    db = FakeSession()
    db.preload(
        Route(
            id=1,
            name="m1",
            status="published",
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            failover_enabled=False,
            failover_max_retries=0,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    db.preload(
        ModelEndpoint(
            id=10,
            deployment_id="dep-1",
            engine_type="vllm",
            protocol="openai",
            api_base="http://engine.test",
            path="/v1/chat/completions",
            api_key=None,
            served_model="internal-m1",
            name="m1",
            context_window=None,
            operations=["generate"],
            capabilities=[],
            enabled=True,
            health_status="healthy",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    db.preload(
        RouteBinding(
            id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True, created_at=_NOW, updated_at=_NOW
        )
    )
    return db


class TestRebuildInvalidation:
    async def test_rebuild_discards_stale_resolve_cache(self):
        """回源正结论随重建作废：删除路由后立即 404，不被 5s TTL 顶回旧条目。"""
        db = _seeded_db()
        runtime = ProxyRuntime()
        await runtime.rebuild(db)  # m3 尚不在快照里

        # 场景回放：发布 m3（快照未轮到）→ 回源点查命中并缓存（发布即时可用路径）
        db.preload(
            Route(
                id=3,
                name="m3",
                status="published",
                input_price_per_mtok=None,
                output_price_per_mtok=None,
                failover_enabled=False,
                failover_max_retries=0,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        db.preload(
            RouteBinding(
                id=3, route_id=3, endpoint_id=10, weight=1, priority=0, enabled=True, created_at=_NOW, updated_at=_NOW
            )
        )
        entry = await runtime.resolve(db, "m3")
        assert entry is not None and entry.name == "m3"
        assert "m3" in runtime._resolved

        # 删除路由 + 下一轮重建：无缓存的旧结论，回源答「查无此名」→ 404 口径
        route3 = next(r for r in db.all(Route) if r.name == "m3")
        await db.delete(route3)
        await runtime.rebuild(db)
        assert await runtime.resolve(db, "m3") is None

    async def test_rebuild_reads_maintenance_fresh(self):
        """维护态绕过 settings 进程 TTL 缓存：重建即所见即所得，开关一个周期内生效。"""
        from llmpod.services.settings_service import clear_settings_cache

        clear_settings_cache()
        db = _seeded_db()
        runtime = ProxyRuntime()
        await runtime.rebuild(db)
        assert runtime.snapshot.maintenance.enabled is False

        settings = SystemConfig(key="settings", value={"maintenanceMode": True}, updated_by=1, updated_at=_NOW)
        db.preload(settings)
        await runtime.rebuild(db)
        assert runtime.snapshot.maintenance.enabled is True

        # 反向再翻一次：关维护也即时
        db._store[SystemConfig] = {}
        await runtime.rebuild(db)
        assert runtime.snapshot.maintenance.enabled is False
        clear_settings_cache()
