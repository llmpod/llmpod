"""NVIDIA GPU 采集器：基于 pynvml。

采集显存/核心利用率/编码器/解码器/温度/风扇/功耗/功耗上限/时钟/进程数/PCIe 吞吐。
所有可选指标在不可用（老驱动/无权限）时静默回退到默认值，不影响整体采集。

通过 :class:`NvidiaCollector` 实现 :class:`GpuCollectorBase` 接口；实例在
分发器的 ``REGISTRY``（``gpu/__init__.py``）里登记。
"""

from __future__ import annotations

import logging

from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase
from llmpod.apps.node.collectors.gpu.probe import pci_vendor_present
from llmpod.tunnel.schemas.system import GpuInfo

log = logging.getLogger("llmpod.apps.node.collectors.gpu.nvidia")

_GB = 1024**3
_KB = 1024  # pynvml PCIe throughput 字段单位为 KiB/s

# NVIDIA 的 PCI vendor id（用于在打开 NVML 前先确认机器上确实有卡）
_PCI_VENDOR_NVIDIA = "0x10de"


class NvidiaCollector(GpuCollectorBase):
    """NVIDIA GPU 采集器（基于 pynvml）。

    ``probe_available()`` 逐层确认：``pynvml`` 已安装 → PCI 上确实有 NVIDIA 卡
    → ``nvmlInit()`` 成功（等价于 libnvidia-ml.so 能加载，即驱动已装且设备对
    本进程可见）。只 ``import pynvml`` 成功是不够的：驱动缺失或容器没注入设备时
    它照样能导入，要等到 ``collect()`` 才报错。
    """

    vendor = "nvidia"
    arch = "nvidia"  # 软件栈名（CUDA 栈沿用厂商名）

    def probe_available(self) -> bool:
        try:
            import pynvml
        except ImportError:
            log.warning("pynvml not installed, NVIDIA GPU collection disabled")
            return False

        if pci_vendor_present(_PCI_VENDOR_NVIDIA) is False:
            log.info("no NVIDIA PCI device (vendor %s), skipping NVML probe", _PCI_VENDOR_NVIDIA)
            return False

        try:
            pynvml.nvmlInit()
        except Exception as e:
            log.warning("NVML init failed, NVIDIA GPU collection disabled: %s", e)
            return False
        # 探测完即关，collect() 会重新 init（NVML 的 init 是引用计数，可重复）
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
        return True

    def collect(self) -> list[GpuInfo]:
        """使用 pynvml 采集 NVIDIA GPU 信息。无 GPU/pynvml 缺失时返回空列表。"""
        try:
            import pynvml

            pynvml.nvmlInit()

            device_count = pynvml.nvmlDeviceGetCount()
            gpus = []

            # 驱动版本为系统级，所有 GPU 共享，循环外只取一次
            driver_version = _decode(_safe_call(lambda: pynvml.nvmlSystemGetDriverVersion()))

            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)

                # GPU UUID
                uuid = _decode(pynvml.nvmlDeviceGetUUID(handle))

                # GPU 名称
                name = _decode(pynvml.nvmlDeviceGetName(handle))

                # 显存信息
                used_vram_gb, total_vram_gb, mem_util = _collect_memory(handle, pynvml)

                # 利用率（核心/显存/编码器/解码器）
                gpu_util, enc_util, dec_util, mem_util2 = _collect_utilization(handle, pynvml)
                # 优先使用 utilization 结构中的显存利用率，回退到 memory_info 的比例
                mem_util = mem_util2 if mem_util2 > 0 else mem_util

                # 温度
                temp = _safe_int(lambda: pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))

                # 风扇转速
                fan_speed = _safe_int(lambda: pynvml.nvmlDeviceGetFanSpeed(handle))

                # 功耗
                power_draw = _safe_float(
                    lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # 毫瓦 → 瓦
                )
                power_limit = _safe_float(lambda: pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0)

                # 时钟
                sm_clock, mem_clock = _collect_clocks(handle, pynvml)

                # 进程数
                process_count = _safe_int(lambda: len(pynvml.nvmlDeviceGetComputeRunningProcesses(handle)))

                # PCIe 吞吐
                pcie_rx, pcie_tx = _collect_pcie(handle, pynvml)

                gpus.append(
                    GpuInfo(
                        uuid=uuid,
                        index=i,
                        name=name,
                        vendor=self.vendor,
                        arch=self.arch,
                        # 显存
                        used_vram_gb=round(used_vram_gb, 2),
                        total_vram_gb=round(total_vram_gb, 2),
                        memory_utilization=round(mem_util, 1),
                        # 计算
                        utilization=round(gpu_util, 1),
                        encoder_utilization=round(enc_util, 1),
                        decoder_utilization=round(dec_util, 1),
                        # 散热/功耗
                        temperature=temp,
                        fan_speed=fan_speed,
                        power_draw_w=round(power_draw, 1),
                        power_limit_w=round(power_limit, 1),
                        # 时钟
                        sm_clock_mhz=sm_clock,
                        mem_clock_mhz=mem_clock,
                        # 进程/带宽
                        process_count=process_count,
                        pcie_rx_throughput_mb_s=round(pcie_rx, 2),
                        pcie_tx_throughput_mb_s=round(pcie_tx, 2),
                        # 其它
                        driver_version=driver_version,
                    )
                )

            pynvml.nvmlShutdown()
            return gpus

        except ImportError:
            log.warning("pynvml not installed, NVIDIA GPU collection disabled")
            return []
        except Exception as e:
            log.warning("NVIDIA GPU collection failed: %s", e)
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
            return []


# ---- 内部辅助 ----


def _decode(value) -> str:
    """将 pynvml 返回的 bytes 解码为 str（坏字节按替换符容忍，不抛）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def _safe_call(fn):
    """安全调用 pynvml 函数，失败返回 None。"""
    try:
        return fn()
    except Exception:
        return None


def _safe_int(fn) -> int:
    """安全调用并转 int。"""
    val = _safe_call(fn)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(fn) -> float:
    """安全调用并转 float。"""
    val = _safe_call(fn)
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _collect_memory(handle, pynvml) -> tuple[float, float, float]:
    """获取 (used_vram_gb, total_vram_gb, mem_util_percent)。"""
    try:
        mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        used = mem_info.used / _GB
        total = mem_info.total / _GB
        util = (used / total * 100) if total > 0 else 0.0
        return used, total, util
    except Exception:
        return 0.0, 0.0, 0.0


def _collect_utilization(handle, pynvml) -> tuple[float, float, float, float]:
    """获取 (gpu_util, encoder_util, decoder_util, memory_util)。

    nvmlDeviceGetUtilizationRates 返回 gpu/memory 0-100 整数；
    编码器/解码器利用率通过 nvmlDeviceGetEncoderUtilization / GetDecoderUtilization 获取（返回 (util, sampling_period_us)）。
    """
    gpu_util = 0.0
    mem_util = 0.0
    try:
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        gpu_util = float(util.gpu)
        mem_util = float(util.memory)
    except Exception:
        pass

    enc_util = _safe_float(lambda: pynvml.nvmlDeviceGetEncoderUtilization(handle)[0])
    dec_util = _safe_float(lambda: pynvml.nvmlDeviceGetDecoderUtilization(handle)[0])
    return gpu_util, enc_util, dec_util, mem_util


def _collect_clocks(handle, pynvml) -> tuple[int, int]:
    """获取 (sm_clock_mhz, mem_clock_mhz)。"""
    sm_clock = _safe_int(lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM))
    mem_clock = _safe_int(lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM))
    return sm_clock, mem_clock


def _collect_pcie(handle, pynvml) -> tuple[float, float]:
    """获取 (pcie_rx_mb_s, pcie_tx_mb_s)。

    nvmlDeviceGetPcieThroughput 返回 KiB/s，转 MB/s。
    """
    rx = _safe_float(lambda: pynvml.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_RX) / _KB)
    tx = _safe_float(lambda: pynvml.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_TX) / _KB)
    return rx, tx
