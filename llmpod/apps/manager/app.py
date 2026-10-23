"""Manager 应用：WebSocket 中心节点 + 编排生命周期。

Manager 仅暴露 WebSocket 路由 (/ws/node) 供 Node 反向连接。
任务下发等 HTTP API 由 webserver 通过 import manager 包
(访问 node_hub 单例) 实现,不在此挂任何 REST 路由。

结构（参照 webserver 的瘦 app.py + 关注点分离）:
  app.py         - ManagerLifespan + create_manager_router + 全局单例
  bootstrap.py   - 节点事件回调 (on_hello/on_heartbeat/on_close)
  buffers.py     - GPU / 节点系统 实时状态内存缓冲
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, WebSocket

from llmpod.apps.manager.buffers import GpuStatusBuffer, SystemStatusBuffer
from llmpod.config.settings import Settings
from llmpod.apps.manager.nodehub import NodeHub
from llmpod.core.taskwatch import BackgroundTaskManager

log = logging.getLogger("llmpod.apps.manager")

_HELLO_TIMEOUT_SECONDS = 10
# ENSURE_MODEL 周期重发间隔：稳态 PENDING 空集，全表扫描成本可忽略；间隔只需
# 覆盖「RPC 瞬时失败到自动恢复」的典型抖动窗口
_REDRIVE_INTERVAL_S = 30.0

# 全局单例（由管理端生命周期注入）
node_hub: NodeHub | None = None
# GPU 实时状态内存缓冲（进程内存态，与 node_hub 同生命周期）
gpu_buffer: GpuStatusBuffer | None = None
# 节点系统状态内存缓冲（实时 RAM 等，feasibility GGUF offload 判定用）
system_buffer: SystemStatusBuffer | None = None


class ManagerLifespan:
    """Manager 生命周期管理句柄。"""

    def __init__(self):
        self.settings: Settings | None = None
        self.node_token: str = ""
        self._background_tasks: BackgroundTaskManager | None = None

    async def startup(self, settings: Settings):
        self.settings = settings
        global node_hub, gpu_buffer, system_buffer
        node_hub = NodeHub()
        gpu_buffer = GpuStatusBuffer()
        system_buffer = SystemStatusBuffer()

        token_path = Path(settings.node_token_file)
        if settings.node_token:
            self.node_token = settings.node_token
        elif token_path.exists():
            self.node_token = token_path.read_text().strip()
        else:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            self.node_token = secrets.token_urlsafe(32)
            token_path.write_text(self.node_token)
            try:
                token_path.chmod(0o600)
            except OSError:
                pass  # 网络文件系统等不支持 chmod：文件已在本地，权限宽松不阻断启动
        if not self.node_token:
            raise ValueError("node token is empty")
        log.info("Manager started, node_token loaded (len=%d)", len(self.node_token))

        # 启动恢复：节点/GPU offline 对齐 + creating/launching 部署清空重跑 + VRAM 预留重建
        from llmpod.apps.manager.deployment.recovery import recover_on_startup
        from llmpod.apps.manager.deployment.orchestrator import get_orchestrator

        await recover_on_startup()
        await get_orchestrator().ledger.rebuild_from_db()

        from llmpod.apps.manager.deployment.controller import get_controller

        background_tasks = BackgroundTaskManager()
        self._background_tasks = background_tasks
        # 部署控制循环停摆影响收敛：保留原来的 CRITICAL 告警与 2s 重启节奏。
        background_tasks.start(
            get_controller().run,
            name="deploy-controller",
            restart_delay=2.0,
            failure_level=logging.CRITICAL,
        )
        # ENSURE_MODEL PENDING 周期重发：dispatch 瞬时失败 / node 在线但 RPC 抖动时，
        # 任务留 PENDING 无人重发（重连 sync 才触发），staging 会一直挂着
        background_tasks.every(_redrive_once, name="model-redrive", interval=_REDRIVE_INTERVAL_S)

    async def shutdown(self):
        global node_hub, gpu_buffer, system_buffer
        if self._background_tasks is not None:
            await self._background_tasks.stop()
            self._background_tasks = None
        if node_hub:
            await node_hub.close()
            node_hub = None
        if gpu_buffer is not None:
            gpu_buffer.clear()
            gpu_buffer = None
        if system_buffer is not None:
            system_buffer.clear()
            system_buffer = None
        log.info("Manager shutdown complete")


manager_lifespan = ManagerLifespan()


async def _redrive_once() -> None:
    """重发 PENDING 的 ENSURE_MODEL（幂等；失败仍留 PENDING 下轮再试）。

    任务留 PENDING 的成因是下发瞬时失败（node 在线、RPC 抖动），重连 sync 之外
    唯一的自愈入口——没有本循环，staging 会挂到节点重连或 manager 重启。
    """
    from llmpod.apps.manager.deployment.placement import PlacementService

    n = await PlacementService().redrive_pending()
    if n:
        log.info("periodic redrive: %d PENDING task(s) redispatched", n)


def create_manager_router() -> APIRouter:
    """创建 Manager WebSocket 路由 (仅 /ws/node)。

    任务下发相关 REST API 由 webserver 侧实现,通过 import manager 包
    访问 node_hub 单例完成 RPC 调用。
    """
    router = APIRouter()

    @router.websocket("/ws/node")
    async def ws_node(websocket: WebSocket):
        """Node 连接端点。"""
        if node_hub is None:
            await websocket.close(code=1011, reason="Manager not ready")
            return
        from llmpod.apps.manager.bootstrap import authenticate_upgrade, setup_callbacks

        try:
            admission = await authenticate_upgrade(websocket.headers, manager_lifespan)
        except Exception:
            log.exception("Node WebSocket header authentication failed")
            await websocket.close(code=1008)
            return
        if admission is None:
            await websocket.close(code=1008)
            return
        channel = await node_hub.accept(websocket)

        try:
            setup_callbacks(channel, node_hub, manager_lifespan)
            # 鉴权前只读一帧；通用 RPC 循环只处理已注册的连接。
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=_HELLO_TIMEOUT_SECONDS)
            try:
                hello = json.loads(raw)
            except (TypeError, ValueError):
                return
            if (
                not isinstance(hello, dict)
                or hello.get("method") != "$hello"
                or "id" in hello
                or not isinstance(hello.get("params"), dict)
            ):
                return
            params = hello["params"]
            if (
                (params.get("node_id") or "") != admission.node_id
                or params.get("cluster_id") != admission.cluster_id
                or params.get("token") != admission.node_token
                or (params.get("bootstrap_token") or "") != admission.bootstrap_token
            ):
                return
            await channel.on_hello(params)
            if channel.closed or not channel.node_id:
                return
            await channel.start()
            await channel.wait_closed()
        except asyncio.TimeoutError:
            log.warning("Node $hello timed out")
        except Exception:
            log.exception("Node WebSocket failed")
        finally:
            await channel.close()
            # 被拒连接也必须从 TunnelServer 的通道跟踪中移除。
            await node_hub.serve_channel(channel)

    return router
