"""Node→Manager typed caller（封装 ``call_unary`` + channel 离线守卫 + best-effort）。

Manager→Node 方向有 ``hub.proxy()`` 的 typed ``ServiceProxy``；Node→Manager 方向此前只能裸
``client.channel.call_unary("ManagerTaskService", "method", {...})``。本模块补对称的 typed
helper：``ManagerLink``，按 ``ManagerTaskService`` 契约暴露 ``report_task_event`` /
``sync_node_state``。

设计要点：
- channel 在重连间隙为 ``None``（``TunnelClient.channel``，``client.py`` _serve 置、断线清），
  离线时上报 best-effort 丢弃（仅 debug 日志）——不阻塞 executor。
- 远端失败/超时由 ``call_unary`` 抛 ``RemoteError``，捕获后 best-effort 丢弃。
- ``node_id`` 取自 ``client.node_id``（``$register_ack`` 后更新），非 settings 快照。
"""

from __future__ import annotations

import logging

from llmpod.tunnel.schemas import Ack, InstanceStatusPayload, TaskEventPayload, TaskState

log = logging.getLogger("llmpod.apps.node.manager_link")

_SERVICE = "ManagerTaskService"


class ManagerLink:
    """Node→Manager typed 调用封装（持有 ``TunnelClient`` 引用以读 live channel/node_id）。"""

    def __init__(self, client):
        self._client = client

    @property
    def node_id(self) -> str:
        return getattr(self._client, "node_id", "") or ""

    @property
    def _channel(self):
        return getattr(self._client, "channel", None)

    @property
    def online(self) -> bool:
        """channel 是否就绪（用于 agent 决定是否 sync/上报）。"""
        return self._channel is not None

    async def report_task_event(self, evt: TaskEventPayload) -> Ack | None:
        """上报任务事件。离线/失败 best-effort 返回 None（不阻塞 executor）。"""
        ch = self._channel
        if ch is None:
            log.debug("report_task_event: channel 离线，丢弃 task=%s", evt.task_id[:8])
            return None
        try:
            result = await ch.call_unary(_SERVICE, "report_task_event", {"evt": evt.model_dump()})
        except Exception as e:  # RemoteError/超时/断线 一律 best-effort
            log.debug("report_task_event 失败 task=%s: %s", evt.task_id[:8], e)
            return None
        return _to_ack(result)

    async def sync_node_state(self, tasks: list[TaskState]) -> Ack | None:
        """重连后全量同步本地任务状态（``node_id`` 取自 client）。

        tasks 经 JSON 序列化为 wire list[dict]；Manager 端 ``coerce_params`` 按
        ``list[TaskState]`` 注解逐元素 ``model_validate`` 归一（无需手动 dump）。
        """
        ch = self._channel
        if ch is None:
            return None
        try:
            result = await ch.call_unary(
                _SERVICE,
                "sync_node_state",
                {"node_id": self.node_id, "tasks": tasks},
            )
        except Exception as e:
            log.debug("sync_node_state 失败: %s", e)
            return None
        return _to_ack(result)

    async def report_instance_status(self, evt: InstanceStatusPayload) -> Ack | None:
        """上报实例长期状态迁移（启动/就绪/失败/停止）。离线/失败 best-effort 返回 None。

        task_event 是审计权威来源（先报），实例状态是其投影（后报）——两者在 Manager 侧
        都有各自守卫，单通道丢失由下轮 reconcile 收敛。
        """
        ch = self._channel
        if ch is None:
            log.debug("report_instance_status: channel 离线，丢弃 instance=%s", evt.instance_id[:8])
            return None
        try:
            result = await ch.call_unary(_SERVICE, "report_instance_status", {"evt": evt.model_dump()})
        except Exception as e:
            log.debug("report_instance_status 失败 instance=%s: %s", evt.instance_id[:8], e)
            return None
        return _to_ack(result)


def _to_ack(result) -> Ack | None:
    if result is None:
        return None
    if isinstance(result, Ack):
        return result
    try:
        return Ack.model_validate(result)
    except Exception:
        return Ack(ok=True) if result else None
