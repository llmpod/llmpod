"""节点升级/注销端点集成测试：守卫、通知路径、批量下发、版本可见性。

遵循 stub 范式（get_db 覆盖 FakeSession / 鉴权覆盖 admin_session /
dispatch monkeypatch 路由模块命名空间符号）。
"""

from __future__ import annotations

from httpx import AsyncClient

import llmpod
from llmpod.app import app
from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.config.constants import InstanceStatus, NodeStatus
from llmpod.core.exceptions import ErrorCode
from llmpod.db import get_db
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.node import Node

_admin = admin_session()


def _node(node_id="n1", version=None, status=NodeStatus.ONLINE) -> Node:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Node(
        id=node_id,
        cluster_id=1,
        hostname="h1",
        ip="10.0.0.1",
        status=status,
        version=version,
        capabilities=[],
        created_at=now,
        updated_at=now,
    )


def _instance(node_id="n1", status=InstanceStatus.RUNNING, iid="inst-1") -> Instance:
    return Instance(instance_id=iid, deployment_id="d1", rank=0, assigned_node=node_id, status=status, spec={})


def _db(session):
    """依赖覆盖用生成器工厂（FastAPI 依赖要的是函数，不是 context manager）。"""

    async def _gen():
        yield session

    return _gen


def _setup(client_fixture, session, monkeypatch):
    app.dependency_overrides[get_db] = _db(session)
    app.dependency_overrides[get_current_user] = lambda: _admin


class _FakeUpgradeChannel:
    """dispatch.send_node_upgrade 的 channel 替身：捕获出站通知。"""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_notification(self, method: str, params: dict):
        self.sent.append((method, params))


def _fake_hub(monkeypatch, dispatch_mod):
    """把 dispatch._require_node 指到假 channel（send_node_upgrade 的真实策略链可跑通）。"""
    channel = _FakeUpgradeChannel()

    class _Node:
        pass

    node = _Node()
    node.channel = channel
    monkeypatch.setattr(dispatch_mod, "_require_node", lambda node_id: (None, node))
    return channel


class TestUpgradeEndpoint:
    async def test_upgrade_node_not_found(self, client: AsyncClient):
        session = FakeSession()
        _setup(client, session, None)
        response = await client.post("/api/v1/admin/nodes/nope/upgrade")
        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value

    async def test_upgrade_offline_node(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch

        monkeypatch.setattr(dispatch, "node_online", lambda nid: False)
        session = FakeSession()
        session.preload(_node(version="0.0.1"))
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/upgrade")
        data = response.json()["data"]
        assert data["dispatched"] is False and "不在线" in data["reason"]

    async def test_upgrade_up_to_date(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch

        monkeypatch.setattr(dispatch, "node_online", lambda nid: True)
        session = FakeSession()
        session.preload(_node(version=llmpod.__version__))
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/upgrade")
        data = response.json()["data"]
        assert data["dispatched"] is False and "不落后" in data["reason"]

    async def test_upgrade_dispatched(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager import dispatch
        from llmpod.services.version_service import official_image_ref

        monkeypatch.setattr(dispatch, "node_online", lambda nid: True)
        channel = _fake_hub(monkeypatch, dispatch)
        session = FakeSession()
        session.preload(_node(version="0.0.1"))
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/upgrade")
        data = response.json()["data"]
        assert data["dispatched"] is True and data["reason"] is None
        # 真实策略链跑通：官方镜像 + 当前版本推导（无部署配置）
        assert channel.sent == [
            (
                "$node_upgrade",
                {"target_version": llmpod.__version__, "image_ref": official_image_ref()},
            )
        ]


class TestUpgradeBatch:
    async def test_batch_defaults_to_outdated_online(self, client: AsyncClient, monkeypatch):
        """缺省全量：只对在线落后节点下发。"""
        from llmpod.apps.manager import dispatch

        online = {"n1", "n2"}
        monkeypatch.setattr(dispatch, "node_online", lambda nid: nid in online)
        _fake_hub(monkeypatch, dispatch)
        session = FakeSession()
        session.preload(_node("n1", version="0.0.1"))  # 在线落后 → 下发
        session.preload(_node("n2", version=llmpod.__version__))  # 在线同版 → 跳过
        session.preload(_node("n3", version="0.0.1", status=NodeStatus.OFFLINE))  # 离线 → 跳过
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/upgrade-batch", json={})
        data = response.json()["data"]
        assert data["dispatched_count"] == 1
        by_id = {i["id"]: i for i in data["items"]}
        assert by_id["n1"]["dispatched"] is True
        assert by_id["n2"]["dispatched"] is False
        assert by_id["n3"]["dispatched"] is False and "不在线" in by_id["n3"]["reason"]

    async def test_batch_explicit_ids_missing_404(self, client: AsyncClient):
        session = FakeSession()
        _setup(client, session, None)
        response = await client.post("/api/v1/admin/nodes/upgrade-batch", json={"node_ids": ["ghost"]})
        assert response.status_code == 404

    async def test_batch_empty_ids_rejected(self, client: AsyncClient, monkeypatch):
        """显式空列表 ≠ 缺省全量：拒绝（防「没勾选」传 [] 被解释成全集群升级）。"""

        async def _no_audit(*_a, **_k):
            return None

        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        session = FakeSession()
        session.preload(_node("n1", version="0.0.1"))
        _setup(client, session, None)
        response = await client.post("/api/v1/admin/nodes/upgrade-batch", json={"node_ids": []})
        assert response.status_code == 422
        assert "node_ids" in response.json()["message"]
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value


class TestDeleteEndpoint:
    async def test_delete_not_found(self, client: AsyncClient):
        session = FakeSession()
        _setup(client, session, None)
        response = await client.post("/api/v1/admin/nodes/nope/delete")
        assert response.status_code == 404

    async def test_delete_active_instances_conflict(self, client: AsyncClient):
        """未停止实例硬禁删（除 STOPPED 外任一即 409——含自愈集 CRASHED/FAILED，
        漏拦会让实例行残留指向已删节点、部署卡死在自愈循环）。"""
        for status in InstanceStatus:
            if status == InstanceStatus.STOPPED:
                continue
            session = FakeSession()
            session.preload(_node())
            session.preload(_instance(status=status))
            _setup(client, session, None)
            response = await client.post("/api/v1/admin/nodes/n1/delete")
            assert response.status_code == 409, status
            assert "未停止的推理实例" in response.json()["message"], status

    async def test_delete_offline_node_cascade(self, client: AsyncClient, monkeypatch):
        """离线节点（无活跃实例）：注销标记 + 级联删 GPU，无通知。"""
        from llmpod.apps.manager import dispatch
        from llmpod.services import node_service

        monkeypatch.setattr(dispatch, "node_online", lambda nid: False)
        tombstones: list[str] = []

        async def fake_tomb(node_id):
            tombstones.append(node_id)

        monkeypatch.setattr(node_service, "set_node_tombstone", fake_tomb)
        session = FakeSession()
        session.preload(_node(status=NodeStatus.OFFLINE))
        session.preload(Gpu(uuid="gpu-1", node_id="n1", index=0, name="G", arch="nvidia"))
        session.preload(_instance(status=InstanceStatus.STOPPED))  # 终态实例不拦
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/delete")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["notified"] is False and data["cleaned"] is False
        assert tombstones == ["n1"]
        assert session.all(Gpu) == []  # GPU 级联清

    async def test_delete_online_node_notifies_and_waits(self, client: AsyncClient, monkeypatch):
        """在线节点：发 decommission 通知，等 WS 断（node_online 翻 False）→ cleaned=True。"""
        from llmpod.apps.manager import dispatch
        from llmpod.services import node_service

        state = {"online": True}
        monkeypatch.setattr(dispatch, "node_online", lambda nid: state["online"])
        sent: list[str] = []

        async def fake_send(node_id):
            sent.append(node_id)
            state["online"] = False  # 模拟 node rm -f 自己 → WS on_close

        monkeypatch.setattr(dispatch, "send_node_decommission", fake_send)

        async def fake_tomb(node_id):
            pass

        monkeypatch.setattr(node_service, "set_node_tombstone", fake_tomb)
        monkeypatch.setattr(node_service, "_DECOMMISSION_WAIT_SECONDS", 0.2)
        session = FakeSession()
        session.preload(_node())
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/delete")
        data = response.json()["data"]
        assert data == {"id": "n1", "deleted": True, "notified": True, "cleaned": True}
        assert sent == ["n1"]

    async def test_delete_online_node_timeout_continues(self, client: AsyncClient, monkeypatch):
        """WS 一直不断（自清理失败）：超时也继续删，cleaned=False（注销标记兜底拦重连）。"""
        from llmpod.apps.manager import dispatch
        from llmpod.services import node_service

        monkeypatch.setattr(dispatch, "node_online", lambda nid: True)  # 永在线
        monkeypatch.setattr(node_service, "_DECOMMISSION_WAIT_SECONDS", 0.05)

        async def fake_send(node_id):
            pass

        monkeypatch.setattr(dispatch, "send_node_decommission", fake_send)

        async def fake_tomb(node_id):
            pass

        monkeypatch.setattr(node_service, "set_node_tombstone", fake_tomb)
        session = FakeSession()
        session.preload(_node())
        _setup(client, session, monkeypatch)
        response = await client.post("/api/v1/admin/nodes/n1/delete")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["notified"] is True and data["cleaned"] is False


class TestVersionVisibility:
    async def test_list_nodes_carries_version_status(self, client: AsyncClient):
        """列表装配 version/version_status（落后/同版/未知三态）。"""
        session = FakeSession()
        session.preload(_node("n1", version="0.0.1"))
        session.preload(_node("n2", version=llmpod.__version__))
        session.preload(_node("n3", version=None))
        _setup(client, session, None)
        response = await client.get("/api/v1/admin/nodes")
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        by_id = {i["id"]: i for i in items}
        assert by_id["n1"]["version"] == "0.0.1"
        assert by_id["n1"]["version_status"] == "outdated"
        assert by_id["n2"]["version_status"] == "ok"
        assert by_id["n3"]["version_status"] == "unknown"
