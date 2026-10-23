"""部署容器阶段的 phase 驱动推进器（reconciler）。

``orchestrator.deploy()`` 完成规划 + 模型分发后交接（phase=PULL_IMAGE）；controller 每轮
扫描对容器阶段的部署调 :func:`advance_deployment` 推进一步：

    PULL_IMAGE → START_HEAD → [START_WORKERS → WAIT_WORKERS_READY] → WAIT_HEAD_READY
              → FINALIZING → RUNNING

多机（nnodes>1）：head 先起（host rendezvous），workers 随即并发起（不等 head HTTP
ready——vLLM 多机 head /health 需 workers 连上才 200，先等会死锁）；workers 用容器
稳定存活判定就绪（headless 无 HTTP 端口），最后 head /health 作整集群就绪门。
单节点（无 worker 行）：START_HEAD → WAIT_HEAD_READY → FINALIZING，路径不变。

每步幂等（task 去重/attempt 经 ``task_ops.ensure_task_row``，phase 写入经 ``set_phase``
终态守卫）；Manager 重启后从 phase 续跑，不丢进度。WAIT 阶段采用双门判定
（Instance.status==READY 或其 START task SUCCESS）——实例状态上报与 task 事件任一通道
丢失都不卡死。失败（attempt 耗尽）→ :func:`fail_deployment`（取消活跃 START task +
释放 VRAM + status-aware failed）。

head 失败重试目标仍是 START_HEAD：重走对已 READY 的 workers 是 no-op（状态跳过），
保持单线性 phase 图；workers 存活阻塞在 rendezvous 重连（torch init timeout 覆盖
attempt 预算），head 容器重建后 workers 重连。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import select

from llmpod.apps.manager import dispatch
from llmpod.apps.manager.deployment import image_catalog, task_ops
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.orchestrator import build_run_spec, get_orchestrator
from llmpod.apps.manager.deployment.schemas import DeploymentConfig, InstanceSpec
from llmpod.apps.manager.deployment.task_ops import (
    instance_key_args as _key_args,
    instance_node_rank as _node_rank,
    instance_start_key as _start_key,
    pull_resource_key,
    trailing_fail_count,
)
from llmpod.apps.manager.deployment.constants import (
    CONTAINER_PHASES,
    DEPLOYMENT_TERMINAL_STATUSES,
    INSTANCE_DEAD_STATUSES,
    INSTANCE_TRANSIENT_STATUSES,
    MAX_AUTO_ATTEMPTS,
    RETRY_EXHAUSTED_MESSAGE,
    RUNTIME_ABNORMAL_INSTANCE_STATUSES,
    DeployStep,
    DeploymentPhase,
    DeploymentStatus,
    LogLevel,
    LogStatus,
    ReplicaStatus,
    STAGING_PHASES,
    runtime_restart_backoff_sec,
)
from llmpod.config.constants import (
    TASK_INCOMPLETE_STATUSES,
    InstanceStatus,
    TaskResourceType,
    TaskStatus,
    TaskType,
)
from llmpod.core.exceptions import LLMPodError, LLMPodValidationError, NotFound
from llmpod.config.settings import settings
from llmpod.db import get_db_context
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task
from llmpod.services import deploy_log_service, deployment_projection, deployment_service
from llmpod.tunnel.schemas import PullImagePayload, ReadinessConfig, StartInstancePayload
from llmpod.utils.crypto import generate_uuid
from llmpod.utils.docker_naming import deployment_container_name

log = logging.getLogger("llmpod.apps.manager.deployment.reconciler")

__all__ = [
    "resolve_image",
    "CONTAINER_PHASES",
    "STAGING_PHASES",
    "advance_deployment",
    "converge_stop_deployment",
    "fail_deployment",
    "reconcile_runtime_instances",
]

# 容器阶段分组（CONTAINER_PHASES/STAGING_PHASES）定义在本包 constants.py，
# 此处再导出供 controller/recovery 按历史路径引用

READINESS_INITIAL_DELAY_S = 5  # 容器启动后起探活前的宽限（引擎进程初始化）
READINESS_STABLE_INTERVAL_S = 4  # workers 稳定存活判定的轮询间隔（复刻历史 gate 节奏）
# 等容器 RUNNING 门的最长时限：START task 创建后超时仍未 RUNNING → 视为节点无响应/启动卡死，
# fail（旧流程 phase 立即前进，此门会让 node 离线时 worker 永不启动，需兜底失败而非无限挂）
_WAIT_RUNNING_TIMEOUT_S = 300.0


def resolve_image(engine: str, adapter, config: DeploymentConfig, inst: InstanceSpec | None = None) -> str:
    """镜像 ref（与 build_run_spec 同口径：config.image 覆盖 > catalog 命中；miss 即 raise）。

    有 ``inst`` 时按实例目标卡（arch/gpu_model/driver）精确匹配 catalog，保证 PULL 阶段拉到
    的**就是** START 阶段会用的真实镜像（driver 感知，不拉默认镜像——拉下来起不了纯浪费）。
    无 inst（调用方无具体卡上下文）时按 ``adapter.default_arch`` 粗匹配。
    """
    image_ref, _ = image_catalog.resolve_image_ref(
        engine=engine,
        arch=(inst.gpu_arch or adapter.default_arch) if inst is not None else adapter.default_arch,
        gpu_model=getattr(inst, "target_gpu_model", None) if inst is not None else None,
        driver_version=getattr(inst, "target_driver_version", None) if inst is not None else None,
        engine_version=config.image_tag,
        explicit_image=config.image,
    )
    return image_ref


def resolve_image_size_gb(
    engine: str, adapter, config: DeploymentConfig, inst: InstanceSpec | None = None
) -> float | None:
    """catalog 条目标注的镜像体积（PULL 看门狗分桶用；未标注/显式覆盖返回 None）。"""
    _, entry = image_catalog.resolve_image_ref(
        engine=engine,
        arch=(inst.gpu_arch or adapter.default_arch) if inst is not None else adapter.default_arch,
        gpu_model=getattr(inst, "target_gpu_model", None) if inst is not None else None,
        driver_version=getattr(inst, "target_driver_version", None) if inst is not None else None,
        engine_version=config.image_tag,
        explicit_image=config.image,
    )
    if entry is None:
        return None
    return getattr(entry, "size_gb", None)


# ---- 主入口 ----


async def advance_deployment(deployment_id: str) -> None:
    """推进容器 phase 一步（幂等；部署不再驱动时 no-op）。

    兜底：step 函数抛出的**永久性配置错误**（镜像解析/引擎适配/设备配置——输入在
    ``_plan`` 后即固定，重试无意义）捕获并 fail_deployment，避免 controller 每轮 scan
    重跑同一步骤无限刷日志。瞬时基础设施错误（DB/RPC 异常）不在此列，照常冒给
    controller 走重试语义。
    """
    async with get_db_context() as db:
        dep = await db.get(ModelDeployment, deployment_id)
        if dep is None or dep.status != DeploymentStatus.CREATING:
            return
        phase = dep.phase
        if phase not in CONTAINER_PHASES:
            return

    try:
        if phase == DeploymentPhase.PULL_IMAGE:
            await _step_pull_image(deployment_id)
        elif phase == DeploymentPhase.START_HEAD:
            await _step_start_head(deployment_id)
        elif phase == DeploymentPhase.START_WORKERS:
            await _step_start_workers(deployment_id)
        elif phase == DeploymentPhase.WAIT_WORKERS_READY:
            await _step_wait_workers_ready(deployment_id)
        elif phase == DeploymentPhase.WAIT_HEAD_READY:
            await _step_wait_head_ready(deployment_id)
        elif phase == DeploymentPhase.FINALIZING:
            await _step_finalize(deployment_id)
    except (LLMPodValidationError, ValueError, NotImplementedError, NotFound) as e:
        await fail_deployment(deployment_id, error=f"{type(e).__name__}: {e}")


class DeploymentContext(NamedTuple):
    """_load_context 的返回（reconciler 各步共用的加载结果；前两字段同 ``LoadedDeployment``）。"""

    dep: ModelDeployment
    model: Model
    config: DeploymentConfig
    irows: list[Instance]


async def _load_context(deployment_id: str) -> DeploymentContext:
    """加载部署 + 模型 + config + Instance 行（reconciler 各步共用）。"""
    async with get_db_context() as db:
        dep, model = await deployment_service.load_deployment(db, deployment_id)
        config = DeploymentConfig.model_validate(dep.spec or {})
        irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        irows.sort(key=lambda r: r.rank)
    return DeploymentContext(dep=dep, model=model, config=config, irows=irows)


def _split_head_workers(irows: list[Instance]) -> tuple[list[Instance], list[Instance]]:
    """按 node_rank 拆分：head（node_rank==0）/ workers（node_rank>0）。"""
    heads = [r for r in irows if _node_rank(r) == 0]
    workers = [r for r in irows if _node_rank(r) > 0]
    return heads, workers


def _startup_groups(adapter, irows: list[Instance]) -> tuple[list[Instance], list[Instance]]:
    """首起组 / 次起组。默认 head 先起（作 rendezvous 服务器，workers 才能连）；
    ``workers_first`` 引擎反转（head 启动即注册全部 workers 地址，workers 先起才有得连）。"""
    heads, workers = _split_head_workers(irows)
    return (workers, heads) if adapter.startup_order == "workers_first" else (heads, workers)


def _specs(irows: list[Instance]) -> list[InstanceSpec]:
    return [InstanceSpec.model_validate(r.spec or {}) for r in irows]


async def _provisioning_engine_ids(deployment_id: str) -> set[str]:
    """扩容中（provisioning）副本的引擎 ID 集：这些分片的启动归 scale 协程，自愈跳过。"""
    async with get_db_context() as db:
        reps = [
            r
            for r in await deployment_projection.replica_rows_of(db, deployment_id)
            if r.status == ReplicaStatus.PROVISIONING
        ]
        if not reps:
            return set()
        engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in reps])
        return {e.instance_id for e in engines}


async def _task_rows_by_keys(keys: list[str]) -> list[Task]:
    if not keys:
        return []
    async with get_db_context() as db:
        return list((await db.execute(select(Task).where(Task.resource_key.in_(keys)))).scalars())


def _latest_by_key(tasks: list[Task]) -> dict[str, Task]:
    """resource_key → attempt 最大的 task（终态 FAILED/SUCCESS 判定用）。"""
    latest: dict[str, Task] = {}
    for t in tasks:
        cur = latest.get(t.resource_key)
        if cur is None or t.attempt > cur.attempt:
            latest[t.resource_key] = t
    return latest


async def _probe_image_present(node_id: str, image: str) -> bool | None:
    """RPC 复核镜像是否真在节点（超时在 ``dispatch.image_exists`` 层收口）。

    True=确认在位；False=确认不在位（node 侧只把 ImageNotFound 塌缩成 False）；
    None=通道不可用（超时/RPC 异常——含 node 侧 daemon 错误上抛），调用方按各自
    语义保守处理。
    """
    try:
        return await dispatch.image_exists(node_id, image)
    except Exception:
        return None


async def probe_pull_images(
    node_image: dict[str, str], prev_latest: dict[str, Task]
) -> dict[tuple[str, str], bool | None]:
    """拉取前复核/探测一批「节点 → 镜像」，返回 ``(node_id, image) → 三态``。

    **必须在打开 DB 事务之前调用**（ensure_pull_tasks 的调用方负责）：逐个串行
    await 会让「活着但不答」的节点把整轮扫描冻住（每节点各吃满超时），且探测
    全程占着池连接 idle-in-transaction——gather 并发后最坏耗时 = 单节点超时。

    只探需要判定的对：最新行 SUCCESS（复核是否已被 rmi/prune）与无行/FAILED/
    CANCELLED（探测是否本地缓存命中）；PENDING/RUNNING 在途的不探。
    """
    need: list[tuple[str, str]] = []
    for node_id, image in node_image.items():
        prev = prev_latest.get(pull_resource_key(node_id, image))
        if prev is None or prev.status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED):
            need.append((node_id, image))
    if not need:
        return {}
    results = await asyncio.gather(*(_probe_image_present(n, i) for n, i in need))
    return dict(zip(need, results))


async def ensure_pull_tasks(
    db,
    node_image: dict[str, str],
    prev_latest: dict[str, Task],
    probes: dict[tuple[str, str], bool | None],
    *,
    deployment_id: str | None = None,
    image_sizes: dict[str, float | None] | None = None,
) -> tuple[list[Task], tuple[str, str] | None]:
    """对一组「节点 → 镜像」ensure PULL_IMAGE task（幂等）。返回 (待下发, 耗尽的 (node, image))。

    部署期 reconciler 与在线扩容 ``_drive_new_replicas`` 共用的拉取收敛口径。
    ``probes`` 是事务外预先并发的探测结果（:func:`probe_pull_images`），本函数
    不发 RPC——事务内逐节点 await 会冻住扫描并占住池连接：

    - 最新行 SUCCESS → 探测确认镜像真在节点本地才短路：镜像可能已被 rmi/prune，
      也可能存在历史假成功行（拉取流内错误被吞的年代），盲信会让后续部署永远
      「跳过拉取 → 启动 No such image」死循环；通道不可用（None）保守信任
      （节点抖动不值得引发重拉风暴），复核未过时按 ``deployment_id`` 记一笔时间
      线日志（运维能把「重新拉取」与首次冷拉区分开）；
    - 无行 / FAILED / CANCELLED → 探测确认已在节点本地则直接记 SUCCESS 行
      （离线/无 registry 也能复用本地缓存）；通道不可用保守按不在位，交给 pull 流程暴露真因；
    - 其余（PENDING/RUNNING）交 ``ensure_task_row`` 接管；
    - 失败重建按末尾连续失败数判（不看单调 attempt——拉取键按节点+镜像**跨部署共享**，
      历史成功也会抬高编号，用 max(attempt) 判耗尽会误伤）。超限返回该 (node, image)，
      收敛动作（fail_deployment / 抛错回滚）由调用方决定。
    """
    to_dispatch: list[Task] = []
    for node_id, image in node_image.items():
        rk = pull_resource_key(node_id, image)
        prev = prev_latest.get(rk)
        if prev is not None and prev.status == TaskStatus.SUCCESS:
            # 复核通过才短路；不在位落到下方 ensure_task_row 重建 attempt 重拉
            # （attempt 单调 +1，不撞唯一索引；镜像若已回来，node 侧 image_exists
            # 短路直接报 SUCCESS）
            present = probes.get((node_id, image))
            if present is None:
                log.warning("ensure_pull %s: SUCCESS 复核通道不可用，保守信任历史行", rk)
                continue
            if present:
                continue
            log.info("ensure_pull %s: SUCCESS 复核镜像不在位，重建拉取", rk)
            if deployment_id is not None:
                await deploy_log_service.append_log(
                    db,
                    deployment_id=deployment_id,
                    step=DeployStep.PULL_IMAGE,
                    level=LogLevel.WARN,
                    status=LogStatus.RUNNING,
                    message=f"镜像复核不在位（已被清理或历史记录失真），重新拉取: {image}",
                    node_id=node_id,
                    extra={"trigger": "reconciler"},
                )
        if prev is None or prev.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            present = probes.get((node_id, image))
            if present:  # 只有确认在位才短路；None（通道不可用）交给 pull 流程暴露真因
                db.add(
                    Task(
                        id=generate_uuid(),
                        resource_type=TaskResourceType.IMAGE_PULL,
                        resource_key=rk,
                        node_id=node_id,
                        type=TaskType.PULL_IMAGE,
                        status=TaskStatus.SUCCESS,
                        payload=PullImagePayload(image=image).model_dump(),
                        attempt=(prev.attempt + 1) if prev is not None else 1,
                    )
                )
                continue
        task, need_dispatch, denial = await task_ops.ensure_task_row(
            db,
            resource_type=TaskResourceType.IMAGE_PULL,
            resource_key=rk,
            node_id=node_id,
            task_type=TaskType.PULL_IMAGE,
            payload=PullImagePayload(image=image).model_dump(),
            # 看门狗按体积分桶（未标注回落保守默认）；键跨部署共享，历史失败传染
            # 由冷却衰减（默认 1h 后重开一轮）
            timeout_sec=task_ops.pull_hang_timeout((image_sizes or {}).get(image)),
            exhaust_cooldown_s=settings.pull_exhaust_cooldown_s,
        )
        if task is None:
            if denial == "concurrent":
                # 并发已建（别的部署同轮对同 (节点,镜像) 发起拉取，撞 active 唯一索引）：
                # 不是失败——键跨部署共享，本轮视为已在途，下一轮扫描看到 SUCCESS/FAILED 再收敛
                continue
            return to_dispatch, (node_id, image)
        if need_dispatch:
            to_dispatch.append(task)
    return to_dispatch, None


# ---- PULL_IMAGE ----


async def _step_pull_image(deployment_id: str) -> None:
    """对每个涉及 node ensure PULL_IMAGE task；全 SUCCESS → START_HEAD；超限失败 → fail。

    镜像**按节点真实目标卡（arch/gpu_model/driver）经 catalog 精确解析**（与 START 阶段
    build_run_spec 同口径），不做一刀切默认——driver 不符的镜像拉下来也起不来，纯浪费。
    """
    dep, model, config, irows = await _load_context(deployment_id)
    adapter = get_adapter(dep.engine)
    # 节点 → 该节点首个实例解析出的真实镜像（同部署同节点实例 arch 一致，取首例即可）
    node_image: dict[str, str] = {}
    node_image_size: dict[str, float | None] = {}
    for r in irows:
        if not r.assigned_node or r.assigned_node in node_image:
            continue
        inst = InstanceSpec.model_validate(r.spec or {})
        try:
            node_image[r.assigned_node] = resolve_image(dep.engine, adapter, config, inst)
            node_image_size[r.assigned_node] = resolve_image_size_gb(dep.engine, adapter, config, inst)
        except LLMPodValidationError:
            # catalog miss（driver/型号/版本不匹配）是永久错误，重试无意义：直接 fail，
            # 用 explain_unresolved 给可操作原因（真实镜像 + 驱动/版本要求，指导用户升驱动或换 tag）
            reason = image_catalog.explain_unresolved(
                engine=dep.engine,
                arch=getattr(inst, "gpu_arch", None) or adapter.default_arch,
                gpu_model=getattr(inst, "target_gpu_model", None),
                driver_version=getattr(inst, "target_driver_version", None),
                engine_version=config.image_tag,
            )
            await fail_deployment(deployment_id, error=f"镜像解析失败: {reason}")
            return
    node_ids = sorted(node_image)
    if not node_ids:
        await _goto_phase(deployment_id, DeploymentPhase.START_HEAD)
        return

    keys = [pull_resource_key(n, node_image[n]) for n in node_ids]
    existing = _latest_by_key(await _task_rows_by_keys(keys))
    # 探测在事务外并发（见 probe_pull_images docstring）
    probes = await probe_pull_images(node_image, existing)
    image_sizes = {node_image[n]: node_image_size.get(n) for n in node_ids}
    async with get_db_context() as db:
        to_dispatch, exhausted = await ensure_pull_tasks(
            db, node_image, existing, probes, deployment_id=deployment_id, image_sizes=image_sizes
        )
        await db.flush()

    if exhausted is not None:
        node_id, image = exhausted
        prev = existing.get(pull_resource_key(node_id, image))
        over_cap_error = (prev.last_error if prev is not None else None) or (
            f"镜像拉取失败（超自动重试上限 {MAX_AUTO_ATTEMPTS}）: {image}"
        )
        await fail_deployment(deployment_id, error=over_cap_error, message=RETRY_EXHAUSTED_MESSAGE)
        return
    for t in to_dispatch:
        await task_ops.dispatch_task(t)

    # 全部 SUCCESS 才推进（含本轮之前已 SUCCESS 的 node）
    latest = _latest_by_key(await _task_rows_by_keys(keys))
    if all((latest.get(k) is not None and latest[k].status == TaskStatus.SUCCESS) for k in keys):
        # 本轮未发起真实 pull（全为本地缓存/前置探测命中）→ 补「跳过拉取」行：真实答案在
        # 探测这一刻已知，比 orchestrator 预探测可靠（不用重复 RPC 只为挑一句文案）
        if not to_dispatch:
            async with get_db_context() as db:
                await deploy_log_service.append_log(
                    db,
                    deployment_id=deployment_id,
                    step=DeployStep.PULL_IMAGE,
                    level=LogLevel.INFO,
                    status=LogStatus.SUCCESS,
                    message="镜像已存在，跳过拉取",
                    extra={"trigger": "reconciler"},
                )
        await _goto_phase(deployment_id, DeploymentPhase.START_HEAD)


# ---- START（head / workers 共用）----


def _start_payload(
    dep: ModelDeployment, model, adapter, config: DeploymentConfig, inst: InstanceSpec
) -> StartInstancePayload:
    """START_INSTANCE task 配方（typed 契约，调用方 ``model_dump`` 进 Task.payload）。

    DeploySpec 快照 + readiness 探活配置（无 secrets）。head（node_rank==0）：HTTP 探活
    （/health，谓词 200）；worker（node_rank>0，headless 无 HTTP 端口）：stable_running
    容器稳定存活判定。timeout 统一取 health_check 配置（单一用户旋钮）。
    """
    run_spec = build_run_spec(dep, model, inst, adapter, config)
    if inst.node_rank > 0:
        readiness = ReadinessConfig(
            mode="stable_running",
            initial_delay_sec=READINESS_INITIAL_DELAY_S,
            interval_sec=READINESS_STABLE_INTERVAL_S,
            timeout_sec=config.health_check.timeout_seconds,
        )
        endpoint = None  # headless worker 无服务端点
    else:
        health_path, _ = adapter.health_check()
        ip = inst.master_addr or "127.0.0.1"
        port = inst.port or adapter.default_port
        readiness = ReadinessConfig(
            url=f"http://{ip}:{port}{health_path}",
            initial_delay_sec=READINESS_INITIAL_DELAY_S,
            interval_sec=config.health_check.interval_seconds,
            timeout_sec=config.health_check.timeout_seconds,
        )
        endpoint = f"http://{ip}:{port}"
    return StartInstancePayload(
        deployment_id=dep.deployment_id,
        instance_id=inst.instance_id,
        rank=inst.rank,
        role=inst.role,
        container_name=run_spec.name,
        spec=run_spec,
        readiness=readiness,
        endpoint=endpoint,
    )


async def _ensure_start_tasks(
    dep: ModelDeployment,
    model,
    config: DeploymentConfig,
    adapter,
    rows: list[Instance],
    *,
    fail_on_exhausted: bool = True,
) -> dict[str, Task] | None:
    """为一组实例（heads 或 workers）ensure START_INSTANCE task 并下发。

    READY/STOPPED 跳过；RUNNING（容器已起，正等 readiness）跳过——不得重置成 STARTING，
    否则 START 阶段等 RUNNING 时永远等不到（ensure 会把 RUNNING 打回 STARTING，死循环）；
    runtime 恢复路径里卡 RUNNING 的实例由 monitor / ``_backfill_ready`` 收敛，不走本函数。
    prev SUCCESS 短路（幂等）；新 attempt（重试）重置实例 PENDING + 清 container_id/last_error，
    首发置 STARTING。返回 resource_key → attempt 最大 task（含本轮新建，供 ``_wait_rows``
    复用避免重复查库）；attempt 超限时按 ``fail_on_exhausted`` 二选一——True（首建期默认）
    ``fail_deployment`` 整部署停摆并返回 None；False（在线扩容）就地抛 ``LLMPodError``，
    让调用方走「只回滚新份」的收敛（它自己再查一遍 task 行既慢又与本次结果同源）。
    """
    deployment_id = dep.deployment_id
    keys = [_start_key(r) for r in rows]
    existing = _latest_by_key(await _task_rows_by_keys(keys))
    latest = dict(existing)  # 随 ensure 推进补入新建 task，最终返回给 _wait_rows
    to_dispatch: list[Task] = []
    over_cap_error: str | None = None
    async with get_db_context() as db:
        # irow 来自 _load_context 的已关闭 session（detached），写须在本 session 重取；
        # 一次 in_ 预取建映射，替代循环内逐实例 db.get（每轮 N 次点查）
        rows_by_id = {
            r.instance_id: r
            for r in (
                await db.execute(select(Instance).where(Instance.instance_id.in_([r.instance_id for r in rows])))
            ).scalars()
        }
        for irow in rows:
            if irow.status in (InstanceStatus.READY, InstanceStatus.STOPPED):
                continue
            # RUNNING（容器已起，正等 readiness）跳过——不得重置成 STARTING，否则 START
            # 阶段等 RUNNING 时永远等不到（ensure 会把 RUNNING 打回 STARTING，死循环）。
            # 注意 runtime 恢复路径同样依赖本跳过：RUNNING 实例由 monitor / _backfill_ready
            # 负责收敛（容器活着只是上报丢失），此处重发 START 会双起容器
            if irow.status == InstanceStatus.RUNNING:
                continue
            rk = _start_key(irow)
            prev = existing.get(rk)
            # 幂等短路：历史 START SUCCESS 且实例非「容器已死」才代表容器还活着——
            # CRASHED/FAILED 意味着容器已死，旧 SUCCESS 不再成立，须重走 attempt 建新 START
            if prev is not None and prev.status == TaskStatus.SUCCESS and irow.status not in INSTANCE_DEAD_STATUSES:
                continue  # 已启动成功（幂等短路）
            inst = InstanceSpec.model_validate(irow.spec or {})
            task, need_dispatch, denial = await task_ops.ensure_task_row(
                db,
                resource_type=TaskResourceType.INSTANCE,
                resource_key=rk,
                node_id=irow.assigned_node,
                task_type=TaskType.START_INSTANCE,
                payload=_start_payload(dep, model, adapter, config, inst).model_dump(mode="json"),
                # 看门狗：readiness 探活本身受 health_check.timeout_seconds 约束，
                # 任务级上限再加一倍余量（容器创建/启动也计入），与 scale 的
                # 驱动余量 _DRIVE_TIMEOUT_MARGIN 同口径；只兜挂死不误杀慢启动。
                # config 为 None（扩容回滚等无 config 调用路径）回落固定 30 分钟
                timeout_sec=(max(config.health_check.timeout_seconds * 2, 600) if config is not None else 1800),
            )
            if task is None:
                if denial == "concurrent":
                    # 并发已建（撞 active 唯一索引）：已在途，不是失败——下轮扫描收敛
                    continue
                over_cap_error = (
                    prev.last_error if prev is not None else None
                ) or f"实例启动失败（超自动重试上限 {MAX_AUTO_ATTEMPTS}）"
                break
            latest[rk] = task
            if need_dispatch:
                row = rows_by_id.get(irow.instance_id)
                if row is not None:
                    if task.attempt > 1:
                        # 重试：清上一轮的容器态/错误，回到 PENDING 等本轮 STARTING
                        row.status = InstanceStatus.PENDING
                        row.container_id = None
                        row.last_error = None
                    else:
                        row.status = InstanceStatus.STARTING
                to_dispatch.append(task)
        await db.flush()

    if over_cap_error is not None:
        # 在线扩容要的是「只回滚新份」，不是把整个部署停摆——它的调用方传
        # ``fail_on_exhausted=False`` 就地抛错，交给 scale 的失败收敛
        if not fail_on_exhausted:
            raise LLMPodError(over_cap_error)
        await fail_deployment(deployment_id, error=over_cap_error, message=RETRY_EXHAUSTED_MESSAGE)
        return None
    for t in to_dispatch:
        await task_ops.dispatch_task(t)
    return latest


async def _step_start_head(deployment_id: str) -> None:
    """起首起组（head_first 引擎=head，workers_first=workers）：ensure START task 下发后，
    **等容器进程起来（RUNNING）再切**——head_first：head 先起作 rendezvous 服务器，
    workers 才能连；workers_first 引擎反转首起组（head 启动即注册全部 workers 地址）。
    phase 流不变，只换驱动对象。

    等 RUNNING 让时间线呈现「启动主节点 → 容器已启动 → 健康检查」，而非 phase 事件
    先于实例上报（reconciler 下发 task 即切 phase，导致「等待健康检查」比「容器已启动」早）。
    """
    dep, model, config, irows = await _load_context(deployment_id)
    adapter = get_adapter(dep.engine)
    first_group, second_group = _startup_groups(adapter, irows)
    latest = await _ensure_start_tasks(dep, model, config, adapter, first_group)
    if latest is None:
        return
    # 次起组还有活干 → 先起它；没有次起组直接等就绪
    next_phase = DeploymentPhase.START_WORKERS if second_group else DeploymentPhase.WAIT_HEAD_READY
    await _wait_rows(
        deployment_id,
        first_group,
        retry_phase=DeploymentPhase.START_HEAD,
        next_phase=next_phase,
        target=frozenset({InstanceStatus.RUNNING, InstanceStatus.READY}),
        backfill=InstanceStatus.RUNNING,
        fail_msg="实例启动失败（容器未启动）",
        latest=latest,
    )


async def _step_start_workers(deployment_id: str) -> None:
    """起次起组（head_first 引擎=workers/headless，workers_first=head）：
    ensure START task 下发后等容器 RUNNING → WAIT_WORKERS_READY。无次起组直接跳过。"""
    dep, model, config, irows = await _load_context(deployment_id)
    adapter = get_adapter(dep.engine)
    _, second_group = _startup_groups(adapter, irows)
    if not second_group:
        await _goto_phase(deployment_id, DeploymentPhase.WAIT_HEAD_READY)
        return
    latest = await _ensure_start_tasks(dep, model, config, adapter, second_group)
    if latest is None:
        return
    await _wait_rows(
        deployment_id,
        second_group,
        retry_phase=DeploymentPhase.START_WORKERS,
        next_phase=DeploymentPhase.WAIT_WORKERS_READY,
        target=frozenset({InstanceStatus.RUNNING, InstanceStatus.READY}),
        backfill=InstanceStatus.RUNNING,
        fail_msg="实例启动失败（容器未启动）",
        latest=latest,
    )


# ---- WAIT（head / workers 共用双门判定）----


async def _wait_rows(
    deployment_id: str,
    rows: list[Instance],
    retry_phase: str,
    next_phase: str,
    *,
    target: frozenset[str],
    backfill: str,
    fail_msg: str,
    latest: dict[str, Task] | None = None,
) -> None:
    """双门等待一组实例到目标状态（READY=探活通过，或 RUNNING=容器已启动），再切下一阶段。

    达成：``Instance.status ∈ target``，或其 START task SUCCESS（双门兜底，补写 backfill）。
    START task FAILED：attempt 未耗尽 → 实例回 PENDING 重试，phase 经 ``_goto_phase(retry_phase,
    attempt=...)`` 回退或同相落重试行（set_phase 是唯一出口，同相也放行）；耗尽 →
    ``fail_deployment``。PENDING task 每轮重发。``latest`` 由 start_head/start_workers 传入
    （``_ensure_start_tasks`` 已取过并含本轮新建 task），其余调用方缺省自行查。
    """
    keys = [_start_key(r) for r in rows]
    if latest is None:
        latest = _latest_by_key(await _task_rows_by_keys(keys))

    now = datetime.now(timezone.utc)
    all_ok = True
    need_restart = False
    retry_attempt: int | None = None
    fail_error: str | None = None
    retry_exhausted = False
    for irow in rows:
        if irow.status in target:
            continue
        rk = _start_key(irow)
        t = latest.get(rk)
        # 等 RUNNING 门（backfill==RUNNING）：task 卡 PENDING/RUNNING 超时 → fail，防 node
        # 离线永久挂 START_HEAD（此门为「容器已启动」时序新增，需兜底失败而非无限重发）
        if backfill == InstanceStatus.RUNNING and t is not None and t.created_at is not None:
            age = (now - t.created_at).total_seconds()
            if age > _WAIT_RUNNING_TIMEOUT_S and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                fail_error = "容器启动超时（节点无响应），请检查节点状态"
                break
        if t is not None and t.status == TaskStatus.SUCCESS:
            # 双门兜底：task 已成功但实例上报丢失 → 补写 backfill（容器确定起来过）。
            # 仅瞬态（INSTANCE_TRANSIENT_STATUSES）允许补写——崩溃/停止竞态的实例
            # （FAILED/CRASHED/STOPPING/STOPPED）不得被复活成 RUNNING/READY：CRASHED 容器
            # 已死，补写后 all_ok 会带着死实例推进 phase，且 _ensure_start_tasks 对 READY
            # 短路不再重启，假运行状态无法自动恢复
            async with get_db_context() as db2:
                row = await db2.get(Instance, irow.instance_id)
                if row is not None and row.status in INSTANCE_TRANSIENT_STATUSES and row.status not in target:
                    row.status = backfill
                await db2.flush()
            if irow.status in INSTANCE_DEAD_STATUSES:
                all_ok = False  # 死实例不得静默过关（finalize 带死容器标 running）
            continue
        all_ok = False
        if t is None or t.status == TaskStatus.PENDING:
            if t is not None:
                await task_ops.dispatch_task(t)  # 重发遗留 PENDING
            continue
        if t.status == TaskStatus.RUNNING:
            continue
        if t.status == TaskStatus.FAILED:
            # 判耗尽只认末尾连续 FAILED 数：attempt 编号因唯一索引必须单调递增
            # （成功操作同样抬高它），拿 attempt 比上限会把手动 stop/start 过几次的
            # 部署一次失败就误判耗尽
            key_rows = await _task_rows_by_keys([rk])
            if trailing_fail_count(key_rows) >= MAX_AUTO_ATTEMPTS:
                fail_error = t.last_error or fail_msg
                retry_exhausted = True
            else:
                async with get_db_context() as db2:
                    row = await db2.get(Instance, irow.instance_id)
                    if row is not None:
                        row.status = InstanceStatus.PENDING
                        row.container_id = None
                        row.last_error = None
                    await db2.flush()
                need_restart = True
                # 一轮多实例以不同 attempt 失败时取最大（防 last-writer-wins 只记最后一行）
                retry_attempt = max(retry_attempt or 0, t.attempt + 1)

    if fail_error is not None:
        await fail_deployment(
            deployment_id,
            error=fail_error,
            message=RETRY_EXHAUSTED_MESSAGE if retry_exhausted else None,
        )
        return
    if all_ok:
        await _goto_phase(deployment_id, next_phase)
    elif need_restart:
        # 同相（phase 不变）也走 set_phase：带 attempt 时其守卫放行并落重试行（唯一出口）
        await _goto_phase(deployment_id, retry_phase, attempt=retry_attempt)


async def _creating_instances(deployment_id: str) -> tuple[ModelDeployment, list[Instance]] | None:
    """加载部署与其实例行（rank 升序）；部署不存在或非 CREATING（已取消/终态）返回 None。

    创建期 wait 类步骤的共用前置：两个 wait 门都要求「仅在 CREATING 推进」。
    """
    async with get_db_context() as db:
        dep = await db.get(ModelDeployment, deployment_id)
        if dep is None or dep.status != DeploymentStatus.CREATING:
            return None
        irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        irows.sort(key=lambda r: r.rank)
        return dep, irows


async def _step_wait_workers_ready(deployment_id: str) -> None:
    """次起组全就绪（stable 判定）→ WAIT_HEAD_READY；失败重试回 START_WORKERS。"""
    loaded = await _creating_instances(deployment_id)
    if loaded is None:
        return
    dep, irows = loaded
    _, second_group = _startup_groups(get_adapter(dep.engine), irows)
    if not second_group:
        await _goto_phase(deployment_id, DeploymentPhase.WAIT_HEAD_READY)
        return
    await _wait_rows(
        deployment_id,
        second_group,
        DeploymentPhase.START_WORKERS,
        DeploymentPhase.WAIT_HEAD_READY,
        target=frozenset({InstanceStatus.READY}),
        backfill=InstanceStatus.READY,
        fail_msg="实例启动失败（readiness 未通过）",
    )


async def _step_wait_head_ready(deployment_id: str) -> None:
    """整集群就绪门：head /health 通过（此时 workers 已连上 rendezvous）→ FINALIZING。

    head 失败重试目标仍是 START_HEAD：重走对已 READY workers 是 no-op（状态跳过），
    head 容器重建后 workers 经 rendezvous 重连。
    """
    loaded = await _creating_instances(deployment_id)
    if loaded is None:
        return
    _, irows = loaded
    heads, _ = _split_head_workers(irows)
    await _wait_rows(
        deployment_id,
        heads,
        DeploymentPhase.START_HEAD,
        DeploymentPhase.FINALIZING,
        target=frozenset({InstanceStatus.READY}),
        backfill=InstanceStatus.READY,
        fail_msg="实例启动失败（readiness 未通过）",
    )


# ---- FINALIZING ----


async def _step_finalize(deployment_id: str) -> None:
    """产出 ModelEndpoint + status=running（编排器 _finalize 已含 cancel/endpoint/终态三重守卫）。"""
    orch = get_orchestrator()
    async with get_db_context() as db:
        dep, model = await deployment_service.load_deployment(db, deployment_id)
        adapter = get_adapter(dep.engine)
        irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        irows.sort(key=lambda r: r.rank)
        await orch._finalize(db, dep, _specs(irows), adapter, model)
    await _goto_phase(deployment_id, DeploymentPhase.RUNNING)
    log.info("deployment %s running", deployment_id)


# ---- RUNNING 部署的运行期自愈（崩溃实例自动重启）----


async def _backfill_ready(deployment_id: str, irows: list[Instance], latest: dict[str, Task] | None = None) -> None:
    """lost-report 兜底：瞬态实例 + 最新 START task SUCCESS → 补写 READY（对齐 _wait_rows 双门）。

    仅 ``INSTANCE_TRANSIENT_STATUSES`` 允许补写——CRASHED/FAILED/STOPPING/STOPPED 不被
    复活（同 ``_wait_rows`` 守卫语义，防部署带死容器标 READY）。
    ``latest`` 给定时直接复用调用方刚查到的 task 映射（在线扩容每轮都在拿它，逐分片再查
    一遍既慢又可能与那批结果不同源）；缺省按分片自行查。
    """
    async with get_db_context() as db:
        # 待补写行一次预取 task 索引 + 统一补写后一次性投影（逐行开会话/逐行全量
        # 投影是每 5s 自愈循环里的放大器：一次命中多个丢失上报时原实现按行数付 N 次）
        pending = [r for r in irows if r.status in INSTANCE_TRANSIENT_STATUSES and r.status != InstanceStatus.READY]
        if latest is not None:
            t_by_key = dict(latest)
        else:
            t_by_key = _latest_by_key(await _task_rows_by_keys([_start_key(r) for r in pending]))
        rows_by_id = {
            r.instance_id: r
            for r in (
                await db.execute(select(Instance).where(Instance.instance_id.in_([r.instance_id for r in pending])))
            ).scalars()
        }
        changed = False
        for irow in pending:
            t = t_by_key.get(_start_key(irow))
            if t is None or t.status != TaskStatus.SUCCESS:
                continue
            row = rows_by_id.get(irow.instance_id)
            if row is not None and row.status in INSTANCE_TRANSIENT_STATUSES and row.status != InstanceStatus.READY:
                row.status = InstanceStatus.READY
                changed = True
        if changed:
            # 补写 READY 也要刷新部署健康：READY 上报若经隧道丢失（离线丢弃不重试），
            # 本路径是唯一会写回就绪的出口，缺它部署会永久卡 degraded
            await deployment_projection.project_upwards(db, deployment_id)
        await db.flush()


async def reconcile_runtime_instances(deployment_id: str) -> None:
    """RUNNING 部署的运行期自愈：异常分片按**引擎整份重启**（指数退避）驱动到 READY。

    整份重启：任一分片真死（CRASHED/FAILED）⇒ 同引擎已就绪分片一并重置——集体
    通信状态随 peer 崩溃已脏，接回脏状态不如重启干净（模型在盘、镜像缓存，重启秒级）。
    跨副本互不影响（副本=隔离边界）；PD 的跨引擎配对重置随 PD 实装扩展。

    指数退避：末尾连续失败后等 10s→2×→5min 封顶再发下一次 attempt（attempt
    上限 MAX_AUTO_ATTEMPTS 保留，超限 fail_deployment）——无退避的固定短间隔会在
    故障期放大负载。退避窗内本轮直接跳过（controller 下轮再来看）。

    复用 ``_ensure_start_tasks``（active 去重 + attempt 预算 + PENDING 每轮重发 + 状态
    重置），不重写 dispatch 循环。head-first 门控：首起组容器未 RUNNING 前只驱动首起组，
    容器起来后次起组同轮放行（判据是容器已启动而非 READY——多机 head /health 需 workers
    连上，等 READY 再放行次起组会互相死锁）。

    与启动期 ``_wait_rows`` 的差异：**不带 300s 启动超时门**——RUNNING 部署因 node
    短暂离线而 auto-fail 是错的（部署本来健康），node 回来自愈；节点永久离线由用户
    stop/delete 收敛。卡 RUNNING 的实例不在驱动集（``_ensure_start_tasks`` 跳过）——
    容器活着只是上报丢失，由 monitor 上报 CRASHED / ``_backfill_ready`` 补 READY 收敛。

    TODO(探测健康)：当前触发集是「推论健康」的补集——Instance.status 非 READY 即认为
    异常（信号源为 Node monitor 容器 inspect + task 上报）。node 掉线时 monitor 停报，
    实例仍 READY 就永远不会进本循环，部署保持 running。未来实现探测健康（周期对
    ModelEndpoint 逐行探活 api_base/health——权威清单在端点表，非部署单值代表地址）
    后，探测失败也应并入本恢复路径（见 controller 触发点 TODO）。
    """
    dep, model, config, irows = await _load_context(deployment_id)
    if dep.status != DeploymentStatus.RUNNING:
        return  # 双守卫：已终态/收敛中的部署不参与（触发器只挑 RUNNING，此处防竞态）
    if any(r.status in (InstanceStatus.STOPPING, InstanceStatus.STOPPED) for r in irows):
        return  # 反停止竞态：任一实例停止进行中/已停 → 不驱动，避免与 StopService 打架
    adapter = get_adapter(dep.engine)

    # lost-report 兜底：task SUCCESS 但实例仍卡瞬态 → 补写 READY（否则永被当异常重启）
    await _backfill_ready(deployment_id, irows)

    abnormal = [r for r in irows if r.status in RUNTIME_ABNORMAL_INSTANCE_STATUSES]
    if not abnormal:
        return

    # 扩容中的新份分片不进自愈（归 scale 协程驱动）：provisioning 副本的容器天然
    # PENDING/STARTING，自愈提前发 START 会绕过 staging（模型没下完就起容器）。
    # phase≠SCALING 时不可能有 provisioning 副本（扩容全程持 SCALING，全胜/回滚后才
    # 复位）——直接跳过查询，稳态 degraded 部署每 tick 免两条查询
    if dep.phase == DeploymentPhase.SCALING:
        provisioning_engine_ids = await _provisioning_engine_ids(deployment_id)
        if provisioning_engine_ids:
            abnormal = [r for r in abnormal if r.model_deployment_instance_id not in provisioning_engine_ids]
            if not abnormal:
                return

    # 退避门：末尾连续失败 → 距上次失败不足 backoff(trailing) 的分片本轮不驱动。
    # 一次批量取全部 abnormal 分片的 task 行再按 key 分组（N 分片一条查询，不再逐个 N+1）
    now = datetime.now(timezone.utc)
    drivable: list[Instance] = []
    tasks_by_key: dict[str, list[Task]] = defaultdict(list)
    for t in await _task_rows_by_keys([_start_key(r) for r in abnormal]):
        tasks_by_key[t.resource_key].append(t)
    for r in abnormal:
        rows = tasks_by_key.get(_start_key(r), [])
        trailing = trailing_fail_count(rows)
        if trailing > 0:
            last_failed = max(
                (t.finished_at or t.created_at or now for t in rows if t.status == TaskStatus.FAILED),
                default=None,
            )
            if last_failed is not None:
                waited = (now - last_failed).total_seconds()
                if waited < runtime_restart_backoff_sec(trailing):
                    log.info(
                        "runtime self-heal %s: instance %s 退避中（连续失败 %d，已等 %.0fs/%.0fs）",
                        deployment_id[:8],
                        r.instance_id[:8],
                        trailing,
                        waited,
                        runtime_restart_backoff_sec(trailing),
                    )
                    continue
        drivable.append(r)
    if not drivable:
        return

    # 整份重启（引擎粒度）：真死分片所在的引擎，其已就绪分片一并重置（取消旧 SUCCESS
    # START task 防幂等短路 + 实例回 PENDING），与本轮驱动的死分片同轮重启
    dead_engines = {_key_args(r)[0] for r in drivable if r.status in INSTANCE_DEAD_STATUSES}
    sibling_ids: set[str] = set()
    siblings: list[Instance] = []
    if dead_engines:
        siblings = [
            r
            for r in irows
            if _key_args(r)[0] in dead_engines and r.status == InstanceStatus.READY and r not in drivable
        ]
        if siblings:
            # 先直停兄弟容器再重发 START：node 执行器对同名运行中容器幂等短路
            # （already_running → 跳过 deploy），不预停会把脏进程原样接回，「整份
            # 重启」名存实亡。各容器互不依赖，并发停（best-effort：停失败——节点
            # 失联——时 START 也到不了）
            async def _prestop(r):
                if not r.assigned_node:
                    return
                try:
                    await dispatch.stop_container(
                        r.assigned_node,
                        deployment_container_name(deployment_id, r.instance_id),
                    )
                except Exception as e:
                    log.warning(
                        "runtime self-heal %s: 兄弟容器预停失败 inst=%s（best-effort）: %s",
                        deployment_id[:8],
                        r.instance_id[:8],
                        e,
                    )

            await asyncio.gather(*[_prestop(r) for r in siblings])
            async with get_db_context() as db:
                sib_keys = [_start_key(r) for r in siblings]
                cancel_rows = [
                    t
                    for t in (await db.execute(select(Task).where(Task.resource_key.in_(sib_keys)))).scalars()
                    if t.status in TASK_INCOMPLETE_STATUSES
                ]
                for t in cancel_rows:
                    t.status = TaskStatus.CANCELLED
                # 库里置 CANCELLED 之外还要下发 $cancel：在途 START 的 node 端执行器
                # 不知道任务已取消，可能在预停之后又把容器拉起来（与 StopService 口径
                # 一致，见 stop.py 的 cancel + dispatch_cancel_task）
                for t in cancel_rows:
                    await task_ops.dispatch_cancel_task(t.node_id, t.id)
                for r in siblings:
                    row = await db.get(Instance, r.instance_id)
                    if row is not None:
                        row.status = InstanceStatus.PENDING
                        row.container_id = None
                await db.flush()
                log.info(
                    "runtime self-heal %s: 引擎 %s 整份重启（预停并重置已就绪分片 %s）",
                    deployment_id[:8],
                    sorted(dead_engines),
                    [r.instance_id[:8] for r in siblings],
                )

    # head-first 门控（与启动期 START_HEAD→START_WORKERS 同序）：首起组容器未 RUNNING 前
    # 只驱动首起组——rendezvous 服务器没起来，次起组起容器只会连不上空烧 attempt；首起组
    # 容器全 RUNNING/READY 后次起组同轮放行。判据必须是「容器已启动」而非「READY」：
    # vLLM 多机 head /health 要 workers 连上才 200，等 READY 再放行 workers 会互相死锁
    # （head 永不 READY → workers 永不被驱动 → 每轮烧 attempt 直至 fail_deployment）。
    # 门控只看**本轮要驱动**的首起组成员：别份起不来的 head（节点失联/退避窗/provisioning）
    # 不在驱动集内，不该挡住健康副本 workers 的自愈——rendezvous 约束按引擎份内生效
    first_group, second_group = _startup_groups(adapter, irows)
    drive_ids = {r.instance_id for r in drivable}
    # 整份重启重置出的兄弟分片本轮就要跟随驱动——直接取重置集（irows 是进 session
    # 前的快照，读不到重置后的 PENDING，从快照按状态重算会漏掉刚重置的分片）
    sibling_ids.update(r.instance_id for r in siblings)
    drive_pool = drive_ids | sibling_ids
    pending_first = [r for r in first_group if r.instance_id in drive_pool]
    # 首起组是否仍有未起成员：快照里刚重置的兄弟挂着旧状态（如 READY），按未起算
    first_booting = any(
        r.instance_id in sibling_ids or r.status not in (InstanceStatus.RUNNING, InstanceStatus.READY)
        for r in pending_first
    )
    drive = pending_first if first_booting else pending_first + [r for r in second_group if r.instance_id in drive_pool]
    if drive:
        await _ensure_start_tasks(dep, model, config, adapter, drive)


# ---- 失败收敛 ----


async def fail_deployment(
    deployment_id: str,
    error: str | None = None,
    *,
    message: str | None = None,
) -> None:
    """容器阶段失败收敛：派发停止全部容器（含已 SUCCESS 的 worker）+ 标 failed。

    只派发停止（cancel 活跃 START + ensure STOP task），不阻塞等待——容器由 Node 收敛停掉。
    **不在此释放 VRAM / 删 assignment**：多机部署里 worker 可能已先于 head 到达 SUCCESS（容器在
    跑、占着 GPU），若立即释放 VRAM，Manager 会把这些 GPU 当空闲重新分配，导致同一 GPU 被两个
    容器抢占（OOM/冲突）。VRAM/assignment 释放延迟到 controller 对 ``failed`` 部署的收敛——待
    实例全 STOPPED 后由 converge_stop_deployment 释放（容器确已退出，无抢占风险）。

    ``message`` 覆盖编排行文案：重试耗尽类调用点传「部署异常终止（已达最大重试次数）」做摘要
    （实例 FAILED 行已带崩溃详情，不重复）；一次性失败（镜像解析/配置错误）不传，缺省
    ``部署失败：{error}`` 带真实原因，避免谎称重试耗尽。
    """
    log.warning("deployment %s 容器阶段失败: %s", deployment_id[:8], error)
    try:
        from llmpod.apps.manager.deployment.stop import StopService

        await StopService().dispatch_stop(deployment_id)
    except Exception as e:
        log.warning("fail_deployment %s: dispatch_stop 失败: %s", deployment_id[:8], e)
    async with get_db_context() as db:
        await deployment_service.mark_failed_if_active(db, deployment_id, error=error, message=message)
        # STOPPING：失败收敛子态（set_phase 守卫允许终态部署写 STOPPING）
        await deployment_service.set_phase(db, deployment_id, DeploymentPhase.STOPPING)


async def converge_stop_deployment(deployment_id: str) -> None:
    """stopped/failed/deleting 残留收敛（controller 崩溃兜底；路由内已同步停止过一次）。

    与 stop/delete 路由同一 ``StopService``（纯 stop，幂等可重入）：确保容器全部 STOPPED。
    - ``deleting``：纯停后 **label 扫除删容器**（delete 独有）→ 级联删行 + 释放 VRAM；
    - ``stopped``/``failed``：保留部署行 + **保留容器**（failed 留崩溃容器供 RPC 取证、
      stopped 留停止容器），释放 VRAM + 删 assignment（failed 原先不释放会泄漏，此处修）。

    稳态快路径：stopped/failed 且实例全 STOPPED + assignment 已清 → 无事可做直接返回——
    避免每轮扫描重复跑 StopService/释放。``deleting`` 永不走快路径（崩溃窗口内 cascade
    未完成时行仍在，必须继续收敛）。
    """
    from llmpod.apps.manager.deployment.stop import StopService

    async with get_db_context() as db:
        dep = await db.get(ModelDeployment, deployment_id)
        if dep is None or dep.status not in DEPLOYMENT_TERMINAL_STATUSES:
            return
        status = dep.status
        irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        arows = list(
            (
                await db.execute(
                    select(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id == deployment_id)
                )
            ).scalars()
        )

    # stopped/failed 稳态：全 STOPPED + assignment 已清 + 账本无残留即收敛完成。
    # 账本残留（纯 CPU 部署没有 assignment 行，收敛可能还没走到释放）必须落下去释放，
    # 否则 RAM claim 一直占着，其他部署永远看不到这部分内存。
    if (
        status in (DeploymentStatus.STOPPED, DeploymentStatus.FAILED)
        and not arows
        and all(r.status == InstanceStatus.STOPPED for r in irows)
        and not get_orchestrator().ledger.has_reservation(deployment_id)
    ):
        return

    # 纯停容器（不删）；容器删除仅 deleting 分支的 cleanup 触发。
    # 停止未确认（离线/超时，容器可能仍在运行）时不得释放——否则同卡被调度给
    # 别的部署，双容器抢卡 OOM。留下一轮 converge 继续收敛（幂等）。
    all_stopped = await StopService().stop_instances(deployment_id)

    if status == DeploymentStatus.DELETING:
        # delete 独有：label 扫除删容器 + 级联删行 + 释放 VRAM。
        # deleting 不受 all_stopped 门控：label 扫除本身是 docker 级强制清理
        # （rm -f），不依赖 STOP task 的确认路径。
        node_ids = {r.assigned_node for r in irows if r.assigned_node}
        await get_orchestrator().cleanup_deployment_containers(deployment_id, node_ids)
        async with get_db_context() as db:
            await deployment_service.delete_deployment_cascade(db, deployment_id)
        try:
            await get_orchestrator().release_vram(deployment_id)
        except Exception as e:
            log.warning("converge_stop(delete): release_vram failed: %s", e)
    elif all_stopped:
        # stopped/failed 且停止已确认：保留部署行 + 保留容器；释放 VRAM + 删 assignment（幂等）
        try:
            await get_orchestrator().release_vram(deployment_id)
        except Exception as e:
            log.warning("converge_stop(%s): release_vram failed: %s", status, e)
        async with get_db_context() as db:
            await deployment_service.delete_assignments(db, deployment_id)
    else:
        log.warning(
            "converge_stop(%s): 实例停止未确认（离线/超时），本轮不释放 VRAM/assignment: %s",
            status,
            deployment_id[:8],
        )


async def _goto_phase(deployment_id: str, phase: str, attempt: int | None = None) -> None:
    async with get_db_context() as db:
        await deployment_service.set_phase(db, deployment_id, phase, attempt=attempt)
