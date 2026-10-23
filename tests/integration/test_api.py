"""API 端点集成测试（stub 驱动，不依赖真实 PostgreSQL/Redis）。

遵循"stub get_db + 鉴权覆盖"原则：
- get_db 由 conftest 的 client fixture 覆盖为桩；
- 登录链路 monkeypatch authenticate，绕开 DB + Redis；
- 需登录态的端点覆盖 get_current_user 注入超管（短路 require_permission 的 DB 权限校验）；
- cluster 落库由 monkeypatch cluster_service 的函数代替。

HTTP 层只验证：路由注册、依赖链、统一 Result 响应契约、领域异常→HTTP 映射。
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import AuthError, ErrorCode
from llmpod.models.cluster import Cluster


def _admin_user() -> SessionData:
    """超管会话：require_permission 见 is_superuser=True 直接短路，不查 DB 权限。"""
    return admin_session()


def _stub_cluster(**overrides) -> Cluster:
    """构造一个字段齐全的 Cluster 实例（ClusterOut 由 ORM 派生，需覆盖全部列）。"""
    base = dict(
        id=1,
        name="Test Cluster",
        type="docker",
        description="",
        status="active",
        spec={},
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Cluster(**base)


class TestAuthEndpoints:
    """认证端点。"""

    async def test_login_success(self, client: AsyncClient, monkeypatch):
        """登录成功：authenticate 桩返回 LoginResult，路由组装为 Result[LoginData]。"""
        from llmpod.apps.webserver.auth.session import LoginResult, SessionData

        async def _fake_authenticate(db, username, password, settings):
            return LoginResult(
                token="stub-token",
                session=SessionData(
                    user_id=1,
                    tenant_id=0,
                    username=username,
                    role_codes=["super_admin"],
                    access_domain="platform",
                    is_superuser=True,
                ),
            )

        # login 端点 `from ...auth_service import authenticate`，patch 路由模块命名空间
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", _fake_authenticate)

        # 首次引导弹窗触发是登录链路的真实落库逻辑（本会话为断开 DB 的桩），
        # 同样 patch 路由模块符号——HTTP 层只验契约，不跑 service 真实现
        async def _skip_open_guide(db, *, user_id):
            return None

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.open_first_guide", _skip_open_guide)

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "testuser", "password": "testpass"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["token"] == "stub-token"
        assert data["username"] == "testuser"
        assert data["is_superuser"] is True

    async def test_login_invalid_credentials(self, client: AsyncClient, monkeypatch):
        """凭据无效：authenticate 抛 AuthError(INVALID_CREDENTIALS) → HTTP 401。"""

        async def _fail_authenticate(db, username, password, settings):
            raise AuthError("Invalid username or password", error_code=ErrorCode.INVALID_CREDENTIALS)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", _fail_authenticate)

        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "invalid", "password": "wrong"},
        )

        assert response.status_code == 401
        body = response.json()
        assert body["error_code"] == ErrorCode.INVALID_CREDENTIALS.value

    async def test_get_current_user_authenticated(self, client: AsyncClient):
        """带登录态访问 /me：覆盖 get_current_user，返回当前会话信息。"""
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.get("/api/v1/auth/me")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["username"] == "tester"
        assert data["is_superuser"] is True

    async def test_get_current_user_no_token(self, client: AsyncClient):
        """不带 token 访问 /me：get_current_user 在触达 Redis 前即抛 AuthError → 401。"""
        response = await client.get("/api/v1/auth/me")

        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value


class TestClusterEndpoints:
    """集群管理端点（挂在 /api/v1/admin/clusters）。"""

    async def test_create_cluster(self, client: AsyncClient, monkeypatch):
        """创建集群：超管登录 + 桩 create_cluster，验证 Result[ClusterDetailOut] 契约。

        create 路径不查 DB 装配统计（新建集群下无节点/GPU，直接置 0），故无需桩 enrich。
        """
        app.dependency_overrides[get_current_user] = _admin_user

        async def _fake_create(db, name, cluster_type, description=""):
            return _stub_cluster(id=1, name=name, type=cluster_type, description=description)

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.cluster.create_cluster", _fake_create)

        response = await client.post(
            "/api/v1/admin/clusters",
            json={"name": "Test Cluster", "type": "docker"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Test Cluster"
        assert data["type"] == "docker"
        assert "id" in data
        # 新建集群统计直接置默认 0（create 路径跳过装配查询）
        assert data["node_count"] == 0
        assert data["online_node_count"] == 0
        assert data["gpu_count"] == 0

    async def test_list_clusters(self, client: AsyncClient, monkeypatch):
        """列出集群：超管登录 + 桩 list_clusters，验证分页响应 items 结构。"""
        app.dependency_overrides[get_current_user] = _admin_user

        async def _fake_list(db, status=None, page=1, page_size=20):
            return [_stub_cluster(id=1, name="C1")], 1

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.cluster.list_clusters", _fake_list)

        # 富化装配查 DB，桩成 no-op（items 仍经 ClusterDetailOut 序列化，统计字段降级为默认 0）
        async def _fake_enrich(db, clusters):
            return clusters

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.cluster.enrich_clusters", _fake_enrich)

        response = await client.get("/api/v1/admin/clusters")

        assert response.status_code == 200
        data = response.json()["data"]
        assert "items" in data
        assert isinstance(data["items"], list)
        assert len(data["items"]) == 1
        # 未回填统计时安全降级为 0（绝不 500）
        assert data["items"][0]["node_count"] == 0

    async def test_list_clusters_no_auth(self, client: AsyncClient):
        """无登录态访问集群列表：require_permission → get_current_user 抛 AuthError → 401。"""
        response = await client.get("/api/v1/admin/clusters")

        assert response.status_code == 401


class TestHealthEndpoint:
    """健康检查端点。"""

    async def test_health_check(self, client: AsyncClient):
        """健康检查返回 ok。"""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
