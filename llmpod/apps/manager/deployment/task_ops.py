"""容器任务创建/去重/重试：``ensure_task_row``（Manager 侧，domain-neutral）。

复刻 ``PlacementService.ensure_present`` 的事务内决策模式（供 PULL_IMAGE / START_INSTANCE /
STOP_INSTANCE 等容器阶段任务共用）：

- active（PENDING/RUNNING）同 ``resource_key`` 已存在 → 复用（PENDING 重发，RUNNING 不打扰）；
- 无 active → attempt = 历史 max+1（单调，唯一索引）；「超上限」判末尾连续失败数
  （SUCCESS 打断失败链），不按 max(attempt)——否则成功操作也抬高 attempt，手动 stop/start
  数次后误触上限；
- DB 层 ``uq_task_active_resource`` partial unique index 兜底并发竞态（IntegrityError → 跳过）；
- 事务外 best-effort ``dispatch.ensure_task``（失败不回滚 task，PENDING 行由调用方每轮重发）。

容器任务均无 secrets——下发时 ``secrets=None``。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from llmpod.apps.manager.deployment.constants import MAX_AUTO_ATTEMPTS
from llmpod.config.constants import TASK_INCOMPLETE_STATUSES, TaskStatus, TaskType
from llmpod.models.task import Task
from llmpod.tunnel.schemas import TaskPayload
from llmpod.utils.crypto import generate_uuid

log = logging.getLogger("llmpod.apps.manager.deployment.task_ops")

# created_at 兜底值（FakeSession/手工构造行可能缺省，排序键不得崩）
_ATTEMPT_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _next_attempt(rows: list[Task]) -> int:
    """新一轮 attempt 编号 = 历史最大 attempt + 1（单调）。

    ``uq_task_resource_key_attempt (resource_key, attempt)`` 唯一索引要求 attempt 单调——
    不得按「连续失败数+1」重计（尾端非 FAILED 时会复用历史编号撞索引）。SUCCESS 不重置
    编号；「是否超重试上限」另由 :func:`trailing_fail_count` 判断，两者职责分离。
    """
    return (max(t.attempt for t in rows) + 1) if rows else 1


def trailing_fail_count(rows: list[Task]) -> int:
    """末尾连续 FAILED 数（重试预算/退避判定共用；SUCCESS/CANCELLED 打断失败链）。

    attempt 因唯一索引必须单调递增，故「是否超自动重试上限」不能看 ``max(attempt)``——
    否则每次成功操作都抬高 attempt，用户手动 stop/start 数次后误触上限（正常操作被拒）。
    只有**连续失败**才说明该资源真的有问题，SUCCESS 重置失败计数。
    """
    ordered = sorted(rows, key=lambda t: t.created_at or _ATTEMPT_EPOCH)
    n = 0
    for t in reversed(ordered):
        if t.status == TaskStatus.FAILED:
            n += 1
        else:
            break
    return n


def _last_failed_age_s(rows: list[Task]) -> float:
    """末条 FAILED 行距现在的秒数（超限冷却判定；无 FAILED/无时间戳返回 0）。"""
    failed = [t for t in rows if t.status == TaskStatus.FAILED and t.finished_at is not None]
    if not failed:
        return 0.0
    latest = max(t.finished_at for t in failed)
    return max(0.0, (datetime.now(timezone.utc) - latest).total_seconds())


def image_ref_hash(image: str) -> str:
    """镜像 ref 的稳定短哈希（resource_key 用；ref 可能很长/含特殊字符）。"""
    return hashlib.sha256(image.encode()).hexdigest()[:16]


# 任务看门狗默认值（秒）：node 侧 ``wait_for(timeout_sec)`` 的挂死兜底——executor
# 内部已有分段超时（readiness / 停止 / 下载重试），这里只兜「await 永不返回」
# （docker SDK 卡死、pull 流 stall、文件服务半开连接）。取值须显著大于正常执行
# 上限，宁可晚触发不可误杀长任务；超时 → node 报 FAILED → 既有 attempt/重试收敛。
# ENSURE_MODEL（大模型下载）在 placement.py 创建，超时单独给（见其调用点注释）。
TASK_HANG_TIMEOUTS: dict[str, int] = {
    # PULL 默认 2h 兜底：调用方（ensure_pull_tasks）按镜像体积显式传分桶值，
    # 未传时保底仍要有——不能因为缺体积信息就无限时
    TaskType.PULL_IMAGE: 2 * 3600,
    # 停止 = docker stop（≤10s）+ 上报，5 分钟绰绰有余
    TaskType.STOP_INSTANCE: 300,
}

# PULL 看门狗随镜像体积分桶：每 GB 15min + 半小时地板，2h 封顶——小镜像
# registry 断供时不再空等统一 2h（烧满 3×2h）；无体积信息回落 2h（保守）。
_PULL_TIMEOUT_FLOOR_S = 1800
_PULL_TIMEOUT_PER_GB_S = 900
_PULL_TIMEOUT_CAP_S = 2 * 3600


def default_hang_timeout(task_type: str) -> int | None:
    """按任务类型取看门狗默认值（无默认的类型返回 None = 不限时）。"""
    return TASK_HANG_TIMEOUTS.get(task_type)


def pull_hang_timeout(size_gb: float | None) -> int:
    """PULL_IMAGE 看门狗（按镜像体积分桶，catalog 未标注回落保守默认）。"""
    if not size_gb or size_gb <= 0:
        return TASK_HANG_TIMEOUTS[TaskType.PULL_IMAGE]
    return min(
        max(_PULL_TIMEOUT_FLOOR_S, int(size_gb) * _PULL_TIMEOUT_PER_GB_S + _PULL_TIMEOUT_FLOOR_S),
        _PULL_TIMEOUT_CAP_S,
    )


def pull_resource_key(node_id: str, image: str) -> str:
    return f"image:{node_id}:{image_ref_hash(image)}"


def start_resource_key(engine_instance_id: str, node_rank: int) -> str:
    """START task 的稳定键：引擎实例 + 引擎内分片序。

    引擎实例 ID 与 node_rank 在容器重建/restart 后均不变，键随之稳定；restart 归零
    attempt 靠删旧 task 行（键不变，机制平移自旧编法）。
    """
    return f"instance:{engine_instance_id}:{node_rank}"


def stop_resource_key(engine_instance_id: str, node_rank: int) -> str:
    return f"instance:{engine_instance_id}:{node_rank}:stop"


def instance_node_rank(irow) -> int:
    """实例的 rendezvous 序号（spec JSONB 轻量解析；缺省 0=head，旧行安全）。"""
    return int((irow.spec or {}).get("node_rank") or 0)


def instance_key_args(irow) -> tuple[str, int]:
    """从容器行取 task resource_key 键源（引擎实例 + 引擎内分片序）。

    列优先（``Instance.model_deployment_instance_id``），spec 的 ``engine_instance_id``
    兜底，容器自身 ID 兜底最旧的数据——保证任何年代的行都能得出稳定且互不冲突的键。
    """
    spec = irow.spec or {}
    engine_id = irow.model_deployment_instance_id or spec.get("engine_instance_id")
    return (engine_id or irow.instance_id, instance_node_rank(irow))


def instance_start_key(irow) -> str:
    """容器行的 START task 键（reconciler / stop / scale 共用）。"""
    return start_resource_key(*instance_key_args(irow))


def instance_stop_key(irow) -> str:
    """容器行的 STOP task 键（reconciler / stop / scale 共用）。"""
    return stop_resource_key(*instance_key_args(irow))


def task_to_payload(task: Task) -> TaskPayload:
    """task 行 → wire ``TaskPayload``（ensure_task 下发用；manager 内部全程 typed）。

    ``or`` 兜底列默认值：新建行 flush 前属性可能仍是 None（列 default 在 INSERT 才落），
    wire 模型的默认语义在此对齐。
    """
    return TaskPayload(
        id=task.id,
        type=task.type,
        resource_type=task.resource_type,
        resource_key=task.resource_key,
        node_id=task.node_id,
        status=task.status or TaskStatus.PENDING,
        attempt=task.attempt or 1,
        progress=task.progress or 0,
        timeout_sec=task.timeout_sec,
        payload=task.payload or {},
    )


async def ensure_task_row(
    db,
    *,
    resource_type: str,
    resource_key: str,
    node_id: str,
    task_type: str,
    payload: dict,
    timeout_sec: int | None = None,
    max_attempts: int = MAX_AUTO_ATTEMPTS,
    exhaust_cooldown_s: float | None = None,
) -> tuple[Task | None, bool, str | None]:
    """声明式确保某 resource_key 有 active task（事务内决策；不在此处下发）。

    返回 ``(task, need_dispatch, denial)``：
    - ``(task, True, None)``：新建（PENDING）或遗留 PENDING 需（重）下发；
    - ``(task, False, None)``：active RUNNING（node 已在跑），不打扰；
    - ``(None, False, denial)``：本次拒绝创建。``denial`` 区分两种成因——
      ``"exhausted"``（末尾连续失败超上限，终态语义，调用方应判失败收敛）与
      ``"concurrent"``（撞 active 唯一索引，别的执行者刚建了行，**不是失败**，
      调用方应按「已在途」等下一轮，误判耗尽会回滚/失败掉健康任务）。历史上
      两者共用一个 ``(None, False)`` 返回值，调用方无从区分——扩成三元组。

    ``exhaust_cooldown_s``：超限冷却——末条 FAILED 距今已超该值时视为可重试
    （重开一轮 attempt），供失败会跨部署传染的键（PULL 的 (node,image) 跨部署
    共享）衰减历史失败；None = 维持「超限即终态」语义。

    调用方在事务提交后对 ``need_dispatch`` 行 best-effort 下发（``dispatch.ensure_task``）。
    """
    rows = list((await db.execute(select(Task).where(Task.resource_key == resource_key))).scalars())
    task = next((t for t in rows if t.status in TASK_INCOMPLETE_STATUSES), None)
    need_dispatch = False
    if task is None:
        # 超限判定看「末尾连续失败数」，不看 max(attempt)：attempt 因唯一索引必须单调，
        # 成功操作也会抬高它——手动 stop/start 数次后会误触上限（正常操作被拒）。
        trailing = trailing_fail_count(rows)
        if trailing >= max_attempts:
            cooled = exhaust_cooldown_s is not None and _last_failed_age_s(rows) > exhaust_cooldown_s
            if not cooled:
                log.warning(
                    "task_ops %s: 连续失败 %d 次超上限（%d），不再自动创建",
                    resource_key,
                    trailing,
                    max_attempts,
                )
                return None, False, "exhausted"
            log.info(
                "task_ops %s: 超限冷却已过（%.0fs），重开一轮重试",
                resource_key,
                exhaust_cooldown_s,
            )
        attempt = _next_attempt(rows)
        task = Task(
            id=generate_uuid(),
            resource_type=resource_type,
            resource_key=resource_key,
            node_id=node_id,
            type=task_type,
            status=TaskStatus.PENDING,
            payload=payload,
            # 未显式指定时按类型兜底看门狗（防「await 永不返回」的挂死无人管）；
            # 某类型确需不限时，从 TASK_HANG_TIMEOUTS 移除即可
            timeout_sec=timeout_sec if timeout_sec is not None else default_hang_timeout(task_type),
            attempt=attempt,
        )
        db.add(task)
        try:
            # flush 包在 SAVEPOINT 里：撞 partial unique index 时只回滚到保存点，
            # 调用方（reconciler/stop 循环）在同一 session 里已写入的其他 Task 行 /
            # Instance 状态改写保得住——整事务 rollback 会把它们静默丢掉，且本函数
            # 返回后 to_dispatch 里的幽灵 task 仍被下发。
            async with db.begin_nested():
                await db.flush()
        except IntegrityError:
            log.info("task_ops %s: 并发已建 active task，跳过", resource_key)
            return None, False, "concurrent"
        log.info("task_ops %s: created task %s attempt=%d", resource_key, task.id[:8], attempt)
        need_dispatch = True
    elif task.status == TaskStatus.PENDING:
        need_dispatch = True  # 已建但下发失败/node 离线 → 重发
    return task, need_dispatch, None


async def dispatch_task(task: Task) -> None:
    """事务外 best-effort 下发（node 离线/RPC 失败 → task 留 PENDING，调用方下轮重发）。"""
    from llmpod.apps.manager import dispatch

    try:
        await dispatch.ensure_task(task.node_id, task_to_payload(task), secrets=None)
    except Exception as e:
        log.debug("task_ops: ensure_task 下发失败（留下轮重发）task=%s: %s", task.id[:8], e)


async def dispatch_cancel_task(node_id: str, task_id: str) -> None:
    """best-effort 取消下发（Node 离线忽略；Manager 侧已置 CANCELLED）。"""
    from llmpod.apps.manager import dispatch

    try:
        await dispatch.cancel_task(node_id, task_id)
    except Exception as e:
        log.debug("task_ops: cancel_task 下发失败 task=%s: %s", task_id[:8], e)
