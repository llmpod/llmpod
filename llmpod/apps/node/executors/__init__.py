"""Node 端任务执行器（声明式任务的具体执行）。

``Executor`` ABC 是唯一扩展点：新增任务类型 = 新增 ``Executor`` 子类 + 注册到 registry。
``NodeAgent`` 按 ``task.type`` 从 registry 派发；未知 type → FAILED ``无执行器 type=…``。

任务类型：``ENSURE_MODEL``（模型分发）+ 容器类任务（需 Deployer）：``PULL_IMAGE``
（镜像落点）、``START_INSTANCE``（起容器 + Node 本地 readiness 探活）、``STOP_INSTANCE``
（纯停容器，不删）。未注入 deployer 的 registry 不注册容器类任务——测试/轻量进程只用
模型分发。

执行器只负责具体任务的运行时逻辑 + 进度上报（``report`` callback）；持久化（local_tasks）
与 Manager 上报由 ``NodeAgent`` 的 report 包装器接管（解耦）。
"""

from __future__ import annotations

from llmpod.apps.node.deployers.base import Deployer
from llmpod.config.constants import TaskType
from llmpod.apps.node.executors.base import Executor, ReconcileResult, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, EnsureModelExecutor
from llmpod.apps.node.executors.pull_image import PullImageExecutor
from llmpod.apps.node.executors.start_instance import StartInstanceExecutor
from llmpod.apps.node.executors.stop_instance import StopInstanceExecutor

__all__ = [
    "Executor",
    "ReconcileResult",
    "ReportFn",
    "EnsureModelExecutor",
    "PullImageExecutor",
    "StartInstanceExecutor",
    "StopInstanceExecutor",
    "DownloadCancelled",
    "default_registry",
]


def default_registry(deployer: Deployer | None = None) -> dict[str, Executor]:
    """executor registry：``{task.type: Executor}``。新增类型在此注册。

    ``deployer`` 为容器生命周期任务所需；None 时只注册模型分发，容器类任务走
    「无执行器」兜底（部署进程外的轻量场景）。
    """
    registry: dict[str, Executor] = {TaskType.ENSURE_MODEL: EnsureModelExecutor()}
    if deployer is not None:
        registry[TaskType.PULL_IMAGE] = PullImageExecutor(deployer)
        registry[TaskType.START_INSTANCE] = StartInstanceExecutor(deployer)
        registry[TaskType.STOP_INSTANCE] = StopInstanceExecutor(deployer)
    return registry
