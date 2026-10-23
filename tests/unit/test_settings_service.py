"""全局设置服务单元测试：sanitize 白名单 / 三态 merge / TTL 缓存 / 维护判定。"""

from __future__ import annotations


import pytest

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services.settings_service import (
    MaintenanceState,
    clear_settings_cache,
    get_settings,
    get_maintenance,
    sanitize_settings,
    update_settings,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按全局设置服务的查询形态工作：select(SystemConfig).where(key==...) 单点查。"""

    def __init__(self, row=None):
        self.row = row  # SystemConfig 行或 None
        self.added = []

    async def execute(self, stmt):
        # 取 where 条件里的 key 值，与本桩行匹配
        cmp = stmt.whereclause.right.value
        if self.row is not None and self.row.key == cmp:
            return _FakeResult([self.row])
        return _FakeResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


class _Row:
    """SystemConfig 桩（全局设置服务只读 key / value 两列）。"""

    def __init__(self, value):
        self.key = "settings"
        self.value = value
        self.updated_by = None


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


class TestSanitize:
    def test_unknown_keys_dropped(self):
        out = sanitize_settings({"siteName": "x", "evil": 1, "nested": {"a": 1}})
        assert out.siteName == "x"
        assert "evil" not in out.model_dump()

    def test_hex_color_validation(self):
        ok = sanitize_settings({"themeColor": "#1a2b3c", "loginPrimary": "#abc"})
        assert ok.themeColor == "#1a2b3c"
        assert ok.loginPrimary == "#abc"
        bad = sanitize_settings({"themeColor": "red", "loginPrimary": "#12345"})
        assert bad.themeColor is None
        assert bad.loginPrimary is None

    def test_mode_enum(self):
        assert sanitize_settings({"mode": "dark"}).mode == "dark"
        assert sanitize_settings({"mode": "auto"}).mode == "auto"
        assert sanitize_settings({"mode": "blue"}).mode is None

    def test_scheme_whitelist(self):
        """URL 字段带危险 scheme（javascript:/vbscript:/data:text）读侧丢弃（review fix #5）。"""
        for bad in ("javascript:alert(1)", "vbscript:x", "data:text/html;base64,xxx"):
            assert sanitize_settings({"logo": bad}).logo is None
            assert sanitize_settings({"loginBackground": bad}).loginBackground is None
        # 放行：http(s)/data:image、协议相对、站内相对、emoji 文本
        ok = sanitize_settings(
            {
                "logo": "https://a/b.png",
                "loginBackground": "data:image/png;base64,xx",
            }
        )
        assert ok.logo == "https://a/b.png"
        assert ok.loginBackground.startswith("data:image/")
        assert sanitize_settings({"logo": "🚀"}).logo == "🚀"
        assert sanitize_settings({"logo": "/logo.png"}).logo == "/logo.png"
        assert sanitize_settings({"logo": "//cdn.x/logo.png"}).logo == "//cdn.x/logo.png"

    def test_length_limit(self):
        out = sanitize_settings({"siteName": "a" * 101, "footerText": "b" * 200})
        assert out.siteName is None
        assert out.footerText == "b" * 200

    def test_type_mismatch_dropped(self):
        out = sanitize_settings({"siteName": 123, "maintenanceMode": "yes"})
        assert out.siteName is None
        assert out.maintenanceMode is False

    def test_empty_string_normalized_to_none(self):
        out = sanitize_settings({"siteName": "", "mode": ""})
        assert out.siteName is None
        # mode 空串是合法值（=跟随本地偏好），但也归一为 None 统一「未配置」语义
        assert out.mode is None

    def test_non_dict_input(self):
        assert sanitize_settings(None).siteName is None
        assert sanitize_settings("junk").siteName is None


class TestThreeWayMerge:
    async def test_absent_key_kept(self):
        """键缺省=不动：只传 maintenanceMode，其余字段保持库中值。"""
        db = _FakeSession(_Row({"siteName": "保留", "themeColor": "#111111"}))
        out = await update_settings(db, {"maintenanceMode": True}, updated_by=9)
        assert out.siteName == "保留"
        assert out.themeColor == "#111111"
        assert out.maintenanceMode is True
        assert db.added == []  # 已有行走赋值分支
        assert db.row.updated_by == 9

    async def test_null_clears_value(self):
        """null/空串=清空。"""
        db = _FakeSession(_Row({"siteName": "旧", "footerText": "旧脚"}))
        out = await update_settings(db, {"siteName": None, "footerText": ""}, updated_by=1)
        assert out.siteName is None
        assert out.footerText is None

    async def test_value_overwrites(self):
        db = _FakeSession(_Row({"siteName": "旧"}))
        out = await update_settings(db, {"siteName": "新", "themeColor": "#1a2b3c"}, updated_by=1)
        assert out.siteName == "新"
        assert out.themeColor == "#1a2b3c"

    async def test_invalid_value_raises_422(self):
        """写侧非法值 raise LLMPodValidationError，不清空已存合法值（review fix #4）。

        三态契约「有值=覆写」不含「清空」：5 位 hex 手误必须报错让操作员看到，
        不能静默把库中合法主色一起清掉再返回 200。
        """
        db = _FakeSession(_Row({"themeColor": "#1a2b3c", "siteName": "旧"}))
        with pytest.raises(LLMPodValidationError):
            await update_settings(db, {"themeColor": "#12345"}, updated_by=1)
        with pytest.raises(LLMPodValidationError):
            await update_settings(db, {"mode": "blue"}, updated_by=1)
        with pytest.raises(LLMPodValidationError):
            await update_settings(db, {"siteName": "a" * 101}, updated_by=1)
        # 异常不落库：库中合法值原样保留
        assert db.row.value["themeColor"] == "#1a2b3c"

    async def test_write_rejects_dangerous_scheme(self):
        """写侧 URL 字段带危险 scheme 同样 raise（review fix #5）。"""
        db = _FakeSession(_Row({"siteName": "旧"}))
        with pytest.raises(LLMPodValidationError):
            await update_settings(db, {"logo": "javascript:alert(1)"}, updated_by=1)
        with pytest.raises(LLMPodValidationError):
            await update_settings(db, {"loginBackground": "data:text/html;base64,xx"}, updated_by=1)
        # None / 空串仍是合法的「清空」
        out = await update_settings(db, {"logo": None, "footerText": ""}, updated_by=1)
        assert out.logo is None
        assert out.footerText is None

    async def test_merge_base_is_db_row_not_stale_cache(self):
        """合并基=库中当前行而非 TTL 缓存（review fix #2），防并发以过期值互相覆盖。"""
        clear_settings_cache()
        cached_db = _FakeSession(_Row({"themeColor": "#111111"}))
        await get_settings(cached_db)  # 把缓存填成旧值 #111111
        fresh_db = _FakeSession(_Row({"themeColor": "#222222"}))  # 库中已被并发更新为新值
        out = await update_settings(fresh_db, {"maintenanceMode": True}, updated_by=1)
        assert out.themeColor == "#222222"  # 以 DB 行为准，不被缓存旧值覆盖

    async def test_new_row_created_when_missing(self):
        db = _FakeSession(None)
        out = await update_settings(db, {"siteName": "首写"}, updated_by=2)
        assert out.siteName == "首写"
        assert len(db.added) == 1
        assert db.added[0].key == "settings"
        assert db.added[0].updated_by == 2


class TestCache:
    async def test_ttl_hit(self):
        clear_settings_cache()
        db = _FakeSession(_Row({"siteName": "A"}))
        first = await get_settings(db)
        assert first.siteName == "A"
        # TTL 内换 DB 内容也读缓存
        db2 = _FakeSession(_Row({"siteName": "B"}))
        assert (await get_settings(db2)).siteName == "A"

    async def test_write_invalidates(self):
        db = _FakeSession(_Row({"siteName": "A"}))
        await get_settings(db)
        await update_settings(db, {"siteName": "B"}, updated_by=1)
        # 缓存已失效，新值可见
        assert (await get_settings(db)).siteName == "B"

    async def test_empty_result_cached(self):
        """空结果（未配置）也要被缓存住（ts>0 判定，空值不杀缓存）。"""
        db = _FakeSession(None)
        await get_settings(db)
        db2 = _FakeSession(_Row({"siteName": "迟到的写入"}))
        assert (await get_settings(db2)).siteName is None

    async def test_db_failure_falls_back_to_cache(self):
        db = _FakeSession(_Row({"siteName": "旧"}))
        await get_settings(db)

        class _Boom:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        out = await get_settings(_Boom())  # type: ignore[arg-type]
        assert out.siteName == "旧"

    async def test_db_failure_no_cache_returns_empty(self):
        class _Boom:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        clear_settings_cache()
        out = await get_settings(_Boom())  # type: ignore[arg-type]
        assert out.siteName is None


class TestMaintenance:
    async def test_state_read(self):
        db = _FakeSession(_Row({"maintenanceMode": True, "maintenanceMessage": "升级中"}))
        state = await get_maintenance(db)
        assert state.enabled is True
        assert state.message == "升级中"

    async def test_message_default(self):
        db = _FakeSession(_Row({"maintenanceMode": True}))
        state = await get_maintenance(db)
        assert state.message == "系统维护中"

    async def test_disabled_by_default(self):
        assert MaintenanceState().enabled is False
        db = _FakeSession(_Row({}))
        assert (await get_maintenance(db)).enabled is False
