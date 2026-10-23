"""execute_shell 独立 RPC 链路单元测试。

不依赖 HTTP 路由挂载（绕开 test_manager_node_dispatch 的 404 基建问题），
直接通过管道传输验证 Manager→Node 的 execute_shell RPC 调用。
"""

from __future__ import annotations

import asyncio

import pytest_asyncio

from llmpod.tunnel.core.hub import Hub
from llmpod.tunnel.core.rpc_channel import RpcChannel
from llmpod.tunnel.core.registry import ServiceRegistry
from llmpod.tunnel.contract import ContainerService, NodeService
from llmpod.tunnel.schemas import (
    ContainerState,
    ContainerSummary,
    DeployResult,
    DeploySpec,
    NodeReport,
    RemoveResult,
    ShellResult,
    StopResult,
)


class _PipeTransport:
    """双向管道传输：两端通过两个 asyncio.Queue 通信。"""

    def __init__(self, send_q: asyncio.Queue, recv_q: asyncio.Queue):
        self._send = send_q
        self._recv = recv_q
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
        await self._send.put(None)


def _make_pipe():
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    # master 发到 q1、从 q2 收；worker 发到 q2、从 q1 收
    return _PipeTransport(q1, q2), _PipeTransport(q2, q1)


class _StubNodeService:
    """记录所有 RPC 调用的桩实现（实现全部 2 个契约，无 catch-all）。"""

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

    async def list(self, label_filter=None, all=True) -> list[ContainerSummary]:
        self.calls.append(("list", label_filter, all))
        return []

    async def inspect(self, name: str) -> ContainerState:
        self.calls.append(("inspect", name))
        return ContainerState(Status="running", Running=True, Restarting=False, RestartCount=0)

    async def execute_shell(self, command, timeout=60, cwd=None, env=None) -> ShellResult:
        self.calls.append(("execute_shell", command, timeout, cwd, env))
        return ShellResult(exit_code=0, stdout=f"out:{command}", stderr="")

    async def docker_pull(self, image):
        yield "x"

    async def tail_log(self, container_name, lines=20):
        yield "x"

    async def info(self) -> NodeReport:
        return NodeReport()


@pytest_asyncio.fixture
async def rpc_pair():
    """返回 (stub, master_channel) 已就绪的 RPC 对。"""
    stub = _StubNodeService()
    worker_registry = ServiceRegistry()
    worker_registry.register(stub, "ContainerService")
    worker_registry.register(stub, "NodeService")

    master_t, worker_t = _make_pipe()
    master_channel = RpcChannel(master_t, registry=None, name="master-test")
    worker_channel = RpcChannel(worker_t, registry=worker_registry, name="worker-test")
    await master_channel.start()
    await worker_channel.start()

    hub = Hub()
    hub.register("node-1", master_channel, NodeReport())
    try:
        yield stub, hub
    finally:
        await master_channel.close()
        await worker_channel.close()


class TestExecuteShellRpc:
    async def test_execute_shell_round_trip(self, rpc_pair):
        """execute_shell 作为独立 RPC 被 Manager 调用，参数透传。"""
        stub, hub = rpc_pair
        proxy = hub.proxy("node-1", NodeService)

        result = await proxy.execute_shell(command="ls -la", timeout=30, cwd="/tmp")

        assert result.exit_code == 0
        assert "ls -la" in result.stdout
        # 验证 stub 收到独立 execute_shell 调用，参数完整透传
        shell_calls = [c for c in stub.calls if c[0] == "execute_shell"]
        assert len(shell_calls) == 1
        assert shell_calls[0][1] == "ls -la"
        assert shell_calls[0][2] == 30
        assert shell_calls[0][3] == "/tmp"

    async def test_execute_shell_defaults(self, rpc_pair):
        """仅传 command，其余参数使用默认值。"""
        stub, hub = rpc_pair
        proxy = hub.proxy("node-1", NodeService)

        result = await proxy.execute_shell(command="whoami")

        assert result.exit_code == 0
        shell_calls = [c for c in stub.calls if c[0] == "execute_shell"]
        assert len(shell_calls) == 1
        assert shell_calls[0][1] == "whoami"
        assert shell_calls[0][2] == 60  # 默认 timeout

    async def test_docker_run_typed_rpc(self, rpc_pair):
        """docker_run 作为独立 typed RPC（不再经 execute_task 分发）。"""
        stub, hub = rpc_pair
        proxy = hub.proxy("node-1", ContainerService)

        result = await proxy.deploy(spec={"image": "nginx", "name": "c1", "args": ["serve"]})

        assert result.success is True
        assert result.container_name == "c1"
        run_calls = [c for c in stub.calls if c[0] == "deploy"]
        assert len(run_calls) == 1
        assert run_calls[0][1].image == "nginx"
        # 不应存在 execute_task 调用（catch-all 已删）
        assert not any(c[0] == "execute_task" for c in stub.calls)

    async def test_docker_inspect_typed_rpc(self, rpc_pair):
        """docker_inspect 返回 State 子树（gate 用）。"""
        stub, hub = rpc_pair
        proxy = hub.proxy("node-1", ContainerService)

        state = await proxy.inspect(name="c1")

        assert state.Status == "running"
        assert state.Running is True
        assert state.RestartCount == 0
