"""容器生命周期服务实现：委托 Deployer（docker/k8s 可插拔）。

本类只做薄派发 + 日志；同名容器的 deploy 串行化由 Deployer 层的模块级
per-name 锁承担（RPC 路径与执行器直调共用同一把锁），本层不再另建锁表。
"""

from __future__ import annotations

import logging

from llmpod.apps.node.deployers import Deployer
from llmpod.tunnel.contract import ContainerService
from llmpod.tunnel.schemas import (
    ContainerState,
    ContainerSummary,
    DeployResult,
    DeploySpec,
    RemoveResult,
    StopResult,
)

log = logging.getLogger("llmpod.apps.node.service.container")


class ContainerServiceImpl(ContainerService):
    """容器生命周期 RPC 实现（薄派发器 + 注入的 Deployer）。"""

    def __init__(self, deployer: Deployer):
        self._deployer: Deployer = deployer

    async def deploy(self, spec: DeploySpec) -> DeployResult:
        """拉起 workload（幂等：Deployer 层 per-name 锁 + 内部同名先清再起）。不回传 container_id。"""
        log.info("deploy: name=%s image=%s", spec.name, spec.image)
        return await self._deployer.deploy(spec)

    async def stop(self, name: str, timeout: int = 10) -> StopResult:
        log.info("stop: name=%s", name)
        return await self._deployer.stop(name, timeout=timeout)

    async def remove(self, name: str, force: bool = False) -> RemoveResult:
        log.info("remove: name=%s force=%s", name, force)
        return await self._deployer.remove(name, force=force)

    async def list(self, label_filter: list[str] | None = None, all: bool = True) -> list[ContainerSummary]:
        return await self._deployer.list(label_filter=label_filter, all=all)

    async def inspect(self, name: str) -> ContainerState:
        return await self._deployer.inspect(name)

    async def logs(self, name: str, tail: int = 200) -> str:
        """取容器日志尾部（引擎崩溃诊断用）—— rollback 删容器前抓 stderr。"""
        return await self._deployer.logs(name, tail=tail)
