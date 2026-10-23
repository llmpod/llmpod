"""TaskService 实现：声明式任务 RPC 入口（薄委托层，编排交给 NodeAgent）。

wire 格式 ``TaskService.{method}``（ensure_task / cancel_task）。
``secrets`` 经 ``ensure_task`` 独立参数透传给 NodeAgent → executor（仅内存，不落盘）。
升级维护窗口（upgrader busy）拒新任务：抛错走 RPC error → manager 侧按
「下发失败等 redrive」处理（busy ack 的 ok=False 无人消费，静默丢失语义）。
"""

from __future__ import annotations

import logging

from llmpod.apps.node.agent import NodeAgent
from llmpod.tunnel.contract import TaskService
from llmpod.tunnel.schemas import Ack, TaskPayload

log = logging.getLogger("llmpod.apps.node.services.task")


class TaskServiceImpl(TaskService):
    """声明式任务 RPC 实现——全部编排委托 NodeAgent。"""

    def __init__(self, agent: NodeAgent, *, upgrade_busy):
        """:param upgrade_busy: ``() -> bool``——升级维护窗口判定（NodeUpgrader.busy）。
        维护期 sqlite 已关闭/将被新容器独占，新任务落库会失败，拒绝由 manager 重派。"""
        self._agent = agent
        self._upgrade_busy = upgrade_busy

    async def ensure_task(self, task: TaskPayload, secrets: dict | None = None) -> Ack:
        if self._upgrade_busy():
            log.info("ensure_task: 升级维护中，拒绝新任务 task=%s", task.id[:8])
            raise RuntimeError("节点升级维护中，任务被拒绝，等待重派")
        log.info("ensure_task: task=%s type=%s attempt=%s", task.id[:8], task.type, task.attempt)
        return await self._agent.handle_ensure(task, secrets)

    async def cancel_task(self, task_id: str) -> Ack:
        log.info("cancel_task: task=%s", task_id[:8])
        return await self._agent.handle_cancel(task_id)
