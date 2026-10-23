"""task_event → 领域投影注册表（去中心化分发）。

task_ingest 落完 task/task_event 后，按 ``resource_type`` 把事件分发给各领域投影
（node_model、deploy_log）。**新增任务类型 = 写 executor + 写 projector + ``register``
一行**，不碰 task_ingest 主体——取代原先「新增类型必须改中心 switch」的硬编码。

约定：
- **查找用裸 str、不做枚举转换**：``task.resource_type`` 无 DB CHECK，
  ``TaskResourceType(...)`` 对脏数据/版本不一致的旧值会 ValueError 炸掉整个上报
  （比不投影还脆弱）；StrEnum 成员即 str，注册表用枚举成员做 key、裸 str 查找，
  天然等值命中，未知类型 miss 空列表。
- **错误隔离**：单个 projector 异常由 task_ingest 捕获（log.warning），不阻断其他
  投影与状态机——观测不反噬状态机。
- 投影只在「鲜活」事件上跑（task 未终态）：stale 报告不得把 present/failed 回退。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.constants import (
    DeploymentPhase,
    DeploymentStatus,
    DeployStep,
    LogStatus,
    NodeModelState,
)
from llmpod.config.constants import (
    TASK_TERMINAL_STATUSES,
    EnsureModelStage,
    PullImageStage,
    TaskResourceType,
    TaskStatus,
)
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.services import deploy_log_service
from llmpod.tunnel.schemas import TaskEventPayload

log = logging.getLogger("llmpod.apps.manager.services.task_projections")

Projector = Callable[[AsyncSession, Task, TaskEventPayload], Awaitable[None]]

# key 用枚举成员（= str）、查找用裸 str——见模块 docstring 的容错说明
TASK_PROJECTORS: dict[str, list[Projector]] = {}


def register(resource_type: TaskResourceType) -> Callable[[Projector], Projector]:
    """把 projector 注册到某 resource_type 下（可叠多个装饰器 = 多类型共用）。"""

    def _wrap(fn: Projector) -> Projector:
        TASK_PROJECTORS.setdefault(resource_type, []).append(fn)
        return fn

    return _wrap


# ---- node_model 投影（MODEL_PLACEMENT）----

# ENSURE_MODEL stage → node_model.status（按 (resource_type, stage) 二元组匹配，
# PullImageStage.FINISHED 不会进这张表——跨族重复值靠 resource_type 分发隔离）
_STAGE_TO_STATUS = {
    EnsureModelStage.DOWNLOADING: NodeModelState.DOWNLOADING,
    EnsureModelStage.VERIFYING: NodeModelState.VERIFYING,
    EnsureModelStage.FINISHED: NodeModelState.PRESENT,
}
# task 终态 → node_model.status（stage 缺失时的兜底投影）
_TASK_STATUS_TO_NM = {
    TaskStatus.RUNNING: NodeModelState.DOWNLOADING,
    TaskStatus.SUCCESS: NodeModelState.PRESENT,
    TaskStatus.FAILED: NodeModelState.FAILED,
}


@register(TaskResourceType.MODEL_PLACEMENT)
async def project_node_model(db: AsyncSession, task: Task, evt: TaskEventPayload) -> None:
    """模型分发进度 → node_model（status/bytes/model_path/last_error）。"""
    mid, nid = _parse_resource_key(task.resource_key)
    if mid is None:
        return
    rows = list((await db.execute(select(NodeModel).where(NodeModel.model_id == mid))).scalars())
    nm = next((r for r in rows if r.node_id == nid), None)
    if nm is None:
        return  # 行已随 recompute_demand 清理或从未建——丢弃投影

    extra = evt.extra or {}
    stage = _evt_stage(evt)
    # 状态投影：stage 优先，其次 task 状态。present 只认权威信号（stage=finished / task
    # SUCCESS）；model_path 单独出现（如 node 重连同步的陈旧本地任务上报）只随权威信号
    # 一起记，不得独自翻状态——曾据此把 bytes_done=0 的行写成 present，staging 短路后
    # 容器 bind 到空目录、引擎拿不到权重反复崩溃。
    finished = stage == EnsureModelStage.FINISHED or evt.status == TaskStatus.SUCCESS
    if extra.get("model_path") and finished:
        nm.status = NodeModelState.PRESENT
        nm.model_path = extra["model_path"]
    elif stage in _STAGE_TO_STATUS:
        nm.status = _STAGE_TO_STATUS[stage]
    elif evt.status in _TASK_STATUS_TO_NM:
        nm.status = _TASK_STATUS_TO_NM[evt.status]

    if "bytes_done" in extra:
        nm.bytes_done = extra.get("bytes_done") or 0
    if "bytes_total" in extra:
        nm.bytes_total = extra.get("bytes_total")
    if extra.get("local_dir"):
        nm.local_dir = extra["local_dir"]
    if evt.status == TaskStatus.FAILED and evt.message:
        nm.last_error = evt.message
    nm.last_checked_at = datetime.now(timezone.utc)


def _parse_resource_key(resource_key: str) -> tuple[int | None, str | None]:
    """``model:{mid}:{nid}`` → ``(mid, nid)``。非匹配返回 ``(None, None)``。"""
    parts = (resource_key or "").split(":")
    if len(parts) == 3 and parts[0] == "model":
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return None, None
    return None, None


def _evt_stage(evt: TaskEventPayload) -> str | None:
    """事件子阶段：一等字段优先；``extra["stage"]`` 兼容旧节点 rolling 升级期。"""
    return evt.stage or (evt.extra or {}).get("stage")


# ---- deploy_log 进度点投影（IMAGE_PULL + MODEL_PLACEMENT 共用）----

# 进度点节流状态：task_id → (stage, progress, bytes_done, 落库时刻)。仅在 asyncio 单线程事件循环
# 内读写，无锁。终态 pop 清理防长期运行只增不减；manager 重启丢失只导致多落一行，
# 无正确性影响。
_LOG_THROTTLE: dict[str, tuple[str | None, int, int | None, float]] = {}
_LOG_PROGRESS_DELTA = 5  # 进度增量阈值（%）
_LOG_PROGRESS_INTERVAL = 5.0  # 时间阈值（秒）——慢拉取即使 Δ 小也周期性落一行（心跳）

# 子阶段 → deploy_log.step（进度点只落在 RUNNING 期间的阶段上）
_STAGE_TO_STEP = {
    EnsureModelStage.DOWNLOADING: DeployStep.STAGING_MODEL,
    EnsureModelStage.VERIFYING: DeployStep.STAGING_MODEL,
    PullImageStage.PULLING: DeployStep.PULL_IMAGE,
}


def reset_log_throttle() -> None:
    """清空节流状态（测试隔离用）。"""
    _LOG_THROTTLE.clear()


def _fmt_rate(rate: int) -> str:
    """bytes/s → 人读速率（B/s 起步，逐级 MB/s / GB/s），时间线消息用。

    低速必须保住非零显示：500B/s 渲染成「0KB/s」读起来像卡死，实际还在走。
    """
    if rate >= 2**30:
        return f"{rate / 2**30:.1f}GB/s"
    if rate >= 2**20:
        return f"{rate / 2**20:.1f}MB/s"
    if rate >= 2**10:
        return f"{rate / 2**10:.0f}KB/s"
    return f"{rate}B/s"


@register(TaskResourceType.IMAGE_PULL)
@register(TaskResourceType.MODEL_PLACEMENT)
async def project_deploy_log_progress(db: AsyncSession, task: Task, evt: TaskEventPayload) -> None:
    """下载/拉取进度点 → deploy_log（部署详情时间线可见「拉到多少」）。

    双层节流各管各的资源：executor 侧（``ByteProgressReporter``，镜像拉取与模型下载共用）
    最快 2s 一条管 wire 流量；本投影 Δ≥5% 或 Δt≥5s 管库内行数（45GB 镜像拉 10 分钟 ≈
    40-120 行）。**阶段迁移必落**（downloading→
    verifying、pulling 开始），不受节流。终态只清节流状态不落行——成功/失败由
    set_phase 时间线与实例生命周期投影负责。``extra`` 除进度百分比外还结构化携带
    bytes_done/bytes_total/bytes_per_sec（消息文本之外的可渲染字段，logs/nodes 端点透出）。
    """
    if evt.status in TASK_TERMINAL_STATUSES:
        _LOG_THROTTLE.pop(task.id, None)
        return
    stage = _evt_stage(evt)
    step = _STAGE_TO_STEP.get(stage) if stage is not None else None
    if step is None:
        return
    now = time.monotonic()
    extra = evt.extra or {}
    done = extra.get("bytes_done")
    done_key = done if isinstance(done, int) else None  # 归一化一次，比较与存储同口径
    prev = _LOG_THROTTLE.get(task.id)
    if (
        prev is not None
        and prev[0] == stage
        and evt.progress - prev[1] < _LOG_PROGRESS_DELTA
        and now - prev[3] < _LOG_PROGRESS_INTERVAL
    ):
        return
    # 停滞去重：字节与进度都没动（续传等待/卡速期的心跳）→ 不落重复行，也不给节流续命
    if prev is not None and prev[0] == stage and prev[2] == done_key and evt.progress == prev[1]:
        return
    _LOG_THROTTLE[task.id] = (stage, evt.progress, done_key, now)

    total = extra.get("bytes_total")
    has_bytes = isinstance(done, int) and isinstance(total, int) and total > 0
    rate = extra.get("bytes_per_sec")
    has_rate = isinstance(rate, int) and rate >= 0  # 0B/s 也是信号（停滞 vs 未上报）
    sizes = f"{done / 2**30:.1f}GB / {total / 2**30:.1f}GB" if has_bytes else None
    rate_txt = f"（{_fmt_rate(rate)}）" if has_rate else ""
    if stage == EnsureModelStage.VERIFYING:
        message = "校验中"
    elif stage == EnsureModelStage.DOWNLOADING:
        message = f"已下载 {sizes}{rate_txt}" if sizes else "开始下载"
    elif stage == PullImageStage.PULLING:
        message = f"已拉取 {sizes}{rate_txt}" if sizes else "开始拉取镜像"
    else:
        message = f"进度 {evt.progress}%"

    dep_ids = await _resolve_deployments(db, task)
    if not dep_ids:
        return  # 无匹配部署（非部署触发的分发），不落日志
    # 有匹配部署才查 Node 展示名，避免无匹配时白查一次
    host, ip = await deploy_log_service.node_display(db, task.node_id)
    log_extra: dict = {
        "progress": evt.progress,
        "stage": stage,
        "trigger": "system",
        "node_hostname": host,
        "node_ip": ip,
    }
    if has_bytes:
        log_extra["bytes_done"] = done
        log_extra["bytes_total"] = total
    if has_rate:
        log_extra["bytes_per_sec"] = rate
    for dep_id in dep_ids:
        await deploy_log_service.append_log(
            db,
            deployment_id=dep_id,
            node_id=task.node_id,
            task_id=task.id,
            step=step,
            status=LogStatus.RUNNING,
            message=message,
            extra=log_extra,
        )


async def _resolve_deployments(db: AsyncSession, task: Task) -> list[str]:
    """进度点归属的部署：找「正卡在对应编排阶段」的 active 部署。

    resource_key 前缀定阶段——``image:{nid}:{hash}`` → PULL_IMAGE；``model:{mid}:{nid}``
    → STAGING_MODEL（且 model 匹配）。模型分发/镜像拉取任务按 resource_key 去重、跨
    部署共享，进度同时落给所有正等待它的部署；找不到（非部署触发的分发）就跳过。
    """
    rk = task.resource_key or ""
    want_model: int | None = None
    if rk.startswith("image:"):
        phase = DeploymentPhase.PULL_IMAGE
    elif rk.startswith("model:"):
        phase = DeploymentPhase.STAGING_MODEL
        mid, _ = _parse_resource_key(rk)
        want_model = mid
    else:
        return []
    if want_model is None and phase == DeploymentPhase.STAGING_MODEL:
        return []  # resource_key 解析失败

    insts = list((await db.execute(select(Instance).where(Instance.assigned_node == task.node_id))).scalars())
    dep_ids = sorted({i.deployment_id for i in insts})
    if not dep_ids:
        return []
    deps = list((await db.execute(select(ModelDeployment).where(ModelDeployment.deployment_id.in_(dep_ids)))).scalars())
    return [
        d.deployment_id
        for d in deps
        if d.status == DeploymentStatus.CREATING
        and d.phase == phase
        and (want_model is None or d.model_id == want_model)
    ]
