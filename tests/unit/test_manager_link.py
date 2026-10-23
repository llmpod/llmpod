"""manager_link.ManagerLink 单测：Node→Manager typed caller（call_unary 封装 + 离线/失败 best-effort）。"""

from __future__ import annotations


from llmpod.apps.node.manager_link import ManagerLink
from llmpod.tunnel.entity.errors import RemoteError
from llmpod.tunnel.schemas import InstanceStatusPayload, TaskEventPayload, TaskState


class FakeChannel:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    async def call_unary(self, service, method, params, timeout=None):
        self.calls.append((service, method, params))
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, channel=None, node_id="n1"):
        self.channel = channel
        self.node_id = node_id


class TestReportTaskEvent:
    async def test_sends_evt_param_and_parses_ack(self):
        ch = FakeChannel(result={"ok": True, "message": "ok"})
        link = ManagerLink(FakeClient(ch))

        ack = await link.report_task_event(TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING", progress=10))

        assert ack is not None and ack.ok and ack.message == "ok"
        assert ch.calls[0][0] == "ManagerTaskService"
        assert ch.calls[0][1] == "report_task_event"
        assert ch.calls[0][2]["evt"]["task_id"] == "t1"
        assert ch.calls[0][2]["evt"]["status"] == "RUNNING"

    async def test_offline_returns_none(self):
        link = ManagerLink(FakeClient(channel=None))
        assert await link.report_task_event(TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING")) is None

    async def test_remote_error_returns_none(self):
        ch = FakeChannel(error=RemoteError("boom"))
        link = ManagerLink(FakeClient(ch))
        assert await link.report_task_event(TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING")) is None


class TestSyncNodeState:
    async def test_sends_node_id_and_tasks(self):
        ch = FakeChannel(result={"ok": True})
        link = ManagerLink(FakeClient(ch, node_id="node-42"))

        await link.sync_node_state([TaskState(id="t1", status="RUNNING", progress=50)])

        service, method, params = ch.calls[0]
        assert (service, method) == ("ManagerTaskService", "sync_node_state")
        assert params["node_id"] == "node-42"
        # tasks 原样传 TaskState（wire 序列化时经 _json_default dump；Manager 端 coerce 归一）
        assert [t.model_dump() for t in params["tasks"]] == [
            {"id": "t1", "status": "RUNNING", "progress": 50, "resource_key": None, "last_error": None, "extra": None}
        ]

    async def test_offline_returns_none(self):
        link = ManagerLink(FakeClient(channel=None))
        assert await link.sync_node_state([]) is None


class TestReportInstanceStatus:
    async def test_sends_evt_param(self):
        ch = FakeChannel(result={"ok": True})
        link = ManagerLink(FakeClient(ch))

        ack = await link.report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1",
                instance_id="i1",
                node_id="n1",
                status="READY",
                endpoint="http://10.0.1.5:8000",
            )
        )

        assert ack is not None and ack.ok
        service, method, params = ch.calls[0]
        assert (service, method) == ("ManagerTaskService", "report_instance_status")
        assert params["evt"]["instance_id"] == "i1"
        assert params["evt"]["status"] == "READY"
        assert params["evt"]["endpoint"] == "http://10.0.1.5:8000"

    async def test_offline_returns_none(self):
        link = ManagerLink(FakeClient(channel=None))
        assert (
            await link.report_instance_status(
                InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="READY")
            )
            is None
        )


class TestOnlineFlag:
    def test_online_when_channel_present(self):
        assert ManagerLink(FakeClient(FakeChannel())).online is True

    def test_offline_when_channel_none(self):
        assert ManagerLink(FakeClient(channel=None)).online is False

    def test_node_id_from_client(self):
        assert ManagerLink(FakeClient(node_id="abc")).node_id == "abc"
