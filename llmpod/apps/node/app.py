"""Node 主入口：asyncio 事件循环 + 断线重连。

Node 进程：纯 asyncio，不使用 FastAPI，通过 WebSocket 客户端连接 Manager。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from llmpod.config.settings import settings
from llmpod.core.exceptions import ConfigError
from llmpod.core.logging import setup_logging
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.tunnel import TunnelClient
from llmpod.tunnel.core.registry import ServiceRegistry
from llmpod.apps.node.services import ContainerServiceImpl, NodeServiceImpl
from llmpod.apps.node.deployers import make_deployer

log = logging.getLogger("llmpod.apps.node")


async def main():
    """Node 主入口函数。"""
    setup_logging(settings.log_level)

    log.info("Starting LLMPod Node...")

    # Node ID：首次注册时由 Manager 生成的 UUID，持久化到 node_id_file。
    # 优先级：环境变量 LLMPOD_NODE_ID > 本地文件 > 空（首次注册，等待 Manager 分配）
    if not settings.node_id:
        id_path = Path(settings.node_id_file)
        if id_path.exists():
            settings.node_id = id_path.read_text().strip()
            log.info("Reusing registered node_id from %s: %s", settings.node_id_file, settings.node_id)
        else:
            log.info("No registered node_id found, first registration, waiting for Manager $register_ack")

    # 验证 cluster_id 是否已设置（必填）
    if not settings.node_cluster_id:
        raise ConfigError("LLMPOD_NODE_CLUSTER_ID 未设置：须由引导脚本注入")

    # 初始化服务实现（容器操作委托 Deployer）
    deployer = make_deployer(settings)
    container_svc = ContainerServiceImpl(deployer)
    # node_id 经回调取 client.node_id（client 在下方构造；首启注册前为空，与 $hello 同口径）
    node_svc = NodeServiceImpl(settings, node_id_provider=lambda: client.node_id)

    # 注册本地服务，供 Manager 调用（按类名注册，wire 格式 ContainerService.deploy 等）
    registry = ServiceRegistry()
    registry.register(container_svc, "ContainerService")
    registry.register(node_svc, "NodeService")

    # Node 声明式任务编排（local_tasks 持久化 + NodeAgent + TaskService RPC）。
    # agent 的 client 绑定延迟到 client 构造后（attach_client），打破 registry↔client↔agent 环。
    # registry 注入共享 deployer：容器类任务（PULL_IMAGE/…）经它操作 runtime。
    from llmpod.apps.node.agent import NodeAgent
    from llmpod.apps.node.agent_store import TaskStore
    from llmpod.apps.node.executors import default_registry
    from llmpod.apps.node.services.task import TaskServiceImpl
    from llmpod.apps.node.upgrader import NodeUpgrader
    from llmpod.tunnel.schemas.system import REGISTER_NACK_DECOMMISSIONED

    task_store = TaskStore(settings.node_task_db)
    node_agent = NodeAgent(task_store, default_registry(deployer))

    # 自升级执行器：维护回调挂 agent（取消任务 + 关/重开 sqlite）；结果经 live
    # channel 上报 $node_upgrade_result（best-effort；闭包延迟解析 client）
    async def _report_upgrade_result(outcome) -> None:
        ch = client.channel
        if ch is None:
            log.info("升级结果上报跳过（channel 离线）: %s", outcome.result)
            return
        await ch.send_notification("$node_upgrade_result", outcome.to_params(client.node_id))

    upgrader = NodeUpgrader(
        settings,
        suspend=node_agent.suspend_for_upgrade,
        resume=node_agent.resume_after_upgrade,
        report_result=_report_upgrade_result,
    )

    async def _on_decommission(params: dict) -> None:
        await upgrader.self_destruct("decommission")

    async def _on_register_nack(params: dict) -> None:
        reason = str(params.get("reason") or "")
        if reason == REGISTER_NACK_DECOMMISSIONED:
            await upgrader.self_destruct(f"register_nack: {reason}")
            return
        log.error("注册被拒：%s（token 失效或 manager 拒绝），稍后重连重试", reason)

    async def _on_registered(node_id: str) -> None:
        # $register_ack 后写 ready marker（每次注册都写；老容器的升级轮询据此判定）
        upgrader.write_ready_marker(node_id)
        # 名字被升级接管者持有 → 残活前任自清理（残活 -old 防残活互踢，见 upgrader）
        await upgrader.retire_if_superseded()

    registry.register(
        TaskServiceImpl(node_agent, upgrade_busy=lambda: upgrader.busy),
        "TaskService",
    )

    # 读取 node token（优先级：环境变量 LLMPOD_NODE_TOKEN > node_token_file）
    token = settings.node_token
    if not token:
        token_path = Path(settings.node_token_file)
        if token_path.exists():
            token = token_path.read_text().strip()
        else:
            raise ConfigError("节点令牌缺失：请设置 LLMPOD_NODE_TOKEN 或创建 node_token 文件")

    # 节点信息采集器：$hello/$heartbeat 携带的 info 由它产出（系统 + GPU + capabilities）
    from llmpod.apps.node.collectors import collect_node_info

    # 创建 Node 客户端（首次启动时 node_id 为空，Manager 通过 $register_ack 分配）
    client = TunnelClient(
        master_url=settings.node_manager_url,
        node_id=settings.node_id,
        cluster_id=settings.node_cluster_id,
        token=token,
        bootstrap_token=settings.node_bootstrap_token,
        registry=registry,
        node_id_file=settings.node_id_file,
        heartbeat_interval=settings.node_heartbeat_interval,
        reconnect_fast_interval=settings.node_reconnect_fast_interval,
        reconnect_slow_interval=settings.node_reconnect_slow_interval,
        reconnect_fast_window=settings.node_reconnect_fast_window,
        # info_collector：$hello/$heartbeat 的 info 由它产出（系统 + GPU +
        # capabilities；引擎 /metrics 已改由管理端 scraper 直接抓取）
        info_collector=collect_node_info,
        # manager 下发的升级/注销/注册拒绝通知 → upgrader（$register_nack：
        # 注销标记命中 → 自清理；token 失效 → 重连循环重试）
        notification_handlers={
            "$node_upgrade": upgrader.handle_upgrade,
            "$node_decommission": _on_decommission,
            "$register_nack": _on_register_nack,
        },
        # $register_ack 后写 ready marker（老容器的升级等就绪轮询据此判定）
        on_registered=_on_registered,
    )

    # 启动残留恢复：升级中断（断电/进程被杀）后重启，按落盘相位收敛。
    # 必须先于 task_store.init()——恢复可能用快照覆盖任务库文件，库已打开、
    # 连接入池后再原地覆盖，池内连接会对着被换掉的文件继续读写
    recovered = await upgrader.recover_on_boot()
    if recovered:
        log.info("升级残留已收敛，继续正常启动")
    await task_store.init()

    background_tasks = BackgroundTaskManager()
    try:
        # agent/monitor 与 Node 进程同生命周期；离线时仍自主运行。
        node_agent.attach_client(client)
        background_tasks.start(node_agent.run, name="node-agent")

        from llmpod.apps.node.manager_link import ManagerLink
        from llmpod.apps.node.monitor import ContainerLivenessMonitor

        monitor = ContainerLivenessMonitor(
            deployer,
            ManagerLink(client),
            scan_interval=settings.node_container_scan_interval,
        )
        background_tasks.start(monitor.run, name="container-monitor")

        # 连接到 Manager（内置分级重连策略；$heartbeat 携带状态信息）
        await client.run()
    except KeyboardInterrupt:
        log.info("Node shutting down...")
    finally:
        await background_tasks.stop()


if __name__ == "__main__":
    asyncio.run(main())
