"""集群拓扑数据聚合（供前端节点图可视化）。

查询 Cluster + Node + Gpu + Deployment + Assignment + Model，组装 ClusterTopology。
GPU 实时指标（available_vram / util）从 gpu_buffer 取；缺则按 total 乐观估。
engine 按 deployment.engine 去重合成 EngineSummary（从 adapter registry 查显示名/镜像）。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.constants import DeploymentPhase, DeploymentStatus
from llmpod.apps.manager.buffers import SystemStatus
from llmpod.config.constants import ClusterStatus, GpuStatus
from llmpod.core.exceptions import NotFound

from llmpod.apps.manager.deployment.schemas import DeploymentConfig
from llmpod.apps.webserver.schemas.topology import (
    ClusterSummary,
    ClusterTopology,
    DeploymentSummary,
    EngineSummary,
    GpuSummary,
    ModelSummary,
    NodeSummary,
    ReplicaSummary,
)
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.cluster import Cluster
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node import Node
from llmpod.services import deployment_projection

log = logging.getLogger("llmpod.apps.manager.deployment.topology")


def _load_gpu_realtime() -> dict[str, tuple[float, float]]:
    """从 gpu_buffer 取实时 used_vram + util（按 uuid）；失败返空 dict（乐观按 total）。"""
    out: dict[str, tuple[float, float]] = {}
    try:
        from llmpod.apps.manager.app import gpu_buffer

        if gpu_buffer is not None:
            for _nid, infos in gpu_buffer.get_all().items():
                for info in infos:
                    out[info.uuid] = (info.used_vram_gb, info.utilization or 0.0)
    except Exception as e:
        log.warning("topology: gpu_buffer 取数失败，按 total 乐观估: %s", e)
    return out


def _load_node_realtime() -> dict[str, SystemStatus]:
    """从 system_buffer 取实时系统指标（按 node_id）；失败返空 dict（按 DB total 乐观估）。"""
    out: dict[str, SystemStatus] = {}
    try:
        from llmpod.apps.manager.app import system_buffer

        if system_buffer is not None:
            out.update(system_buffer.get_all())
    except Exception as e:
        log.warning("topology: system_buffer 取数失败，按 DB memory_gb 乐观估: %s", e)
    return out


def _engine_summary(engine: str) -> EngineSummary:
    """从 adapter registry 合成 EngineSummary。"""
    from llmpod.apps.manager.deployment import image_catalog

    try:
        adapter = get_adapter(engine)
        arch = adapter.default_arch
        image = image_catalog.pick_image(engine=engine, arch=arch) or ""
        return EngineSummary(
            engine=engine,
            display_name=engine.capitalize(),
            image=image,
            supported_categories=list(adapter.supported_categories),
            supported_gpu_archs=list(adapter.supported_gpu_archs),
            supported_formats=list(adapter.supported_formats),
            deployable=adapter.deployable,
            supported_parallel=list(adapter.supported_parallel),
            supports_multinode=adapter.supports_multinode,
            protected_flags=list(adapter.protected_flags),
            param_suggestions=[p.model_dump() for p in adapter.param_suggestions],
            kv_cache_dtype_options=[o.model_dump() for o in adapter.kv_cache_dtype_options],
        )
    except Exception:
        return EngineSummary(
            engine=engine,
            display_name=engine.capitalize(),
            image="",
            supported_categories=[],
            supported_gpu_archs=[],
            supported_formats=[],
            deployable=False,
        )


async def build_cluster_topology(db: AsyncSession, cluster_id: int, *, with_replicas: bool = True) -> ClusterTopology:
    """聚合查询单集群的全部拓扑实体。

    ``with_replicas=False`` 跳过副本/引擎两层的查询与组装——给「替换掉 deployments 的
    调用方」（预览拓扑：合成行无副本）省两轮查询与分组；真实拓扑路径保持全量。
    集群不存在抛 :class:`NotFound`（HTTP 404）——直接抛 ValueError 会变 500。
    """
    # Cluster
    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise NotFound("Cluster", cluster_id)

    # Nodes
    nodes_orm = list((await db.execute(select(Node).where(Node.cluster_id == cluster_id))).scalars())

    # GPUs（通过 node_id 关联）
    node_ids = [n.id for n in nodes_orm]
    gpus_orm: list[Gpu] = []
    if node_ids:
        gpus_orm = list((await db.execute(select(Gpu).where(Gpu.node_id.in_(node_ids)))).scalars())

    # 实时 GPU 指标 + 节点内存
    realtime = _load_gpu_realtime()
    node_realtime = _load_node_realtime()

    # Deployments
    deploys_orm = list(
        (await db.execute(select(ModelDeployment).where(ModelDeployment.cluster_id == cluster_id))).scalars()
    )

    # GPU assignments（deploy → gpu_uuids）
    deploy_ids = [d.deployment_id for d in deploys_orm]
    gpu_uuids_by_deploy: dict[str, list[str]] = defaultdict(list)
    if deploy_ids:
        for a in (
            await db.execute(
                select(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id.in_(deploy_ids))
            )
        ).scalars():
            gpu_uuids_by_deploy[a.deployment_id].append(a.gpu_uuid)

    # Instances（deploy → instance_count + 副本摘要的节点归组）
    instance_counts: dict[str, int] = defaultdict(int)
    shards_by_deploy: dict[str, list[Instance]] = defaultdict(list)
    if deploy_ids:
        for inst in (await db.execute(select(Instance).where(Instance.deployment_id.in_(deploy_ids)))).scalars():
            instance_counts[inst.deployment_id] += 1
            shards_by_deploy[inst.deployment_id].append(inst)

    # 副本/引擎两层（deploy → replica_summaries；旧数据无副本行自然为空）
    replica_rows: list[ModelDeploymentReplica] = []
    if deploy_ids and with_replicas:
        replica_rows = list(
            (
                await db.execute(
                    select(ModelDeploymentReplica).where(ModelDeploymentReplica.deployment_id.in_(deploy_ids))
                )
            ).scalars()
        )
    engine_rows = await deployment_projection.engine_rows_of(db, [r.replica_id for r in replica_rows])
    engines_by_replica = deployment_projection.engines_by_replica(engine_rows)

    # Models（去重）
    model_ids = list({d.model_id for d in deploys_orm})
    models_map: dict[int, Model] = {}
    if model_ids:
        for m in (await db.execute(select(Model).where(Model.id.in_(model_ids)))).scalars():
            models_map[m.id] = m

    # ---- 组装 Summary ----
    cluster_summary = ClusterSummary(
        id=cluster.id,
        name=cluster.name,
        type=cluster.type,
        status=cluster.status,
    )

    node_summaries = []
    for n in nodes_orm:
        st = node_realtime.get(n.id)
        # system_buffer 心跳漏 memory_gb 时存 0.0，不能覆盖 DB 的 Node.memory_gb（与 feasibility 同口径）
        if st is not None and st.memory_gb > 0:
            mem_total = st.memory_gb
            mem_used = st.memory_used_gb or 0.0
            mem_percent = st.memory_percent or 0.0
            cpu_percent = st.cpu_percent or 0.0
        else:
            mem_total = n.memory_gb or 0.0
            mem_used = 0.0
            mem_percent = 0.0
            cpu_percent = 0.0
        node_summaries.append(
            NodeSummary(
                id=n.id,
                cluster_id=n.cluster_id,
                hostname=n.hostname or "",
                ip=n.ip or "",
                status=n.status,
                memory_gb=round(mem_total, 2),
                available_memory_gb=round(max(0.0, mem_total - mem_used), 2),
                memory_used_gb=round(mem_used, 2),
                memory_percent=round(mem_percent, 1),
                cpu_percent=round(cpu_percent, 1),
            )
        )

    gpu_summaries = []
    for g in gpus_orm:
        used, util = realtime.get(g.uuid, (0.0, 0.0))
        available = max(0.0, (g.total_vram_gb or 0.0) - used)
        gpu_summaries.append(
            GpuSummary(
                id=g.id,
                uuid=g.uuid,
                node_id=g.node_id,
                index=g.index,
                name=g.name or "",
                arch=g.arch or "",
                total_vram_gb=g.total_vram_gb or 0.0,
                used_vram_gb=round(used, 2),
                available_vram_gb=round(available, 2),
                util=round(util, 1),
                status=g.status,
            )
        )

    deploy_summaries = []
    engine_names: set[str] = set()
    replicas_by_deploy: dict[str, list[ModelDeploymentReplica]] = defaultdict(list)
    for r in replica_rows:
        replicas_by_deploy[r.deployment_id].append(r)
    for d in deploys_orm:
        config = DeploymentConfig.model_validate(d.spec or {})
        topo = config.topology
        engine_names.add(d.engine)
        # 副本摘要：份内引擎 → 容器分片 → 所在节点（去重有序，按份内序号）
        shard_by_engine = deployment_projection.shards_by_engine(shards_by_deploy.get(d.deployment_id, []))
        rep_summaries = []
        for rep in sorted(replicas_by_deploy.get(d.deployment_id, []), key=lambda r: r.seq):
            node_ids_ordered: list[str] = []
            for eng in sorted(engines_by_replica.get(rep.replica_id, []), key=lambda e: e.role):
                for s in shard_by_engine.get(eng.instance_id, []):
                    if s.assigned_node and s.assigned_node not in node_ids_ordered:
                        node_ids_ordered.append(s.assigned_node)
            rep_summaries.append(ReplicaSummary(seq=rep.seq, status=rep.status, node_ids=node_ids_ordered))
        deploy_summaries.append(
            DeploymentSummary(
                deployment_id=d.deployment_id,
                cluster_id=d.cluster_id,
                name=d.name,
                status=d.status,
                phase=d.phase,
                engine=d.engine,
                model_id=d.model_id,
                gpu_uuids=gpu_uuids_by_deploy.get(d.deployment_id, []),
                replicas=topo.replicas,
                nnodes=topo.nnodes,
                api_endpoint=d.api_endpoint,
                health_status=d.health_status,
                instance_count=instance_counts.get(d.deployment_id, 0),
                replica_summaries=rep_summaries,
            )
        )

    engine_summaries = [_engine_summary(e) for e in sorted(engine_names)]

    model_summaries = [
        ModelSummary(
            id=m.id,
            name=m.name,
            model_type=m.model_type,
            size_gb=m.size_gb or 0.0,
            format=m.format or "",
        )
        for m in models_map.values()
    ]

    return ClusterTopology(
        cluster=cluster_summary,
        nodes=node_summaries,
        gpus=gpu_summaries,
        deployments=deploy_summaries,
        engines=engine_summaries,
        models=model_summaries,
    )


async def build_all_topologies(db: AsyncSession) -> list[ClusterTopology]:
    """遍历全部 active 集群，逐个组装拓扑。"""
    clusters = list((await db.execute(select(Cluster).where(Cluster.status == ClusterStatus.ACTIVE))).scalars())
    return [await build_cluster_topology(db, c.id) for c in clusters]


async def build_preview_topology(
    db: AsyncSession,
    cluster_id: int,
    model: Model,
    engine: str,
    topology_dict: dict,
) -> ClusterTopology:
    """合成预览拓扑：调 build_cluster_topology 拿集群实体，替换 deployments 为一条合成预览。

    不落库——deployments 只有一条预览行（engine/model 按推荐值，gpu_uuids 取集群全部可用 GPU）。
    前端 inspect 页和拓扑页共用同一渲染逻辑。
    """
    # 合成行无副本：with_replicas=False 省两层查询（replica_summaries 由部署详情另给）
    topo = await build_cluster_topology(db, cluster_id, with_replicas=False)

    # 合成预览部署
    online_gpu_uuids = [g.uuid for g in topo.gpus if g.status == GpuStatus.ONLINE]
    topo.deployments = [
        DeploymentSummary(
            deployment_id="preview",
            cluster_id=cluster_id,
            name=f"[预览] {model.name}",
            status=DeploymentStatus.CREATING,
            phase=DeploymentPhase.PENDING,
            engine=engine,
            model_id=model.id,
            gpu_uuids=online_gpu_uuids,
            replicas=topology_dict.get("replicas", 1),
            nnodes=topology_dict.get("nnodes", 1),
            api_endpoint=None,
            health_status=None,
            instance_count=0,
        )
    ]

    # 合成 engine + model 条目
    topo.engines = [_engine_summary(engine)]
    topo.models = [
        ModelSummary(
            id=model.id,
            name=model.name,
            model_type=model.model_type,
            size_gb=model.size_gb or 0.0,
            format=model.format or "",
        )
    ]

    return topo
