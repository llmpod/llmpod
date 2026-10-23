"""audit_service 单元测试：脱敏 / resource_id 三来源 / tenant_id 归属 / 装饰器 fail-fast 与落账。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from llmpod.apps.webserver.auth.session import SessionData
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.models.audit_log import AuditLog
from llmpod.services import audit_service
from llmpod.services.audit_service import (
    _resolve_resource_id,
    _resolve_tenant_id,
    audit_action,
    sanitize_params,
)


class _FakeResult:
    """最小 execute() 返回桩：scalar() 给 total，scalars() 给行。"""

    def __init__(self, rows):
        self._rows = rows

    def scalar(self):
        return len(self._rows)

    def scalars(self):
        return list(self._rows)


class _FakeSession:
    """捕获 where 条件的 session 桩：记录 select 链上的过滤表达式。"""

    def __init__(self):
        self.captured_conds = []

    async def execute(self, stmt):
        # select(...) 的 where 条件在 stmt.whereclause；None 表示无条件。
        # and_() 单条件时直接返回 BinaryExpression（无 .clauses），统一包成元组。
        where = getattr(stmt, "whereclause", None)
        if where is None:
            conds: tuple = ()
        elif hasattr(where, "clauses"):
            conds = tuple(where.clauses)
        else:
            conds = (where,)
        self.captured_conds.extend(str(c) for c in conds)
        return _FakeResult([])


class TestSanitizeParams:
    """参数脱敏：敏感键命中 / 大小写 / 嵌套 / 4KB 截断。"""

    def test_sensitive_keys_masked(self):
        out = sanitize_params({"username": "alice", "password": "s3cret"})
        assert out["username"] == "alice"
        assert out["password"] == "***"

    def test_case_insensitive_and_substring(self):
        out = sanitize_params({"PASSWD": "x", "access_token": "t", "Authorization": "Bearer z"})
        assert out["PASSWD"] == "***"
        assert out["access_token"] == "***"
        assert out["Authorization"] == "***"

    def test_api_key_masked(self):
        """api_key 是上游凭据（端点登记/编辑/探测的请求体都带它）——漏进审计行就是
        明文持久化到可导出的表。"""
        out = sanitize_params({"body": {"endpoint_id": 7, "api_key": "sk-live-xxx"}, "apikey": "k", "api-key": "k"})
        assert out["body"]["endpoint_id"] == 7
        assert out["body"]["api_key"] == "***"
        assert out["apikey"] == "***"
        assert out["api-key"] == "***"

    def test_source_auth_masked(self):
        """source_auth 是模型来源凭据（HF token 等），键名不含 token/secret 片段——
        靠 ``auth`` 片段命中打码；create/update model 的请求体整个 dump 进审计。"""
        out = sanitize_params({"request": {"name": "qwen", "source_auth": "hf_xxx"}})
        assert out["request"]["source_auth"] == "***"
        assert out["request"]["name"] == "qwen"

    def test_nested_dict_recursive(self):
        out = sanitize_params({"a": {"b": {"secret": "v", "ok": 1}, "token": "t"}})
        assert out["a"]["b"]["secret"] == "***"
        assert out["a"]["b"]["ok"] == 1
        assert out["a"]["token"] == "***"

    def test_list_recursive(self):
        out = sanitize_params({"items": [{"password": "p"}, {"name": "n"}]})
        assert out["items"][0]["password"] == "***"
        assert out["items"][1]["name"] == "n"

    def test_non_sensitive_untouched(self):
        out = sanitize_params({"name": "m", "count": 3, "enabled": True})
        assert out == {"name": "m", "count": 3, "enabled": True}

    def test_truncate_over_4kb(self):
        big = {"blob": "x" * 6000}
        out = sanitize_params(big)
        assert out["_truncated"] is True
        assert len(out["data"]) <= 4096

    def test_truncate_by_bytes_not_chars(self):
        # 多字节 UTF-8（CJK，每字 3 字节）：截断须按字节计，避免超 4KB 上限
        big = {"blob": "汉" * 6000}
        out = sanitize_params(big)
        assert out["_truncated"] is True
        assert len(out["data"].encode("utf-8")) <= 4096


class TestResolveResourceId:
    """resource_id 三来源：callable / 参数名 / Result.data.id 兜底。"""

    def test_callable(self):
        r = _resolve_resource_id(lambda res: res["data"]["id"], {}, {"data": {"id": 42}})
        assert r == "42"

    def test_callable_with_none_result(self):
        # 失败路径 result=None：不调用 callable（资源未创建），返回 None，不抛异常
        r = _resolve_resource_id(lambda res: res["data"]["id"], {}, None)
        assert r is None

    def test_param_name(self):
        r = _resolve_resource_id("user_id", {"user_id": 7}, None)
        assert r == "7"

    def test_param_name_missing(self):
        r = _resolve_resource_id("user_id", {}, None)
        assert r is None

    def test_none_fallback_from_dict(self):
        r = _resolve_resource_id(None, {}, {"code": 200, "data": {"id": 99}})
        assert r == "99"

    def test_none_fallback_from_object(self):
        class Obj:
            id = 5

        r = _resolve_resource_id(None, {}, {"data": Obj()})
        assert r == "5"

    def test_none_no_id(self):
        r = _resolve_resource_id(None, {}, {"data": {}})
        assert r is None


class TestResolveTenantId:
    """tenant_id 归属：resolver / arg / 租户域默认 / 平台域默认。"""

    def test_resolver(self):
        assert _resolve_tenant_id(None, lambda r: r["data"]["tenant_id"], {}, None, {"data": {"tenant_id": 5}}) == 5

    def test_arg(self):
        assert _resolve_tenant_id("tenant_id", None, {"tenant_id": 8}, None, None) == 8

    def test_tenant_domain_default(self):
        user = SessionData(user_id=1, tenant_id=3, access_domain="tenant")
        assert _resolve_tenant_id(None, None, {}, user, None) == 3

    def test_platform_domain_default_none(self):
        user = SessionData(user_id=1, tenant_id=0, access_domain="platform")
        assert _resolve_tenant_id(None, None, {}, user, None) is None

    def test_fixed_wins_over_other_sources(self):
        """固定归属优先级最高：给了它就不看 arg / resolver / 默认。"""
        assert _resolve_tenant_id("tenant_id", None, {"tenant_id": 8}, None, None, 0) == 0
        assert _resolve_tenant_id(None, lambda r: 7, {}, None, {"data": {}}, 0) == 0

    def test_fixed_survives_failure_path(self):
        """失败路径（result=None）resolver 会跳过，固定归属不受影响——这正是它存在的理由。"""
        platform_user = SessionData(user_id=1, tenant_id=0, access_domain="platform")
        assert _resolve_tenant_id(None, lambda r: 7, {}, platform_user, None, 0) == 0
        # 对照：同一入参不给固定归属时退化成默认归属（平台用户 → None）
        assert _resolve_tenant_id(None, lambda r: 7, {}, platform_user, None) is None

    def test_owner_is_actor(self):
        """资源即操作者本人的数据：归属 = 操作者所属租户，平台域账号记 0（不退化成无归属）。"""
        platform_user = SessionData(user_id=1, tenant_id=0, access_domain="platform")
        tenant_user = SessionData(user_id=2, tenant_id=9, access_domain="tenant")
        assert _resolve_tenant_id(None, None, {}, platform_user, None, None, True) == 0
        assert _resolve_tenant_id(None, None, {}, tenant_user, None, None, True) == 9


class TestListAuditLogsScope:
    """admin 视角开关：exclude_tenant_scope 在 tenant_id 缺省时过滤平台域行。

    平台域 = ``tenant_id IS NULL OR tenant_id = 0``（无租户维度 + 平台租户自身），
    ``tenant_id=0`` 与不传等价；``_FakeSession`` 会把 ``or_`` 的 clauses 摊开逐条
    记账，故断言按「两个条件都在」写。
    """

    @pytest.mark.asyncio
    async def test_platform_scope_when_no_tenant_filter(self):
        session = _FakeSession()
        await audit_service.list_audit_logs(session, page=1, page_size=20, exclude_tenant_scope=True)
        assert any("IS NULL" in c for c in session.captured_conds)
        assert any("tenant_id = " in c and "IS NULL" not in c for c in session.captured_conds)

    @pytest.mark.asyncio
    async def test_tenant_zero_means_platform_scope(self):
        """平台视角下传 tenant_id=0 = 平台域（与不传同解），不是「只看平台租户那些行」。"""
        session = _FakeSession()
        await audit_service.list_audit_logs(session, page=1, page_size=20, tenant_id=0, exclude_tenant_scope=True)
        assert any("IS NULL" in c for c in session.captured_conds)
        assert any("tenant_id = " in c and "IS NULL" not in c for c in session.captured_conds)

    @pytest.mark.asyncio
    async def test_tenant_zero_stays_strict_without_platform_scope(self):
        """租户端不置位该开关：即便 tenant_id=0 也只精确等值，不放宽成平台域（防越权）。

        ``access_domain`` 由角色归属判定，不保证与 ``user.tenant_id`` 一致——租户域
        调用方理论上可能带着 tenant_id=0 进来，此处固定不得放宽。
        """
        session = _FakeSession()
        await audit_service.list_audit_logs(session, page=1, page_size=20, tenant_id=0)
        assert not any("IS NULL" in c for c in session.captured_conds)
        assert any("tenant_id = " in c for c in session.captured_conds)

    @pytest.mark.asyncio
    async def test_tenant_filter_wins_over_scope_switch(self):
        session = _FakeSession()
        await audit_service.list_audit_logs(session, page=1, page_size=20, tenant_id=7, exclude_tenant_scope=True)
        assert any("tenant_id" in c and "IS NULL" not in c for c in session.captured_conds)
        assert not any("IS NULL" in c for c in session.captured_conds)

    @pytest.mark.asyncio
    async def test_no_scope_switch_no_null_cond(self):
        session = _FakeSession()
        await audit_service.list_audit_logs(session, page=1, page_size=20)
        assert not any("IS NULL" in c for c in session.captured_conds)


class RecordingSession:
    """独立 session 桩：捕获 add 的 AuditLog，供断言装饰器落账内容。"""

    def __init__(self, records):
        self._records = records

    def add(self, obj):
        self._records.append(obj)

    async def commit(self):
        pass

    async def close(self):
        pass


class TestAuditAction:
    """装饰器：签名 fail-fast、成功/失败落账、审计失败不影响业务。"""

    def test_fail_fast_missing_user(self):
        async def handler(db):
            return {"ok": 1}

        with pytest.raises(TypeError):
            audit_action(action="create", resource="user")(handler)

    def test_fail_fast_conflicting_tenant_sources(self):
        """归属来源互斥：固定归属与 arg/resolver 同时给，装饰期即抛（防静默取错来源）。"""

        async def handler(user, db):
            return {"ok": 1}

        with pytest.raises(TypeError):
            audit_action(action="create", resource="user", fixed_tenant_id=0, tenant_id_arg="tenant_id")(handler)
        with pytest.raises(TypeError):
            audit_action(action="create", resource="user", fixed_tenant_id=0, tenant_id_resolver=lambda r: 1)(handler)
        with pytest.raises(TypeError):
            audit_action(action="create", resource="user", owner_is_actor=True, tenant_id_arg="tenant_id")(handler)
        with pytest.raises(TypeError):
            audit_action(action="create", resource="user", owner_is_actor=True, fixed_tenant_id=0)(handler)

    @pytest.mark.asyncio
    async def test_owner_is_actor_writes_platform_tenant(self, monkeypatch):
        """自助改菜单一类「本人数据」端点：平台域账号落 0，租户域账号落本租户。"""
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        @audit_action(action="update", resource="menus", resource_id=lambda r: "k", owner_is_actor=True)
        async def handler(user, db):
            return {"code": 200, "data": {"user_id": user.user_id}}

        await handler(user=SessionData(user_id=1, tenant_id=0, access_domain="platform"), db=None)
        assert records[-1].tenant_id == 0
        await handler(user=SessionData(user_id=2, tenant_id=9, access_domain="tenant"), db=None)
        assert records[-1].tenant_id == 9

    @pytest.mark.asyncio
    async def test_fixed_tenant_id_on_success_and_failure(self, monkeypatch):
        """平台归属资源（平台用户/角色管理）成功与失败都落 0，不再落 NULL。

        失败路径尤其重要：换用 tenant_id_resolver 会因拿不到返回值退化成 NULL，
        同一资源的行会随成败落在两个桶里。
        """
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))
        platform_user = SessionData(user_id=1, tenant_id=0, access_domain="platform", username="alice")

        @audit_action(action="update", resource="user", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
        async def handler(user, db, user_id):
            return {"code": 200, "data": {"id": user_id}}

        await handler(user=platform_user, db=None, user_id=5)
        assert records[-1].tenant_id == PLATFORM_TENANT_ID

        class Boom(Exception):
            pass

        @audit_action(action="delete", resource="user", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
        async def failing(user, db, user_id):
            raise Boom("nope")

        with pytest.raises(Boom):
            await failing(user=platform_user, db=None, user_id=5)
        assert records[-1].result == "failed"
        assert records[-1].tenant_id == PLATFORM_TENANT_ID

    @pytest.mark.asyncio
    async def test_success_writes_row(self, monkeypatch):
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        @audit_action(action="create", resource="user")
        async def handler(user, db):
            return {"code": 200, "data": {"id": 12}}

        user = SessionData(user_id=1, tenant_id=0, access_domain="platform", username="alice")
        result = await handler(user=user, db=None)

        assert result["data"]["id"] == 12  # 业务返回不受影响
        assert len(records) == 1
        row = records[0]
        assert isinstance(row, AuditLog)
        assert row.action == "create"
        assert row.resource_type == "user"
        assert row.resource_id == "12"  # 从 Result.data.id 兜底
        assert row.result == "success"
        assert row.actor_id == 1
        assert row.actor_name == "alice"
        assert row.tenant_id is None  # 未给固定归属的平台级资源（集群/节点等）仍是 NULL

    @pytest.mark.asyncio
    async def test_failure_writes_failed_row_and_reraises(self, monkeypatch):
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        class Boom(Exception):
            pass

        @audit_action(action="delete", resource="user", resource_id="user_id")
        async def handler(user, db, user_id):
            raise Boom("gone")

        user = SessionData(user_id=2, tenant_id=3, access_domain="tenant", username="bob")
        with pytest.raises(Boom):
            await handler(user=user, db=None, user_id=9)

        assert len(records) == 1
        row = records[0]
        assert row.result == "failed"
        assert row.resource_id == "9"
        assert row.tenant_id == 3  # 租户域默认归属
        assert "Boom" in row.detail["error"]

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_business(self, monkeypatch):
        # 独立 session 工厂抛异常 → 审计失败，但 handler 返回值照常
        def broken_factory():
            raise RuntimeError("no db")

        monkeypatch.setattr(audit_service, "_session_factory", broken_factory)

        @audit_action(action="update", resource="user")
        async def handler(user, db):
            return {"ok": True}

        user = SessionData(user_id=1, tenant_id=0, access_domain="platform")
        result = await handler(user=user, db=None)
        assert result == {"ok": True}


class TestCollectParamsDump:
    """_collect_params 的序列化口径：默认 exclude_none；exclude_unset 保留显式 null。"""

    class _UpdateBody(BaseModel):
        siteName: str | None = None
        maintenanceMode: bool | None = None

    def test_default_drops_explicit_none(self):
        """默认口径：update 类请求显式传的 null（清空/关闭）被剔除，无法留痕。"""
        body = self._UpdateBody(maintenanceMode=None)
        out = audit_service._collect_params({"request": body})
        assert out == {"request": {}}

    def test_exclude_unset_keeps_explicit_none(self):
        """三态端点口径：显式 null 保留、未提供的键剔除（fix #3——关维护可留痕）。"""
        body = self._UpdateBody(maintenanceMode=None)
        out = audit_service._collect_params({"request": body}, exclude_unset=True)
        assert out == {"request": {"maintenanceMode": None}}

    def test_exclude_unset_still_drops_unprovided(self):
        body = self._UpdateBody(siteName="站名")
        out = audit_service._collect_params({"request": body}, exclude_unset=True)
        assert out == {"request": {"siteName": "站名"}}  # maintenanceMode 未提供 → 不出现
