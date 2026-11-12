"""PlacementService：模型分发的声明式编排（Manager 侧）。

职责：
- ``ensure_present(model_id, node_id)``：声明「需要 model 在 node 上 present」，按需创建
  ENSURE_MODEL 任务（active 去重 + attempt 自增 + 上限 3），best-effort 下发到 node。
- ``recompute_demand()``：全局重算 ``(model,node)`` 需求——无活跃部署需要的 →
  ``desired_state=absent`` + 取消其 active 下载（不误杀仍被需要的）。

去重兜底：DB 层 ``uq_task_active_resource`` partial unique index（同 resource_key 同时只
一个 active task）——应用层 check + ``FOR UPDATE`` 之外再加一道 DB 级保险，捕并发竞态。

secrets：仅内存随 RPC 下发，**不入 task.payload / 库 / 日志**（见 ``core.secrets``）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata

from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from llmpod.apps.manager.deployment.task_ops import (
    _next_attempt,
    task_to_payload,
)
from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_ACTIVE_STATUSES,
    MAX_AUTO_ATTEMPTS,
    NodeModelDesiredState,
    NodeModelState,
)
from llmpod.config.constants import (
    TASK_INCOMPLETE_STATUSES,
    TaskResourceType,
    TaskStatus,
    TaskType,
)
from llmpod.config.enums import ModelSource
from llmpod.config.settings import settings
from llmpod.core.advertise import manager_http_origin
from llmpod.core.exceptions import LLMPodError, LLMPodValidationError, NotFound
from llmpod.core.secrets import build_secrets
from llmpod.db import get_db_context
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.tunnel.schemas import EnsureModelPayload, TaskPayload
from llmpod.utils.crypto import generate_uuid

log = logging.getLogger("llmpod.apps.manager.deployment.placement")

# ENSURE_MODEL 看门狗（任务级）：大模型下载（node 内部 3 次重试 + 断点续传）合法耗时
# 以小时计，取值只兜「await 永不返回」的挂死，不误杀慢下载。staging 轮询方的总预算
# 由此派生（orchestrator 部署期 / scale 扩容期），保证轮询方不先于内层任务中止
ENSURE_MODEL_WATCHDOG_S = 12 * 3600

# 部署状态视为「仍需模型」——停止/删除后不再计入需求
_DEMANDING_STATUSES = DEPLOYMENT_ACTIVE_STATUSES


def model_resource_key(model_id: int, node_id: str) -> str:
    """``resource_key = model:{model_id}:{node_id}``"""
    return f"model:{model_id}:{node_id}"


async def task_status_map(db, task_ids: dict[str, str]) -> dict[str, str]:
    """``任务 id → status``（``task_ids`` = node → 任务 id；空入参返回空表）。

    staging 放行只认复核任务结论：``node_model.status`` 是上一次上报的缓存，本次复核
    发现配套文件缺失（如 mmproj 被删）时它仍是 present——据此放行会让缺权重的容器上线。
    """
    if not task_ids:
        return {}
    rows = list((await db.execute(select(Task).where(Task.id.in_(list(task_ids.values()))))).scalars())
    return {t.id: t.status for t in rows}


def model_extra_files(model) -> list[str]:
    """模型的配套文件清单（repo 相对路径）——随主模型一起下载/校验（如 GGUF 视觉投影器）。

    清单随 payload 下发，node 侧两处消费：下载 allow_patterns 收窄、判在位逐项比对。
    两处吃的是同一份（payload 里的 ``extra_files``），这里改口径即两端同步。
    """
    return [model.mmproj_file] if model.mmproj_file else []


def mmproj_path_for(model, base_dir: str | None) -> str | None:
    """视觉投影器在节点上的绝对路径 = ``base_dir`` / repo 相对路径；无投影器或目录未知 → None。

    单一取用点：「与主模型同落 canonical 目录、按 repo 相对结构保留」这条目录规则在
    规划期（预估值，用 canonical 目录）与 staging 落实期（权威值，用节点回报的 local_dir）
    都要用——两处各拼一次，改目录布局就得同步改两处，漏改即写出容器里打不开的
    ``--mmproj``。
    """
    if not model.mmproj_file or not base_dir:
        return None
    return str(PurePosixPath(base_dir) / model.mmproj_file)


def slugify_name(name: str) -> str:
    """``model.name`` → 路径安全 slug：取末段（repo 写法 ``Qwen/Qwen3-8B`` → ``Qwen3-8B``）、
    小写、非字母数字→``-``、去头尾、截断、空回退 ``model``。

    保留 Unicode 字母/数字（中文等可读名不丢）；NFKC 归一化把全角/兼容字符折成标准形。
    目录名与注册名直接对应（``/data/models/qwen3-8b``），用户可用 ``hf download
    --local-dir`` / modelscope ``--local_dir`` 直接在 store 里预置与管理。
    """
    s = unicodedata.normalize("NFKC", (name or "")).lower()
    s = s.rsplit("/", 1)[-1]  # repo 写法取末段：org 前缀不进目录名
    # [^\w]+ 一次吞掉全部非字母数字连片（含连续 ``-``），无需二次塌缩；截 48 再 strip 两端 ``-``
    s = re.sub(r"[^\w]+", "-", s)[:48].strip("-")
    return s or "model"


def canonical_local_dir(model: Model) -> str:
    """模型目录（恒为绝对路径；规划期估计值，权威路径以节点回报为准）：

    - local/manager 源：**= source_url**（node 与 manager 常共享同一存储挂载，同机
      直接用源目录、远端下载到同一路径——所有节点 model_path 恒相同，多机 TP 不分叉；
      内容身份由目录本身承载）。source_url 为单文件时取其所在目录，文件名由
      source_subpath 定位。路径合法性（store 内、非根）由 ``ensure_present`` 下发前校验。
    - hf/oss 等远程源：``{store}/{name-slug}``（slug 取 name 末段），下载器以
      ``local_dir`` 模式直写该目录——与 ``hf download --local-dir`` / modelscope
      ``--local_dir`` 的落盘布局一致，用户在 store 内用官方 CLI 预置/管理的目录平台
      直接可用；重复下发由 ``hf_hub_download`` 的 etag 元数据校验跳过（不搬字节）。
    """
    if ModelSource.is_manager_source(model.source):
        d = manager_source_dir(model.source_url)
        if d is None:
            return os.path.abspath(settings.model_store_dir)  # 无源路径：占位，下发前被拦
        return str(d)
    return os.path.abspath(f"{settings.model_store_dir}/{slugify_name(model.name)}")


def manager_source_dir(source_url: str | None) -> Path | None:
    """local/manager 源的落盘目录推导（canonical / 建模与下发校验 / 清扫在役判定 /
    删除级联共用，改路径规则只改这里）：

    ``source_url`` → expanduser → 单文件取所在目录 → resolve。空 source_url → None。
    """
    raw = (source_url or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_file():
        p = p.parent
    return p.resolve()


def validate_manager_source(source: str | None, source_url: str | None) -> None:
    """local 源 source_url 合法性校验（建模 create/update 与下发 ensure_present 共用）。

    必须解析为 ``model_store_dir`` 之内的目录，且不是 store 根 / upload 根——坏路径
    在入库或下发前显式报错，不静默落成「创建成功、部署才报错」的行。非 local 源恒通过。
    """
    if not ModelSource.is_manager_source(source):
        return
    resolved = manager_source_dir(source_url)
    store_root = Path(settings.model_store_dir).expanduser().resolve()
    ok = (
        resolved is not None
        and resolved != store_root
        and resolved.is_relative_to(store_root)
        and resolved != (store_root / "upload")
    )
    if not ok:
        raise LLMPodValidationError(
            f"local 源 source_url 不是 {settings.model_store_dir} 之内的合法目录: {source_url!r}（请修正模型来源路径）"
        )


def _is_manager_source(model_or_source) -> bool:
    """是否走 Manager 文件服务下发（local/manager 源）。"""
    source = model_or_source.source if hasattr(model_or_source, "source") else model_or_source
    return ModelSource.is_manager_source(source)


def _distribution_base_url() -> str:
    """Manager 文件服务只使用管理员配置的可信 origin，不采信节点可控 Host 头。"""
    return f"{manager_http_origin()}/api/v1/distribution"


def _mint_download_token(model_id: int) -> str:
    """为 local 源下载签发独立签名域的短期 HMAC token。"""
    from llmpod.apps.webserver.utils.download_token import mint_download_token, download_signing_key

    return mint_download_token(download_signing_key(settings), model_id, settings.download_token_ttl)[0]


def _manager_source_size_bytes(model) -> int | None:
    """local 源模型体积（模型根目录递归汇总，字节）；非 local 源/路径不可读返回 None。

    在线程池里跑（rglob 大目录阻塞事件循环）；失败不阻断下发（None → node 回落
    固定阈值预检）。
    """
    from pathlib import Path

    if not _is_manager_source(model) or not model.source_url:
        return None
    root = Path(model.source_url).expanduser()
    if not root.is_dir():
        return None  # 单文件源：build_manifest 会按文件口径处理，这里不猜
    total = 0
    try:
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
    except OSError:
        return None
    return total or None


class ModelStagingGate:
    """模型在节点就位的复核门（部署 staging 与在线扩容共用）。

    放行只认**复核任务终态** ``SUCCESS`` 且 ``node_model`` present：``node_model.status``
    是节点上一次上报的缓存，本次复核发现配套文件缺失（如 mmproj 被删）时它仍是 present，
    据此放行会让缺权重的容器上线。

    复核任务跨轮保留（同一 node 只派发一次）——每轮重派会不断新建任务、永远等不到
    同一任务的终态，与 node_model 缓存来回打架；
    终态失败才清出，下一轮由 ``ensure_present`` 建下一 attempt。

    重试预算按**本 staging 会话**计（观察到几次任务失败就算几次）：模型下载任务是
    跨部署共享的（同 (model,node) 一个物理下载，同一节点同一模型同时只允许一个下载
    在跑，并发写同一目录会损坏文件），但别的部署/上一轮部署留下的失败历史不再锁住
    本部署——新部署、手动启动、扩容各自新起一个 gate（自带满额预算），与拉取键按
    部署隔离是同一语义。
    """

    def __init__(self, model_id: int) -> None:
        self._model_id = model_id
        self._placement = PlacementService()
        self._tasks: dict[str, str] = {}  # node_id → 复核任务 id（跨轮保留）
        self._statuses: dict[str, str] = {}  # task_id → 上一轮读到的 status
        self._failed_seen: dict[str, int] = {}  # node_id → 本会话观察到的失败任务数

    async def dispatch(self, pending: set[str]) -> None:
        """为尚未派发的节点触发/续接复核任务（幂等驱动，无返回值）。

        预算按会话内观察到的任务失败数计（达 ``MAX_AUTO_ATTEMPTS`` 不再建新任务——
        预算耗尽只停「建任务」，不停「观察」，终态收敛交给 :meth:`advance`）。

        已记录节点不重复派发（防与在途执行打架），但**PENDING 态补发**：首发 RPC
        瞬时失败（node 在线、通道抖动）时任务留 PENDING，若无人重发会一直挂到
        节点重连——``ensure_present`` 幂等，对 PENDING 行重走下发路径。
        """
        # 派发判据必须重读任务现态，不能用 advance 留下的上一轮快照：复核任务在
        # 权重已就位时秒级完成（PENDING→SUCCESS 全程 < 一个轮询间隔），拿隔轮快照
        # 判「PENDING 需补发」会让 ensure_present 每轮重铸新任务（上一条已 SUCCESS
        # 不算 active），终态永远观察不到 → staging 无限轮询。
        async with get_db_context() as db:
            self._statuses = await task_status_map(db, self._tasks)
        for node_id, task_id in list(self._tasks.items()):
            status = self._statuses.get(task_id)
            if status == TaskStatus.FAILED:
                # 失败计入本会话预算（判死只认自己观察到的失败，共享键上的历史
                # 失败不锁本部署），清出派发记录让下一轮建新 attempt
                self._failed_seen[node_id] = self._failed_seen.get(node_id, 0) + 1
                self._tasks.pop(node_id, None)
            elif status == TaskStatus.CANCELLED:
                self._tasks.pop(node_id, None)
        for node_id in sorted(pending):
            if self._failed_seen.get(node_id, 0) >= MAX_AUTO_ATTEMPTS:
                # 本会话预算耗尽：不再建新任务（否则 staging 轮询无限重铸），收敛交 advance
                self._tasks.pop(node_id, None)
                continue
            recorded = self._tasks.get(node_id)
            if recorded is not None:
                status = self._statuses.get(recorded)
                if status in (TaskStatus.RUNNING, TaskStatus.SUCCESS):
                    continue  # 在途/已成功：不打扰
                # PENDING / 未知（首读尚未发生）：补发一次（幂等）
            task_id = await self._placement.ensure_present(self._model_id, node_id)
            if task_id is None:
                # 并发建任务撞 active 唯一索引（别的执行者刚建了同键在途行）：
                # 不是失败——下一轮现态重读会看到它，无需动作
                continue
            self._tasks[node_id] = task_id

    async def advance(self, db, pending: set[str]) -> tuple[set[str], dict[str, NodeModel]]:
        """读共享复核任务最新行 + ``node_model``，返回（已确认就位的 node, node → node_model 行）。

        放行判据统一为「共享键最新任务行 SUCCESS + node_model PRESENT」：下载任务跨部署
        共享（同 (model,node) 一个物理下载），最新行无论哪个会话建的下成都算——本会话
        预算耗尽后不再建任务，别的会话下成也要能放行（否则轮询永远等不到结论）。
        PRESENT 缓存单独不足为凭：陈旧缓存 + 最新 FAILED 不放行（缺权重容器上线比多等
        一轮更糟）。
        本会话失败预算耗尽（``_failed_seen`` 达 ``MAX_AUTO_ATTEMPTS``）且 ``node_model``
        终态 FAILED → 抛 ``LLMPodError``：下载永久失败，继续等没有意义。预算未尽时
        ``dispatch`` 已建下一 attempt，照常继续轮询；既放不了行也判不了死的（如预算
        耗尽、缓存停在 PRESENT、最新行 FAILED）由调用方总预算兜底。
        """
        rows = list((await db.execute(select(NodeModel).where(NodeModel.model_id == self._model_id))).scalars())
        by_node = {r.node_id: r for r in rows if r.node_id in pending}
        # 共享键最新行（attempt 最大者）一次批量取回：确认与判死都只认它
        latest_by_node: dict[str, Task] = {}
        keys = [model_resource_key(self._model_id, nid) for nid in pending]
        if keys:
            for t in (await db.execute(select(Task).where(Task.resource_key.in_(keys)))).scalars():
                cur = latest_by_node.get(t.node_id)
                if cur is None or t.attempt > cur.attempt:
                    latest_by_node[t.node_id] = t
        over_cap = {nid for nid in pending if self._failed_seen.get(nid, 0) >= MAX_AUTO_ATTEMPTS}
        dead = sorted(
            nid for nid in over_cap if by_node.get(nid) is not None and by_node[nid].status == NodeModelState.FAILED
        )
        if dead:
            raise LLMPodError(
                f"模型下载失败（超自动重试上限 {MAX_AUTO_ATTEMPTS}）model={self._model_id} "
                f"node={','.join(dead)}: {by_node[dead[0]].last_error or ''}"
            )
        confirmed = {
            nid
            for nid in pending
            if by_node.get(nid) is not None
            and by_node[nid].status == NodeModelState.PRESENT
            and latest_by_node.get(nid) is not None
            and latest_by_node[nid].status == TaskStatus.SUCCESS
        }
        return confirmed, by_node


class PlacementService:
    """模型分发声明式编排（无状态方法；按需实例化）。"""

    async def ensure_present(self, model_id: int, node_id: str) -> str | None:
        """声明需要 model 在 node 上 present。

        返回 task_id（新建的，或同 resource_key 已存在的 active task）。``None`` = 并发建
        任务撞 active 唯一索引（下一轮现态重读接管）。**不因 ``node_model.status=present``
        短路**——那只是节点上次上报的缓存，每次部署都要一个复核任务去实际校验权重集
        （见方法内注释）。失败重试预算不在本方法判：模型下载任务跨部署共享（同 (model,node)
        一个物理下载），预算由调用方 :class:`ModelStagingGate` 按 staging 会话计——别的
        部署/上一轮留下的失败历史不锁新会话。
        """
        resource_key = model_resource_key(model_id, node_id)
        # 事务内完成全部 DB 决策；事务外 best-effort 下发（失败不回滚 task，等 redrive 重发）
        async with get_db_context() as db:
            model = await db.get(Model, model_id)
            if model is None:
                raise NotFound("Model", model_id)

            # 1. upsert node_model + 行锁（desired=present）。
            #    注意：present 只是 node 上次上报的缓存声明，磁盘真相（目录被清/文件被删）
            #    Manager 看不见——不据此短路。每次部署都 ensure 一个复核任务：node 侧
            #    check-existing 快路径秒级校验完整权重集，真在位 → 立即 SUCCESS；不在位 →
            #    自然转入下载。防「status=present 但目录空」直通容器（空权重上线）。
            nm = await self._lock_node_model(db, model_id, node_id)
            nm.desired_state = NodeModelDesiredState.PRESENT

            # 2. 同 resource_key 的全部历史 task：一条查询取出（active 命中判 + attempt 推导共用）
            rows = list((await db.execute(select(Task).where(Task.resource_key == resource_key))).scalars())
            task = next((t for t in rows if t.status in TASK_INCOMPLETE_STATUSES), None)
            # 仅在「需新建」或「卡在 PENDING 未触达 node」时下发；RUNNING = node 已在跑，
            # staging 每轮轮询不重复 dispatch（省 build_secrets 解密 + ensure_task RPC）。
            need_dispatch = False
            if task is None:
                # 新建任务前校验 local 源路径（存在 active 任务时不重跑——轮询热路径省
                # 一次 stat/resolve，且在飞任务的目录本就不该再变）
                validate_manager_source(model.source, model.source_url)
                # 失败预算不在共享键的历史里判（别的部署/上一轮的失败不锁新会话），
                # 由调用方 ModelStagingGate 按会话观察计——这里只建任务
                attempt = _next_attempt(rows)
                # canonical 随建新任务刷新（不做首写冻结）：模型行更新（改源路径/换 revision）
                # 后，新任务落到新目录；在飞任务不受影响——节点回报 local_dir 时投影写回权威值
                nm.local_dir = canonical_local_dir(model)
                # 3. 构造 task（payload 不含 secrets）。看门狗 12h：大模型下载
                #    （node 内部 3 次重试 + 断点续传）合法耗时以小时计，取值只兜
                #    「await 永不返回」的挂死，不误杀慢下载
                # 模型体积汇总在线程池（rglob 大目录阻塞事件循环）；失败回落 None
                size_bytes = await asyncio.to_thread(_manager_source_size_bytes, model)
                payload = self._build_payload(model, nm.local_dir, node_id)
                payload = payload.model_copy(update={"total_bytes": size_bytes})
                task = Task(
                    id=generate_uuid(),
                    resource_type=TaskResourceType.MODEL_PLACEMENT,
                    resource_key=resource_key,
                    node_id=node_id,
                    type=TaskType.ENSURE_MODEL,
                    status=TaskStatus.PENDING,
                    payload=payload.model_dump(mode="json"),
                    timeout_sec=ENSURE_MODEL_WATCHDOG_S,
                    attempt=attempt,
                )
                db.add(task)
                try:
                    # flush 包在 SAVEPOINT 里：撞 partial unique index 只回滚到保存点
                    # （整事务 rollback 会丢掉同 session 的 nm 状态写，与 task_ops 同修）
                    async with db.begin_nested():
                        await db.flush()
                except IntegrityError:
                    # partial unique index 兜底：并发已建 active task
                    log.info("placement %s: 并发已建 active task，跳过", resource_key)
                    return None
                log.info("placement %s: created task %s attempt=%d", resource_key, task.id[:8], attempt)
                need_dispatch = True
            elif task.status == TaskStatus.PENDING:
                need_dispatch = True  # 已建但下发失败/node 离线 → redrive 式重发

            # secrets 仅内存（不入库）；仅在下发时构建（解密有成本）。model 行 detach 后属性仍可读
            secrets = build_secrets(model) if need_dispatch else None
            # local 源额外注入短期 download_token（密，仅内存；redrive_pending 同样补）
            if need_dispatch and _is_manager_source(model):
                secrets = {**(secrets or {}), "download_token": _mint_download_token(model.id)}

        # 4. best-effort 下发（事务外，node 离线/RPC 失败 → task 留 PENDING，等 redrive 重发）
        if need_dispatch:
            await self._dispatch_ensure_payload(task.node_id, self._task_to_payload(task), secrets)
        return task.id

    async def recompute_demand(self) -> None:
        """全局重算 ``(model,node)`` 需求。

        活跃需求 = ``status∈{creating,running}`` 的部署 × 其 instances 的 assigned_node。
        无人需要的 ``(model,node)`` → ``desired_state=absent``；若其下载进行中 → 标 task
        ``CANCELLED`` + ``node_model.status=absent`` + best-effort cancel_task。仍被需要的绝不
        误杀。幂等安全。
        """
        cancelled: list[Task] = []
        async with get_db_context() as db:
            # 1. 活跃部署 → (model_id, node_id) 需求集
            active_deps = list(
                (
                    await db.execute(select(ModelDeployment).where(ModelDeployment.status.in_(_DEMANDING_STATUSES)))
                ).scalars()
            )
            model_of_dep = {d.deployment_id: d.model_id for d in active_deps}
            demanded: set[tuple[int, str]] = set()
            if model_of_dep:
                insts = list(
                    (
                        await db.execute(select(Instance).where(Instance.deployment_id.in_(list(model_of_dep.keys()))))
                    ).scalars()
                )
                for inst in insts:
                    mid = model_of_dep.get(inst.deployment_id)
                    if mid is not None and inst.assigned_node:
                        demanded.add((mid, inst.assigned_node))

            # 2. 遍历 node_model：不在需求集 → desired=absent；取消其所有 active 任务。
            #    下载/校验中额外回退 status=absent（present 文件仍在，不谎称 absent）。
            #    cancel 不限 status——一个尚未触达 node 的 PENDING 任务（node_model 仍
            #    absent/failed）也必须取消，否则 node 上线后会按遗留 PENDING 误触发下载。
            cancel_keys: list[str] = []
            for nm in list((await db.execute(select(NodeModel))).scalars()):
                if (nm.model_id, nm.node_id) in demanded:
                    nm.desired_state = NodeModelDesiredState.PRESENT
                    continue
                nm.desired_state = NodeModelDesiredState.ABSENT
                if nm.status in (NodeModelState.DOWNLOADING, NodeModelState.VERIFYING):
                    nm.status = NodeModelState.ABSENT  # cancel 回退：下载/校验中断回退
                cancel_keys.append(model_resource_key(nm.model_id, nm.node_id))

            # 3. 取消无人需要的 active 下载任务
            if cancel_keys:
                for t in list((await db.execute(select(Task).where(Task.resource_key.in_(cancel_keys)))).scalars()):
                    if t.status in TASK_INCOMPLETE_STATUSES:
                        t.status = TaskStatus.CANCELLED
                        cancelled.append(t)
            await db.flush()

        # 4. 事务外 best-effort 通知 node 取消（清 .part）；各 node 独立，并发下发
        if cancelled:
            await asyncio.gather(
                *(self._dispatch_cancel(t.node_id, t.id) for t in cancelled),
                return_exceptions=True,
            )

    async def redrive_pending(self, node_id: str | None = None) -> int:
        """重发 PENDING 任务（dispatch 失败 / node 离线遗留 → 声明式 redrive）。

        扫描 ``task`` 表的 PENDING 行，重新解析 model + secrets 后 best-effort 重发。
        ``node_id`` 限定只重发该 node（node 重连 sync 用）；不限定则全部（周期 sweep 触发）。
        模型已删 / 非 MODEL_PLACEMENT 的 PENDING 跳过（recompute_demand 会清理前者）。
        返回重发数；幂等——重发失败仍留 PENDING，下轮再发。
        """
        redispatch: list[tuple[str, dict, dict | None]] = []
        async with get_db_context() as db:
            rows = list((await db.execute(select(Task).where(Task.status == TaskStatus.PENDING))).scalars())
            if node_id:
                rows = [t for t in rows if t.node_id == node_id]
            for t in rows:
                # 仅模型分发任务走本通道（需重建 secrets）；容器任务（PULL_IMAGE/START_INSTANCE/
                # STOP_INSTANCE）的重发由部署 reconciler/停止收敛每轮声明式覆盖，不经此处。
                if t.type != TaskType.ENSURE_MODEL:
                    continue
                mid = (t.payload or {}).get("model_id")
                model = await db.get(Model, mid) if mid is not None else None
                if model is None:
                    continue
                # 事务内取 wire 投影 + secrets（避免事务外访问已 detach 的 ORM 行）
                secrets = build_secrets(model)
                if _is_manager_source(model):
                    secrets = {**(secrets or {}), "download_token": _mint_download_token(model.id)}
                redispatch.append((t.node_id, self._task_to_payload(t), secrets))
        for node_id_, payload, secrets in redispatch:
            await self._dispatch_ensure_payload(node_id_, payload, secrets)
        return len(redispatch)

    # ---- 内部：DB 查询（FakeSession 兼容：单 where + Python 过滤，避免复合子句）----

    async def _lock_node_model(self, db, model_id: int, node_id: str) -> NodeModel:
        """读/建 node_model 行（real PG 上 FOR UPDATE 串行化同 model 的 ensure_present）。"""
        rows = list(
            (await db.execute(select(NodeModel).where(NodeModel.model_id == model_id).with_for_update())).scalars()
        )
        nm = next((r for r in rows if r.node_id == node_id), None)
        if nm is None:
            nm = NodeModel(
                model_id=model_id,
                node_id=node_id,
                desired_state=NodeModelDesiredState.ABSENT,
                status=NodeModelState.ABSENT,
            )
            db.add(nm)
            await db.flush()
        return nm

    # ---- payload / 下发 ----

    @staticmethod
    def _build_payload(model: Model, local_dir: str, node_id: str | None = None) -> EnsureModelPayload:
        """ENSURE_MODEL 任务配方（typed 契约，调用方 ``model_dump`` 进 Task.payload）。"""
        return EnsureModelPayload(
            model_id=model.id,
            source=model.source,
            source_url=model.source_url,
            source_kind=model.source_kind,
            source_subpath=model.source_subpath,
            # 配套文件（GGUF 视觉投影器）：主模型按 subpath 收窄时也要一起下发
            extra_files=model_extra_files(model),
            revision=model.revision,
            hash_mode=model.hash_mode,
            hash_value=model.hash,
            local_dir=local_dir,
            desired_state=NodeModelDesiredState.PRESENT,
            # local 源：Manager 文件服务下发，注入 Node 可达的文件服务 origin（非密，进 payload）
            manager_base_url=_distribution_base_url() if _is_manager_source(model) else None,
        )

    @staticmethod
    def _task_to_payload(task: Task) -> TaskPayload:
        """task 行 → wire ``TaskPayload``（ensure_task 下发用；共用 task_ops 实现）。"""
        return task_to_payload(task)

    async def _dispatch_ensure_payload(self, node_id: str, payload: TaskPayload, secrets: dict | None) -> None:
        from llmpod.apps.manager import dispatch

        try:
            await dispatch.ensure_task(node_id, payload, secrets=secrets)
        except Exception as e:
            # node 离线 / RPC 失败：task 已在库（PENDING），redrive_pending 时重发
            log.debug("placement: ensure_task 下发失败（等 redrive）task=%s: %s", payload.id[:8], e)

    async def _dispatch_cancel(self, node_id: str, task_id: str) -> None:
        from llmpod.apps.manager import dispatch

        try:
            await dispatch.cancel_task(node_id, task_id)
        except Exception as e:
            log.debug("placement: cancel_task 下发失败 task=%s: %s", task_id[:8], e)
