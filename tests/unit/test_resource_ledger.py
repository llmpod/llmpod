"""ResourceLedger 单测（显存/内存乐观预留 + 容量校验 + 并发 + 重建）。"""

from __future__ import annotations

import asyncio

import pytest

from llmpod.apps.manager.deployment.resource_ledger import ResourceLedger


@pytest.fixture
def ledger() -> ResourceLedger:
    return ResourceLedger()


class TestReserveRelease:
    async def test_reserve_within_capacity(self, ledger):
        # 24GB 卡，0.9 util = 21.6GB 容量
        assert await ledger.reserve("dep1", "gpu-A", 10.0, 24.0) is True
        assert await ledger.reserve("dep1", "gpu-A", 10.0, 24.0) is True  # 累计 20 ≤ 21.6

    async def test_reserve_over_capacity_rejected(self, ledger):
        assert await ledger.reserve("dep1", "gpu-A", 20.0, 24.0) is True  # 20 ≤ 21.6
        assert await ledger.reserve("dep2", "gpu-A", 5.0, 24.0) is False  # 20+5=25 > 21.6

    async def test_reserve_zero_ok(self, ledger):
        assert await ledger.reserve("dep1", "gpu-A", 0.0, 24.0) is True

    async def test_release_frees_capacity(self, ledger):
        await ledger.reserve("dep1", "gpu-A", 20.0, 24.0)
        assert await ledger.reserve("dep2", "gpu-A", 5.0, 24.0) is False
        await ledger.release("dep1")
        assert await ledger.reserve("dep2", "gpu-A", 5.0, 24.0) is True

    async def test_release_unknown_deployment_noop(self, ledger):
        await ledger.release("nonexistent")  # 不抛

    async def test_partial_release_keeps_other_deployment(self, ledger):
        """增量总和与明细表同步：同卡多部署，部分释放只减自己的量。"""
        await ledger.reserve("dep1", "gpu-A", 10.0, 24.0)
        await ledger.reserve("dep2", "gpu-A", 5.0, 24.0)
        assert ledger.get_available("gpu-A", 24.0) == pytest.approx(21.6 - 15.0)
        await ledger.release_cards("dep1", ["gpu-A"])
        assert ledger.get_available("gpu-A", 24.0) == pytest.approx(21.6 - 5.0)
        assert ledger.get_reserved_for_deployment("dep2") == {"gpu-A": 5.0}


class TestGetAvailable:
    def test_fresh_gpu_full_capacity(self, ledger):
        # 24GB * 0.9 = 21.6
        assert ledger.get_available("gpu-A", 24.0) == pytest.approx(21.6)

    async def test_decreases_after_reserve(self, ledger):
        await ledger.reserve("dep1", "gpu-A", 8.0, 24.0)
        assert ledger.get_available("gpu-A", 24.0) == pytest.approx(21.6 - 8.0)

    def test_per_gpu_isolated(self, ledger):
        # 不同 GPU 的预留互不影响
        assert ledger.get_available("gpu-A", 24.0) == pytest.approx(21.6)


class TestReservedForDeployment:
    async def test_detail(self, ledger):
        await ledger.reserve("dep1", "gpu-A", 8.0, 24.0)
        await ledger.reserve("dep1", "gpu-B", 4.0, 24.0)
        detail = ledger.get_reserved_for_deployment("dep1")
        assert detail == {"gpu-A": 8.0, "gpu-B": 4.0}

    async def test_empty_for_unknown(self, ledger):
        assert ledger.get_reserved_for_deployment("nope") == {}


class TestConcurrency:
    async def test_concurrent_reserve_does_not_oversubscribe(self, ledger):
        """并发 reserve 同一 GPU 不能突破容量上限（乐观锁）。"""
        # 容量 21.6GB，每次 reserve 8GB，最多 2 次成功（16≤21.6），第 3 次（24>21.6）失败
        results = await asyncio.gather(*[ledger.reserve(f"dep{i}", "gpu-A", 8.0, 24.0) for i in range(3)])
        successes = sum(1 for r in results if r)
        assert successes == 2


class TestRamReserveRelease:
    """内存维度（CPU/混合 offload 的权重与 KV 落内存部分）——与显存同一套语义。"""

    async def test_reserve_within_capacity(self, ledger):
        # 64GB 节点，0.9 util = 57.6GB 容量
        assert await ledger.reserve_ram("dep1", "node-A", 30.0, 64.0) is True
        assert await ledger.reserve_ram("dep1", "node-A", 20.0, 64.0) is True  # 累计 50 ≤ 57.6

    async def test_over_capacity_rejected(self, ledger):
        assert await ledger.reserve_ram("dep1", "node-A", 50.0, 64.0) is True
        assert await ledger.reserve_ram("dep2", "node-A", 10.0, 64.0) is False  # 60 > 57.6

    async def test_force_reserve_skips_check(self, ledger):
        await ledger.force_reserve_ram("dep1", "node-A", 100.0)
        assert ledger.get_available_ram("node-A", 64.0) == 0.0

    async def test_release_frees_capacity(self, ledger):
        await ledger.reserve_ram("dep1", "node-A", 50.0, 64.0)
        assert await ledger.reserve_ram("dep2", "node-A", 10.0, 64.0) is False
        await ledger.release("dep1")
        assert await ledger.reserve_ram("dep2", "node-A", 10.0, 64.0) is True

    async def test_release_nodes_partial(self, ledger):
        """按量扣减：同节点多份只释放被删那份的量。"""
        await ledger.reserve_ram("dep1", "node-A", 20.0, 64.0)
        await ledger.reserve_ram("dep1", "node-B", 20.0, 64.0)
        await ledger.release_ram_nodes("dep1", {"node-A": 8.0})
        assert ledger.get_ram_reserved_for_deployment("dep1") == {"node-A": 12.0, "node-B": 20.0}
        await ledger.release_ram_nodes("dep1", {"node-A": 12.0})
        assert ledger.get_ram_reserved_for_deployment("dep1") == {"node-B": 20.0}

    async def test_available_and_detail(self, ledger):
        await ledger.reserve_ram("dep1", "node-A", 8.0, 64.0)
        assert ledger.get_available_ram("node-A", 64.0) == pytest.approx(57.6 - 8.0)
        assert ledger.get_ram_reserved_for_deployment("dep1") == {"node-A": 8.0}
        assert ledger.get_ram_reserved_for_deployment("nope") == {}

    async def test_concurrent_ram_reserve_does_not_oversubscribe(self, ledger):
        results = await asyncio.gather(*[ledger.reserve_ram(f"dep{i}", "node-A", 20.0, 64.0) for i in range(4)])
        # 容量 57.6GB，每次 20GB → 最多 2 次成功
        assert sum(1 for r in results if r) == 2

    async def test_vram_and_ram_dimensions_are_independent(self, ledger):
        """同一 deployment 的显存与内存预留互不覆盖（release_cards 不动内存）。"""
        await ledger.reserve("dep1", "gpu-A", 10.0, 24.0)
        await ledger.reserve_ram("dep1", "node-A", 10.0, 64.0)
        await ledger.release_cards("dep1", ["gpu-A"])
        assert ledger.get_reserved_for_deployment("dep1") == {}
        assert ledger.get_ram_reserved_for_deployment("dep1") == {"node-A": 10.0}

    async def test_ram_totals_stay_consistent_across_deployments(self, ledger):
        """内存增量总和与明细表同步：释放一个部署不影响同节点另一个的占用。"""
        await ledger.reserve_ram("dep1", "node-A", 20.0, 64.0)
        await ledger.reserve_ram("dep2", "node-A", 20.0, 64.0)
        assert ledger.get_available_ram("node-A", 64.0) == pytest.approx(57.6 - 40.0)
        await ledger.release("dep1")
        assert ledger.get_available_ram("node-A", 64.0) == pytest.approx(57.6 - 20.0)
        await ledger.release_ram_nodes("dep2", {"node-A": 20.0})
        assert ledger.get_available_ram("node-A", 64.0) == pytest.approx(57.6)
