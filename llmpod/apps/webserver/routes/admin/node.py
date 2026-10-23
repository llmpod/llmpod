"""节点管理路由。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager import dispatch
from llmpod.apps.webserver.app import get_settings
from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.utils.bootstrap_params import BootstrapNodeParams
from llmpod.apps.webserver.utils.bootstrap_token import mint_bootstrap_token, bootstrap_signing_key
from llmpod.apps.webserver.schemas import Result, success
from llmpod.core.advertise import manager_http_origin
from llmpod.core.exceptions import ConfigError, LLMPodValidationError, NotFound
from llmpod.db import get_db
from llmpod.models.node import Node
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import NodeDetailOut
from llmpod.config.constants import NodeStatus
from llmpod.permission.permissions import NODE_BOOTSTRAP, NODE_DELETE, NODE_READ, NODE_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.node_service import (
    decommission_node,
    enrich_nodes,
    enrich_nodes_runtime,
    enrich_nodes_version_status,
    validate_cluster,
)

# 节点状态合法值（与 Node ORM 的 ck_node_status 约束一致）
_NODE_STATUSES = set(NodeStatus)

log = logging.getLogger("llmpod.apps.webserver.routes.admin.node")

router = APIRouter()


@router.get(
    "",
    response_model=PaginatedResponse[NodeDetailOut],
    summary="列出节点",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def list_nodes(
    cluster_id: int | None = Query(None, description="集群ID"),
    status: str | None = Query(None, description="节点状态"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    """列出所有节点，支持按条件过滤。"""
    stmt = select(Node)
    count_stmt = select(func.count(Node.id))

    if cluster_id:
        stmt = stmt.where(Node.cluster_id == cluster_id)
        count_stmt = count_stmt.where(Node.cluster_id == cluster_id)
    if status:
        stmt = stmt.where(Node.status == status)
        count_stmt = count_stmt.where(Node.status == status)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.order_by(Node.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    nodes = list(result.scalars())
    # 批量装配 cluster_name（一条 IN 查询，零 N+1），供前端表直接显示所属集群
    nodes = await enrich_nodes(db, nodes)
    # 装配实时资源使用摘要（读 Manager 内存缓冲，离线节点 runtime=None）
    nodes = enrich_nodes_runtime(nodes)
    # 装配版本比对结果（ok|outdated|newer|unknown）
    nodes = enrich_nodes_version_status(nodes)

    return PaginatedResponse.result(items=nodes, total=total, page=page, page_size=page_size)


@router.get(
    "/{node_id}",
    response_model=Result[NodeDetailOut],
    summary="获取节点详情",
    dependencies=[Depends(require_permission(NODE_READ))],
)
async def get_node(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取节点。"""
    stmt = select(Node).where(Node.id == node_id)
    result = await db.execute(stmt)
    node = result.scalar_one_or_none()
    if not node:
        raise NotFound("Node", node_id)
    await enrich_nodes(db, [node])
    enrich_nodes_runtime([node])
    enrich_nodes_version_status([node])
    return success(node)


class UpdateNodeRequest(BaseModel):
    remark: str | None = Field(None, description="节点备注；不传则不修改")
    status: str | None = Field(
        None, description="节点状态：online | offline | maintenance | busy | drained；不传则不修改"
    )
    allowed_capabilities: list[str] | None = Field(
        None, description="允许该节点使用的能力标签全集（全量替换）；不传则不修改"
    )


class NodeDeleteData(BaseModel):
    """注销节点回执（级联删该节点 GPU 记录；在线时已通知自清理）。"""

    id: str = Field(..., description="被注销节点ID（Manager 分配的 UUID）")
    deleted: bool = Field(True, description="删除是否生效")
    notified: bool = Field(False, description="是否向在线节点下发过自清理通知（$node_decommission）")
    cleaned: bool = Field(False, description="节点是否确认自清理（WS 已断开）；离线节点/超时为 False")


class NodeUpgradeItem(BaseModel):
    """升级提议回执（单节点 / 批量逐节点共用形态）。"""

    id: str = Field(..., description="节点ID")
    dispatched: bool = Field(..., description="升级提议是否已下发（$node_upgrade）")
    reason: str | None = Field(None, description="未下发原因（如节点不在线、版本不落后、下发失败）")


class NodeUpgradeBatchRequest(BaseModel):
    node_ids: list[str] | None = Field(None, description="目标节点ID列表；缺省=全部在线落后节点（显式空列表会被拒绝）")


class NodeUpgradeBatchData(BaseModel):
    """批量升级回执（admin 收敛视图：列表实时看 outdated → ok 追平）。"""

    dispatched_count: int = Field(..., description="成功下发提议的节点数")
    items: list[NodeUpgradeItem] = Field(default_factory=list, description="逐节点下发结果")


@router.post(
    "/{node_id}/update",
    response_model=Result[NodeDetailOut],
    summary="更新节点",
    dependencies=[Depends(require_permission(NODE_UPDATE))],
)
@audit_action(action="update", resource="node", resource_id="node_id")
async def update_node(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    request: UpdateNodeRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新节点运维字段（remark / status / allowed_capabilities）。

    节点的 hostname / ip / cpu 等硬件信息由节点注册时上报、不允许在此手改；
    本接口仅暴露运维可调字段（备注、状态调度、能力白名单）。
    """
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise LLMPodValidationError("未提供任何待更新字段")

    if "status" in updates and updates["status"] not in _NODE_STATUSES:
        raise LLMPodValidationError(f"非法节点状态：{updates['status']}")

    stmt = select(Node).where(Node.id == node_id)
    node = (await db.execute(stmt)).scalar_one_or_none()
    if not node:
        raise NotFound("Node", node_id)

    # 逐字段显式赋值（model_dump(exclude_none) 已滤 None；新字段加这里，不走键名循环）
    if "remark" in updates:
        node.remark = updates["remark"]
    if "status" in updates:
        node.status = updates["status"]
    if "allowed_capabilities" in updates:
        node.allowed_capabilities = updates["allowed_capabilities"]
    await db.flush()
    # flush 不带回 onupdate 生成的 updated_at，序列化前同步 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(node)
    await db.commit()
    await enrich_nodes(db, [node])
    enrich_nodes_runtime([node])
    return success(node)


async def _try_dispatch_upgrade(node: Node) -> tuple[bool, str | None]:
    """单节点升级提议（校验与推导在 dispatch.send_node_upgrade）；批量路径逐节点隔离异常。

    ``auto=False``：手动端点不受 node_auto_upgrade 开关限制——开关关闭时
    admin 手动触发是唯一升级手段。
    """
    try:
        return await dispatch.send_node_upgrade(node.id, current_version=node.version, auto=False)
    except NotFound:
        # 检查在线与真正发送之间节点掉线的窄竞态
        return False, "节点不在线"
    except Exception as e:
        return False, f"下发失败: {e}"


@router.post(
    "/{node_id}/upgrade",
    response_model=Result[NodeUpgradeItem],
    summary="触发节点升级",
    dependencies=[Depends(require_permission(NODE_UPDATE))],
)
@audit_action(action="upgrade", resource="node", resource_id="node_id")
async def upgrade_node(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """向在线落后节点下发 $node_upgrade 升级提议（自动跟随失败时的人工补手段）。

    提议即返回，不等待升级完成；结果看节点列表 version_status 追平 + 审计日志。
    """
    stmt = select(Node).where(Node.id == node_id)
    node = (await db.execute(stmt)).scalar_one_or_none()
    if not node:
        raise NotFound("Node", node_id)
    dispatched, reason = await _try_dispatch_upgrade(node)
    return success(NodeUpgradeItem(id=node_id, dispatched=dispatched, reason=reason))


@router.post(
    "/upgrade-batch",
    response_model=Result[NodeUpgradeBatchData],
    summary="批量触发节点升级",
    dependencies=[Depends(require_permission(NODE_UPDATE))],
)
@audit_action(action="upgrade_batch", resource="node")
async def upgrade_nodes_batch(
    request: NodeUpgradeBatchRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """批量下发升级提议（缺省=全部在线落后节点；显式空列表拒绝）。

    逐节点独立校验与下发、互不影响（gather 并行）；响应逐节点 ``{dispatched, reason}``。
    """
    if request.node_ids is None:
        nodes = list((await db.execute(select(Node))).scalars())
    elif not request.node_ids:
        # 显式空列表≠缺省：把「没勾选」传成 [] 的调用方不该被解释成全集群升级
        raise LLMPodValidationError("请指定要升级的节点；如需升级所有在线的旧版本节点，请不要传 node_ids")
    else:
        stmt = select(Node).where(Node.id.in_(request.node_ids))
        nodes = list((await db.execute(stmt)).scalars())
        found = {n.id for n in nodes}
        missing = [nid for nid in request.node_ids if nid not in found]
        if missing:
            raise NotFound("Node", ",".join(missing))

    dispatch_results = await asyncio.gather(*(_try_dispatch_upgrade(n) for n in nodes))
    items = []
    for node, result in zip(nodes, dispatch_results):
        dispatched, reason = result
        items.append(NodeUpgradeItem(id=node.id, dispatched=dispatched, reason=reason))
    return success(
        NodeUpgradeBatchData(
            dispatched_count=sum(1 for i in items if i.dispatched),
            items=items,
        )
    )


@router.post(
    "/{node_id}/delete",
    response_model=Result[NodeDeleteData],
    summary="注销节点",
    dependencies=[Depends(require_permission(NODE_DELETE))],
)
@audit_action(action="delete", resource="node", resource_id="node_id")
async def delete_node(
    node_id: Annotated[str, Path(description="节点ID（由 Manager 分配的 UUID）")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """注销节点（通知在线节点自清理，然后级联删除节点/GPU 记录）。

    守卫与编排见 ``services.node_service.decommission_node``：节点带活跃负载时
    硬禁删；在线节点发 ``$node_decommission``（node 侧 rm -f 自己 + 删本节点
    推理容器 + 回收具名数据卷）等 WS 断开（≤10s）；写 Redis 注销标记拦残活容器
    重连。挂载进来的宿主路径（模型目录、extra_mounts）不连带删。
    """
    result = await decommission_node(node_id, db)
    return success(NodeDeleteData(id=node_id, notified=result.notified, cleaned=result.cleaned))


# ===== 节点引导令牌（短期 HMAC）=====
class BootstrapTokenRequest(BaseModel):
    cluster_id: int = Field(..., description="集群ID")
    ttl: int | None = Field(None, description="令牌有效期（秒）")
    node_ip: str | None = Field(None, description="节点互联 IP；无终端安装时必填，交互安装时可在脚本中选择")
    container_name: str | None = Field(None, description="节点容器名，默认 llmpod-node")
    data_volume: str | None = Field(None, description="节点数据卷，可填卷名或宿主机绝对路径；默认 llmpod-node-data")
    model_host_dir: str | None = Field(
        None,
        description="宿主机模型目录；留空使用管理端配置的路径，自定义目录需已存在",
    )
    extra_mounts: list[str] | None = Field(None, description="额外挂载列表，每项格式为 宿主路径或卷名:容器路径[:mode]")
    image: str | None = Field(None, description="节点镜像，默认使用管理端当前版本")


class BootstrapTokenData(BaseModel):
    cluster_id: int | None = Field(None, description="集群ID")
    token: str | None = Field(None, description="节点引导令牌（短期 HMAC，粘贴进 curl 用）")
    expires_at: str | None = Field(None, description="Token过期时间")
    command: str | None = Field(None, description="执行命令")


@router.post(
    "/bootstrap-token",
    response_model=Result[BootstrapTokenData],
    summary="签发节点引导令牌",
    dependencies=[Depends(require_permission(NODE_BOOTSTRAP))],
)
@audit_action(action="bootstrap", resource="node")
async def mint_bootstrap_token_endpoint(
    request: BootstrapTokenRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """签发短期节点引导令牌（登录态调用）。

    管理员在「添加节点」时调用，得到 ``token`` + 可直接粘贴的 ``curl_command``；按 ttl 过期（默认 8h、上限 24h）
    """
    settings = get_settings()

    # 1. 校验集群存在且活跃
    if not await validate_cluster(request.cluster_id, db):
        raise NotFound("Cluster", request.cluster_id)

    # 引导凭据由部署密钥派生的独立签名域签发。
    ttl = request.ttl if request.ttl is not None else settings.bootstrap_token_ttl
    ttl = max(60, min(int(ttl), settings.bootstrap_token_max_ttl))

    # 引导参数只作为目标机安装时的默认值。
    params = BootstrapNodeParams.from_raw(
        node_ip=request.node_ip,
        container_name=request.container_name,
        data_volume=request.data_volume,
        model_host_dir=request.model_host_dir,
        extra_mounts=request.extra_mounts,
        image=request.image,
    )
    token, exp = mint_bootstrap_token(bootstrap_signing_key(settings), request.cluster_id, ttl)

    # 6. 构造可直接粘贴的 curl 命令（safe 集合保住 / 与 : 原样可读；不含 +，
    #    避免 + 落进 URL 后被 query 解码成空格；仅非默认参数进 URL，不传参数时
    #    与旧格式一致）。origin 取 manager 对外地址的 http 侧。
    #    外层 bash -c + pipefail：裸 `curl -fsSL ... | bash` 在 curl 失败（令牌过期/
    #    manager 不可达）时 -f 吞掉响应体、bash 收到空输入静默 exit 0——装机端
    #    看到的是「秒完成、什么都没发生」；包一层后 curl 的错误可见、退出码非零。
    #    URL 必须单引号包裹（管理端地址混入 $ ` " 等字符时不发生展开），单引号
    #    在外层单引号串里用 '"'"' 形态拼接；字符集闸是第二道保险——超集字符
    #    直接拒绝签发，不产出会炸的命令。
    origin = manager_http_origin()
    query = [("cluster_id", str(request.cluster_id)), ("token", token)]
    query += params.query_items(settings.model_store_dir)
    qs = urlencode(query, doseq=True, safe=":/,@-")
    url = f"{origin}/api/v1/nodes/bootstrap?{qs}"
    if not re.fullmatch(r"https?://[A-Za-z0-9._:~/@,\[\]?=&%-]+", url):
        raise ConfigError(f"manager 对外地址含非常规字符，拒绝生成引导命令: {url!r}")
    q = "'\"'\"'"
    curl_command = f"bash -c 'set -eo pipefail; curl -fsSL {q}{url}{q} | bash'"

    return success(
        BootstrapTokenData(
            cluster_id=request.cluster_id,
            token=token,
            expires_at=datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
            command=curl_command,
        )
    )
