"""GPU 显存占比共享单测：生效显存占比 → reserved=%×total + 多部署共享一卡。

零 DB：FakeLedger 镜像 ResourceLedger 的「Σ预留 ≤ total×0.9」共享语义。生效占比由编排器
从引擎 flag（--gpu-memory-utilization/--mem-fraction-static）解析后经 assign 的 vram_percent 传入。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import GpuCandidate, Topology
from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler, card_reserve
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard, validate_topology
from llmpod.core.exceptions import LLMPodValidationError


def _cand(uuid, node, total=24.0, arch="nvidia", status="online", index=0):
    return GpuCandidate(uuid=uuid, node_id=node, index=index, arch=arch, total_vram_gb=total, status=status)


class _FakeLedger:
    """镜像 ResourceLedger：一卡多部署，Σ预留 ≤ total×0.9。"""

    def __init__(self):
        self.reserved: dict[str, dict[str, float]] = {}

    def _used(self, uuid):
        return sum(r.get(uuid, 0.0) for r in self.reserved.values())

    def get_available(self, uuid, total):
        return max(0.0, total * 0.9 - self._used(uuid))

    async def reserve(self, deployment_id, uuid, vram_gb, total_gb):
        if self._used(uuid) + vram_gb > total_gb * 0.9 + 1e-9:
            return False
        self.reserved.setdefault(deployment_id, {})[uuid] = vram_gb
        return True

    async def release(self, deployment_id):
        self.reserved.pop(deployment_id, None)

    async def release_cards(self, deployment_id, gpu_uuids):
        gpus = self.reserved.get(deployment_id)
        if not gpus:
            return
        for u in gpu_uuids:
            gpus.pop(u, None)
        if not gpus:
            self.reserved.pop(deployment_id, None)


class TestVramPercentShare:
    async def test_two_deployments_share_one_gpu(self):
        """两部署各占比 0.4 同卡 → 都成功，共享 g0，共预留 19.2GB。"""
        ledger = _FakeLedger()
        sched = GpuScheduler(ledger)
        pool = [_cand("g0", "n1")]
        for did in ("depA", "depB"):
            insts = expand_standard(Topology(tp=1, replicas=1))
            a = await sched.assign(did, insts, pool, vram_per_gpu_gb=8.0, arch="nvidia", vram_percent=0.4)
            assert a[0].gpu_uuids == ["g0"]
            assert a[0].gpu_memory_utilization == 0.4
        assert self._total(ledger, "g0") == pytest.approx(19.2, abs=0.01)

    @staticmethod
    def _total(ledger, uuid):
        return sum(r.get(uuid, 0.0) for r in ledger.reserved.values())

    async def test_third_deployment_rejected_when_full(self):
        """两部署占 19.2GB（cap 21.6），第三部署 0.4(9.6GB) 超限 → 无可用卡被拒。"""
        ledger = _FakeLedger()
        sched = GpuScheduler(ledger)
        pool = [_cand("g0", "n1")]
        for did in ("depA", "depB"):
            await sched.assign(
                did,
                expand_standard(Topology(tp=1, replicas=1)),
                pool,
                vram_per_gpu_gb=8.0,
                arch="nvidia",
                vram_percent=0.4,
            )
        with pytest.raises(LLMPodValidationError):
            await sched.assign(
                "depC",
                expand_standard(Topology(tp=1, replicas=1)),
                pool,
                vram_per_gpu_gb=8.0,
                arch="nvidia",
                vram_percent=0.4,
            )

    async def test_utilization_cap_too_small_for_model(self):
        """占比×total < 模型需求 → 卡被排除 → 分配失败。"""
        ledger = _FakeLedger()
        sched = GpuScheduler(ledger)
        pool = [_cand("g0", "n1", total=24.0)]
        with pytest.raises(LLMPodValidationError):  # 0.2×24=4.8 < 模型需求 8
            await sched.assign(
                "dep",
                expand_standard(Topology(tp=1, replicas=1)),
                pool,
                vram_per_gpu_gb=8.0,
                arch="nvidia",
                vram_percent=0.2,
            )

    async def test_no_utilization_reserves_full_gpu(self):
        """未设显存占比 → 独占整卡：reserved = total×0.9（对齐引擎默认 util 抢占整卡），util=None。

        旧行为预留模型需求（8GB），账本误判余量、并发部署堆同一卡 → 实际 OOM；改整卡预留。
        """
        ledger = _FakeLedger()
        sched = GpuScheduler(ledger)
        pool = [_cand("g0", "n1", total=24.0)]
        a = await sched.assign(
            "dep",
            expand_standard(Topology(tp=1, replicas=1)),
            pool,
            vram_per_gpu_gb=8.0,
            arch="nvidia",
            gpu_selector=None,
        )
        assert ledger.reserved["dep"]["g0"] == pytest.approx(24.0 * 0.9, abs=0.01)
        assert a[0].gpu_memory_utilization is None

    async def test_share_heterogeneous_totals(self):
        """不同 total 的卡：各自按 %×自身 total 预留。"""
        ledger = _FakeLedger()
        sched = GpuScheduler(ledger)
        pool = [_cand("g0", "n1", total=24.0), _cand("g1", "n2", total=16.0)]
        insts = expand_standard(Topology(tp=2, nnodes=2, replicas=1))
        await sched.assign("dep", insts, pool, vram_per_gpu_gb=4.0, arch="nvidia", gpu_selector=None)
        # 注：auto 调度不保证落到 g0/g1，这里只验 card_reserve 逐卡算（见 smoke）
        # 直接验逐卡 reserve 口径（生效占比传具体值，与编排器 memory_utilization_from 产物一致）
        assert card_reserve(_cand("g0", "n1", total=24.0), 0.5) == 12.0
        assert card_reserve(_cand("g1", "n2", total=16.0), 0.5) == 8.0


class TestVramPercentValidation:
    def test_pp_zero_rejected(self):
        with pytest.raises(LLMPodValidationError, match="pp"):
            validate_topology(Topology(tp=1, pp=0))


async def test_pp_allocates_tp_times_pp_gpus():
    """pp=2, tp=1 → 单节点分配 2 张卡（PP 计入总并行度，不再按 tp=1 单卡跑）。"""
    ledger = _FakeLedger()
    sched = GpuScheduler(ledger)
    pool = [_cand("g0", "n1", index=0), _cand("g1", "n1", index=1)]
    insts = expand_standard(Topology(tp=1, pp=2, replicas=1))
    a = await sched.assign("dep", insts, pool, vram_per_gpu_gb=8.0, arch="nvidia")
    assert a[0].gpu_uuids == ["g0", "g1"]
    assert insts[0].assigned_gpus == [0, 1]
