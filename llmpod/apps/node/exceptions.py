"""Node app 专属异常：节点侧任务执行、连接相关错误。

通用领域异常见 llmpod.core.exceptions；这里只放 Node 进程特有的异常分类。
"""

from __future__ import annotations

from llmpod.core.exceptions import LLMPodError


class NodeError(LLMPodError):
    """Node 侧错误的基类。"""


class NodeConnectionError(NodeError):
    """节点连接错误（与 Manager 通信失败、离线、握手超时等）。"""


class TaskExecutionError(NodeError):
    """节点侧任务执行失败（docker SDK 操作、shell 执行、推理部署等）。"""


class ContainerGone(NodeError):
    """目标容器已不存在（docker NotFound 语义）。

    与 TaskExecutionError 分开：调用方须区分「容器不在 = 目标态可能已达成」与
    「daemon 故障 = 状态未知」——停止/收敛路径对前者可判成功，对后者必须保守。
    """


class TaskCancelled(NodeError):
    """节点侧任务被取消（长时操作如镜像拉取检测 cancel 后停止中间进程并抛此异常）。"""
