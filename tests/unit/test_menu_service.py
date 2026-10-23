"""菜单可见性服务单元测试：两层过滤矩阵、开关清洗、用户自助开关读写。

纯函数（filter_visible_menus / sanitize_disabled）直接矩阵断言；落库路径
（按用户开关读写）用共享 FakeSession 跑 SystemConfig 单行读写，不碰真实 PG。
菜单注册表用真实声明（不 reset——menu_service 的白名单依赖真实全集，reset 后
子集断言失真）。
"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models.system_config import SystemConfig
from llmpod.permission import SCOPE_PLATFORM, SCOPE_TENANT
from llmpod.permission.menu import MenuSpec, declared_menu_paths, menus_by_scope
from llmpod.permission.platform import CLUSTER_READ, NODE_READ
from llmpod.services.menu_service import (
    filter_visible_menus,
    get_user_disabled_menus,
    sanitize_disabled,
    set_user_disabled_menus,
    user_menus_key,
    visible_menus_for,
)
from tests.integration._fakes import FakeSession


def _db_with(*rows) -> FakeSession:
    """空库起手，可选预置若干行（SystemConfig）。"""
    db = FakeSession()
    for row in rows:
        db.preload(row)
    return db


API_KEY = "/admin/api-key"  # required=(platform:api_key:read,)
DASHBOARD = "/admin/dashboard"  # required=(platform:dashboard:read,)
PLAYGROUND = "/admin/playground"  # required=(platform:inference_debug:exec)——注入平台 Key 的入口须授权

T_CONSOLE = "/tenant/console"  # required=(tenant:tenant:read,)
T_API_KEY = "/tenant/api-key"  # required=(tenant:api_key:read,)
T_PLAYGROUND = "/tenant/playground"  # required 为空


class TestFilterVisibleMenus:
    """两层过滤纯函数矩阵：required 判定 / 开关标注。"""

    def _empty_required_paths(self) -> set[str]:
        return {m.path for m in menus_by_scope(SCOPE_PLATFORM) if not m.required}

    def _visible_with(self, code: str) -> set[str]:
        """持某权限时**按声明应当可见**的菜单（required 含该码，或 required 为空）。

        从注册表推期望而不是写死集合：多个菜单可以共享同一个权限码（如三个大屏
        都要求 ``dashboard:read``），写死会让「加菜单」被误报成过滤失效。
        """
        return {m.path for m in menus_by_scope(SCOPE_PLATFORM) if not m.required or code in m.required_codes}

    def test_superuser_full_set_with_switch_annotation(self):
        """超管：跳过 required 判定，全集下发；被开关关掉的标 False 但仍在。"""
        specs = menus_by_scope(SCOPE_PLATFORM)
        out = filter_visible_menus(specs, frozenset({DASHBOARD}), set(), True)
        assert len(out) == len(specs)
        assert out[DASHBOARD] is False
        assert all(v for k, v in out.items() if k != DASHBOARD)

    def test_normal_user_zero_perms_sees_only_empty_required(self):
        """零权限用户：恰可见 required 为空的菜单（该域全员可见），其余不进返回。"""
        specs = menus_by_scope(SCOPE_PLATFORM)
        out = filter_visible_menus(specs, frozenset(), set(), False)
        assert set(out) == self._empty_required_paths()
        assert all(out.values())
        assert PLAYGROUND not in out  # 需 inference_debug:exec，零权限不可见
        assert DASHBOARD not in out

    def test_normal_user_without_required_hidden(self):
        """required 判定：持 dashboard:read → 只开要求该码的菜单，api-key 仍藏。"""
        specs = menus_by_scope(SCOPE_PLATFORM)
        out = filter_visible_menus(specs, frozenset(), {"platform:dashboard:read"}, False)
        assert out[DASHBOARD] is True
        assert API_KEY not in out
        assert set(out) == self._visible_with("platform:dashboard:read")

    def test_normal_user_holding_required_visible(self):
        """持 api_key:read → api-key 菜单可见（required 空的照常搭车）。"""
        specs = menus_by_scope(SCOPE_PLATFORM)
        out = filter_visible_menus(specs, frozenset(), {"platform:api_key:read"}, False)
        assert set(out) == self._visible_with("platform:api_key:read")

    def test_required_or_semantics_synthetic(self):
        """required OR 语义：真实声明全为单权限，用合成 spec 验证持任一即可见。

        菜单权限已从权限模型移除——并集里混入 ``{scope}:menu:`` 形态的码
        不再授予任何可见性（不再是权限，只是无效字符串）。
        """
        spec = MenuSpec(path="/x", title="x", scope=SCOPE_PLATFORM, required=(CLUSTER_READ, NODE_READ))
        out = filter_visible_menus([spec], frozenset(), {CLUSTER_READ.code}, False)
        assert out == {"/x": True}
        out = filter_visible_menus([spec], frozenset(), {NODE_READ.code}, False)
        assert out == {"/x": True}
        out = filter_visible_menus([spec], frozenset(), {"platform:menu:/x"}, False)
        assert out == {}

    def test_visible_but_switched_off_still_present_false(self):
        """开关与可见性正交：有权菜单被关掉仍下发但标 False（呈现决策留前端）。"""
        specs = menus_by_scope(SCOPE_PLATFORM)
        out = filter_visible_menus(
            specs,
            frozenset({API_KEY, PLAYGROUND}),
            {"platform:api_key:read", "platform:inference_debug:exec"},
            False,
        )
        assert out[API_KEY] is False
        assert out[PLAYGROUND] is False


class TestSanitizeDisabled:
    """白名单清洗：只保留当前已声明路径，脏结构兜底空集。"""

    def test_keeps_declared_drops_unknown(self):
        value = {"disabled": [API_KEY, "/admin/nope", DASHBOARD]}
        assert sanitize_disabled(value, SCOPE_PLATFORM) == frozenset({API_KEY, DASHBOARD})

    def test_scope_narrows_whitelist_to_domain(self):
        """scope 收窄：平台口径下租户路径不再是合法项，反之亦然。"""
        value = {"disabled": [T_API_KEY, API_KEY, "/tenant/nope"]}
        assert sanitize_disabled(value, SCOPE_TENANT) == frozenset({T_API_KEY})
        assert sanitize_disabled(value, SCOPE_PLATFORM) == frozenset({API_KEY})

    def test_dirty_structures_return_empty(self):
        assert sanitize_disabled(None) == frozenset()
        assert sanitize_disabled([1, 2]) == frozenset()
        assert sanitize_disabled({"disabled": "x"}) == frozenset()
        assert sanitize_disabled({"disabled": None}) == frozenset()

    def test_non_string_elements_dropped(self):
        assert sanitize_disabled({"disabled": [1, None, API_KEY]}) == frozenset({API_KEY})


class TestUserDisabledMenus:
    """用户自助开关：域收窄白名单、全量覆盖、按用户 key 隔离。"""

    async def test_read_sanitize_restricted_to_scope(self):
        """读按 scope 收窄：同一行里混入的异域路径被静默丢弃。"""
        db = _db_with(
            SystemConfig(
                key=user_menus_key(9),
                value={"disabled": [T_CONSOLE, DASHBOARD, "/tenant/nope"]},
            )
        )
        assert await get_user_disabled_menus(db, 9, SCOPE_TENANT) == frozenset({T_CONSOLE})
        assert await get_user_disabled_menus(db, 9, SCOPE_PLATFORM) == frozenset({DASHBOARD})

    async def test_read_missing_row_returns_empty(self):
        assert await get_user_disabled_menus(_db_with(), 9, SCOPE_TENANT) == frozenset()

    async def test_set_rejects_out_of_scope_and_unknown_paths(self):
        """写侧白名单收窄到 scope：异域路径与未声明路径同样 422。"""
        db = _db_with()
        with pytest.raises(LLMPodValidationError, match="未声明"):
            await set_user_disabled_menus(db, user_id=9, scope=SCOPE_TENANT, paths=[DASHBOARD], updated_by=1)
        with pytest.raises(LLMPodValidationError, match="未声明"):
            await set_user_disabled_menus(db, user_id=9, scope=SCOPE_TENANT, paths=["/tenant/nope"], updated_by=1)

    async def test_set_full_overwrite_persists(self):
        db = _db_with()
        await set_user_disabled_menus(db, user_id=9, scope=SCOPE_TENANT, paths=[T_CONSOLE, T_API_KEY], updated_by=3)
        row = await db.get(SystemConfig, user_menus_key(9))
        assert row.value == {"disabled": sorted([T_CONSOLE, T_API_KEY])}
        assert row.updated_by == 3

        # 二次全量覆盖为空列表 = 该用户全开（非增量合并）
        await set_user_disabled_menus(db, user_id=9, scope=SCOPE_TENANT, paths=[], updated_by=4)
        assert row.value == {"disabled": []}

    async def test_set_updates_existing_row_reused(self):
        """已有行时走更新分支（不重复建行），不同用户 key 互不干扰。"""
        db = _db_with(SystemConfig(key=user_menus_key(9), value={"disabled": [T_CONSOLE]}))
        await set_user_disabled_menus(db, user_id=9, scope=SCOPE_TENANT, paths=[T_API_KEY], updated_by=5)
        assert len(db.all(SystemConfig)) == 1
        assert db.all(SystemConfig)[0].value == {"disabled": [T_API_KEY]}


class TestVisibleMenusFor:
    """组装器：超管短路只查开关；普通用户单查权限并集做 required 判定。"""

    async def test_superuser_skips_permission_queries(self):
        """超管分支不查用户权限（session=None 不触 DB 即通过），仍吃本人开关。"""
        db = _db_with(SystemConfig(key=user_menus_key(1), value={"disabled": [API_KEY]}))
        out = await visible_menus_for(db, scope=SCOPE_PLATFORM, user_id=1, is_superuser=True)
        assert len(out) == len(menus_by_scope(SCOPE_PLATFORM))
        assert out[API_KEY] is False

    async def test_normal_user_visibility_from_permission_union(self, monkeypatch):
        """普通用户：可见性由权限并集做 required 判定。

        并集里混入 ``tenant:menu:...`` 形态的历史码不再授予任何菜单；
        api-key 被本人开关关掉仍下发但标 False；空 required 菜单照常可见。
        """
        db = _db_with(SystemConfig(key=user_menus_key(7), value={"disabled": [API_KEY]}))

        async def fake_perms(_db, _uid):
            return {
                "platform:dashboard:read",
                "platform:api_key:read",
                "platform:inference_debug:exec",
                "tenant:menu:/tenant/console",  # 历史菜单码：已非权限，无效果
            }

        monkeypatch.setattr("llmpod.services.menu_service.get_user_permissions", fake_perms)
        out = await visible_menus_for(db, scope=SCOPE_PLATFORM, user_id=7, is_superuser=False)
        assert out[DASHBOARD] is True
        assert out[API_KEY] is False  # 有权但被开关关掉：仍下发、标 False
        assert "/tenant/console" not in out
        assert PLAYGROUND in out

    async def test_declarations_cover_both_scopes(self):
        """两侧声明齐全（开关白名单的健全性前提）。"""
        assert len(menus_by_scope(SCOPE_PLATFORM)) >= 15
        assert len(menus_by_scope(SCOPE_TENANT)) >= 8
        assert API_KEY in declared_menu_paths()

    async def test_tenant_scope_overlays_user_switch(self):
        """租户域吃本人下线集，超管同样吃开关标注。"""
        db = _db_with(SystemConfig(key=user_menus_key(9), value={"disabled": [T_CONSOLE]}))
        out = await visible_menus_for(db, scope=SCOPE_TENANT, user_id=9, is_superuser=True)
        assert out[T_CONSOLE] is False
        assert out[T_PLAYGROUND] is True

    async def test_tenant_normal_user_overlay_marks_false(self, monkeypatch):
        """非超管叠加：空 required 菜单可见但可被本人开关关掉（仍下发）。"""
        db = _db_with(SystemConfig(key=user_menus_key(9), value={"disabled": [T_PLAYGROUND]}))

        async def fake_perms(_db, _uid):
            return {"tenant:tenant:read"}

        monkeypatch.setattr("llmpod.services.menu_service.get_user_permissions", fake_perms)
        out = await visible_menus_for(db, scope=SCOPE_TENANT, user_id=9, is_superuser=False)
        assert out[T_CONSOLE] is True
        assert out[T_PLAYGROUND] is False

    async def test_platform_scope_uses_user_switch(self):
        """平台域同样吃本人下线集（平台开关已移除，两域统一口径）。"""
        db = _db_with(SystemConfig(key=user_menus_key(1), value={"disabled": [API_KEY]}))
        out = await visible_menus_for(db, scope=SCOPE_PLATFORM, user_id=1, is_superuser=True)
        assert out[API_KEY] is False
        assert out[DASHBOARD] is True
