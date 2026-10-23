"""编排 API 路由：manager → node 任务下发链路 (webserver 侧 HTTP 入口)。

此路由属于 webserver 表现层，**不直接 import ``llmpod.tunnel.*``**。
tunnel 的契约代理 / 异常封装收敛在 manager 侧门面 ``apps/manager/dispatch.py``，
本路由只负责：HTTP 参数校验 → 调门面 → 统一 ``Result`` 封装 / NDJSON 帧化。

链路: HTTP API → webserver 路由 → manager dispatch 门面 → node_hub.proxy → RPC → Node。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse

from llmpod.apps.manager import dispatch
from llmpod.apps.webserver.auth.deps import require_permission, get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.services.audit_service import get_audit_ip, write_audit
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.orchestration import (
    ContainerRunRequest,
    ContainerRunResultOut,
    DockerPullRequest,
    GpuMetricOut,
    NodeInfoOut,
    OnlineNodeOut,
    OrchestrationStatusOut,
    ShellRequest,
    ShellResultOut,
    TailLogRequest,
)
from llmpod.permission.permissions import NODE_EXEC, NODE_READ, NODE_UPDATE

log = logging.getLogger("llmpod.apps.webserver.routes.admin.orchestration")

router = APIRouter()


# ---- 节点查询 ----


@router.get(
    "/status",
    response_model=Result[OrchestrationStatusOut],
    summary="获取编排引擎状态",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def orchestration_status():
    """编排引擎状态。"""
    return success(
        OrchestrationStatusOut(
            status="running",
            version="1.0.0",
            features=["node_management", "gpu_monitoring", "task_dispatch"],
            online_nodes=dispatch.count_online_nodes(),
        )
    )


@router.get(
    "/nodes",
    response_model=Result[list[OnlineNodeOut]],
    summary="列出在线节点",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def list_online_nodes():
    """列出当前在线节点 (来自 node_hub 内存,非 DB；精简摘要，详情走 /info)。"""
    return success(dispatch.list_online_nodes())


@router.get(
    "/nodes/{node_id}/info",
    response_model=Result[NodeInfoOut],
    summary="获取节点详细信息",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def get_node_info(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
):
    """通过 RPC 查询节点详细信息 (调用 NodeService.info)。"""
    return success(await dispatch.get_node_info(node_id))


@router.get(
    "/nodes/{node_id}/gpus",
    response_model=Result[list[GpuMetricOut]],
    summary="节点 GPU 实时指标",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def get_node_gpus_realtime(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
):
    """读 GpuStatusBuffer 实时 GPU 指标（used_vram/util/温度/功耗，内存态有 HTTP 出口）。

    注意：DB 的 Gpu 表只存静态资产；实时高频指标走本内存缓冲（心跳镜像）。
    """
    return success(dispatch.get_node_gpus_realtime(node_id))


# ---- 任务下发: 一元 ----


@router.post(
    "/nodes/{node_id}/tasks/docker-run",
    response_model=Result[ContainerRunResultOut],
    summary="运行Docker容器",
    dependencies=[Depends(require_permission(NODE_EXEC))],
)
async def deploy(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    request: ContainerRunRequest,
    user: SessionData = Depends(get_current_user),
):
    """下发容器运行任务 (直调 NodeService.deploy(spec)，typed 契约；URL 沿用 /tasks/docker-run)。"""
    audit_detail = {
        "operation": "docker-run",
        "image": request.image,
        "container_name": request.name,
        "args": request.args,
    }
    try:
        result = await dispatch.deploy_container(node_id, request.model_dump(exclude_none=True))
    except Exception as exc:
        await write_audit(
            action="execute",
            resource_type="node",
            resource_id=node_id,
            actor_id=user.user_id,
            actor_name=user.username,
            tenant_id=user.tenant_id,
            result="failed",
            detail={**audit_detail, "error_type": type(exc).__name__},
            ip_address=get_audit_ip(),
        )
        raise
    await write_audit(
        action="execute",
        resource_type="node",
        resource_id=node_id,
        actor_id=user.user_id,
        actor_name=user.username,
        tenant_id=user.tenant_id,
        result="success",
        detail=audit_detail,
        ip_address=get_audit_ip(),
    )
    return success(result)


@router.post(
    "/nodes/{node_id}/tasks/shell",
    response_model=Result[ShellResultOut],
    summary="执行Shell命令",
    dependencies=[Depends(require_permission(NODE_EXEC))],
)
async def execute_shell(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    request: ShellRequest,
    user: SessionData = Depends(get_current_user),
):
    """下发 shell 任务 (调用 NodeService.execute_shell 独立 RPC)。"""
    audit_detail = {
        "operation": "shell",
        "command": request.command,
        "timeout": request.timeout,
        "cwd": request.cwd,
    }
    try:
        result = await dispatch.execute_shell(
            node_id,
            request.command,
            request.timeout,
            request.cwd,
            request.env,
        )
    except Exception as exc:
        await write_audit(
            action="execute",
            resource_type="node",
            resource_id=node_id,
            actor_id=user.user_id,
            actor_name=user.username,
            tenant_id=user.tenant_id,
            result="failed",
            detail={**audit_detail, "error_type": type(exc).__name__},
            ip_address=get_audit_ip(),
        )
        raise
    await write_audit(
        action="execute",
        resource_type="node",
        resource_id=node_id,
        actor_id=user.user_id,
        actor_name=user.username,
        tenant_id=user.tenant_id,
        result="success",
        detail=audit_detail,
        ip_address=get_audit_ip(),
    )
    return success(result)


# ---- 任务下发: 流式 (NDJSON) ----


_NDJSON_HEARTBEAT_S = 10  # 静默心跳间隔：无数据也要定期 send，断开才能及时暴露


async def _stream_ndjson(agen):
    """把异步生成器的每条字符串帧编码为 NDJSON 行 (一行一个 JSON 对象)。

    静默超 ``_NDJSON_HEARTBEAT_S`` 发 ``{"event":"ping"}`` 心跳帧（前端忽略未知
    event 即可）：starlette 走 ASGI ≥2.4 快路径时**只在 send 时才能感知客户端
    断开**（不再监听 http.disconnect）——安静流（引擎编译期几分钟无日志）若无
    心跳，断开后生成器会一路空转到复活窗口超时，白占 node 轮询。

    实现注意：**不能用 ``wait_for(anext)``**——超时会 cancel 注入底层生成器，
    把还在正常轮询的 tail_log 直接杀死。须用 ``asyncio.wait(timeout=)``（不取消
    pending），下一个数据帧继续等待同一个挂起的 ``__anext__`` 任务；消费者断开
    （生成器被关闭）时 finally 主动 cancel 该任务，取消沿 anext 传播进底层生成器
    → 隧道 $cancel → node 停轮询——取消传播因此成为确定路径，不依赖 GC 时机。
    """
    it = agen.__aiter__()
    pending_next: asyncio.Task | None = None
    try:
        while True:
            if pending_next is None:
                pending_next = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending_next}, timeout=_NDJSON_HEARTBEAT_S)
            if not done:
                yield json.dumps({"event": "ping"}, ensure_ascii=False) + "\n"
                continue  # 同一个 anext 任务继续挂起等数据（不 cancel 生成器）
            try:
                line = pending_next.result()
            except StopAsyncIteration:
                break
            pending_next = None
            yield json.dumps({"event": "data", "data": line}, ensure_ascii=False) + "\n"
    except Exception as e:
        yield json.dumps({"event": "error", "message": str(e)}, ensure_ascii=False) + "\n"
        return
    finally:
        if pending_next is not None:
            pending_next.cancel()  # 断开/异常时取消 anext → 取消注入底层生成器
    yield json.dumps({"event": "end"}, ensure_ascii=False) + "\n"


@router.post(
    "/nodes/{node_id}/tasks/docker-pull",
    summary="拉取Docker镜像",
    dependencies=[Depends(require_permission(NODE_EXEC))],
)
async def docker_pull(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    request: DockerPullRequest,
    user: SessionData = Depends(get_current_user),
):
    """流式 docker pull - 逐行返回 docker 输出 (NDJSON 流)。

    响应格式: 每行一个 JSON 对象 {event: data|error|end, ...}。
    """
    # 流式端点在拿到 stream 前只可能因「节点不在线/RPC 失败」抛错，此时与
    # shell/docker-run 同口径落一条失败审计；拉取过程本身的成败由流内事件
    # 承载，不在此落账。
    try:
        stream = await dispatch.docker_pull_stream(node_id, request.image)
    except Exception as exc:
        await write_audit(
            action="execute",
            resource_type="node",
            resource_id=node_id,
            actor_id=user.user_id,
            actor_name=user.username,
            tenant_id=user.tenant_id,
            result="failed",
            detail={"operation": "docker-pull", "image": request.image, "error_type": type(exc).__name__},
            ip_address=get_audit_ip(),
        )
        raise
    await write_audit(
        action="execute",
        resource_type="node",
        resource_id=node_id,
        actor_id=user.user_id,
        actor_name=user.username,
        tenant_id=user.tenant_id,
        result="success",
        detail={"operation": "docker-pull", "image": request.image},
        ip_address=get_audit_ip(),
    )

    return StreamingResponse(
        _stream_ndjson(stream),
        media_type="application/x-ndjson",
        headers={"X-Node-Id": node_id, "X-Image": request.image},
    )


@router.post(
    "/nodes/{node_id}/tasks/tail-log", summary="追踪容器日志", dependencies=[Depends(require_permission(NODE_UPDATE))]
)
async def tail_log(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    request: TailLogRequest,
):
    """流式追踪容器日志 (NDJSON)。"""
    stream = await dispatch.tail_log_stream(node_id, request.container_name, request.lines)

    return StreamingResponse(
        _stream_ndjson(stream),
        media_type="application/x-ndjson",
        headers={"X-Node-Id": node_id, "X-Container": request.container_name},
    )
