"""部署域 typed model 统一出口（按内聚分文件，re-export 便于单点 import）。

分组：
- ``config``：DeploymentConfig + Topology/HealthCheck/GpuSelector/EngineConfig（spec CRD，组合内聚）
- ``instance``：InstanceSpec（编排器内部 instance 规格）
- ``scheduler``：GpuCandidate / PlacementPrefs / GpuAssignment（调度器 I/O）
- ``occupancy``：GpuOccupancy / OccupiedBy（声明占用）+ CardSummary / CardOccupant（卡的现状）
- ``device``：DeviceConfig（适配器产出的设备配置）
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.schemas.config import (
    CpuLaneConfig,
    DeploymentConfig,
    EngineConfig,
    GpuSelector,
    HealthCheck,
    KVCacheConfig,
    PublishConfig,
    Topology,
    set_topology_copies,
)
from llmpod.apps.manager.deployment.schemas.device import DeviceConfig
from llmpod.apps.manager.deployment.schemas.instance import InstanceSpec
from llmpod.apps.manager.deployment.schemas.occupancy import CardSummary, CardOccupant, GpuOccupancy, OccupiedBy
from llmpod.apps.manager.deployment.schemas.scheduler import (
    GpuAssignment,
    GpuCandidate,
    NodeCandidate,
    PlacementPrefs,
)

__all__ = [
    "DeploymentConfig",
    "CpuLaneConfig",
    "Topology",
    "HealthCheck",
    "EngineConfig",
    "GpuSelector",
    "PublishConfig",
    "KVCacheConfig",
    "InstanceSpec",
    "GpuCandidate",
    "PlacementPrefs",
    "NodeCandidate",
    "GpuAssignment",
    "DeviceConfig",
    "GpuOccupancy",
    "CardSummary",
    "CardOccupant",
    "OccupiedBy",
    "set_topology_copies",
]
