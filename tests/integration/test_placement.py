"""PlacementService 集成测试：active 去重 / attempt 自增与上限 / present 短路 /
recompute_demand 取消与不误杀。

用 FakeSession（单 where + Python 过滤，适配其限制）+ dispatch 桩（记录 ensure/cancel 下发）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from _fakes import FakeSession

import pytest

import llmpod.apps.manager.dispatch as dispatch_mod
from llmpod.apps.manager.deployment import placement as placement_mod
from llmpod.apps.manager.deployment.placement import (
    MAX_AUTO_ATTEMPTS,
    ModelStagingGate,
    PlacementService,
    model_resource_key,
)
from llmpod.core.exceptions import LLMPodError
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task


# ---- fixtures / helpers ----


def _patch_db(monkeypatch, session):
    @asynccontextmanager
    async def _fake():
        yield session

    monkeypatch.setattr(placement_mod, "get_db_context", _fake)


def _patch_dispatch(monkeypatch, ensures=None, cancels=None):
    async def _ensure(node_id, task, secrets=None):
        (ensures if ensures is not None else []).append((node_id, task, secrets))
        return {"ok": True}

    async def _cancel(node_id, task_id):
        (cancels if cancels is not None else []).append((node_id, task_id))
        return {"ok": True}

    monkeypatch.setattr(dispatch_mod, "ensure_task", _ensure)
    monkeypatch.setattr(dispatch_mod, "cancel_task", _cancel)


def _model(session, **kw):
    defaults = dict(
        id=1,
        name="m",
        model_type="llm",
        source="huggingface",
        source_url="org/m",
        source_kind="transformers_folder",
    )
    defaults.update(kw)
    return session.preload(Model(**defaults))


def _terminal_task(session, attempt, status="FAILED", tid=None):
    return session.preload(
        Task(
            id=tid or f"t{attempt}",
            resource_type="MODEL_PLACEMENT",
            resource_key="model:1:n1",
            node_id="n1",
            type="ENSURE_MODEL",
            status=status,
            attempt=attempt,
            payload={},
        )
    )


# ---- resource_key ----


def test_resource_key_format():
    assert model_resource_key(1, "n1") == "model:1:n1"
    assert model_resource_key(42, "node-a") == "model:42:node-a"


# ---- ModelStagingGate（staging 放行依据：只认复核任务终态 + node_model present） ----


class TestModelStagingGate:
    async def test_only_success_and_present_counts(self, monkeypatch):
        """复核任务 SUCCESS 且 node_model present 才放行；FAILED 任务不放行。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(NodeModel(model_id=1, node_id="n1", status="present", local_dir="/m"))
        session.preload(NodeModel(model_id=1, node_id="n2", status="present", local_dir="/m"))
        _terminal_task(session, 1, status="SUCCESS", tid="t-ok")  # n1 的复核任务
        session.preload(
            Task(
                id="t-fail",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n2",
                node_id="n2",
                type="ENSURE_MODEL",
                status="FAILED",
                attempt=1,
                payload={},
            )
        )
        ids = {"n1": "t-ok", "n2": "t-fail"}

        async def _ensure(self, mid, nid):
            return ids.get(nid)

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        over_cap = await gate.dispatch({"n1", "n2"})
        assert over_cap == set()
        confirmed, by_node = await gate.advance(session, {"n1", "n2"}, over_cap)
        assert confirmed == {"n1"}
        assert set(by_node) == {"n1", "n2"}

    async def test_over_cap_failed_raises(self, monkeypatch):
        """超 attempt 上限（ensure_present 返回 None）且 node_model 终态 failed → 判死。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(NodeModel(model_id=1, node_id="n1", status="failed", local_dir="/m", last_error="boom"))

        async def _ensure(self, mid, nid):
            return None

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        over_cap = await gate.dispatch({"n1"})
        assert over_cap == {"n1"}
        with pytest.raises(LLMPodError):
            await gate.advance(session, {"n1"}, over_cap)

    async def test_pending_record_is_redispatched(self, monkeypatch):
        """已记录节点任务仍 PENDING（首发 RPC 瞬时失败）→ 下一轮 dispatch 补发（幂等）。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="PENDING",
                attempt=1,
                payload={},
            )
        )
        calls: list[str] = []

        async def _ensure(self, mid, nid):
            calls.append(nid)
            return "t1"

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        await gate.dispatch({"n1"})  # 首轮派发
        await gate.dispatch({"n1"})  # 下一轮（现态重读 t1 仍 PENDING）
        assert calls == ["n1", "n1"]  # 补发

    async def test_running_record_not_redispatched(self, monkeypatch):
        """已记录节点任务 RUNNING（node 在跑）→ 不打扰，不重派。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )

        async def _ensure(self, mid, nid):
            raise AssertionError("RUNNING 态不应重派")

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        gate._tasks["n1"] = "t1"
        await gate.dispatch({"n1"})  # 现态重读 RUNNING → 不触发 ensure_present 即通过


# ---- ensure_present ----


class TestEnsurePresent:
    async def test_creates_task_and_node_model(self, monkeypatch):
        session = FakeSession()
        _model(session)
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        task_id = await PlacementService().ensure_present(1, "n1")

        assert task_id
        tasks = session.all(Task)
        assert len(tasks) == 1
        t = tasks[0]
        assert t.status == "PENDING" and t.attempt == 1
        assert t.type == "ENSURE_MODEL" and t.resource_type == "MODEL_PLACEMENT"
        assert t.resource_key == "model:1:n1" and t.node_id == "n1"

        nm = session.all(NodeModel)
        assert len(nm) == 1
        assert nm[0].desired_state == "present" and nm[0].status == "absent"
        # local_dir 现为 {name-slug}-{digest}（不用自增 id，清库重建 id 不串模型）
        assert nm[0].local_dir.endswith("/m-79436ad53f8c")

        # secrets 边界：payload 不含凭证，dispatch 收独立 secrets（无凭证→None）
        assert "source_auth_encrypted" not in t.payload
        assert "hf_token" not in str(t.payload)
        assert ensures and ensures[0][2] is None

    async def test_source_subpath_flows_into_payload(self, monkeypatch):
        """C1：Model.source_subpath → task payload（供 node allow_patterns 收窄 + verify 定位）。"""
        session = FakeSession()
        _model(session, source_subpath="Q4_K_M", source_kind="gguf_shards")
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        await PlacementService().ensure_present(1, "n1")

        tasks = session.all(Task)
        assert tasks[0].payload["source_subpath"] == "Q4_K_M"
        assert tasks[0].payload["source_kind"] == "gguf_shards"

    async def test_dedup_active_no_second_task(self, monkeypatch):
        """并发/重复 ensure_present 同 (model,node) 只建一个 active task。"""
        session = FakeSession()
        _model(session)
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        id1 = await PlacementService().ensure_present(1, "n1")
        id2 = await PlacementService().ensure_present(1, "n1")

        assert id1 == id2
        assert len(session.all(Task)) == 1  # 不建第二个 active

    async def test_present_creates_reverify_task(self, monkeypatch):
        """present 只是缓存声明：不短路，建复核任务交 node check-existing 裁决磁盘真相。

        防「status=present 但目录空」直通容器（空权重上线反复崩溃）。
        """
        session = FakeSession()
        _model(session)
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="present"))
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        ret = await PlacementService().ensure_present(1, "n1")

        assert ret is not None  # 不再返回 None 短路
        tasks = session.all(Task)
        assert len(tasks) == 1 and tasks[0].attempt == 1  # 无 FAILED 历史 → attempt 从 1 起
        assert len(ensures) == 1

    async def test_attempt_increments_after_terminal(self, monkeypatch):
        session = FakeSession()
        _model(session)
        _terminal_task(session, attempt=1, status="FAILED")
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        tid = await PlacementService().ensure_present(1, "n1")

        tasks = session.all(Task)
        assert len(tasks) == 2  # 历史终态 + 新建
        new = next(t for t in tasks if t.id == tid)
        assert new.attempt == 2 and new.status == "PENDING"

    async def test_attempt_cap_blocks_creation(self, monkeypatch):
        """超过 MAX_AUTO_ATTEMPTS 不再自动建，标 node_model.failed。"""
        session = FakeSession()
        _model(session)
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal_task(session, attempt=i, status="FAILED")
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        ret = await PlacementService().ensure_present(1, "n1")

        assert ret is None
        assert len(session.all(Task)) == MAX_AUTO_ATTEMPTS  # 没新建
        nm = session.all(NodeModel)[0]
        assert nm.status == "failed" and nm.desired_state == "present"
        assert ensures == []


# ---- recompute_demand ----


class TestRecomputeDemand:
    async def test_cancels_unneeded_download(self, monkeypatch):
        """无人需要的下载 → desired=absent + status=absent + task=CANCELLED + cancel_task。"""
        session = FakeSession()
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="downloading"))
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        cancels = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, cancels=cancels)

        await PlacementService().recompute_demand()

        nm = session.all(NodeModel)[0]
        assert nm.desired_state == "absent" and nm.status == "absent"
        assert session.all(Task)[0].status == "CANCELLED"
        assert cancels == [("n1", "t1")]

    async def test_cancels_pending_on_absent_node_model(self, monkeypatch):
        """无人需要的 (model,node) 即使 node_model 仍 absent（PENDING 尚未触达 node），
        其遗留 PENDING 任务也必须取消——否则 node 上线后按 PENDING 误触发下载。"""
        session = FakeSession()
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="absent"))
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="PENDING",
                attempt=1,
                payload={},
            )
        )
        cancels = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, cancels=cancels)

        await PlacementService().recompute_demand()

        nm = session.all(NodeModel)[0]
        assert nm.desired_state == "absent"
        assert nm.status == "absent"  # 本就 absent，未被谎改为 downloading
        assert session.all(Task)[0].status == "CANCELLED"
        assert cancels == [("n1", "t1")]

    async def test_keeps_needed_download(self, monkeypatch):
        """running 部署仍需要 → 不取消、status 不动。"""
        session = FakeSession()
        session.preload(
            ModelDeployment(
                deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        session.preload(
            Instance(instance_id="i1", deployment_id="dep-1", role="full", assigned_node="n1", status="running")
        )
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="downloading"))
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        cancels = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, cancels=cancels)

        await PlacementService().recompute_demand()

        nm = session.all(NodeModel)[0]
        assert nm.desired_state == "present" and nm.status == "downloading"
        assert session.all(Task)[0].status == "RUNNING"
        assert cancels == []

    async def test_partial_keep_when_other_dep_needs_it(self, monkeypatch):
        """停止共享模型的一个部署，另一部署仍需要 → 不误杀下载。"""
        session = FakeSession()
        session.preload(
            ModelDeployment(
                deployment_id="dep-a", cluster_id=1, name="a", model_id=1, engine="vllm", status="stopped", spec={}
            )
        )
        session.preload(
            ModelDeployment(
                deployment_id="dep-b", cluster_id=1, name="b", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        session.preload(
            Instance(instance_id="ia", deployment_id="dep-a", role="full", assigned_node="n1", status="stopped")
        )
        session.preload(
            Instance(instance_id="ib", deployment_id="dep-b", role="full", assigned_node="n1", status="running")
        )
        session.preload(NodeModel(model_id=1, node_id="n1", desired_state="present", status="downloading"))
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        cancels = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, cancels=cancels)

        await PlacementService().recompute_demand()

        assert session.all(NodeModel)[0].desired_state == "present"
        assert session.all(Task)[0].status == "RUNNING"
        assert cancels == []


# ---- redrive_pending ----


class TestRedrive:
    async def test_redispatches_pending_for_node(self, monkeypatch):
        session = FakeSession()
        _model(session)
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="PENDING",
                attempt=1,
                payload={"model_id": 1},
            )
        )
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        n = await PlacementService().redrive_pending("n1")

        assert n == 1
        assert ensures and ensures[0][0] == "n1"
        assert ensures[0][1].id == "t1"
        assert ensures[0][2] is None  # 无凭证 → None

    async def test_skips_when_model_gone(self, monkeypatch):
        session = FakeSession()
        # 不建 model（模型已删）
        session.preload(
            Task(
                id="t1",
                resource_type="MODEL_PLACEMENT",
                resource_key="model:1:n1",
                node_id="n1",
                type="ENSURE_MODEL",
                status="PENDING",
                attempt=1,
                payload={"model_id": 1},
            )
        )
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        n = await PlacementService().redrive_pending("n1")

        assert n == 0 and ensures == []

    async def test_node_filter_excludes_other_nodes(self, monkeypatch):
        session = FakeSession()
        _model(session)
        for tid, nid in (("t1", "n1"), ("t2", "n2")):
            session.preload(
                Task(
                    id=tid,
                    resource_type="MODEL_PLACEMENT",
                    resource_key=f"model:1:{nid}",
                    node_id=nid,
                    type="ENSURE_MODEL",
                    status="PENDING",
                    attempt=1,
                    payload={"model_id": 1},
                )
            )
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        n = await PlacementService().redrive_pending("n1")

        assert n == 1
        assert [e[0] for e in ensures] == ["n1"]  # 不误发 n2 的 PENDING

    async def test_skips_non_ensure_model_tasks(self, monkeypatch):
        """容器任务（PULL_IMAGE 等）的 PENDING 不走模型分发 redrive（重发归 reconciler）。"""
        session = FakeSession()
        _model(session)
        session.preload(
            Task(
                id="tp",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                type="PULL_IMAGE",
                status="PENDING",
                attempt=1,
                payload={"image": "img:v1"},
            )
        )
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        n = await PlacementService().redrive_pending("n1")

        assert n == 0 and ensures == []


class TestPeriodicRedriveLoop:
    """manager 的周期 redrive：单轮异常不杀循环、cancel 正确退出。"""

    async def test_loop_redispatches_and_survives_errors(self, monkeypatch):
        import asyncio

        import llmpod.apps.manager.app as manager_app
        from llmpod.core.taskwatch import BackgroundTaskManager

        rounds = {"n": 0}

        async def _redrive(self, node_id=None):
            rounds["n"] += 1
            if rounds["n"] == 1:
                raise RuntimeError("db blip")  # 单轮异常
            if rounds["n"] >= 3:
                raise asyncio.CancelledError  # 第三轮后退出循环（模拟 shutdown cancel）
            return 0

        monkeypatch.setattr(PlacementService, "redrive_pending", _redrive)
        background_tasks = BackgroundTaskManager()
        task = background_tasks.every(manager_app._redrive_once, name="model-redrive", interval=0.01)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        await background_tasks.stop()
        assert rounds["n"] == 3  # 异常后循环仍在跑
