"""DeploymentOrchestrator.restart 单测：原卡重新预留 VRAM + 冲突检测/force + 状态重置。

FakeSession（支持 select.where ==/in_ + 逐行 delete）+ 真实 ResourceLedger + 种子
Gpu/Model/ModelDeployment/Instance。覆盖：成功重预留+重置、原卡被占→Conflict(occupied)、
force 抢占、非 stopped/failed 拒绝、原卡离线拒绝。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.orchestrator import DeploymentOrchestrator
from llmpod.apps.manager.deployment.schemas import InstanceSpec
from llmpod.core.exceptions import Conflict
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.node import Node


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
        pk = list(obj.__table__.primary_key.columns)
        return getattr(obj, pk[0].key, None) if pk else None

    async def get(self, cls, pk):
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

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
        # 字面量 delete()：按表名反查类，等值/IN where 匹配的行从 store 移除
        # （delete_assignments 等已转批量 DML）
        from sqlalchemy.sql.dml import Delete

        if isinstance(stmt, Delete):
            table = stmt.table
            cls = next(
                (c for c in self._store if getattr(getattr(c, "__table__", None), "name", "") == table.name),
                None,
            )
            if cls is not None:
                store = self._store.get(cls, {})
                wc = stmt.whereclause
                if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
                    right = wc.right
                    val = getattr(right, "effective_value", None) or getattr(right, "value", right)
                    if isinstance(val, (list, tuple, set)):
                        doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) in val]
                    else:
                        doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) == val]
                else:
                    doomed = list(store.keys())
                for pk in doomed:
                    store.pop(pk, None)
            return _Result([])

        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        objs = list(self._store.get(entity, {}).values())
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None) or getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                objs = [o for o in objs if getattr(o, wc.left.key, None) in val]
            else:
                objs = [o for o in objs if getattr(o, wc.left.key, None) == val]
        return _Result(objs)

    async def delete(self, obj):
        for d in self._store.values():
            for k in [k for k, v in d.items() if v is obj]:
                del d[k]


class _Result:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self._objs

    def scalar_one_or_none(self):
        return self._objs[0] if self._objs else None


_SPEC = InstanceSpec(
    role="full",
    tp=1,
    pp=1,
    nnodes=1,
    node_rank=0,
    rank=0,
    instance_id="i-0",
    assigned_node="n1",
    gpu_arch="nvidia",
    assigned_gpus=[0],
    assigned_gpu_uuids=["g1"],
).model_dump()


def _seed(session, status="stopped"):
    session.preload(Model(id=1, name="m", model_type="text-generation", size_gb=1.0))
    session.preload(
        ModelDeployment(
            deployment_id="dep-1",
            cluster_id=1,
            name="d",
            model_id=1,
            engine="vllm",
            status=status,
            phase="STOPPING",
            spec={
                "topology": {"tp": 1, "pp": 1, "replicas": 1, "nnodes": 1},
                "health_check": {"timeout_seconds": 60, "interval_seconds": 5},
            },
        )
    )
    session.preload(
        Instance(
            instance_id="i-0",
            deployment_id="dep-1",
            role="full",
            rank=0,
            assigned_node="n1",
            status="STOPPED",
            spec=_SPEC,
        )
    )
    session.preload(
        Gpu(
            id=1,
            uuid="g1",
            node_id="n1",
            index=0,
            name="2080Ti",
            arch="nvidia",
            total_vram_gb=22.0,
            status="online",
            spec={},
        )
    )
    # 模型已 present → restart 走 PULL_IMAGE（未 present 的部署回 STAGING_MODEL 重新分发）
    session.preload(
        NodeModel(
            model_id=1,
            node_id="n1",
            desired_state="present",
            status="present",
            model_path="/data/models/m",
            local_dir="/data/models/m",
        )
    )


@pytest.fixture
def orch(monkeypatch):
    o = DeploymentOrchestrator()
    # wake() 不依赖真实控制器单例
    monkeypatch.setattr("llmpod.apps.manager.deployment.controller.get_controller", lambda: _Ctrl())
    return o


class _Ctrl:
    def wake(self):
        pass


@pytest.fixture
def patched_db(monkeypatch, session):
    @asynccontextmanager
    async def _fake_db():
        yield session

    import llmpod.apps.manager.deployment.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "get_db_context", _fake_db)
    return session


@pytest.fixture
def session():
    return FakeSession()


def _expected_vram() -> float:
    # 整卡预留：无显式占比 → 引擎默认 0.9 × total（与 _card_reserve 同口径，非模型需求）
    return get_adapter("vllm").default_gpu_memory_utilization * 22.0


class TestRestart:
    async def test_restart_reserves_and_resets(self, orch, patched_db):
        """成功：原卡重新预留 VRAM + persist assignment + status→creating/instance→PENDING/phase→PULL_IMAGE。"""
        session = patched_db
        _seed(session)
        await orch.restart("dep-1", trigger="user:1")

        dep = await session.get(ModelDeployment, "dep-1")
        assert dep.status == "creating" and dep.phase == "PULL_IMAGE"
        inst = await session.get(Instance, "i-0")
        assert inst.status == "PENDING" and inst.container_id is None
        # VRAM 已在 ledger 预留 + assignment 行已写
        assert orch.ledger.get_reserved_for_deployment("dep-1") == {"g1": pytest.approx(_expected_vram())}
        assert len(session.all(DeploymentGpuAssignment)) == 1

    async def test_restart_cpu_lane_reserves_ram(self, orch, patched_db):
        """纯 CPU 部署实例（arch=cpu、无卡号）不被缺卡号守卫误拒：按节点重预留内存。"""
        session = patched_db
        _seed(session)
        session.preload(
            Node(
                id="n1",
                cluster_id=1,
                hostname="h1",
                ip="10.0.1.5",
                status="online",
                cpu_cores=8,
                memory_gb=64.0,
            )
        )
        session._store[Instance]["i-0"].spec = InstanceSpec(
            role="full",
            tp=1,
            pp=1,
            nnodes=1,
            node_rank=0,
            rank=0,
            instance_id="i-0",
            assigned_node="n1",
            gpu_arch="cpu",
            assigned_gpus=[],
            assigned_gpu_uuids=[],
            ram_claim_gb=3.0,
        ).model_dump()

        await orch.restart("dep-1")

        assert (await session.get(ModelDeployment, "dep-1")).status == "creating"
        assert orch.ledger.get_ram_reserved_for_deployment("dep-1") == {"n1": 3.0}
        assert orch.ledger.get_reserved_for_deployment("dep-1") == {}  # 无显存预留

    async def test_restart_rejects_gpu_instance_without_card_ids(self, orch, patched_db):
        """GPU 部署实例缺卡号（旧版本行 / 从备份恢复的 spec）→ 拒绝重启。

        不能把它当纯 CPU 部署：那条路径会先 delete_assignments（显存权威来源没了，账本
        重建也算不出这笔占用），再因 ram_claim_gb 为空逐实例跳过 → 显存/内存都不预留，
        而容器仍按 gpu_arch=nvidia 下发 --gpus all 拿整机卡 → 并发部署撞同一张卡。
        """
        session = patched_db
        _seed(session)
        spec = dict(_SPEC)
        spec["assigned_gpu_uuids"] = []
        spec["assigned_gpus"] = []
        session._store[Instance]["i-0"].spec = spec

        with pytest.raises(Conflict, match="无已分配 GPU"):
            await orch.restart("dep-1")
        # 未进入重启：部署态与 assignment 都不动
        assert (await session.get(ModelDeployment, "dep-1")).status == "stopped"
        assert session.all(DeploymentGpuAssignment) == []

    async def test_restart_conflict_occupied(self, orch, patched_db):
        """原卡被别的部署占用（非 force）→ Conflict 带 occupied 明细。"""
        session = patched_db
        _seed(session)
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id="other",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        # 占用方部署须为 active（stopped/failed 的残留 assignment 不阻拦重启，见 _reserve_restart）
        session.preload(
            ModelDeployment(
                deployment_id="other",
                cluster_id=1,
                name="other",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={"topology": {"tp": 1, "pp": 1, "replicas": 1, "nnodes": 1}},
            )
        )
        with pytest.raises(Conflict) as ei:
            await orch.restart("dep-1")
        details = ei.value.details
        assert details["occupied"][0]["gpu_uuid"] == "g1"
        assert details["occupied"][0]["by"][0]["deployment_id"] == "other"
        # 冲突时不动部署态
        assert (await session.get(ModelDeployment, "dep-1")).status == "stopped"

    async def test_restart_force_overrides_conflict(self, orch, patched_db):
        """force=true：原卡被占仍强制预留（ledger 记账），重启成功。"""
        session = patched_db
        _seed(session)
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id="other",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        await orch.restart("dep-1", force=True)
        assert (await session.get(ModelDeployment, "dep-1")).status == "creating"
        assert orch.ledger.get_reserved_for_deployment("dep-1") == {"g1": pytest.approx(_expected_vram())}

    async def test_restart_colocation_exemption_without_force(self, orch, patched_db):
        """共址豁免：生效占比下的预留 + 他部署声明装得下 0.9×total → 不 Conflict 不需 force。

        显式调低显存占比（实例 spec 已落盘 vram_percent=0.3）共址一张卡的部署，
        stop→start 不应撞「原卡已被其他部署占用」排他闸——那闸防的是抢卡，不是共址。
        """
        session = patched_db
        _seed(session)
        spec = dict(_SPEC)
        spec["vram_percent"] = 0.3
        session._store[Instance]["i-0"].spec = spec
        # 他部署在 g1 声明 13.2GB（= 0.6×22，running）
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id="other",
                gpu_uuid="g1",
                allocated_vram_gb=13.2,
                resource_type="nvidia",
            )
        )
        session.preload(
            ModelDeployment(
                deployment_id="other",
                cluster_id=1,
                name="other",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={"topology": {"tp": 1, "pp": 1, "replicas": 1, "nnodes": 1}},
            )
        )

        await orch.restart("dep-1")  # 不带 force

        # 豁免通过：正常重预留 0.3×22=6.6GB（13.2+6.6=19.8 ≤ 0.9×22）
        assert (await session.get(ModelDeployment, "dep-1")).status == "creating"
        assert orch.ledger.get_reserved_for_deployment("dep-1") == {"g1": pytest.approx(0.3 * 22.0)}

    async def test_restart_colocation_envelope_exceeded_still_conflicts(self, orch, patched_db):
        """共址但信封装不下（他部署声明 + 本部署预留 > 0.9×total）→ 仍 Conflict，与原排他闸同消息。"""
        session = patched_db
        _seed(session)
        spec = dict(_SPEC)
        spec["vram_percent"] = 0.6
        session._store[Instance]["i-0"].spec = spec
        # 他部署声明 15.4GB + 本部署 0.6×22=13.2 → 28.6 > 19.8（0.9×22），豁免不成立
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id="other",
                gpu_uuid="g1",
                allocated_vram_gb=15.4,
                resource_type="nvidia",
            )
        )
        session.preload(
            ModelDeployment(
                deployment_id="other",
                cluster_id=1,
                name="other",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={"topology": {"tp": 1, "pp": 1, "replicas": 1, "nnodes": 1}},
            )
        )

        with pytest.raises(Conflict, match="原卡已被其他部署占用，如需继续请使用强制启动"):
            await orch.restart("dep-1")

    async def test_restart_reallocates_port_taken_by_neighbor(self, orch, patched_db):
        """停止期间同节点其他部署拿走旧端口 → restart 重分到下一个空闲口（写回 spec）。"""
        session = patched_db
        _seed(session)
        # 邻居部署的 active 实例占了 n1 的 8000/8001
        session.preload(
            Instance(
                instance_id="i-other",
                deployment_id="other",
                role="full",
                rank=0,
                assigned_node="n1",
                status="RUNNING",
                spec={"port": 8000},
            )
        )
        session.preload(
            Instance(
                instance_id="i-other2",
                deployment_id="other",
                role="full",
                rank=1,
                assigned_node="n1",
                status="RUNNING",
                spec={"port": 8001},
            )
        )
        await orch.restart("dep-1")
        inst = await session.get(Instance, "i-0")
        assert inst.spec["port"] == 8002

    async def test_restart_rejects_non_restartable(self, orch, patched_db):
        """非 stopped/failed（如 running）→ Conflict。"""
        session = patched_db
        _seed(session, status="running")
        with pytest.raises(Conflict):
            await orch.restart("dep-1")

    async def test_restart_offline_gpu_conflict(self, orch, patched_db):
        """原卡离线 → Conflict（不可重启）。"""
        session = patched_db
        _seed(session)
        (await session.get(Gpu, 1)).status = "offline"
        with pytest.raises(Conflict):
            await orch.restart("dep-1", force=True)  # force 也救不回离线卡

    async def test_restart_deletes_stale_start_task(self, orch, patched_db):
        """restart 清掉上一轮的 SUCCESS START_INSTANCE task。

        修前：restart 只重置实例为 PENDING，旧 SUCCESS START task 仍在 → reconciler
        幂等短路（prev SUCCESS 不重起容器）+ 双门补写 READY，导致不建容器却标 RUNNING
        （假运行部署，/health 不通）。修后：本部署 rank 的旧 START task 被删，reconciler
        按新 cycle 重建并真实拉起容器。
        """
        from llmpod.models.task import Task

        session = patched_db
        _seed(session)
        session.preload(
            Task(
                id="task-1",
                resource_type="INSTANCE",
                resource_key="instance:dep-1:0",
                node_id="n1",
                type="START_INSTANCE",
                status="SUCCESS",
                attempt=1,
            )
        )
        await orch.restart("dep-1")

        assert not [t for t in session.all(Task) if t.resource_key == "instance:dep-1:0"]

    async def test_restart_deletes_stale_stop_task(self, orch, patched_db):
        """restart 清掉上一轮的 STOP_INSTANCE task，attempt 归零重计。

        attempt 因唯一索引必须单调，不清会跨轮无限增长。与 task_ops 的「末尾连续失败数
        判超限」配合：后者根治了手动 stop/start 误触 attempt 上限（SUCCESS 不清零编号但
        也不计失败链），前者让 STOP task 的 attempt 每轮 restart 归零、语义清晰。
        """
        from llmpod.models.task import Task

        session = patched_db
        _seed(session)
        # 三次 stop 累积的 STOP task（attempt 1..3，均 SUCCESS）
        for i in range(1, 4):
            session.preload(
                Task(
                    id=f"stop-{i}",
                    resource_type="INSTANCE",
                    resource_key="instance:dep-1:0:stop",
                    node_id="n1",
                    type="STOP_INSTANCE",
                    status="SUCCESS",
                    attempt=i,
                )
            )
        await orch.restart("dep-1")

        assert not [t for t in session.all(Task) if t.resource_key == "instance:dep-1:0:stop"]


class TestRestartConvergeRace:
    """restart 与 controller converge 的竞态：状态先移出终态集合再重预留。"""

    async def test_restart_flips_creating_before_reserve(self, orch, patched_db, monkeypatch):
        """第一步短事务里置 CREATING——重预留发生时部署已不在 converge 终态集合。

        用 hooks 记录 _reserve_restart 执行时刻的 status：必须是 creating 而非 stopped。
        """
        session = patched_db
        _seed(session)
        seen: list[str] = []
        real_reserve = orch._reserve_restart

        async def _spy(db, deployment_id, instances, gpu_pool, adapter, config, model, force):
            dep = await db.get(ModelDeployment, deployment_id)
            seen.append(dep.status)
            return await real_reserve(db, deployment_id, instances, gpu_pool, adapter, config, model, force)

        monkeypatch.setattr(orch, "_reserve_restart", _spy)
        await orch.restart("dep-1", trigger="user:1")
        assert seen == ["creating"]  # 预留时已移出终态，converge 不会再对本部署释放

    async def test_restart_reserve_conflict_rolls_back_status(self, orch, patched_db):
        """重预留失败（原卡被占）→ 状态回滚为原终态 stopped，路由向用户报 409。"""
        session = patched_db
        _seed(session)
        # 原卡被其他 active 部署占用（制造非 force Conflict）
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id="dep-other",
                gpu_uuid="g1",
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        session.preload(
            ModelDeployment(
                deployment_id="dep-other",
                cluster_id=1,
                name="other",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={"topology": {"tp": 1, "pp": 1, "replicas": 1, "nnodes": 1}},
            )
        )
        from llmpod.core.exceptions import Conflict

        with pytest.raises(Conflict):
            await orch.restart("dep-1", trigger="user:1", force=False)
        dep = await session.get(ModelDeployment, "dep-1")
        assert dep.status == "stopped"  # 回滚到原终态
