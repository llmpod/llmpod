"""节点管理服务：注册、状态同步、引导脚本。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Callable, NamedTuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import Conflict, NotFound
from llmpod.apps.manager.deployment.constants import DeploymentStatus
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node import Node
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.services.cluster_service import get_cluster_name_map
from llmpod.services.deployment_projection import reproject_after_node_transition
from llmpod.services.version_service import parse_version
from llmpod.tunnel.schemas.system import GpuInfo, NodeReport
from llmpod.apps.webserver.schemas.out_models import GpuRuntimeOut
from llmpod.utils import now_utc
from llmpod.config.constants import ClusterStatus, GpuStatus, InstanceStatus, NodeStatus

import llmpod

log = logging.getLogger("llmpod.services.node")

# manager 自身版本的解析结果：进程生命周期内不变，模块导入时算一次
# （version_status_of 在节点列表逐行 / 每次 $hello 都会调）
_MANAGER_VERSION = parse_version(llmpod.__version__)


class Registration(NamedTuple):
    """register_node 的返回：分配/复用的 node_id + 是否首次注册。

    版本变迁（重连上报版本与库中记录不同）在函数内直接落系统审计——升级审计
    不依赖 $node_upgrade_result 送达（升级中 manager 重启/断网时靠落库变迁补齐），
    调用方无需再据返回值写审计。
    """

    node_id: str
    is_first: bool


async def register_node(
    node_id: str | None,
    cluster_id: int,
    info: NodeReport,
    db: AsyncSession,
    resolve_ip: Callable[[NodeReport, str | None], str] | None = None,
) -> Registration:
    """注册或重新注册节点（首次由 Manager 分配 UUID 并持久化）。

    ``resolve_ip`` 是 $hello 期的地址选定器（apps/manager/node_addr.resolve_node_ip）；
    传入时在本函数已加载的 Node 行上就地选定——同一次查询同时拿到「已存值」与
    更新目标，不另开 session 重读同一行。None = 直接采信 info.ip（测试/内部路径）。
    """
    is_first = not node_id
    if is_first:
        node_id = str(uuid.uuid4())

    hostname = info.hostname or "unknown"
    cpu_cores = info.cpu_cores or None
    cpu_arch = info.cpu_arch
    memory_gb = info.memory_gb or None
    capabilities = info.capabilities
    version = info.version or None

    # 查询现有节点（重连场景）
    node = None
    if not is_first:
        stmt = select(Node).where(Node.id == node_id)
        result = await db.execute(stmt)
        node = result.scalar_one_or_none()
        if node is None:
            # 重连但 DB 中查不到该 node_id（如 DB 被重置或节点数据被清理）：
            # 降级为新建，保留原 node_id 以免 Node 端配置失效
            log.warning("Reconnect with unknown node_id=%s, creating new record", node_id)

    # 地址选定（显式配置 > 已存合法值 > 自报值兜底）；已存值取自刚加载的行。
    # NOT NULL 列的补位哨兵：选定结果为空（未显式固定且自报值不可用）时落 0.0.0.0，
    # 语义=未固定——下游按地址消费处遇它即知节点未经引导固定。
    reported_ip = info.ip
    if resolve_ip is not None:
        info.ip = resolve_ip(info, node.ip if node is not None else None)
    ip = info.ip or "0.0.0.0"
    # 卡名只在「选定结果 == 自报值」时可信（自报卡名是自报 ip 的函数）：选定
    # 换用已存/显式值时自报卡名即失真 → 置 None，消费方回落显式配置与代码兜底
    net_ifname: str | None = None
    if info.ip == reported_ip:
        net_ifname = info.net_ifname or None

    version_changed = False
    ip_changed = False

    if node is not None:
        # 更新已有节点
        node.status = NodeStatus.ONLINE
        node.hostname = hostname
        # ip 变迁落审计：互联地址是部署可达性的根，换址必须留痕（谁换的、何时、
        # 从什么值换成什么值）——地址选定只在 $hello 期发生，正常情况变迁 =
        # 操作者重新指定；静默变化即异常信号。
        if ip != node.ip:
            ip_changed = True
            previous_ip = node.ip
            node.ip = ip
        node.net_ifname = net_ifname
        node.cpu_cores = cpu_cores
        node.cpu_arch = cpu_arch
        node.memory_gb = memory_gb
        node.capabilities = capabilities
        # 版本变迁（升级完成的新容器 $hello 追平时发生）→ 落审计（升级结果的双保险）
        if version != node.version:
            version_changed = True
            previous_version = node.version
            node.version = version
    else:
        # 创建新节点（首次注册或重连降级）
        node = Node(
            id=node_id,
            cluster_id=cluster_id,
            hostname=hostname,
            ip=ip,
            net_ifname=net_ifname,
            status=NodeStatus.ONLINE,
            cpu_cores=cpu_cores,
            cpu_arch=cpu_arch,
            memory_gb=memory_gb,
            capabilities=capabilities,
            version=version,
        )
        db.add(node)

    if version_changed:
        await _audit_version_change(node_id, hostname, previous_version, version)
    if ip_changed:
        await _audit_ip_change(node_id, hostname, previous_ip, ip)

    await db.flush()

    # 回线恢复面：重算该节点上分片所属部署的投影（健康灯按当前容器状态与节点在
    # 线情况重新落定）。与 mark_node_offline 的重算对称。
    await reproject_after_node_transition(db, node_id)
    return Registration(node_id=node_id, is_first=is_first)


async def _audit_version_change(node_id: str, hostname: str, old: str | None, new: str | None) -> None:
    """节点版本变迁落系统审计（actor=system，admin 审计页可见）。

    独立 session 写入（与调用方业务事务隔离）；write_audit 自身兜底不抛，
    审计缺失不阻断注册。
    """
    from llmpod.services.audit_service import write_audit

    await write_audit(
        actor_type="system",
        action="upgrade",
        resource_type="node",
        resource_id=node_id,
        result="success",
        detail={"node_hostname": hostname, "previous_version": old, "version": new},
    )


async def _audit_ip_change(node_id: str, hostname: str, old: str | None, new: str | None) -> None:
    """节点互联 IP 变迁落系统审计（actor=system，admin 审计页可见）。

    与版本变迁审计同构：独立 session 写入、write_audit 兜底不抛，审计缺失不阻断注册。
    """
    from llmpod.services.audit_service import write_audit

    await write_audit(
        actor_type="system",
        action="update",
        resource_type="node",
        resource_id=node_id,
        result="success",
        detail={"node_hostname": hostname, "previous_ip": old, "ip": new},
    )


async def sync_gpus(node_id: str, gpus: list[GpuInfo], db: AsyncSession):
    """根据上报的 GPU 信息对节点的 GPU 记录进行更新或插入。"""
    # 已有行一次 in_ 预取建索引（$hello 重连 / GPU 集合变化热插拔都会全量跑本函数，
    # 逐卡点查是按卡数放的串行往返——16 卡节点一次注册即 16 次）
    valid_uuids = [g.uuid for g in gpus if g.uuid and g.arch]
    existing_by_uuid: dict[str, Gpu] = {}
    if valid_uuids:
        rows = (await db.execute(select(Gpu).where(Gpu.uuid.in_(valid_uuids)))).scalars()
        existing_by_uuid = {g.uuid: g for g in rows}
    for gpu_info in gpus:
        gpu_uuid = gpu_info.uuid
        if not gpu_uuid:
            continue

        # arch 是必填核心列：collector 必须上报。缺失属数据流错误，但不因单卡脏数据中断整个节点注册
        # 跳过该卡并告警，其余正常 GPU 照常 upsert，$register_ack 照发（见 bootstrap.on_hello 兜底）。
        arch = gpu_info.arch
        if not arch:
            log.warning(
                "GPU %s missing required 'arch' field, skipping (collector must report nvidia/cann/cpu)",
                gpu_uuid,
            )
            continue
        # soc_model 等架构专属字段进 spec jsonb，谁关注谁读
        soc_model = gpu_info.soc_model
        spec = {"soc_model": soc_model} if soc_model else {}

        existing = existing_by_uuid.get(gpu_uuid)

        if existing:
            existing.index = gpu_info.index or existing.index
            # 重锚定 node_id 前审计——若原属主是别的节点，告警（uuid 冲突/虚拟化迁移，不再静默过户）
            if existing.node_id != node_id:
                log.warning("GPU %s re-parenting: node %s -> %s", gpu_uuid, existing.node_id, node_id)
            existing.node_id = node_id  # 重注册时把 GPU 重新锚定到当前 node
            existing.status = GpuStatus.ONLINE
            existing.arch = arch
            existing.spec = spec
            existing.name = gpu_info.name or existing.name  # 驱动升级改名/显存刷新
            existing.total_vram_gb = gpu_info.total_vram_gb or existing.total_vram_gb
            existing.driver_version = gpu_info.driver_version or existing.driver_version
        else:
            gpu = Gpu(
                uuid=gpu_uuid,
                node_id=node_id,
                index=gpu_info.index,
                name=gpu_info.name or "Unknown GPU",
                arch=arch,
                spec=spec,
                total_vram_gb=gpu_info.total_vram_gb,
                driver_version=gpu_info.driver_version,
                status=GpuStatus.ONLINE,
            )
            db.add(gpu)

    await db.flush()


async def mark_node_offline(node_id: str, db: AsyncSession):
    """将节点及其 GPU 标记为离线。"""
    stmt = select(Node).where(Node.id == node_id)
    result = await db.execute(stmt)
    node = result.scalar_one_or_none()
    if node:
        node.status = NodeStatus.OFFLINE

    # 将 GPU 标记为离线
    gpu_stmt = select(Gpu).where(Gpu.node_id == node_id, Gpu.status != GpuStatus.OFFLINE)
    gpu_result = await db.execute(gpu_stmt)
    for gpu in gpu_result.scalars():
        gpu.status = GpuStatus.OFFLINE

    await db.flush()

    # 重算投影：节点状态只驱动部署健康灯降级（运维信号）。端点是否摘流不跟节点
    # 通道走——node agent 的 WS 与引擎进程是两回事，网关直连引擎端口，误摘流会让
    # 一次计划内的节点升级造成整条路由不可用。引擎死活只认容器上报状态与网关真实
    # 请求失败（选择器熔断），见 deployment_projection 模块 docstring。
    await reproject_after_node_transition(db, node_id)


async def touch_node_heartbeat(node_id: str, db: AsyncSession, version: str | None = None) -> None:
    """更新 Node.last_heartbeat_at（让该索引列活起来，DB 节点列表与内存口径一致）。

    on_heartbeat 每跳调一次：单行 UPDATE（无需先 SELECT 加载 ORM 对象）。on_hello 首次也设。
    ``version`` 非空时顺带落库（心跳也带版本——升级追平后即使不重连，列表也立即翻新；
    与库中相同时 UPDATE 无变化，零成本）。
    """
    values: dict = {"last_heartbeat_at": now_utc()}
    if version:
        values["version"] = version
    await db.execute(update(Node).where(Node.id == node_id).values(**values))


async def validate_cluster(cluster_id: int, db: AsyncSession) -> bool:
    """检查集群是否存在且处于活跃状态。"""
    stmt = select(Cluster).where(Cluster.id == cluster_id, Cluster.status == ClusterStatus.ACTIVE)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


# ---- 节点 / GPU 关联装配（展示所属集群名 / 节点主机名）----


async def get_gpu_host_map(db: AsyncSession, gpu_ids: list[int]) -> dict[int, dict]:
    """批量取 ``gpu_id → {node_hostname, cluster_id, cluster_name}``。

    一条 join：gpu → node → cluster。未出现的 gpu_id 由调用方补空。
    """
    if not gpu_ids:
        return {}
    stmt = (
        select(Gpu.id, Node.hostname, Node.cluster_id, Cluster.name)
        .join(Node, Node.id == Gpu.node_id)
        .outerjoin(Cluster, Cluster.id == Node.cluster_id)
        .where(Gpu.id.in_(gpu_ids))
    )
    mapping: dict[int, dict] = {}
    for gid, hostname, cluster_id, cluster_name in (await db.execute(stmt)).all():
        mapping[gid] = {
            "node_hostname": hostname,
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
        }
    return mapping


async def enrich_gpus(db: AsyncSession, gpus: list[Gpu]) -> list[Gpu]:
    """把每个 GPU 的 node_hostname / cluster_id / cluster_name 批量回填到 ORM 实例上。"""
    if not gpus:
        return gpus
    mapping = await get_gpu_host_map(db, [g.id for g in gpus])
    for g in gpus:
        info = mapping.get(g.id, {})
        g.node_hostname = info.get("node_hostname")
        g.cluster_id = info.get("cluster_id")
        g.cluster_name = info.get("cluster_name")
    return gpus


async def enrich_nodes(db: AsyncSession, nodes: list[Node]) -> list[Node]:
    """把每个节点的 cluster_name 批量回填到 ORM 实例上（复用 cluster_service 的名称映射）。"""
    if not nodes:
        return nodes
    cluster_ids = list({n.cluster_id for n in nodes})
    names = await get_cluster_name_map(db, cluster_ids)
    for n in nodes:
        n.cluster_name = names.get(n.cluster_id)
    return nodes


def node_gpu_summaries(node_ids: set[str]) -> dict[str, dict]:
    """从 Manager 实时缓冲取每节点 GPU 摘要（未取整的原始聚合值）。

    消费方两处：节点列表 runtime 装配（展示前取整）与大屏集群池（按卡数加权后再
    取整）；缓冲不可用（Manager 未初始化/单测未起缓冲）返回空 dict，调用方按
    「无实时数据」处理。离线/无缓冲节点不在结果中。
    """
    try:
        from llmpod.apps.manager.app import gpu_buffer
    except Exception:
        return {}
    if gpu_buffer is None:
        return {}
    result: dict[str, dict] = {}
    for nid in node_ids:
        gpus = gpu_buffer.get(nid)
        if not gpus:
            continue
        result[nid] = {
            "gpu_count": len(gpus),
            "gpu_utilization": sum(g.utilization for g in gpus) / len(gpus),
            "used_vram_gb": sum(g.used_vram_gb for g in gpus),
            "total_vram_gb": sum(g.total_vram_gb for g in gpus),
        }
    return result


def enrich_nodes_runtime(nodes: list[Node]) -> list[Node]:
    """把在线节点的实时资源使用摘要（CPU/内存/磁盘/GPU）回填到 ORM 实例的 ``runtime``。

    数据源是 Manager 进程内的实时缓冲（心跳镜像），非 DB——DB 只存静态资产。离线节点
    无缓冲 → ``runtime=None``（前端据此区分「离线/无数据」）。
    """
    if not nodes:
        return nodes
    try:
        from llmpod.apps.manager.app import system_buffer
    except Exception:
        return nodes  # manager 未初始化，无实时数据可装配

    gpu_summaries = node_gpu_summaries({n.id for n in nodes})

    for n in nodes:
        runtime: dict = {}

        # 系统实时（CPU / 内存 / 磁盘）
        if system_buffer is not None:
            sys = system_buffer.get(n.id)
            if sys is not None:
                runtime.update(
                    cpu_percent=sys.cpu_percent,
                    memory_used_gb=sys.memory_used_gb,
                    memory_percent=sys.memory_percent,
                    disk_total_gb=sys.disk_total_gb,
                    disk_used_gb=sys.disk_used_gb,
                    disk_percent=sys.disk_percent,
                )

        # GPU 实时（逐卡聚合：数量 / 核心利用率均值 / 显存求和；展示取整）
        rt = gpu_summaries.get(n.id)
        if rt is not None:
            runtime.update(
                gpu_count=rt["gpu_count"],
                gpu_utilization=round(rt["gpu_utilization"], 1),
                used_vram_gb=round(rt["used_vram_gb"], 2),
                total_vram_gb=round(rt["total_vram_gb"], 2),
            )

        n.runtime = runtime if runtime else None
    return nodes


def version_status_of(node_version: str | None) -> str:
    """节点版本 vs manager 自身版本 → ``ok | outdated | newer | unknown``（唯一定义处）。

    比对基准是 manager ``__version__``（节点与 manager 同版即对齐）；任一侧版本缺失
    或不可解析 → ``unknown``（fail-open，不告警）。
    """
    mine = _MANAGER_VERSION
    theirs = parse_version(node_version or "")
    if mine is None or theirs is None:
        return "unknown"
    if theirs < mine:
        return "outdated"
    if theirs > mine:
        return "newer"
    return "ok"


def enrich_nodes_version_status(nodes: list[Node]) -> list[Node]:
    """把每个节点的版本比对结果回填到 ORM 实例的 ``version_status``（节点列表/详情输出前调用）。"""
    for n in nodes:
        n.version_status = version_status_of(n.version)
    return nodes


def enrich_gpus_runtime(gpus: list[Gpu]) -> list[Gpu]:
    """把每张 GPU 的实时指标摘要（显存 / 利用率 / 温度 / 功耗）回填到 ORM 实例的 ``runtime``。

    与 ``enrich_nodes_runtime`` 同一机制（Manager 进程内实时缓冲，心跳镜像），粒度为逐卡。
    缓冲无该卡数据（节点离线 / 未上报）→ ``runtime=None``（前端据此区分「离线/无数据」）。
    Manager 生命周期外（如导入边界、单测未起缓冲）时整体跳过，不抛错。
    """
    if not gpus:
        return gpus
    try:
        from llmpod.apps.manager.app import gpu_buffer
    except Exception:
        return gpus  # manager 未初始化，无实时数据可装配

    for g in gpus:
        info = next((i for i in (gpu_buffer.get(g.node_id) if gpu_buffer else []) if i.uuid == g.uuid), None)
        g.runtime = (
            GpuRuntimeOut(
                used_vram_gb=round(info.used_vram_gb, 2),
                memory_utilization=round(info.memory_utilization, 1),
                utilization=round(info.utilization, 1),
                temperature=info.temperature,
                fan_speed=info.fan_speed,
                power_draw_w=round(info.power_draw_w, 1),
                power_limit_w=round(info.power_limit_w, 1),
                sm_clock_mhz=info.sm_clock_mhz,
                mem_clock_mhz=info.mem_clock_mhz,
                process_count=info.process_count,
            )
            if info is not None
            else None
        )
    return gpus


# ---------------------------------------------------------------------------
# 节点注销：注销标记（Redis）+ 级联删除编排
#
# 注销标记是删除路径与 $hello 注册路径共享的状态，放服务层供 bootstrap（拦重连）
# 与 admin 删除路由（写注销标记）同源调用；Redis 不可用一律 fail-open——删除路径
# 本身已级联删库，注销标记只是防残活容器重连的附加保险。
# ---------------------------------------------------------------------------

# Redis key 前缀 + TTL：7 天内该 node_id 的 $hello 一律拒绝
_TOMBSTONE_PREFIX = "llmpod:node-tombstone:"
_TOMBSTONE_TTL_SECONDS = 7 * 86400

# 删除守卫：除 STOPPED 外全部拦截——RUNNING/READY 持有容器、GPU 与端口，CRASHED/
# FAILED 是 reconciler 的运行期自愈集（重启/重派），PENDING..STOPPING 是瞬态持有。
# 漏拦 CRASHED/FAILED 会让实例行残留指向已删节点：旁路清理已把容器删掉，自愈
# 循环却永远对不存在的节点发 STOP/START，部署卡死在非终态。已停实例不拦：其
# 容器随注销自清理一并删除，配置在 manager 库可重建。
_ACTIVE_INSTANCE_STATUSES = (
    InstanceStatus.PENDING,
    InstanceStatus.STARTING,
    InstanceStatus.RUNNING,
    InstanceStatus.READY,
    InstanceStatus.CRASHED,
    InstanceStatus.FAILED,
    InstanceStatus.STOPPING,
)

# 通知自清理后等 WS 断开的上限（秒）；超时也继续删（cleaned=false）
_DECOMMISSION_WAIT_SECONDS = 10.0

# 强制移除时等在途部署协程退出的上限（秒）：取消事件已置位，deploy 在 staging 循环
# 里下一个检查点就会退出；节点离线时 RPC 也不会真发包，等不到就继续删。
_FORCE_CANCEL_WAIT_SECONDS = 10.0


class DecommissionResult(NamedTuple):
    """decommission_node 的结果：是否通知过自清理 / 是否确认 WS 已断 / 连带删掉的部署 ID。"""

    notified: bool
    cleaned: bool
    deleted_deployments: list[str]


async def node_tombstone_hit(node_id: str) -> bool:
    """注销标记是否命中（Redis 不可用 fail-open 放行）。"""
    try:
        from llmpod.db.redis import get_redis

        return bool(await get_redis().exists(f"{_TOMBSTONE_PREFIX}{node_id}"))
    except Exception:
        return False


async def set_node_tombstone(node_id: str) -> None:
    """写注销标记（TTL 7 天，期间该 node_id 的 $hello 一律拒绝）。"""
    try:
        from llmpod.db.redis import get_redis

        await get_redis().set(f"{_TOMBSTONE_PREFIX}{node_id}", "1", ex=_TOMBSTONE_TTL_SECONDS)
    except Exception:
        log.warning("注销标记写入失败（Redis 不可用）：node %s 重连可能复活", node_id)


async def decommission_node(node_id: str, db: AsyncSession, *, force: bool = False) -> DecommissionResult:
    """注销节点：守卫 → 通知在线节点自清理（等 WS 断）→ 注销标记 → 级联删节点/GPU。

    注销语义声明：
    - 守卫：节点上存在未停止的实例（PENDING/STARTING/RUNNING/READY/STOPPING/
      CRASHED/FAILED，即除 STOPPED 外全部）时抛 Conflict——节点带着负载或还在
      自愈循环里时不允许注销，先停止或迁移部署；已停实例不拦，其推理容器随节点
      自清理一并删除（配置在 manager 库，可重建）。
    - 在线节点：发 ``$node_decommission``，node 侧自清理 = 起一次性旁路清理
      容器后 rm -f 自己；旁路等容器消失后收尾：删本节点推理容器（label
      node-id 圈定）+ 删具名数据卷（整卷；bind 挂载的宿主路径与模型目录
      一律不碰）。重活放旁路是为了让 WS 尽快断开（本端等待窗只有 10s），
      也因为容器 rm 自己后进程即被杀、死后的语句不可靠。等 WS 断开确认
      （≤10s，超时也继续删）。
    - 离线节点：无法触达，容器/卷留在机器上需手动清理（docker rm +
      docker volume rm）。
    - 注销标记（7 天）：拦住残活旧身份的 $hello，防「删了又自动注册回来」；被
      拦的节点同样执行自清理并作废身份，同机重装以全新 node_id 注册不受误伤。
    等待期间连接已释放（守卫读后先 commit），不占连接池。

    ``force=True`` 走强制移除路径（见 :func:`_force_remove_node`）：跳过上述实例
    守卫、连带删除该节点上的部署。只对已离线节点开放。
    """
    from llmpod.apps.manager import dispatch

    node = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one_or_none()
    if node is None:
        raise NotFound("Node", node_id)

    if force:
        return await _force_remove_node(node, db)

    active_count = (
        await db.execute(
            select(func.count())
            .select_from(Instance)
            .where(
                Instance.assigned_node == node_id,
                Instance.status.in_(_ACTIVE_INSTANCE_STATUSES),
            )
        )
    ).scalar() or 0
    if active_count > 0:
        raise Conflict(f"节点存在 {active_count} 个未停止的推理实例（含运行中/崩溃/失败），请先停止或迁移部署后再注销")

    # 守卫读到此为止：先结束事务释放连接，再进入 ≤10s 的等待
    await db.commit()

    # 在线 → 通知自清理，等 WS 断开（on_close 会把节点标 offline 并移出 hub）
    notified = cleaned = False
    if dispatch.node_online(node_id):
        try:
            await dispatch.send_node_decommission(node_id)
            notified = True
        except Exception:
            log.warning("decommission 通知发送失败，按离线路径继续删除 node=%s", node_id)
        if notified:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _DECOMMISSION_WAIT_SECONDS
            while loop.time() < deadline:
                if not dispatch.node_online(node_id):
                    cleaned = True
                    break
                await asyncio.sleep(0.5)

    # 注销标记：自清理失败/老容器残活时拦住 $hello（防「删了又自动注册回来」）
    await set_node_tombstone(node_id)

    # 级联清理该节点的 GPU 记录（逻辑外键，无 DB 级联）
    await db.execute(delete(Gpu).where(Gpu.node_id == node_id))
    await db.delete(node)
    await db.flush()
    await db.commit()
    return DecommissionResult(notified=notified, cleaned=cleaned, deleted_deployments=[])


async def _force_remove_node(node: Node, db: AsyncSession) -> DecommissionResult:
    """强制移除节点：连同该节点上仍存在的部署一起删除（机器报废/过期时的唯一出口）。

    由 ``decommission_node(force=True)`` 在节点行已加载后调用。与正常注销的差别只在
    守卫与连带范围：

    - **不做实例状态守卫**。正常注销要求节点上只剩 STOPPED 实例，而报废机器上的实例
      永远停在 RUNNING/READY/CRASHED——节点再也回不来，那些部署也永远起不来，
      恢复正常路径等于永久锁死这条记录。这里是管理员确认过的破窗操作。
    - **只允许离线节点**：节点 WS 还连着说明机器活着，走正常注销（会通知节点自清理
      容器与具名数据卷）。在线强行删记录会把容器和卷留在活机器上成为孤儿。
    - **连带删除部署**：该节点上有实例的部署整条级联删（实例 / 副本 / 引擎 / 端点 /
      路由绑定 / 部署日志 / 卡预留），而不只是把实例标失败——单机部署失去宿主后没有
      任何恢复可能，留着只会让自愈循环每轮对不存在的节点重发 START；多机部署的其余
      分片也随之一并删除，用户按需重新部署到别的机器。

    顺序：先取消在途编排并等协程退出（避免删行的同时 deploy/staging 还在写实例行），
    再落库级联，最后同步进程内资源账本。

    机器上残留的容器与数据卷无法触达（节点已离线），需人工在机器上清理——节点身份
    被注销标记拦住不会再自动注册回来。

    不连带删模型记录：从该节点目录导入的模型（``Model.source=node`` 绑定此节点）会变成
    再也部署不了的记录（权重在那台机器上），但模型行本身可能还有别的用途，留给管理员
    在模型页自行删除。
    """
    from llmpod.apps.manager import dispatch
    from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
    from llmpod.apps.manager.deployment.placement import PlacementService
    from llmpod.services import deployment_service

    node_id = node.id
    if dispatch.node_online(node_id):
        raise Conflict("节点仍在线，无法强制移除；请先注销（会通知节点清理容器与数据卷）")

    # 该节点上仍有实例的部署（含多机部署：其余节点的分片一并删除）
    deployment_ids = sorted(
        set((await db.execute(select(Instance.deployment_id).where(Instance.assigned_node == node_id))).scalars())
    )

    # 先置 deleting 再提交：控制器不再为这些部署新起 staging / 运行期自愈协程，
    # 后续级联删除不会与编排写同一批行打架（与「删除部署」路由同一落位手法）。
    for deployment_id in deployment_ids:
        dep = await db.get(ModelDeployment, deployment_id)
        if dep is not None:
            dep.status = DeploymentStatus.DELETING
            dep.health_status = None

    # 守卫读到此为止：先提交释放连接。取消在途编排期间不占连接池。
    await db.commit()

    orch = get_orchestrator()
    if deployment_ids:
        # 并发取消：每个部署最多等 _FORCE_CANCEL_WAIT_SECONDS，串行等会让一次请求
        # 拖成「部署数 × 上限」
        await asyncio.gather(*(orch.request_cancel(did) for did in deployment_ids))
        await asyncio.gather(
            *(orch.await_deploy(did, timeout=_FORCE_CANCEL_WAIT_SECONDS) for did in deployment_ids)
        )

    # 级联删部署（与「删除部署」路由同一套清理：四层规划产物 / 端点 / 路由绑定 / 日志）
    for deployment_id in deployment_ids:
        await deployment_service.delete_deployment_cascade(db, deployment_id)

    # 该节点上的模型分发记录与任务行（逻辑外键，无 DB 级联）——留着的 PENDING
    # ENSURE_MODEL 会被周期 redrive 每 30s 重发给一台不存在的节点，纯噪声
    await db.execute(delete(NodeModel).where(NodeModel.node_id == node_id))
    await db.execute(delete(Task).where(Task.node_id == node_id))
    await db.execute(delete(Gpu).where(Gpu.node_id == node_id))
    await db.delete(node)
    await db.flush()
    await db.commit()

    # 进程内账本同步（DB 权威行已随级联删除，这里只清内存态）
    for deployment_id in deployment_ids:
        try:
            await orch.release_vram(deployment_id)
        except Exception as e:
            log.warning("强制移除节点：释放部署 %s 的预留失败: %s", deployment_id[:8], e)

    # 模型分发需求重算：失主模型在其它节点上的下载需求可能随之消失
    try:
        await PlacementService().recompute_demand()
    except Exception:
        log.exception("强制移除节点：recompute_demand 失败（不影响已完成的删除）")

    await set_node_tombstone(node_id)
    log.warning(
        "节点 %s 已强制移除（连带删除 %d 个部署：%s）",
        node_id,
        len(deployment_ids),
        deployment_ids,
    )
    return DecommissionResult(notified=False, cleaned=False, deleted_deployments=deployment_ids)
