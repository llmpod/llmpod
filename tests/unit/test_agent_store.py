"""agent_store.TaskStore 单测：SQLAlchemy async ORM 本地任务持久化（upsert 幂等/恢复/COALESCE）。

用 tmp_path 真实 sqlite 文件（aiosqlite 驱动，无 FakeSession/RPC 依赖），覆盖
「Node 重启不丢任务上下文」。
"""

from __future__ import annotations

import pytest

from llmpod.apps.node.agent_store import TaskStore
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import TaskPayload


def _payload(task_id="t1", status="PENDING", progress=0, payload=None) -> TaskPayload:
    return TaskPayload(
        id=task_id,
        type="ENSURE_MODEL",
        resource_type="MODEL_PLACEMENT",
        resource_key="model:1:n1",
        node_id="n1",
        status=status,
        attempt=1,
        progress=progress,
        payload=payload or {"model_id": 1, "source": "huggingface"},
    )


@pytest.fixture
async def store(tmp_path):
    s = TaskStore(str(tmp_path / "tasks.db"))
    await s.init()
    yield s
    await s.close()


class TestRoundTrip:
    async def test_upsert_then_get(self, store):
        await store.upsert(LocalTask.from_payload(_payload(progress=10)))
        t = await store.get("t1")
        assert t is not None
        assert t.task_id == "t1" and t.type == "ENSURE_MODEL"
        assert t.status == "PENDING" and t.progress == 10
        assert t.payload["source"] == "huggingface"

    async def test_payload_roundtrip_dict(self, store):
        await store.upsert(LocalTask.from_payload(_payload(payload={"model_id": 7, "nested": {"a": [1, 2]}})))
        assert (await store.get("t1")).payload == {"model_id": 7, "nested": {"a": [1, 2]}}

    async def test_get_missing_returns_none(self, store):
        assert await store.get("nope") is None


class TestUpsertIdempotent:
    async def test_active_progress_not_overwritten(self, store):
        """active 任务的执行期进度不被 ensure_task 重发冲掉（幂等语义）。"""
        await store.upsert(LocalTask.from_payload(_payload(status="RUNNING", progress=50)))
        # manager 重发同一任务（progress=0）——不应回退 node 本地 50
        await store.upsert(LocalTask.from_payload(_payload(status="PENDING", progress=0)))
        t = await store.get("t1")
        assert t.progress == 50 and t.status == "RUNNING"

    async def test_overwrite_running_resets(self, store):
        await store.upsert(LocalTask.from_payload(_payload(status="RUNNING", progress=50)))
        await store.upsert(
            LocalTask.from_payload(_payload(status="PENDING", progress=0)),
            overwrite_running=True,
        )
        t = await store.get("t1")
        assert t.progress == 0 and t.status == "PENDING"

    async def test_terminal_row_overwritten(self, store):
        """终态行再次 upsert 按新值覆盖（允许重新激活）。"""
        await store.upsert(LocalTask.from_payload(_payload(status="FAILED", progress=80)))
        await store.upsert(LocalTask.from_payload(_payload(status="PENDING", progress=0)))
        t = await store.get("t1")
        assert t.status == "PENDING" and t.progress == 0


class TestUpdateAndQuery:
    async def test_update_status_progress(self, store):
        await store.upsert(LocalTask.from_payload(_payload()))
        await store.update_status("t1", "RUNNING", progress=30)
        t = await store.get("t1")
        assert t.status == "RUNNING" and t.progress == 30

    async def test_update_status_coalesce_keeps_progress(self, store):
        """progress=None / last_error=None → COALESCE 保留既有值。"""
        await store.upsert(LocalTask.from_payload(_payload(progress=42)))
        await store.update_status("t1", "FAILED", last_error="disk full")
        t = await store.get("t1")
        assert t.status == "FAILED" and t.progress == 42 and t.last_error == "disk full"

    async def test_list_active_filters_terminal(self, store):
        await store.upsert(LocalTask.from_payload(_payload(task_id="t1", status="RUNNING")))
        await store.upsert(LocalTask.from_payload(_payload(task_id="t2", status="PENDING")))
        await store.upsert(LocalTask.from_payload(_payload(task_id="t3", status="SUCCESS")))
        await store.upsert(LocalTask.from_payload(_payload(task_id="t4", status="FAILED")))
        active = {t.task_id for t in await store.list_active()}
        assert active == {"t1", "t2"}
        assert len(await store.list_all()) == 4

    async def test_delete(self, store):
        await store.upsert(LocalTask.from_payload(_payload()))
        await store.delete("t1")
        assert await store.get("t1") is None


class TestRecovery:
    async def test_restart_recovers_context(self, tmp_path):
        """重启（新 store 实例同文件）读回全部任务上下文。"""
        path = str(tmp_path / "tasks.db")
        s1 = TaskStore(path)
        await s1.init()
        await s1.upsert(
            LocalTask.from_payload(_payload(task_id="t1", status="RUNNING", progress=66, payload={"model_id": 9}))
        )
        await s1.close()

        s2 = TaskStore(path)
        await s2.init()
        tasks = await s2.list_all()
        await s2.close()

        assert len(tasks) == 1
        t = tasks[0]
        assert t.task_id == "t1" and t.status == "RUNNING" and t.progress == 66
        assert t.payload == {"model_id": 9}


class TestConversions:
    def test_from_payload_and_to_state(self):
        tp = _payload(status="RUNNING", progress=77)
        lt = LocalTask.from_payload(tp)
        assert lt.task_id == tp.id and lt.resource_key == tp.resource_key
        st = lt.to_state()
        assert st.id == "t1" and st.status == "RUNNING" and st.progress == 77
        assert st.resource_key == "model:1:n1"
