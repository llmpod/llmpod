"""StopService 集成测试：任务化停止收敛（cancel START → STOP_INSTANCE → 轮询 → 兜底）。

FakeSession + dispatch 捕获 + orchestrator 桩（cleanup/ledger），不触真实 node。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from llmpod.apps.manager.deployment import stop as stop_mod
from llmpod.apps.manager.deployment.stop import StopService
from llmpod.apps.manager.deployment.task_ops import (
    pull_resource_key,
    start_resource_key,
    stop_resource_key,
)
from llmpod.config.constants import TaskType
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task


class FakeSession:
    def __init__(self):
        self._store: dict = {}
        self._staged: list = []
        self._counters: dict = {}

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

    def all(self, cls):
        return list(self._store.get(cls, {}).values())

    def _pk(self, obj):
        pk_cols = list(obj.__table__.primary_key.columns)
        if pk_cols:
            return getattr(obj, pk_cols[0].key, None)
        return None

    async def get(self, cls, pk):
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

    def begin_nested(self):
        """SAVEPOINT 桩：还原为直通上下文（回滚到保存点的语义由 service 侧承担）。"""

        @asynccontextmanager
        async def _sp():
            yield

        return _sp()

    async def flush(self):
        for obj in self._staged:
            cls = type(obj)
            pk = self._pk(obj)
            if pk is None and hasattr(obj, "id"):
                n = self._counters.get(cls, 0) + 1
                self._counters[cls] = n
                obj.id = n
                pk = n
            self._store.setdefault(cls, {})[pk] = obj
        self._staged = []

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, *_a, **_k):
        pass

    async def execute(self, stmt):
        # 批量 delete()：等值/IN where 匹配的行直接从 store 移除（clear_planning_slate
        # 的四层清口走此形状）；select 走下方原路径
        if type(stmt).__name__ == "Delete":
            table = stmt.table
            cls = next(
                (
                    c
                    for c in self._store
                    if getattr(c, "__table__", None) is not None and c.__table__.name == table.name
                ),
                None,
            )
            if cls is not None:
                store = self._store.get(cls, {})
                wc = stmt.whereclause
                if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
                    val = getattr(wc.right, "effective_value", None) or getattr(wc.right, "value", wc.right)
                    keys = [
                        k
                        for k, o in store.items()
                        if (
                            getattr(o, wc.left.key, None) in val
                            if isinstance(val, (list, tuple, set))
                            else getattr(o, wc.left.key, None) == val
                        )
                    ]
                    for k in keys:
                        del store[k]
            return _FakeResult([])
        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        objs = list(self._store.get(entity, {}).values())
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                objs = [o for o in objs if getattr(o, wc.left.key, None) in val]
            else:
                objs = [o for o in objs if getattr(o, wc.left.key, None) == val]
        return _FakeResult(objs)

    async def delete(self, obj):
        for d in self._store.values():
            for k in list(d):
                if d[k] is obj:
                    del d[k]


class _FakeResult:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self._objs

    def scalar(self):
        return self._objs[0] if self._objs else None

    def scalar_one_or_none(self):
        return self._objs[0] if self._objs else None


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def patched_db(monkeypatch, session):
    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(stop_mod, "get_db_context", _fake_db)
    return session


def _deployment(session, status="running", did="dep-1"):
    return session.preload(
        ModelDeployment(
            deployment_id=did,
            cluster_id=1,
            name="d",
            model_id=1,
            engine="vllm",
            status=status,
            phase="RUNNING",
        )
    )


def _instance(session, did="dep-1", status="READY", rank=0, node_rank=0):
    return session.preload(
        Instance(
            instance_id=f"i-{rank}",
            deployment_id=did,
            model_deployment_instance_id="eng-1",
            role="full",
            rank=rank,
            assigned_node="n1",
            status=status,
            spec={"rank": rank, "node_rank": node_rank, "engine_instance_id": "eng-1"},
        )
    )


class _Recorder:
    def __init__(self, session, emulate_stop=True):
        self.session = session
        self.emulate_stop = emulate_stop
        self.ensures = []
        self.cancels = []

    def install(self, monkeypatch):
        async def _dispatch(task):
            self.ensures.append(task)
            # 模拟 Node 立即执行 STOP 并上报 STOPPED
            if self.emulate_stop and task.type == TaskType.STOP_INSTANCE:
                inst_id = (task.payload or {}).get("instance_id")
                inst = self.session._store.get(Instance, {}).get(inst_id)
                if inst is not None:
                    inst.status = "STOPPED"

        async def _cancel(node_id, task_id):
            self.cancels.append((node_id, task_id))

        from llmpod.apps.manager.deployment import task_ops

        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)
        monkeypatch.setattr(task_ops, "dispatch_cancel_task", _cancel)


def _patch_orch(monkeypatch, cleanups, released, instances):
    """桩 orchestrator 的 label 兜底清理 + VRAM 释放。"""
    from llmpod.apps.manager.deployment import orchestrator as orch_mod

    async def _cleanup(self, deployment_id, node_ids):
        cleanups.append((deployment_id, node_ids))

    async def _release(self, deployment_id):
        released.append(deployment_id)

    monkeypatch.setattr(orch_mod.DeploymentOrchestrator, "cleanup_deployment_containers", _cleanup)
    monkeypatch.setattr(orch_mod.DeploymentOrchestrator, "release_vram", _release)
    orch_mod._orchestrator = None


class TestStopInstances:
    async def test_stop_flow(self, patched_db, monkeypatch):
        """stop：cancel START → ensure STOP（纯停，payload 无 remove）→ 轮询 STOPPED → 全停。

        StopService 只停不删——容器删除归 delete 的 cleanup_deployment_containers，不在本服务。
        """
        session = patched_db
        _deployment(session)
        _instance(session, status="READY")
        # 活跃 START task（运行中，须先取消）
        session.preload(
            Task(
                id="start-1",
                resource_type="INSTANCE",
                resource_key=start_resource_key("eng-1", 0),
                node_id="n1",
                type="START_INSTANCE",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1")

        assert ok is True
        assert rec.cancels == [("n1", "start-1")]  # START task 先被取消
        assert (await session.get(Task, "start-1")).status == "CANCELLED"
        # STOP task 被创建并下发（resource_key 带 :stop 后缀，payload 不含 remove）
        assert len(rec.ensures) == 1
        assert rec.ensures[0].type == TaskType.STOP_INSTANCE
        assert rec.ensures[0].resource_key == stop_resource_key("eng-1", 0)
        assert "remove" not in rec.ensures[0].payload  # 纯 stop，无 remove 语义
        assert (await session.get(Instance, "i-0")).status == "STOPPED"
        assert cleanups == []  # StopService 不删容器（delete 才 cleanup）

    async def test_already_stopped_idempotent(self, patched_db, monkeypatch):
        """实例已 STOPPED → 不重复建 STOP task，直接收敛（stop 路径不扫除）。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="STOPPED")
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1")

        assert ok is True
        assert rec.ensures == []  # 幂等：不重建
        assert cleanups == []  # stop 路径不扫除

    async def test_no_instances_returns_true(self, patched_db, monkeypatch):
        session = patched_db
        _deployment(session)
        rec = _Recorder(session)
        rec.install(monkeypatch)
        assert await StopService().stop_instances("dep-1") is True
        assert rec.ensures == [] and rec.cancels == []

    async def test_dual_gate_stop_task_success(self, patched_db, monkeypatch):
        """实例上报丢失但 STOP task SUCCESS → 双门判定视为已停 + 补写 STOPPED。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="STOPPING")  # 实例状态未翻 STOPPED
        # 预置已 SUCCESS 的 STOP task
        session.preload(
            Task(
                id="stop-1",
                resource_type="INSTANCE",
                resource_key=stop_resource_key("eng-1", 0),
                node_id="n1",
                type=TaskType.STOP_INSTANCE,
                status="SUCCESS",
                attempt=1,
                payload={},
            )
        )
        rec = _Recorder(session, emulate_stop=False)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1")
        assert ok is True  # 双门：task SUCCESS 兜底
        assert (await session.get(Instance, "i-0")).status == "STOPPED"  # 补写

    async def test_timeout_returns_false_no_cleanup(self, patched_db, monkeypatch):
        """轮询超时 → 返回 False；StopService 不做 cleanup（容器删除归 delete）。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="READY")
        rec = _Recorder(session, emulate_stop=False)  # 不上报 STOPPED
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1", timeout=0.3)
        assert ok is False
        assert cleanups == []  # StopService 永不 cleanup

    async def test_pull_tasks_not_cancelled(self, patched_db, monkeypatch):
        """PULL_IMAGE 为跨部署共享任务——停止不取消它。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="READY")
        image = "vllm/vllm-openai:v0.23.0"
        session.preload(
            Task(
                id="pull-1",
                resource_type="IMAGE_PULL",
                resource_key=pull_resource_key("n1", image),
                node_id="n1",
                type="PULL_IMAGE",
                status="RUNNING",
                attempt=1,
                payload={"image": image},
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await StopService().stop_instances("dep-1")

        assert (await session.get(Task, "pull-1")).status == "RUNNING"  # 未被取消
        assert rec.cancels == []

    async def test_multinode_stops_workers_before_head(self, patched_db, monkeypatch):
        """两阶段反序：worker（node_rank>0）的 STOP 先下发，head 后下发。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="READY", rank=0, node_rank=0)  # head
        _instance(session, status="READY", rank=1, node_rank=1)  # worker
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1")

        assert ok is True
        stop_orders = [t.payload["rank"] for t in rec.ensures if t.type == TaskType.STOP_INSTANCE]
        assert stop_orders == [1, 0]  # worker 先于 head
        assert (await session.get(Instance, "i-0")).status == "STOPPED"
        assert (await session.get(Instance, "i-1")).status == "STOPPED"

    async def test_multinode_worker_stage_timeout_still_attempts_head(self, patched_db, monkeypatch):
        """两阶段：worker 阶段超时（不上报 STOPPED）仍继续 head 阶段，返回 False（无 cleanup）。"""
        session = patched_db
        _deployment(session)
        _instance(session, status="READY", rank=0, node_rank=0)
        _instance(session, status="READY", rank=1, node_rank=1)
        rec = _Recorder(session, emulate_stop=False)  # 不上报 STOPPED → 两阶段都超时
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1", timeout=0.2)

        assert ok is False
        # 两阶段都 ensure 了 STOP task（worker 超时未阻断 head）
        stop_ranks = sorted(t.payload["rank"] for t in rec.ensures if t.type == TaskType.STOP_INSTANCE)
        assert stop_ranks == [0, 1]
        assert cleanups == []  # StopService 不 cleanup

    async def test_legacy_spec_without_node_rank_treated_as_head(self, patched_db, monkeypatch):
        """旧 spec 无 node_rank 键 → 视为 head（node_rank 缺省 0），单阶段行为不变。"""
        session = patched_db
        _deployment(session)
        session.preload(
            Instance(
                instance_id="i-legacy",
                deployment_id="dep-1",
                role="full",
                rank=0,
                assigned_node="n1",
                status="READY",
                spec={"rank": 0},  # 无 node_rank
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        ok = await StopService().stop_instances("dep-1")

        assert ok is True
        assert len(rec.ensures) == 1  # 单实例单阶段
        assert (await session.get(Instance, "i-legacy")).status == "STOPPED"


class TestConvergeStopDeployment:
    """reconciler.converge_stop_deployment：stopped/deleting 残留收敛。"""

    async def test_stopped_converge_keeps_rows_releases_vram(self, patched_db, monkeypatch):
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="stopped")
        _instance(session, status="STOPPED")
        session.preload(
            DeploymentGpuAssignment(
                id=1,
                deployment_id="dep-1",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        # stopped：保留部署行，释放 VRAM + 删 assignment
        assert await session.get(ModelDeployment, "dep-1") is not None
        assert released == ["dep-1"]
        assert session.all(DeploymentGpuAssignment) == []

    async def test_stopped_fast_path_noop(self, patched_db, monkeypatch):
        """稳态快路径：stopped + 全 STOPPED + assignment 已清 → 不触 StopService/label 扫描。

        stopped 行长期留存，每轮扫描不能重复跑停止流程。
        """
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="stopped")
        _instance(session, status="STOPPED")
        # 无 assignment（stop 路由已删）→ 全收敛完毕的稳态
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        assert cleanups == [] and released == [] and rec.ensures == [] and rec.cancels == []

    async def test_stopped_incomplete_resume(self, patched_db, monkeypatch):
        """stopped 残留（路由崩溃窗口）：有未停实例/未清 assignment → 继续收敛。"""
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="stopped")
        _instance(session, status="READY")  # 容器未停（路由崩溃）
        session.preload(
            DeploymentGpuAssignment(
                id=1,
                deployment_id="dep-1",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        assert len(rec.ensures) == 1 and rec.ensures[0].type == TaskType.STOP_INSTANCE
        assert released == ["dep-1"]
        assert session.all(DeploymentGpuAssignment) == []

    async def test_deleting_converge_cascade_deletes(self, patched_db, monkeypatch):
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="deleting")
        _instance(session, status="STOPPED")
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        # deleting：显式 cleanup 删容器 + 级联删行 + 释放 VRAM 内存态
        assert cleanups == [("dep-1", {"n1"})]  # delete 独有：label 扫除删容器
        assert await session.get(ModelDeployment, "dep-1") is None
        assert session.all(Instance) == []
        assert released == ["dep-1"]

    async def test_failed_converge_releases_vram_keeps_container(self, patched_db, monkeypatch):
        """failed 收敛：释放 VRAM + 删 assignment，但保留部署行/实例行/崩溃容器（无 cleanup）。

        修复 failed 的 VRAM/assignment 泄漏（原先 failed 不在 RECONCILE_STATUSES，永不收敛）。
        """
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="failed")
        _instance(session, status="FAILED")  # 崩溃实例（未停）
        session.preload(
            DeploymentGpuAssignment(
                id=1,
                deployment_id="dep-1",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        # failed：保留行（审计/可重启），释放 VRAM + 删 assignment，崩溃容器不动（无 cleanup）
        assert await session.get(ModelDeployment, "dep-1") is not None
        assert session.all(Instance)  # 实例行保留
        assert session.all(DeploymentGpuAssignment) == []
        assert released == ["dep-1"]
        assert cleanups == []  # failed 不删容器（留崩溃容器取证）
        assert len(rec.ensures) == 1 and rec.ensures[0].type == TaskType.STOP_INSTANCE

    async def test_creating_deployment_noop(self, patched_db, monkeypatch):
        """creating 部署不归 converge_stop 管（由 advance_deployment 推进）。"""
        from llmpod.apps.manager.deployment.reconciler import converge_stop_deployment
        import llmpod.apps.manager.deployment.reconciler as rec_mod

        @asynccontextmanager
        async def _fake_db():
            yield patched_db

        monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

        session = patched_db
        _deployment(session, status="creating")
        _instance(session, status="STARTING")
        rec = _Recorder(session)
        rec.install(monkeypatch)
        cleanups, released = [], []
        _patch_orch(monkeypatch, cleanups, released, [])

        await converge_stop_deployment("dep-1")

        assert cleanups == [] and released == []  # no-op
