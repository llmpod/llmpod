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


async def _drive_budget_exhausted(gate, session) -> None:
    """驱动 dispatch 轮询直到会话失败预算耗尽（每轮把在派任务置 FAILED 模拟失败上报）。"""
    for _ in range(10):
        for tid in list(gate._tasks.values()):
            session._store[Task][tid].status = "FAILED"
        await gate.dispatch({"n1"})
        if gate._failed_seen.get("n1", 0) >= MAX_AUTO_ATTEMPTS:
            return
    raise AssertionError("预算未耗尽（10 轮内未观察到 3 次失败）")


# ---- resource_key ----


def test_resource_key_format():
    assert model_resource_key(1, "n1") == "model:1:n1"
    assert model_resource_key(42, "node-a") == "model:42:node-a"


# ---- ModelStagingGate（staging 放行依据：只认复核任务终态 + node_model present） ----


class TestModelStagingGate:
    async def test_only_success_and_present_counts(self, monkeypatch):
        """共享键最新行 SUCCESS 且 node_model present 才放行；最新 FAILED 不放行。"""
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
        await gate.dispatch({"n1", "n2"})
        confirmed, by_node = await gate.advance(session, {"n1", "n2"})
        assert confirmed == {"n1"}
        assert set(by_node) == {"n1", "n2"}

    async def test_over_cap_failed_raises(self, monkeypatch):
        """会话内观察到 3 次下载失败（node_model 终态 failed）→ 判死抛错。

        预算按 staging 会话计（自己观察到的失败数），与共享键上的历史失败无关。
        """
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(NodeModel(model_id=1, node_id="n1", status="failed", local_dir="/m", last_error="boom"))
        seq = iter(("t1", "t2", "t3"))
        for i, tid in enumerate(("t1", "t2", "t3"), start=1):
            _terminal_task(session, i, status="PENDING", tid=tid)

        async def _ensure(self, mid, nid):
            return next(seq, None)

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        await _drive_budget_exhausted(gate, session)
        assert gate._failed_seen["n1"] == MAX_AUTO_ATTEMPTS  # 第 3 次观察失败后预算耗尽
        assert next(seq, None) is None  # t1/t2/t3 恰好用完，无第 4 个任务
        with pytest.raises(LLMPodError):
            await gate.advance(session, {"n1"})

    async def test_over_cap_present_confirmed_via_shared_success(self, monkeypatch):
        """预算耗尽但共享键最新任务 SUCCESS（别的会话下成）+ node_model present → 放行。

        不放行则本会话已不建任务、观察不到那行终态，staging 轮询永远等不到结论。
        同款陈旧缓存（present + 最新 FAILED）不放行——见下一条。
        """
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(NodeModel(model_id=1, node_id="n1", status="present", local_dir="/m", model_path="/m/q"))
        # 本会话观察失败的 t1..t3 之后，别的会话建的第 4 个 attempt 下成了
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal_task(session, i, status="FAILED")
        _terminal_task(session, 4, status="SUCCESS", tid="t-ok")

        async def _ensure(self, mid, nid):
            return "t3"  # 只派发到第 3 个，预算耗尽后不再调

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        await _drive_budget_exhausted(gate, session)

        confirmed, by_node = await gate.advance(session, {"n1"})
        assert confirmed == {"n1"}  # 共享键最新 SUCCESS + present → 放行
        assert by_node["n1"].model_path == "/m/q"

    async def test_over_cap_stale_present_latest_failed_not_confirmed(self, monkeypatch):
        """预算耗尽、node_model 停在陈旧 present、共享键最新行 FAILED → 不放行。

        present 缓存可能是上一轮残影（复核发现配套文件缺失），据此放行会让缺权重的
        容器上线；判死条件（node_model FAILED）也未达 → 悬在等待由调用方总预算兜底。
        """
        session = FakeSession()
        _patch_db(monkeypatch, session)
        session.preload(NodeModel(model_id=1, node_id="n1", status="present", local_dir="/m"))
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal_task(session, i, status="FAILED")

        async def _ensure(self, mid, nid):
            return "t3"

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)
        await _drive_budget_exhausted(gate, session)

        confirmed, _ = await gate.advance(session, {"n1"})
        assert confirmed == set()  # 不判死（node_model 非 failed）也不放行

    async def test_history_failures_do_not_lock_new_session(self, monkeypatch):
        """别的部署留下的 3 条 FAILED 历史 + 全新 gate：会话预算从零计，照常建任务。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal_task(session, i, status="FAILED", tid=f"t-old-{i}")

        async def _ensure(self, mid, nid):
            return "t-new"

        monkeypatch.setattr(PlacementService, "ensure_present", _ensure)
        gate = ModelStagingGate(1)  # 新部署/手动启动 → 新会话
        await gate.dispatch({"n1"})

        assert gate._failed_seen.get("n1", 0) == 0  # 历史失败不计入本会话预算
        assert gate._tasks["n1"] == "t-new"  # 照常派发新任务

    async def test_pending_record_is_redispatched(self, monkeypatch):
        """已记录节点任务仍 PENDING（首发 RPC 瞬时失败）→ 下一轮 dispatch 补发（幂等）。"""
        session = FakeSession()
        _patch_db(monkeypatch, session)
        _terminal_task(session, 1, status="PENDING", tid="t1")
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
        _terminal_task(session, 1, status="RUNNING", tid="t1")

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
        # local_dir 为 {store}/{name-slug}（与 hf download --local-dir 同布局，用户可直管）
        assert nm[0].local_dir.endswith("/m")

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

    async def test_local_dir_refreshed_on_new_task(self, monkeypatch, tmp_path):
        """nm.local_dir 建新任务时刷新为当前 canonical（不再首写冻结）：模型行换
        source_url 后，新任务落新目录，旧缓存目录不拖累。"""
        monkeypatch.setattr(placement_mod.settings, "model_store_dir", str(tmp_path))
        old_dir = tmp_path / "old-model"
        new_dir = tmp_path / "new-model"
        # 源目录必须真实存在（校验会查存在性——空清单会在节点侧「下载成功」一个空目录）
        new_dir.mkdir()
        session = FakeSession()
        model = _model(session, source="local", source_url=str(old_dir))
        session.preload(
            NodeModel(model_id=1, node_id="n1", desired_state="present", status="present", local_dir=str(old_dir))
        )
        model.source_url = str(new_dir)  # 模型行更新（旧实现冻结旧值）
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        await PlacementService().ensure_present(1, "n1")

        nm = session.all(NodeModel)[0]
        assert nm.local_dir == str(new_dir)  # 刷新为当前 canonical
        task = session.all(Task)[0]
        assert task.payload["local_dir"] == str(new_dir)  # 任务用新目录下发

    async def test_local_source_invalid_path_rejected(self, monkeypatch):
        """local 源 source_url 在 store 外 → staging 显式失败（中文领域错误），
        不产生「下载到意外路径」的任务。"""
        session = FakeSession()
        _model(session, source="local", source_url="/etc/passwd")
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=[])

        with pytest.raises(LLMPodError, match="合法目录"):
            await PlacementService().ensure_present(1, "n1")
        assert session.all(Task) == []  # 未建任务

    async def test_history_failures_do_not_block_creation(self, monkeypatch):
        """共享键上的历史失败不锁新会话：别的部署/上一轮留下的 FAILED 行不影响本会话
        建下一 attempt（失败预算由 ModelStagingGate 按 staging 会话计，不在这里判）。"""
        session = FakeSession()
        _model(session)
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal_task(session, attempt=i, status="FAILED")
        ensures = []
        _patch_db(monkeypatch, session)
        _patch_dispatch(monkeypatch, ensures=ensures)

        ret = await PlacementService().ensure_present(1, "n1")

        assert ret is not None  # 历史超限不拒绝：新 attempt 正常建出
        tasks = session.all(Task)
        assert len(tasks) == MAX_AUTO_ATTEMPTS + 1
        assert tasks[-1].attempt == MAX_AUTO_ATTEMPTS + 1 and tasks[-1].status == "PENDING"
        assert len(ensures) == 1  # 正常下发


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
