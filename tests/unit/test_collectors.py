"""Node 采集器和系统信息模型单元测试。"""

from __future__ import annotations

import pytest

from llmpod.apps.node.collectors import gpu as gpu_mod
from llmpod.apps.node.collectors.gpu import dcmi
from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase
from llmpod.tunnel.schemas.system import SystemInfo, GpuInfo


class TestSystemInfo:
    def test_creation(self):
        info = SystemInfo(
            hostname="test-host",
            ip="192.168.1.10",
            cpu_cores=16,
            cpu_arch="x86_64",
            memory_gb=64.0,
        )
        assert info.hostname == "test-host"
        assert info.ip == "192.168.1.10"
        assert info.cpu_cores == 16
        assert info.cpu_arch == "x86_64"
        assert info.memory_gb == 64.0

    def test_runtime_metrics_defaults(self):
        """新增的运行时指标字段应均有合理默认值，便于渐进上报。"""
        info = SystemInfo(
            hostname="h",
            ip="127.0.0.1",
            cpu_cores=1,
            cpu_arch="x86_64",
            memory_gb=1.0,
        )
        # CPU 运行时
        assert info.cpu_percent == 0.0
        assert info.cpu_load_avg_1 == 0.0
        assert info.cpu_load_avg_5 == 0.0
        assert info.cpu_load_avg_15 == 0.0
        # 内存运行时
        assert info.memory_used_gb == 0.0
        assert info.memory_percent == 0.0
        # swap
        assert info.swap_total_gb == 0.0
        assert info.swap_used_gb == 0.0
        assert info.swap_percent == 0.0
        # 磁盘
        assert info.disk_total_gb == 0.0
        assert info.disk_used_gb == 0.0
        assert info.disk_percent == 0.0
        # OS
        assert info.os_name == ""
        assert info.os_version == ""
        assert info.kernel_version == ""
        assert info.uptime_seconds == 0.0

    def test_runtime_metrics_populated(self):
        info = SystemInfo(
            hostname="h",
            ip="127.0.0.1",
            cpu_cores=8,
            cpu_arch="x86_64",
            memory_gb=32.0,
            os_name="Linux",
            os_version="5.15.0",
            kernel_version="...generic",
            uptime_seconds=3600.0,
            cpu_percent=42.5,
            cpu_load_avg_1=1.2,
            cpu_load_avg_5=1.0,
            cpu_load_avg_15=0.8,
            memory_used_gb=16.0,
            memory_percent=50.0,
            swap_total_gb=4.0,
            swap_used_gb=1.0,
            swap_percent=25.0,
            disk_total_gb=100.0,
            disk_used_gb=70.0,
            disk_percent=70.0,
        )
        assert info.cpu_percent == 42.5
        assert info.memory_percent == 50.0
        assert info.swap_percent == 25.0
        assert info.disk_percent == 70.0
        assert info.os_name == "Linux"
        assert info.uptime_seconds == 3600.0


class TestGpuInfo:
    def test_creation(self):
        gpu = GpuInfo(
            uuid="GPU-abc123",
            index=0,
            name="NVIDIA A100",
            used_vram_gb=2.0,
            total_vram_gb=40.0,
            utilization=50.0,
            temperature=65,
            power_draw_w=250.0,
            driver_version="535.104.05",
        )
        assert gpu.uuid == "GPU-abc123"
        assert gpu.name == "NVIDIA A100"
        assert gpu.total_vram_gb == 40.0
        assert gpu.utilization == 50.0

    def test_vram_gb_conversion(self):
        gpu = GpuInfo(
            uuid="GPU-test",
            index=0,
            name="Test GPU",
            total_vram_gb=80.0,
            used_vram_gb=40.0,
        )
        assert gpu.total_vram_gb == 80.0
        assert gpu.used_vram_gb == 40.0

    def test_defaults(self):
        gpu = GpuInfo(uuid="GPU-default", index=0, name="Default GPU")
        # 原有默认
        assert gpu.used_vram_gb == 0
        assert gpu.total_vram_gb == 0
        assert gpu.utilization == 0.0
        assert gpu.temperature == 0
        assert gpu.power_draw_w == 0.0
        assert gpu.driver_version == ""
        # 新增默认
        assert gpu.memory_utilization == 0.0
        assert gpu.encoder_utilization == 0.0
        assert gpu.decoder_utilization == 0.0
        assert gpu.fan_speed == 0
        assert gpu.power_limit_w == 0.0
        assert gpu.sm_clock_mhz == 0
        assert gpu.mem_clock_mhz == 0
        assert gpu.process_count == 0
        assert gpu.pcie_rx_throughput_mb_s == 0.0
        assert gpu.pcie_tx_throughput_mb_s == 0.0

    def test_enriched_metrics_populated(self):
        gpu = GpuInfo(
            uuid="GPU-x",
            index=1,
            name="A100",
            memory_utilization=60.0,
            encoder_utilization=10.0,
            decoder_utilization=5.0,
            fan_speed=80,
            power_limit_w=400.0,
            sm_clock_mhz=1410,
            mem_clock_mhz=1593,
            process_count=3,
            pcie_rx_throughput_mb_s=1024.0,
            pcie_tx_throughput_mb_s=512.0,
        )
        assert gpu.memory_utilization == 60.0
        assert gpu.fan_speed == 80
        assert gpu.power_limit_w == 400.0
        assert gpu.sm_clock_mhz == 1410
        assert gpu.process_count == 3
        assert gpu.pcie_rx_throughput_mb_s == 1024.0


class TestCollector:
    def test_collect_system_info(self):
        """测试系统采集器返回有效信息（含新增运行时指标）。"""
        from llmpod.apps.node.collectors import collect_system_info

        info = collect_system_info()
        assert isinstance(info, SystemInfo)
        assert info.hostname  # 不应为空
        assert info.ip  # 不应为空
        assert info.cpu_cores >= 1
        assert info.memory_gb > 0
        # OS 信息在 Linux 上必填
        assert info.os_name
        assert info.uptime_seconds >= 0
        # 运行时指标应为数值（即使采集失败也是 0.0，不应为 None）
        assert isinstance(info.cpu_percent, float)
        assert isinstance(info.memory_percent, float)
        assert isinstance(info.memory_used_gb, float)
        assert isinstance(info.swap_total_gb, float)
        assert isinstance(info.disk_percent, float)
        assert isinstance(info.cpu_load_avg_1, float)

    def test_collect_system_info_uses_node_name(self, monkeypatch):
        from llmpod.apps.node.collectors import system

        monkeypatch.setattr(system.settings, "node_name", "gpu-west-2")
        monkeypatch.setattr(system.socket, "gethostname", lambda: "cloned-host")
        assert system.collect_system_info().hostname == "gpu-west-2"

    def test_collect_gpu_info_no_gpu(self):
        """测试在无 GPU 机器上的 GPU 采集器。"""
        from llmpod.apps.node.collectors import collect_gpu_info

        gpus = collect_gpu_info()
        # 应返回列表（开发机上可能为空）
        assert isinstance(gpus, list)
        for gpu in gpus:
            assert isinstance(gpu, GpuInfo)

    def test_collect_node_info_aggregation(self):
        """collect_node_info 聚合系统+GPU+capabilities，返回 NodeReport。"""
        from llmpod.apps.node.collectors import collect_node_info
        from llmpod.tunnel.schemas.system import NodeReport

        info = collect_node_info()
        assert isinstance(info, NodeReport)
        # 基础字段
        assert info.hostname
        assert info.ip
        assert info.cpu_cores >= 1
        assert info.memory_gb > 0
        # 运行时字段
        assert hasattr(info, "cpu_percent")
        assert hasattr(info, "memory_used_gb")
        assert hasattr(info, "memory_percent")
        assert hasattr(info, "swap_total_gb")
        assert hasattr(info, "disk_percent")
        assert hasattr(info, "cpu_load_avg_1")
        # GPU + capabilities
        assert isinstance(info.gpus, list)
        assert info.capabilities == ["docker", "shell", "inference"]


class TestTunnelClientInfoCollector:
    """验证 TunnelClient._collect_info 对 info_collector 注入的支持。"""

    def test_collect_info_uses_injected_collector(self):
        from llmpod.tunnel.client import TunnelClient
        from llmpod.tunnel.schemas.system import NodeReport

        client = TunnelClient(
            master_url="ws://x",
            cluster_id=7,
            info_collector=lambda: NodeReport(hostname="h1", ip="10.0.0.1", cpu_cores=8),
        )
        info = client._collect_info()
        assert isinstance(info, NodeReport)
        assert info.hostname == "h1"
        assert info.ip == "10.0.0.1"
        assert info.cpu_cores == 8

    def test_collect_info_fallback_on_collector_exception(self):
        from llmpod.tunnel.client import TunnelClient
        from llmpod.tunnel.schemas.system import NodeReport

        def bad_collector():
            raise RuntimeError("boom")

        client = TunnelClient(
            master_url="ws://x",
            cluster_id=9,
            info_collector=bad_collector,
        )
        info = client._collect_info()
        # 采集异常时回退到基础信息，不抛出
        assert isinstance(info, NodeReport)
        assert info.hostname  # 有默认 hostname

    def test_collect_info_default_without_collector(self):
        from llmpod.tunnel.client import TunnelClient
        from llmpod.tunnel.schemas.system import NodeReport

        client = TunnelClient(master_url="ws://x", cluster_id=3)
        info = client._collect_info()
        assert isinstance(info, NodeReport)
        assert info.hostname

    def test_fallback_report_uses_node_name(self, monkeypatch):
        from llmpod.tunnel.client import TunnelClient
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "node_name", "gpu-west-2")
        assert TunnelClient._base_report().hostname == "gpu-west-2"


# ---- 多厂商 GPU 采集架构测试 ----


class TestGpuInfoVendor:
    """GpuInfo.vendor 字段。"""

    def test_vendor_default_empty(self):
        gpu = GpuInfo(uuid="x", index=0, name="X")
        assert gpu.vendor == ""

    def test_vendor_populated(self):
        gpu = GpuInfo(uuid="x", index=0, name="X", vendor="ascend")
        assert gpu.vendor == "ascend"


class TestGpuCollectorBase:
    """GpuCollectorBase 抽象基类与厂商登记表。"""

    def test_base_is_abstract(self):
        """基类不能直接实例化（未实现抽象方法）。"""
        from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase

        with pytest.raises(TypeError):
            GpuCollectorBase()

    def test_registry_lists_every_collector_with_vendor_and_arch(self):
        """登记表是唯一的厂商入口：每个采集器都自带 vendor 与 arch。"""
        from llmpod.apps.node.collectors.gpu import REGISTRY

        vendors = [c.vendor for c in REGISTRY]
        assert vendors == ["nvidia", "ascend"]

        for collector in REGISTRY:
            # 两个字段都是 GPU 表必填列，缺一个该厂商的卡就会静默落不了库
            assert collector.vendor
            assert collector.arch

    def _probe_counter(self):
        """返回 (采集器实例, 每次探测追加元素的列表)。"""
        from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase

        calls: list[int] = []

        def probe_available(self) -> bool:
            calls.append(1)
            return True

        def collect(self) -> list[GpuInfo]:
            return []

        cls = type(
            "_Probe_Count",
            (GpuCollectorBase,),
            {"vendor": "probe_count_test", "probe_available": probe_available, "collect": collect},
        )
        return cls(), calls

    def test_probe_success_is_cached(self):
        """探测成功后永久缓存，每个心跳不再重复打开驱动。"""
        c, calls = self._probe_counter()
        assert c.is_available() is True
        assert c.is_available() is True
        assert len(calls) == 1

    def test_probe_failure_retried_after_interval(self, monkeypatch):
        """探测失败只缓存一段时间，窗口过后重试——否则启动早期的瞬时失败
        会让该厂商在整个进程生命周期内再也不上报 GPU。"""
        import llmpod.apps.node.collectors.gpu.base as base_mod

        c, calls = self._probe_counter()

        def always_fail() -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(c, "probe_available", always_fail)

        assert c.is_available() is False
        assert c.is_available() is False  # 窗口内走缓存
        assert len(calls) == 1

        monkeypatch.setattr(base_mod, "PROBE_RETRY_SECONDS", 0.0)
        assert c.is_available() is False  # 窗口过后重新探测（仍失败）
        assert len(calls) == 2

    def test_probe_failure_can_recover(self, monkeypatch):
        """瞬时失败后，重试窗口一过即可恢复上报。"""
        import llmpod.apps.node.collectors.gpu.base as base_mod

        c, _ = self._probe_counter()
        probes: list[bool] = []

        def flaky() -> bool:
            probes.append(True)
            return len(probes) > 1

        monkeypatch.setattr(c, "probe_available", flaky)

        assert c.is_available() is False
        monkeypatch.setattr(base_mod, "PROBE_RETRY_SECONDS", 0.0)
        assert c.is_available() is True
        assert c.is_available() is True  # 恢复后永久缓存
        assert len(probes) == 2


class TestCollectGpuInfoDispatcher:
    """collect_gpu_info 分发器：可用性闸 + 异常隔离 + 全局重排 index。"""

    @pytest.fixture(autouse=True)
    def _reset_gpu_cache(self):
        """每个测试前后清空 GPU 缓存，避免缓存跨测试污染。"""
        from llmpod.apps.node.collectors.gpu import clear_gpu_cache

        clear_gpu_cache()
        yield
        clear_gpu_cache()

    @staticmethod
    def _make_vendor(
        vendor: str, gpus: list[GpuInfo] | None = None, available: bool = True, raises: Exception | None = None
    ):
        from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase

        # 用 type() 动态创建类：命名空间在类创建时即包含 probe_available/collect，
        # ABCMeta 才会认为它们实现了抽象方法（赋值到类对象上则不行）
        def probe_available(self) -> bool:
            return available

        def collect(self) -> list[GpuInfo]:
            if raises is not None:
                raise raises
            return list(gpus or [])

        cls = type(
            f"_Vendor_{vendor}",
            (GpuCollectorBase,),
            {"vendor": vendor, "probe_available": probe_available, "collect": collect},
        )
        return cls()

    def test_skips_unavailable_collectors(self, monkeypatch):
        """probe_available() == False 的采集器不应被 collect()。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        calls: list[str] = []

        class Available(GpuCollectorBase):
            vendor = "avail_dispatch_test"

            def probe_available(self) -> bool:
                return True

            def collect(self) -> list[GpuInfo]:
                calls.append("avail")
                return [GpuInfo(uuid="a", index=0, name="A", vendor=self.vendor)]

        class Unavailable(GpuCollectorBase):
            vendor = "unavail_dispatch_test"

            def probe_available(self) -> bool:
                return False

            def collect(self) -> list[GpuInfo]:
                calls.append("unavail")
                return []

        monkeypatch.setattr(gpu_mod, "REGISTRY", [Available(), Unavailable()])
        gpus = collect_gpu_info()
        assert "avail" in calls
        assert "unavail" not in calls
        assert len(gpus) == 1
        assert gpus[0].vendor == "avail_dispatch_test"

    def test_exception_isolation(self, monkeypatch):
        """一个采集器抛异常不影响其它厂商。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        good = self._make_vendor(
            "good_dispatch_test",
            gpus=[GpuInfo(uuid="g", index=0, name="G", vendor="good_dispatch_test")],
        )
        bad = self._make_vendor("bad_dispatch_test", raises=RuntimeError("boom"))
        monkeypatch.setattr(gpu_mod, "REGISTRY", [bad, good])
        gpus = collect_gpu_info()
        assert len(gpus) == 1
        assert gpus[0].vendor == "good_dispatch_test"

    def test_global_index_rerank(self, monkeypatch):
        """跨厂商合并后 index 应全局唯一连续。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        a = self._make_vendor(
            "vendor_a_test",
            gpus=[
                GpuInfo(uuid="a0", index=0, name="A0", vendor="vendor_a_test"),
                GpuInfo(uuid="a1", index=1, name="A1", vendor="vendor_a_test"),
            ],
        )
        b = self._make_vendor(
            "vendor_b_test",
            gpus=[
                GpuInfo(uuid="b0", index=0, name="B0", vendor="vendor_b_test"),
                GpuInfo(uuid="b1", index=1, name="B1", vendor="vendor_b_test"),
            ],
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [a, b])
        gpus = collect_gpu_info()
        assert len(gpus) == 4
        assert [g.index for g in gpus] == [0, 1, 2, 3]
        assert [g.vendor for g in gpus] == [
            "vendor_a_test",
            "vendor_a_test",
            "vendor_b_test",
            "vendor_b_test",
        ]

    def test_empty_registry_returns_empty(self, monkeypatch):
        """空注册表返回空列表。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        monkeypatch.setattr(gpu_mod, "REGISTRY", [])
        assert collect_gpu_info() == []


class TestGpuCache:
    """collect_gpu_info 的 TTL 缓存：降频采集，避免每次心跳都走一遍驱动查询。"""

    @pytest.fixture(autouse=True)
    def _reset_gpu_cache(self):
        from llmpod.apps.node.collectors.gpu import clear_gpu_cache

        clear_gpu_cache()
        yield
        clear_gpu_cache()

    @staticmethod
    def _counting_vendor(vendor: str, gpus: list[GpuInfo]) -> tuple:
        """返回 (采集器实例, collect 调用计数列表) 用于断言缓存命中/未命中。"""
        calls: list[int] = []

        class _C(GpuCollectorBase):
            pass

        def probe_available(self) -> bool:
            return True

        def collect(self) -> list[GpuInfo]:
            calls.append(1)
            return list(gpus)

        cls = type(
            f"_Count_{vendor}",
            (GpuCollectorBase,),
            {"vendor": vendor, "probe_available": probe_available, "collect": collect},
        )
        return cls(), calls

    def test_cache_miss_triggers_collection(self, monkeypatch):
        """首次调用(缓存未命中)触发实际采集。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        collector, calls = self._counting_vendor(
            "cache_miss_test", [GpuInfo(uuid="a", index=0, name="A", vendor="cache_miss_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        gpus = collect_gpu_info()
        assert len(gpus) == 1
        assert len(calls) == 1, "首次应触发实际采集"

    def test_cache_hit_skips_collection(self, monkeypatch):
        """缓存命中时返回副本,不触发实际采集。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        collector, calls = self._counting_vendor(
            "cache_hit_test", [GpuInfo(uuid="b", index=0, name="B", vendor="cache_hit_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        # 第一次:缓存未命中,触发采集
        collect_gpu_info()
        assert len(calls) == 1
        # 第二次:缓存命中,不触发采集
        collect_gpu_info()
        assert len(calls) == 1, "缓存命中应跳过采集"

    def test_cache_returns_copy_not_reference(self, monkeypatch):
        """缓存命中返回副本,调用方修改不影响缓存。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        collector, calls = self._counting_vendor(
            "cache_copy_test", [GpuInfo(uuid="c", index=0, name="C", vendor="cache_copy_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        g1 = collect_gpu_info()
        # 修改返回结果
        g1[0].name = "MODIFIED"
        # 再次获取:缓存应返回原始数据(未受影响)
        g2 = collect_gpu_info()
        assert g2[0].name == "C", "缓存副本应独立,修改不应影响缓存"

    def test_cache_expiry_triggers_recollection(self, monkeypatch):
        """缓存过期后重新触发采集。"""
        import llmpod.apps.node.collectors.gpu as gpu_pkg
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        collector, calls = self._counting_vendor(
            "cache_expiry_test", [GpuInfo(uuid="d", index=0, name="D", vendor="cache_expiry_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        # 用极短 TTL 加快测试
        monkeypatch.setattr(gpu_pkg, "_GPU_CACHE_TTL", 0.05)
        collect_gpu_info()
        assert len(calls) == 1
        import time

        time.sleep(0.1)  # 等待缓存过期
        collect_gpu_info()
        assert len(calls) == 2, "缓存过期后应重新采集"

    def test_clear_gpu_cache_empties_cache(self, monkeypatch):
        """clear_gpu_cache 清空缓存,下次调用重新采集。"""
        from llmpod.apps.node.collectors.gpu import collect_gpu_info, clear_gpu_cache

        collector, calls = self._counting_vendor(
            "clear_test", [GpuInfo(uuid="e", index=0, name="E", vendor="clear_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        collect_gpu_info()
        assert len(calls) == 1
        clear_gpu_cache()
        collect_gpu_info()
        assert len(calls) == 2, "清缓存后应重新采集"

    def test_ttl_zero_disables_cache(self, monkeypatch):
        """_GPU_CACHE_TTL=0 禁用缓存,每次都实际采集。"""
        import llmpod.apps.node.collectors.gpu as gpu_pkg
        from llmpod.apps.node.collectors.gpu import collect_gpu_info

        collector, calls = self._counting_vendor(
            "ttl_zero_test", [GpuInfo(uuid="f", index=0, name="F", vendor="ttl_zero_test")]
        )
        monkeypatch.setattr(gpu_mod, "REGISTRY", [collector])
        monkeypatch.setattr(gpu_pkg, "_GPU_CACHE_TTL", 0)
        collect_gpu_info()
        collect_gpu_info()
        collect_gpu_info()
        assert len(calls) == 3, "TTL=0 时每次都应实际采集"


class TestNvidiaCollector:
    """NvidiaCollector 基本属性与可用性缓存。"""

    def test_vendor(self):
        from llmpod.apps.node.collectors.gpu.nvidia import NvidiaCollector

        assert NvidiaCollector.vendor == "nvidia"

    def test_probe_requires_nvml_init(self, monkeypatch):
        """pynvml 在但 NVML 打不开（驱动缺失/设备未注入）→ 不可用。"""
        import pynvml

        from llmpod.apps.node.collectors.gpu.nvidia import NvidiaCollector

        def boom():
            raise RuntimeError("libnvidia-ml.so not found")

        monkeypatch.setattr(pynvml, "nvmlInit", boom)
        assert NvidiaCollector().probe_available() is False

    def test_probe_succeeds_when_nvml_init_works(self, monkeypatch):
        """NVML 能打开即驱动已装 → 可用；探测后应立即 shutdown。"""
        import pynvml

        from llmpod.apps.node.collectors.gpu.nvidia import NvidiaCollector
        from llmpod.apps.node.collectors.gpu import nvidia as nvidia_mod

        shuts: list[int] = []
        monkeypatch.setattr(pynvml, "nvmlInit", lambda: None)
        monkeypatch.setattr(pynvml, "nvmlShutdown", lambda: shuts.append(1))
        monkeypatch.setattr(nvidia_mod, "pci_vendor_present", lambda vendor: True)

        assert NvidiaCollector().probe_available() is True
        assert len(shuts) == 1

    def test_probe_skips_nvml_when_no_pci_device(self, monkeypatch):
        """PCI 上没有 NVIDIA 卡时直接跳过，不去打开 NVML。"""
        import pynvml

        from llmpod.apps.node.collectors.gpu.nvidia import NvidiaCollector
        from llmpod.apps.node.collectors.gpu import nvidia as nvidia_mod

        inits: list[int] = []
        monkeypatch.setattr(pynvml, "nvmlInit", lambda: inits.append(1))
        monkeypatch.setattr(nvidia_mod, "pci_vendor_present", lambda vendor: False)

        assert NvidiaCollector().probe_available() is False
        assert inits == []


class TestAscendCollector:
    """AscendCollector 基本属性、可用性探测与 DCMI 采集。"""

    def test_vendor(self):
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        assert AscendCollector.vendor == "ascend"

    def test_is_available_with_driver_ready(self, monkeypatch):
        """有 davinci 设备节点 + DCMI 初始化成功 → 可用。"""
        from llmpod.apps.node.collectors.gpu import ascend as ascend_mod
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        inits: list[int] = []

        def record_init():
            inits.append(1)

        c = AscendCollector()
        monkeypatch.setattr(ascend_mod, "device_nodes", lambda patterns: ["/dev/davinci0"])
        monkeypatch.setattr(ascend_mod.dcmi, "init", record_init)

        assert c.is_available() is True
        assert inits == [1]

    def test_is_available_without_device_nodes(self, monkeypatch):
        """驱动在但设备没直通 → 不可用（不必逐卡调用失败才发现）。"""
        from llmpod.apps.node.collectors.gpu import ascend as ascend_mod
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        def fail_init():
            raise AssertionError("设备节点都没有就不该去初始化 DCMI")

        c = AscendCollector()
        monkeypatch.setattr(ascend_mod, "device_nodes", lambda patterns: [])
        monkeypatch.setattr(ascend_mod.dcmi, "init", fail_init)

        assert c.is_available() is False

    def test_is_available_when_dcmi_init_fails(self, monkeypatch):
        """设备节点在但 DCMI 初始化失败（驱动异常）→ 不可用。"""
        from llmpod.apps.node.collectors.gpu import ascend as ascend_mod
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        def fail_init():
            raise ascend_mod.dcmi.DcmiError(-99999, "load")

        c = AscendCollector()
        monkeypatch.setattr(ascend_mod, "device_nodes", lambda patterns: ["/dev/davinci0"])
        monkeypatch.setattr(ascend_mod.dcmi, "init", fail_init)

        assert c.is_available() is False

    def test_collect_single_card(self, monkeypatch):
        """单卡：DCMI 各查询的返回值映射到 GpuInfo。"""
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[8],  # 单卡机的卡 ID 不一定是 0
            devices={8: 1},
            names={(8, 0): "310P3"},
            memory={(8, 0): (21527, 1857)},  # (总量 MB, 已用 MB)
            utilization={(8, 0): 42},
            temperature={(8, 0): 67},
            power={(8, 0): None},  # 310P 不支持查功耗 → 降级为 0
            serials={8: "SN10SYNTH000027068"},
            driver_version="25.2.3",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 1
        gpu = gpus[0]
        assert gpu.uuid == "SN10SYNTH000027068"
        assert gpu.index == 0
        assert gpu.name == "310P3"
        assert gpu.vendor == "ascend"
        assert gpu.arch == "cann"
        assert gpu.soc_model == "310p"
        assert gpu.temperature == 67
        assert gpu.power_draw_w == 0.0
        assert gpu.utilization == 42.0
        assert gpu.used_vram_gb == round(1857 / 1024.0, 2)
        assert gpu.total_vram_gb == round(21527 / 1024.0, 2)
        assert abs(gpu.memory_utilization - round(1857 / 21527 * 100, 1)) < 0.1
        assert gpu.driver_version == "25.2.3"

    def test_collect_multi_chip_card_merges_to_one_gpu(self, monkeypatch):
        """多芯片卡：显存求和、利用率与温度取峰值、功耗求和，归并成一块卡。"""
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[0],
            devices={0: 2},
            names={(0, 0): "910B4", (0, 1): "910B4"},
            memory={(0, 0): (16384, 1024), (0, 1): (16384, 2048)},
            utilization={(0, 0): 10, (0, 1): 80},
            temperature={(0, 0): 55, (0, 1): 61},
            power={(0, 0): 150.0, (0, 1): 150.0},
            serials={0: "BOARD-SERIAL-0"},
            driver_version="25.2.3",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 1
        gpu = gpus[0]
        assert gpu.name == "910B4"
        assert gpu.total_vram_gb == round(32768 / 1024.0, 2)
        assert gpu.used_vram_gb == round(3072 / 1024.0, 2)
        assert gpu.utilization == 80.0  # 取最忙的芯片
        assert gpu.temperature == 61
        assert gpu.power_draw_w == 300.0

    def test_collect_multi_card(self, monkeypatch):
        """多卡：逐卡产出 GpuInfo，index 按卡序。"""
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[0, 1],
            devices={0: 1, 1: 1},
            names={(0, 0): "910A", (1, 0): "910B"},
            memory={(0, 0): (16384, 1024), (1, 0): (16384, 2048)},
            utilization={(0, 0): 0, (1, 0): 25},
            temperature={(0, 0): 45, (1, 0): 50},
            power={(0, 0): 65.5, (1, 0): 70.2},
            serials={0: "BOARD-0", 1: "BOARD-1"},
            driver_version="25.2.3",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 2
        assert [g.index for g in gpus] == [0, 1]
        assert gpus[0].uuid == "BOARD-0"
        assert gpus[0].name == "910A"
        assert gpus[1].uuid == "BOARD-1"
        assert gpus[1].utilization == 25.0

    def test_collect_skips_card_whose_core_query_fails(self, monkeypatch):
        """单卡核心字段（芯片数/芯片名/显存）取不到只跳过该卡，其余卡照常上报。"""
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[0, 1],
            devices={0: 1, 1: 1},
            failing_cards={0},
            names={(0, 0): "", (1, 0): "910B"},
            memory={(0, 0): (16384, 1024), (1, 0): (16384, 2048)},
            utilization={(0, 0): 0, (1, 0): 0},
            temperature={(0, 0): 0, (1, 0): 0},
            power={(0, 0): 0.0, (1, 0): 0.0},
            serials={0: "BOARD-0", 1: "BOARD-1"},
            driver_version="25.2.3",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 1
        assert gpus[0].uuid == "BOARD-1"

    def test_collect_returns_empty_when_dcmi_unavailable(self, monkeypatch):
        """DCMI 初始化就失败 → 返回空列表，不抛异常。"""
        from llmpod.apps.node.collectors.gpu import ascend as ascend_mod
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        def fail_init():
            raise ascend_mod.dcmi.DcmiError(-99999, "load")

        monkeypatch.setattr(ascend_mod.dcmi, "init", fail_init)
        assert AscendCollector().collect() == []

    def test_collect_falls_back_uuid_when_serial_missing(self, monkeypatch):
        """序列号读不到或为空时，uuid 回退到 ascend-{card_id}-{name}。"""
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[3],
            devices={3: 1},
            names={(3, 0): "910A"},
            memory={(3, 0): (16384, 1024)},
            utilization={(3, 0): 0},
            temperature={(3, 0): 45},
            power={(3, 0): 0.0},
            serials={3: ""},
            driver_version="23.0.1",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 1
        assert gpus[0].uuid == "ascend-3-910A"

    def test_collect_real_310p3_shape(self, monkeypatch):
        """实机样本（310P3）：DCMI 卡 ID 是 8 而非 0。

        与 ``test_collect_single_card`` 的区别只在这一点：卡 ID 不连续时仍要
        逐卡采到，且功耗不支持时降级为 0。
        """
        from llmpod.apps.node.collectors.gpu.ascend import AscendCollector

        _stub_dcmi(
            monkeypatch,
            cards=[8],
            devices={8: 1},
            names={(8, 0): "310P3"},
            memory={(8, 0): (21527, 1850)},
            utilization={(8, 0): 0},
            temperature={(8, 0): 79},
            power={(8, 0): None},
            serials={8: "SN10SYNTH000027068"},
            driver_version="25.2.3",
        )

        gpus = AscendCollector().collect()
        assert len(gpus) == 1
        assert gpus[0].uuid == "SN10SYNTH000027068"
        assert gpus[0].temperature == 79
        assert gpus[0].power_draw_w == 0.0


class _StubDcmi:
    """按卡/芯片返回预设值的 DCMI 替身，整体替换 ``ascend_mod.dcmi``。

    - ``DcmiError`` / ``is_degradable`` 绑定真实实现，降级判定走真实逻辑
    - 功耗预设成 ``None`` 表示该型号不支持查询——替身照真实驱动的做法抛
      「不支持」码，用于验证采集器把它降级为 0 而不是整卡失败
    - ``failing_cards`` 里的卡在核心查询（芯片数）上抛错，用于验证单卡失败隔离
    """

    DcmiError = dcmi.DcmiError
    is_degradable = staticmethod(dcmi.is_degradable)

    def __init__(self, plan: dict):
        self.plan = plan

    def init(self) -> None:
        pass

    def get_driver_version(self) -> str:
        return self.plan["driver_version"]

    def get_card_list(self) -> list[int]:
        return list(self.plan["cards"])

    def get_device_count(self, card_id: int) -> int:
        if card_id in self.plan.get("failing_cards", ()):
            raise dcmi.DcmiError(dcmi.DCMI_ERR_NOT_SUPPORT, "dcmi_get_device_num_in_card")
        return self.plan["devices"][card_id]

    def get_chip_name(self, card_id: int, device_id: int) -> str:
        return self.plan["names"][(card_id, device_id)]

    def get_memory_mb(self, card_id: int, device_id: int) -> tuple[int, int]:
        return self.plan["memory"][(card_id, device_id)]

    def get_utilization(self, card_id: int, device_id: int) -> int:
        return self.plan["utilization"][(card_id, device_id)]

    def get_temperature(self, card_id: int, device_id: int) -> int:
        return self.plan["temperature"][(card_id, device_id)]

    def get_power_draw_w(self, card_id: int, device_id: int) -> float:
        value = self.plan["power"][(card_id, device_id)]
        if value is None:
            raise dcmi.DcmiError(dcmi.DCMI_ERR_NOT_SUPPORT, "dcmi_get_device_power_info")
        return value

    def get_card_serial(self, card_id: int) -> str:
        return self.plan["serials"][card_id]


def _stub_dcmi(monkeypatch, **plan) -> None:
    """把采集器看到的 DCMI 换成本地替身（不加载真实驱动）。

    ``DcmiError`` / ``is_degradable`` 仍指向真实实现——降级判定逻辑要真的被测到。
    其余查询全由替身接管，替身没定义的名字会直接抛 AttributeError，不会悄悄
    落到真驱动上。
    """
    from llmpod.apps.node.collectors.gpu import ascend as ascend_mod

    monkeypatch.setattr(ascend_mod, "dcmi", _StubDcmi(plan))


class TestLocalIp:
    def test_explicit_advertise_ip_wins(self, monkeypatch):
        """显式 node_advertise_ip 优先（bridge 网络手动指定宿主可达 IP）。"""
        from llmpod.apps.node.collectors import system as sys_mod
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "node_advertise_ip", "10.1.2.3")
        assert sys_mod._get_local_ip() == "10.1.2.3"

    def test_never_loopback(self, monkeypatch):
        """8.8.8.8 不可达 + 无非 loopback 网卡 → 空串，绝不回退 127.0.0.1。"""
        from llmpod.apps.node.collectors import system as sys_mod
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod, "_non_loopback_ips", lambda pairs=None: iter(()))
        # 让 UDP connect 抛异常 → 走到枚举兜底（空）→ 返回 ""
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)
        assert sys_mod._get_local_ip() == ""


class _NoNetworkSocket:
    """mock socket：connect 抛异常，模拟 8.8.8.8 不可达。"""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, *a):
        raise OSError("network unreachable")

    def getsockname(self):
        return ("0.0.0.0", 0)
