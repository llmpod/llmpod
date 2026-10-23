"""隧道核心单元测试: RpcChannel、Hub、ServiceRegistry。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from llmpod.tunnel.core.rpc_channel import RpcChannel, StreamContext
from llmpod.tunnel.core.registry import ServiceRegistry
from llmpod.tunnel.core.hub import Hub, ServiceProxy
from llmpod.tunnel.entity.protocol import request, success, stream_frame, stream_end, error
from llmpod.tunnel.entity.errors import RemoteError, NodeOffline
from llmpod.tunnel.schemas.system import NodeReport

if TYPE_CHECKING:
    from llmpod.tunnel.client import TunnelClient


class TestStreamContext:
    """测试 StreamContext 异步迭代器。"""

    @pytest.mark.asyncio
    async def test_push_and_iterate(self):
        """测试推送值和迭代。"""
        ctx = StreamContext("test-id")
        await ctx.push(1)
        await ctx.push(2)
        await ctx.push(3)
        ctx.end()

        values = []
        async for v in ctx:
            values.append(v)
            if len(values) >= 3:
                break

        assert values == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_error_terminates_iteration(self):
        """测试错误会终止迭代。"""
        ctx = StreamContext("test-id")
        await ctx.push(1)
        ctx.error(RemoteError("test error"))

        with pytest.raises(RemoteError, match="test error"):
            async for v in ctx:
                pass


class TestServiceRegistry:
    """测试 ServiceRegistry 分发逻辑。"""

    def test_register_and_get_callable(self):
        """测试服务注册和 get_callable。"""
        registry = ServiceRegistry()

        class TestService:
            def echo(self, msg: str) -> str:
                return msg

        service = TestService()
        name = registry.register(service, "TestService")
        assert name == "TestService"

        # 获取可调用对象
        fn = registry.get_callable("TestService", "echo")
        assert fn == service.echo

    def test_get_nonexistent_service_raises(self):
        """测试获取不存在的服务会抛出 KeyError。"""
        registry = ServiceRegistry()

        with pytest.raises(KeyError, match="not registered"):
            registry.get_callable("NonExistent", "method")

    def test_get_nonexistent_method_raises(self):
        """测试获取不存在的方法会抛出 KeyError。"""
        registry = ServiceRegistry()

        class TestService:
            def echo(self, msg: str) -> str:
                return msg

        registry.register(TestService(), "TestService")

        with pytest.raises(KeyError, match="not found"):
            registry.get_callable("TestService", "nonexistent")

    @pytest.mark.asyncio
    async def test_dispatch_sync_method(self):
        """测试分发同步方法返回元组 (value, result)。"""
        registry = ServiceRegistry()

        class TestService:
            def add(self, a: int, b: int) -> int:
                return a + b

        registry.register(TestService(), "TestService")

        kind, result = await registry.dispatch("TestService", "add", {"a": 2, "b": 3})
        assert kind == "value"
        assert result == 5

    @pytest.mark.asyncio
    async def test_dispatch_async_method(self):
        """测试分发异步方法。"""
        registry = ServiceRegistry()

        class TestService:
            async def async_add(self, a: int, b: int) -> int:
                await asyncio.sleep(0)
                return a + b

        registry.register(TestService(), "TestService")

        kind, result = await registry.dispatch("TestService", "async_add", {"a": 10, "b": 20})
        assert kind == "value"
        assert result == 30

    @pytest.mark.asyncio
    async def test_dispatch_generator_method(self):
        """测试分发生成器方法返回 (stream, async_gen)。"""
        registry = ServiceRegistry()

        class TestService:
            async def count(self, n: int):
                for i in range(n):
                    yield i

        registry.register(TestService(), "TestService")

        # 应返回 (stream, async_generator)
        kind, gen = await registry.dispatch("TestService", "count", {"n": 3})
        assert kind == "stream"
        assert hasattr(gen, "__aiter__")

        values = []
        async for v in gen:
            values.append(v)

        assert values == [0, 1, 2]


class TestRpcChannel:
    """测试 RpcChannel 消息处理。"""

    @pytest.mark.asyncio
    async def test_send_notification(self):
        """测试发送通知。"""
        transport = AsyncMock()
        transport.send_text = AsyncMock()

        channel = RpcChannel(transport=transport)
        await channel.send_notification("$hello", {"node_id": "test"})

        transport.send_text.assert_called_once()
        frame = json.loads(transport.send_text.call_args[0][0])
        assert frame["method"] == "$hello"
        assert frame["params"]["node_id"] == "test"
        assert "id" not in frame  # 通知没有 id

    @pytest.mark.asyncio
    async def test_handle_call(self):
        """测试处理传入的调用（带 id 的请求）。"""
        registry = ServiceRegistry()

        class TestService:
            def echo(self, msg: str) -> str:
                return f"echo: {msg}"

        registry.register(TestService(), "TestService")

        transport = AsyncMock()
        transport.send_text = AsyncMock()

        channel = RpcChannel(transport=transport, registry=registry)

        # 模拟传入的请求
        req = request("TestService.echo", {"msg": "hello"}, "req-1")
        await channel._handle_call(req)

        # 应发送响应
        transport.send_text.assert_called_once()
        resp = json.loads(transport.send_text.call_args[0][0])
        assert resp["id"] == "req-1"
        assert resp["result"] == "echo: hello"

    @pytest.mark.asyncio
    async def test_handle_unary_result(self):
        """测试处理一元结果响应。"""
        transport = AsyncMock()
        transport.send_text = AsyncMock()

        channel = RpcChannel(transport=transport)

        # 创建待处理的请求
        fut = asyncio.Future()
        channel._pending["req-1"] = fut

        # 模拟带结果的响应
        resp = success("req-1", {"data": "test"})
        await channel._handle(resp)

        # Future 应被解析
        assert fut.done()
        assert fut.result() == {"data": "test"}

    @pytest.mark.asyncio
    async def test_handle_error_response(self):
        """测试处理错误响应。"""
        transport = AsyncMock()
        transport.send_text = AsyncMock()

        channel = RpcChannel(transport=transport)

        # 创建待处理的请求
        fut = asyncio.Future()
        channel._pending["req-1"] = fut

        # 模拟错误响应
        err = error("req-1", -32000, "test error")
        await channel._handle(err)

        # Future 应抛出 RemoteError
        assert fut.done()
        with pytest.raises(RemoteError, match="test error"):
            fut.result()

    @pytest.mark.asyncio
    async def test_closed_channel_discards_queued_call_and_cancels_notification(self):
        registry = ServiceRegistry()
        calls = []

        class TestService:
            def run(self):
                calls.append("rpc")

        registry.register(TestService(), "TestService")
        channel = RpcChannel(AsyncMock(), registry=registry)
        started = asyncio.Event()

        async def heartbeat(_params):
            started.set()
            await asyncio.Future()

        channel.on_heartbeat = heartbeat
        task = asyncio.create_task(channel._handle({"method": "$heartbeat", "params": {}}))
        channel._bg_tasks.add(task)
        await started.wait()
        await channel.close()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 模拟已排队、却在关闭后才开始处理的 RPC 帧。
        await channel._handle(request("TestService.run", {}, "1"))
        assert calls == []

    @pytest.mark.asyncio
    async def test_handle_stream_frames(self):
        """测试处理流帧。"""
        transport = AsyncMock()
        transport.send_text = AsyncMock()

        channel = RpcChannel(transport=transport)

        # 创建流上下文
        ctx = StreamContext("req-1")
        channel._pending["req-1"] = ctx

        # 模拟流帧
        await channel._handle(stream_frame("req-1", 1))
        await channel._handle(stream_frame("req-1", 2))
        await channel._handle(stream_end("req-1"))

        # 收集值
        values = []
        async for v in ctx:
            values.append(v)

        assert values == [1, 2]


class TestHub:
    """测试 Hub 节点管理。"""

    def test_register_and_get_node(self):
        """测试注册和获取节点。"""
        hub = Hub()
        channel = MagicMock()

        hub.register("node-1", channel, NodeReport(hostname="test"))
        node = hub.get("node-1")

        assert node is not None
        assert node.node_id == "node-1"
        assert node.channel == channel
        assert node.info.hostname == "test"

    def test_get_nonexistent_node(self):
        """测试获取不存在的节点返回 None。"""
        hub = Hub()
        assert hub.get("nonexistent") is None

    def test_remove_node(self):
        """测试移除节点。"""
        hub = Hub()
        channel = MagicMock()

        hub.register("node-1", channel, NodeReport())
        hub.remove("node-1")

        assert hub.get("node-1") is None

    def test_list_nodes(self):
        """测试列出所有节点。"""
        hub = Hub()

        hub.register("node-1", MagicMock(), NodeReport())
        hub.register("node-2", MagicMock(), NodeReport())
        hub.register("node-3", MagicMock(), NodeReport())

        nodes = hub.list()
        assert len(nodes) == 3
        node_ids = {n.node_id for n in nodes}
        assert node_ids == {"node-1", "node-2", "node-3"}

    def test_proxy_returns_service_proxy(self):
        """测试代理返回 ServiceProxy。"""
        hub = Hub()
        channel = MagicMock()

        hub.register("node-1", channel, NodeReport())

        class TestService:
            def echo(self, msg: str) -> str:
                pass

        proxy = hub.proxy("node-1", TestService)
        assert isinstance(proxy, ServiceProxy)

    def test_proxy_nonexistent_node_raises_on_call(self):
        """测试在不存在的节点上调用代理方法会抛出 NodeOffline。"""
        hub = Hub()

        class TestService:
            def echo(self, msg: str) -> str:
                return msg

        # proxy() 返回 ServiceProxy（不会立即报错）
        proxy = hub.proxy("nonexistent", TestService)
        assert isinstance(proxy, ServiceProxy)

        # 但调用其方法会抛出 NodeOffline
        with pytest.raises(NodeOffline):
            proxy.echo("test")


async def test_manager_hub_binds_auth_callbacks_before_reading_hello():
    """预排队的 hello 不能抢先触发 TunnelServer 的无鉴权默认回调。"""
    import json
    from llmpod.apps.manager.nodehub import NodeHub

    class QueuedWebSocket:
        def __init__(self):
            self.frames = asyncio.Queue()
            self.read_count = 0

        async def accept(self):
            pass

        async def receive_text(self):
            self.read_count += 1
            frame = await self.frames.get()
            if frame is None:
                raise RuntimeError("closed")
            return frame

        async def send_text(self, _frame):
            pass

        async def close(self):
            await self.frames.put(None)

    ws = QueuedWebSocket()
    await ws.frames.put(json.dumps({"method": "$hello", "params": {"node_id": "attacker"}}))
    hub = NodeHub()
    channel = await hub.accept(ws)
    await asyncio.sleep(0)
    assert ws.read_count == 0
    assert hub.get("attacker") is None

    seen = asyncio.Event()

    async def reject_hello(_params):
        seen.set()
        await channel.close()

    channel.on_hello = reject_hello
    await channel.start()
    await asyncio.wait_for(seen.wait(), timeout=1)
    assert hub.get("attacker") is None

    # 消融：恢复“启动读循环后才绑定业务回调”的旧顺序，同一帧会经默认
    # TunnelServer._on_hello 把未认证者注册进 hub。
    ws_before_fix = QueuedWebSocket()
    await ws_before_fix.frames.put(json.dumps({"method": "$hello", "params": {"node_id": "attacker"}}))
    old_order_hub = NodeHub()
    old_order_channel = await old_order_hub.server.accept_worker(ws_before_fix, start=True)
    for _ in range(100):
        if old_order_hub.get("attacker") is not None:
            break
        await asyncio.sleep(0.001)
    assert old_order_hub.get("attacker") is not None
    await old_order_channel.close()


@pytest.mark.parametrize(
    "first_frame, authorized",
    [
        (request("TestService.run", {}, "1"), False),
        ({"method": "$heartbeat", "params": {}}, False),
        ({"method": "$hello", "id": "1", "params": {"valid": True}}, False),
        ({"method": "$hello", "params": []}, False),
        ({"method": "$hello", "params": {"valid": False}}, False),
        ({"method": "$hello", "params": {"valid": True, "cluster_id": 1, "token": "t", "bootstrap_token": "b"}}, True),
    ],
)
async def test_manager_dispatches_only_after_authenticated_hello(monkeypatch, first_frame, authorized):
    """manager 首帧门：$hello 校验通过前不进读循环，抢跑 RPC 无从到达；合法握手后可调用。"""
    from llmpod.apps.manager import app as manager_app
    from llmpod.apps.manager import bootstrap
    from llmpod.apps.manager.nodehub import NodeHub

    class WebSocket:
        def __init__(self):
            self.frames = asyncio.Queue()
            self.headers = {}

        async def accept(self):
            pass

        async def receive_text(self):
            frame = await self.frames.get()
            if frame is None:
                raise RuntimeError("closed")
            return frame

        async def send_text(self, _frame):
            pass

        async def close(self, **_kwargs):
            await self.frames.put(None)

    called = asyncio.Event()
    registry = ServiceRegistry()

    class TestService:
        async def run(self):
            called.set()

    registry.register(TestService(), "TestService")
    hub = NodeHub()
    monkeypatch.setattr(manager_app, "node_hub", hub)

    def setup(channel, _hub, _lifespan):
        channel.registry = registry

        async def hello(params):
            if params["valid"]:
                channel.node_id = "n1"
                hub.register("n1", channel, NodeReport())

        async def close():
            if hub.get("n1") and hub.get("n1").channel is channel:
                hub.remove("n1")

        channel.on_hello = hello
        channel.on_close = close

    monkeypatch.setattr(bootstrap, "setup_callbacks", setup)

    async def admit(*_args):
        return bootstrap.NodeAdmission("", 1, "t", "b")

    monkeypatch.setattr(bootstrap, "authenticate_upgrade", admit)
    ws = WebSocket()
    task = asyncio.create_task(manager_app.create_manager_router().routes[0].endpoint(ws))
    try:
        await ws.frames.put(json.dumps(first_frame))
        await ws.frames.put(json.dumps(request("TestService.run", {}, "1")))
        if authorized:
            await asyncio.wait_for(called.wait(), 1)
            assert hub.get("n1") is not None
        else:
            await asyncio.wait_for(task, 1)
            assert not called.is_set()
    finally:
        await ws.frames.put(None)
        await asyncio.wait_for(task, 1)
    assert hub.get("n1") is None
    assert hub.server._channels == []


async def test_manager_closes_idle_connection_without_hello(monkeypatch):
    from llmpod.apps.manager import app as manager_app
    from llmpod.apps.manager import bootstrap
    from llmpod.apps.manager.nodehub import NodeHub

    class IdleWebSocket:
        def __init__(self):
            self.closed = False
            self.read_cancelled = False
            self.headers = {}

        async def accept(self):
            pass

        async def receive_text(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.read_cancelled = True
                raise

        async def close(self, **_kwargs):
            self.closed = True

    hub = NodeHub()
    channels = []
    monkeypatch.setattr(manager_app, "node_hub", hub)
    monkeypatch.setattr(manager_app, "_HELLO_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bootstrap, "setup_callbacks", lambda channel, *_: channels.append(channel))

    async def admit(*_args):
        return bootstrap.NodeAdmission("", 1, "t", "b")

    monkeypatch.setattr(bootstrap, "authenticate_upgrade", admit)
    ws = IdleWebSocket()

    await manager_app.create_manager_router().routes[0].endpoint(ws)

    assert ws.read_cancelled and ws.closed
    assert channels[0].closed
    assert hub.server._channels == []


class TestHelloAckWatchdog:
    """$hello 后无 $register_ack 不得无限等待——看门狗超时断开、重连退避。"""

    def _client(self, tmp_path, **kw) -> "TunnelClient":
        from llmpod.tunnel.client import TunnelClient

        return TunnelClient(
            "ws://127.0.0.1:8080/ws/node",
            node_id="n1",
            token="t",
            node_id_file=str(tmp_path / "node_id"),
            heartbeat_interval=999,
            hello_ack_timeout=kw.pop("hello_ack_timeout", 0.05),
            **kw,
        )

    async def test_watchdog_fires_when_no_ack(self, monkeypatch, tmp_path):
        """超时无 ack → 看门狗关 channel，_serve 返回（主循环随即走重连退避）。"""
        import websockets

        c = self._client(tmp_path)
        channel = MagicMock()
        channel.closed = True
        channel.start = AsyncMock()
        channel.send_notification = AsyncMock()
        closed_event = asyncio.Event()

        async def _wait_closed():
            await closed_event.wait()

        channel.wait_closed = _wait_closed
        channel.close = AsyncMock(side_effect=lambda: closed_event.set())

        class _FakeConnect:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(websockets, "connect", lambda *a, **k: _FakeConnect())
        import llmpod.tunnel.client as client_mod

        monkeypatch.setattr(client_mod, "RpcChannel", lambda *a, **k: channel)

        await asyncio.wait_for(c._serve(), timeout=2)
        channel.close.assert_awaited()

    async def test_ack_cancels_watchdog(self, tmp_path):
        """ack 到达 → 看门狗任务被取消，连接保持。"""
        c = self._client(tmp_path, hello_ack_timeout=10.0)
        c._ack_watchdog = asyncio.create_task(c._hello_ack_watchdog())
        await asyncio.sleep(0.01)  # 让任务真正启动
        await c._on_register_ack({"node_id": "n1"})
        await asyncio.sleep(0.01)
        assert c._ack_watchdog is None or c._ack_watchdog.cancelled()

    async def test_serve_cleanup_after_ack(self, monkeypatch, tmp_path):
        """注册成功（看门狗已撤）后断连：finally 清理不得抛 AttributeError，
        且 channel 必须清空——否则重连间隙 online 恒 True，重连后的全量对账被禁用。"""
        import websockets

        c = self._client(tmp_path, hello_ack_timeout=10.0)
        channel = MagicMock()
        channel.closed = True
        channel.start = AsyncMock()
        channel.send_notification = AsyncMock()

        async def _wait_closed():
            # 模拟注册成功在先、断连在后：ack 把看门狗撤掉，然后连接结束
            await c._on_register_ack({"node_id": "n1"})

        channel.wait_closed = _wait_closed
        channel.close = AsyncMock()

        class _FakeConnect:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(websockets, "connect", lambda *a, **k: _FakeConnect())
        import llmpod.tunnel.client as client_mod

        monkeypatch.setattr(client_mod, "RpcChannel", lambda *a, **k: channel)

        await asyncio.wait_for(c._serve(), timeout=2)  # 旧实现此处抛 AttributeError
        assert c.channel is None, "断连后 channel 必须清空（online 判定依据）"

    async def test_collect_info_timeout_falls_back(self, tmp_path):
        """采集器卡死：整体超时回退基础信息，$hello/心跳不因采集悬挂。"""
        import time as _time

        from llmpod.tunnel.schemas.system import NodeReport

        def stuck_collector():
            _time.sleep(0.5)
            return NodeReport(hostname="stuck")

        c = self._client(tmp_path, info_collector=stuck_collector)
        report = await asyncio.wait_for(c._collect_info_async(timeout=0.05), timeout=2)
        assert report.hostname != "stuck", "超时必须回退本机基础信息"
        # 单飞：上一帧仍在途（线程未返回）时不再叠加新任务，继续回退基础信息
        again = await asyncio.wait_for(c._collect_info_async(timeout=5), timeout=2)
        assert again.hostname != "stuck", "in-flight 期间回退基础信息而非叠线程"


class TestStreamBackpressure:
    """流帧反压：入队阻塞必须作用在读取循环上（慢消费者 → TCP 背压），而不是
    每帧起独立任务（慢消费者下 _bg_tasks 无界增长、反压名存实亡）。"""

    @staticmethod
    def _paused_consumer_channel() -> tuple[RpcChannel, asyncio.Event]:
        """挂一个不做消费的 StreamContext，返回通道与放行事件。"""
        transport = AsyncMock()
        transport.send_text = AsyncMock()
        channel = RpcChannel(transport=transport)
        ctx = StreamContext("req-1", maxsize=2)
        channel._pending["req-1"] = ctx
        release = asyncio.Event()
        return channel, release

    @pytest.mark.asyncio
    async def test_stream_frame_blocks_read_loop_until_consumed(self):
        """流帧入队反压作用在读取循环上：队列满后读循环阻塞在 push，不再拉新帧，
        _bg_tasks 不堆积；消费一帧即恢复读取、in-flight 帧不丢。"""
        channel, gate = self._paused_consumer_channel()

        read_calls = 0

        async def receive_text():
            # 第 4 帧起挂起（模拟上游暂无更多数据）：1/2 帧入队填满队列(maxsize=2)，
            # 第 3 帧阻塞在 _stream_push 的入队上——这正是被测的反压点
            nonlocal read_calls
            read_calls += 1
            n = read_calls
            if n > 4:
                await gate.wait()
            return json.dumps({"id": "req-1", "stream": True, "result": {"n": n}})

        channel.transport.receive_text = receive_text
        await channel.start()

        async def settle(cond):
            for _ in range(400):
                if cond():
                    return True
                await asyncio.sleep(0.005)
            return False

        # 稳态①：1/2 帧入队 + 第 3 帧阻塞在 push，读取循环停摆
        assert await settle(lambda: channel._pending["req-1"]._q.full()), "队列应被填满"
        await asyncio.sleep(0.02)
        assert read_calls == 3, f"读取循环应停在 in-flight 帧，实读 {read_calls}"
        assert not channel._bg_tasks, "流帧不得走派生任务（反压必须作用在读取循环）"

        # 消费 1 帧 → 队列腾位 → in-flight 帧(3)入队、读循环继续拉第 4 帧（随后挂起）
        ctx = channel._pending["req-1"]
        first = await asyncio.wait_for(ctx.__anext__(), timeout=2)
        assert first["n"] == 1
        assert await settle(lambda: read_calls >= 4), "队列腾位后读取循环应继续"

        second = await asyncio.wait_for(ctx.__anext__(), timeout=2)
        assert second["n"] == 2
        third = await asyncio.wait_for(ctx.__anext__(), timeout=2)
        assert third["n"] == 3, "in-flight 帧在腾位后入队，不丢帧"
        # 第 4 帧随后也入队（队列此刻空），消费方拿得到
        fourth = await asyncio.wait_for(ctx.__anext__(), timeout=2)
        assert fourth["n"] == 4

        # 收尾：关流并关闭通道。gate 保持未置位——读循环停在第 5 帧的挂起点上，
        # 由 close() 的取消干净收尾（置位 gate 会让读循环在已置位事件上热自旋，
        # 无挂起点可投递取消，事件循环关不掉）
        ctx.end()
        await channel.close()

    @pytest.mark.asyncio
    async def test_non_stream_frame_still_spawns_task(self):
        """请求/通知/响应帧不受流反压影响，仍并发处理（单帧慢不拖慢读节奏）。"""
        channel, _ = self._paused_consumer_channel()
        channel._pending.clear()
        fut = asyncio.get_running_loop().create_future()
        channel._pending["req-2"] = fut

        served = False

        async def receive_text():
            # 首帧后挂起（模拟空闲连接）：立即返回的桩会让读循环独占事件循环，
            # future 回调永远得不到调度
            nonlocal served
            if served:
                await asyncio.Event().wait()
            served = True
            return json.dumps({"id": "req-2", "result": "ok"})

        channel.transport.receive_text = receive_text
        await channel.start()
        await asyncio.wait_for(fut, timeout=2)
        assert fut.result() == "ok"
        await channel.close()


class TestStreamBackpressureRelease:
    """流式背压的放弃放行：消费者断开时不得把读循环永久卡在满队列 put 上。"""

    @pytest.mark.asyncio
    async def test_abandon_releases_blocked_push(self):
        """满队列上阻塞的 push，经 abandon() 取消 putter 后立即返回。"""
        ctx = StreamContext("req-1", maxsize=1)
        await ctx.push("a")  # 队列满

        blocked = asyncio.ensure_future(ctx.push("b"))
        for _ in range(3):
            await asyncio.sleep(0)  # 让 push 挂到 put() 上
        assert not blocked.done()

        ctx.abandon()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(blocked, timeout=1.0)  # 必须立即放行（以取消形式）

        # 终止后再 push 直接丢弃（不入队、不阻塞）
        await ctx.push("c")
        assert ctx._term is not None

    @pytest.mark.asyncio
    async def test_abandon_terminates_iteration(self):
        """abandon 后已排队的帧吐完即止、空队列直接止——迭代不悬挂。

        （队列残余帧先行返回是 __anext__ 的既有语义：end/error 同样如此；
        abandon 的关键诉求是消费者（若还在）能终止、读循环能放行。）
        """
        ctx = StreamContext("req-2", maxsize=1)
        await ctx.push("a")
        ctx.abandon()
        seen = []
        async for v in ctx:
            seen.append(v)
        assert seen == ["a"]  # 排队帧吐出后立即终止

        ctx2 = StreamContext("req-3", maxsize=1)
        ctx2.abandon()
        seen2 = []
        async for v in ctx2:
            seen2.append(v)
        assert seen2 == []  # 空队列直接终止


class TestReadLoopSurvivesAbandonedStream:
    """回归：读循环内联入队的流帧，消费者放弃后通道不得假死。"""

    @pytest.mark.asyncio
    async def test_read_loop_survives_abandoned_stream(self):
        transport = AsyncMock()
        frames: asyncio.Queue[str] = asyncio.Queue()
        transport.receive_text = frames.get

        channel = RpcChannel(transport=transport)
        await channel.start()

        ctx = StreamContext("s1", maxsize=1)
        channel._pending["s1"] = ctx
        await frames.put(json.dumps({"id": "s1", "stream": True, "result": 1}))  # 填满
        await frames.put(json.dumps({"id": "s1", "stream": True, "result": 2}))  # 读循环将卡在这帧
        await asyncio.sleep(0.05)
        assert not channel.closed

        # 消费者放弃（= stream() finally 的动作：pop + abandon）
        channel._pending.pop("s1", None)
        ctx.abandon()

        # 后续一元响应帧必须仍能被处理——通道没有死在读循环里
        fut = asyncio.get_running_loop().create_future()
        channel._pending["u1"] = fut
        await frames.put(json.dumps({"id": "u1", "result": "ok"}))
        assert await asyncio.wait_for(fut, timeout=1.0) == "ok"

        await channel.close()
