"""Manager 启动引导：node hello/heartbeat/close 回调 + GPU 过期恢复 + 升级联动。"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Mapping
from typing import NamedTuple

import llmpod
from llmpod.apps.manager import app as manager_app_mod
from llmpod.apps.manager.dispatch import send_node_upgrade
from llmpod.apps.manager.node_addr import resolve_node_ip
from llmpod.config.settings import settings as global_settings
from llmpod.db import get_db_context
from llmpod.models.node import Node
from llmpod.services.node_service import (
    node_tombstone_hit,
    register_node,
    sync_gpus,
    mark_node_offline,
    validate_cluster,
    touch_node_heartbeat,
)
from llmpod.apps.manager.nodehub import NodeHub
from llmpod.tunnel.core.rpc_channel import RpcChannel
from llmpod.tunnel.core.registry import ServiceRegistry
from llmpod.tunnel.schemas import NodeReport
from llmpod.tunnel.schemas.system import REGISTER_NACK_DECOMMISSIONED
from llmpod.apps.webserver.utils.bootstrap_token import bootstrap_signing_key, verify_bootstrap_token

log = logging.getLogger("llmpod.apps.manager.bootstrap")

# 自动升级重提议的冷却表（node_id → 上次实际下发提议的 monotonic 时刻）。
# hello 对账只在节点重连时发生——在线节点失败一次（pull 超时等）后不重连就
# 停在旧版无人再管，心跳驱动补上这一程。冷却防失败循环期间每 10s 一跳就重发
# （提议本身幂等、node 忙时忽略，但省掉无谓的升级重启与日志）。进程内状态：
# manager 重启即清零，无持久化必要（重启本身会触发全量 hello 对账）；键为在册
# 节点 id、每键一个 float，量级受集群规模约束。
_UPGRADE_PROPOSE_AT: dict[str, float] = {}
_UPGRADE_REPROPOSE_COOLDOWN = 600.0


class NodeAdmission(NamedTuple):
    node_id: str
    cluster_id: int
    node_token: str
    bootstrap_token: str


async def authenticate_upgrade(headers: Mapping[str, str], lifespan) -> NodeAdmission | None:
    """在 WebSocket accept 前校验节点身份，不创建通道或改变节点状态。"""
    authorization = headers.get("authorization", "")
    scheme, separator, node_token = authorization.partition(" ")
    node_id = headers.get("x-llmpod-node-id", "")
    cluster_text = headers.get("x-llmpod-cluster-id", "")
    bootstrap_token = headers.get("x-llmpod-bootstrap-token", "")
    if (
        scheme.lower() != "bearer"
        or not separator
        or not 0 < len(node_token) <= 256
        or len(node_id) > 64
        or not cluster_text.isdecimal()
        or len(cluster_text) > 10
        or len(bootstrap_token) > 2048
    ):
        return None
    cluster_id = int(cluster_text)
    if cluster_id <= 0 or not lifespan.node_token or not hmac.compare_digest(node_token, lifespan.node_token):
        return None

    if node_id:
        async with get_db_context() as db:
            node = await db.get(Node, node_id)
            if node is None or node.cluster_id != cluster_id or await node_tombstone_hit(node_id):
                return None
    elif not verify_bootstrap_token(
        bootstrap_signing_key(lifespan.settings or global_settings), bootstrap_token, cluster_id
    ):
        return None
    return NodeAdmission(node_id, cluster_id, node_token, bootstrap_token)


def _upgrade_proposal_due(node_id: str) -> bool:
    """距上次实际下发升级提议是否已过冷却窗。"""
    last = _UPGRADE_PROPOSE_AT.get(node_id)
    return last is None or (time.monotonic() - last) >= _UPGRADE_REPROPOSE_COOLDOWN


def setup_callbacks(channel: RpcChannel, hub: NodeHub, lifespan):
    """将 LLMPod 特定的回调绑定到 RpcChannel 上。"""
    settings = lifespan.settings

    async def on_hello(params: dict):
        """Node $hello：校验共享令牌、首次引导令牌，再完成注册。"""
        if getattr(channel, "_hello_complete", False):
            await channel.close()
            return
        node_id = params.get("node_id")
        token = params.get("token")
        cluster_id = params.get("cluster_id")
        info = NodeReport.model_validate(params.get("info") or {})

        if not lifespan.node_token or not hmac.compare_digest(lifespan.node_token, str(token or "")):
            await channel.close()
            return
        if not node_id and not verify_bootstrap_token(
            bootstrap_signing_key(lifespan.settings or global_settings),
            str(params.get("bootstrap_token") or ""),
            int(cluster_id or 0),
        ):
            await channel.close()
            return

        # 1. 注销标记拦截：删除节点后残活/自清理失败的容器重连，在此终结
        # （注销标记读写见 services.node_service，Redis 不可用 fail-open）
        if node_id and await node_tombstone_hit(str(node_id)):
            log.warning("node_id=%s 命中注销标记，拒绝注册", node_id)
            try:
                await channel.send_notification("$register_nack", {"reason": REGISTER_NACK_DECOMMISSIONED})
            except Exception:
                pass
            await channel.close()
            return

        # 2. 验证 cluster_id 必填且存在
        if not cluster_id:
            log.warning("cluster_id not provided, rejecting registration")
            await channel.close()
            return

        async with get_db_context() as db:
            if not await validate_cluster(cluster_id, db):
                log.warning("cluster_id=%s not found or inactive, rejecting", cluster_id)
                await channel.close()
                return

        # 3. 注册/更新节点（版本/ip 变迁在 register_node 内落审计）。地址选定
        # 不探活、不猜地址（口径见 node_addr 模块头）：显式配置 > 已存合法值
        # （沿用首次选定，重连不重协商）> 自报值（未显式固定收下+告警）。选定在
        # register_node 已加载的行上就地做，不多开一次 session 重读同一行。
        # 引擎 api_base 与多机 rendezvous 都消费 Node.ip——换址必须走操作者重新指定。
        async with get_db_context() as db:
            node_id, is_first = await register_node(node_id, cluster_id, info, db, resolve_ip=resolve_node_ip)
            await touch_node_heartbeat(node_id, db)

        # 4. 在 hub 中注册（info 统一存 NodeReport 实例，与 on_heartbeat 口径一致）。
        # **先 register 新 channel、再踢旧**：升级替换流程让「旧容器还活着 + 新容器注册」
        # 这个 duplicate 场景首次真实发生——若先踢旧，旧 channel 的 on_close 会因
        # hub 里仍是旧 channel 而误判 `node.channel is channel` 成立、把节点标 offline，
        # 覆盖刚由 register_node 写入的 online（引擎连续性：manager 视角节点全程不 offline）。
        # 同一连接只暴露绑定了鉴权节点的服务；在 node_id 放行入站调用前完成。
        from llmpod.apps.manager.services.task_ingest import ManagerTaskServiceImpl

        registry = ServiceRegistry()
        registry.register(ManagerTaskServiceImpl(actor_node_id=node_id), "ManagerTaskService")
        channel.registry = registry
        channel.node_id = node_id
        channel._hello_complete = True
        old = hub.get(node_id)
        hub.register(node_id, channel, info)
        if old is not None and old.channel is not channel:
            log.warning("node_id=%s duplicate connection, kicking old", node_id)
            await old.channel.close()
        log.info("Node online: %s (first=%s)", node_id, is_first)

        # 5. 同步 GPU（静态资产写库）。sync_gpus 失败不阻断注册（修复）：
        # 节点仍 online 并发 $register_ack，GPU 可在下次心跳/重连重试；避免「Node 已入库但 ack 永不发」的幽灵节点。
        try:
            async with get_db_context() as db:
                await sync_gpus(node_id, info.gpus, db)
        except Exception as e:
            log.error("sync_gpus failed for node %s (non-fatal; ack still sent): %s", node_id, e)

        # 6. 初始化 GPU 实时状态缓冲（首次注册即写入一帧，便于即时查询）
        gpu_buf = manager_app_mod.gpu_buffer
        if gpu_buf is not None:
            gpu_buf.update(node_id, info.gpus)

        # 6b. 初始化节点系统状态缓冲（实时 RAM，feasibility GGUF offload 用）
        sys_buf = manager_app_mod.system_buffer
        if sys_buf is not None:
            sys_buf.update(node_id, info)

        # 7. 发送 $register_ack，首次登记的 node_id 由 Manager 分配。
        ack_params: dict = {
            "node_id": node_id,
            "heartbeat_interval": settings.node_heartbeat_interval if settings else 10,
            "allowed_capabilities": info.capabilities,
        }
        await channel.send_notification("$register_ack", ack_params)

        # 8. 升级联动（重连补发）：manager 升级重启 → 节点重连即在此对账——
        # 落后即提议（在线检查/落后判定/官方镜像推导与 admin 手动升级同源
        # dispatch.send_node_upgrade，自动路径受 node_auto_upgrade 开关闸门）。
        # 通知是提议，node 自判（已达标/升级中/目标镜像不可拉 → 忽略）；
        # 发送失败下次重连 hello 兜底。
        try:
            dispatched, reason = await send_node_upgrade(node_id, current_version=info.version)
            if dispatched:
                _UPGRADE_PROPOSE_AT[node_id] = time.monotonic()
                log.info(
                    "node %s 版本落后（%s < %s），已发送升级提议", node_id, info.version or "?", llmpod.__version__
                )
            elif reason:
                log.debug("升级提议未下发 node=%s：%s", node_id, reason)
        except Exception:
            log.debug("升级提议发送失败（下次重连 hello 兜底）node=%s", node_id)

    async def on_heartbeat(params: dict):
        """Node $heartbeat：更新 hub 信息 + GPU 状态缓冲。

        info 携带 CPU/内存/swap/disk/load 等运行时指标与 GPU 实时状态。
        高频字段写入内存缓冲（GpuStatusBuffer），DB 仅保存静态资产。
        """
        node_id = channel.node_id
        if not node_id:
            return
        node = hub.get(node_id)
        raw_info = params.get("info") or {}
        info = NodeReport.model_validate(raw_info)
        if node:
            # hello 期选定的 ip 在本连接生命周期内保持：心跳不重选（避免全集群
            # 每 10s 查一遍库），地址沿用由断线重连的 $hello 查库实现。
            if node.info and node.info.ip:
                info.ip = node.info.ip
            node.last_heartbeat = time.time()
            node.info = info

        # 写 DB last_heartbeat_at（让该索引列活起来，DB 节点列表与内存口径一致）；
        # 心跳顺带落 version——升级追平后即使不重连，列表也立即翻新
        try:
            async with get_db_context() as db:
                await touch_node_heartbeat(node_id, db, version=info.version or None)
        except Exception as e:
            log.debug("touch_node_heartbeat failed for %s: %s", node_id, e)

        # 更新 GPU 实时状态缓冲 + 热插拔检测（GPU uuid 集合变化 → sync_gpus 写库）
        gpu_buf = manager_app_mod.gpu_buffer
        if gpu_buf is not None and "gpus" in raw_info:
            gpus = info.gpus
            new_uuids = {g.uuid for g in gpus if g.uuid}
            if new_uuids != gpu_buf.gpu_uuids(node_id):
                # GPU 集合变化（热插拔/驱动重载）→ 写库，使新卡对调度可见
                try:
                    async with get_db_context() as db:
                        await sync_gpus(node_id, gpus, db)
                except Exception as e:
                    log.warning("hot-plug sync_gpus for node %s failed: %s", node_id, e)
            gpu_buf.update(node_id, gpus)

        # 更新节点系统状态缓冲（实时 RAM）
        sys_buf = manager_app_mod.system_buffer
        if sys_buf is not None:
            sys_buf.update(node_id, info)

        # 升级失败重提议（自动路径）：hello 对账只在重连时发生，在线节点失败一次
        # （pull 超时等）后就停在旧版无人再管。心跳带着版本——落后且冷却已过即
        # 再提议。开关闸门与落后判定都在 dispatch.send_node_upgrade（auto 路径受
        # node_auto_upgrade 闸门）：开关关/版本不落后时在版本比较处即返回，每跳
        # 只花一次字典查询 + 一次版本比较，无副作用。
        if info.version and _upgrade_proposal_due(node_id):
            try:
                dispatched, _reason = await send_node_upgrade(node_id, current_version=info.version)
                if dispatched:
                    _UPGRADE_PROPOSE_AT[node_id] = time.monotonic()
                    log.info(
                        "node %s 仍落后（%s < %s），已重发升级提议（心跳对账）",
                        node_id,
                        info.version,
                        llmpod.__version__,
                    )
            except Exception:
                log.debug("心跳升级重提议失败 node=%s", node_id)

    async def on_close():
        """Node 断开连接：从 hub 中移除，标记为离线，清空 GPU 缓冲。

        （通道从 server _channels 的移除由 TunnelServer.serve_channel 无条件兜底，
        不依赖本回调——此处只做节点业务态清理。）
        """
        node_id = getattr(channel, "node_id", None)
        if not node_id:
            return
        node = hub.get(node_id)
        if node is not None and node.channel is channel:
            hub.remove(node_id)
            async with get_db_context() as db:
                await mark_node_offline(node_id, db)
            # 清空该节点的 GPU 实时状态缓冲
            gpu_buf = manager_app_mod.gpu_buffer
            if gpu_buf is not None:
                gpu_buf.remove_node(node_id)
            # 清空该节点的系统状态缓冲
            sys_buf = manager_app_mod.system_buffer
            if sys_buf is not None:
                sys_buf.remove_node(node_id)
            log.info("Node offline: %s", node_id)

    async def on_upgrade_result(params: dict):
        """Node $node_upgrade_result：升级结果落系统审计（admin 审计页可见）。

        身份只认通道：``channel.node_id`` 在 $hello 通过 token/注销标记/cluster 校验
        后才有值——通知绑定在通道建立时就生效，注册前的任意连接可发帧伪造节点
        身份刷审计，帧内自报的 node_id 不可信（不一致即丢弃）。best-effort 通道
        （成功路径老容器 rm -f 自己后通知可能未达；失败路径 WS 必在）；write_audit
        自身兜底不抛。审计的另一保险是 register_node 的 version 变迁检测
        （不依赖本通知送达）。
        """
        node_id = channel.node_id
        if not node_id:
            log.warning("upgrade_result 来自未注册通道，丢弃: %s", params)
            return
        claimed = str(params.get("node_id") or "")
        if claimed and claimed != node_id:
            log.warning("upgrade_result 帧内 node_id=%s 与通道 %s 不符，丢弃", claimed, node_id)
            return
        result = str(params.get("result") or "unknown")
        detail = {
            "result": result,
            "target_version": params.get("target_version"),
        }
        if params.get("error_class"):
            detail["error_class"] = params["error_class"]
        if params.get("detail"):
            detail["detail"] = str(params["detail"])[:300]
        from llmpod.services.audit_service import write_audit

        await write_audit(
            actor_type="system",
            action="upgrade",
            resource_type="node",
            resource_id=node_id,
            result="success" if result == "success" else "failed",
            detail=detail,
        )

    # 绑定回调
    channel.on_hello = on_hello
    channel.on_heartbeat = on_heartbeat
    channel.on_close = on_close
    channel.on_notification("$node_upgrade_result", on_upgrade_result)
