"""进程生命周期结束或启动失败时，后台循环先于 DB 关闭。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest


async def test_startup_failure_stops_background_task_before_resources(monkeypatch):
    import llmpod.app as control_plane
    from llmpod.apps.webserver import metrics_scraper
    from llmpod.apps.webserver.services import upload

    events: list[str] = []
    started = asyncio.Event()

    async def initialize(*args):
        events.append("db-open")

    async def close():
        events.append("db-close")

    async def manager_start(*args):
        events.append("manager-start")

    async def manager_stop():
        events.append("manager-stop")

    async def web_start(*args):
        events.append("web-start")

    async def web_stop():
        events.append("web-stop")

    async def sweep_loop():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("sweep-stop")

    async def start_sweep(background_tasks):
        background_tasks.start(sweep_loop, name="test-sweep")
        await started.wait()

    async def fail_metrics(background_tasks, settings):
        raise RuntimeError("metrics setup failed")

    monkeypatch.setattr(control_plane, "setup_logging", lambda *args: None)
    monkeypatch.setattr(control_plane, "init_all_dbs", initialize)
    monkeypatch.setattr(control_plane, "close_all_dbs", close)
    monkeypatch.setattr(control_plane.manager_lifespan, "startup", manager_start)
    monkeypatch.setattr(control_plane.manager_lifespan, "shutdown", manager_stop)
    monkeypatch.setattr(control_plane.webserver_lifespan, "startup", web_start)
    monkeypatch.setattr(control_plane.webserver_lifespan, "shutdown", web_stop)
    monkeypatch.setattr(upload, "start_sweep_task", start_sweep)
    monkeypatch.setattr(metrics_scraper, "start_metrics_scraper_task", fail_metrics)

    with pytest.raises(RuntimeError, match="metrics setup failed"):
        async with control_plane.control_plane_lifespan(None):
            pass

    assert events == [
        "db-open",
        "manager-start",
        "web-start",
        "sweep-stop",
        "manager-stop",
        "web-stop",
        "db-close",
    ]


async def test_proxy_stops_polling_before_db_close(monkeypatch, tmp_path):
    import llmpod.apps.proxyserver.app as proxy_app
    import llmpod.apps.proxyserver.protocol as protocol
    import llmpod.db as db_module

    from llmpod.apps.proxyserver.app import ProxyRuntime, create_proxy_app
    from llmpod.config.settings import settings
    from llmpod.services import api_key_service

    started = asyncio.Event()
    stopped = asyncio.Event()
    closed = asyncio.Event()
    rebuilds = 0

    async def initialize(*args):
        pass

    async def close():
        assert stopped.is_set()
        closed.set()

    @asynccontextmanager
    async def session():
        yield object()

    async def rebuild(self, db):
        nonlocal rebuilds
        rebuilds += 1
        if rebuilds > 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    class Redis:
        async def get(self, key):
            return "1"  # quota marker 存在，跳过重建

    monkeypatch.setattr(db_module, "init_required_dbs", initialize)
    monkeypatch.setattr(db_module, "close_all_dbs", close)
    monkeypatch.setattr(db_module, "get_db_context", session)
    monkeypatch.setattr(db_module, "get_redis", lambda: Redis())
    monkeypatch.setattr(ProxyRuntime, "rebuild", rebuild)
    monkeypatch.setattr(proxy_app, "reset_frozen_state", initialize)
    monkeypatch.setattr(api_key_service, "warm_apikey_cache", initialize)
    monkeypatch.setattr(protocol, "close_shared_clients", initialize)

    cfg = settings.model_copy(
        update={
            "poll_interval_seconds": 0.01,
            "body_sweep_interval_seconds": 3600,
            "hold_reap_interval_seconds": 3600,
            "body_dir": str(tmp_path),
        }
    )
    app = create_proxy_app(cfg)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
    assert stopped.is_set()
    assert closed.is_set()
