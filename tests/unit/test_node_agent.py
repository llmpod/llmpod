"""NodeAgent 单测：ensure/cancel/幂等/secrets 边界/启动恢复/重连 sync。

真实 TaskStore（tmp sqlite）+ FakeManagerLink + 可注入 FakeExecutor（cancel/行为控制），
亦覆盖真实 EnsureModelExecutor 的 present 短路。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from llmpod.apps.node.agent import NodeAgent
from llmpod.apps.node.agent_store import TaskStore
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import Ack, TaskPayload


class FakeManagerLink:
    def __init__(self):
        self.events = []
        self.syncs = []
        self.instance_reports = []
        self._online = False

    @property
    def online(self):
        return self._online

    async def report_task_event(self, evt):
        self.events.append(evt)
        return Ack(ok=True)

    async def sync_node_state(self, tasks):
        self.syncs.append(list(tasks))
        return Ack(ok=True)

    async def report_instance_status(self, evt):
        self.instance_reports.append(evt)
        return Ack(ok=True)


class FakeExecutor:
    """可控执行器：等 cancel_event（命中→CANCELLED）；否则 SUCCESS；记录 secrets。"""

    def __init__(self):
        self.calls = []  # (task_id, secrets)

    async def execute(self, task, secrets, report, cancel_event):
        self.calls.append((task.task_id, secrets))
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=0.5)
            await report("CANCELLED", message="cancelled")
        except asyncio.TimeoutError:
            await report("SUCCESS", 100, extra={"model_path": "/data/models/model-1"})


class InstanceStatusExecutor:
    """上报携带 instance_status 的执行器——验证实例状态旁路通道。"""

    async def execute(self, task, secrets, report, cancel_event):
        await report("RUNNING", instance_status="STARTING", container_id="c-1")
        await report("SUCCESS", 100, instance_status="READY", endpoint="http://n1:8000")


class RaisingExecutor:
    """抛出携带 secret 文本的异常——验证 H1：异常消息脱敏（不落 last_error / 不入上报）。"""

    async def execute(self, task, secrets, report, cancel_event):
        raise RuntimeError(f"download failed: token={secrets['hf_token']} invalid")


def _tp(task_id="t1", local_dir="", source="huggingface", source_kind="transformers_folder") -> TaskPayload:
    return TaskPayload(
        id=task_id,
        type="ENSURE_MODEL",
        resource_type="MODEL_PLACEMENT",
        resource_key="model:1:n1",
        node_id="n1",
        status="PENDING",
        attempt=1,
        payload={"model_id": 1, "source": source, "source_kind": source_kind, "local_dir": str(local_dir)},
    )


@pytest.fixture
async def store(tmp_path):
    s = TaskStore(str(tmp_path / "tasks.db"))
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def link():
    return FakeManagerLink()


async def _drain():
    await asyncio.sleep(0)  # 让 create_task 调度的 executor 跑一步


async def _wait_status(store, task_id, statuses, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = await store.get(task_id)
        if t is not None and t.status in statuses:
            return t
        await asyncio.sleep(0.01)
    t = await store.get(task_id)
    raise AssertionError(f"timeout: task={task_id} status={t.status if t else None}")


class TestHandleEnsure:
    async def test_present_model_reports_success(self, store, link, tmp_path):
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")  # H1：verify 需权重才判在位
        agent = NodeAgent(store, manager_link=link)
        await agent.handle_ensure(_tp(local_dir=d))

        t = await _wait_status(store, "t1", {"SUCCESS"})
        assert t.progress == 100
        assert any(e.status == "SUCCESS" and e.extra["model_path"] == str(d) for e in link.events)

    async def test_idempotent_no_double_dispatch(self, store, link):
        ex = FakeExecutor()
        agent = NodeAgent(store, {"ENSURE_MODEL": ex}, manager_link=link)
        a1 = await agent.handle_ensure(_tp())
        a2 = await agent.handle_ensure(_tp())  # 并发/重发同 task_id
        assert a1.ok and a2.ok
        for _ in range(50):  # 等 create_task 真正进入 executor
            if ex.calls:
                break
            await asyncio.sleep(0.01)
        assert len(ex.calls) == 1  # 不双派发
        await agent.handle_cancel("t1")  # 收尾：让 executor 退出，避免 store 关闭后写
        await _wait_status(store, "t1", {"CANCELLED", "SUCCESS"})

    async def test_secrets_passed_not_persisted(self, store, link):
        ex = FakeExecutor()
        agent = NodeAgent(store, {"ENSURE_MODEL": ex}, manager_link=link)
        await agent.handle_ensure(_tp(), secrets={"hf_token": "SECRET-X"})
        await _wait_status(store, "t1", {"CANCELLED", "SUCCESS"})

        assert ex.calls[0][1] == {"hf_token": "SECRET-X"}  # executor 收到内存 secrets
        t = await store.get("t1")
        assert "hf_token" not in str(t.payload) and "SECRET-X" not in str(t.payload)  # 不落盘
        assert "SECRET-X" not in str(link.events)  # 不入上报
        assert "t1" not in agent._secrets  # 终态即清

    async def test_exception_message_scrubbed(self, store, link):
        """H1：executor 抛携带 token 的异常 → message 脱敏后入 last_error / 上报。"""
        agent = NodeAgent(store, {"ENSURE_MODEL": RaisingExecutor()}, manager_link=link)
        await agent.handle_ensure(_tp(), secrets={"hf_token": "hf_SECRET_TOKEN"})
        t = await _wait_status(store, "t1", {"FAILED"})
        assert t.status == "FAILED"
        assert "hf_SECRET_TOKEN" not in (t.last_error or "")  # 不落 local_tasks
        assert "hf_SECRET_TOKEN" not in str(link.events)  # 不入 task_event
        assert "***" in (t.last_error or "")  # 脱敏标记保留（可排障）

    async def test_unknown_type_reports_failed(self, store, link):
        """registry 无此 type 的执行器 → FAILED（registry 兜底契约；未知 type 不崩）。"""
        agent = NodeAgent(store, manager_link=link)  # default_registry（仅 ENSURE_MODEL）
        bogus = TaskPayload(
            id="t1",
            type="PULL_IMAGE",
            resource_type="IMAGE",
            resource_key="img:foo",
            node_id="n1",
            status="PENDING",
            attempt=1,
            payload={},
        )
        await agent.handle_ensure(bogus)
        t = await _wait_status(store, "t1", {"FAILED"})
        assert t.status == "FAILED" and "无可执行器" in (t.last_error or "")
        assert any(e.status == "FAILED" for e in link.events)


class TestHandleCancel:
    async def test_cancel_reports_cancelled(self, store, link):
        ex = FakeExecutor()
        agent = NodeAgent(store, {"ENSURE_MODEL": ex}, manager_link=link)
        await agent.handle_ensure(_tp())
        await _drain()
        ack = await agent.handle_cancel("t1")
        assert ack.ok
        t = await _wait_status(store, "t1", {"CANCELLED"})
        assert t.status == "CANCELLED"
        assert any(e.status == "CANCELLED" for e in link.events)


class TestInstanceStatusChannel:
    """_emit 钩子：extra.instance_status → 额外 report_instance_status（在 task_event 之后）。"""

    async def test_fires_instance_report_when_key_present(self, store, link):
        tp = TaskPayload(
            id="t1",
            type="START_INSTANCE",
            resource_type="INSTANCE",
            resource_key="instance:dep1:0",
            node_id="n1",
            status="PENDING",
            attempt=1,
            payload={
                "deployment_id": "dep1",
                "instance_id": "i1",
                "container_name": "llmpod-dep1-i1",
                "spec": {"image": "x", "name": "llmpod-dep1-i1"},
            },
        )
        agent = NodeAgent(store, {"START_INSTANCE": InstanceStatusExecutor()}, manager_link=link)
        await agent.handle_ensure(tp)
        await _wait_status(store, "t1", {"SUCCESS"})

        assert [e.status for e in link.instance_reports] == ["STARTING", "READY"]
        ready = link.instance_reports[1]
        assert ready.deployment_id == "dep1" and ready.instance_id == "i1"
        assert ready.endpoint == "http://n1:8000"
        # task_event 照常发（审计权威来源），实例上报条数 ≤ task_event 条数
        assert len(link.events) >= len(link.instance_reports)

    async def test_no_instance_report_without_key(self, store, link, tmp_path):
        """普通 ENSURE_MODEL 上报（无 instance_status）不触发实例通道。"""
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")
        agent = NodeAgent(store, manager_link=link)
        await agent.handle_ensure(_tp(local_dir=d))
        await _wait_status(store, "t1", {"SUCCESS"})
        assert link.instance_reports == []

    async def test_no_instance_report_without_ids_in_payload(self, store, link):
        """payload 缺 deployment_id/instance_id → 不组装（防孤儿上报）。"""
        tp = TaskPayload(
            id="t1",
            type="START_INSTANCE",
            resource_type="INSTANCE",
            resource_key="instance:dep1:0",
            node_id="n1",
            status="PENDING",
            attempt=1,
            payload={},
        )
        agent = NodeAgent(store, {"START_INSTANCE": InstanceStatusExecutor()}, manager_link=link)
        await agent.handle_ensure(tp)
        await _wait_status(store, "t1", {"SUCCESS"})
        assert link.instance_reports == []


class TestRecover:
    async def test_present_inflight_success(self, store, link, tmp_path):
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")  # H1：verify 需权重才判在位
        await store.upsert(
            LocalTask(
                task_id="t1",
                type="ENSURE_MODEL",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                status="RUNNING",
                payload={"model_id": 1, "source_kind": "transformers_folder", "local_dir": str(d)},
            )
        )
        agent = NodeAgent(store, manager_link=link)
        await agent._recover()
        assert (await store.get("t1")).status == "SUCCESS"
        assert any(e.status == "SUCCESS" for e in link.events)

    async def test_absent_inflight_failed(self, store, link, tmp_path):
        await store.upsert(
            LocalTask(
                task_id="t2",
                type="ENSURE_MODEL",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                status="RUNNING",
                payload={"model_id": 1, "source_kind": "transformers_folder", "local_dir": str(tmp_path / "absent")},
            )
        )
        agent = NodeAgent(store, manager_link=link)
        await agent._recover()
        assert (await store.get("t2")).status == "FAILED"
        assert any(e.status == "FAILED" and "重启中断" in (e.message or "") for e in link.events)

    async def test_pending_left_for_redrive(self, store, link):
        await store.upsert(
            LocalTask(
                task_id="t3",
                type="ENSURE_MODEL",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                status="PENDING",
                payload={},
            )
        )
        agent = NodeAgent(store, manager_link=link)
        await agent._recover()
        assert (await store.get("t3")).status == "PENDING"  # 未被改写
        assert link.events == []  # 不上报（等 manager redrive 重新下发）


class TestRunSync:
    async def test_sync_to_manager_sends_states(self, store, link):
        await store.upsert(
            LocalTask(
                task_id="t1",
                type="ENSURE_MODEL",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                status="RUNNING",
            )
        )
        agent = NodeAgent(store, manager_link=link)
        await agent._sync_to_manager()
        assert len(link.syncs) == 1 and link.syncs[0][0].id == "t1"

    async def test_run_syncs_on_reconnect(self, store, link):
        await store.upsert(
            LocalTask(
                task_id="t1",
                type="ENSURE_MODEL",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                status="RUNNING",
            )
        )
        agent = NodeAgent(store, manager_link=link, sync_interval=0.02)
        loop_task = asyncio.create_task(agent.run())
        try:
            link._online = True  # 模拟 channel 就绪（False→True）
            await asyncio.sleep(0.15)
            assert any(sync for sync in link.syncs), f"sync 未触发: {link.syncs}"
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
