"""Tunnel wire 契约 Pydantic model 集。

所有 Manager↔Node wire 契约 Pydantic model：RPC 形参（入参）+ 返回值（出参）
+ ``$hello``/``$heartbeat`` payload。与 ``tunnel/contract.py``（IDL）配套，构成 wire 层
完整类型契约。

- ``deploy``: ContainerService.deploy 形参（DeploySpec，入参）
- ``container``: ContainerService 返回值（DeployResult / StopResult / ...）
- ``model_store``: 文件服务 manifest 契约（ManifestFile / Manifest）
- ``node``: NodeService 返回值（ShellResult）
- ``system``: $hello/$heartbeat payload + info() 返回值（SystemInfo / GpuInfo / NodeReport）
"""

from llmpod.tunnel.schemas.container import (
    ContainerState,
    ContainerSummary,
    DeployResult,
    RemoveResult,
    StopResult,
)
from llmpod.tunnel.schemas.deploy import DeploySpec, docker_run_command
from llmpod.tunnel.schemas.model_store import Manifest, ManifestFile
from llmpod.tunnel.schemas.node import ShellResult
from llmpod.tunnel.schemas.system import GpuInfo, NodeReport, SystemInfo
from llmpod.tunnel.schemas.task import (
    Ack,
    EnsureModelPayload,
    InstanceStatusPayload,
    PullImagePayload,
    ReadinessConfig,
    StartInstancePayload,
    StopInstancePayload,
    TaskEventPayload,
    TaskPayload,
    TaskState,
)

__all__ = [
    # deploy (input)
    "DeploySpec",
    "docker_run_command",
    # container
    "DeployResult",
    "StopResult",
    "RemoveResult",
    "ContainerSummary",
    "ContainerState",
    # model_store（distribution HTTP manifest 契约）
    "ManifestFile",
    "Manifest",
    # node
    "ShellResult",
    # system
    "SystemInfo",
    "GpuInfo",
    "NodeReport",
    # task (统一任务底座 Manager↔Node)
    "Ack",
    "TaskPayload",
    "TaskState",
    "TaskEventPayload",
    "InstanceStatusPayload",
    # 任务配方（TaskPayload.payload 的 typed 内层）
    "ReadinessConfig",
    "PullImagePayload",
    "StartInstancePayload",
    "StopInstancePayload",
    "EnsureModelPayload",
]
