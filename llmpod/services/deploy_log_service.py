"""部署全流程日志 DB 服务（纯 async 函数，首参 ``AsyncSession``）。

管理端编排埋点 + 每节点实例生命周期投影统一经此落 ``deploy_log`` 表。append-only；本方案
deploy_log 不接触引擎原始 stdout，编排/实例 message 为系统生成、不含密。

两类入口：
- ``append_log(...)``：在调用方既有 session 内追加（flush 由调用方控制），与调用方事务原子。
  用于 set_phase / create_deployment / mark_failed_if_active / report_instance_status 等已在
  session 内的埋点。
- ``record(...)``：独立短事务写一条（无既有 session 场景）；best-effort，失败仅 debug，**绝不**
  阻断主流程。

读侧 ``list_logs`` 按 deployment_id 单条件过滤 + Python 侧多条件/分页（FakeSession 单 where
兼容，见项目记忆 fakesession-bulk-stmt-limit）；``latest_log_by_node`` 给前端「每节点当前步骤/
状态」摘要。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.constants import (
    DeployStep,
    DeploymentPhase,
    LogLevel,
    LogStatus,
    MAX_AUTO_ATTEMPTS,
)
from llmpod.db import get_db_context
from llmpod.models.deploy_log import DeployLog
from llmpod.models.node import Node
from llmpod.utils.time import as_utc

log = logging.getLogger("llmpod.services.deploy_log_service")

# 应用层 step 白名单：step 不加 DB CHECK（跨 phase/实例/引擎多源、会扩展），此处仅防拼写静默入库。
# 未知 step 仍入库（不阻断日志），仅 log.warning。
_KNOWN_STEPS = frozenset(DeployStep)
_LEVELS = frozenset(LogLevel)
_STATUSES = frozenset(LogStatus)


class _PhaseInfo(NamedTuple):
    """deployment.phase 的 deploy_log 投影（step / message / level 三值一张表）。"""

    step: str
    message: str
    level: str


# deployment.phase → (step, message, level)：三投影合一，新增 phase 只改这一处
# （此前 step/message 两张平行 dict 需同步增键，易漂移）。STOPPING 是「停止」阶段，warn。
_PHASE_INFO: dict[DeploymentPhase, _PhaseInfo] = {
    DeploymentPhase.PENDING: _PhaseInfo(DeployStep.QUEUED, "排队中，等待调度", LogLevel.INFO),
    DeploymentPhase.STAGING_MODEL: _PhaseInfo(DeployStep.STAGING_MODEL, "开始下载模型", LogLevel.INFO),
    DeploymentPhase.PULL_IMAGE: _PhaseInfo(DeployStep.PULL_IMAGE, "开始拉取镜像", LogLevel.INFO),
    DeploymentPhase.START_HEAD: _PhaseInfo(DeployStep.START_HEAD, "启动主节点容器", LogLevel.INFO),
    DeploymentPhase.WAIT_HEAD_READY: _PhaseInfo(DeployStep.HEAD_HEALTH_CHECK, "等待主节点健康检查通过", LogLevel.INFO),
    DeploymentPhase.START_WORKERS: _PhaseInfo(DeployStep.START_WORKERS, "启动工作节点容器", LogLevel.INFO),
    DeploymentPhase.WAIT_WORKERS_READY: _PhaseInfo(
        DeployStep.WORKER_HEALTH_CHECK, "等待工作节点健康检查通过", LogLevel.INFO
    ),
    DeploymentPhase.FINALIZING: _PhaseInfo(DeployStep.FINALIZE, "生成访问端点", LogLevel.INFO),
    DeploymentPhase.RUNNING: _PhaseInfo(DeployStep.RUNNING, "服务已就绪", LogLevel.INFO),
    DeploymentPhase.SCALING: _PhaseInfo(DeployStep.RUNNING, "在线调整副本数", LogLevel.INFO),
    DeploymentPhase.STOPPING: _PhaseInfo(DeployStep.STOPPING, "停止中", LogLevel.WARN),
}
_DEFAULT_PHASE_INFO = _PhaseInfo(DeployStep.ORCHESTRATION, "编排推进", LogLevel.INFO)


def _phase_info(phase: str | None) -> _PhaseInfo:
    return _PHASE_INFO.get(phase or "", _DEFAULT_PHASE_INFO)


def phase_to_step(phase: str | None) -> str:
    """deployment.phase → deploy_log.step（缺省 ``orchestration``）。"""
    return _phase_info(phase).step


def phase_to_message(phase: str | None) -> str:
    """deployment.phase → deploy_log.message（缺省 ``编排推进``）。"""
    return _phase_info(phase).message


def phase_to_level(phase: str | None) -> str:
    """deployment.phase → deploy_log.level（STOPPING=warn，其余 info）。"""
    return _phase_info(phase).level


def phase_retry_message(phase: str | None, attempt: int) -> str:
    """重试阶段行文案：`{阶段 message}（第 {attempt}/{MAX_AUTO_ATTEMPTS} 次重试）`。"""
    return f"{phase_to_message(phase)}（第 {attempt}/{MAX_AUTO_ATTEMPTS} 次重试）"


def phase_to_log_status(phase: str | None) -> str:
    """deployment.phase → deploy_log 步骤进度 status（非部署态）。

    RUNNING→success（部署达成）；STOPPING→running（停止这一步进行中，step=stopping 已消歧）；
    其余非终态 phase→running（进行中）。
    """
    if phase == DeploymentPhase.RUNNING:
        return LogStatus.SUCCESS
    return LogStatus.RUNNING


def _norm_step(step: str) -> str:
    if step not in _KNOWN_STEPS:
        log.warning("deploy_log 未知 step=%r（仍入库，请修正拼写）", step)
    return step


def _norm_level(level: str | None) -> str:
    lvl = (level or LogLevel.INFO).lower()
    return lvl if lvl in _LEVELS else LogLevel.INFO


def _norm_status(status: str | None) -> str | None:
    if status is None:
        return None
    s = status.lower()
    return s if s in _STATUSES else None


async def append_log(
    db: AsyncSession,
    *,
    deployment_id: str,
    step: str,
    level: str = LogLevel.INFO,
    status: str | None = None,
    message: str | None = None,
    node_id: str | None = None,
    task_id: str | None = None,
    extra: dict | None = None,
) -> None:
    """在调用方 session 内追加一条部署日志（flush 由调用方控制，与调用方事务原子）。"""
    db.add(
        DeployLog(
            deployment_id=deployment_id,
            node_id=node_id,
            task_id=task_id,
            step=_norm_step(step),
            level=_norm_level(level),
            status=_norm_status(status),
            message=message,
            extra=extra,
        )
    )
    await db.flush()


async def record(*, deployment_id: str, step: str, **kw) -> None:
    """独立短事务写一条部署日志（无既有 session 场景）。

    best-effort：日志写失败绝不阻断主流程（仅 debug），与部署写事务解耦。
    """
    try:
        async with get_db_context() as db:
            await append_log(db, deployment_id=deployment_id, step=step, **kw)
    except Exception as e:
        log.debug("deploy_log record 失败 dep=%s step=%s: %s", deployment_id, step, e)


async def list_logs(
    db: AsyncSession,
    *,
    deployment_id: str,
    node_id: str | None = None,
    task_id: str | None = None,
    step: str | None = None,
    level: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[DeployLog], int]:
    """分页查询部署日志（按 created_at 升序——日志时间线正序），返回 (items, total)。

    ``deployment_id`` 必传（高选择性、单条件 ``.where``，FakeSession 兼容）；node_id/task_id/
    step/level/status/since/until/search 均 Python 侧过滤（FakeSession 不支持复合 where /
    ilike / offset / limit）。
    """
    rows = list((await db.execute(select(DeployLog).where(DeployLog.deployment_id == deployment_id))).scalars())

    lvl_q = level.lower() if level else None
    sts_q = status.lower() if status else None
    needle = search.lower() if search else None
    # created_at 读出即 aware，naive since/until（无偏移 ISO 串解析产物）先归一为 UTC
    since = as_utc(since)
    until = as_utc(until)

    def _match(r: DeployLog) -> bool:
        if node_id is not None and r.node_id != node_id:
            return False
        if task_id is not None and r.task_id != task_id:
            return False
        if step is not None and r.step != step:
            return False
        if lvl_q is not None and r.level != lvl_q:
            return False
        if sts_q is not None and r.status != sts_q:
            return False
        if since is not None and (r.created_at is None or r.created_at < since):
            return False
        if until is not None and (r.created_at is None or r.created_at > until):
            return False
        if needle and (not r.message or needle not in r.message.lower()):
            return False
        return True

    filtered = [r for r in rows if _match(r)]
    # 排序键加 id：同一事务内多行 created_at 相同（PG now() 事务稳定），靠自增 id 保证插入序
    filtered.sort(key=lambda r: (r.created_at, r.id))
    total = len(filtered)
    start = (page - 1) * page_size
    items = filtered[start : start + page_size] if page_size > 0 else filtered
    return items, total


async def latest_log_by_node(db: AsyncSession, deployment_id: str) -> list[DeployLog]:
    """每节点最后一条日志行：仅 ``node_id`` 非空的行，升序后者覆盖（编排事件不参与）。

    返回 ORM 行（每节点一条胜出行）；表现层投影（OUT 模型 / ISO 时间串）由路由负责，
    service 不依赖 webserver schema。
    """
    rows = list((await db.execute(select(DeployLog).where(DeployLog.deployment_id == deployment_id))).scalars())
    rows = [r for r in rows if r.node_id]
    rows.sort(key=lambda r: (r.created_at, r.id))
    latest: dict[str, DeployLog] = {}
    for r in rows:
        latest[r.node_id] = r
    return list(latest.values())


async def node_display_map(
    db: AsyncSession, node_ids: set[str] | list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """node_id → (hostname, ip)：部署日志每节点摘要附可读名用。

    ``in_`` 批量取 Node 行（FakeSession 只支持单条件 == 时，调用方/测试可 monkeypatch 本函数）。
    """
    ids = {i for i in node_ids if i}
    if not ids:
        return {}
    rows = list((await db.execute(select(Node).where(Node.id.in_(ids)))).scalars())
    return {n.id: (n.hostname, n.ip) for n in rows}


async def node_display(db: AsyncSession, node_id: str) -> tuple[str | None, str | None]:
    """node_id → (hostname, ip)：单节点展示名（实例生命周期/进度事件补可读名用）。

    对偶 ``node_display_map`` 的批量版；node 行缺失（离线/未落库）返回 (None, None)。
    """
    if not node_id:
        return (None, None)
    node = await db.get(Node, node_id)
    if node is None:
        return (None, None)
    return (node.hostname, node.ip)


async def delete_for_deployment(db: AsyncSession, deployment_id: str) -> int:
    """删某部署的全部 deploy_log 行（delete 级联用）。"""
    rows = list((await db.execute(select(DeployLog).where(DeployLog.deployment_id == deployment_id))).scalars())
    for obj in rows:
        await db.delete(obj)
    await db.flush()
    return len(rows)
