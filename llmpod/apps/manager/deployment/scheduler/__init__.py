"""调度器三件：拓扑展开 / GPU 分配 / rank 解析。

- 拓扑展开：``adapter.expand_topology``（默认 ``expand_standard``，见 topology.py）。
- GPU 分配：``GpuScheduler``（有状态，依赖 ResourceLedger）。
- rank 解析：``RankResolver``。
"""

from llmpod.apps.manager.deployment.scheduler.topology import (
    expand_standard,
    validate_cpu_lane,
    validate_topology,
)
from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler
from llmpod.apps.manager.deployment.scheduler.rank import RankResolver, subnet_warning, validate_same_subnet

__all__ = [
    "expand_standard",
    "validate_topology",
    "validate_cpu_lane",
    "GpuScheduler",
    "RankResolver",
    "validate_same_subnet",
    "subnet_warning",
]
