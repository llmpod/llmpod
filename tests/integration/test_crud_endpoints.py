"""新增 CRUD 端点集成测试（stub 驱动，不依赖真实 PG/Redis）。

沿用项目"stub get_db + 鉴权覆盖 + service 桩"原则：

- Cluster delete 走 service 桩（与 cluster create/list 同形）：成功 / NotFound→404。
- 内联读取端点（GPU / User get）用单查询 fake session，验证 ``Result[*Out]`` 契约
  与 NotFound→404 映射。
- Pre-DB 守卫在触达数据库前即 raise：删平台租户→400、各 update 非法状态/空体→422。
- 新写端点无 token→401：证明路由已注册且受 ``require_permission`` 保护。

HTTP 层只验路由注册 / 依赖链 / 统一 Result 契约 / 领域异常→HTTP 映射，不验真实业务逻辑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient

from llmpod.app import app
from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import ErrorCode
from llmpod.db import get_db
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.user import User


def _admin_user() -> SessionData:
    """超管会话：require_permission 见 is_superuser=True 直接短路，不查 DB 权限。"""
    return admin_session()


def _stub_gpu(**ovr) -> Gpu:
    base = dict(
        id=1,
        uuid="GPU-XYZ",
        node_id="node-1",
        index=0,
        name="A100",
        arch="ampere",
        total_vram_gb=80.0,
        driver_version="535.0",
        status="online",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return Gpu(**base)


def _stub_user(**ovr) -> User:
    base = dict(
        id=10,
        tenant_id=0,
        username="alice",
        password_hash="x",
        email="a@b.c",
        phone="100",
        status="active",
        last_login_at=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return User(**base)


class _FakeResult:
    """单查询结果桩：仅支持 scalar_one_or_none（读取端点用）。"""

    def __init__(self, *, one=None):
        self._one = one

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """最小 async session 桩：execute 返回固定结果，记录调用。"""

    def __init__(self, one):
        self._result = _FakeResult(one=one)

    async def execute(self, stmt):
        return self._result


def _db_yielding(one):
    """构造 get_db 覆盖：yield 一个返回固定结果的 fake session。"""

    async def _gen():
        yield _FakeSession(one)

    return _gen


class TestClusterDelete:
    """集群删除端点（service 桩）。"""

    async def test_delete_cluster_success(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user

        async def _fake_delete(db, cluster_id):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.cluster.delete_cluster", _fake_delete)

        response = await client.post("/api/v1/admin/clusters/1/delete")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data == {"id": 1, "deleted": True}

    async def test_delete_cluster_not_found(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user

        async def _fake_delete(db, cluster_id):
            return False

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.cluster.delete_cluster", _fake_delete)

        response = await client.post("/api/v1/admin/clusters/99/delete")

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value

    async def test_topology_route_not_shadowed_by_cluster_id(self, client: AsyncClient, monkeypatch):
        """/clusters/topology 字面量路由须优先于 /{cluster_id} 参数路由。

        修复前 /topology 声明在 /{cluster_id} 之后，被其拦截把 "topology" 当 cluster_id
        转 int 失败 → 422。此处桩掉 builder，验证路由解析到 topology 处理器（200）。
        """
        app.dependency_overrides[get_current_user] = _admin_user

        async def _fake_build_all(db):
            return []

        monkeypatch.setattr("llmpod.apps.manager.deployment.topology.build_all_topologies", _fake_build_all)

        response = await client.get("/api/v1/admin/clusters/topology")

        assert response.status_code == 200


class _ModelFakeSession:
    """model delete 专用 session 桩：支持 get/delete/commit + execute（返回空集）。

    记录 deleted 行；commit 为 no-op；execute 恒空——delete_model 的部署引用守卫
    在该桩下恒不命中（这些用例不预置任何 ModelDeployment）。覆盖 http 层契约
    （NotFound→404 / success Result），不验真实落库（落库逻辑不进 HTTP 层测试）。
    """

    def __init__(self, model):
        self._model = model
        self.deleted = False
        self.siblings: list = []  # canonical 共享行（同名同内容）；默认空=无共享

    async def get(self, cls, pk):
        return self._model

    async def delete(self, obj):
        self.deleted = True

    async def commit(self):
        return None

    async def execute(self, stmt):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self._rows

        # 按 select 目标实体分发：部署引用守卫（查 ModelDeployment）恒空，
        # canonical 共享行查询（查 Model）返回预置 siblings
        target = stmt.column_descriptions[0]["entity"]
        if target is Model:
            return _Result(list(self.siblings))
        return _Result([])


def _db_yielding_model(model):
    async def _gen():
        yield _ModelFakeSession(model)

    return _gen


def _db_yielding_session(session):
    async def _gen():
        yield session

    return _gen


def _local_gguf_model(model_id: int, name: str, source_url) -> Model:
    """single_gguf 本地模型行（canonical 目录判定所需的最小字段集）。"""
    return Model(
        id=model_id,
        name=name,
        model_type="llm",
        source="local",
        source_url=str(source_url),
        format="gguf",
        quantization="Q4_K_M",
        source_kind="single_gguf",
    )


class TestModelDelete:
    """模型删除端点：local 删库+删盘 / 远程仅删库 / 不存在→404。

    盘删除 best-effort：本测试用真实 tmp_path 验证 local 路径确实被 rmtree。
    """

    async def test_delete_model_not_found(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        app.dependency_overrides[get_db] = _db_yielding_model(None)

        response = await client.post("/api/v1/admin/models/99/delete")

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value

    async def test_delete_local_purges_dir(self, client: AsyncClient, monkeypatch, tmp_path):
        from llmpod.config.settings import settings
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        # staged 目录在 model_store_dir 之下：tmp_path/upload/qwen（仿 /upload 端点落地形态）
        staged = tmp_path / "upload" / "qwen"
        staged.mkdir(parents=True)
        (staged / "model.safetensors").write_bytes(b"x" * 100)
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = Model(id=1, name="qwen", model_type="llm", source="local", source_url=str(staged))
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        response = await client.post("/api/v1/admin/models/1/delete")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["model_id"] == 1 and data["deleted"] is True
        # upload 产物属于平台生成目录 → 级联删（canonical 目录在 tmp_path 下不存在，仅 1 条）
        assert data["purged_dir"] == str(staged.resolve())
        assert data["purged_dirs"] == [str(staged.resolve())]
        # 目录确实被删
        assert not staged.exists()

    async def test_delete_local_preserves_user_preseeded_dir(self, client: AsyncClient, monkeypatch, tmp_path):
        """source_url 直指用户手工预置目录（非 upload/ 之下）→ 只删库行，目录保留。"""
        from llmpod.config.settings import settings
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        user_dir = tmp_path / "my-models" / "qwen"
        user_dir.mkdir(parents=True)
        (user_dir / "model.gguf").write_bytes(b"x" * 100)
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = Model(id=2, name="qwen2", model_type="llm", source="local", source_url=str(user_dir))
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        response = await client.post("/api/v1/admin/models/2/delete")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["purged_dir"] is None and data["purged_dirs"] == []
        assert user_dir.exists(), "用户预置目录不得被级联删除"
        assert (user_dir / "model.gguf").exists()

    async def test_delete_local_purges_canonical_dir(self, client: AsyncClient, monkeypatch, tmp_path):
        """canonical 目录（slug-digest 命名，平台生成）确实被清掉，防孤儿残留。

        digest 由 canonical_local_dir 按模型配方计算——用真实函数反推目录路径，
        预建该目录后断言删除后不存在且回执如实列出（此前该用例只断言 deleted，
        盘清理实际零覆盖）。
        """
        from llmpod.apps.manager.deployment.placement import canonical_local_dir
        from llmpod.config.settings import settings

        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = _local_gguf_model(3, "qwen", tmp_path / "upload" / "qwen-src")
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        canonical = Path(canonical_local_dir(m))
        canonical.mkdir(parents=True)
        (canonical / "model.gguf").write_bytes(b"x" * 10)

        response = await client.post("/api/v1/admin/models/3/delete")

        assert response.status_code == 200, response.text
        assert not canonical.exists(), "canonical 目录必须被清理"
        assert str(canonical) in response.json()["data"]["purged_dirs"]

    async def test_delete_local_shared_canonical_dir_preserved(self, client: AsyncClient, monkeypatch, tmp_path):
        """同名同内容的另一条 Model 行共享 canonical 目录 → 只删本行，目录保留。"""
        from llmpod.apps.manager.deployment.placement import canonical_local_dir
        from llmpod.config.settings import settings

        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = _local_gguf_model(3, "qwen", tmp_path / "upload" / "qwen-src")
        sibling = _local_gguf_model(9, "qwen", tmp_path / "upload" / "qwen-src-2")
        assert canonical_local_dir(m) == canonical_local_dir(sibling), "前置：同名同内容同目录"

        session = _ModelFakeSession(m)
        session.siblings = [sibling]  # 共享行仍存活
        app.dependency_overrides[get_db] = _db_yielding_session(session)

        canonical = Path(canonical_local_dir(m))
        canonical.mkdir(parents=True)
        (canonical / "model.gguf").write_bytes(b"x" * 10)

        response = await client.post("/api/v1/admin/models/3/delete")

        assert response.status_code == 200, response.text
        assert canonical.exists(), "共享目录不得因删其中一行被端掉"
        assert response.json()["data"]["purged_dirs"] == []

    async def test_delete_local_upload_root_never_purged(self, client: AsyncClient, monkeypatch, tmp_path):
        """source_url 直指 upload 根 → 只删库行，整棵上传树保留。"""
        from llmpod.config.settings import settings
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        upload_root = tmp_path / "upload"
        upload_root.mkdir()
        (upload_root / "a.bin").write_bytes(b"x" * 10)
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = Model(id=4, name="qwen4", model_type="llm", source="local", source_url=str(upload_root))
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        response = await client.post("/api/v1/admin/models/4/delete")

        assert response.status_code == 200, response.text
        assert upload_root.exists(), "upload 根目录绝不可删"
        assert (upload_root / "a.bin").exists()
        assert response.json()["data"]["purged_dirs"] == []

    async def test_delete_model_with_deployment_refused(self, client: AsyncClient, monkeypatch):
        """已有部署引用的模型不可删——否则删掉配方后部署 load_deployment 会 NotFound、无法重启。"""
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        db = FakeSession()
        db.preload(Model(id=1, name="qwen", model_type="llm", source="huggingface", source_url="Qwen/Qwen2"))
        db.preload(
            ModelDeployment(
                deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm", status="running", spec={}
            )
        )

        async def _gen():
            yield db

        app.dependency_overrides[get_db] = _gen

        response = await client.post("/api/v1/admin/models/1/delete")

        assert response.status_code == 409
        assert "个部署引用" in response.json()["message"]

    async def test_delete_remote_skips_disk(self, client: AsyncClient, monkeypatch, tmp_path):
        from llmpod.config.settings import settings
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        m = Model(id=2, name="qwen-hf", model_type="llm", source="huggingface", source_url="Qwen/Qwen2")
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        response = await client.post("/api/v1/admin/models/2/delete")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["deleted"] is True
        assert data["purged_dir"] is None  # 远程权重不在 manager 盘，不删盘

    async def test_delete_local_source_url_outside_store_skips_disk(self, client: AsyncClient, monkeypatch, tmp_path):
        """source_url 不在 model_store_dir 之内（篡改/错配）→ 仅删库，不越界删盘。"""
        from llmpod.config.settings import settings
        from llmpod.models.model import Model

        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path / "store"))

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "evil.bin").write_bytes(b"x")
        m = Model(id=3, name="evil", model_type="llm", source="local", source_url=str(outside))
        app.dependency_overrides[get_db] = _db_yielding_model(m)

        response = await client.post("/api/v1/admin/models/3/delete")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["deleted"] is True
        assert data["purged_dir"] is None  # 越界拒删
        assert outside.exists()  # 未被误删


class TestInlineReadEndpoints:
    """内联读取端点（fake session 单查询）。"""

    async def test_get_gpu_success(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        app.dependency_overrides[get_db] = _db_yielding(_stub_gpu())

        # 富化装配由路由调 enrich_gpus；桩掉它（断开 DB），并借机验证富化字段进响应
        async def _fake_enrich(db, gpus):
            for g in gpus:
                g.node_hostname = "host-1"
                g.cluster_id = 9
                g.cluster_name = "C9"
            return gpus

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.gpu.enrich_gpus", _fake_enrich)

        response = await client.get("/api/v1/admin/gpus/1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["uuid"] == "GPU-XYZ"
        assert data["name"] == "A100"
        # 关联字段经 enrich 装配进 GpuDetailOut 响应
        assert data["node_hostname"] == "host-1"
        assert data["cluster_id"] == 9
        assert data["cluster_name"] == "C9"

    async def test_get_gpu_not_found(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        app.dependency_overrides[get_db] = _db_yielding(None)

        response = await client.get("/api/v1/admin/gpus/999")

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value

    async def test_get_admin_user_success(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        app.dependency_overrides[get_db] = _db_yielding(_stub_user())

        # 富化装配由路由调 enrich_users_role_ids；桩掉它（断开 DB），并验证 role_ids 进响应
        async def _fake_enrich(db, users):
            for u in users:
                u.role_ids = [5, 7]
            return users

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.users.enrich_users_role_ids", _fake_enrich)

        response = await client.get("/api/v1/admin/users/10")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["username"] == "alice"
        assert data["tenant_id"] == 0  # 管理员端固定平台域
        # password_hash 必须被 orm_schema exclude，不外泄
        assert "password_hash" not in data
        # 关联字段经 enrich 装配进 UserDetailOut 响应
        assert data["role_ids"] == [5, 7]

    async def test_get_admin_user_not_found(self, client: AsyncClient):
        """平台域作用域：stub 返回 None（查询无命中）→ 404。"""
        app.dependency_overrides[get_current_user] = _admin_user
        app.dependency_overrides[get_db] = _db_yielding(None)

        response = await client.get("/api/v1/admin/users/999")

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value


class TestPreDbGuards:
    """触达 DB 前即 raise 的守卫（无需 fake session，stub None db 即可）。"""

    async def test_delete_platform_tenant_refused(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post("/api/v1/admin/tenants/0/delete")

        assert response.status_code == 400
        assert response.json()["error_code"] == ErrorCode.BUSINESS_ERROR.value

    async def test_cluster_create_k8s_refused(self, client: AsyncClient):
        """创建 k8s 类型集群被拒：K8s 编排未实现，接口只放行 docker。"""
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post(
            "/api/v1/admin/clusters",
            json={"name": "k8s-pool", "type": "k8s"},
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value

    async def test_user_update_invalid_status(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post("/api/v1/admin/users/10/update", json={"status": "bogus"})

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value

    async def test_user_update_empty_body(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post("/api/v1/admin/users/10/update", json={})

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value

    async def test_node_update_empty_body(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post("/api/v1/admin/nodes/n1/update", json={})

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value

    async def test_tenant_update_invalid_status(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user

        response = await client.post("/api/v1/admin/tenants/5/update", json={"status": "frozen"})

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/admin/clusters/1/delete"),
        ("POST", "/api/v1/admin/nodes/n1/delete"),
        ("POST", "/api/v1/admin/nodes/n1/update"),
        ("POST", "/api/v1/admin/tenants/1/delete"),
        ("POST", "/api/v1/admin/tenants/1/update"),
        ("GET", "/api/v1/admin/tenants/1"),
        ("POST", "/api/v1/admin/users/1/delete"),
        ("POST", "/api/v1/admin/users/1/update"),
        ("GET", "/api/v1/admin/users/1"),
        ("GET", "/api/v1/admin/gpus/1"),
        ("POST", "/api/v1/admin/access/roles/1/delete"),
        ("POST", "/api/v1/admin/access/roles/1/update"),
        ("POST", "/api/v1/admin/models/1/delete"),
        ("POST", "/api/v1/tenant/users/1/delete"),
        ("POST", "/api/v1/tenant/users/1/update"),
        ("GET", "/api/v1/tenant/users/1"),
    ],
)
class TestNewEndpointAuthGating:
    """新 CRUD 端点须受鉴权保护：无 token → 401（证明路由已注册）。"""

    async def test_requires_auth(self, client: AsyncClient, method, path):
        if method == "GET":
            response = await client.get(path)
        else:
            response = await client.post(path)
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value
