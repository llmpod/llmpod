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
import hashlib
import logging
import os
import re
import unicodedata

from collections.abc import Sequence
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from llmpod.apps.manager.deployment.task_ops import (
    _next_attempt,
    task_to_payload,
    trailing_fail_count,
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
from llmpod.core.exceptions import LLMPodError, NotFound
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

    单一取用点：下发（``ensure_present``）与目录寻址（``canonical_local_dir`` 的 digest）
    必须看到同一份清单，否则同一模型会算出两个目录。
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
    """``model.name`` → 路径安全 slug：小写、非字母数字→``-``、去头尾、截断、空回退 ``model``。

    保留 Unicode 字母/数字（中文等可读名不丢）；NFKC 归一化把全角/兼容字符折成标准形。
    """
    s = unicodedata.normalize("NFKC", name or "").lower()
    # [^\w]+ 一次吞掉全部非字母数字连片（含连续 ``-``），无需二次塌缩；截 48 再 strip 两端 ``-``
    s = re.sub(r"[^\w]+", "-", s)[:48].strip("-")
    return s or "model"


def recipe_digest(
    source: str | None,
    source_url: str | None,
    source_subpath: str | None,
    revision: str | None,
    format: str | None = None,
    quantization: str | None = None,
    source_kind: str | None = None,
    hash: str | None = None,
    extra_files: Sequence[str] | None = None,
) -> str:
    """下载配方的稳定短哈希（前 12 位 sha256），None 字段记空串。

    身份 = 怎么获取（source + 来源 + 变体子路径 + 版本）+ 权重形态（format/quantization/
    source_kind/hash）+ 配套文件（视觉投影器）——同仓库不同格式/量化/源形态的模型行解析到
    **不同**目录，防未下载直接以第一份权重服务第二份（旧实现漏形态字段导致目录碰撞）。
    与 DB 自增 id、与 ``model.name`` 都无关——清库 id 重建、改名都不会改变它。
    """
    fields = [
        source or "",
        source_url or "",
        source_subpath or "",
        revision or "",
        format or "",
        quantization or "",
        source_kind or "",
        hash or "",
    ]
    # 配套文件只在非空时入键：无 mmproj 的模型 digest 与旧版逐字节一致（已下载目录不失效）
    if extra_files:
        fields.append(",".join(sorted(extra_files)))
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()[:12]  # 12 位 = 48bit，碰撞概率可忽略


def canonical_local_dir(model: Model) -> str:
    """模型下载目录 ``{model_store_dir}/{name-slug}-{digest}``（恒为绝对路径）。

    所有 Node 配同一 ``model_store_dir`` → 同 model 路径全 Node 一致，多机 TP 跑同一条
    ``--model`` 不会路径分叉。远程源（hf/oss）身份用配方哈希（source + source_url + revision
    + 形状，revision 固定 sha），可读部分用 name。local/manager 源的 ``source_url`` 是每次上传
    生成的独立目录（含随机后缀）、``name`` 是可改显示名——身份改用「source + 形状 + 内容哈希」
    的稳定 digest（排除 source_url/subpath/revision）：同名同内容不重下、内容变了才重下。
    """
    slug = slugify_name(model.name)
    url, subpath, rev = (
        (None, None, None)
        if ModelSource.is_manager_source(model.source)
        else (model.source_url, model.source_subpath, model.revision)
    )
    digest = recipe_digest(
        model.source,
        url,
        subpath,
        rev,
        format=model.format,
        quantization=model.quantization,
        source_kind=model.source_kind,
        hash=model.hash,
        extra_files=model_extra_files(model),
    )
    return os.path.abspath(f"{settings.model_store_dir}/{slug}-{digest}")


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
    """

    def __init__(self, model_id: int) -> None:
        self._model_id = model_id
        self._placement = PlacementService()
        self._tasks: dict[str, str] = {}  # node_id → 复核任务 id（跨轮保留）
        self._statuses: dict[str, str] = {}  # task_id → 上一轮读到的 status

    async def dispatch(self, pending: set[str]) -> set[str]:
        """为尚未派发的节点触发/续接复核任务；返回超 attempt 上限的节点集合。

        超上限的节点由 ``ensure_present`` 标 ``node_model.FAILED`` 并返回 None，是否判死
        交给 :meth:`advance`（要等 node 终态上报，避免上报时序误杀）。

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
            if self._statuses.get(task_id) in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                self._tasks.pop(node_id, None)
        over_cap: set[str] = set()
        for node_id in sorted(pending):
            recorded = self._tasks.get(node_id)
            if recorded is not None:
                status = self._statuses.get(recorded)
                if status in (TaskStatus.RUNNING, TaskStatus.SUCCESS):
                    continue  # 在途/已成功：不打扰
                # PENDING / 未知（首读尚未发生）：补发一次（幂等）
            task_id = await self._placement.ensure_present(self._model_id, node_id)
            if task_id is None:
                over_cap.add(node_id)
                self._tasks.pop(node_id, None)
            else:
                self._tasks[node_id] = task_id
        return over_cap

    async def advance(self, db, pending: set[str], over_cap: set[str]) -> tuple[set[str], dict[str, NodeModel]]:
        """读复核任务终态 + ``node_model``，返回（已确认就位的 node, node → node_model 行）。

        超 attempt 上限且 ``node_model`` 终态 FAILED → 抛 ``LLMPodError``：下载永久失败，
        继续等没有意义（未超上限时 ``ensure_present`` 已建下一 attempt，照常继续轮询）。
        """
        rows = list((await db.execute(select(NodeModel).where(NodeModel.model_id == self._model_id))).scalars())
        by_node = {r.node_id: r for r in rows if r.node_id in pending}
        self._statuses = await task_status_map(db, self._tasks)
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
            if self._statuses.get(self._tasks.get(nid)) == TaskStatus.SUCCESS
            and by_node.get(nid) is not None
            and by_node[nid].status == NodeModelState.PRESENT
        }
        return confirmed, by_node

    def release(self, node_id: str) -> None:
        """节点已确认就位 → 清出派发记录。"""
        self._tasks.pop(node_id, None)


class PlacementService:
    """模型分发声明式编排（无状态方法；按需实例化）。"""

    async def ensure_present(self, model_id: int, node_id: str) -> str | None:
        """声明需要 model 在 node 上 present。

        返回 task_id（新建的，或同 resource_key 已存在的 active task）。``None`` = 没有可用
        任务：连续失败超 attempt 上限（已标 ``node_model.FAILED``，等人工）或并发建任务撞唯一
        索引。**不因 ``node_model.status=present`` 短路**——那只是节点上次上报的缓存，每次
        部署都要一个复核任务去实际校验权重集（见方法内注释）。
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
            if not nm.local_dir:
                nm.local_dir = canonical_local_dir(model)

            # 2. 同 resource_key 的全部历史 task：一条查询取出（active 命中判 + attempt 推导共用）
            rows = list((await db.execute(select(Task).where(Task.resource_key == resource_key))).scalars())
            task = next((t for t in rows if t.status in TASK_INCOMPLETE_STATUSES), None)
            # 仅在「需新建」或「卡在 PENDING 未触达 node」时下发；RUNNING = node 已在跑，
            # staging 每轮轮询不重复 dispatch（省 build_secrets 解密 + ensure_task RPC）。
            need_dispatch = False
            if task is None:
                if trailing_fail_count(rows) >= MAX_AUTO_ATTEMPTS:
                    nm.status = NodeModelState.FAILED
                    nm.last_error = f"超过自动重试上限 ({MAX_AUTO_ATTEMPTS})，等待人工处理"
                    log.warning("placement %s: 连续失败 %d 次超上限，不再自动创建", resource_key, MAX_AUTO_ATTEMPTS)
                    return None
                attempt = _next_attempt(rows)
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
                    timeout_sec=12 * 3600,
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
