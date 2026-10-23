"""遥测服务（官网遥测端点，schema 2）。

每日向官网发送一次本实例的遥测汇总：环境 / 规模 / 模型结构标识 /
引擎 / 特性计数。**静默执行**——全模块零日志，发送成败、tick 异常一律
吞掉，靠下一小时 tick 自然重试（契约：失败不重试指单次失败不立即重发，
当日内允许下一轮再试）。payload 契约::

    POST https://llmpod.ai/api/telemetry/report   （无鉴权，成功恒 204）
    {
      "schema": 2,
      "instance_id": "...",           # 首次启动生成，32 hex，存 SystemConfig
      "version": "26.9.20",
      "installed_at": "...",          # 首代定格，永不改写
      "reported_at": "...",           # 实例本地时间；站点按其日期做幂等键
      "env": {"os": ..., "arch": ..., "python": ...},
      "scale": {"cluster_count", "node_count", "gpu_by_model", "vram_total_gb"},
      "models": [{architecture, kind, format, quant, size_gb, deployed}],
      "engines": {"vllm": 3, ...},
      "features": {deployment/multi_replica/cpu_offload/route/api_key/endpoint}
    }

隐私口径：``models[]`` 六键全部来自模型文件自带的结构标识，**用户给模型
起的显示名（``Model.name``）永不进遥测数据**。站点校验：instance_id 8–64
字符、version ≤32、reported_at 与站点日期差 ±2 天外 422、遥测体 ≤512KB；
幂等键 = instance_id + reported_at 当地日期，同日重复遥测只刷新当日行。

节奏：每小时 tick 一次，「今日（本地日期）尚未成功遥测」才构建并发送，
成功后把当日日期落库；站点幂等键与客户端门控正好一天一行。
"""

from __future__ import annotations

import asyncio
import json
import platform
import secrets
from datetime import date, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import llmpod
from llmpod.config.settings import settings
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.models.api_key import ApiKey
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.node import Node
from llmpod.models.route import Route
from llmpod.models.system_config import SystemConfig
from llmpod.utils.time import now_local

# 官网遥测端点（产品级固定值，随代码发版变更；不做部署配置——与更新检查
# 端点同风格）
TELEMETRY_REPORT_URL = "https://llmpod.ai/api/telemetry/report"

# SystemConfig 存储 key；value 形状见 _state_value
TELEMETRY_CONFIG_KEY = "telemetry_instance"

TELEMETRY_SCHEMA_VERSION = 2
TELEMETRY_TICK_SECONDS = 3600.0  # 每小时检查一次当日是否已报
TELEMETRY_SEND_TIMEOUT_SECONDS = 10.0  # 单次 POST 超时
TELEMETRY_MAX_BODY_BYTES = 512 * 1024  # 站点遥测体上限


# ---------------------------------------------------------------------------
# payload 模型（schema 2 镜像）
# ---------------------------------------------------------------------------


class TelemetryEnv(BaseModel):
    """运行环境三元组。"""

    os: str = ""
    arch: str = ""
    python: str = ""


class TelemetryScale(BaseModel):
    """规模汇总。gpu_by_model 按显卡型号名计数；vram 单位 GB。"""

    cluster_count: int = 0
    node_count: int = 0
    gpu_by_model: dict[str, int] = Field(default_factory=dict)
    vram_total_gb: float = 0.0


class TelemetryModelEntry(BaseModel):
    """models[] 条目。六键全部来自模型文件自带的结构标识，显示名永不进 payload。"""

    architecture: str = ""
    kind: str = ""
    format: str = ""
    quant: str = ""
    size_gb: float = 0.0
    deployed: bool = False


class TelemetryFeatures(BaseModel):
    """特性使用计数。"""

    deployment_count: int = 0
    multi_replica_count: int = 0
    cpu_offload_count: int = 0
    route_count: int = 0
    api_key_count: int = 0
    endpoint_count: int = 0


class TelemetryPayload(BaseModel):
    """遥测体顶层。schema 字段经序列化别名输出为 "schema"。"""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(default=TELEMETRY_SCHEMA_VERSION, serialization_alias="schema")
    instance_id: str
    version: str
    installed_at: datetime
    reported_at: datetime
    env: TelemetryEnv = Field(default_factory=TelemetryEnv)
    scale: TelemetryScale = Field(default_factory=TelemetryScale)
    models: list[TelemetryModelEntry] = Field(default_factory=list)
    engines: dict[str, int] = Field(default_factory=dict)
    features: TelemetryFeatures = Field(default_factory=TelemetryFeatures)


# ---------------------------------------------------------------------------
# 纯函数：kind 推导 / env
# ---------------------------------------------------------------------------


def derive_kind(model_type: str, is_vision: bool) -> str:
    """模型类别：llm 且带视觉升格 multimodal，其余原样直传。"""
    if model_type == "llm" and is_vision:
        return "multimodal"
    return model_type or ""


def collect_env() -> TelemetryEnv:
    """运行环境三元组（os 统一小写，如 linux/darwin/windows）。"""
    return TelemetryEnv(
        os=platform.system().lower(),
        arch=platform.machine(),
        python=platform.python_version(),
    )


# ---------------------------------------------------------------------------
# 采集（AsyncSession 首参；查询形状保持平铺列 + Python 侧分组，无 join）
# ---------------------------------------------------------------------------


async def collect_scale(db: AsyncSession) -> TelemetryScale:
    """规模：集群/节点计数 + 显卡按型号分组与显存合计。"""
    cluster_count = (await db.execute(select(func.count(Cluster.id)))).scalar()
    node_count = (await db.execute(select(func.count(Node.id)))).scalar()
    # GPU 行数是单机规模（几十~几百），平铺拉回 Python 侧分组 + 累加
    gpu_rows = (await db.execute(select(Gpu.name, Gpu.total_vram_gb))).all()
    gpu_by_model: dict[str, int] = {}
    vram_total_gb = 0.0
    for name, vram in gpu_rows:
        gpu_by_model[name] = gpu_by_model.get(name, 0) + 1
        vram_total_gb += float(vram or 0)
    return TelemetryScale(
        cluster_count=int(cluster_count or 0),
        node_count=int(node_count or 0),
        gpu_by_model=gpu_by_model,
        vram_total_gb=round(vram_total_gb, 2),
    )


async def _deployed_model_ids(db: AsyncSession) -> set[int]:
    """有部署的 model_id 集合。"""
    rows = (await db.execute(select(ModelDeployment.model_id))).scalars()
    return {int(model_id) for model_id in rows}


async def collect_models(db: AsyncSession) -> list[TelemetryModelEntry]:
    """模型结构标识清单（逐行显式构造；Model.name 永不进 payload）。"""
    deployed_ids = await _deployed_model_ids(db)
    models = (await db.execute(select(Model))).scalars()
    entries: list[TelemetryModelEntry] = []
    for m in models:
        entries.append(
            TelemetryModelEntry(
                architecture=m.architecture or "",
                kind=derive_kind(m.model_type, m.is_vision),
                format=m.format or "",
                quant=(m.quantization or "").lower(),
                size_gb=float(m.size_gb or 0),
                deployed=m.id in deployed_ids,
            )
        )
    return entries


async def collect_engines(db: AsyncSession) -> dict[str, int]:
    """引擎 → 部署行数（平铺拉回 Python 侧分组）。"""
    rows = (await db.execute(select(ModelDeployment.engine))).scalars()
    engines: dict[str, int] = {}
    for engine in rows:
        engines[engine] = engines.get(engine, 0) + 1
    return engines


async def collect_features(db: AsyncSession, deployment_count: int) -> TelemetryFeatures:
    """特性计数。deployment_count 由 engines 合计传入（一次扫描两用）。"""
    replica_rows = (await db.execute(select(ModelDeploymentReplica.deployment_id))).scalars()
    replicas_per_deployment: dict[str, int] = {}
    for deployment_id in replica_rows:
        replicas_per_deployment[deployment_id] = replicas_per_deployment.get(deployment_id, 0) + 1
    multi_replica_count = 0
    for n in replicas_per_deployment.values():
        if n >= 2:
            multi_replica_count += 1

    # CPU 形态部署数：纯 CPU 部署不落 GPU 分配行（DeploymentGpuAssignment 只写
    # nvidia/cann），形态标记在 Instance.spec 的 gpu_arch=="cpu"（同一部署多实例只计一次）
    instance_rows = (await db.execute(select(Instance.deployment_id, Instance.spec))).all()
    cpu_deployments = set()
    for deployment_id, spec in instance_rows:
        if deployment_id is not None and isinstance(spec, dict) and spec.get("gpu_arch") == "cpu":
            cpu_deployments.add(deployment_id)
    cpu_offload_count = len(cpu_deployments)

    route_count = (await db.execute(select(func.count(Route.id)))).scalar()
    api_key_count = (await db.execute(select(func.count(ApiKey.id)))).scalar()
    endpoint_count = (await db.execute(select(func.count(ModelEndpoint.id)))).scalar()
    return TelemetryFeatures(
        deployment_count=deployment_count,
        multi_replica_count=multi_replica_count,
        cpu_offload_count=cpu_offload_count,
        route_count=int(route_count or 0),
        api_key_count=int(api_key_count or 0),
        endpoint_count=int(endpoint_count or 0),
    )


async def build_payload(db: AsyncSession, state: TelemetryInstanceState, reported_at: datetime) -> TelemetryPayload:
    """编排全部采集段为一份遥测数据。reported_at 由调用方传实例本地时间。"""
    engines = await collect_engines(db)
    return TelemetryPayload(
        instance_id=state.instance_id,
        version=llmpod.__version__,
        installed_at=state.installed_at,
        reported_at=reported_at,
        env=collect_env(),
        scale=await collect_scale(db),
        models=await collect_models(db),
        engines=engines,
        features=await collect_features(db, deployment_count=sum(engines.values())),
    )


# ---------------------------------------------------------------------------
# 实例标识（SystemConfig）
# ---------------------------------------------------------------------------


class TelemetryInstanceState(BaseModel):
    """遥测实例标识。instance_id 首次启动生成后不变；installed_at 同步定格。"""

    instance_id: str
    installed_at: datetime
    last_report_date: str = ""  # 最近成功遥测的本地日期（isoformat；""=从未）


def _state_value(state: TelemetryInstanceState) -> dict:
    return {
        "instance_id": state.instance_id,
        "installed_at": state.installed_at.isoformat(),
        "last_report_date": state.last_report_date,
    }


async def ensure_instance_state(db: AsyncSession) -> TelemetryInstanceState:
    """读取（缺失/损坏则生成或修复并落库）遥测实例标识。

    行内容可能被通用 SystemConfig 配置接口改坏（value 非 dict、instance_id
    不在站点 8–64 限长内、installed_at 不可解析）——损坏即修复落库：
    instance_id 有效则保留（身份不变，只修安装时间），无效则重新生成并清空
    日期记忆；不做只读兜底，否则坏值每小时以当刻时间反复外发、永不收敛。
    """
    stmt = select(SystemConfig).where(SystemConfig.key == TELEMETRY_CONFIG_KEY)
    config = (await db.execute(stmt)).scalar_one_or_none()
    value = config.value if config is not None and isinstance(config.value, dict) else None
    instance_id = str(value.get("instance_id") or "") if value else ""
    installed_at = None
    if value is not None:
        try:
            installed_at = datetime.fromisoformat(str(value.get("installed_at") or ""))
        except ValueError:
            installed_at = None
    id_valid = 8 <= len(instance_id) <= 64
    if id_valid and installed_at is not None:
        return TelemetryInstanceState(
            instance_id=instance_id,
            installed_at=installed_at,
            last_report_date=str(value.get("last_report_date") or ""),
        )
    # 首次（行缺失）或内容损坏：缺啥补啥后落库
    state = TelemetryInstanceState(
        instance_id=instance_id if id_valid else secrets.token_hex(16),
        installed_at=installed_at or now_local(),
        last_report_date=(str(value.get("last_report_date") or "") if id_valid else ""),
    )
    if config is not None:
        config.value = _state_value(state)  # 整体重赋值（JSONB 变更检测）
    else:
        db.add(
            SystemConfig(
                key=TELEMETRY_CONFIG_KEY,
                value=_state_value(state),
                description="遥测实例标识与最近成功遥测日期",
            )
        )
    await db.flush()
    return state


async def mark_report_sent(db: AsyncSession, instance_id: str, day: date) -> None:
    """成功遥测后落当日日期（幂等键的客户端侧记忆）。"""
    stmt = select(SystemConfig).where(SystemConfig.key == TELEMETRY_CONFIG_KEY)
    config = (await db.execute(stmt)).scalar_one_or_none()
    if config is None:
        return  # 防御：ensure 已建行，理论不可达
    value = dict(config.value or {})
    value["instance_id"] = instance_id
    value["last_report_date"] = day.isoformat()
    config.value = value  # 整体重赋值，原地改键 SQLAlchemy 不感知
    await db.flush()


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


def payload_body(payload: TelemetryPayload) -> bytes:
    """序列化遥测体（键名走别名，时间带时区偏移 ISO）。"""
    return json.dumps(payload.model_dump(by_alias=True, mode="json"), ensure_ascii=False).encode()


async def send_report(payload: TelemetryPayload) -> bool:
    """POST 遥测数据到官网端点。成功 True；失败/超限静默返回 False（不抛、不重试）。"""
    body = payload_body(payload)
    if len(body) > TELEMETRY_MAX_BODY_BYTES:
        # 超限换副本丢 models 保心跳（截断前缀比空列表更污染统计，也不改入参）；
        # 仍超限则放弃本轮
        payload = payload.model_copy(update={"models": []})
        body = payload_body(payload)
        if len(body) > TELEMETRY_MAX_BODY_BYTES:
            return False
    try:
        async with httpx.AsyncClient(timeout=TELEMETRY_SEND_TIMEOUT_SECONDS) as client:
            response = await client.post(
                TELEMETRY_REPORT_URL,
                content=body,
                headers={
                    # httpx 对 content=bytes 不自动补 Content-Type；站点是
                    # FastAPI pydantic body，缺头按非 JSON 处理恒 422
                    "Content-Type": "application/json",
                    "User-Agent": f"llmpod/{llmpod.__version__}",
                },
            )
    except httpx.HTTPError:
        return False
    return response.is_success


# ---------------------------------------------------------------------------
# 每日门控 + 周期任务（挂管理端 lifespan）
# ---------------------------------------------------------------------------


async def report_if_due(*, force: bool = False) -> None:
    """单次检查：当日未成功遥测才构建并发送；成功落当日日期。

    force=True（启动首轮）跳过门控直发且**不落日期**——不吃当天的遥测额度，
    当天后续 tick 仍会正常再发一次并落日期。进程每次启动都直发一次（重启
    多次即多发多次，站点按日幂等，同日重复只是刷新当日行）。
    """
    from llmpod.db.pg import get_db_context

    # 门控日期 / 遥测时间 / 落库日期共用同一读钟：跨本地午夜触发的 tick
    # 不会把次日日期写上而跳过整个次日
    now = now_local()
    async with get_db_context() as db:
        state = await ensure_instance_state(db)
        if not force and state.last_report_date == now.date().isoformat():
            return
        payload = await build_payload(db, state, reported_at=now)

    sent = await send_report(payload)  # 网络调用不占 DB 会话
    if not sent or force:
        return  # 失败或直发都不落日期 → 下一 tick 仍按门控正常遥测
    async with get_db_context() as db:
        await mark_report_sent(db, state.instance_id, payload.reported_at.date())


async def run_telemetry_loop() -> None:
    """每小时检查一次当日是否已报；启动首轮直发（不吃当日门控）。"""
    first = True
    while True:
        try:
            await report_if_due(force=first)
        except Exception:
            pass  # 静默：DB/网络故障靠下一 tick 自愈；CancelledError 不在捕获范围
        first = False
        await asyncio.sleep(TELEMETRY_TICK_SECONDS)


async def start_telemetry_task(background_tasks: BackgroundTaskManager) -> None:
    """启动遥测周期监督任务（control_plane_lifespan 调）。开关关闭时不注册。"""
    if not settings.telemetry_enabled:
        return
    background_tasks.start(run_telemetry_loop, name="telemetry-report")
