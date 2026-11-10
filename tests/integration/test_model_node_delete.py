"""删除节点副本集成测试（FakeSession 驱动）。

覆盖两种语义：仅移除平台记录（默认）与删除磁盘文件；删除失败保留记录；
有部署实例时拒绝；记录不存在 404。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.db import get_db
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.tunnel.schemas import RemoveModelDirResult


def _override_db(session: FakeSession) -> None:
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db


def _override_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: admin_session()


def _session(*, with_deployment: bool = False) -> FakeSession:
    session = FakeSession()
    session.preload(Model(id=1, name="Qwen", model_type="llm", source="huggingface"))
    session.preload(
        NodeModel(
            model_id=1,
            node_id="n1",
            desired_state="present",
            status="present",
            local_dir="/data/models/qwen",
            model_path="/data/models/qwen",
        )
    )
    if with_deployment:
        session.preload(
            ModelDeployment(
                deployment_id="d1",
                cluster_id=1,
                name="dep",
                model_id=1,
                engine="vllm",
                status="running",
            )
        )
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="d1",
                assigned_node="n1",
                status="RUNNING",
            )
        )
    return session


class TestDeleteModelNode:
    async def test_unbind_record_only_keeps_files(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch

        async def _should_not_be_called(*_args, **_kwargs):
            raise AssertionError("delete_files=false 不应调用节点")

        monkeypatch.setattr(dispatch, "remove_model_dir", _should_not_be_called)
        session = _session()
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": False},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["files_removed"] is False
        assert not session.all(NodeModel)

    async def test_delete_files_confirmed_then_record_removed(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch

        calls: list[tuple] = []

        async def _remove(node_id, path):
            calls.append((node_id, path))
            return RemoveModelDirResult(removed=True, path=path)

        monkeypatch.setattr(dispatch, "remove_model_dir", _remove)
        session = _session()
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": True},
        )
        assert resp.status_code == 200, resp.text
        assert calls == [("n1", "/data/models/qwen")]
        assert resp.json()["data"]["files_removed"] is True
        assert not session.all(NodeModel)

    async def test_node_failure_keeps_record(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch

        async def _remove(node_id, path):
            return RemoveModelDirResult(removed=False, path=path, message="删除失败：权限不足")

        monkeypatch.setattr(dispatch, "remove_model_dir", _remove)
        session = _session()
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": True},
        )
        assert resp.status_code == 400, resp.text
        assert session.all(NodeModel)  # 记录保留

    async def test_active_deployment_blocks_delete(self, client: AsyncClient):
        session = _session(with_deployment=True)
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": False},
        )
        assert resp.status_code == 409, resp.text
        assert session.all(NodeModel)

    async def test_missing_copy_returns_404(self, client: AsyncClient):
        session = FakeSession()
        session.preload(Model(id=1, name="Qwen", model_type="llm", source="huggingface"))
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": False},
        )
        assert resp.status_code == 404


class TestDeleteGuards:
    async def test_shared_canonical_dir_refused(self, client: AsyncClient):
        from llmpod.models.model import Model

        session = FakeSession()
        fields = dict(
            name="Qwen",
            model_type="llm",
            source="huggingface",
            source_url="Qwen/Qwen2.5",
            format="safetensors",
            source_kind="transformers_folder",
            hash_mode="none",
        )
        session.preload(Model(id=1, **fields))
        session.preload(Model(id=2, **fields))
        session.preload(
            NodeModel(
                model_id=1,
                node_id="n1",
                desired_state="present",
                status="present",
                local_dir="/data/models/qwen",
                model_path="/data/models/qwen",
            )
        )
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": True},
        )
        assert resp.status_code == 409, resp.text
        assert session.all(NodeModel)

    async def test_planning_stage_deployment_blocks_delete(self, client: AsyncClient):
        from llmpod.models.model import Model

        session = FakeSession()
        session.preload(Model(id=1, name="Qwen", model_type="llm", source="node", node_id="n1"))
        session.preload(
            NodeModel(
                model_id=1,
                node_id="n1",
                desired_state="present",
                status="present",
                local_dir="/data/models/qwen",
                model_path="/data/models/qwen",
            )
        )
        session.preload(
            ModelDeployment(
                deployment_id="d2",
                cluster_id=1,
                name="planning",
                model_id=1,
                engine="vllm",
                status="creating",
                spec={"topology": {"tp": 1}, "gpu_selector": {"node_id": "n1"}},
            )
        )
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models/1/nodes/n1/delete",
            json={"delete_files": False},
        )
        assert resp.status_code == 409, resp.text
        assert session.all(NodeModel)
