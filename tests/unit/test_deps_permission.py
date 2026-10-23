"""require_permission 依赖单元测试：超管短路 / 放行 / 拒绝（scope 三元组）。"""

from __future__ import annotations

import pytest

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import PermissionDenied
from llmpod.permission.permissions import CLUSTER_CREATE


def _checker_with(user, db):
    """构造一个 require_permission 依赖并调用。"""
    checker = require_permission(CLUSTER_CREATE)
    return checker(user=user, db=db)


class TestRequirePermission:
    """require_permission(perm)：超管短路、有权限放行、无权限拒绝。"""

    @pytest.mark.asyncio
    async def test_superuser_short_circuit(self, monkeypatch):
        # 超管即使无授权也应放行，且不调用 DB
        called = {"n": 0}

        async def fake_check(db, user_id, scope, resource, action):
            called["n"] += 1
            return False

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", fake_check)
        user = SessionData(user_id=1, tenant_id=0, access_domain="platform", is_superuser=True)
        result = await _checker_with(user, db=None)
        assert result is user
        assert called["n"] == 0  # 超管短路，未查 DB

    @pytest.mark.asyncio
    async def test_has_permission_passes(self, monkeypatch):
        async def fake_check(db, user_id, scope, resource, action):
            assert scope == "platform" and resource == "cluster" and action == "create"
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", fake_check)
        user = SessionData(user_id=7, tenant_id=0, access_domain="platform", is_superuser=False)
        result = await _checker_with(user, db=None)
        assert result is user

    @pytest.mark.asyncio
    async def test_no_permission_denied(self, monkeypatch):
        async def fake_check(db, user_id, scope, resource, action):
            return False

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", fake_check)
        user = SessionData(user_id=7, tenant_id=0, access_domain="platform", is_superuser=False)
        with pytest.raises(PermissionDenied) as exc_info:
            await _checker_with(user, db=None)
        assert "platform:cluster:create" in str(exc_info.value)
        assert exc_info.value.scope == "platform"
        assert exc_info.value.resource == "cluster"
        assert exc_info.value.action == "create"
