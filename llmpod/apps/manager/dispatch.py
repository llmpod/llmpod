"""编排入口：manager 进程所有出站 node RPC 的唯一出口。

webserver 表现层**不应**直接 import ``llmpod.tunnel.*``（传输层）；manager 内部编排模块
（orchestrator/reconciler 等）的 RPC 下发也统一经本门面，不做第二处 ``hub.proxy`` 散调
——出站日志 / 异常归一（NodeOffline→NotFound 等）/ 将来加超时重试指标只改这一处。

返回类型：**wire model 原样返回**（DeployResult/ShellResult/Ack/ContainerSummary...，
即 node 侧契约对象）——manager 内部调用方全程 typed（``c.name`` 而非 ``c.get("name")``）。
tunnel 类型**不越过 webserver 边界**：路由的 ``success()`` 会自动 ``model_dump()``，
webserver 自有 OUT 模型（``apps/webserver/schemas/orchestration.py``）仍从 dict 校验，
分层不受影响。

职责边界：本模块只做「取 hub → proxy → 调 RPC → 透传 wire model / 流迭代器 + 捕 tunnel
异常转领域异常」。请求体校验、HTTP 帧化（NDJSON）仍在 webserver 路由层；容器级业务
编排（label 扫除顺序、重试策略）留在编排模块，只借这里的原子调用。
"""

from __future__ import annotations

import logging

import llmpod
from pydantic import BaseModel, Field

from llmpod.config.settings import settings
from llmpod.core.exceptions import LLMPodError, NotFound
from llmpod.services.node_service import version_status_of
from llmpod.services.version_service import official_image_ref
from llmpod.tunnel.contract import ContainerService, NodeService, TaskService
from llmpod.tunnel.entity.errors import NodeOffline, RemoteError
from llmpod.tunnel.schemas import (
    Ack,
    ContainerSummary,
    DeployResult,
    GpuInfo,
    NodeReport,
    RemoveResult,
    ShellResult,
    StopResult,
    TaskPayload,
)

log = logging.getLogger("llmpod.apps.manager.dispatch")

# image_exists 探测 RPC 的传输层超时（秒）：调用方多在事务内 await，无界挂起会
# 冻住 reconciler 整轮扫描（「WS 活着但不答」的节点）
_IMAGE_PROBE_TIMEOUT_S = 10.0


class NodeSummary(BaseModel):
    """在线节点摘要（hub 内存视图投影，非 DB 行）。

    webserver 侧 ``OnlineNodeOut`` 继承本模型（字段唯一定义处，不再手写镜像）。
    """

    node_id: str = Field(..., description="节点ID（Manager 分配的 UUID）")
    hostname: str = Field("", description="主机名")
    ip: str = Field("", description="节点 IP")
    cpu_arch: str = Field("", description="CPU 架构，如 x86_64 / aarch64")
    online: bool = Field(True, description="是否在线（列表项恒为 True）")
    gpu_count: int = Field(0, description="节点 GPU 数量")
    gpu_archs: list[str] = Field(default_factory=list, description="节点 GPU 软件栈去重列表，如 ['nvidia'] / ['cann']")
    last_heartbeat: float | None = Field(None, description="最近一次心跳时间戳（epoch 秒，未上报则空）")


def node_online(node_id: str) -> bool:
    """节点 WS 是否在线（hub 内存视图；manager 未初始化恒 False——webserver 路由轮询用）。"""
    try:
        hub = _get_hub()
    except LLMPodError:
        return False
    return hub.get(node_id) is not None


async def send_node_upgrade(
    node_id: str, current_version: str | None = None, *, auto: bool = True
) -> tuple[bool, str | None]:
    """校验并向节点发 $node_upgrade 升级提议，返回 ``(dispatched, 未下发原因)``。

    在线检查 + 落后判定 + target/image 推导都在这一处（升级提议唯一策略点）：
    hello 重连补发与 admin 手动端点共用，改策略只改这里。提议语义是 node 自判
    执行（版本达标/升级中会忽略），manager 不落升级状态机；发送失败 best-effort，
    提议丢了由下次重连 hello 兜底（对账模式）。

    ``auto=True``（hello 重连对账等自动路径）受 ``node_auto_upgrade`` 开关闸门：
    未开启只标落后不下发（升级交 admin 手动端点）；``auto=False`` 是 admin 手动
    端点路径，永不拦截——开关关闭时的唯一升级手段。
    """
    if auto and not settings.node_auto_upgrade:
        return False, "节点自动升级未开启"
    if not node_online(node_id):
        return False, "节点不在线"
    if version_status_of(current_version) != "outdated":
        return False, f"节点版本不落后（version={current_version or '未知'}）"
    _, node = _require_node(node_id)
    target_version = llmpod.__version__
    image_ref = official_image_ref()
    log.info("Dispatching $node_upgrade to node=%s target=%s image=%s", node_id, target_version, image_ref)
    try:
        await node.channel.send_notification(
            "$node_upgrade", {"target_version": target_version, "image_ref": image_ref}
        )
    except (NodeOffline, RemoteError) as e:
        return False, f"下发失败: {_wrap_rpc_error(e, node_id)}"
    return True, None


async def send_node_decommission(node_id: str) -> None:
    """向在线节点发 $node_decommission 自清理通知（node 收到后 docker rm -f 自己）。"""
    _, node = _require_node(node_id)
    log.info("Dispatching $node_decommission to node=%s", node_id)
    try:
        await node.channel.send_notification("$node_decommission", {})
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


def _get_hub():
    """延迟获取 manager 端底层 Hub 单例（生命周期启动后才存在）。

    生产环境: node_hub 是 NodeHub,通过 .hub 属性暴露底层 Hub。
    测试环境: node_hub 可能直接是 Hub 实例,此时返回它本身。
    统一返回具备 list/get/proxy 接口的 Hub 对象。
    """
    from llmpod.apps.manager.app import node_hub

    if node_hub is None:
        raise LLMPodError("Manager 未初始化：node_hub 为空")
    return getattr(node_hub, "hub", node_hub)


def _require_node(node_id: str):
    """获取在线节点,不存在则抛 NotFound。"""
    hub = _get_hub()
    node = hub.get(node_id)
    if node is None:
        raise NotFound("Node (online)", node_id)
    return hub, node


def _wrap_rpc_error(e: Exception, node_id: str) -> LLMPodError:
    """将 RPC 层异常转换为 LLMPodError,保留原始信息。"""
    if isinstance(e, NodeOffline):
        return NotFound("Node (online)", node_id)
    if isinstance(e, RemoteError):
        # traceback 仅 DEBUG：node 可能回显请求体，secrets 虽走独立参数，仍避免 WARNING 落库
        log.warning("RPC to node=%s failed: %s", node_id, e.message)
        log.debug("RPC to node=%s traceback:\n%s", node_id, e.tb)
        return LLMPodError(f"节点 RPC 调用失败: {e.message}")
    log.exception("Unexpected RPC error to node=%s", node_id)
    return LLMPodError(f"节点 RPC 调用出现未预期异常: {e!r}")


def count_online_nodes() -> int:
    """在线节点数（hub 内存视图；状态摘要端点用，不做逐节点投影）。"""
    return len(_get_hub().list())


# ---- 在线节点查询 ----


def list_online_nodes() -> list[NodeSummary]:
    """列出在线节点精简摘要（hub 内存视图，非 DB）。

    投影自每个 NodeInfo.info（NodeReport）：node_id / hostname / ip / cpu_arch /
    gpu_count / gpu_archs（GPU 软件栈去重，如 ["nvidia"]）/ last_heartbeat。
    全量系统指标 + 逐卡详情走 :func:`get_node_info`，避免列表载荷过重。
    """
    hub = _get_hub()
    items: list[NodeSummary] = []
    for n in hub.list():
        info = n.info
        gpus = getattr(info, "gpus", None) or []
        # gpu_archs：节点上 GPU 软件栈去重（nvidia/cann/cpu），列表选择节点的判别字段
        archs: list[str] = []
        for g in gpus:
            arch = getattr(g, "arch", None)
            if arch and arch not in archs:
                archs.append(arch)
        items.append(
            NodeSummary(
                node_id=n.node_id,
                hostname=getattr(info, "hostname", "") or "",
                ip=getattr(info, "ip", "") or "",
                cpu_arch=getattr(info, "cpu_arch", "") or "",
                gpu_count=len(gpus),
                gpu_archs=archs,
                last_heartbeat=n.last_heartbeat,
            )
        )
    return items


async def get_node_info(node_id: str) -> NodeReport:
    """通过 RPC 查询节点详细信息（调用 NodeService.info），返回 NodeReport。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, NodeService)
    try:
        return await proxy.info()
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


def get_node_gpus_realtime(node_id: str) -> list[GpuInfo]:
    """读 GpuStatusBuffer 实时 GPU 指标（内存态有 HTTP 出口），返回 GpuInfo 列表。

    注意：DB 的 Gpu 表只存静态资产；实时高频指标走本内存缓冲（心跳镜像）。
    """
    _require_node(node_id)  # 校验节点在线
    from llmpod.apps.manager.app import gpu_buffer

    if gpu_buffer is None:
        return []
    return list(gpu_buffer.get(node_id))


# ---- 部署容器日志 / 启动命令（诊断出口）----


async def instance_start_command(db, deployment_id: str, instance_id: str) -> dict:
    """查某实例实际下发的完整 ``docker run`` 命令（容器诊断出口，无 RPC、纯读库）。

    权威来源是 START_INSTANCE task 行的 ``payload``——下发那一刻写死的 DeploySpec 快照
    （image/gpus/env/挂载/网络/entrypoint/args 全量），不受部署后 catalog/配置漂移影响。
    经 ``docker_run_command`` 渲染为 CLI 形态返回；无 task 行（重启窗口/未下发过）或
    payload 已不可解析（历史形状漂移）一律 ``found=False``，不报错。
    """
    from pydantic import ValidationError
    from sqlalchemy import select

    from llmpod.apps.manager.deployment.task_ops import instance_start_key
    from llmpod.models.instance import Instance
    from llmpod.models.model_deployment import ModelDeployment
    from llmpod.models.task import Task
    from llmpod.tunnel.schemas import StartInstancePayload, docker_run_command

    empty = {
        "found": False,
        "command": None,
        "image": None,
        "container_name": None,
        "task_status": None,
        "attempt": None,
        "dispatched_at": None,
    }

    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        raise NotFound("ModelDeployment", deployment_id)
    irow = await db.get(Instance, instance_id)
    if irow is None or irow.deployment_id != deployment_id:
        raise NotFound("Instance", instance_id)

    # 只取最大 attempt 一行（uq_task_resource_key_attempt 索引直接支撑），不拉全量
    rows = list(
        (
            await db.execute(
                select(Task).where(Task.resource_key == instance_start_key(irow)).order_by(Task.attempt.desc()).limit(1)
            )
        ).scalars()
    )
    if not rows:
        return dict(empty)
    task = rows[0]

    meta = {
        "task_status": task.status,
        "attempt": task.attempt,
        "dispatched_at": task.created_at.isoformat() if task.created_at else None,
    }
    try:
        spec = StartInstancePayload.model_validate(task.payload).spec
    except ValidationError as e:
        # 历史 payload 形状漂移：命令不可信，只回 task 元信息
        log.warning("instance_start_command payload invalid: %s", e)
        return {**empty, **meta}
    return {
        "found": True,
        "command": docker_run_command(spec),
        "image": spec.image,
        "container_name": spec.name,
        **meta,
    }


async def container_logs(
    db,
    deployment_id: str,
    tail: int = 200,
) -> list[dict]:
    """拉某部署各实例容器的日志尾部（引擎崩溃诊断出口）。

    instance 行是容器名/节点的权威来源：按 assigned_node 分组，逐节点取一次
    ContainerService.proxy 后按容器名逐实例调 ``logs``（容器已死也能取——docker
    logs 对 exited 容器仍有效，除非节点已 docker rm）。单实例 RPC 失败只落入该项
    ``error``，不拖垮其余实例的日志收集。
    """
    from sqlalchemy import select

    from llmpod.models.instance import Instance
    from llmpod.models.model_deployment import ModelDeployment
    from llmpod.utils import deployment_container_name

    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        raise NotFound("ModelDeployment", deployment_id)
    insts = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())

    proxies: dict[str, object] = {}
    items: list[dict] = []
    for inst in insts:
        name = deployment_container_name(deployment_id, inst.instance_id)
        item = {
            "instance_id": inst.instance_id,
            "node_id": inst.assigned_node,
            "container_name": name,
            "status": inst.status,
            "logs": None,
            "error": None,
        }
        items.append(item)
        if not inst.assigned_node:
            item["error"] = "instance 未分配节点（调度前/已回收）"
            continue
        try:
            hub, _ = _require_node(inst.assigned_node)
            proxy = proxies.get(inst.assigned_node)
            if proxy is None:
                proxy = hub.proxy(inst.assigned_node, ContainerService)
                proxies[inst.assigned_node] = proxy
            item["logs"] = await proxy.logs(name, tail=tail)
        except Exception as e:  # NodeOffline/RemoteError/NotFound(节点不在线) 统一降级为该项 error
            log.warning("container_logs %s failed: %r", name, e)
            item["error"] = str(e)
    return items


# ---- 任务下发: 一元 ----


async def deploy_container(node_id: str, spec_dict: dict) -> DeployResult:
    """下发容器运行任务（直调 ContainerService.deploy），返回 DeployResult。

    ``spec_dict`` 为 webserver IN 模型 ``model_dump(exclude_none=True)`` 的产物；
    此处 ``DeploySpec.model_validate`` 归一为 wire 契约（顺带边界校验），再透传 Node。
    """
    from llmpod.tunnel.schemas import DeploySpec

    spec = DeploySpec.model_validate(spec_dict)
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, ContainerService)
    log.info("Dispatching deploy to node=%s image=%s name=%s", node_id, spec.image, spec.name)
    try:
        return await proxy.deploy(spec=spec)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


async def execute_shell(
    node_id: str,
    command: str,
    timeout: int = 60,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ShellResult:
    """下发 shell 任务（调用 NodeService.execute_shell），返回 ShellResult。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, NodeService)
    log.info("Dispatching shell to node=%s command=%r", node_id, command)
    try:
        return await proxy.execute_shell(command=command, timeout=timeout, cwd=cwd, env=env)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


# ---- 任务下发: 流式（返回异步迭代器，HTTP 帧化由路由层 _stream_ndjson 完成）----


async def docker_pull_stream(node_id: str, image: str):
    """流式 docker pull，返回逐行 docker 输出的异步迭代器。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, NodeService)
    log.info("Dispatching docker_pull to node=%s image=%s", node_id, image)
    return proxy.docker_pull(image=image)


async def image_exists(node_id: str, image: str) -> bool:
    """探测节点上镜像是否已存在（PULL_IMAGE 前置检查；不在位才建 pull task）。

    超时 10s 在此层收口：调用方（reconciler/scale 的 SUCCESS 行复核）在事务内
    await，节点「WS 活着但不答」时若无界挂起会冻住整轮扫描——超时经隧道转
    RemoteError 上抛，调用方按通道不可用保守处理。
    """
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, NodeService)
    return await proxy.image_exists(image=image, _rpc_timeout=_IMAGE_PROBE_TIMEOUT_S)


async def tail_log_stream(node_id: str, container_name: str, lines: int = 20):
    """流式追踪容器日志，返回异步迭代器。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, NodeService)
    log.info("Dispatching tail_log to node=%s container=%s", node_id, container_name)
    return proxy.tail_log(container_name=container_name, lines=lines)


# ---- 统一任务下发 (TaskService)：声明式模型分发 ENSURE_MODEL ----


async def ensure_task(node_id: str, task: TaskPayload, secrets: dict | None = None) -> Ack:
    """下发声明式任务（``TaskService.ensure_task``），返回 Ack。

    ``task`` 为 wire ``TaskPayload``（manager 内部构造方为 ``task_ops.task_to_payload``
    / ``PlacementService``，全程 typed）；``secrets`` 独立参数，仅内存下发（HF token 等），
    不入库/日志。节点离线/RPC 失败抛 ``LLMPodError``/``NotFound``，由调用方
    （PlacementService）决定是否吞掉（best-effort 下发）。
    """
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, TaskService)
    log.info(
        "Dispatching ensure_task to node=%s task=%s type=%s",
        node_id,
        task.id[:8],
        task.type,
    )
    try:
        return await proxy.ensure_task(task=task, secrets=secrets)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


async def cancel_task(node_id: str, task_id: str) -> Ack:
    """取消节点上的任务（``TaskService.cancel_task``），返回 Ack。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, TaskService)
    log.info("Dispatching cancel_task to node=%s task=%s", node_id, task_id[:8])
    try:
        return await proxy.cancel_task(task_id=task_id)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


# ---- 容器生命周期原子操作（ContainerService；编排模块的清容器/替换走这里）----


async def list_containers(
    node_id: str, label_filter: list[str] | None = None, all: bool = True
) -> list[ContainerSummary]:
    """按标签列出节点上的容器（``ContainerService.list``），返回 ContainerSummary 列表。

    ``label_filter`` 为 ``["key=value", ...]``；rollback/清理场景须 ``all=True``
    （stop 后 all=False 会漏已停容器 → 孤儿）。
    """
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, ContainerService)
    try:
        return await proxy.list(label_filter=label_filter, all=all)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


async def stop_container(node_id: str, name: str, timeout: int = 10) -> StopResult:
    """停止单个容器（``ContainerService.stop``，纯停不删），返回 StopResult。"""
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, ContainerService)
    try:
        return await proxy.stop(name=name, timeout=timeout)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)


async def remove_container(node_id: str, name: str, force: bool = False) -> RemoveResult:
    """删除单个容器（``ContainerService.remove``），返回 RemoveResult。

    对 created/restarting 态容器传 ``force=True``（否则 daemon 拒删）。
    """
    hub, _ = _require_node(node_id)
    proxy = hub.proxy(node_id, ContainerService)
    try:
        return await proxy.remove(name=name, force=force)
    except (NodeOffline, RemoteError) as e:
        raise _wrap_rpc_error(e, node_id)
