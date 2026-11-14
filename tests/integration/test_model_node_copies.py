"""模型节点副本列表集成测试（FakeSession 驱动，不依赖真实 PG/Redis）。

验证 ``GET /admin/models/{id}/nodes`` 的外部行为：Result 封套、节点展示信息关联、
节点在线状态与模型状态分字段、空列表与 404。
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


def _override_db(session: FakeSession) -> None:
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db


def _override_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: admin_session()


def _model(model_id: int = 1) -> Model:
    return Model(id=model_id, name="Qwen2.5-7B", model_type="llm")


def _node(node_id: str, *, status: str = "online") -> Node:
    return Node(
        id=node_id,
        cluster_id=1,
        hostname=f"host-{node_id}",
        ip=f"10.0.0.{node_id[-1]}",
        status=status,
    )


def _copy(model_id: int, node_id: str, *, status: str = "present") -> NodeModel:
    return NodeModel(
        model_id=model_id,
        node_id=node_id,
        desired_state="present",
        status=status,
        local_dir=f"/data/models/m-{node_id}",
        model_path=f"/data/models/m-{node_id}",
        bytes_total=1024,
        bytes_done=1024,
        last_checked_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestListModelNodes:
    async def test_returns_copies_with_node_display(self, client: AsyncClient):
        session = FakeSession()
        session.preload(_model())
        session.preload(_node("n1", status="online"))
        session.preload(_node("n2", status="offline"))
        session.preload(_copy(1, "n1"))
        session.preload(_copy(1, "n2", status="failed"))
        _override_db(session)
        _override_user()

        resp = await client.get("/api/v1/admin/models/1/nodes")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert [item["node_id"] for item in data] == ["n1", "n2"]
        by_id = {item["node_id"]: item for item in data}
        assert by_id["n1"]["hostname"] == "host-n1"
        assert by_id["n1"]["node_status"] == "online"
        assert by_id["n1"]["model_status"] == "present"
        assert by_id["n2"]["node_status"] == "offline"
        assert by_id["n2"]["model_status"] == "failed"

    async def test_empty_when_no_copies(self, client: AsyncClient):
        session = FakeSession()
        session.preload(_model())
        _override_db(session)
        _override_user()

        resp = await client.get("/api/v1/admin/models/1/nodes")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == []

    async def test_model_not_found(self, client: AsyncClient):
        _override_db(FakeSession())
        _override_user()

        resp = await client.get("/api/v1/admin/models/999/nodes")
        assert resp.status_code == 404

    async def test_requires_auth(self, client: AsyncClient):
        _override_db(FakeSession())
        resp = await client.get("/api/v1/admin/models/1/nodes")
        assert resp.status_code == 401


def _full_model(model_id: int = 1) -> Model:
    """列表序列化需要 NOT NULL 字段与时间戳（桩会话不跑 server_default）。"""
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Model(
        id=model_id,
        name="Qwen2.5-7B",
        model_type="llm",
        is_moe=False,
        is_vision=False,
        needs_trust_remote_code=False,
        source_kind="transformers_folder",
        hash_mode="none",
        created_at=now,
        updated_at=now,
    )


class TestModelListNodeCount:
    async def test_node_count_counts_only_present(self, client: AsyncClient):
        session = FakeSession()
        session.preload(_full_model())
        session.preload(_copy(1, "n1", status="present"))
        session.preload(_copy(1, "n2", status="failed"))
        _override_db(session)
        _override_user()

        resp = await client.get("/api/v1/admin/models")
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]["items"]
        assert items[0]["node_count"] == 1
