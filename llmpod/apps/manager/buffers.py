"""Manager 进程内实时状态缓冲区：心跳数据的内存镜像。

两个对偶缓冲区，均在 ``app.py`` 与 ``node_hub`` 同生命周期实例化为全局单例，
被 ``bootstrap``（写）与 ``feasibility``/``topology``/``dispatch`` 等路由（读）共享：

- ``GpuStatusBuffer``：GPU 实时指标（used_vram/util/温度/功耗），逐卡。
- ``SystemStatusBuffer``：节点级系统指标（可用 RAM），feasibility 的 GGUF offload 判定需要。

设计：DB 只存静态资产（Gpu/Node 表）；高频实时指标走内存，避免每心跳刷库。
当前仅内存缓存，批量刷 PG 是 TODO（used_vram/utilization 变化过于频繁）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from llmpod.tunnel.schemas.system import GpuInfo, NodeReport

log = logging.getLogger("llmpod.apps.manager.buffers")


class GpuStatusBuffer:
    """GPU 状态内存缓冲区：提供实时查询能力。

    DB 的 Gpu 表只存静态资产；实时高频指标走本内存缓冲（心跳镜像）。
    """

    def __init__(self):
        self._buffer: dict[str, list[GpuInfo]] = {}  # node_id -> [GpuInfo, ...]

    def update(self, node_id: str, gpus: list[GpuInfo]):
        """使用心跳中的最新 GPU 状态更新缓冲区。"""
        self._buffer[node_id] = gpus

    def get(self, node_id: str) -> list[GpuInfo]:
        """获取指定节点的当前 GPU 状态。"""
        return self._buffer.get(node_id, [])

    def gpu_uuids(self, node_id: str) -> set[str]:
        """获取指定节点已缓冲的 GPU uuid 集合"""
        return {g.uuid for g in self._buffer.get(node_id, []) if g.uuid}

    def get_all(self) -> dict[str, list[GpuInfo]]:
        """获取所有 GPU 状态。"""
        return dict(self._buffer)

    def remove_node(self, node_id: str):
        """断开连接时移除节点的 GPU 缓冲区。"""
        self._buffer.pop(node_id, None)

    def clear(self):
        """清空所有缓冲区。"""
        self._buffer.clear()


@dataclass
class SystemStatus:
    """节点级实时系统指标（内存 + CPU/磁盘使用率，供监控与调度共用）。

    ``memory_gb``/``memory_used_gb`` 是 feasibility GGUF offload 判定依赖的两个字段；
    其余（cpu_percent/磁盘等）为节点监控面使用率，字段成本可忽略，随心跳一并镜像。
    """

    memory_gb: float = 0.0  # 总内存（GB）
    memory_used_gb: float = 0.0  # 已用内存（GB）
    cpu_percent: float = 0.0  # CPU 使用率 0-100
    memory_percent: float = 0.0  # 内存使用率 0-100
    disk_total_gb: float = 0.0  # 根分区总量（GB）
    disk_used_gb: float = 0.0  # 根分区已用（GB）
    disk_percent: float = 0.0  # 根分区使用率 0-100


class SystemStatusBuffer:
    """节点系统状态内存缓冲区：提供实时 RAM / CPU / 磁盘查询。

    ``NodeReport`` 每条心跳带全量系统指标；feasibility 的 GGUF offload 判定需要可用
    RAM，节点监控面需要 CPU/内存/磁盘使用率，故统一缓冲节点级系统指标（与 GPU 缓冲平行）。
    """

    def __init__(self):
        self._buffer: dict[str, SystemStatus] = {}  # node_id -> SystemStatus

    def update(self, node_id: str, info: NodeReport) -> None:
        """用心跳中的最新系统状态更新缓冲区。"""
        self._buffer[node_id] = SystemStatus(
            memory_gb=info.memory_gb or 0.0,
            memory_used_gb=info.memory_used_gb or 0.0,
            cpu_percent=info.cpu_percent or 0.0,
            memory_percent=info.memory_percent or 0.0,
            disk_total_gb=info.disk_total_gb or 0.0,
            disk_used_gb=info.disk_used_gb or 0.0,
            disk_percent=info.disk_percent or 0.0,
        )

    def get(self, node_id: str) -> SystemStatus | None:
        """获取指定节点的当前系统状态；未上报返回 None。"""
        return self._buffer.get(node_id)

    def get_all(self) -> dict[str, SystemStatus]:
        """获取所有节点系统状态。"""
        return dict(self._buffer)

    def remove_node(self, node_id: str) -> None:
        """断开连接时移除节点的系统状态缓冲。"""
        self._buffer.pop(node_id, None)

    def clear(self) -> None:
        """清空所有缓冲区。"""
        self._buffer.clear()
