"""版本与更新检查服务单元测试：版本解析 / manifest 契约 / 失败兜底 / TTL 缓存。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

import llmpod
from llmpod.services.version_service import (
    parse_version,
    check_update,
    clear_update_cache,
)

# 官网端点地址与 check_update 内写死的字面量保持一致（断言外呼目标用）
OFFICIAL_MANIFEST_URL = "https://llmpod.ai/api/version/latest"


@pytest.fixture
def manifest_url():
    """外呼目标恒为官网端点（地址写死不可注入；各用例自行替换 httpx.get 断网）。"""
    return OFFICIAL_MANIFEST_URL


class TestParseVersion:
    """日历版本号：yy.m.d，当天追加发布 yy.m.d.n。"""

    def test_calendar_plain(self):
        assert parse_version("26.9.3") == (26, 9, 3, 0)

    def test_calendar_daily_patch(self):
        assert parse_version("26.9.3.1") == (26, 9, 3, 1)

    def test_daily_patch_beats_plain(self):
        # 26.9.3 等价 26.9.3.0，追加发布更大
        assert parse_version("26.9.3.1") > parse_version("26.9.3")

    def test_next_day_beats_any_patch(self):
        # 26.9.4 > 26.9.3.9：日期进位压过任意追加号
        assert parse_version("26.9.4") > parse_version("26.9.3.9")

    def test_v_prefix_and_whitespace(self):
        assert parse_version("  v26.9.3 ") == (26, 9, 3, 0)

    def test_missing_segments(self):
        assert parse_version("26.9") == (26, 9, 0, 0)
        assert parse_version("26") == (26, 0, 0, 0)

    def test_channel_suffix(self):
        assert parse_version("26.9.3-test") == (26, 9, 3, 0)
        assert parse_version("26.9.3.1-beta") == (26, 9, 3, 1)

    @pytest.mark.parametrize("garbage", ["", "abc", "1.x.3", "latest", "1..2", "v", "26.9.3.1.2"])
    def test_garbage_returns_none(self, garbage):
        assert parse_version(garbage) is None


class TestCheckUpdateManifest:
    async def test_newer_version_with_notes(self, monkeypatch, manifest_url):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            assert url == manifest_url
            return httpx.Response(
                200,
                json={
                    "latest_version": "99.9.9",
                    "release_type": "patch",
                    "notes": "1. 新增\n2. 修复",
                    "release_url": "https://x",
                    "published_at": "2026-09-04T10:00:00Z",
                },
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.has_update is True
        assert result.latest_version == "99.9.9"
        assert result.current_version == llmpod.__version__
        assert result.update_check_enabled is True
        assert result.checked_at is not None
        assert result.notes == "1. 新增\n2. 修复"
        assert result.release_url == "https://x"
        assert result.published_at == datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc)
        assert result.release_type == "patch"
        assert result.error_message is None

    async def test_published_at_missing_or_invalid_is_none(self, monkeypatch, manifest_url):
        """published_at 缺失/非法为 None，不影响检查结果。"""
        clear_update_cache()

        def make_response(payload):
            def fake_get(url, timeout=None, follow_redirects=None, headers=None):
                return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

            return fake_get

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", make_response({"latest_version": "99.9.9"}))
        result = await check_update()
        assert result.has_update is True
        assert result.published_at is None

        clear_update_cache()
        import llmpod.services.version_service as svc

        svc._cache = None
        monkeypatch.setattr(
            "llmpod.services.version_service.httpx.get",
            make_response({"latest_version": "99.9.9", "published_at": "not-a-date"}),
        )
        result = await check_update()
        assert result.has_update is True
        assert result.published_at is None
        assert result.error_message is None

    async def test_older_or_equal_version_reports_no_update(self, monkeypatch, manifest_url):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            return httpx.Response(
                200,
                json={"latest_version": "0.1.1", "release_type": "patch"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.has_update is False
        # 无更新时 latest 恒等于 current（前端 hook 判等即「已是最新版本」）
        assert result.latest_version == llmpod.__version__
        assert result.notes is None
        # 回显态描述的是当前版本，不携带新版本类型
        assert result.release_type is None

    async def test_v_prefix_normalized(self, monkeypatch, manifest_url):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            return httpx.Response(200, json={"latest_version": "v99.9.9.1"}, request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.latest_version == "99.9.9.1"

    async def test_request_carries_version_header(self, monkeypatch, manifest_url):
        """外呼必须带 X-LLMPod-Version 头：官网按它查表定通道（缺头按 stable 回答）。"""
        clear_update_cache()
        seen_headers = {}

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            seen_headers.update(headers or {})
            return httpx.Response(200, json={"latest_version": "0.1.1"}, request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        await check_update()
        assert seen_headers.get("X-LLMPod-Version") == llmpod.__version__
        assert seen_headers.get("User-Agent") == f"llmpod/{llmpod.__version__}"


class TestCheckUpdateFailures:
    async def test_unreachable_upstream(self, monkeypatch, manifest_url):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.has_update is False
        assert result.latest_version == llmpod.__version__
        assert result.error_message is not None

    async def test_http_error_status(self, monkeypatch, manifest_url):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            return httpx.Response(500, request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.has_update is False
        assert result.error_message is not None

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # 缺 latest_version
            {"latest_version": ""},  # 空版本
            {"latest_version": "not-a-version"},  # 非法版本
            {"latest_version": ["26.9.4"]},  # 类型不对
        ],
    )
    async def test_malformed_manifest(self, monkeypatch, manifest_url, payload):
        clear_update_cache()

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        result = await check_update()
        assert result.has_update is False
        assert result.error_message is not None

    async def test_failure_keeps_last_checked_at(self, monkeypatch, manifest_url):
        """上游失败时保留最近一次成功检查时间，方便前端展示「上次检查」。"""
        clear_update_cache()

        def ok(url, timeout=None, follow_redirects=None, headers=None):
            return httpx.Response(200, json={"latest_version": "0.1.1"}, request=httpx.Request("GET", url))

        def bad(url, timeout=None, follow_redirects=None, headers=None):
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", ok)
        ok_result = await check_update()
        assert ok_result.checked_at is not None

        # 过期缓存后失败（时间戳归零过期，但保留上次结果供 checked_at 回显）
        import llmpod.services.version_service as svc

        svc._cache = (0.0, svc._cache[1])
        monkeypatch.setattr("llmpod.services.version_service.httpx.get", bad)
        fail_result = await check_update()
        assert fail_result.checked_at == ok_result.checked_at


class TestCheckUpdateCache:
    async def test_ttl_window_single_fetch(self, monkeypatch, manifest_url):
        """TTL 窗口内重复调用只打一次上游。"""
        clear_update_cache()
        calls = []

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            calls.append(url)
            return httpx.Response(200, json={"latest_version": "0.1.1"}, request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        await check_update()
        await check_update()
        await check_update()
        assert len(calls) == 1

        # 缓存过期后再次触发外呼
        import llmpod.services.version_service as svc

        svc._cache = None
        await check_update()
        assert len(calls) == 2

    async def test_failure_negatively_cached(self, monkeypatch, manifest_url):
        """失败走短 TTL 负缓存：故障期不逐请求打上游，过期后自动重试。"""
        clear_update_cache()
        calls = []

        def fake_get(url, timeout=None, follow_redirects=None, headers=None):
            calls.append(url)
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

        monkeypatch.setattr("llmpod.services.version_service.httpx.get", fake_get)
        await check_update()
        await check_update()
        assert len(calls) == 1  # 负缓存窗口内第二次调用不再外呼

        import llmpod.services.version_service as svc

        svc._cache = None
        await check_update()
        assert len(calls) == 2  # 负缓存过期（测试模拟清空）后重试


class TestRecordBootVersion:
    """启动版本对账：方向判定 + SystemConfig 读写 + 已有行更新。"""

    @staticmethod
    def _make_row(value: dict):
        from llmpod.models.system_config import SystemConfig

        return SystemConfig(key="system_version", value=value)

    @staticmethod
    def _make_session(row):
        from tests.integration._fakes import FakeSession

        session = FakeSession()
        if row is not None:
            session.preload(row)
        return session

    async def test_initial_boot(self, monkeypatch):
        """无历史记录（首装/老库首次接入）：direction=initial，写入当前版本。"""
        import llmpod as llmpod_pkg
        from llmpod.services import version_service as svc

        session = self._make_session(None)
        monkeypatch.setattr(llmpod_pkg, "__version__", "26.9.4", raising=False)
        boot = await svc.record_boot_version(session)
        assert boot.direction == "initial"
        assert boot.previous_version is None
        assert boot.current_version == "26.9.4"
        from llmpod.models.system_config import SystemConfig

        assert len(session.all(SystemConfig)) == 1  # 新建 SystemConfig 行

    async def test_direction_compare(self):
        from llmpod.services.version_service import _compare_direction

        assert _compare_direction("26.9.3", "26.9.3") == "same"
        assert _compare_direction(None, "26.9.3") == "initial"
        assert _compare_direction("26.9.3", "26.9.3.1") == "upgrade"
        assert _compare_direction("26.9.3.1", "26.9.3") == "downgrade"
        assert _compare_direction("26.9.3", "26.10.1") == "upgrade"
        # 旧记录是脏数据（不可解析）时：版本串变了按升级，没变按 same
        assert _compare_direction("garbage", "26.9.3") == "upgrade"
        assert _compare_direction("garbage", "garbage") == "same"

    async def test_existing_row_updated_in_place(self, monkeypatch):
        """已有 system_version 行：原地更新 value，不 add 新行；previous 留档。"""
        import llmpod as llmpod_pkg
        from llmpod.models.system_config import SystemConfig
        from llmpod.services import version_service as svc

        row = self._make_row(
            {"version": "26.9.3", "recorded_at": "2026-01-01T00:00:00+00:00", "previous_version": None}
        )
        session = self._make_session(row)
        monkeypatch.setattr(llmpod_pkg, "__version__", "26.9.3.1", raising=False)
        boot = await svc.record_boot_version(session)
        assert boot.direction == "upgrade"
        assert boot.previous_version == "26.9.3"
        assert row.value["version"] == "26.9.3.1"
        assert row.value["previous_version"] == "26.9.3"
        assert len(session.all(SystemConfig)) == 1  # 原地更新，不 add 新行
