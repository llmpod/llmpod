"""manager → node 任务下发链路的集成测试。

测试完整链路: API → webserver → manager 模块 → RPC → node。

不依赖真实 PG/Redis: 通过 FastAPI 依赖注入覆盖鉴权 + DB,
通过 monkeypatch 注入一个"内存传输 + 桩 NodeService"的伪节点到
node_hub 单例中,使整条链路在进程内闭环。

注意: 这里测试真实的 RpcChannel 编解码 + Hub.proxy 反射调用,
只是省略了 WebSocket 物理传输,改用 asyncio.Queue 模拟双向管道。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from llmpod.tunnel.schemas import (
    ContainerState,
    DeployResult,
    DeploySpec,
    NodeReport,
    RemoveResult,
    ShellResult,
    StopResult,
)


# ============================================================
# 测试基础设施: 内存双向管道传输 + 桩 NodeService
# ============================================================


class PipeTransport:
    """两个端点通过 asyncio.Queue 互连的内存双向传输。

    master.send → worker.recv; worker.send → master.recv。
    close 时向对端投递 None 哨兵以唤醒阻塞的 receive_text。
    """

    def __init__(self, name: str, recv: asyncio.Queue, send: asyncio.Queue):
        self.name = name
        self._recv = recv
        self._send = send
        self._closed = False

    async def send_text(self, data: str) -> None:
        if self._closed:
            return
        await self._send.put(data)

    async def receive_text(self) -> str:
        if self._closed:
            raise ConnectionError("transport closed")
        item = await self._recv.get()
        if item is None:
            raise ConnectionError("peer closed")
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 唤醒对端阻塞的 receive_text
        await self._send.put(None)


def make_pipe():
    """创建一对相连的 (master_transport, worker_transport)。"""
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    return PipeTransport("master", q1, q2), PipeTransport("worker", q2, q1)


class StubNodeService:
    """测试用 NodeService 桩实现 - 记录所有调用并返回可预测的响应。"""

    def __init__(self):
        self.calls: list[tuple] = []

    async def deploy(self, spec: DeploySpec) -> DeployResult:
        self.calls.append(("deploy", spec))
        return DeployResult(success=True, container_name=spec.name)

    async def stop(self, name: str, timeout: int = 10) -> StopResult:
        self.calls.append(("stop", name))
        return StopResult(success=True, container_name=name)

    async def remove(self, name: str, force: bool = False) -> RemoveResult:
        self.calls.append(("remove", name, force))
        return RemoveResult(success=True, removed=True, container_name=name)

    async def list(self, label_filter=None, all=True) -> list[dict]:
        self.calls.append(("list", label_filter, all))
        return []

    async def inspect(self, name: str) -> ContainerState:
        self.calls.append(("inspect", name))
        return ContainerState(Status="running", Running=True, Restarting=False, RestartCount=0)

    async def execute_shell(
        self,
        command: str,
        timeout: int = 60,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        self.calls.append(("execute_shell", command, timeout, cwd, env))
        return ShellResult(
            exit_code=0,
            stdout=f"stub stdout for: {command}",
            stderr="",
        )

    async def docker_pull(self, image: str) -> AsyncIterator[str]:
        self.calls.append(("docker_pull", image))
        yield f"Pulling from {image}"
        yield "Digest: sha256:stubdigest"
        yield "Status: Image is up to date"

    async def tail_log(self, container_name: str, lines: int = 20) -> AsyncIterator[str]:
        self.calls.append(("tail_log", container_name, lines))
        for i in range(min(lines, 3)):  # 测试中只产 3 行
            yield f"[{container_name}] line {i + 1}"

    async def info(self) -> NodeReport:
        self.calls.append(("info",))
        return NodeReport(
            node_id="test-node-001",
            hostname="stub-host",
            ip="10.0.0.1",
            cpu_cores=8,
            capabilities=["docker", "shell", "inference"],
        )


# ============================================================
# Fixtures
# ============================================================

NODE_ID = "test-node-001"
# webserver 路由统一挂在 /api/v1 前缀下；admin 子路由再挂 /admin（RBAC 重构后收拢至 /api/v1/admin/*）
PREFIX = "/api/v1/admin/orchestration"


@pytest_asyncio.fixture
async def stub_node(monkeypatch):
    """注入一个桩节点到 node_hub 单例。

    返回 (stub_service, master_channel, worker_channel)。
    """
    from llmpod.apps.manager import app as manager_app_mod
    from llmpod.tunnel.core.rpc_channel import RpcChannel
    from llmpod.tunnel.core.registry import ServiceRegistry
    from llmpod.tunnel.core.hub import Hub

    # 1. 创建管道传输对
    master_transport, worker_transport = make_pipe()

    # 2. 桩服务 + worker 端注册表
    stub = StubNodeService()
    worker_registry = ServiceRegistry()
    worker_registry.register(stub, "ContainerService")
    worker_registry.register(stub, "NodeService")

    # 3. 创建两端 RpcChannel (共享同一传输管道)
    master_channel = RpcChannel(
        master_transport,
        registry=None,
        name="master-test",
    )
    worker_channel = RpcChannel(
        worker_transport,
        registry=worker_registry,
        name="worker-test",
    )

    # 4. 启动两端读循环
    await master_channel.start()
    await worker_channel.start()

    # 5. 构造一个最小 Hub 并注册节点
    fake_hub = Hub()
    fake_hub.register(NODE_ID, master_channel, NodeReport(hostname="stub-host"))
    # 给 NodeInfo 设置 last_heartbeat
    fake_hub.get(NODE_ID).last_heartbeat = 9999999999.0

    # 6. monkeypatch 替换 manager 模块中的 node_hub 单例
    monkeypatch.setattr(manager_app_mod, "node_hub", fake_hub)

    try:
        yield stub, master_channel, worker_channel, fake_hub
    finally:
        await master_channel.close()
        await worker_channel.close()


@pytest_asyncio.fixture
async def client(stub_node):
    """带 stub_node 注入 + 鉴权/DB 覆盖的 HTTP 测试客户端。"""
    from _fakes import admin_session

    from llmpod.app import app
    from llmpod.apps.webserver.auth.deps import get_current_user
    from llmpod.db import get_db

    # 覆盖鉴权依赖: 任何请求都被当作已登录超管
    # （is_superuser=True 让 require_permission 短路，不再查 DB 权限）
    async def _fake_user():
        return admin_session(role_ids=[])

    # 覆盖 DB 依赖: 不连接真实 PostgreSQL
    # （require_permission 的 _checker 即便超管短路，FastAPI 仍会解析 get_db）
    async def _stub_get_db():
        yield None

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = _stub_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


# ============================================================
# 测试用例
# ============================================================


class TestOrchestrationStatus:
    """编排引擎状态端点。"""

    async def test_status(self, client: AsyncClient, stub_node):
        """GET /api/v1/orchestration/status 返回引擎状态 + 在线节点数。"""
        response = await client.get(f"{PREFIX}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 200
        assert data["data"]["status"] == "running"
        assert "task_dispatch" in data["data"]["features"]
        assert data["data"]["online_nodes"] == 1


class TestListOnlineNodes:
    """在线节点列表端点。"""

    async def test_list_online_nodes(self, client: AsyncClient, stub_node):
        """GET /api/v1/orchestration/nodes 返回在线节点列表。"""
        response = await client.get(f"{PREFIX}/nodes")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 200
        assert len(data["data"]) == 1
        assert data["data"][0]["node_id"] == NODE_ID


class TestNodeInfoRpc:
    """通过 RPC 查询节点信息。"""

    async def test_get_node_info(self, client: AsyncClient, stub_node):
        """GET /api/v1/orchestration/nodes/{id}/info 触发 NodeService.info() RPC。"""
        stub, *_ = stub_node
        response = await client.get(f"{PREFIX}/nodes/{NODE_ID}/info")

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 200
        assert data["data"]["node_id"] == NODE_ID
        assert data["data"]["hostname"] == "stub-host"
        # 验证 RPC 真的被调用了
        assert ("info",) in stub.calls

    async def test_get_info_node_not_found(self, client: AsyncClient, stub_node):
        """不存在的节点 → 404。"""
        response = await client.get(f"{PREFIX}/nodes/nonexistent/info")
        assert response.status_code == 404


class TestDockerRunRpc:
    """容器运行快捷端点（直调 NodeService.deploy(spec)，typed 契约；URL 沿用 /tasks/docker-run）。"""

    async def test_docker_run(self, client: AsyncClient, stub_node):
        """POST /api/v1/orchestration/nodes/{id}/tasks/docker-run → proxy.deploy(spec)（URL 沿用）。"""
        stub, *_ = stub_node
        response = await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/docker-run",
            json={
                "image": "nginx:latest",
                "name": "test-nginx",
                "ports": {"80": 8080},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 200
        assert data["data"]["success"] is True
        assert data["data"]["container_name"] == "test-nginx"

        # 验证 typed docker_run RPC 收到 spec（参数透传，无 execute_task 包装）
        run_calls = [c for c in stub.calls if c[0] == "deploy"]
        assert len(run_calls) == 1
        spec = run_calls[0][1]
        assert spec.image == "nginx:latest"
        assert spec.name == "test-nginx"
        assert spec.ports == {"80": 8080}
        # 不应存在 execute_task 调用（catch-all 已删）
        assert not any(c[0] == "execute_task" for c in stub.calls)

    async def test_docker_run_node_offline(self, client: AsyncClient, stub_node):
        """目标节点不在线 → 404。"""
        response = await client.post(
            f"{PREFIX}/nodes/nonexistent/tasks/docker-run",
            json={"image": "nginx:latest", "name": "x"},
        )
        assert response.status_code == 404


class TestShellRpc:
    """shell 快捷端点。"""

    async def test_shell(self, client: AsyncClient, stub_node):
        """POST /api/v1/orchestration/nodes/{id}/tasks/shell。"""
        stub, *_ = stub_node
        response = await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/shell",
            json={"command": "ls -la", "timeout": 30},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 200
        # execute_shell RPC 直接返回 {exit_code, stdout, stderr}，无外层 result 包装
        assert data["data"]["exit_code"] == 0
        assert "stub stdout for: ls -la" in data["data"]["stdout"]

        # 验证走独立 execute_shell RPC（不再经 execute_task 分发）
        shell_calls = [c for c in stub.calls if c[0] == "execute_shell"]
        assert len(shell_calls) == 1
        assert shell_calls[0][1] == "ls -la"
        assert shell_calls[0][2] == 30
        exec_calls = [c for c in stub.calls if c[0] == "execute_task"]
        assert len(exec_calls) == 0


class TestStreamingRpc:
    """流式 RPC (docker_pull / tail_log) 端点。"""

    async def test_docker_pull_stream(self, client: AsyncClient, stub_node):
        """POST /api/v1/orchestration/nodes/{id}/tasks/docker-pull 返回 NDJSON 流。"""
        stub, *_ = stub_node
        response = await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/docker-pull",
            json={"image": "nginx:latest"},
            headers={"Accept-Encoding": "gzip"},  # 浏览器强制带 gzip，须验证不被压缩
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "content-encoding" not in response.headers  # 流式响应不得被 gzip

        # 解析 NDJSON: 每行一个 JSON 对象
        lines = [json.loads(line) for line in response.text.strip().split("\n") if line]

        # 至少 2 个 data 帧 + 1 个 end 帧
        data_events = [e for e in lines if e["event"] == "data"]
        end_events = [e for e in lines if e["event"] == "end"]
        assert len(data_events) >= 2
        assert len(end_events) == 1
        assert "nginx:latest" in data_events[0]["data"]

        # 验证流式 RPC 被调用
        pull_calls = [c for c in stub.calls if c[0] == "docker_pull"]
        assert len(pull_calls) == 1
        assert pull_calls[0][1] == "nginx:latest"

    async def test_tail_log_stream(self, client: AsyncClient, stub_node):
        """POST /api/v1/orchestration/nodes/{id}/tasks/tail-log 返回 NDJSON 流。"""
        stub, *_ = stub_node
        response = await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/tail-log",
            json={"container_name": "mycontainer", "lines": 10},
            headers={"Accept-Encoding": "gzip"},  # 浏览器强制带 gzip，须验证不被压缩
        )

        assert response.status_code == 200
        assert "content-encoding" not in response.headers  # 流式响应不得被 gzip
        lines = [json.loads(line) for line in response.text.strip().split("\n") if line]

        data_events = [e for e in lines if e["event"] == "data"]
        assert len(data_events) >= 1
        assert "mycontainer" in data_events[0]["data"]

        tail_calls = [c for c in stub.calls if c[0] == "tail_log"]
        assert len(tail_calls) == 1
        # (method_name, container_name, lines)
        assert tail_calls[0][1] == "mycontainer"
        assert tail_calls[0][2] == 10


class TestRpcLinkIntegrity:
    """验证完整链路的关键不变量: 框架正确编解码 + 真实 RPC 调用。"""

    async def test_rpc_actually_invokes_stub(self, client: AsyncClient, stub_node):
        """连续调用多个端点,验证每次都到达桩并产生独立的 RPC 调用。"""
        stub, *_ = stub_node

        await client.get(f"{PREFIX}/nodes/{NODE_ID}/info")
        await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/shell",
            json={"command": "cmd1"},
        )
        await client.post(
            f"{PREFIX}/nodes/{NODE_ID}/tasks/shell",
            json={"command": "cmd2"},
        )

        info_calls = [c for c in stub.calls if c[0] == "info"]
        # shell 走独立 execute_shell RPC（不再经 execute_task 分发）
        shell_calls = [c for c in stub.calls if c[0] == "execute_shell"]
        assert len(info_calls) == 1
        assert len(shell_calls) == 2
        # 两次 shell 调用的 command 不同（execute_shell 桩记录: (name, command, timeout, cwd, env)）
        commands = [c[1] for c in shell_calls]
        assert commands == ["cmd1", "cmd2"]

    async def test_concurrent_rpc_requests(self, client: AsyncClient, stub_node):
        """并发 RPC 请求应该都被处理 (单连接多路复用)。"""
        import asyncio as _asyncio

        n = 5
        results = await _asyncio.gather(*[client.get(f"{PREFIX}/nodes/{NODE_ID}/info") for _ in range(n)])

        assert all(r.status_code == 200 for r in results)
        stub, *_ = stub_node
        info_calls = [c for c in stub.calls if c[0] == "info"]
        assert len(info_calls) == n
