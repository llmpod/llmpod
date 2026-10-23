"""节点服务契约 (IDL) — manager 和 node 共享。

Manager 使用此契约进行类型化代理 (hub.proxy) + 流检测 (class_metadata);
Node 实现此契约并注册到 ServiceRegistry。双方共享同一契约。

注意: 状态上报 (cpu/gpus/containers) 使用 $heartbeat 通知 (Node→Manager),
不使用 RPC 契约。此 IDL 仅包含 Manager→Node 的主动调用 (操作、流式)。

设计原则 (操作原子化): 契约只暴露细粒度 typed 原语,
**绝不**回退到 execute_task/run_task 类 catch-all dispatch。Node 是 runtime 无关的通用
执行器——容器生命周期委托 Node 内的 Deployer(env LLMPOD_DEPLOYER_TYPE 选 docker/k8s);
所有 arch/引擎差异由 Manager 适配器在 spec(DeploySpec)里组装好。

所有入参和出参均使用 Pydantic 强类型 model（``tunnel.schemas``），不走裸 dict。
wire 上以 JSON dict 传输，Pydantic model 做边界校验 + 类型安全。

服务拆分:
- ``ContainerService``: 容器生命周期 (deploy/stop/remove/list/inspect)
- ``NodeService``: 节点级操作 (info/execute_shell/docker_pull/tail_log)

服务方法编写指南 (Node 实现者必读):
- 一元方法 (非流式): 可以写普通 def (框架自动卸载到线程池)
  或 async def (但阻塞 I/O 必须使用异步库或 asyncio.to_thread)。
- 流式方法 (每帧 yield): 必须是 async def + yield (异步生成器),
  内部"等待"部分必须真正 await — 使用 asyncio.subprocess / httpx 等。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from llmpod.tunnel.schemas import (
    Ack,
    ContainerState,
    ContainerSummary,
    DeployResult,
    DeploySpec,
    InstanceStatusPayload,
    NodeReport,
    RemoveResult,
    ShellResult,
    StopResult,
    TaskEventPayload,
    TaskPayload,
    TaskState,
)


class ContainerService(ABC):
    """容器生命周期契约 (IDL)。

    runtime 无关通用执行器,委托 Node 内 Deployer。
    wire 格式: ``ContainerService.{method}``。
    """

    @abstractmethod
    async def deploy(self, spec: DeploySpec) -> DeployResult:
        """拉起 workload (一元,幂等)。

        幂等: 同名先 stop+rm 再 run (per-name asyncio.Lock 防并发 409)。
        **不回传 container_id** (Manager 生成/Node 只执行分离)。

        spec 为 DeploySpec (wire 上 dict,Node 侧 Deployer 归一);字段 (Node 侧映射到
        具体 runtime,非全直传):
          image, entrypoint(list|None), name, args[], env{},
          gpus('"device=2,3"'→device_requests), ports{}, volumes{}, labels{},
          network_mode, ipc(→ipc_mode), shm_size, extra_devices[](→devices),
          runtime, privileged, cap_add[], driver_mounts[](并入 volumes), restart_policy

        返回 :class:`~llmpod.tunnel.schemas.DeployResult`。
        """

    @abstractmethod
    async def stop(self, name: str, timeout: int = 10) -> StopResult:
        """停止 workload (一元)。返回 :class:`~llmpod.tunnel.schemas.StopResult`。"""

    @abstractmethod
    async def remove(self, name: str, force: bool = False) -> RemoveResult:
        """删除 workload (一元)。rollback/恢复对 created/restarting 态传 force=True。
        返回 :class:`~llmpod.tunnel.schemas.RemoveResult`。"""

    @abstractmethod
    async def list(
        self,
        label_filter: list[str] | None = None,
        all: bool = True,
    ) -> list[ContainerSummary]:
        """按标签列出 workload (一元)。

        label_filter 为 ``["key=value", ...]`` 的 list[str] (**非 dict**,dict 语法 SDK 静默返回空)。
        rollback 场景必须 all=True (stop 后 all=False 会漏已 stop 容器→孤儿)。
        返回 :class:`~llmpod.tunnel.schemas.ContainerSummary` 列表。
        """

    @abstractmethod
    async def inspect(self, name: str) -> ContainerState:
        """查 workload State (一元,gate 用)。

        返回 :class:`~llmpod.tunnel.schemas.ContainerState`（PascalCase，由 docker daemon 决定形状）。
        gate: Status=="running" AND Running AND NOT Restarting AND RestartCount==0。
        """

    @abstractmethod
    async def logs(self, name: str, tail: int = 200) -> str:
        """取容器日志尾部（一元，引擎崩溃诊断用）。

        健康检查期容器退出（引擎 ExitCode!=0）时，rollback 删容器前调此抓 stderr，避免崩溃原因
        随 rm 永久丢失。``tail`` 行数上限（默认 200）。返回拼接后的日志字符串。
        """


class NodeService(ABC):
    """节点级操作契约 (IDL): 信息查询 + shell + 流式。

    wire 格式: ``NodeService.{method}``。
    """

    @abstractmethod
    async def info(self) -> NodeReport:
        """返回节点信息 :class:`~llmpod.tunnel.schemas.NodeReport` (供通过 RPC 按需查询)。"""

    @abstractmethod
    async def execute_shell(
        self,
        command: str,
        timeout: int = 60,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        """在节点上执行 shell 命令 (一元 RPC)。

        返回 :class:`~llmpod.tunnel.schemas.ShellResult`。
        """

    @abstractmethod
    async def docker_pull(self, image: str) -> AsyncIterator[str]:
        """流式拉取容器镜像,逐行 yield 进度输出。

        走 docker SDK 经挂载的 docker.sock（生产 node 容器内无 docker CLI）；阻塞迭代器
        需经 asyncio.to_thread 逐块跳线程，避免冻结 worker 事件循环。
        """

    @abstractmethod
    async def image_exists(self, image: str) -> bool:
        """探测镜像是否已在本节点（存在性检查，不触发 pull）。

        PULL_IMAGE 前置检查用：镜像已在位 → manager 直接记 SUCCESS，不建 pull task
        也不下发 docker pull（离线/无 registry 也能复用本地缓存镜像）。

        异常契约：只有「确认不在位」返回 False；探测通道不可用（docker daemon
        抖动/RPC 超时）必须抛异常——调用方（manager SUCCESS 行复核）靠异常区分
        「确不在位」与「探测不了」，塌缩成 False 会把在位镜像误判缺失、触发
        无谓重拉。
        """

    @abstractmethod
    async def tail_log(self, container_name: str, lines: int = 20) -> AsyncIterator[str]:
        """追踪容器日志（时间戳游标轮询，延迟 ≈ 轮询间隔）。

        走 docker SDK 经挂载的 docker.sock（生产 node 容器内无 docker CLI）。按容器名
        轮询 + ``since`` 游标增量：崩溃自启/同名替换对调用方透明；非 running 态节流吐
        ``[llmpod]`` 诊断行；容器持续消失超窗口才结束。阻塞调用经 ``asyncio.to_thread``
        跳线程，不得冻结 worker 事件循环。
        """


class TaskService(ABC):
    """统一任务契约 (IDL)：Manager→Node 声明式任务下发（类型 ``ENSURE_MODEL``）。

    Node 自治执行——下载/重试/续传/取消的运行时生命周期由 Node 持有；Manager 仅下发配方
    + 收集上报（K8s 声明式风格）。``secrets`` 仅内存传递（``ensure_task`` 独立参数），
    **永不**入 ``task.payload`` / ``task_event`` / Node 本地表 / 日志。

    wire 格式：``TaskService.{method}``。
    """

    @abstractmethod
    async def ensure_task(self, task: TaskPayload, secrets: dict | None = None) -> Ack:
        """声明式下发任务（幂等）。

        同一 ``resource_key`` 的重复下发，Node 应识别为同一任务（已存在则不重建、不重置进度）。
        返回 :class:`~llmpod.tunnel.schemas.Ack`。
        """

    @abstractmethod
    async def cancel_task(self, task_id: str) -> Ack:
        """取消任务（Node 停止执行 + 清理 ``.part`` 等中间态，尽力而为）。

        返回 :class:`~llmpod.tunnel.schemas.Ack`。
        """


class ManagerTaskService(ABC):
    """Manager 端任务契约 (IDL)：Node→Manager 状态上报。

    Manager 实现在 $hello 校验通过后按通道注册（绑定鉴权节点身份）；Node 经
    ``call_unary`` 调用。wire 格式：``ManagerTaskService.{method}``。
    """

    @abstractmethod
    async def report_task_event(self, evt: TaskEventPayload) -> Ack:
        """任务状态变更上报（Node→Manager）。驱动 ``task`` / ``task_event`` / ``node_model`` 更新。

        返回 :class:`~llmpod.tunnel.schemas.Ack`。
        """

    @abstractmethod
    async def sync_node_state(self, node_id: str, tasks: list[TaskState]) -> Ack:
        """重连后全量同步 Node 本地任务状态（Manager 对齐 drift：重发遗漏的 cancel）。

        ``tasks`` wire 上为 ``list[dict]``（``list[TaskState]`` 的 JSON 投影）。
        返回 :class:`~llmpod.tunnel.schemas.Ack`。
        """

    @abstractmethod
    async def report_instance_status(self, evt: InstanceStatusPayload) -> Ack:
        """实例长期状态迁移上报（Node→Manager）。驱动 ``instance`` 行更新 + 部署推进。

        Task 结束 ≠ 容器结束——启动/就绪/失败/停止的长期状态经本通道维护。
        返回 :class:`~llmpod.tunnel.schemas.Ack`。
        """
