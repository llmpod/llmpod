"""部署 inspect 请求/响应模型。

用户选模型后预填部署配置 + 校验能否跑 + 推荐集群 + 预览拓扑 + 回显三层覆盖解析出的最终 args 与用户自定义 env。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llmpod.apps.manager.deployment.resource import MemoryEstimate
from llmpod.apps.manager.deployment.schemas import (
    CardOccupant,
    CpuLaneConfig,
    EngineConfig,
    GpuSelector,
    OccupiedBy,
    Topology,
)
from llmpod.apps.webserver.schemas.topology import ClusterTopology
from llmpod.config.constants import GpuStatus


class InspectDeploymentRequest(CpuLaneConfig):
    """部署预检入参（扁平结构，对齐 DeploymentConfig 的用户可配项）。

    渐进三阶段：仅 model_id → 推荐引擎/拓扑/集群；+engine+topology → 结构校验 + 容量校验 +
    三层解析回显 resolved；+gpu_selector → 额外校验指定卡集。
    """

    model_config = ConfigDict(extra="forbid")

    model_id: int = Field(..., description="模型 ID（必填）")
    cluster_id: int | None = Field(None, description="集群 ID（不填则推荐）")
    engine: str | None = Field(None, description="引擎（不填则推荐）")
    topology: Topology | None = Field(None, description="拓扑（不填则推荐）")
    gpu_selector: GpuSelector | None = Field(
        None, description="GPU 选择器（手动选卡 gpu_uuids / 指定节点 node_id；不填则自动调度）"
    )
    engine_params: dict[str, Any] = Field(
        default_factory=dict,
        description="用户引擎参数（extra_args/env）——三层覆盖最高层，优先于镜像层与系统层",
    )
    engine_config: EngineConfig = Field(
        default_factory=EngineConfig,
        description="引擎级系统环境（hf_endpoint/nccl_ifname）",
    )
    image: str | None = Field(None, description="镜像全量覆盖（escape-hatch，覆盖 catalog 与 adapter 默认）")
    image_tag: str | None = Field(None, description="镜像 tag（= catalog engine_version 维度）")
    model_path: str | None = Field(None, description="模型权重路径（覆盖 canonical 路径）")


class ClusterGpuInfo(BaseModel):
    """集群可用 GPU 条目（供前端选 GPU）。"""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    node_id: str
    index: int
    arch: str
    name: str = Field("", description="GPU 型号（catalog gpu_model glob 匹配用）")
    driver_version: str | None = Field(None, description="驱动/CANN 版本（catalog 区间匹配用）")
    total_vram_gb: float
    available_vram_gb: float
    # 心跳实时已用显存（含平台外进程；None = 节点未上报）。部署形态求解（lane_candidates）
    # 用它叠物理闸——只看账本会把被外部进程占着的卡当空闲，解出 GPU 形态后调度必败
    used_vram_gb: float | None = None
    status: GpuStatus = Field(GpuStatus.ONLINE, description="online | stale | offline")
    grey_reason: str | None = Field(
        None,
        description="灰化原因：arch_mismatch(架构不被引擎支持) | no_image(引擎/镜像对该卡型号/驱动/镜像tag无匹配) | offline(离线) | None(可选用)",
    )
    grey_detail: str | None = Field(
        None, description="灰化原因人读详情（前端 tooltip 用）；no_image 时必填，其余可为 None"
    )
    fit: bool = Field(True, description="单卡(tp=1)即可承载模型；False 表示需多卡 TP 才行")
    group_key: str | None = Field(
        None, description="选择组键 f'{cluster}:{arch}:{total_vram}'；同组才能同 TP，离线卡为 None"
    )
    required_vram_gb: float | None = Field(
        None, description="模型整体所需显存(size+KV)参考；前端按 required/tp 判定选定 tp 下的卡是否够"
    )
    declared_vram_gb: float = Field(0, description="active 部署声明占用之和（status-aware：stopped/failed 不计）")
    occupied_pct: float = Field(0, description="declared_vram_gb / total_vram_gb")
    occupied_by: list[OccupiedBy] = Field(
        default_factory=list,
        description="占用归因（deployment_id / deployment_name / declared_gb）；空 = 零占用可推荐",
    )
    card_state: str | None = Field(
        None,
        description=(
            "部署痕迹（调度软偏好，不影响可行性）：residual=只剩已停止/失败部署的声明行，"
            "shared=仍被运行中部署占用，None=无痕迹（最优先被选中）"
        ),
    )
    model_local: bool = Field(False, description="该卡所在节点已有本次模型的权重文件（选它可免一次下发）")
    occupants: list[CardOccupant] = Field(
        default_factory=list,
        description="卡上现有部署明细（活跃在前，含已停止/失败；same_model=就是本次这个模型）——用户挑选调度位置用",
    )


class Recommendation(BaseModel):
    """推荐配置（adapter 默认值；唯一例外是 tp——用户未显式指定时按集群可选卡推导最小可行值）。"""

    model_config = ConfigDict(extra="ignore")

    engine: str
    image: str
    topology: Topology
    vram_per_gpu_gb: float
    supported_parallel: list[str] = Field(
        default_factory=list,
        description="引擎声明的并行能力（tp/pp/ep 白名单；pp 门控位，未声明则 pp>1 需 force）",
    )
    supports_multinode: bool = Field(
        default=True,
        description="引擎是否支持多机部署（nnodes>1）；False = 单机引擎（如 llama.cpp），前端应禁用多机控件",
    )


class ResolvedSpec(BaseModel):
    """三层覆盖解析出的最终部署规格（inspect dry-run 回显，供前端预览真命令行）。

    优先级 image(floor) < system(adapter) < user(engine_params)；protected flag 免疫用户覆盖。
    多机 rendezvous 字段（--master-addr/--node-rank/--master-port）在 inspect 阶段为占位值
    （未调度实际节点），部署期由 scheduler 定稿。
    """

    model_config = ConfigDict(extra="ignore")

    image: str | None = Field(
        None,
        description="三层选中最终镜像（config.image > catalog entry）；None=未解析出（如驱动/版本不兼容，见 issues）",
    )
    args: list[str] = Field(default_factory=list, description="三层覆盖后最终启动参数（image<system<user）")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="用户自定义环境变量（engine_params.env 原样回显）——系统/镜像层注入项不回显，前端预填后回传不会把系统值固化成用户层",
    )
    protected_overlap: list[str] = Field(
        default_factory=list, description="用户 extra_args 撞 protected flag 的列表（已被忽略，前端提示）"
    )
    gpu_memory_utilization: float | None = Field(
        None, description="生效显存利用率（= 引擎显存 flag 值；未设则引擎默认 default_gpu_memory_utilization）"
    )


class NodeSupportInfo(BaseModel):
    """单节点可选性（引擎 + 镜像 tag 视角，前端节点选择禁用用）。

    节点可选 ⇔ 存在一张「引擎可用」的卡（online + arch 支持 + 有匹配镜像，即 grey_reason=None）。
    """

    model_config = ConfigDict(extra="ignore")

    node_id: str
    hostname: str = Field("", description="节点主机名")
    selectable: bool = Field(..., description="是否可作为部署目标（有 ≥1 张引擎可用卡；allow_cpu 下内存够也可选）")
    reason: str | None = Field(None, description="selectable=False 的原因（前端 tooltip 用）")
    memory_gb: float = Field(0.0, description="节点总内存（GB，system_buffer 实时值优先）")
    available_memory_gb: float = Field(0.0, description="账面可用内存（GB，扣已预留）")
    cpu_lane: bool = Field(False, description="该节点是否因 CPU 部署（内存）而可选（无可用卡）")


class UnitPlan(BaseModel):
    """每份内单个引擎模板的推选（inspect 展示用）。"""

    model_config = ConfigDict(extra="ignore")

    role: str = Field("full", description="引擎角色：full | prefill | decode")
    tp: int = Field(1, description="张量并行度")
    pp: int = Field(1, description="流水线并行度")
    ep: int = Field(1, description="专家并行度")
    nnodes: int = Field(1, description="跨机节点数")
    gpus_per_engine: int = Field(1, description="每引擎总卡数（tp×pp×ep；跨机分布在 nnodes 节点）")
    image: str | None = Field(None, description="该单元解析镜像")


class UnitPlacement(BaseModel):
    """逐引擎调度（replica_plans 内元素）。"""

    model_config = ConfigDict(extra="ignore")

    role: str = Field("full", description="引擎角色")
    gpu_uuids: list[str] = Field(default_factory=list, description="该引擎分到的卡")
    fit: bool = Field(True, description="每张卡余量 − 每卡所需 ≥ 0")
    headroom_gb: float | None = Field(None, description="最小余量（GB）；不 fit 为 None")


class ReplicaPlan(BaseModel):
    """逐份主结构（自动调度 dry-run / 手选分组配对两路数据源）。"""

    model_config = ConfigDict(extra="ignore")

    seq: int = Field(..., description="份内序号（0..copies-1）")
    node_ids: list[str] = Field(default_factory=list, description="该份所跨节点（去重有序）")
    arch: str = Field("", description="该份调度架构")
    units: list[UnitPlacement] = Field(default_factory=list, description="份内逐引擎调度")
    fit: bool = Field(True, description="份内全部引擎 fit")
    issues: list[str] = Field(default_factory=list, description="配不齐/不 fit 的人读原因")


class RecommendedCard(BaseModel):
    """推荐调度里某张卡的现状（用户判断「这张卡该不该用」的全部依据）。

    与 ``ClusterGpuInfo`` 同源（同一份卡事实的投影）：cluster_gpus 面向「全部卡面板」，
    本模型只描述被推荐的那几张、且内联在推荐结构里，省掉前端按 uuid 关联的步骤。
    """

    model_config = ConfigDict(extra="ignore")

    uuid: str
    node_id: str
    hostname: str = ""
    arch: str
    index: int
    total_vram_gb: float = 0.0
    available_vram_gb: float = 0.0
    required_vram_gb: float | None = Field(None, description="模型整体所需显存（含 KV），判断放不放得下")
    card_state: str | None = Field(None, description="residual / shared / None（同 ClusterGpuInfo）")
    model_local: bool = Field(False, description="该节点已有本次模型权重（免一次下发）")
    occupants: list[CardOccupant] = Field(
        default_factory=list, description="卡上现有部署（含已停止/失败；same_model=就是本次这个模型）"
    )


class RecommendedPlacement(BaseModel):
    """推荐选卡（scheduler dry-run，不预留 VRAM）：自动调度推默认选卡；手选卡时为选集回显。

    自动路径给前端「默认选哪几张卡」：单节点选首个满足节点的 tp×pp 张同架构在线卡；跨机选
    nnodes 个不同节点各 tp×pp//nnodes 张。空列表 = 无满足组合，回退手动选（原因见 issues /
    deployable）。advisory：部署期 scheduler 按 DB 顺序 + 实时 ledger 选卡，资源变动后实际
    分配可能不同。手选路径顶层字段 = 用户选集（gpu_selector.gpu_uuids 去重保序），不跑内核。
    arch 优先 adapter 默认，默认 arch 无合格卡时退回其它受支持 arch（全 Ascend 集群也能出推荐）。

    多副本结构（total_gpus / arch_summary / unit_plans / replica_plans）：顶层 gpu_uuids /
    node_ids = 全部份调度到的卡/节点的去重有序合集（单副本消费方等价于「第一份展开」）；
    replica_plans 为逐份主结构（前端色环分组 / 异构分栏 diff 的数据源）。

    纯 CPU 部署（arch="cpu"）：无卡——gpu_uuids / cards / unit_plans 恒空（前端不得把
    gpu_uuids 当手选卡回传，与 allow_cpu 互斥守卫对撞）；node_ids / replica_plans 为
    节点级调度（按节点内存 binpack dry-run，与创建期同一内核），无 GPU 集群上这是
    前端展示/预选节点的唯一调度来源。
    """

    model_config = ConfigDict(extra="ignore")

    gpu_uuids: list[str] = Field(default_factory=list, description="推荐选卡 UUID 合集（全部份；单节点份 = tp×pp 张）")
    node_ids: list[str] = Field(
        default_factory=list,
        description="所跨节点合集（单节点 1 个，跨机 nnodes 个；去重有序）；纯 CPU 部署 = binpack 调度节点（无卡）",
    )
    arch: str = Field("", description="推荐架构（优先 adapter 默认；默认 arch 无合格卡时退回其它受支持 arch）")
    total_gpus: int = Field(0, description="部署总卡数 = 每引擎卡数 × 份数")
    arch_summary: dict[str, int] = Field(default_factory=dict, description="arch → 卡数（异构份分布）")
    unit_plans: list[UnitPlan] = Field(default_factory=list, description="每份内引擎模板推选")
    replica_plans: list[ReplicaPlan] = Field(
        default_factory=list, description="逐份调度（自动 dry-run；手选时为分组配对结果）"
    )
    cards: list[RecommendedCard] = Field(
        default_factory=list,
        description="被推荐卡片的现状快照（按 gpu_uuids 顺序，前端不必再与 cluster_gpus 关联即可展示理由）",
    )


class InspectDeploymentResponse(BaseModel):
    """部署预检响应。"""

    model_config = ConfigDict(extra="ignore")

    deployable: bool = Field(
        ...,
        description="是否存在阻断项（结构非法/引擎能力硬伤等「force 也绕不过」的冲突）；策略类风险只进 warnings 不影响本值",
    )
    issues: list[str] = Field(default_factory=list, description="阻断项（红灯）：存在任一项即 deployable=false")
    warnings: list[str] = Field(
        default_factory=list,
        description="提醒项（黄灯）：不影响 deployable；含「创建时将被拒但可 force 强制」的策略类风险与占位值说明",
    )
    recommended_cluster_id: int | None = Field(None, description="推荐/指定的集群")
    cross_node: bool = Field(False, description="推荐集群是否需要跨机")
    recommendation: Recommendation
    cluster_gpus: list[ClusterGpuInfo] = Field(default_factory=list)
    node_support: list[NodeSupportInfo] = Field(
        default_factory=list,
        description="逐节点可选性（引擎+镜像 tag 视角，前端节点选择禁用用）；无集群/无在线节点时为空",
    )
    recommended_placement: RecommendedPlacement | None = Field(
        None,
        description=(
            "推荐调度（cluster 解析出即有；None=未解析到集群或部署形态无解（如 CPU 档内存装不下，"
            "原因见 issues）；空列表=集群在但无满足组合）"
        ),
    )
    preview_topology: ClusterTopology | None = Field(None, description="推荐集群拓扑预览（前端画节点图用）")
    resolved: ResolvedSpec | None = Field(
        None, description="传了 engine+topology 时回显三层解析结果；仅 model_id 阶段为 None"
    )
    memory: MemoryEstimate | None = Field(
        None,
        description="纯 CPU 部署资源测算（请求了 CPU 部署且引擎支持时返回）：逐层表 + 场景 + 推荐档位",
    )
