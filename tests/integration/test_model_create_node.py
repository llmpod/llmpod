"""从节点目录创建模型集成测试（FakeSession 驱动）。

验证 ``POST /admin/models {source:node}``：node_id 必填、节点必须存在、创建前重新探测、
落库绑定 node_id，并在同一事务写入 node_model(present)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from llmpod.app import app
from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.db import get_db
from llmpod.models.node import Node
from llmpod.models.node_model import NodeModel
from llmpod.tunnel.schemas import ModelDirEntry, ModelDirProbe


class _Session(FakeSession):
    """FakeSession + 补 created_at/updated_at（真实库由 server_default 填充）。"""

    async def flush(self):
        await super().flush()
        now = datetime.now(timezone.utc)
        for store in self._store.values():
            for obj in store.values():
                if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
                    obj.created_at = now
                if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
                    obj.updated_at = now


def _override_db(session: FakeSession) -> None:
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db


def _override_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: admin_session()


def _node() -> Node:
    return Node(id="n1", cluster_id=1, hostname="host-n1", ip="10.0.0.1", status="online")


def _probe(**overrides) -> ModelDirProbe:
    base = dict(
        exists=True,
        is_dir=True,
        config_name="config.json",
        config={"model_type": "qwen2"},
        files=[
            ModelDirEntry(path="config.json", size=60),
            ModelDirEntry(path="model.safetensors", size=1024),
        ],
        total_bytes=1084,
    )
    base.update(overrides)
    return ModelDirProbe(**base)


def _patch_probe(monkeypatch, probe: ModelDirProbe) -> None:
    from llmpod.apps.manager import dispatch

    async def _call(node_id, path):
        assert node_id == "n1"
        return probe

    monkeypatch.setattr(dispatch, "probe_model_dir", _call)


def _payload(**overrides) -> dict:
    base = {
        "name": "Qwen2.5-7B",
        "source": "node",
        "source_url": "/data/models/qwen",
        "node_id": "n1",
        "model_type": "llm",
        "files": {
            "success": True,
            "source_kind": "transformers_folder",
            "format": "safetensors",
            "size_gb": 0.00000095367431640625,
        },
    }
    base.update(overrides)
    return base


class TestCreateNodeModel:
    async def test_creates_model_and_present_copy(self, client: AsyncClient, monkeypatch):
        session = _Session()
        session.preload(_node())
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())

        resp = await client.post("/api/v1/admin/models", json=_payload())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["source"] == "node"
        assert data["node_id"] == "n1"

        copies = session.all(NodeModel)
        assert len(copies) == 1
        copy = copies[0]
        assert copy.model_id == data["id"]
        assert copy.node_id == "n1"
        assert copy.status == "present"
        assert copy.desired_state == "present"
        assert copy.local_dir == "/data/models/qwen"
        assert copy.model_path == "/data/models/qwen"
        assert copy.bytes_total == 1084

    async def test_single_gguf_model_path_points_to_file(self, client: AsyncClient, monkeypatch):
        session = _Session()
        session.preload(_node())
        _override_db(session)
        _override_user()
        _patch_probe(
            monkeypatch,
            _probe(
                config=None,
                config_name=None,
                files=[ModelDirEntry(path="model.gguf", size=2048)],
                total_bytes=2048,
            ),
        )

        resp = await client.post(
            "/api/v1/admin/models",
            json=_payload(files={"success": True, "source_kind": "single_gguf", "format": "gguf", "size_gb": 2}),
        )
        assert resp.status_code == 200, resp.text
        copy = session.all(NodeModel)[0]
        assert copy.model_path == "/data/models/qwen/model.gguf"

    async def test_missing_node_id_rejected(self, client: AsyncClient):
        _override_db(FakeSession())
        _override_user()
        resp = await client.post("/api/v1/admin/models", json=_payload(node_id=None))
        assert resp.status_code == 422

    async def test_unknown_node_rejected(self, client: AsyncClient):
        _override_db(FakeSession())
        _override_user()
        resp = await client.post("/api/v1/admin/models", json=_payload())
        assert resp.status_code == 404

    async def test_invalid_dir_rejected_on_create(self, client: AsyncClient, monkeypatch):
        session = _Session()
        session.preload(_node())
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe(exists=False, message="目录不存在或不可达"))

        resp = await client.post("/api/v1/admin/models", json=_payload())
        assert resp.status_code == 422
        assert not session.all(NodeModel)


class TestUpdateNodeModelGuard:
    """节点目录模型的绑定不可原地改（改来源/目录需重新导入）。"""

    async def _seed(self):
        from llmpod.models.model import Model

        session = _Session()
        session.preload(
            Model(
                id=1,
                name="Qwen",
                model_type="llm",
                source="node",
                source_url="/data/models/qwen",
                node_id="n1",
                is_moe=False,
                is_vision=False,
                needs_trust_remote_code=False,
                source_kind="transformers_folder",
                hash_mode="none",
            )
        )
        _override_db(session)
        _override_user()
        return session

    async def test_source_change_rejected(self, client: AsyncClient):
        await self._seed()
        resp = await client.post("/api/v1/admin/models/1/update", json={"source": "huggingface"})
        assert resp.status_code == 422

    async def test_dir_change_rejected(self, client: AsyncClient):
        await self._seed()
        resp = await client.post(
            "/api/v1/admin/models/1/update", json={"source_url": "/data/models/other"}
        )
        assert resp.status_code == 422

    async def test_name_change_allowed(self, client: AsyncClient):
        await self._seed()
        resp = await client.post("/api/v1/admin/models/1/update", json={"name": "Qwen-renamed"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "Qwen-renamed"


class TestUpdateSourceToNodeGuard:
    async def test_cannot_switch_existing_model_to_node_source(self, client: AsyncClient):
        from llmpod.models.model import Model

        session = _Session()
        session.preload(Model(id=1, name="Qwen", model_type="llm", source="huggingface"))
        _override_db(session)
        _override_user()

        resp = await client.post("/api/v1/admin/models/1/update", json={"source": "node"})
        assert resp.status_code == 422
