"""GPU 采集器抽象基类。

``GpuCollectorBase`` 是所有厂商采集器的抽象基类，自带可用性探测的缓存策略。
厂商实例的登记在分发器（``gpu/__init__.py`` 的 ``REGISTRY``）里显式声明，
新增厂商 = 实现一个本类子类 + 在 ``REGISTRY`` 追加一个实例。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型注解用，避免运行期循环导入
    from llmpod.tunnel.schemas.system import GpuInfo

# 探测失败后的重试间隔（秒）。
# 探测成功永久缓存，避免每个心跳都重新打开驱动；
# 探测失败只缓存这么久——否则节点刚起来时驱动或设备尚未就绪的一次瞬时失败，
# 会让该厂商在整个进程生命周期内再也不上报 GPU。
PROBE_RETRY_SECONDS = 60.0


class GpuCollectorBase(ABC):
    """GPU 采集器抽象基类。

    子类需声明/实现：

    - ``vendor``：厂商标识（``nvidia``/``ascend``/...），写入 ``GpuInfo.vendor``
    - ``arch``：软件栈名（``nvidia``/``cann``/...），写入 ``GpuInfo.arch``；
      这是 ``gpu`` 表的必填列兼镜像选择依据，所以随类声明而不是散在构造调用里
    - ``probe_available()``：本厂商驱动与设备在当前环境是否真的可用
      （``pynvml`` / ``libdcmi`` 存在只是前提，还要确认驱动能打开、设备已直通）
    - ``collect()``：实际采集逻辑，返回 ``GpuInfo`` 列表；``index`` 可保持
      厂商内部序号，分发器会做全局重排以保证跨厂商唯一连续

    ``is_available()`` 由基类实现并缓存探测结果，子类不要覆写。
    """

    vendor: str = ""
    arch: str = ""

    # 可用性缓存：None 未探测，True/False 为上次探测结论
    _available: bool | None = None
    # 上次探测时刻，失败时据此判断是否到了重试间隔
    _probed_at: float = 0.0

    @abstractmethod
    def probe_available(self) -> bool:
        """探测本厂商驱动与设备是否可用（只在缓存失效时被调用）。"""
        raise NotImplementedError

    @abstractmethod
    def collect(self) -> "list[GpuInfo]":
        """采集该厂商所有 GPU 的运行时信息。"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """本厂商 GPU 是否可用（带缓存，供分发器闸门使用）。"""
        if self._available is True:
            return True
        now = time.time()
        if self._available is False and (now - self._probed_at) < PROBE_RETRY_SECONDS:
            return False
        self._available = self.probe_available()
        self._probed_at = now
        return self._available
