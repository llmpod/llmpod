"""从节点目录创建模型集成测试（FakeSession 驱动）。

验证 ``POST /admin/models {source:node}``：node_id 必填、节点必须存在、创建前重新探测、
落库绑定 node_id，并在同一事务写入 node_model(present)。同名节点目录导入合并到既有
模型行（追加副本），不新建 Model。
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from llmpod.app import app
from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.db import get_db
from llmpod.models.model import Model
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


def _node(node_id: str = "n1") -> Node:
    return Node(id=node_id, cluster_id=1, hostname=f"host-{node_id}", ip="10.0.0.1", status="online")


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


def _existing(node_id: str = "n1") -> Model:
    """已导入的节点目录模型行（source=node、副本在 node_id）。"""
    return Model(
        id=1,
        name="Qwen2.5-7B",
        model_type="llm",
        source="node",
        source_url="/data/models/qwen",
        node_id=node_id,
        is_moe=False,
        is_vision=False,
        needs_trust_remote_code=False,
        source_kind="transformers_folder",
        hash_mode="none",
    )


class TestMergeNodeImport:
    """同名节点目录导入合并：不新建 Model 行，往 node_model 追加/刷新副本。"""

    async def test_second_node_appends_copy_to_existing_model(self, client: AsyncClient, monkeypatch):
        session = _Session()
        session.preload(_existing(node_id="n1"))
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="present"))
        session.preload(_node("n1"))
        session.preload(_node("n2"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())

        resp = await client.post("/api/v1/admin/models", json=_payload(node_id="n2"))

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["id"] == 1  # 返回既有模型，不新建
        assert len(session.all(Model)) == 1
        copies = {c.node_id: c for c in session.all(NodeModel)}
        assert set(copies) == {"n1", "n2"}
        assert copies["n2"].status == "present"
        assert copies["n2"].local_dir == "/data/models/qwen"
        assert copies["n2"].bytes_total == 1084

    async def test_same_node_reimport_refreshes_copy_row(self, client: AsyncClient, monkeypatch):
        """同节点重复导入 = 原地刷新该副本行（路径/观测值跟随新探测）。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))
        session.preload(
            NodeModel(
                model_id=1,
                node_id="n1",
                desired_state="present",
                status="failed",
                last_error="旧目录被移动",
                local_dir="/data/models/qwen-old",
            )
        )
        session.preload(_node("n1"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())

        resp = await client.post("/api/v1/admin/models", json=_payload())

        assert resp.status_code == 200, resp.text
        assert len(session.all(Model)) == 1
        assert len(session.all(NodeModel)) == 1  # 不产生重复行
        copy = session.all(NodeModel)[0]
        assert copy.status == "present"
        assert copy.local_dir == "/data/models/qwen"
        assert copy.last_error is None

    async def test_model_type_conflict_rejected(self, client: AsyncClient, monkeypatch):
        """同名但类别不同（embedding vs llm）大概率不是同一个模型：拒绝合并。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))
        session.preload(_node("n1"))
        session.preload(_node("n2"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())

        resp = await client.post(
            "/api/v1/admin/models", json=_payload(node_id="n2", model_type="embedding")
        )

        assert resp.status_code == 409, resp.text
        assert len(session.all(NodeModel)) == 0  # 无新副本行

    async def test_same_name_other_source_rejected(self, client: AsyncClient, monkeypatch):
        """同名但来源是 HF：不同来源不合并，明确拒绝（不是静默新建重名行）。"""
        session = _Session()
        session.preload(
            Model(id=2, name="Qwen2.5-7B", model_type="llm", source="huggingface", source_url="Qwen/Qwen2.5-7B")
        )
        session.preload(_node("n1"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())

        resp = await client.post("/api/v1/admin/models", json=_payload())

        assert resp.status_code == 409, resp.text

    async def test_merge_probe_failure_leaves_no_rows(self, client: AsyncClient, monkeypatch):
        """合并路径同样先探测：目录无效直接 422，不动既有模型与副本行。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))
        session.preload(_node("n2"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe(exists=False, message="目录不存在或不可达"))

        resp = await client.post("/api/v1/admin/models", json=_payload(node_id="n2"))

        assert resp.status_code == 422
        assert len(session.all(Model)) == 1
        copies = [c for c in session.all(NodeModel) if c.node_id == "n2"]
        assert copies == []

    async def test_concurrent_name_clash_falls_back_to_merge(self, client: AsyncClient, monkeypatch):
        """并发同名导入撞唯一索引：回滚保存点后并入赢家行，不 500。

        时序模拟：查重时赢家尚不可见（合并分支落空）→ 本事务插入 Model 时撞
        uq_model_node_name（真实 PG 由索引触发）→ 赢家提交可见 → 重查并入赢家。
        """
        from sqlalchemy.exc import IntegrityError

        session = _Session()
        session.preload(_node("n1"))
        session.preload(_node("n2"))
        _override_db(session)
        _override_user()
        _patch_probe(monkeypatch, _probe())
        orig_flush = session.flush
        state = {"clash": True}

        async def _flush():
            if not state["clash"]:
                await orig_flush()
                return
            # 撞索引的同时赢家事务提交：库中可见同名 node 模型行（含其 n1 副本）
            state["clash"] = False
            session.preload(_existing(node_id="n1"))
            session.preload(
                NodeModel(model_id=1, node_id="n1", desired_state="present", status="present")
            )
            raise IntegrityError("INSERT INTO model ...", {}, Exception("duplicate key"))

        session.flush = _flush

        resp = await client.post("/api/v1/admin/models", json=_payload(node_id="n2"))

        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["id"] == 1  # 并发输家并入赢家行
        assert len(session.all(Model)) == 1
        assert {c.node_id for c in session.all(NodeModel)} == {"n1", "n2"}


class TestDeleteModelCascadesNodeModel:
    async def test_delete_model_removes_copy_rows(self, client: AsyncClient, monkeypatch):
        """删模型连带清理 node_model 副本行（逻辑外键无级联，不清就是孤儿）。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))
        session.preload(
            NodeModel(
                model_id=1,
                node_id="n1",
                desired_state="present",
                status="present",
                local_dir="/data/models/qwen",
            )
        )
        session.preload(
            NodeModel(
                model_id=1,
                node_id="n2",
                desired_state="present",
                status="present",
                local_dir="/data/models/qwen",
            )
        )
        _override_db(session)
        _override_user()

        resp = await client.post("/api/v1/admin/models/1/delete")

        assert resp.status_code == 200, resp.text
        assert session.all(Model) == []
        assert session.all(NodeModel) == []


class TestRenameGuard:
    async def test_rename_onto_node_source_name_rejected(self, client: AsyncClient):
        """改名撞既有节点目录模型名 → 409（不走库层唯一索引的 IntegrityError→500）。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))  # id=1, name=Qwen2.5-7B
        session.preload(
            Model(
                id=2,
                name="hf-model",
                model_type="llm",
                source="huggingface",
                source_url="Qwen/Qwen2.5-7B",
            )
        )
        _override_db(session)
        _override_user()

        resp = await client.post("/api/v1/admin/models/2/update", json={"name": "Qwen2.5-7B"})

        assert resp.status_code == 409, resp.text

    async def test_rename_to_free_name_allowed(self, client: AsyncClient):
        session = _Session()
        session.preload(_existing(node_id="n1"))
        _override_db(session)
        _override_user()

        resp = await client.post("/api/v1/admin/models/1/update", json={"name": "Qwen2.5-7B-v2"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "Qwen2.5-7B-v2"

    async def test_create_other_source_onto_node_name_rejected(self, client: AsyncClient):
        """反向守卫：非 node 源建模型撞既有 node 模型名 → 409（名字是按名合并的锚点）。"""
        session = _Session()
        session.preload(_existing(node_id="n1"))
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/models",
            json={
                "name": "Qwen2.5-7B",
                "source": "huggingface",
                "source_url": "Qwen/Qwen2.5-7B",
                "model_type": "llm",
            },
        )

        assert resp.status_code == 409, resp.text


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
