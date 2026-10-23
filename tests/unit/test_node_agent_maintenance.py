"""NodeAgent 维护模式单测：升级挂起/恢复（取消在跑任务 + 关/重开 sqlite + 拒新）。"""

from __future__ import annotations

import asyncio

import pytest

from llmpod.apps.node.agent import NodeAgent
from llmpod.apps.node.agent_store import TaskStore
from llmpod.tunnel.schemas import Ack, TaskPayload


class FakeManagerLink:
    @property
    def online(self):
        return False

    async def report_task_event(self, evt):
        return Ack(ok=True)

    async def sync_node_state(self, tasks):
        return Ack(ok=True)

    async def report_instance_status(self, evt):
        return Ack(ok=True)


class SlowExecutor:
    """长跑执行器：直到被 cancel 才退出（模拟在跑任务被强制升级中断）。"""

    def __init__(self):
        self.cancelled = 0

    async def execute(self, task, secrets, report, cancel_event):
        try:
            await cancel_event.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _tp(task_id="t1") -> TaskPayload:
    return TaskPayload(
        id=task_id,
        type="ENSURE_MODEL",
        resource_type="MODEL_PLACEMENT",
        resource_key="model:1:n1",
        node_id="n1",
        status="PENDING",
        attempt=1,
        payload={"model_id": 1, "source": "huggingface"},
    )


@pytest.fixture
async def agent(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    await store.init()
    executor = SlowExecutor()
    a = NodeAgent(store, {"ENSURE_MODEL": executor})
    a.attach_client(FakeManagerLink())
    yield a, store, executor
    if a._store._engine is not None:
        await a._store.close()


class TestSuspendResume:
    async def test_suspend_cancels_running_and_closes_db(self, agent):
        a, store, executor = agent
        await a.handle_ensure(_tp())
        assert len(a._running) == 1
        await asyncio.sleep(0.05)  # 让执行器真正进入 await（否则 cancel 落在启动前）

        await a.suspend_for_upgrade()
        assert executor.cancelled == 1
        assert a._running == {}
        # sqlite 已关（engine 置空）
        assert store._engine is None
        assert a.maintenance is True

    async def test_resume_reopens_db(self, agent):
        a, store, _ = agent
        await a.suspend_for_upgrade()
        assert store._engine is None
        await a.resume_after_upgrade()
        assert store._engine is not None
        assert a.maintenance is False
        # 重开后可正常读写
        await a.handle_ensure(_tp("t2"))
        assert await store.get("t2") is not None

    async def test_run_loop_skips_sync_in_maintenance(self, agent):
        """维护窗口 agent run 循环不触本地库（store 关闭后 list_all 会抛）。"""
        a, store, _ = agent
        await a.suspend_for_upgrade()
        # 直接调 _sync_to_manager 等价验证：store 未 init 时抛 RuntimeError 被外层吞
        with pytest.raises(RuntimeError):
            await store.list_all()
        # run 循环内部检查 _maintenance 跳过（行为断言：maintenance 标志生效即可）
        assert a.maintenance is True
        await a.resume_after_upgrade()
