"""清理守卫：系统零 ClickHouse 依赖。

这些测试锁定"CH 已从代码库移除"这一事实，防止后续变更把 CH 相关
配置、模块或生命周期钩子悄悄加回来。
"""

from __future__ import annotations

import importlib

import pytest

from llmpod.config.settings import Settings


class TestClickHouseRemoved:
    def test_settings_has_no_ch_fields(self):
        """Settings 不再持有任何 ch_* 配置字段。"""
        ch_fields = [name for name in Settings.model_fields if name.startswith("ch_")]
        assert ch_fields == []

    def test_ch_module_removed(self):
        """llmpod.db.ch 模块已删除。"""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("llmpod.db.ch")

    def test_db_package_no_ch_exports(self):
        """llmpod.db 不再导出 CH 客户端访问器。"""
        import llmpod.db as db_pkg

        assert not hasattr(db_pkg, "get_ch")
        assert not hasattr(db_pkg, "is_ch_enabled")

    def test_lifecycle_has_no_optional_db_hook(self):
        """init_optional_dbs 仅为 CH 存在，随 CH 一并移除。"""
        from llmpod.db import lifecycle

        assert not hasattr(lifecycle, "init_optional_dbs")

    async def test_init_all_dbs_touches_only_pg_and_redis(self):
        """init_all_dbs 只初始化 PG 和 Redis，不触碰任何 CH 初始化。"""

        called: list[str] = []

        async def _fake_pg(settings):
            called.append("pg")

        async def _fake_redis(settings):
            called.append("redis")

        monkeypatch_targets = (
            ("init_pg", _fake_pg),
            ("init_redis", _fake_redis),
        )
        import llmpod.db.lifecycle as lc

        for name, fake in monkeypatch_targets:
            assert hasattr(lc, name), f"lifecycle 应保留 {name}"
            setattr(lc, name, fake)

        await lc.init_all_dbs(None)

        assert called == ["pg", "redis"]
