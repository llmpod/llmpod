"""Deployer ABC：runtime 无关的部署执行器接口。

Node 侧按 env ``LLMPOD_DEPLOYER_TYPE`` 选实现：
- ``docker``（默认）→ ``DockerDeployer``（docker SDK）
- ``k8s``（将来）→ ``K8sDeployer``（kubernetes-client，当前显式 ``ConfigError`` 拒绝）

方法签名与 ``ContainerService`` 契约（``tunnel/contract.py``）一一对应：``ContainerServiceImpl``
做薄派发，把调用转发到当前选中的 ``Deployer`` 实现（per-name 互斥锁在 Deployer 层持有）。这样 runtime 差异全部封装在
Deployer 内部，``ContainerService`` 契约与编排层对 runtime 无感。

``inspect`` 返回的 State 子树形状（``{Status, Running, Restarting, RestartCount}``，
PascalCase）是 Node 侧启动任务 gate（StartInstanceExecutor 存活判定）依赖的契约——
任何后端实现都必须合成同形状（K8s 后端把 pod readiness 映射成同形状）。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from llmpod.tunnel.schemas import (
    ContainerState,
    ContainerSummary,
    DeployResult,
    DeploySpec,
    RemoveResult,
    StopResult,
)


class Deployer(ABC):
    """部署执行器抽象基类：把 ``DeploySpec`` 翻译成具体 runtime 调用。"""

    @abstractmethod
    async def deploy(self, spec: DeploySpec) -> DeployResult:
        """拉起 workload（幂等：同名先清再起）。返回 :class:`~llmpod.tunnel.schemas.DeployResult`。"""

    @abstractmethod
    async def stop(self, name: str, timeout: int = 10) -> StopResult:
        """停止 workload。返回 :class:`~llmpod.tunnel.schemas.StopResult`。"""

    @abstractmethod
    async def remove(self, name: str, force: bool = False) -> RemoveResult:
        """删除 workload（rollback/恢复对 created 态传 ``force=True``）。返回 :class:`~llmpod.tunnel.schemas.RemoveResult`。"""

    @abstractmethod
    async def list(self, label_filter: list[str] | None = None, all: bool = True) -> list[ContainerSummary]:
        """按标签列 workload（``label_filter`` 为 ``["key=value", ...]``；rollback 必须 ``all=True`）。
        返回 :class:`~llmpod.tunnel.schemas.ContainerSummary` 列表。"""

    @abstractmethod
    async def inspect(self, name: str) -> ContainerState:
        """查 workload State（gate 用）。返回 :class:`~llmpod.tunnel.schemas.ContainerState`（PascalCase，由 docker daemon 决定形状）。"""

    async def logs(self, name: str, tail: int = 200) -> str:
        """取容器日志尾部（引擎崩溃诊断用）。默认空串（runtime 无日志能力时）；docker 覆盖。

        健康检查期容器退出时 rollback 前抓 stderr，避免崩溃原因随 rm 丢失。
        """
        return ""

    # ---- 镜像级操作（PULL_IMAGE 任务用；runtime 无镜像概念时保持 NotImplementedError）----

    async def image_exists(self, image: str) -> bool:
        """镜像是否已在本节点存在（PULL 任务短路用）。默认 False（强制拉取）。"""
        return False

    async def image_download_size(self, image: str) -> int | None:
        """镜像全部 layer 压缩大小之和（字节）；解析不了返回 None（回落按已出现层聚合）。

        进度条真实分母用——daemon 进度流没有总大小字段，layer 逐步入队使聚合分母
        是长大的下界；预读 manifest 让首条进度报文即带真实总量与百分比。
        """
        return None

    async def pull_image(
        self,
        image: str,
        cancel_event: asyncio.Event | None = None,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        """拉取镜像到本节点；失败抛异常，``cancel_event`` 命中抛 ``TaskCancelled``。

        ``on_progress(bytes_done, bytes_total)`` 为可选进度回调（累计字节 + 当前已知
        分母）——runtime 中立：无字节进度能力的实现（如 K8s 只有 Pulling/Pulled 事件）
        不回调，只靠 stage 表达，executor/前端无需改动。百分比与单调守卫由上层
        reporter 统一计算（「预探测总量 vs 已出现层聚合」取大），实现不重复算。

        默认未实现（runtime 无镜像拉取能力）。
        """
        raise NotImplementedError(f"{type(self).__name__}.pull_image not implemented")
