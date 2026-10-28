"""WebServer 路由的 ORM 衍生输出 DTO，集中定义避免散落在各路由文件。

每个 ``*Out`` 由 :func:`orm_schema` 从 SQLAlchemy ORM 模型自动生成，免去为每张表
手写 DTO。字段描述**直接取自 ORM 列的 ``comment=``**（见 ``llmpod/models``），从而
文档与模型定义同源——注释只在 ORM 一处维护，且会随 DDL 同步生成到数据库列注释，
DBA / psql ``\\d+`` / BI 工具均可见。

例外是 ``*RuntimeOut`` 运行时投影族（``NodeRuntimeOut`` / ``GpuRuntimeOut``）：非 ORM
衍生、手写定义，数据来自 Manager 进程内实时缓冲（心跳镜像），字段描述与 wire 契约
（``tunnel/schemas``）保持一致。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import ConfigDict, Field, create_model, BaseModel, field_validator
from sqlalchemy import inspect as sa_inspect

from llmpod.apps.manager.deployment.schemas import DeploymentConfig, InstanceSpec
from llmpod.models.api_key import ApiKey
from llmpod.models.audit_log import AuditLog
from llmpod.models.balance_transaction import BalanceTransaction
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.node import Node
from llmpod.models.permission import Permission
from llmpod.models.request_log import RequestLog
from llmpod.models.role import Role
from llmpod.models.route import Route
from llmpod.models.route_binding import RouteBinding
from llmpod.models.system_config import SystemConfig
from llmpod.models.tenant import Tenant
from llmpod.models.user import User
from llmpod.models.wallet import Wallet
from llmpod.services import audit_service

# 推理部署资源模型
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.instance import Instance
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.deploy_log import DeployLog


def orm_schema(
    orm_cls: type,
    name: str | None = None,
    exclude: list[str] | None = None,
    descriptions: dict[str, str] | None = None,
    jsonb_types: dict[str, type] | None = None,
) -> type[BaseModel]:
    """从 SQLAlchemy ORM 模型动态生成 Pydantic 模型，免去为每张表手写 DTO。

    - 设置 ``from_attributes=True``，可直接传 ORM 对象（无需手挑字段）。
    - ``exclude``：排除敏感字段（如 password_hash）。
    - 字段描述默认取自 ORM 列的 ``comment=``；如个别字段需覆盖（或列上不便写注释），
      可经 ``descriptions={字段名: 描述}`` 传入，优先级高于列注释。

    例::

        GpuOut = orm_schema(Gpu)
        UserOut = orm_schema(User, exclude=["password_hash"])
        RoleOut = orm_schema(Role, exclude=["created_at", "updated_at"])
    """
    mapper = sa_inspect(orm_cls)
    exclude_set = set(exclude or [])
    desc_map = descriptions or {}
    jsonb_map = jsonb_types or {}
    fields: dict[str, Any] = {}
    for col in mapper.columns:
        if col.key in exclude_set:
            continue
        # 处理 JSONB 类型：优先用 jsonb_types 显式指定，否则 Any
        from sqlalchemy.dialects.postgresql import JSONB

        if isinstance(col.type, JSONB):
            py_type = jsonb_map.get(col.key, Any)
        else:
            try:
                py_type = col.type.python_type
            except (NotImplementedError, AttributeError):
                py_type = Any
        if col.nullable:
            py_type = py_type | None
        default = None if col.nullable else ...

        # 字段描述：显式 descriptions 优先，其次 ORM 列 comment（单一数据源）
        desc = desc_map.get(col.key) or getattr(col, "comment", None)
        if desc:
            # 有描述时构造 FieldInfo，保留必填/可空语义
            if default is ...:
                fields[col.key] = (py_type, Field(..., description=desc))
            else:
                fields[col.key] = (py_type, Field(default=default, description=desc))
        else:
            fields[col.key] = (py_type, default)
    return create_model(
        name or f"{orm_cls.__name__}Schema",
        __config__=ConfigDict(from_attributes=True),
        **fields,
    )


UserOut: type[BaseModel] = orm_schema(User, exclude=["password_hash", "auth_version"])
TenantOut: type[BaseModel] = orm_schema(Tenant)
GpuOut: type[BaseModel] = orm_schema(Gpu)
ClusterOut: type[BaseModel] = orm_schema(Cluster)
NodeOut: type[BaseModel] = orm_schema(Node)
SystemConfigOut: type[BaseModel] = orm_schema(SystemConfig)
PermissionOut: type[BaseModel] = orm_schema(Permission)
RoleOut: type[BaseModel] = orm_schema(Role, exclude=["created_at", "updated_at"])

# 推理部署资源模型
ModelOut: type[BaseModel] = orm_schema(Model, exclude=["source_auth_encrypted"])
ModelDeploymentOut: type[BaseModel] = orm_schema(ModelDeployment, jsonb_types={"spec": DeploymentConfig})
InstanceOut: type[BaseModel] = orm_schema(Instance, jsonb_types={"spec": InstanceSpec})
ModelEndpointOut: type[BaseModel] = orm_schema(ModelEndpoint, exclude=["api_key"])
DeployLogOut: type[BaseModel] = orm_schema(DeployLog)


# 网关配置态（ProxyServer：Key / 路由发布 / 授权 / 计量）
class ApiKeyOut(orm_schema(ApiKey, exclude=["key_plaintext_encrypted"])):
    """API Key 视图：``plaintext`` 直接回显完整明文。

    网关鉴权用 key_hash，不依赖明文；但管理/租户控制台（在线调试、复制凭据）
    需要可用凭据，按普通凭据材料的口径管理（与密码不同：Key 本就设计为用户侧
    可复制保存），不做脱敏。
    """

    plaintext: str = Field(default="", description="Key 完整明文")


RouteOut: type[BaseModel] = orm_schema(Route)
RouteBindingOut: type[BaseModel] = orm_schema(RouteBinding)
# exclude 运营侧字段：provider_name（上游展示名，本地=部署名）/endpoint_id 泄露内部部署名
# 与端点拓扑，served_model/engine/upstream_type 泄露引擎内部模型名/选型与是否转售外部供应商；
# error_detail 为上游错误原文直存，route_reason 暴露选路策略内部（会话亲和/故障换端），
# 排障信息仅管理员可见；
# 仅管理员可见（admin 用 RequestLogAdminOut），租户侧引擎身份与上游不泄露。
RequestLogOut: type[BaseModel] = orm_schema(
    RequestLog,
    exclude=[
        "provider_name",
        "endpoint_id",
        "served_model",
        "engine",
        "upstream_type",
        "route_reason",
        "error_detail",
    ],
)
RequestLogAdminOut: type[BaseModel] = orm_schema(RequestLog)
WalletOut: type[BaseModel] = orm_schema(Wallet)
BalanceTransactionOut: type[BaseModel] = orm_schema(BalanceTransaction)
# 审计日志：detail 已脱敏（password/token→***），tenant 侧可见字段均安全；
# 租户侧查询已按 tenant_id 强制过滤，无跨租户泄露面。租户侧独立形状：
# actor_type（user|system，系统内部视角）与 tenant_id（本就=自己租户，冗余）不外露。
# action/resource_type 出参译为中文（audit_service 词表，未命中回退英文原串）；
# 筛选入参与 DB 存储仍是英文代码，不受影响。
class _AuditLogLabelMixin(BaseModel):
    """审计行展示文案：action/resource_type 响应时译为中文。"""

    @field_validator("action", mode="before", check_fields=False)
    @classmethod
    def _translate_action(cls, v):
        return audit_service.action_label(v)

    @field_validator("resource_type", mode="before", check_fields=False)
    @classmethod
    def _translate_resource_type(cls, v):
        return audit_service.resource_type_label(v)


_AUDIT_LABEL_DESCRIPTIONS = {
    "action": "操作（出参为中文名称；筛选入参与库存储仍为英文代码）",
    "resource_type": "资源类型（出参为中文名称；筛选入参与库存储仍为英文代码）",
}


class AuditLogOut(_AuditLogLabelMixin, orm_schema(AuditLog, descriptions=_AUDIT_LABEL_DESCRIPTIONS)):
    """审计日志（平台域视角）。"""


class AuditLogTenantOut(
    _AuditLogLabelMixin,
    orm_schema(
        AuditLog,
        exclude=["actor_type", "tenant_id"],
        descriptions={
            "actor_id": "操作者用户ID（含管理员代操作，操作者名见 actor_name）",
            **_AUDIT_LABEL_DESCRIPTIONS,
        },
    ),
):
    """审计日志（租户侧形状）。"""


class RequestLogDetailOut(RequestLogOut):
    """计量行 + 请求/响应 body（详情接口读 BodyStore 块还原）。租户侧视图。"""

    request_body: Any = Field(default=None, description="请求体（BodyStore 块还原）")
    response_body: Any = Field(default=None, description="响应体快照（BodyStore 块还原）")


class RequestLogAdminDetailOut(RequestLogAdminOut):
    """计量行 + 请求/响应 body（管理员侧视图，含上游供应商/端点字段）。"""

    request_body: Any = Field(default=None, description="请求体（BodyStore 块还原）")
    response_body: Any = Field(default=None, description="响应体快照（BodyStore 块还原）")


class ApiKeyCreatedOut(BaseModel):
    """Key 签发响应：Key 视图 + 完整明文（顶层字段便于签发方直接取用）。"""

    api_key: ApiKeyOut
    plaintext: str = Field(..., description="Key 完整明文（与列表/详情回显内容一致）")


class ProxyEndpointOut(BaseModel):
    """API 网关（Proxyserver）接入地址（给前端展示给租户用户「API 该打到哪」）。"""

    base_url: str = Field(
        ...,
        description="API 网关接入基址（优先配置的宣告地址，否则按请求 Host 推导，最后回退 proxy_host:proxy_port 拼接）",
    )
    advertise_url_configured: bool = Field(
        ..., description="是否用了显式配置的宣告地址（false=按请求 Host 推导或回退拼接）"
    )


class EndpointProbeOut(BaseModel):
    """来源端点连通性探测结论（接入/编辑前置闸门与 /admin/endpoints/test 共用）。

    ``from_attributes`` 让探测服务返回的 ``EndpointProbeResult`` 直接 ``model_validate``，
    不必先 ``asdict`` 再解包——字段清单只在两个 dataclass/pydantic 定义处各写一次。
    """

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: int | None = Field(
        None, description="探测的端点 ID（未传 endpoint_id、纯字段探测时为 null；审计 resource_id 取自这里）"
    )
    healthy: bool = Field(..., description="探测是否通过（最小 chat 请求回 2xx 且响应体可解析 JSON）")
    url: str = Field(..., description="实际探测地址（= 真实流量会走到的那个地址）")
    status_code: int | None = Field(None, description="上游 HTTP 状态；连接失败/超时为 null")
    detail: str | None = Field(None, description="上游响应体原文（截断 500 字）或连接异常文案；原样返回，不做翻译")
    elapsed_ms: int = Field(0, description="探测耗时（毫秒）")


class PublishOut(BaseModel):
    """对外服务视图：路由 + 成员列表（关联装配）。"""

    route: RouteOut
    bindings: list[RouteBindingOut]


# ---- 组合输出模型（关联装配字段） ----


class UserDetailOut(UserOut):
    """用户 + 已绑定的角色ID列表 + 所属租户余额（关联装配）。

    ``role_ids`` 由 ``enrich_users_role_ids``（user_service）装配；
    ``tenant_*`` 由 ``enrich_users_tenant_balance``（user_service）装配。
    """

    role_ids: list[int] = Field(default=[], description="用户已绑定的角色ID列表（来自 user_role 关联，路由装配）")
    tenant_balance: Decimal = Field(default=Decimal("0"), description="所属租户总余额（元，聚合装配）")
    tenant_frozen_balance: Decimal = Field(default=Decimal("0"), description="所属租户在途冻结（元，聚合装配）")
    tenant_available: Decimal = Field(
        default=Decimal("0"), description="所属租户可用余额（元，= balance - frozen_balance）"
    )


class RoleDetailOut(RoleOut):
    """角色 + 已授权的权限ID列表（来自 role_permission 关联）。

    ``permission_ids`` 由 ``enrich_roles_permission_ids``（permission_service）装配。
    """

    permission_ids: list[int] = Field(
        default=[], description="角色已授权的权限ID列表（来自 role_permission 关联，路由装配）"
    )


class NodeRuntimeOut(BaseModel):
    """节点实时资源使用摘要（CPU / 内存 / 磁盘 + GPU 聚合），供节点列表前端渲染。

    数据来自 Manager 进程内实时缓冲（心跳镜像），非 DB；离线节点为 ``None``。
    ``gpu_utilization`` 为该节点所有 GPU 核心利用率均值；``used_vram_gb`` /
    ``total_vram_gb`` 为逐卡显存求和。
    """

    cpu_percent: float = Field(0.0, description="CPU 使用率 0-100")
    memory_used_gb: float = Field(0.0, description="已用内存（GB）")
    memory_percent: float = Field(0.0, description="内存使用率 0-100")
    disk_total_gb: float = Field(0.0, description="根分区总量（GB）")
    disk_used_gb: float = Field(0.0, description="根分区已用（GB）")
    disk_percent: float = Field(0.0, description="根分区使用率 0-100")
    gpu_count: int = Field(0, description="GPU 数量")
    gpu_utilization: float = Field(0.0, description="GPU 核心利用率均值 0-100")
    used_vram_gb: float = Field(0.0, description="已用显存求和（GB）")
    total_vram_gb: float = Field(0.0, description="显存总量求和（GB）")


class NodeDetailOut(NodeOut):
    """节点 + 所属集群名称 + 实时资源使用摘要 + 版本比对。

    ``cluster_name`` 由 ``enrich_nodes``（node_service）装配；
    ``runtime`` 由 ``enrich_nodes_runtime``（node_service，读 Manager 实时缓冲）装配，
    离线节点为 ``None``；``version_status`` 由 ``enrich_nodes_version_status`` 装配
    （节点版本 vs manager 版本：ok | outdated | newer | unknown）。
    """

    cluster_name: str | None = Field(default=None, description="所属集群名称（关联 cluster 表装配，可空）")
    runtime: NodeRuntimeOut | None = Field(default=None, description="实时资源使用摘要（在线节点装配，离线为 null）")
    version_status: str = Field(
        default="unknown", description="版本比对：ok=同版 | outdated=落后 | newer=超前 | unknown=未上报/不可解析"
    )


class GpuRuntimeOut(BaseModel):
    """GPU 实时指标摘要（显存 / 利用率 / 温度 / 功耗等），供 GPU 列表前端渲染。

    数据来自 Manager 进程内实时缓冲（心跳镜像），非 DB；缓冲无该卡数据（节点离线 /
    未上报）为 ``None``。全量监控字段走编排侧 ``GpuMetricOut``（/nodes/{id}/gpus）。
    与 ``NodeRuntimeOut`` 同属运行时投影族：非 ORM 衍生、手写定义，字段描述与
    wire ``GpuInfo``（tunnel/schemas/system.py）保持一致。
    """

    used_vram_gb: float = Field(..., description="已用显存（GB）")
    memory_utilization: float = Field(..., description="显存利用率 0-100")
    utilization: float = Field(..., description="GPU 核心利用率 0-100")
    temperature: int = Field(..., description="温度（℃）")
    fan_speed: int = Field(..., description="风扇转速百分比 0-100")
    power_draw_w: float = Field(..., description="当前功耗（W）")
    power_limit_w: float = Field(..., description="功耗上限（W）")
    sm_clock_mhz: int = Field(..., description="SM 时钟频率（MHz）")
    mem_clock_mhz: int = Field(..., description="显存时钟频率（MHz）")
    process_count: int = Field(..., description="GPU 上下文进程数")


class GpuDetailOut(GpuOut):
    """GPU + 所属节点主机名 / 集群信息（关联 node / cluster 表）+ 实时指标。

    ``node_hostname`` / ``cluster_id`` / ``cluster_name`` 由 ``enrich_gpus``（node_service）装配；
    ``runtime`` 由 ``enrich_gpus_runtime``（node_service，读 Manager 实时缓冲）装配，
    无数据（离线 / 未上报）为 ``null``。
    """

    node_hostname: str | None = Field(default=None, description="所属节点主机名（关联 node 表装配，可空）")
    cluster_id: int | None = Field(default=None, description="所属集群ID（经 node → cluster 装配，可空）")
    cluster_name: str | None = Field(default=None, description="所属集群名称（经 node → cluster 装配，可空）")
    runtime: GpuRuntimeOut | None = Field(
        default=None, description="实时指标摘要（读 Manager 实时缓冲，无数据为 null）"
    )


class ClusterDetailOut(ClusterOut):
    """集群 + 节点 / GPU 统计（聚合）。

    ``node_count`` / ``online_node_count`` / ``gpu_count`` 由 ``enrich_clusters``（cluster_service）装配。
    """

    node_count: int = Field(default=0, description="集群下节点总数（聚合装配）")
    online_node_count: int = Field(default=0, description="集群下在线节点数（status=online）")
    gpu_count: int = Field(default=0, description="集群下 GPU 总数（经 node 聚合）")


class BlockedModelOut(BaseModel):
    """被封禁的模型（已解析：路由ID + 模型名 + 类别），前端无需再查 publishes 全集映射。

    由 ``enrich_tenants`` 装配：封禁行 join 路由表解析出名称/类别。
    """

    route_id: int = Field(..., description="路由ID")
    name: str = Field(..., description="模型名（对外别名 = route.name）")
    model_type: str = Field(..., description="模型类别（llm|embedding|rerank|...）")


class TenantDetailOut(TenantOut):
    """租户 + 用户 / 角色统计（聚合）。

    ``user_count`` / ``role_count`` 由 ``enrich_tenants``（tenant_service）装配。
    """

    user_count: int = Field(default=0, description="租户下用户总数（聚合装配）")
    role_count: int = Field(default=0, description="租户下角色总数（聚合装配）")
    balance: Decimal = Field(default=Decimal("0"), description="总余额（元，聚合装配）")
    frozen_balance: Decimal = Field(default=Decimal("0"), description="在途冻结（元，聚合装配）")
    available: Decimal = Field(default=Decimal("0"), description="可用余额（元，= balance - frozen_balance）")
    blocked_models: list[BlockedModelOut] = Field(
        default_factory=list,
        description="本租户被封禁的模型列表（已解析 route_id+名称+类别，聚合装配，默认放行其余全部）",
    )


class EngineInstanceOut(BaseModel):
    """引擎实例（一个推理引擎进程组；多机引擎展开为多个容器分片）。

    手写模型：字段投影自 ``model_deployment_instance`` 表（四层模型第三层），status 是
    投影链权威值（全部分片 READY + head 健康 ⇔ ready），前端不自行从容器状态推导。
    """

    instance_id: str = Field(..., description="引擎实例ID")
    role: str = Field(..., description="引擎角色：full | prefill | decode（prefill 永不出端点）")
    tp: int = Field(..., description="张量并行度")
    pp: int = Field(..., description="流水线并行度")
    ep: int = Field(..., description="专家并行度")
    nnodes: int = Field(..., description="跨机节点数")
    gpu_arch: str | None = Field(None, description="调度架构：nvidia | cann")
    status: str = Field(..., description="provisioning | starting | ready | down（投影）")
    shard_ids: list[str] = Field(
        default_factory=list, description="容器分片 instance_id 列表（instances 平铺行的关联键）"
    )


class DeploymentReplicaOut(BaseModel):
    """部署副本（模板的一份完整拷贝；隔离边界 / 故障域 / 伸缩单位）。

    手写模型：字段投影自 ``model_deployment_replica`` 表 + 引擎子层组装；status 是投影
    链权威值（份内全部引擎 ready ⇔ serving）。``endpoint_ids`` 给出该份的端点行反查
    （端点表一副本一协议一行），前端免自行 join。
    """

    replica_id: str = Field(..., description="副本ID")
    seq: int = Field(..., description="份内序号（0..copies-1；缩容 LIFO 从最大 seq 删）")
    status: str = Field(..., description="provisioning | serving | down（投影）")
    engines: list[EngineInstanceOut] = Field(default_factory=list, description="份内引擎实例")
    endpoint_ids: list[int] = Field(default_factory=list, description="该份端点行 ID（→ endpoints[].id）")


class DeploymentDetailOut(BaseModel):
    """部署详情组合输出（deployment + instances + endpoints + replicas 四层视图）。

    组合模型（包多个 ORM 输出），非单一 orm_schema；集中到 out_models.py 统一管理。
    ``instances`` 保持平铺（每行带 model_deployment_instance_id + spec.replica_idx），
    副本/引擎层级在 ``replicas``——旧图渲染不受影响，新图按层级组装。
    """

    deployment: ModelDeploymentOut
    instances: list[InstanceOut]
    endpoints: list[ModelEndpointOut] = []
    replicas: list[DeploymentReplicaOut] = Field(
        default_factory=list, description="副本层（四层化前的旧数据为空列表，前端回退平铺分组）"
    )


class DeployNodeSummaryItem(BaseModel):
    """某部署每节点当前步骤/状态摘要（取该节点最后一条 deploy_log）。

    手写模型（latest_log_by_node 的聚合产出，非 ORM 直接映射）；updated_at 为 ISO8601 字符串。
    """

    node_id: str | None = Field(default=None, description="节点ID")
    node_hostname: str | None = Field(default=None, description="节点主机名")
    node_ip: str | None = Field(default=None, description="节点 IP")
    step: str | None = Field(default=None, description="当前步骤")
    status: str | None = Field(default=None, description="步骤进度状态")
    level: str | None = Field(default=None, description="日志级别")
    message: str | None = Field(default=None, description="最后一条消息")
    updated_at: str | None = Field(default=None, description="最后一条日志时间（ISO8601）")


__all__ = [
    "orm_schema",
    "UserOut",
    "TenantOut",
    "GpuOut",
    "ClusterOut",
    "NodeOut",
    "SystemConfigOut",
    "PermissionOut",
    "RoleOut",
    "ModelOut",
    "ModelDeploymentOut",
    "InstanceOut",
    "ModelEndpointOut",
    "UserDetailOut",
    "RoleDetailOut",
    "NodeDetailOut",
    "GpuDetailOut",
    "ClusterDetailOut",
    "TenantDetailOut",
    "DeploymentDetailOut",
    "EngineInstanceOut",
    "DeploymentReplicaOut",
    "DeployLogOut",
    "DeployNodeSummaryItem",
    "NodeRuntimeOut",
    "GpuRuntimeOut",
    "ApiKeyOut",
    "RouteOut",
    "RouteBindingOut",
    "RequestLogOut",
    "RequestLogDetailOut",
    "RequestLogAdminOut",
    "RequestLogAdminDetailOut",
    "AuditLogOut",
    "AuditLogTenantOut",
    "WalletOut",
    "BalanceTransactionOut",
    "ApiKeyCreatedOut",
    "PublishOut",
    "ProxyEndpointOut",
    "EndpointProbeOut",
    "BlockedModelOut",
]
