"""GPU 信息采集子包：多厂商采集器分发器。

``REGISTRY`` 是本子包唯一的厂商登记处（厂商采集器实例，按顺序执行）。
``collect_gpu_info()`` 作为统一入口，遍历 ``REGISTRY``，对 ``is_available()``
返回 ``True`` 的采集器调用 ``collect()`` 并合并结果，最后对合并后的列表做
全局 ``index`` 重排（跨厂商唯一连续）。

为避免高频 ``$heartbeat``（默认 10 秒）反复走一遍驱动查询（Ascend 的 DCMI
调用每卡若干次 ioctl，NVIDIA 的 NVML 亦然），``collect_gpu_info()`` 带 TTL
缓存（默认 30 秒）：缓存命中时直接返回副本，不触发实际采集；缓存失效才重新采集。

新增厂商支持只需：

1. 新建 ``<vendor>.py``，实现 :class:`GpuCollectorBase` 子类（声明 ``vendor``
   与 ``arch``，实现 ``probe_available`` 与 ``collect``）
2. 在下面 ``REGISTRY`` 里追加一个该采集器实例

绑定库（如 ``dcmi.py``）与采集器同目录平铺即可；只有当某个绑定本身需要多个
文件、或是整份引入的第三方代码时，才值得升格成自己的子包。
"""

from __future__ import annotations

import logging
import time

from llmpod.apps.node.collectors.gpu.ascend import AscendCollector
from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase
from llmpod.apps.node.collectors.gpu.nvidia import NvidiaCollector
from llmpod.tunnel.schemas.system import GpuInfo

log = logging.getLogger("llmpod.apps.node.collectors.gpu")

# 厂商采集器登记处。顺序即执行顺序，也决定未加区分的 index 先后。
REGISTRY: tuple[GpuCollectorBase, ...] = (
    NvidiaCollector(),
    AscendCollector(),
)

__all__ = [
    "collect_gpu_info",
    "clear_gpu_cache",
    "REGISTRY",
    "GpuCollectorBase",
]

# ---- TTL 缓存（降频 GPU 采集，避免每次心跳都走一遍驱动查询）----
_GPU_CACHE_TTL: float = 30.0  # 缓存有效期（秒）；设 0 禁用缓存
_gpu_cache: list[GpuInfo] = []
_gpu_cache_ts: float = 0.0


def clear_gpu_cache() -> None:
    """清空 GPU 采集缓存。主要用于单测隔离。"""
    global _gpu_cache, _gpu_cache_ts
    _gpu_cache = []
    _gpu_cache_ts = 0.0


def collect_gpu_info() -> list[GpuInfo]:
    """采集所有可用厂商的 GPU 信息并合并返回。

    - 带 TTL 缓存（默认 30 秒）：缓存命中返回副本，不触发实际采集；
      缓存失效才重新采集。高频 heartbeat 场景下大幅减少驱动查询次数。
    - 对每个已注册采集器先调用 ``is_available()`` 闸门：不可用直接跳过
    - 单个采集器抛异常不影响其它厂商，仅记日志并跳过该厂商
    - 合并后对 ``index`` 做全局重排，保证跨厂商唯一连续（0..N-1）
    """
    global _gpu_cache, _gpu_cache_ts

    now = time.time()
    # 注意：用 _gpu_cache_ts > 0 而非 _gpu_cache 真值判断命中，
    # 否则空结果（无 GPU）会让缓存永远失效，每次 heartbeat 都重新采集。
    if _GPU_CACHE_TTL > 0 and _gpu_cache_ts > 0 and (now - _gpu_cache_ts) < _GPU_CACHE_TTL:
        # 缓存命中：返回副本，避免调用方修改缓存内对象
        return [g.model_copy() for g in _gpu_cache]

    gpus = _collect_gpu_info_uncached()
    # 缓存副本（与返回值独立）：调用方修改返回值不影响缓存快照
    _gpu_cache = [g.model_copy() for g in gpus]
    _gpu_cache_ts = now
    return gpus


def _collect_gpu_info_uncached() -> list[GpuInfo]:
    """实际采集逻辑（无缓存）。遍历注册表执行各厂商采集器。"""
    gpus: list[GpuInfo] = []
    for collector in REGISTRY:
        try:
            if not collector.is_available():
                continue
            gpus.extend(collector.collect())
        except Exception as e:
            log.warning("%s GPU collection failed: %s", collector.vendor or "?", e)
            continue

    # 全局重排 index，保证跨厂商唯一连续
    for i, g in enumerate(gpus):
        g.index = i

    return gpus
