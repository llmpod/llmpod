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


class TestServicePortBand:
    """引擎服务口带宽（service_port_band）：MindIE 占 port+1/+2 派生口，整组登记与避让。"""

    async def test_mindie_band_reserves_derived_ports(self, session):
        """带宽 3 的分配：下一实例（无论引擎）主口不落进 8001/8002。"""
        local: dict = {}
        assert await deployment_service.allocate_unique_port(session, local, "n1", "service", engine="mindie") == 8000
        # 同批下一个 vLLM 实例：8001/8002 在 8000 的组内，跳到 8003
        assert await deployment_service.allocate_unique_port(session, local, "n1", "service", engine="vllm") == 8003
        # 同批下一个 MindIE 实例：vLLM 只占 8003 一个口，从 8004 起组 8004..8006
        assert await deployment_service.allocate_unique_port(session, local, "n1", "service", engine="mindie") == 8004

    async def test_persisted_band_blocks_later_band1_alloc(self, session):
        """存量 MindIE 行（spec 带 port_band=3）：之后给 vLLM（带宽 1）分配也要避开 8001/8002。"""
        self_inst = Instance(
            instance_id="i1",
            deployment_id="dep-m",
            role="full",
            rank=0,
            assigned_node="n1",
            status="RUNNING",
            spec={"port": 8000, "port_band": 3},
        )
        session.preload(self_inst)
        assert await deployment_service.allocate_node_port(session, "n1", "service", engine="vllm") == 8003

    async def test_legacy_row_without_band_counts_single(self, session):
        """旧结构行（spec 无 port_band）：按带宽 1 登记，不因引擎变大而误扩占口。"""
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="dep-old",
                role="full",
                rank=0,
                assigned_node="n1",
                status="RUNNING",
                spec={"port": 8000},
            )
        )
        assert await deployment_service.allocate_node_port(session, "n1", "service", engine="vllm") == 8001

    async def test_mindie_pool_exhausted_earlier(self, session, monkeypatch):
        """带宽 3 需要池内仍有连续三口起点：2 口池只剩 8001 一个候选、其组越界且落在
        既有 8000 组内 → Conflict（不能只占主口漏掉派生口）。"""
        monkeypatch.setattr(deployment_service, "SERVICE_PORT_RANGE", range(8000, 8002))
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="dep-m",
                role="full",
                rank=0,
                assigned_node="n1",
                status="RUNNING",
                spec={"port": 8000, "port_band": 3},
            )
        )
        with pytest.raises(Conflict):
            await deployment_service.allocate_node_port(session, "n1", "service", engine="mindie")


class TestPortExternalPrecheck:
    """发号前的外部占用预检：节点在线时逐候选 RPC 试绑，被占候选跳过；通道故障 fail-open。"""

    @staticmethod
    def _patch_probe(monkeypatch, check):
        """预检替身三板斧：节点置在线 + dispatch.check_ports_free 换桩（各用例共用）。"""
        monkeypatch.setattr("llmpod.apps.manager.dispatch.node_online", lambda _nid: True, raising=False)
        import llmpod.apps.manager.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "check_ports_free", check)

    async def test_occupied_candidate_skipped(self, session, monkeypatch):
        """外部进程占 8000 → 账本外预检发现并跳过，发 8001。"""

        async def _fake_check(node_id, ports):
            from llmpod.tunnel.schemas import PortCheckResult

            return PortCheckResult(occupied=[p for p in ports if p == 8000])

        self._patch_probe(monkeypatch, _fake_check)
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8001

    async def test_probe_failure_falls_back_to_ledger(self, session, monkeypatch):
        """预检 RPC 抛错（节点瞬断等）→ fail-open：退回纯账本发号，不阻塞调度。"""

        async def _boom(node_id, ports):
            raise RuntimeError("node gone")

        self._patch_probe(monkeypatch, _boom)
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8000

    async def test_node_offline_skips_precheck(self, session, monkeypatch):
        """节点离线：不发起预检 RPC，纯账本发号（离线节点本来也起不了容器）。"""
        monkeypatch.setattr("llmpod.apps.manager.dispatch.node_online", lambda _nid: False, raising=False)

        async def _unexpected(node_id, ports):
            raise AssertionError("离线节点不应发起预检 RPC")

        import llmpod.apps.manager.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "check_ports_free", _unexpected)
        assert await deployment_service.allocate_node_port(session, "n1", "service") == 8000

    async def test_band_group_probed_together(self, session, monkeypatch):
        """带宽 3 的候选：整组三口一次预检，组内任一口被占即整组跳过。"""
        seen: list[list[int]] = []

        async def _fake_check(node_id, ports):
            from llmpod.tunnel.schemas import PortCheckResult

            seen.append(list(ports))
            # 第一个候选组 8000..8002 的派生口 8002 被外部占用
            return PortCheckResult(occupied=[8002] if 8002 in ports else [])

        self._patch_probe(monkeypatch, _fake_check)
        assert await deployment_service.allocate_node_port(session, "n1", "service", engine="mindie") == 8003
        assert seen[0] == [8000, 8001, 8002]  # 整组探测而非只探主口

    async def test_skip_limit_falls_back_to_ledger(self, session, monkeypatch):
        """连跳上限：外部占用候选达到上限后放弃预检，退回纯账本发号（发最小空闲口）。"""
        calls = {"n": 0}

        async def _fake_check(node_id, ports):
            from llmpod.tunnel.schemas import PortCheckResult

            calls["n"] += 1
            return PortCheckResult(occupied=ports)  # 一律报被占

        self._patch_probe(monkeypatch, _fake_check)
        port = await deployment_service.allocate_node_port(session, "n1", "service")
        # 连跳 8 个候选（8000..8007 已进避让集）后放弃预检 → 纯账本：8008 无账本行占用，发 8008
        assert port == 8008
        assert calls["n"] == deployment_service._PORT_PRECHECK_EXTERNAL_SKIP_LIMIT
