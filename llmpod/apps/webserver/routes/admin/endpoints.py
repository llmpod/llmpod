"""管理员来源端点管理路由（外部 API 登记/维护）。

仅 GET/POST：写操作一律 POST 动作路径（/{id}/update、/{id}/delete），
不使用 PATCH/PUT/DELETE。

路由清单：``POST /admin/endpoints`` 登记（收模型数组，单条录入即长度 1；验证两层
分工见 ``post_endpoint`` 文档）、``GET`` 列表、``/models`` 拉上游清单（多选流程的
发现出口）、``/test`` 连通性诊断（探测方法的诊断出口，供前端「测一下」与运维
排查）、``/{id}/update``、``/{id}/delete``。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.out_models import EndpointProbeOut, ModelEndpointOut
from llmpod.apps.webserver.services.endpoint_probe import (
    ProbeTarget,
    ensure_models_present,
    ensure_reachable,
    fetch_upstream_models,
    merge_target,
    probe_applies,
    probe_endpoint,
)
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.db import get_db
from llmpod.models import AuditLog, ModelEndpoint
from llmpod.permission.permissions import ENDPOINT_READ, ENDPOINT_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.endpoint_service import (
    create_endpoint,
    delete_endpoint,
    find_external_endpoints_by_models,
    get_endpoint,
    list_endpoints,
    update_endpoint,
)

log = logging.getLogger("llmpod.apps.webserver.admin.endpoints")

router = APIRouter()


class CreateEndpointItem(BaseModel):
    """登记的单个模型项（共享请求顶层的连接配置）。"""

    served_model: str = Field(..., min_length=1, max_length=255, description="上游模型名（如 gpt-4o）")
    name: str | None = Field(None, min_length=1, max_length=255, description="端点展示名；缺省=served_model")
    operations: list[str] | None = Field(None, description="支持的操作（可空）")


class CreateEndpointRequest(BaseModel):
    """登记入参：共享一份连接配置，收模型数组（1..100；单条录入即长度 1 的数组）。"""

    protocol: str = Field(..., pattern="^(openai|anthropic)$", description="协议：openai | anthropic")
    api_base: str = Field(..., min_length=1, max_length=512, description="转发基址（如 https://api.openai.com/v1）")
    api_key: str | None = Field(None, max_length=255, description="外部 API Key")
    provider_name: str | None = Field(None, max_length=100, description="供应商名（前端分组）")
    path: str | None = Field(
        None,
        max_length=255,
        description="协议端点子路径（不含版本根 /v1；可空，缺省按协议约定；api_base 需携带版本根）",
    )
    models: list[CreateEndpointItem] = Field(..., min_length=1, max_length=100, description="要登记的模型（1..100）")
    skip_probe: bool = Field(False, description="跳过连通性探测与成员资格校验，直接登记")


class UpdateEndpointRequest(BaseModel):
    api_base: str | None = Field(None, min_length=1, max_length=512, description="不传则不改")
    served_model: str | None = Field(None, min_length=1, max_length=255, description="不传则不改")
    name: str | None = Field(None, min_length=1, max_length=255, description="不传则不改")
    api_key: str | None = Field(None, max_length=255, description="不传则不改")
    provider_name: str | None = Field(None, max_length=100, description="不传则不改")
    path: str | None = Field(
        None,
        max_length=255,
        description="协议端点子路径（不含版本根 /v1）；不传则不改",
    )
    enabled: bool | None = Field(None, description="启用开关，不传则不改")
    health_status: str | None = Field(None, pattern="^(healthy|degraded|down)$", description="健康状态")
    skip_probe: bool = Field(False, description="连接配置变更时跳过连通性探测")


class TestEndpointRequest(BaseModel):
    """探测入参：任一项不传就回落 ``endpoint_id`` 那行的存储值（编辑弹窗不回传已存 Key）。"""

    endpoint_id: int | None = Field(None, description="已登记端点 ID；不传则全部字段由本次入参给出")
    protocol: str | None = Field(None, pattern="^(openai|anthropic)$", description="协议；不传则用已存行的")
    api_base: str | None = Field(None, min_length=1, max_length=512, description="转发基址；不传则用已存行的")
    served_model: str | None = Field(None, min_length=1, max_length=255, description="上游模型名；不传则用已存行的")
    api_key: str | None = Field(None, max_length=255, description="外部 API Key；不传则用已存行的")
    path: str | None = Field(None, max_length=255, description="协议端点子路径；不传则用已存行的")


class FetchModelsRequest(BaseModel):
    """拉取上游模型清单入参：登记前的发现动作，全部字段由本次入参给出。

    不收 ``path``：它是 chat 主路径，与清单地址（固定 ``{api_base}/models``）无关。
    """

    protocol: str = Field(..., pattern="^(openai|anthropic)$", description="协议：openai | anthropic")
    api_base: str = Field(..., min_length=1, max_length=512, description="转发基址（如 https://api.openai.com/v1）")
    api_key: str | None = Field(None, max_length=255, description="外部 API Key")


def _created_ids_snapshot(ids: list[int]) -> str:
    """新建端点的 resource_id 快照：id 逗号串，超列宽时收成「前几个…共N个」。

    一次登记多个模型时 id 串可能超 ``AuditLog.RESOURCE_ID_MAX_CHARS`` 列宽；审计
    写入失败只留 error 日志（绝不抛），不先收短就只能靠审计层的硬截断兜底、丢失
    可读性。尾注宽度按本批数量实测预算，只砍尾部、不切半个数字；完整模型清单在
    审计 detail.params 里有落（超大批次会先撞 params 的 4KB 截断，属审计快照的
    通用上限）。
    """
    width = AuditLog.RESOURCE_ID_MAX_CHARS
    full = ",".join(str(i) for i in ids)
    if len(full) <= width:
        return full
    tail = f"…共{len(ids)}个"
    head: list[str] = []
    for value in ids:
        candidate = ",".join([*head, str(value)])
        if len(candidate) + len(tail) > width:
            break
        head.append(str(value))
    return f"{','.join(head)}{tail}"


@router.post("", response_model=Result[list[ModelEndpointOut]], summary="登记来源端点")
@audit_action(
    action="create",
    resource="endpoint",
    # 一次建 1..N 行：resource_id 记「新建端点 id 逗号串」（data 是列表，装饰器
    # 的 Result.data.id 兜底取不到，显式给；超列宽时截断防审计丢行）
    resource_id=lambda r: _created_ids_snapshot([o.id for o in r["data"]]),
)
async def post_endpoint(
    body: CreateEndpointRequest,
    _perm: None = Depends(require_permission(ENDPOINT_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """登记外部推理来源（共享一份连接配置，收模型数组 1..100；单条录入即长度 1）。

    端点是「可调用的上游」，与发布解耦：登记后需经 /admin/publishes 绑定到
    某个 Route 才会实际承接流量。任一校验不过则一行不写，按请求序返回新建行。

    验证分两层（都受全局 ``endpoint_probe_enabled`` 与请求体 ``skip_probe`` 控制，
    集中在探测模块判定）：

    - **第一个模型走 chat 强验证**（同旧单条登记的最小 chat 请求）：真生成一次，
      证明 Key、地址与「模型真的能服务」，同时覆盖自定义 ``path`` 的连通性——
      这是单条录入场景的验证强度，也是批量的抽样验证；
    - **数组里其余模型走清单成员资格**（一次 ``GET /models``，非逐模型 chat——
      逐个真生成是 N×15s 墙钟，不可取）：不在清单即 422 点名。N=1 时只剩 chat 层，
      与旧单条行为一致；清单校验层的缺席对单条场景是零损失。

    其余本地校验：请求内 ``served_model`` 重复 → 422 点名；库内已存在同
    ``(api_base, protocol, served_model)`` 外部端点 → 422 列出已登记名（按入参
    原样字符串比较；尾斜杠等写法差异视为不同地址，不做归一）。
    """
    requested = [item.served_model for item in body.models]
    dupes = sorted({m for m in requested if requested.count(m) > 1})
    if dupes:
        raise LLMPodValidationError(f"请求内存在重复的模型名：{'、'.join(dupes)}")

    existing = await find_external_endpoints_by_models(
        db, api_base=body.api_base, protocol=body.protocol, served_models=requested
    )
    if existing:
        registered = sorted({e.served_model for e in existing})
        raise LLMPodValidationError(f"以下模型已按该地址与协议登记过，请勿重复登记：{'、'.join(registered)}")

    first = body.models[0]
    # 探测前结束读事务（权限门在请求 session 上的 SELECT 已借出连接）：上游往返最长
    # 可走满墙钟时限，拿借出的池连接 idle-in-transaction 等于用一次登记把连接按住——
    # /test 与 update 两个入口同款处理
    await db.commit()
    await ensure_reachable(
        ProbeTarget(
            protocol=body.protocol,
            api_base=body.api_base,
            path=body.path,
            served_model=first.served_model,
            api_key=body.api_key,
        ),
        skip_probe=body.skip_probe,
    )
    await ensure_models_present(
        protocol=body.protocol,
        api_base=body.api_base,
        api_key=body.api_key,
        served_models=requested[1:],
        skip_probe=body.skip_probe,
    )

    endpoints = []
    for item in body.models:
        endpoint = await create_endpoint(
            db,
            protocol=body.protocol,
            api_base=body.api_base,
            served_model=item.served_model,
            name=item.name or item.served_model,
            api_key=body.api_key,
            provider_name=body.provider_name,
            path=body.path,
            operations=item.operations,
        )
        endpoints.append(endpoint)
    await db.commit()
    # TimestampMixin 的 eager_defaults 让 INSERT 走 RETURNING 读回 created_at 等
    # server_default 列，session 又是 expire_on_commit=False——commit 后对象即可直接
    # 序列化，无需逐行 refresh
    return success([ModelEndpointOut.model_validate(e) for e in endpoints])


@router.get("", response_model=Result[list[ModelEndpointOut]], summary="列出来源端点")
async def get_endpoints(
    _perm: None = Depends(require_permission(ENDPOINT_READ)),
    db: AsyncSession = Depends(get_db),
):
    """全量列出来源端点（含禁用的）；网关配置态与发布绑定均引用此处登记。"""
    endpoints = await list_endpoints(db)
    return success([ModelEndpointOut.model_validate(e) for e in endpoints])


@router.post("/models", response_model=Result[list[str]], summary="拉取上游模型清单")
@audit_action(action="fetch_models", resource="endpoint")
async def fetch_models(
    body: FetchModelsRequest,
    _perm: None = Depends(require_permission(ENDPOINT_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """向上游 ``GET {api_base}/models`` 拉模型清单（登记前发现，不落库、不写健康）。

    返回去重排序后的模型 id；上游清单为空时如实返回空列表。Key/地址/协议不对时
    422 带上游原文（多数错误都长这样，是正常的失败表达而非异常）。权限取
    ``ENDPOINT_UPDATE`` 而非只读：与 ``/test`` 同理——它把调用方提交的凭据发往
    调用方指定的地址，是「向外发凭据」的出口。

    权限校验后先结束读事务再拉清单：上游往返最长可走满墙钟时限（同 create/test
    的连接纪律）。
    """
    await db.commit()
    models = await fetch_upstream_models(protocol=body.protocol, api_base=body.api_base, api_key=body.api_key)
    return success(models)


@router.post("/test", response_model=Result[EndpointProbeOut], summary="探测来源端点连通性")
@audit_action(action="probe", resource="endpoint", resource_id=lambda r: r["data"].get("endpoint_id"))
async def test_endpoint(
    body: TestEndpointRequest,
    _perm: None = Depends(require_permission(ENDPOINT_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """向该端点真发一次最小 chat 请求，回报「通不通 + 上游原话」。

    结论恒走 200：本接口的契约是**报告结论**——把「Key 错」做成 4xx 会让调用方分不清
    「你的请求写错了」与「上游拒绝了你」。只有三种真错误：入参不足/地址无法解析 422 /
    端点不存在 404 / 本地部署端点不适用 422（后两者不消耗上游往返）。

    传 ``endpoint_id`` 且探的**正是该行存储的那份配置**时，把结论写回该行健康
    （通→healthy，不通→down）：「Key 被上游吊销了」由此有了发现与修复入口，否则那行会
    一直显示健康。写回有边界（详见函数体注释）：上游未应答（连接失败/超时）不写、人工
    标的 degraded 不碰、探测期间行被并发改过不覆写。改了配置再测（编辑弹窗里试填新 Key）
    只报告、不写回——那份配置还没落库，结论不属于该行现状。

    权限取 ``ENDPOINT_UPDATE`` 而非只读：它可能改健康状态，进而改网关候选集。
    """
    stored = await get_endpoint(db, body.endpoint_id) if body.endpoint_id is not None else None
    if stored is not None and not probe_applies(stored):
        raise LLMPodValidationError("本地部署端点的健康状态由部署决定，无需单独探测")

    current = ProbeTarget.from_stored(stored) if stored is not None else None
    target = merge_target(
        stored=current,
        protocol=body.protocol,
        api_base=body.api_base,
        served_model=body.served_model,
        path=body.path,
        api_key=body.api_key,
    )
    if not target.api_base or not target.served_model or not target.protocol:
        raise LLMPodValidationError("探测端点需要协议、转发基址和上游模型名；未指定已有端点时请填写这些信息")
    # 结束读事务再探测：上游往返最长可走满墙钟时限，拿借出的池连接 idle-in-transaction
    # 等于用一次诊断把连接按住（到此只有读、无待提交写，commit 无副作用）
    await db.commit()

    result = await probe_endpoint(target)
    # 写回条件（全过才动行）：
    # - 探的就是该行存的那份配置（``matches``），否则结论不属于该行现状（改配置试填只报告）；
    # - 上游真的应答过（``status_code`` 非 None）：连接失败/超时只说明「此刻网络不通」，
    #   据此把 healthy 行写成 down，一次抖动就把端点踢出候选池，而本仓没有后台复探自愈、
    #   恢复只能靠人工——那就不写，结论留在响应里即可；
    # - 该行不是人工标的 degraded：degraded 只来自显式标记（与 update 路由同规矩），
    #   诊断点击不该静默把它放回池里。
    # 写回前先 commit 结束当前事务、再 populate_existing 重读：探测最长可走满墙钟时限，
    # 期间行可能被并发改过（换 Key/改健康），同一 session 的 identity-map 旧对象看不见
    # 外部提交——拿旧快照覆写会把别人刚修好的行打回去；重读后配置仍匹配才写。
    if target.matches(current) and result.status_code is not None:
        fresh = await db.get(ModelEndpoint, stored.id, populate_existing=True)
        if fresh is not None and target.matches(ProbeTarget.from_stored(fresh)) and fresh.health_status != "degraded":
            health = "healthy" if result.healthy else "down"
            if fresh.health_status != health:
                log.info("端点健康状态按探测结论写回：endpoint_id=%s %s→%s", fresh.id, fresh.health_status, health)
                await update_endpoint(db, endpoint_id=fresh.id, health_status=health)
                await db.commit()
    out = EndpointProbeOut.model_validate(result)
    # endpoint_id 回显：审计 resource_id 的取值来源（纯字段探测为 None）
    out = out.model_copy(update={"endpoint_id": body.endpoint_id})
    return success(out)


@router.post("/{endpoint_id}/update", response_model=Result[ModelEndpointOut], summary="更新来源端点")
@audit_action(action="update", resource="endpoint", resource_id="endpoint_id")
async def update_endpoint_route(
    endpoint_id: Annotated[int, Path()],
    body: UpdateEndpointRequest,
    _perm: None = Depends(require_permission(ENDPOINT_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新端点登记信息（地址 / 内部模型名 / 鉴权 / 启用开关 / 健康状态）。

    ``enabled=false`` 或 ``health_status!=healthy`` 会让该端点退出网关候选集
    （快照轮询热更新，无需重启）。

    **连接配置真的有变**时先真连一次上游：不通则 422 且不落库。按值比较（而非「字段传了
    没有」），故改名、改开关、改健康状态都不触发探测；未传的字段按「不传=不改」契约回落
    该行存储值——编辑弹窗不回传已存 API Key，不回落的探测必然 401，会把改个名字也拦死。
    本地部署端点（deployment_id 非空）从不探测：其健康归部署状态投影。

    探测通过即把**探测写回的那档** ``down`` 拨回 ``healthy``（显式传了 ``health_status``
    则以显式值为准）：``down`` 是探测结论，探测通了自然该撤销，否则修好 Key 的行会一直
    停在候选池外。``degraded`` / ``healthy`` 不动——``degraded`` 只可能来自人工显式标记
    （探测从不写它），一次改配置的编辑不该把它静默放回池里。
    """
    stored = await get_endpoint(db, endpoint_id)
    current = ProbeTarget.from_stored(stored)
    health_status = body.health_status
    # 停用（enabled=false）不探：停用本身就是要把它摘出候选池，「停用 + 顺手改地址」
    # 是在处理一个已知坏掉的端点——再要求上游当场通得过，会把停用操作也拦死在 422，
    # 坏端点反而继续在池里服务。
    # 探测在事务外进行（先 commit 结束读事务）：上游往返最长可走满墙钟时限，拿借出的
    # 池连接 idle-in-transaction 等于用一次编辑把连接按住；此处只有读、无待提交写。
    if probe_applies(stored) and body.enabled is not False:
        await db.commit()
        verified = await ensure_reachable(
            merge_target(
                stored=current,
                api_base=body.api_base,
                served_model=body.served_model,
                path=body.path,
                api_key=body.api_key,
            ),
            stored=current,
            skip_probe=body.skip_probe,
        )
        if verified and health_status is None and stored.health_status == "down":
            health_status = "healthy"
    endpoint = await update_endpoint(
        db,
        endpoint_id=endpoint_id,
        api_base=body.api_base,
        served_model=body.served_model,
        name=body.name,
        # api_key/path 的空串与 None 统一按「未提供」处理（与 merge_target 的探测口径
        # 一致）：客户端把未编辑的空输入序列化成 "" 不罕见，若落库侧当真值收，会出现
        # 「探测验的是旧 Key、写进去的是空 Key」的分叉——空 Key 会让 bearer_headers
        # 掉鉴权头，端点留在池里持续 401 而健康位仍是 healthy
        api_key=body.api_key or None,
        provider_name=body.provider_name,
        path=body.path or None,
        enabled=body.enabled,
        health_status=health_status,
    )
    await db.commit()
    return success(ModelEndpointOut.model_validate(endpoint))


@router.post("/{endpoint_id}/delete", response_model=Result[None], summary="删除来源端点")
@audit_action(action="delete", resource="endpoint", resource_id="endpoint_id")
async def remove_endpoint(
    endpoint_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(ENDPOINT_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除端点登记；仍被 RouteBinding 引用时拒绝（先解绑再删）。"""
    await delete_endpoint(db, endpoint_id=endpoint_id)
    await db.commit()
    return success(None)
