"""resolve_gpu_occupancy / allocate_node_port 单测（节点共享冲突避免）。

FakeSession（select.where ==/in_）+ 种子 Gpu/ModelDeployment/DeploymentGpuAssignment/Instance。
覆盖：占用解析 status-aware（active 计入、stopped/failed 残留不计）+ 归因与 pct；
端口分配取最小空闲、STOPPED/FAILED 实例不占端口、rendezvous 池独立、池满 Conflict。
"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import Conflict
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.services import deployment_service


class FakeSession:
    def __init__(self):
        self._store: dict = {}
        self._staged: list = []
        self._counters: dict = {}

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

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


@pytest.fixture
def session():
    return FakeSession()


def _gpu(session, uuid="g1", total=100.0):
    return session.preload(
        Gpu(
            id=1,
            uuid=uuid,
            node_id="n1",
            index=0,
            name="A100",
            arch="nvidia",
            total_vram_gb=total,
            status="online",
            spec={},
        )
    )


def _dep(session, deployment_id, status, name):
    return session.preload(
        ModelDeployment(
            deployment_id=deployment_id,
            cluster_id=1,
            name=name,
            model_id=1,
            engine="vllm",
            status=status,
            phase="RUNNING",
            spec={},
        )
    )


def _assign(session, id, deployment_id, gpu_uuid, gb):
    return session.preload(
        DeploymentGpuAssignment(
            id=id,
            deployment_id=deployment_id,
            gpu_uuid=gpu_uuid,
            allocated_vram_gb=gb,
            gpu_memory_utilization=0.9,
        )
    )


class TestResolveGpuOccupancy:
    async def test_active_counted_stale_ignored(self, session):
        """creating/running 声明计入；stopped/failed 残留声明行不计（放行）。"""
        _gpu(session, total=100.0)
        _dep(session, "dep-a", "running", "busy-a")
        _dep(session, "dep-b", "stopped", "stopped-b")
        _dep(session, "dep-c", "failed", "failed-c")
        _assign(session, 1, "dep-a", "g1", 30.0)
        _assign(session, 2, "dep-b", "g1", 50.0)
        _assign(session, 3, "dep-c", "g1", 10.0)

        occ = await deployment_service.resolve_gpu_occupancy(session, ["g1"])
        assert occ["g1"].declared_gb == 30.0
        assert occ["g1"].occupied_pct == 0.3
        assert [o.model_dump() for o in occ["g1"].occupied_by] == [
            {"deployment_id": "dep-a", "deployment_name": "busy-a", "declared_gb": 30.0}
        ]

    async def test_no_declaration_card_absent(self, session):
        """无声明的卡不出现在结果里（调用方 get 默认空 = 零占用）。"""
        _gpu(session)
        occ = await deployment_service.resolve_gpu_occupancy(session, ["g1", "g2"])
        assert occ == {}

    async def test_empty_input(self, session):
        assert await deployment_service.resolve_gpu_occupancy(session, []) == {}


class TestAllocateNodePort:
    def _inst(self, session, iid, status, spec):
        return session.preload(
            Instance(
                instance_id=iid,
                deployment_id="dep-x",
                role="full",
                rank=0,
                assigned_node="n1",
                status=status,
                spec=spec,
            )
        )

    async def test_min_free_port(self, session):
        """取池内最小空闲（跳过已占的 8000 与 8002）。"""
        self._inst(session, "i1", "RUNNING", {"port": 8000})
        self._inst(session, "i2", "READY", {"port": 8002})
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8001

    async def test_stopped_failed_release_port(self, session):
        """STOPPED/FAILED 实例不占端口——停止期间端口可被其他部署复用。"""
        self._inst(session, "i1", "STOPPED", {"port": 8000})
        self._inst(session, "i2", "FAILED", {"port": 8001})
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8000

    async def test_rendezvous_pool_independent(self, session):
        """rendezvous 池与 service 口互不干扰（只看 master_port）；主口带 10 口隔离带。"""
        self._inst(session, "i1", "RUNNING", {"port": 8000, "master_port": 29501})
        # 29501+1..29510 是隔离带，下一主口从 29511 起
        assert await deployment_service.allocate_node_port(session, "n1", "rendezvous") == 29511

    async def test_pool_exhausted_conflict(self, session, monkeypatch):
        """池满 → Conflict（409）。"""
        monkeypatch.setattr(deployment_service, "SERVICE_PORT_RANGE", range(8000, 8001))
        self._inst(session, "i1", "RUNNING", {"port": 8000})
        with pytest.raises(Conflict):
            await deployment_service.allocate_node_port(session, "n1", "service")

    async def test_other_node_not_counted(self, session):
        """端口按节点查重：别的节点占 8000 不影响本节点。"""
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="dep-x",
                role="full",
                rank=0,
                assigned_node="n2",
                status="RUNNING",
                spec={"port": 8000},
            )
        )
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8000


class TestAllocateUniquePort:
    """批内防撞：DB 看不到本轮刚分配的端口（行未落库），靠 local_taken 去重。"""

    async def test_same_batch_does_not_reuse_port(self, session):
        local: dict = {}
        assert await deployment_service.allocate_unique_port(session, local, "n1", "service") == 8000
        assert await deployment_service.allocate_unique_port(session, local, "n1", "service") == 8001

    async def test_same_batch_skips_isolated_band(self, session):
        """rendezvous 本地登记带隔离带：下一个主口不落进上一个的 10 口带内。"""
        local: dict = {}
        assert await deployment_service.allocate_unique_port(session, local, "n1", "rendezvous") == 29501
        assert await deployment_service.allocate_unique_port(session, local, "n1", "rendezvous") == 29511

    async def test_batch_exhaustion_conflict(self, session, monkeypatch):
        """池内端口全被本批占满 → Conflict（不再无限 +1 越出池上界）。"""
        monkeypatch.setattr(deployment_service, "SERVICE_PORT_RANGE", range(8000, 8002))
        local: dict = {}
        await deployment_service.allocate_unique_port(session, local, "n1", "service")
        await deployment_service.allocate_unique_port(session, local, "n1", "service")
        with pytest.raises(Conflict):
            await deployment_service.allocate_unique_port(session, local, "n1", "service")
