"""STOP_INSTANCE 执行器：只负责停止推理容器，绝不删除。

stop 部署只停不删——容器删除是 delete 部署独有职责，由 ``cleanup_deployment_containers``
（label 扫除）执行，不经本执行器。``stop`` 总是 graceful；NotFound 视为成功（幂等：容器已
不在即目标态）。完成即报 STOPPED + task SUCCESS。

失败语义（保守）：stop/inspect 的 daemon 级故障（超时、APIError）**不等于**容器已停——
容器多半还在运行，若误判成功，Manager 会释放 VRAM 把同卡调度给别人 → 双容器抢卡。
故 daemon 故障报 FAILED（reconcile/重试/label 扫除兜底），仅 ContainerGone（NotFound）
或 inspect 确认非 Running 才判达成。``reconcile``（重启恢复）同口径：异常 → None（等
重新下发），不臆断容器已消失。
"""

from __future__ import annotations

import asyncio
import logging

from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.exceptions import ContainerGone
from llmpod.apps.node.executors.base import Executor, ReconcileResult, ReportFn, parse_payload
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import StopInstancePayload
from llmpod.config.constants import TaskStatus, InstanceStatus

log = logging.getLogger("llmpod.apps.node.executors.stop_instance")


class StopInstanceExecutor(Executor):
    """STOP_INSTANCE 执行器（构造注入 Deployer；纯 stop，不 rm）。"""

    def __init__(self, deployer: Deployer):
        self._deployer = deployer

    async def execute(
        self,
        task: LocalTask,
        secrets: dict | None,
        report: ReportFn,
        cancel_event: asyncio.Event,
    ) -> None:
        payload = parse_payload(task, StopInstancePayload)
        if payload is None:
            await report(TaskStatus.FAILED, message="STOP_INSTANCE 配方无效")
            return
        if not payload.container_name:
            await report(TaskStatus.FAILED, message="STOP_INSTANCE 缺少 container_name")
            return

        # 协作取消契约（与 ensure_model / start_instance 一致）：handle_cancel 只置
        # cancel_event，终态由执行器自查上报。入口命中即取消，不启动停止动作
        if cancel_event.is_set():
            await report(TaskStatus.CANCELLED, message="STOP_INSTANCE 已取消（未执行停止）")
            return

        await report(TaskStatus.RUNNING, instance_status=InstanceStatus.STOPPING)
        try:
            await self._deployer.stop(payload.container_name, timeout=payload.stop_timeout)
        except ContainerGone:
            pass  # 容器已不在：停止目标态达成（幂等成功）
        except Exception as e:
            # daemon 超时/故障 ≠ 容器已停：误判成功会让 Manager 释放 VRAM 后同卡双容器。
            # 报 FAILED 交重试；实例留在 STOPPING，终态由 STOPPED 上报/label 扫除收敛。
            log.warning("stop_instance %s: stop 失败（daemon 故障，容器状态未知）: %s", payload.container_name, e)
            await report(
                TaskStatus.FAILED,
                message=f"容器停止失败（状态未知，待重试收敛）: {e}",
            )
            return
        # stop 返回后再查一次：等待期间到达的取消优先于 SUCCESS（SUCCESS 会打断
        # attempt 失败链，把取消任务误记成功）。容器此刻已停，状态通道仍报 STOPPED
        if cancel_event.is_set():
            await report(
                TaskStatus.CANCELLED,
                message="STOP_INSTANCE 已取消（容器已停止，终态以实例通道为准）",
                instance_status=InstanceStatus.STOPPED,
            )
            return
        await report(TaskStatus.SUCCESS, 100, instance_status=InstanceStatus.STOPPED)

    async def reconcile(self, task: LocalTask) -> ReconcileResult | None:
        """重启探测：容器确认已消失（ContainerGone）或已停（非 Running）= 达成，否则 None。

        inspect 的 daemon 级异常**不**判达成（与 execute 同口径的保守方向）——容器状态
        未知时返回 None 等重新下发，宁可多跑一轮也不误报 STOPPED。
        """
        payload = parse_payload(task, StopInstancePayload)
        if payload is None or not payload.container_name:
            return None
        try:
            state = await self._deployer.inspect(payload.container_name)
        except ContainerGone:
            return ReconcileResult(TaskStatus.SUCCESS, instance_status=InstanceStatus.STOPPED)  # 容器已消失
        except Exception as e:
            log.warning("stop_instance reconcile %s: inspect 失败（不臆断已停）: %s", payload.container_name, e)
            return None
        if not state.Running:
            return ReconcileResult(TaskStatus.SUCCESS, instance_status=InstanceStatus.STOPPED)  # 已停
        return None
