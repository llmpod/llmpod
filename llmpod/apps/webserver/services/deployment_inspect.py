"""部署预检（inspect）的编排服务：状态装配 + 逐阶段判定 + 响应组装。

从路由函数整体迁入（行为锁见 tests/unit/test_inspect_*.py 五份文件）。编排顺序与数据流：

    _prepare_baseline   L1 推荐基线（engine/adapter/KV 口径/推荐拓扑）
    realtime 快照       全平台 used_vram 只取这一次，后续阶段只消费
    _pick_cluster       未指定集群 → 遍历 active 集群推荐（arch 按适配器试探序回落）
    _validate_config    引擎能力闸（类别/并行度）+ 结构校验 + feasibility + CPU 准入（node_resources 在此采集）
    _load_gpu_pool      卡池装配 + 逐卡评估 + 占用/现状合并
    _derive_tp          未显式 tp 时按可选卡推导并回写拓扑（必须先于 _solve_lane）
    _solve_lane         CPU/混合部署逐层求解（复用 node_resources，不重查）
    _warn_cross_subnet  多机跨子网提醒
    _validate_selection 手选卡/选节点校验（含容量预检三分支）
    _build_node_support 逐节点可选性
    _resolve_target_arch 目标 arch/型号/驱动（dry-run 与推荐共用）
    _dry_run_resolve    三层解析回显 resolved
    _attribute_all_grey 全灰集群归因
    preview             推荐集群拓扑预览（异常降级 None）
    build_recommended_placement（deployment_inspect_placement）推荐调度 + 放不下归因

判定分两档（与 create 的分工）：**阻断（issues，deployable=false）**= 结构/能力类
确定性硬伤，force 也绕不过，create 对它无条件 422；**提醒（warnings，不影响
deployable）**= 策略类风险（引擎未声明的并行度、跨子网）与说明性信息（占位值、
超上下文）——create 第一遍会硬拒前者、用户加 force 重试交引擎裁决。两档追加顺序
均 = 编排行序，前端按列表顺序渲染——调整阶段顺序前先看
test_inspect_issues_attribution.py 的顺序锁定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment import image_catalog, parallel, resource
from llmpod.apps.manager.deployment.assess import assess_per_gpu, check_protected_overlap
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.engine.base import has_flag
from llmpod.apps.manager.deployment.feasibility import (
    assess_feasibility,
    collect_node_resources,
    load_used_vram_by_uuid,
)
from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
from llmpod.apps.manager.deployment.recommend import (
    GpuPoolEntry,
    build_recommendation,
    derive_tp,
    pick_cluster_by_arch_order,
    recommend_engine,
    should_derive_tp,
)
from llmpod.apps.manager.deployment.scheduler.gpu import (
    _available_ram,
    _validate_node_selection,
    assess_cpu_capacity,
    card_reserve,
    infer_selection_arch,
    physical_fits,
    reserve_fits,
    validate_manual_gpu_selection,
)
from llmpod.apps.manager.deployment.scheduler.rank import subnet_warning
from llmpod.apps.manager.deployment.scheduler.topology import (
    cpu_lane_pins,
    unit_parallel,
    validate_cpu_lane,
    validate_topology,
)
from llmpod.apps.manager.deployment.schemas import InstanceSpec, NodeCandidate, Topology
from llmpod.apps.manager.deployment.topology import build_preview_topology
from llmpod.apps.webserver.schemas.deployment_inspect import (
    ClusterGpuInfo,
    InspectDeploymentRequest,
    InspectDeploymentResponse,
    NodeSupportInfo,
    Recommendation,
    ResolvedSpec,
)
from llmpod.services import deployment_service
from llmpod.apps.webserver.services.deployment_inspect_placement import build_recommended_placement
from llmpod.config.constants import ClusterStatus, GpuStatus, NodeStatus
from llmpod.config.settings import settings
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node import Node

if TYPE_CHECKING:
    # 仅 preview 字段的类型注解用，运行时不需要
    from llmpod.apps.webserver.schemas.topology import ClusterTopology

log = logging.getLogger("llmpod.apps.webserver.services.deployment_inspect")


@dataclass
class InspectState:
    """一次预检的共享事实。字段按装配阶段分组（分组注释即数据流图）。

    除注释标注「回写」的字段外，各阶段对他人产物只读；placement 段内部共享的
    中间量（free_pool / rec_uuids / _headroom 闭包等）不进 state，留在该模块函数体内。
    """

    # 请求侧（构造即定，全程只读）
    request: InspectDeploymentRequest
    model: Model

    # L1 基线（_prepare_baseline 装配；⚠ topo / vram_per_gpu / recommendation 会被 _derive_tp 回写）
    engine: str = "vllm"
    adapter: object | None = None
    util: float = 0.9
    kv_dtype_bytes: float | None = None
    arch_meta: object | None = None
    shape: object | None = None
    recommendation: object | None = None
    topo: Topology | None = None
    vram_per_gpu: float = 0.0
    arch_order: list = field(default_factory=list)
    tp_explicit: bool = False

    # 集群与实时快照（realtime 在编排入口取一次，后续只消费这一份——不同阶段各查
    # 各的会出现「前一阶段认为有卡、后一阶段认为没有」的自相矛盾推荐）
    cluster_id: int | None = None
    cross_node: bool = False
    realtime: dict = field(default_factory=dict)

    # 卡池（_load_gpu_pool 装配；cluster_gpus 富化序：基础池→逐卡评估→占用/现状合并→model_local）
    cluster_gpus: list = field(default_factory=list)
    node_rows: list = field(default_factory=list)
    card_status: dict = field(default_factory=dict)

    # CPU/混合部署测算（node_resources 由 _validate_config 采集、_solve_lane 复用不重查）
    node_resources: list = field(default_factory=list)
    lane_node_resources: list = field(default_factory=list)
    memory: object | None = None
    memory_by_node: dict = field(default_factory=dict)
    # 逐副本指定中账本容量装不下的节点（_solve_lane 产出；调度段据此保留 fit=false
    # 的调度结果——容量类阻断可被 force 绕过，前端要能推进到确认页的强制部署入口）
    cpu_pins_over: set = field(default_factory=set)

    # 准入旗标（_validate_config 产出；后续多个阶段消费）
    manual_gpus: bool = False
    cpu_lane_ok: bool = False
    sel_uuids: list | None = None

    # arch 解析（_resolve_target_arch 产出；dry-run / 全灰归因 / 推荐调度消费）
    arch: str = "nvidia"
    arch_candidates: list = field(default_factory=list)
    target_gpu_model: str | None = None
    target_driver: str | None = None
    cpu_only: bool = False
    manual: bool = False

    # 回显产出
    resolved: ResolvedSpec | None = None
    preview: ClusterTopology | None = None
    node_support: list = field(default_factory=list)
    recommended_placement: object | None = None

    # 判定累积（追加顺序 = 响应顺序 = 编排行序）：issues 阻断（deployable=false）、
    # warnings 提醒（不影响 deployable——含「创建时可 force 绕过」的策略类提示）
    issues: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    deployable: bool = True

    def fail(self, message: str) -> None:
        """记一条阻断性 issue 并判不可部署。"""
        self.issues.append(message)
        self.deployable = False

    def warn(self, message: str) -> None:
        """记一条提醒（不影响 deployable；渲染为黄灯）。"""
        self.warnings.append(message)


async def run_inspect(db: AsyncSession, request: InspectDeploymentRequest) -> InspectDeploymentResponse:
    """部署预检编排：推荐配置 + 逐卡匹配评估 + 校验能否跑 + 预览拓扑。

    按请求完整度三阶段递进：仅 model_id → 推荐；+engine +topology → 结构校验 +
    显存可行性 → deployable/issues；+gpu_selector → 额外校验指定卡集。三阶段判定
    顺序不变（见模块 docstring 的阶段表）。
    """
    model = await db.get(Model, request.model_id)
    if model is None:
        raise NotFound("Model", request.model_id)
    c = InspectState(request=request, model=model)

    # 处理前置基础资源：L1 推荐基线：engine / adapter / KV 口径 / 推荐拓扑 / tp 是否显式；本段无 issue
    _prepare_baseline(c)
    # 收集全平台资源：全平台 used_vram 实况快照：只取这一次，后续阶段只消费这同一份（各查各的会前后矛盾）
    c.realtime = load_used_vram_by_uuid()
    # cluster_id 未填（含 0 等非法值）→ 遍历 active 集群推荐最优（单节点优先、arch 逐引擎回落）；本段无 issue
    await _pick_cluster(db, c)
    # 结构校验 + 引擎能力闸（类别硬伤阻断 / 并行未声明降提醒——create 第一遍才硬拒）+ 显存 feasibility + CPU 准入；node_resources 在此采集（_solve_lane 复用不重查）；阻断：结构非法 / 类别不符 / feasibility 不过 / 引擎不支持 CPU / 缺架构元数据，提醒：并行未声明 / 超上下文 / 信任远程代码
    await _validate_config(db, c)
    # 卡池富化：基础行（含离线卡，前端灰化）→ 逐卡评估（grey_reason / required_vram_gb）→ 占用 + 卡现状两视图合并；本段无 issue（灰化语义经 cluster_gpus 回显）
    await _load_gpu_pool(db, c)
    # 未显式 tp 时按可选卡推导并回写 topo / vram_per_gpu；必须先于 _solve_lane（晚于它会出现集群级结论与 node_support 各说各话）；本段无 issue
    _derive_tp(c)
    # CPU / 混合部署逐层求解：纯 CPU 按节点内存判、显存档逐卡双闸复核；产出 memory / memory_by_node（node_support 直接复用不重算）；阻断：指定节点不在线 / 结构非法 / 内存不足 / 显存档装不下
    if c.cpu_lane_ok:
        await _solve_lane(db, c)
    # 多机跨子网 NCCL 可达性：仅提醒不阻断（create 第一遍硬拒，force 可绕）
    await _warn_cross_subnet(db, c)
    # 手选卡 / 选节点校验（结构规则与 create assign 共用同一对校验函数）+ 手选容量三分支预检（装不下模型 > 平台内占用 > 平台外进程；权威硬拒在 create）
    _validate_selection(c)
    # 逐节点可选性（引擎 + 镜像视角，前端节点选择禁用用）；直接复用 _solve_lane 的逐节点表；本段无 issue
    _build_node_support(c)
    # 目标 arch / 型号 / 驱动 + 可调度架构序（arch_candidates 与 create 同入口，避免预检说能落、create 落不到）；dry-run 与推荐调度共用本段产物；本段无 issue
    _resolve_target_arch(c)
    # dry-run 三层解析回显 resolved：镜像解析失败降级 image=None + 提示（不 422）、适配器组装期结构硬拒转 issue（均阻断）；多机 rendezvous 参数为占位值（提醒）
    _dry_run_resolve(c)
    # 全灰集群归因：有卡但无任何可选卡时给人读原因（no_image / 全离线 / 全架构不符）；阻断
    _attribute_all_grey(c)
    # 推荐集群拓扑预览：无集群不查（Cluster 主键为空只会白查一次 + 一条误导告警）；异常降级 None 不阻断（推荐调度照常出）
    if c.cluster_id:
        try:
            c.preview = await build_preview_topology(db, c.cluster_id, c.model, c.engine, c.recommendation.topology)
        except Exception as e:
            log.warning("inspect preview topology failed: %s", e)
    # 推荐调度 + 逐份 replica_plans + 放不下归因（与 create 同选卡内核；纯 CPU 部署改出
    # 节点级 binpack 调度；段内七函数分工见 deployment_inspect_placement 模块）；阻断：结构性探测 / 放不下五分支归因
    await build_recommended_placement(db, c)
    # 纯投影组装响应，不再判定
    return _build_response(c)


def _prepare_baseline(c: InspectState) -> None:
    """L1 推荐基线。

    填充：engine / adapter / util / kv_dtype_bytes / arch_meta / shape / recommendation /
    topo / vram_per_gpu / arch_order / tp_explicit。不追加 issue（「集群无任何可选卡」
    等阻断在后续判定段）。
    """
    engine = c.request.engine or recommend_engine(c.model.model_type, c.model.format) or "vllm"
    c.engine = engine
    adapter = get_adapter(engine)
    c.adapter = adapter
    # 生效显存占比：**只用于回显**「启动命令长什么样」（用户自己填的 flag 属于他的输入，
    # 照旧出现在预览里）。inspect 的一切判定都按平台默认口径算——一张卡只推荐一个部署，
    # 不看用户覆盖：改显存占比是「我知道要共享这张卡」的显式表达，只在 create 落账本时生效。
    c.util = adapter.memory_utilization_from(c.request.engine_params)
    # 生效 KV 精度档（字节数）与模型架构形状：KV 估算的唯一定义处，贯穿推荐/预检/逐卡/推导
    c.kv_dtype_bytes = adapter.kv_cache_dtype_bytes_from(c.request.engine_params)
    c.arch_meta = resource.KVArchMeta.from_model(c.model)
    # 模型 head 形状（tp 整除校验的输入）：预检/调度/推导三处共用，只取一次。
    # 经 shape_for 取值——引擎不按 head 切 tp（llama.cpp 走 layer 切分）时得空形状，下游不约束
    c.shape = parallel.shape_for(adapter, c.model)
    # 推荐显存含 KV：透传用户选的 max_model_len，未选按 KV_BASE_CONTEXT 兜底（与 deploy 同口径）；
    # 兜底值超模型自带上限时钳到上限——超限是引擎 config 期硬拒，不是估算误差
    rec_ctx = c.request.topology.max_model_len if c.request.topology else None
    c.recommendation = build_recommendation(
        adapter,
        c.model.size_gb,
        c.model.model_type,
        engine,
        context_size=rec_ctx,
        kv_dtype_bytes=c.kv_dtype_bytes,
        arch_meta=c.arch_meta,
        model_max_context=c.model.max_context,
    )

    # 用户传了 topology 就用用户的，否则用推荐的
    c.topo = c.request.topology or Topology(**c.recommendation.topology)
    c.vram_per_gpu = c.recommendation.vram_per_gpu_gb
    # arch 试探顺序（adapter 唯一定义处）：tp 推导 / 目标 arch 解析 / 推荐选卡三处共用
    c.arch_order = adapter.arch_order
    # tp 显式 = 用户原始 payload 带 tp（旧写法）或带 units（模板化写法自带 tp）。
    # 不能用 model_fields_set：归一化 validator 会回填全部形状键。集群推荐须知道它——
    # 显式 tp=1 时不能走「按某个 tp 装得下」的自动判（那是用户已否定的形状）。
    c.tp_explicit = bool(c.request.topology) and bool(
        (c.request.topology.explicit_fields or set()) & {"tp", "units", "copies"}
    )


async def _pick_cluster(db: AsyncSession, c: InspectState) -> None:
    """集群选择：cluster_id 未填时遍历 active 集群推荐最优（单节点优先、跨机次之）。

    填充：cluster_id / cross_node。不追加 issue（无合适集群属「无数据」，各视图回空）。
    0 不是合法集群 id（前端空输入经 Number('') 常见产物）——按未填写处理走自动推荐，
    不然后续各段「已指定集群却查不到卡」会拼出一份全空但绿灯的响应。
    """
    c.cluster_id = c.request.cluster_id
    if c.cluster_id:
        return
    # 遍历 active 集群推荐
    active_clusters = list((await db.execute(select(Cluster).where(Cluster.status == ClusterStatus.ACTIVE))).scalars())
    # 查各集群 GPU 池（JOIN Gpu→Node→Cluster，过滤 online）
    cluster_pools: dict[int, list[GpuPoolEntry]] = {}
    for cl in active_clusters:
        node_rows = list(
            (await db.execute(select(Node).where(Node.cluster_id == cl.id, Node.status == NodeStatus.ONLINE))).scalars()
        )
        if not node_rows:
            continue
        nids = [n.id for n in node_rows]
        gpu_rows = list(
            (await db.execute(select(Gpu).where(Gpu.node_id.in_(nids), Gpu.status == GpuStatus.ONLINE))).scalars()
        )
        if not gpu_rows:
            continue
        entries = []
        for g in gpu_rows:
            used = c.realtime.get(g.uuid, 0.0)
            entries.append(
                GpuPoolEntry(
                    uuid=g.uuid,
                    node_id=g.node_id,
                    arch=g.arch or "",
                    total_vram_gb=g.total_vram_gb or 0.0,
                    available_vram_gb=max(0.0, (g.total_vram_gb or 0.0) - used),
                )
            )
        cluster_pools[cl.id] = entries

    # vram_per_gpu 此刻是 tp=1 口径（= 模型总需求）。tp 由后续自动推导时（未显式指定）
    # 传 shape 让 pick_cluster 按「哪个 tp 装得下」判；用户已显式固定 tp（含 1）时按
    # 用户形状判——shape=None 走逐卡门槛（每卡需求 = 总需求 ÷ tp），不替用户改形状。
    # arch 回落序 = c.arch_order（adapter 唯一定义处，默认 arch 优先）
    pick = pick_cluster_by_arch_order(
        cluster_pools,
        c.vram_per_gpu,
        c.topo.tp,
        c.arch_order,
        shape=None if c.tp_explicit else c.shape,
    )
    if pick:
        c.cluster_id = pick.cluster_id
        c.cross_node = pick.cross_node


async def _validate_config(db: AsyncSession, c: InspectState) -> None:
    """L2 校验：引擎能力闸 + 结构校验 + 显存可行性 + CPU 部署准入（仅传了完整配置时做结构/feasibility）。

    填充：manual_gpus / cpu_lane_ok / node_resources（CPU 部署形态求解复用，不重查）。
    可能追加 warning：超上下文提示、信任远程代码提示、并行度未声明（策略类，
    create 第一遍硬拒、force 可绕）。可能追加 issue（阻断）：引擎类别不符、
    结构校验失败、feasibility 不可部署、CPU 引擎能力不符、模型缺架构元数据。
    """
    request, model, adapter = c.request, c.model, c.adapter
    topo = c.topo
    # 手选卡 = 固定显存部署（与 create 路由的 allow_cpu 互斥守卫同口径），CPU 求解不参与
    c.manual_gpus = bool(request.gpu_selector and request.gpu_selector.gpu_uuids)
    c.cpu_lane_ok = bool(request.cpu_lane_requested and adapter.supports_cpu_lane() and not c.manual_gpus)
    # 节点资源（在线节点 + 可用显存/内存）**一次采集两处共用**：feasibility 的逐层 offload
    # 评估与下方部署测算。无集群 / 缺元数据时两处都不需要。
    if c.cpu_lane_ok and c.cluster_id and model.size_gb and model.num_layers:
        c.node_resources = await collect_node_resources(
            db, c.cluster_id, gpu_archs=adapter.offload_gpu_archs(), used_vram=c.realtime
        )
    # 显式要求超上下文：引擎（vLLM）对「超过模型自带上限」是 config 期硬拒。这里只提示不拦——
    # 自带上限不含 rope_scaling 扩展，硬拦会误伤扩展过上下文的模型；真越界时引擎另有逃生门环境变量。
    if model.max_context and topo.max_model_len and topo.max_model_len > model.max_context:
        c.warn(
            f"max_model_len {topo.max_model_len} 超过模型自带上下文上限 {model.max_context}，"
            "引擎可能拒绝启动（可保持默认值或调小）"
        )
    # 模型带自定义代码（auto_map）→ 引擎要求显式信任才肯加载。创建时已按模型行声明
    # 自动补 flag（见 create 路由 ensure_trust_remote_code），这里只提示安全含义——
    # 信任远程代码等于放行模型仓库里的 Python，用户应知情。
    trust_flag = adapter.trust_remote_code_flag
    if trust_flag and model.needs_trust_remote_code:
        extra_args = (request.engine_params or {}).get("extra_args") or ()
        if not has_flag(extra_args, trust_flag):
            c.warn(
                f"该模型带自定义代码（config auto_map），创建部署时将自动在引擎参数里加 "
                f"{trust_flag}（模型仓库里的 Python 将在引擎容器内执行，请确认来源可信）"
            )
    if request.engine and request.topology:
        # 引擎-类别能力闸（与 create 路由同判据）：引擎服务不了该模型类别时 create 必
        # 422，预检不提前说就是「绿灯走完向导、提交才死」
        try:
            adapter.validate_category(model.model_type)
        except LLMPodValidationError as e:
            c.fail(str(e))
        # 并行能力闸（判据与 create 同源；级别按角色）：pp>1 须引擎声明 + 模型侧门槛。
        # 这类是策略性风险而非确定失败（引擎实际可能接受）——预检只黄灯提醒并把
        # 「创建时 force 可绕」指给用户；真正的硬拒在 create 第一遍，用户加 force
        # 重试即交引擎裁决。与类别闸（确定性硬伤，此处直接阻断）刻意不同级
        try:
            adapter.validate_parallel(topo, architecture=model.architecture, model_type=model.model_type)
        except LLMPodValidationError as e:
            c.warn(str(e))
        try:
            validate_topology(
                request.topology,
                request.gpu_selector,
                adapter=adapter,
                shape=c.shape,
                # 显式 cpu_only 且引擎声明 CPU 能力：node_ids 语义=逐副本指定（长度=
                # 副本数），GPU 侧限制不适用；不支持 CPU 的引擎保持 GPU 规则
                cpu_lane=request.offload_preference == resource.SCENARIO_CPU_ONLY and adapter.supports_cpu_lane(),
            )
        except LLMPodValidationError as e:
            c.fail(str(e))
        try:
            report = await assess_feasibility(
                db,
                model_size_gb=model.size_gb,
                model_type=model.model_type,
                model_format=model.format,
                cluster_id=c.cluster_id,
                context_size=topo.max_model_len,
                kv_dtype_bytes=c.kv_dtype_bytes,
                arch_meta=c.arch_meta,
                total_layers=model.num_layers,
                node_resources=c.node_resources or None,
                used_vram=c.realtime,
                # 部署授权与逐卡口径：与部署测算/编排器同参，否则「预检说能部署、调度失败」
                allow_cpu=request.cpu_lane_requested,
                parallel=unit_parallel(topo),
            )
            if c.cluster_id and c.cluster_id not in report.deployable_clusters:
                c.fail(report.message or "显存不足")
        except Exception as e:
            log.warning("inspect feasibility failed: %s", e)

    # CPU 部署准入（引擎能力 + 模型元数据）：资源测算在逐卡池就绪后（见 _solve_lane）
    # 引擎能力准入：请求了 CPU 部署但该引擎未声明此能力（vLLM/SGLang）→ 明确报不可部署，
    # 不静默按显存部署求解（部署语义不漂移；能力由适配器声明，不在此写引擎名）
    if request.cpu_lane_requested and not adapter.supports_cpu_lane():
        c.fail(f"引擎 {c.engine} 不支持 CPU / 内存部署（allow_cpu / offload_preference=cpu_only）")
    # 模型缺架构元数据（num_layers / size_gb）→ 逐层表与 CPU 部署测算都建不起来，明确告知，
    # 避免落到 GPU 侧「显存不足」的误导提示
    if c.cpu_lane_ok and (model.size_gb is None or not model.num_layers):
        c.fail("模型缺架构元数据（num_layers / size_gb），无法测算 CPU 部署资源；请重新探测模型元数据")


async def _load_gpu_pool(db: AsyncSession, c: InspectState) -> None:
    """集群 GPU 池装配：基础行 → 逐卡评估 → 占用/现状两视图合并。

    填充：node_rows / cluster_gpus（富化后）/ card_status。不追加 issue（灰化语义
    经 cluster_gpus 回显，集群级归因在 _attribute_all_grey / 推荐调度段）。
    离线/stale 卡也返回（前端灰化展示）；节点仍限在线。
    """
    if not c.cluster_id:
        return
    request, model = c.request, c.model
    c.node_rows = list(
        (
            await db.execute(select(Node).where(Node.cluster_id == c.cluster_id, Node.status == NodeStatus.ONLINE))
        ).scalars()
    )
    nids = [n.id for n in c.node_rows]
    if not nids:
        return
    # 不按 Gpu.status 过滤：离线/stale 卡也要返回（前端灰化展示）；节点仍限在线
    raw: list[ClusterGpuInfo] = []
    for g in (await db.execute(select(Gpu).where(Gpu.node_id.in_(nids)))).scalars():
        used = c.realtime.get(g.uuid, 0.0)
        raw.append(
            ClusterGpuInfo(
                uuid=g.uuid,
                node_id=g.node_id,
                index=g.index,
                status=g.status,
                arch=g.arch or "",
                name=g.name or "",
                driver_version=g.driver_version,
                total_vram_gb=g.total_vram_gb or 0.0,
                available_vram_gb=round(max(0.0, (g.total_vram_gb or 0.0) - used), 2),
                # 心跳原始已用（None = 未上报）：部署形态求解叠物理闸用（见
                # resource.lane_candidates）；available_vram_gb 是它的派生展示值
                used_vram_gb=c.realtime.get(g.uuid),
            )
        )
    assessments = {
        a.uuid: a
        for a in assess_per_gpu(
            raw,
            engine=c.engine,
            model_size_gb=model.size_gb,
            context_size=c.topo.max_model_len,
            cluster_id=c.cluster_id,
            engine_version=request.image_tag,
            explicit_image=request.image,
            model_type=model.model_type,
            kv_dtype_bytes=c.kv_dtype_bytes,
            arch_meta=c.arch_meta,
            n_parallel=request.kv_cache.n_parallel,
        )
    }
    for cg in raw:
        a = assessments.get(cg.uuid)
        if a:
            cg.grey_reason = a.grey_reason
            cg.grey_detail = a.grey_detail
            cg.fit = a.fit
            cg.group_key = a.group_key
            cg.required_vram_gb = a.required_vram_gb
    c.cluster_gpus = raw

    # 声明占用 + 卡现状一次取行同时产出（同一批 assignment/部署行的两个视图，
    # 分开查各查一遍）——占用回显给推荐过滤与手选校验，现状给用户挑卡与痕迹偏好
    if c.cluster_gpus:
        uuids = [cg.uuid for cg in c.cluster_gpus]
        occupancy, card_status = await deployment_service.resolve_card_views(
            db,
            uuids,
            model_id=c.model.id,
            totals={cg.uuid: cg.total_vram_gb for cg in c.cluster_gpus},
        )
        c.card_status = card_status
        for cg in c.cluster_gpus:
            e = occupancy.get(cg.uuid)
            if e:
                cg.declared_vram_gb = e.declared_gb
                cg.occupied_pct = e.occupied_pct
                cg.occupied_by = e.occupied_by
            f = card_status.get(cg.uuid)
            cg.card_state = f.state if f is not None else None
            cg.occupants = f.occupants if f is not None else []


def _derive_tp(c: InspectState) -> None:
    """拓扑推导：用户未显式指定 tp 时，按集群可选卡 + 模型显存推导能跑的最小 tp。

    **回写** topo / recommendation.topology / vram_per_gpu（含响应字段），让后续校验、
    dry-run、推荐按能跑的拓扑算——否则默认 tp=1 会把单卡装不下的模型判成空推荐。
    **必须早于 _solve_lane**：逐层表按 tp×pp 均摊每卡需求，两份拓扑会让同一响应里
    集群级结论与逐节点 node_support 互相矛盾（两者都被前端消费）。
    守卫与推导细节见 ``recommend.should_derive_tp``：仅自动调度 + 单机单副本 pp 未设。
    """
    adapter = c.adapter
    # CPU 取向偏好（cpu_only / cpu_main 且 CPU 通道准入通过）：解恒为纯 CPU 档
    # （validate_cpu_lane 要求 tp=pp=nnodes=1），按卡池推导 tp 只会造出 CPU 通道装不下
    # 的形状——同一请求 create 按用户原形状（tp 缺省 1）是绿的，预检却红灯（假阻断）。
    # 不含 allow_cpu 单独为真的情形：那是「允许回落」而非「取向 CPU」，无卡可解时
    # 仍走显存形态，tp 推导照常有用（cpu_lane_ok 也已含 allow_cpu 判定）
    if c.cpu_lane_ok and c.request.offload_preference in resource.CPU_PREFERENCES:
        return
    if should_derive_tp(c.request.gpu_selector, c.topo, tp_explicit=c.tp_explicit, has_pool=bool(c.cluster_gpus)):
        eligible = [cg for cg in c.cluster_gpus if cg.grey_reason is None and not cg.occupied_by]
        req = next((cg.required_vram_gb for cg in c.cluster_gpus if cg.required_vram_gb is not None), None)
        derived = derive_tp(
            eligible,
            required_vram_gb=req,
            arch_order=c.arch_order,
            shape=c.shape,
        )
        if derived > max(1, int(c.topo.tp or 1)):
            log.info("inspect model=%s 推导 tp=%s（用户未显式指定）", c.request.model_id, derived)
            # with_updates 让 units/旧四数两套字段同步（model_copy 直改 tp 会与 units 漂移，
            # recommendation.topology 回读 Topology 时被冲突校验拒）
            c.topo = c.topo.with_updates(tp=derived)
            c.recommendation.topology = c.topo.model_dump()
            c.vram_per_gpu = round(
                adapter.estimate_vram_gb(
                    c.model.size_gb or 0.0,
                    c.topo,
                    model_type=c.model.model_type,
                    dtype_bytes=c.kv_dtype_bytes,
                    arch_meta=c.arch_meta,
                    n_parallel=c.request.kv_cache.n_parallel,
                ),
                2,
            )
            c.recommendation.vram_per_gpu_gb = c.vram_per_gpu


async def _solve_lane(db: AsyncSession, c: InspectState) -> None:
    """CPU 部署资源测算（逐卡池已就绪，节点候选含显存与内存两个池）。

    填充：memory / memory_by_node / lane_node_resources（node_support 复用同一份表）。
    可能追加 issue：指定节点不在线、CPU 拓扑结构非法、内存不足、显存档复核不足、
    纯 CPU 无解。
    """
    request, model, adapter, topo = c.request, c.model, c.adapter, c.topo
    # KV 估算先行：部署形态求解（每档的显存/内存需求）的唯一输入
    kv_gb_est = 0.0
    if model.size_gb and model.num_layers:
        kv_gb_est = adapter.kv_gb_for(
            model,
            request.engine_params,
            context_size=topo.max_model_len,
            n_parallel=request.kv_cache.n_parallel,
        )
    # 完整门槛（编排调用处的 `if c.cpu_lane_ok` 只是可读性副本，本行才承重）：
    # 除策略条件外还挡数据完备——无集群时 node_resources 为空表，指定节点检查会假报
    # 「指定节点不在线」；缺 size_gb / num_layers 时 None 会进逐层表算术
    if not (c.cpu_lane_ok and c.cluster_id and model.size_gb and model.num_layers):
        return
    # node_resources 已在 _validate_config 采集（两处共用），此处只按指定节点收窄
    # 逐副本指定（cpu_lane_pins 唯一定义处：显式 cpu_only + gpu_selector.node_ids，
    # 长度=副本数由 validate_topology 把关）：第 i 份落 node_ids[i]，同节点可指定多份；
    # 其余请求形态的 node_ids 是 GPU 侧候选集语义，不参与 CPU 调度
    pinned_nodes = cpu_lane_pins(request.gpu_selector, request.offload_preference)
    # 指定节点（CPU 节点选取）只收窄「求解域」（容量判定/取解）；node_resources 保持全量——
    # node_support 的可用内存与逐节点 cpu_lane 是展示口径，跟着收窄会把未指定节点显示成
    # 0 可用/不可选（前端判「内存不足」）。指定的节点不在线时**不得静默放宽为全部节点**
    # ——部署路径对同一请求硬拒（「指定节点不在线」），预览报可部署只会误导用户
    pinned_node = request.gpu_selector.node_id if request.gpu_selector else None
    solve_facts = c.node_resources
    if pinned_node and not any(n.node_id == pinned_node for n in c.node_resources):
        c.fail(f"指定节点 {pinned_node} 不在线，无法在其上部署")
        solve_facts = []
    elif pinned_node:
        solve_facts = [n for n in c.node_resources if n.node_id == pinned_node]
    elif pinned_nodes is not None:
        pin_set = set(pinned_nodes)
        missing = sorted(pin_set - {n.node_id for n in c.node_resources})
        if missing:
            c.fail(f"指定节点 {'、'.join(missing)} 不在线，无法在其上部署")
            solve_facts = []
        else:
            solve_facts = [n for n in c.node_resources if n.node_id in pin_set]
    c.lane_node_resources = solve_facts
    # 候选池与逐节点求解都走 resource 的共用入口（与编排器 _resolve_lane 同一口径）：
    # 显存只算**在线**且引擎可用架构的卡（cluster_gpus 刻意带离线/stale 行供前端灰化，
    # 不能拿来当可用量）；两池都取账本可用量——账本已扣别家预留、也已在内部折过利用率
    # 上限，折让只折这一次（此前显存取账本、内存取实时原值混用，内存侧白丢 10% 余量）。
    # 池按**全量节点**装配一次：lane_candidates 是节点过滤 + 逐节点独立映射，全量解
    # 出来再按求解域过滤取解即与收窄池逐解等价；展示口径（memory_by_node /
    # node_support）也要求全量——指定收窄只该影响取解，未指定节点要出自己的
    # cpu_lane/可用档（收窄池会把它们整片判不可选）
    candidates = resource.lane_candidates(
        c.node_resources,
        [cg for cg in c.cluster_gpus if cg.status == GpuStatus.ONLINE],
        get_orchestrator().ledger,
        gpu_archs=adapter.offload_gpu_archs(),
    )
    estimates = resource.lane_estimates(
        candidates,
        weight_gb=model.size_gb,
        kv_gb=kv_gb_est,
        total_layers=model.num_layers,
        allow_cpu=True,
        preference=request.offload_preference,
        kv_source="formula" if c.arch_meta is not None else "heuristic",
        # 逐层表显存侧按该份 tp×pp 均摊（与 estimate_vram_gb 同口径）
        parallel=unit_parallel(topo),
    )
    c.memory_by_node = {cand.node_id: est for cand, est in estimates}
    # 取解限定在求解域（指定节点）内——在收窄子集里挑最优，不拿未指定节点解档
    solve_ids = {n.node_id for n in c.lane_node_resources}
    lane = resource.best_from_estimates([pair for pair in estimates if pair[0].node_id in solve_ids])
    if lane is not None:
        c.memory = lane.memory
        if resource.is_cpu_only(c.memory):
            # 纯 CPU：卡数/显存校验与逐卡推荐都不适用，改用节点内存判定
            try:
                validate_cpu_lane(topo)
            except LLMPodValidationError as e:
                c.fail(str(e))
            if pinned_nodes is not None:
                # 逐副本指定：按指定编排逐节点校验（第 i 份落 node_ids[i]，同节点多份
                # 累加）——binpack 的堆叠口径不镜像指定分布，会把「指定去小节点的份」
                # 算到大节点头上误放行；装不下的节点记入 cpu_pins_over（调度段保留
                # fit=false 的调度结果，容量类阻断走 force 通道而非拦死在第二步）
                counts: dict[str, int] = {}
                for nid in pinned_nodes:
                    counts[nid] = counts.get(nid, 0) + 1
                fact_by_id = {n.node_id: n for n in c.lane_node_resources}
                for nid in sorted(counts):
                    # 指定节点必在求解域内（离线指定已整域判红），缺键直接 KeyError 暴露
                    cpu_ok, cpu_reason = assess_cpu_capacity(
                        [NodeCandidate(node_id=nid, memory_gb=fact_by_id[nid].memory_gb)],
                        replicas=counts[nid],
                        ram_per_replica_gb=lane.ram_gb,
                        ledger=get_orchestrator().ledger,
                    )
                    if not cpu_ok:
                        c.cpu_pins_over.add(nid)
                        c.fail(
                            f"节点 {nid} 内存装不下指定的 {counts[nid]} 份副本"
                            f"（每份约 {lane.ram_gb:.1f}GB）：{cpu_reason or '内存不足'}"
                        )
            else:
                cpu_ok, cpu_reason = assess_cpu_capacity(
                    # memory_gb 是节点**总内存**（账本按 total×利用率上限 − 预留 判可用）
                    [NodeCandidate(node_id=n.node_id, memory_gb=n.memory_gb) for n in c.lane_node_resources],
                    replicas=max(1, int(topo.replicas or 1)),
                    ram_per_replica_gb=lane.ram_gb,
                    # 扣掉账本内存预留：别的部署已规划占用但容器未起时实时 used 还是 0，
                    # 只看实况会给出 plan_cpu_placement 兑现不了的调度结果
                    ledger=get_orchestrator().ledger,
                )
                if not cpu_ok:
                    c.fail(cpu_reason or "内存不足")
        else:
            # 全量上卡档：显存侧按逐卡口径复核。此分支覆盖「allow_cpu 请求但解出显存部署」
            # ——该场景不走下面的自动调度容量校验（cpu_lane_request 会把它挡掉）。
            # 复核与创建路径 card_fits 同一双闸（reserve=util×total：need ≤ reserve
            # 且物理/账本可用装得下 reserve），不是「need ≤ min(两池)」的单侧比较
            # ——卡被平台外进程占超 (1−util)×total 时，need 装得下池却装不下
            # reserve，单侧比较会给出「预检绿灯、create 报无可用卡」
            util = adapter.memory_utilization_from(request.engine_params)
            lane_gpus = [
                cg
                for cg in c.cluster_gpus
                if cg.status == GpuStatus.ONLINE
                and cg.arch in adapter.offload_gpu_archs()
                and (pinned_node is None or cg.node_id == pinned_node)
            ]
            fits = [
                cg
                for cg in lane_gpus
                if physical_fits(cg, (reserve := card_reserve(cg, util)), lane.vram_gb)
                and reserve_fits(
                    reserve, lane.vram_gb, get_orchestrator().ledger.get_available(cg.uuid, cg.total_vram_gb)
                )
            ]
            if not fits:
                best_card_gb = max((cand.vram_avail_gb for cand in candidates), default=0.0)
                need_gb = lane.vram_gb
                reserve_gb = max((card_reserve(cg, util) for cg in lane_gpus), default=0.0)
                c.fail(
                    f"模型每卡需 {need_gb:.1f}GB（按占比 {util:g} 折算预留 {reserve_gb:.1f}GB），"
                    f"集群单卡最大可用 {best_card_gb:.1f}GB，装不下（可提高 tp、降低显存占比或改走纯 CPU 部署）"
                )
    elif c.lane_node_resources and request.offload_preference == resource.SCENARIO_CPU_ONLY:
        # 显式要纯 CPU 却无解：明确报不可部署（与编排期同口径，不悄悄改用显存档）。
        # 求解域有候选才报（离线指定已报过「不在线」，node_resources 全量不再作此判据）
        c.fail("目标节点内存装不下纯 CPU 部署（权重 + KV + 系统保留超过可用内存）")


async def _warn_cross_subnet(db: AsyncSession, c: InspectState) -> None:
    """多机跨子网提醒（仅 warning 不阻断；create 第一遍硬拒，force 可绕）。

    可能追加 warning：跨 /N 子网 NCCL 可达性风险。
    """
    if not (
        c.request.engine
        and c.request.topology
        and max(1, int(c.topo.nnodes or 1)) > 1
        and c.cluster_id
        and c.cluster_gpus
    ):
        return
    sel = c.request.gpu_selector
    candidate_nodes: set = set()
    if sel and sel.node_ids:
        candidate_nodes = set(sel.node_ids)
    elif sel and sel.gpu_uuids:
        candidate_nodes = {cg.node_id for cg in c.cluster_gpus if cg.uuid in sel.gpu_uuids}
    else:
        # 自动调度：候选 = 集群全部在线节点（粗判，实际调度后才定稿）
        candidate_nodes = {cg.node_id for cg in c.cluster_gpus if cg.status == GpuStatus.ONLINE}
    if candidate_nodes:
        node_ips = await deployment_service.load_node_ips(db, candidate_nodes)
        warn = subnet_warning(node_ips, settings.multinode_same_subnet_prefix)
        if warn:
            c.warn(warn)


def _validate_selection(c: InspectState) -> None:
    """手动选卡 / 选节点校验：arch 推断 + 结构规则 + 容量预检（VRAM 权威判定留给 create）。

    可能追加 issue：所选卡架构不支持拓扑、手选结构违规（卡数/同档/分布）、手选容量
    三分支（装不下模型 > 平台内占用 > 平台外进程）、选节点集违规。
    """
    request, adapter, topo = c.request, c.adapter, c.topo
    c.sel_uuids = request.gpu_selector.gpu_uuids if request.gpu_selector else None
    # 手选卡 arch 推断（与 orchestrator _plan 共用 infer_selection_arch）：部署期按所选卡
    # arch 调度（cann 卡→cann），inspect 若硬用默认 arch 会把合法的 cann 选卡误拒成「需
    # nvidia」——故校验一律走本推断值。
    validation_arch = adapter.default_arch
    if c.sel_uuids and c.cluster_gpus:
        validation_arch = infer_selection_arch(c.cluster_gpus, c.sel_uuids, adapter.default_arch)

    # 手动选卡校验（在线/同 arch/分布/多副本分组；VRAM 留给 create 的 scheduler.reserve）
    if c.sel_uuids and c.cluster_gpus:
        # 所选卡架构服务不了本拓扑 → 创建期就拒（如手选 Ascend 卡 + pp>1；内核不按 arch
        # 过滤手选集，只能在这里拦，否则要等占完卡、落完库才在容器阶段失败）。
        # 判据与 create/_plan 同源（adapter.assert_arch_serves_topology），此处转软提示
        try:
            adapter.assert_arch_serves_topology(validation_arch, topo)
        except LLMPodValidationError as e:
            c.fail(str(e))

        try:
            # 逐份形状（per_copy_units 对已归一化拓扑恒非空），同形/异构分流在
            # validate_manual_gpu_selection——与 create 的 assign 共用同一入口
            shapes = [
                (max(1, int(u.tp or 1)), max(1, int(u.pp or 1)), max(1, int(u.nnodes or 1)))
                for u in topo.per_copy_units()
            ]
            validate_manual_gpu_selection(c.cluster_gpus, c.sel_uuids, shapes=shapes, arch=validation_arch)
        except LLMPodValidationError as e:
            c.fail(str(e))

        # 手选卡的容量预检（权威硬拒在 create，此处给前端预览）。判据与 create 端
        # scheduler.card_fits 同一条 reserve_fits、同一平台默认口径（本次预留 = 整卡
        # 0.9×total），双闸取严：心跳实况（含平台外进程）+ 平台内声明占用后的余量
        # （含已规划未起的部署）。每卡需求按份内并行度（tp×pp）摊，异构副本取最保守份
        # （配对前不知卡归哪份，宁可预警不漏报）。消息按最具体的原因分流，一张卡至多一条：
        # 装不下模型 > 平台内占用（带占用方名字）> 平台外进程。
        # per_copy_units() 对已归一化的 Topology 恒非空（归一化拒绝空 units/copies）
        per_card_need = max(
            c.vram_per_gpu / (max(1, int(u.tp or 1)) * max(1, int(u.pp or 1))) for u in topo.per_copy_units()
        )
        for cg in c.cluster_gpus:
            if cg.uuid not in c.sel_uuids:
                continue
            reserve = card_reserve(cg, None)  # 平台默认口径（整卡），不看用户显存占比
            ledger_avail = cg.total_vram_gb * adapter.default_gpu_memory_utilization - cg.declared_vram_gb
            physical_ok = reserve_fits(reserve, per_card_need, cg.available_vram_gb)
            ledger_ok = reserve_fits(reserve, per_card_need, ledger_avail)
            if physical_ok and ledger_ok:
                continue
            if reserve + 1e-6 < per_card_need:
                c.fail(
                    f"GPU {cg.uuid[:8]} 按平台默认显存占比 "
                    f"{adapter.default_gpu_memory_utilization:.0%} 只能预留 {reserve:.1f}GB，"
                    f"装不下本部署每卡需求 {per_card_need:.1f}GB；请提高 tp 或换更大显存的卡"
                )
            elif not ledger_ok and cg.occupied_by:
                names = "、".join(o.deployment_name for o in cg.occupied_by)
                c.fail(
                    f"GPU {cg.uuid[:8]} 被 部署 {names} 占用 {cg.occupied_pct:.0%}，剩余显存装不下"
                    "本部署的默认预留；如确要共用显存，确认部署时在引擎参数调低显存占比"
                )
            else:
                c.fail(
                    f"GPU {cg.uuid[:8]} 实时可用显存不足：可用 {cg.available_vram_gb:.1f}GB < "
                    f"需预留 {reserve:.1f}GB（卡上已有占用，可能是平台外进程）；请换卡或确认占用已释放"
                )

    # 选节点集校验（每节点在线 + 同 arch + ≥ tp×pp//nnodes 张可用）；显式 cpu_only 的
    # node_ids 是逐副本指定（无卡语义），GPU 侧卡数校验不适用（跳过）
    if (
        request.gpu_selector
        and request.gpu_selector.node_ids
        and c.cluster_gpus
        and request.offload_preference != resource.SCENARIO_CPU_ONLY
    ):
        try:
            _validate_node_selection(
                c.cluster_gpus,
                request.gpu_selector.node_ids,
                tp=max(1, int(topo.tp or 1)),
                nnodes=max(1, int(topo.nnodes or 1)),
                arch=validation_arch,
                pp=max(1, int(topo.pp or 1)),
            )
        except LLMPodValidationError as e:
            c.fail(str(e))


def _build_node_support(c: InspectState) -> None:
    """逐节点可选性（引擎 + 镜像 tag 视角，前端节点选择禁用用）。

    填充：node_support。节点可选 ⇔ 存在一张「引擎可用」的卡（grey_reason=None：
    online + arch 支持 + 有匹配镜像）。allow_cpu 下内存放得下自身档位的节点也算可选。
    """
    if not c.node_rows:
        return
    hostnames = {n.id: (n.hostname or "") for n in c.node_rows}
    total_ram_by_node = {n.id: float(n.memory_gb or 0.0) for n in c.node_rows}
    # 可用内存取全量节点资源（展示口径）——lane_node_resources 已被指定收窄，拿它会把
    # 未指定节点显示成 0 可用（前端据此判「内存不足」）。口径走 _available_ram（账本
    # get_available_ram = total×利用率上限 − 已预留，与容量判定/调度 binpack 共用的
    # 那条单一口径）；节点资源的 avail_ram_gb 是实时口径（total − 心跳已用，无折让、
    # 不含别家预留），拿它展示会出现「卡片绿 + 判定红」的常态分叉
    ledger = get_orchestrator().ledger
    avail_ram_by_node = {n.node_id: _available_ram(n, ledger) for n in c.node_resources}
    # 单遍按节点分桶（灰化原因集 + 是否存在可选卡），免每节点重扫 cluster_gpus
    greyed_by_node: dict = {}
    has_ok_by_node: dict = {}
    for cg in c.cluster_gpus:
        if cg.grey_reason:
            greyed_by_node.setdefault(cg.node_id, set()).add(cg.grey_reason)
        else:
            has_ok_by_node[cg.node_id] = True
    for node_id, hostname in hostnames.items():
        selectable = has_ok_by_node.get(node_id, False)
        # allow_cpu：无可用卡但内存放得下该节点自身的可行档位 → 也算可选（纯 CPU / 内存主力）。
        # 必须逐节点重算：集群级推荐档位是按最优节点算出来的，拿它比另一个节点会误判
        # （A 节点算出的 60GB 档位去比只有 40GB 的 B → B 被错标可选，选完调度才抛错）。
        cpu_lane = False
        if not selectable and c.memory is not None:
            # 逐节点结论直接取测算算好的那张表（同一候选池、同一折让口径）：重算一遍
            # 就是两套口径的起点——此前这里用 `vram_by_node`（账本已折）配默认 headroom
            # 再折一次，同一节点在集群级与逐节点结论里得到两个容量
            node_est = c.memory_by_node.get(node_id)
            if node_est is not None:
                selectable = True
                cpu_lane = resource.is_cpu_only(node_est)
        reason = None
        if not selectable:
            greyed = greyed_by_node.get(node_id) or set()
            if not greyed:
                reason = "该节点无 GPU"
            elif greyed <= {"offline"}:
                reason = "节点 GPU 全部离线"
            elif "no_image" in greyed:
                reason = "节点 GPU 无匹配镜像（当前引擎/镜像 tag）"
            elif "arch_mismatch" in greyed:
                reason = "节点 GPU 架构不被该引擎支持"
            else:
                reason = "节点无可用 GPU"
        c.node_support.append(
            NodeSupportInfo(
                node_id=node_id,
                hostname=hostname,
                selectable=selectable,
                reason=reason,
                memory_gb=round(total_ram_by_node.get(node_id, 0.0), 2),
                available_memory_gb=round(avail_ram_by_node.get(node_id, 0.0), 2),
                cpu_lane=cpu_lane,
            )
        )
    c.node_support.sort(key=lambda ns: (not ns.selectable, ns.node_id))


def _resolve_target_arch(c: InspectState) -> None:
    """解析目标 arch / GPU 型号 / 驱动（容量校验 + dry-run + 推荐共用）。

    填充：arch / arch_candidates / target_gpu_model / target_driver / cpu_only / manual。
    手选卡：取首卡 arch（cann 卡 → cann 部署）+ name + driver_version（catalog 精确匹配）；
    自动调度：取集群内首个引擎支持的在线 arch（全 Ascend 集群 → cann 而非默认 nvidia）。
    node_ids 选节点仍走默认 arch（无手选卡部署期 arch=默认，与 scheduler 口径一致）。
    可调度架构序（arch_candidates）只在自动路径需要回落；手选卡即固定架构。
    """
    adapter, topo = c.adapter, c.topo
    if c.sel_uuids and c.cluster_gpus:
        picked = next((cg for cg in c.cluster_gpus if cg.uuid in c.sel_uuids), None)
        c.arch = picked.arch if picked else adapter.default_arch
        # 手选也要给单元素序：空序在调度内核里是「上游架构闸门淘汰了全部候选」的否决信号，
        # 推荐调度会整片回空，而同一响应里 replica_plans 还在描述选集（有推荐卡、无推荐调度）
        c.arch_candidates = [c.arch]
        c.target_gpu_model = picked.name if picked else None
        c.target_driver = picked.driver_version if picked else None
    else:
        c.arch = adapter.default_arch
        node_based = bool(
            c.request.gpu_selector and (c.request.gpu_selector.node_id or c.request.gpu_selector.node_ids)
        )
        # 可调度架构序：适配器声明「本拓扑服务得了（如 cann 不支持 pp>1）+ 集群确有匹配
        # 镜像的卡」——与 create(_plan)/扩容同一入口，避免预检说能落 cann、create 落不到
        c.arch_candidates = (
            adapter.schedulable_archs(
                topo,
                gpu_pool=c.cluster_gpus,
                engine_version=c.request.image_tag,
                explicit_image=c.request.image,
            )
            if c.cluster_gpus
            else []
        )
        if not node_based and c.arch_candidates:
            c.arch = c.arch_candidates[0]
        c.target_gpu_model = None
        c.target_driver = None

    c.cpu_only = resource.is_cpu_only(c.memory)
    c.manual = bool(c.sel_uuids or (c.request.gpu_selector and c.request.gpu_selector.node_ids))
    if c.cpu_only:
        # 纯 CPU：无卡（arch 置 cpu，镜像按 cpu 变体解析，与编排器 _plan 同口径）
        c.arch = "cpu"


def _dry_run_resolve(c: InspectState) -> None:
    """dry-run 三层解析回显 resolved（传了 engine+topology 即产）。

    填充：resolved。可能追加 warning：多机 rendezvous 占位说明；issue（阻断）：
    镜像解析失败、适配器组装期结构硬拒。
    """
    if not (c.request.engine and c.request.topology):
        return
    request, adapter, topo = c.request, c.adapter, c.topo
    # 占位 InstanceSpec：不落库、不调度，仅供 assemble_args/assemble_env 三层合并
    multi_node = max(1, int(topo.nnodes or 1)) > 1
    # 层数回显：只在两端（0=纯 CPU / 999=全量上卡），与编排器取解同口径
    lane_fields: dict = {
        "offload_layers": 0 if c.cpu_only else resource.NGL_ALL,
        # 纯 CPU：无卡（arch 已在 _resolve_target_arch 置 "cpu"，镜像按 cpu 变体解析）
        "assigned_gpus": [] if c.cpu_only else None,
    }
    inst = InstanceSpec(
        model_path=request.model_path or f"<model-{request.model_id}>",
        model_architecture=c.model.architecture,
        served_model_name=None,
        port=adapter.default_port,
        task=adapter.task_for(c.model.model_type),
        gpu_arch=c.arch,
        tp=max(1, int(topo.tp or 1)),
        pp=max(1, int(topo.pp or 1)),
        nnodes=max(1, int(topo.nnodes or 1)),
        node_rank=0,
        master_addr="<master-addr>" if multi_node else None,
        master_port=adapter.default_master_port if multi_node else None,
        vram_percent=c.util,
        target_gpu_model=c.target_gpu_model,
        target_driver_version=c.target_driver,
        **lane_fields,
    )
    image_ref: str | None = None
    entry = None
    try:
        image_ref, entry = image_catalog.resolve_image_ref(
            engine=c.engine,
            arch=c.arch,
            gpu_model=c.target_gpu_model,
            driver_version=c.target_driver,
            engine_version=request.image_tag,
            explicit_image=request.image,
        )
    except LLMPodValidationError:
        # 镜像解析失败（驱动/版本不兼容）不阻断 inspect 预览：降级 resolved.image=None + 明确提示
        # （含目标驱动版本）。真正落库（create）的硬校验在编排期 build_run_spec 兜底。
        c.fail(
            image_catalog.explain_unresolved(
                engine=c.engine,
                arch=c.arch,
                gpu_model=c.target_gpu_model,
                driver_version=c.target_driver,
                engine_version=request.image_tag,
            )
        )
    # 适配器在参数组装期也会给结构级硬拒（如 Ascend 不支持 pp>1）——转成 issue
    # 呈现，不让单个引擎规则把整个预检 422 掉（用户改完参数还得能继续预览）
    final_args: list = []
    try:
        final_args = adapter.assemble_args(inst, topo, entry, request.engine_params)
    except LLMPodValidationError as e:
        c.fail(str(e))
    overlap = check_protected_overlap(c.engine, (request.engine_params or {}).get("extra_args"))
    # env 只回显用户自定义项，系统/镜像层注入不回显。前端把 resolved.env 整表预填进
    # 环境变量编辑器、create 时原样回传——回传即升为用户层，系统注入值（按调度到的节点
    # 定稿的 SOCKET_IFNAME、随健康检查联动的 VLLM_ENGINE_READY_TIMEOUT_S 等）会被
    # 固化成快照，跨环境部署时网卡名对不上直接把引擎打崩（gloo 找不到 eth0）。
    user_env = dict((request.engine_params or {}).get("env") or {})
    c.resolved = ResolvedSpec(
        image=image_ref,
        args=final_args,
        env=user_env,
        protected_overlap=overlap,
        gpu_memory_utilization=c.util,
    )
    if multi_node:
        c.warn("多机 rendezvous 参数（--master-addr/--node-rank/--master-port）为占位值，部署期按实际调度节点定稿")


def _attribute_all_grey(c: InspectState) -> None:
    """集群有卡但无任何可选卡 → 给明确原因（前端据此提示，避免「推荐为空」无解释）。

    可能追加 issue（阻断级）：no_image（带人读详情优先）/ 全离线 / 全架构不符 / 兜底。
    dry-run 已因 resolved.image=None 报镜像缺失时不重复；按灰化原因全集归因——覆盖
    「通用解析成功但真实卡型号/驱动不满足」（如 nvidia 驱动 <590 配 image_tag=0.27.1）
    与全 arch_mismatch / 全离线集群，三场景语义一致。
    """
    cpu_only_intent = c.request.offload_preference == resource.SCENARIO_CPU_ONLY
    if (
        c.cluster_gpus
        and not any(cg.grey_reason is None for cg in c.cluster_gpus)
        and not (c.cpu_only or cpu_only_intent)
    ):
        no_image_cards = [cg for cg in c.cluster_gpus if cg.grey_reason == "no_image"]
        if no_image_cards and (c.resolved is None or c.resolved.image is not None):
            detail = next((cg.grey_detail for cg in no_image_cards if cg.grey_detail), None)
            c.fail("当前引擎/镜像选择对集群全部 GPU 不可用" + (f"：{detail}" if detail else ""))
        elif not no_image_cards:
            greyed = {cg.grey_reason for cg in c.cluster_gpus if cg.grey_reason}
            if greyed <= {"offline"}:
                c.fail("集群 GPU 全部离线，无可选卡")
            elif "arch_mismatch" in greyed:
                c.fail(f"集群 GPU 架构均不被引擎 {c.engine} 支持")
            else:
                c.fail("集群无可用 GPU")


def _build_response(c: InspectState) -> InspectDeploymentResponse:
    """纯投影组装响应，不再判定。"""
    return InspectDeploymentResponse(
        deployable=c.deployable,
        issues=c.issues,
        warnings=c.warnings,
        recommended_cluster_id=c.cluster_id,
        cross_node=c.cross_node,
        recommendation=Recommendation(
            engine=c.recommendation.engine,
            image=c.recommendation.image,
            topology=Topology(**c.recommendation.topology),
            vram_per_gpu_gb=c.recommendation.vram_per_gpu_gb,
            # 版本感知的有效词表（如 SGLang 0.4.2/0.4.6 无 --pp-size → 不含 pp）——
            # pp 门控与 SGLang 组装期硬拒同源，防前端绿灯、提交后才炸
            supported_parallel=c.adapter.supported_parallel_at(c.request.image_tag),
            # 版本感知门控（引擎声明 ∧ 固定镜像词表）——旧 vllm 镜像多机词表 false 时
            # 回显 False，前端不放 nnodes 控件；判据与 vLLM 组装期硬拒同源
            supports_multinode=c.adapter.supports_multinode_at(c.request.image_tag),
        ),
        cluster_gpus=c.cluster_gpus,
        node_support=c.node_support,
        recommended_placement=c.recommended_placement,
        preview_topology=c.preview,
        resolved=c.resolved,
        memory=c.memory,
    )
