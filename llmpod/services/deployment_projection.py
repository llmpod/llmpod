"""四层状态投影链：容器 → 引擎 → 副本 → 端点 → 部署健康（单向，写点唯一）。

容器状态（Node 上报 ``report_instance_status`` 落库）是全链唯一权威来源；引擎/副本/
端点健康/部署健康灯全部是纯读重算的投影——容器状态迁移后调 :func:`project_upwards`
重算所属链路写回，无增量状态机、无双写漂移。

``model_deployment_instance``（引擎，一个推理进程组）与 ``model_deployment_replica``
（副本，模板一份完整拷贝）两表的 status 只在这里被写，别处不得改（例外：编排器
建行时置初值 provisioning）。生命周期动作经本模块的批量写点改写：
``reset_to_provisioning``（restart 重部署）/ ``mark_all_down``（失败收敛）——
规则仍归本模块，调用方不逐行内联。

投影规则：

- 引擎 = 全部分片 READY ⇔ ready；任一分片死或宿主节点离线 ⇒ down；全 PENDING ⇒
  provisioning；其余（启动中/部分就绪）⇒ starting。
- 副本 = 份内全部引擎 ready ⇔ serving；任一引擎 down ⇒ down（份内配对作废，
  整份判死）；其余 provisioning。
- 端点 health = 所属副本 serving ⇔ healthy，否则 down（proxy 快照过滤摘/回流量，
  API 网关零改动）。
- 部署健康灯 = 全副本 serving ⇔ healthy；任一副本 down/provisioning ⇒ degraded
  （仅 running 期维护；crash 计数不在投影范围——那是崩溃事件计数，无法从当前
  状态反推历史）。

节点离线感知：分片宿主节点 OFFLINE ⇒ 该分片按死投影（引擎 down → 端点摘流 →
健康灯降级）。容器行（权威依据）不动——自愈触发集不因节点离线扩大，节点回线
（register_node）后重算即恢复。离线集由 :func:`project_upwards` 每次自查
（OFFLINE 节点集），与容器状态迁移后的常规重算路径同一口径——离线口径不依赖
翻转写点的一次性事件。此外 ``mark_node_offline`` / ``register_node`` 翻完
Node.status 后经 :func:`reproject_after_node_transition` 就地触发受影响部署的
重算（实时性）；manager 重启恢复不额外触发——重启窗口节点会快速重连，全量摘流
反而制造流量抖动，且窗口一过任何一次常规重算都会收敛到正确口径。
"""

from __future__ import annotations

from collections.abc import Collection
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.constants import (
    DeploymentStatus,
    ENGINE_SHARD_DEAD_STATUSES,
    EngineStatus,
    HealthStatus,
    ReplicaStatus,
    SERVING_ENGINE_ROLES,
)
from llmpod.config.constants import InstanceStatus, NodeStatus
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.node import Node

log = logging.getLogger("llmpod.services.deployment_projection")


def engine_status_from_shards(shards: list[Instance], offline_nodes: Collection[str] = frozenset()) -> str:
    """由引擎的全部容器分片状态重算引擎投影状态（纯函数，规则见模块 docstring）。

    ``offline_nodes``：当前 OFFLINE 的节点 id 集——宿主在其中的分片按死投影
    （容器行不动，见模块 docstring「节点离线感知」）。测试注入用。
    """
    if any(s.assigned_node in offline_nodes or s.status in ENGINE_SHARD_DEAD_STATUSES for s in shards):
        return EngineStatus.DOWN
    if shards and all(s.status == InstanceStatus.READY for s in shards):
        return EngineStatus.READY
    if shards and all(s.status == InstanceStatus.PENDING for s in shards):
        return EngineStatus.PROVISIONING
    return EngineStatus.STARTING


def replica_status_from_engines(engines: list[ModelDeploymentInstance]) -> str:
    """由份内全部引擎状态重算副本投影状态：任一 down ⇒ down；全部 ready ⇔ serving。"""
    statuses = [e.status for e in engines]
    if any(s == EngineStatus.DOWN for s in statuses):
        return ReplicaStatus.DOWN
    if statuses and all(s == EngineStatus.READY for s in statuses):
        return ReplicaStatus.SERVING
    return ReplicaStatus.PROVISIONING


def deployment_health_from_replicas(replicas: list[ModelDeploymentReplica]) -> str:
    """部署健康灯（副本层权威口径）：全副本 serving ⇔ healthy，否则 degraded。

    唯一口径定义，project_upwards / finalize / stop 后重算一律调用本函数——空副本集
    （四层化前的旧数据）按 healthy 处理（无副本即无 degraded 依据）。
    """
    return HealthStatus.HEALTHY if all(r.status == ReplicaStatus.SERVING for r in replicas) else HealthStatus.DEGRADED


async def project_upwards(db: AsyncSession, deployment_id: str) -> None:
    """重算某部署的四层投影（容器行只读——权威来源不动）。

    节点离线集每次自查（当前 OFFLINE 节点集）：离线口径不依赖翻转写点的
    一次性事件，任何调用路径（容器上报/扩缩/状态翻转触发）同一收敛结果。

    幂等：值不变不写库（FakeSession 友好，也避免无谓 updated_at 抖动）。部署无
    副本行（挂载前的旧数据）时只跳过引擎/副本级，端点/健康灯按 down/degraded 收敛
    不可取——直接整体跳过，等迁移完成的新数据。
    """
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        return
    offline_nodes = set((await db.execute(select(Node.id).where(Node.status == NodeStatus.OFFLINE))).scalars())
    replica_rows = await replica_rows_of(db, deployment_id)
    if not replica_rows:
        # 无副本行（四层化前的旧数据）：引擎/副本/端点层无从投影；健康灯按容器口径
        # 兜底维护（旧 refresh_deployment_health 的回退语义并入——健康灯写点仍唯一）
        if dep.status == DeploymentStatus.RUNNING:
            irows = (await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars()
            target = (
                HealthStatus.DEGRADED
                if any(r.status != InstanceStatus.READY or r.assigned_node in offline_nodes for r in irows)
                else HealthStatus.HEALTHY
            )
            if dep.health_status != target:
                dep.health_status = target
        return

    engine_rows = await engine_rows_of(db, [r.replica_id for r in replica_rows])
    shard_rows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())

    grouped = shards_by_engine(shard_rows)
    engines_by_rep = engines_by_replica(engine_rows)

    # 引擎投影
    for e in engine_rows:
        target = engine_status_from_shards(grouped.get(e.instance_id, []), offline_nodes=offline_nodes)
        if e.status != target:
            e.status = target

    # 副本投影
    for r in replica_rows:
        target = replica_status_from_engines(engines_by_rep.get(r.replica_id, []))
        if r.status != target:
            r.status = target

    # 端点 health：逐副本镜像（只动本部署的端点行；外部端点不挂 deployment_id 不受影响）
    endpoints = list(
        (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
    )
    replica_status_by_id = {r.replica_id: r.status for r in replica_rows}
    for ep in endpoints:
        rid = ep.model_deployment_replica_id
        target = "healthy" if rid is not None and replica_status_by_id.get(rid) == ReplicaStatus.SERVING else "down"
        if ep.health_status != target:
            ep.health_status = target

    # 部署健康灯：全副本 serving ⇔ healthy（仅 running 期维护）
    if dep.status == DeploymentStatus.RUNNING:
        target_health = deployment_health_from_replicas(replica_rows)
        if dep.health_status != target_health:
            dep.health_status = target_health

    await db.flush()


async def reproject_after_node_transition(db: AsyncSession, node_id: str) -> None:
    """节点上下线后重算其上分片所属部署的投影（实时触发；口径自查见 project_upwards）。

    由 ``mark_node_offline`` / ``register_node`` 在翻完 Node.status 后就地调用。
    单部署重算失败只告警不中断——翻转写点（on_close / on_hello）不能因投影重算
    挂掉而回滚节点注册本身。
    """
    dids = set((await db.execute(select(Instance.deployment_id).where(Instance.assigned_node == node_id))).scalars())
    for did in sorted(dids):
        try:
            await project_upwards(db, did)
        except Exception as e:
            log.warning("节点 %s 状态变迁后投影重算失败 deployment=%s: %s", node_id, did[:8], e)


async def replica_rows_of(db: AsyncSession, deployment_id: str) -> list[ModelDeploymentReplica]:
    """某部署的全部副本行（无序；排序语义由调用方决定——finalize 按 seq）。"""
    return list(
        (
            await db.execute(
                select(ModelDeploymentReplica).where(ModelDeploymentReplica.deployment_id == deployment_id)
            )
        ).scalars()
    )


async def engine_rows_of(db: AsyncSession, replica_ids: list[str]) -> list[ModelDeploymentInstance]:
    """一组副本的全部引擎行。空列表直接返回（FakeSession 对空 in_ 不保证语义）。"""
    if not replica_ids:
        return []
    return list(
        (
            await db.execute(select(ModelDeploymentInstance).where(ModelDeploymentInstance.replica_id.in_(replica_ids)))
        ).scalars()
    )


async def reset_to_provisioning(db: AsyncSession, deployment_id: str) -> None:
    """副本/引擎两层重置回 provisioning 初值（restart 重部署用）。

    停止期间的 down/serving 是上一轮残影，新一轮容器阶段推进时投影链重算（此处
    不重算——分片刚回 PENDING，算了也是 provisioning）。
    """
    rep_rows = await replica_rows_of(db, deployment_id)
    for row in rep_rows:
        row.status = ReplicaStatus.PROVISIONING
    for eng in await engine_rows_of(db, [r.replica_id for r in rep_rows]):
        eng.status = EngineStatus.PROVISIONING


async def mark_all_down(db: AsyncSession, deployment_id: str) -> None:
    """副本/引擎两层随容器判死（失败收敛用；投影规则的批量收敛写点）。"""
    rep_rows = await replica_rows_of(db, deployment_id)
    for row in rep_rows:
        row.status = ReplicaStatus.DOWN
    for eng in await engine_rows_of(db, [r.replica_id for r in rep_rows]):
        eng.status = EngineStatus.DOWN


def endpoint_display_name(base_name: str, seq: int, *, multi_replica: bool) -> str:
    """端点展示名：多副本时 ``base·副本{seq+1}``（人读从 1 起），单副本裸名。

    ``base_name`` 由调用方传**部署名**（``ModelDeployment.name``）——端点名会当着「上游」
    标签展示给用户，而同一模型可有多份部署，用模型名会让这些端点重名、认不出是哪一份服务。
    超长 base 收口到列宽（255 减去后缀余量）：端点名列是 varchar(255)，多副本后缀会把
    长部署名顶过列宽，FINALIZING 写端点行时才炸就晚了。
    """
    suffix = f"·副本{seq + 1}" if multi_replica else ""
    return base_name[: ModelEndpoint.ENDPOINT_NAME_MAX_CHARS - len(suffix)] + suffix


async def sync_endpoint_names(
    db: AsyncSession, dep: ModelDeployment, *, replicas: list | None = None
) -> list[ModelEndpoint]:
    """端点展示名随副本集重算（改名的唯一写点）；返回重算后的端点行。

    展示名是多副本形态的函数：1→2 份后首个端点要带「·副本1」、2→1 份后幸存者要退回
    裸名。扩缩只增删副本行、不会自动改已存在的端点行，故每次副本集变化后由调用方
    （``_finalize`` 补端点前 / 缩容删份后）调一次，避免前端副本列表与路由成员名长期不一致。

    ``dep`` 传部署行而非名字：base 是这份部署的身份，让调用方传字符串就等于把「两处
    必须传同一个值」变成跨文件隐性约定，传错时表现为缩容后的重算把名字改回别的口径。
    两个调用方本就持有部署行（同一 session，无额外查询）。
    ``replicas``：调用方已加载的副本行；None → 本函数自取（调用方多在上一步刚查过同一
    集合）。返回的端点行就是调用方紧接着要用的那份，省掉一次同表重查。
    """
    deployment_id = dep.deployment_id
    if replicas is None:
        replicas = await replica_rows_of(db, deployment_id)
    multi = len(replicas) > 1
    names = {r.replica_id: endpoint_display_name(dep.name, r.seq, multi_replica=multi) for r in replicas}
    endpoints = list(
        (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
    )
    for ep in endpoints:
        target = names.get(ep.model_deployment_replica_id)
        if target is not None and ep.name != target:
            ep.name = target
    await db.flush()
    return endpoints


def serving_engines(engines: list[ModelDeploymentInstance]) -> list[ModelDeploymentInstance]:
    """筛出可出端点的引擎（role ∈ full/decode；prefill 永不出端点）。"""
    return [e for e in engines if e.role in SERVING_ENGINE_ROLES]


def shards_by_engine(shard_rows: list[Instance]) -> dict[str, list[Instance]]:
    """容器分片按引擎分组（无引擎挂载的旧行走不进任何组）。"""
    grouped: dict[str, list[Instance]] = {}
    for s in shard_rows:
        if s.model_deployment_instance_id:
            grouped.setdefault(s.model_deployment_instance_id, []).append(s)
    return grouped


def engines_by_replica(engine_rows: list[ModelDeploymentInstance]) -> dict[str, list[ModelDeploymentInstance]]:
    """引擎行按副本分组（副本层视图/摘要组装的共用读法）。"""
    grouped: dict[str, list[ModelDeploymentInstance]] = {}
    for e in engine_rows:
        grouped.setdefault(e.replica_id, []).append(e)
    return grouped
