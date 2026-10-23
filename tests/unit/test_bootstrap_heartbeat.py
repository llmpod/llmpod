"""Manager bootstrap 心跳/断线 → GPU 缓冲区链路单元测试。

不依赖真实 PG：on_heartbeat 仅触及 hub 与 gpu_buffer 内存态；
on_close 触碰 DB 的部分通过 monkeypatch get_db_context 屏蔽。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from llmpod.apps.manager.buffers import GpuStatusBuffer
from llmpod.tunnel.core.hub import Hub
from llmpod.tunnel.schemas.system import GpuInfo, NodeReport


class _FakeChannel:
    """最小化的 channel 替身：暴露回调槽（含扩展通知 handler 注册表）与 node_id。"""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self.on_hello = None
        self.on_heartbeat = None
        self.on_close = None
        self.notification_handlers: dict = {}
        self.sent: list[tuple[str, dict]] = []
        self.closed = False

    def on_notification(self, method: str, handler):
        self.notification_handlers[method] = handler

    async def send_notification(self, method: str, params: dict):
        self.sent.append((method, params))

    async def close(self):
        self.closed = True
        # 模拟真实 RpcChannel.close() → _on_closed() → on_close 回调（duplicate kick 时序用）
        if self.on_close is not None:
            await self.on_close()


class _FakeLifespan:
    def __init__(self):
        self.settings = None
        self.node_token = ""


def _gpu(uuid: str, **kw) -> GpuInfo:
    """构造完整 GpuInfo（必填字段 uuid/index/name 齐全）。"""
    return GpuInfo(uuid=uuid, index=0, name="Test-GPU", **kw)


@pytest_asyncio.fixture
async def wired_hub(monkeypatch):
    """装配好回调的 (hub, channel, gpu_buffer)。"""
    from llmpod.apps.manager import app as manager_app_mod
    from llmpod.apps.manager.bootstrap import setup_callbacks

    hub = Hub()
    buf = GpuStatusBuffer()
    # 注入 gpu_buffer 单例（替代 lifespan.startup 中的实例化）
    monkeypatch.setattr(manager_app_mod, "gpu_buffer", buf)

    channel = _FakeChannel(node_id="node-x")
    # 在 hub 中注册节点（带初始 info）
    hub.register("node-x", channel, NodeReport(hostname="h"))
    hub.get("node-x").last_heartbeat = 0.0

    setup_callbacks(channel, hub, _FakeLifespan())
    return hub, channel, buf


@asynccontextmanager
async def _fake_db_ctx():
    """屏蔽 on_close 中的 DB 会话创建（mark_node_offline 依赖真实 PG）。"""
    yield object()


async def _noop_mark_node_offline(node_id: str, db):
    """on_close 中替代 mark_node_offline 的空实现。"""
    return None


class TestHeartbeatGpuBufferWiring:
    async def test_heartbeat_updates_gpu_buffer(self, wired_hub):
        hub, channel, buf = wired_hub
        gpus = [_gpu("GPU-1", utilization=77.0, memory_utilization=50.0)]
        await channel.on_heartbeat(
            {
                "node_id": "node-x",
                "info": {"gpus": gpus, "cpu_percent": 30.0},
            }
        )
        # GPU 实时状态写入缓冲（GpuInfo 实例）
        buffered = buf.get("node-x")
        assert len(buffered) == 1
        assert buffered[0].uuid == "GPU-1"
        assert buffered[0].utilization == 77.0
        # hub info 更新为最新心跳 payload（NodeReport 实例）
        assert hub.get("node-x").info.cpu_percent == 30.0
        assert hub.get("node-x").last_heartbeat > 0

    async def test_heartbeat_without_gpus_keeps_buffer(self, wired_hub):
        hub, channel, buf = wired_hub
        # 首帧写入 GPU
        await channel.on_heartbeat({"info": {"gpus": [_gpu("GPU-1")]}})
        # 后续心跳无 gpus 字段，缓冲区保留上一帧
        await channel.on_heartbeat({"info": {"cpu_percent": 10.0}})
        assert buf.get("node-x")[0].uuid == "GPU-1"

    async def test_heartbeat_unknown_node_is_noop(self, wired_hub):
        """channel.node_id 未在 hub 注册时不应抛错。"""
        hub, channel, buf = wired_hub
        channel.node_id = "ghost"
        await channel.on_heartbeat({"info": {"gpus": [_gpu("G")]}})
        # 关键断言：未抛异常
        assert True

    async def test_on_close_clears_gpu_buffer(self, wired_hub, monkeypatch):
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        # 屏蔽 on_close 中的 DB 调用（mark_node_offline 依赖真实 PG）
        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)
        monkeypatch.setattr(bootstrap_mod, "mark_node_offline", _noop_mark_node_offline)

        hub, channel, buf = wired_hub
        buf.update("node-x", [_gpu("GPU-1")])
        await channel.on_close()
        # 节点从 hub 移除 + 缓冲清空
        assert hub.get("node-x") is None
        assert buf.get("node-x") == []


class TestHelloIpPinning:
    """on_hello 的地址选定接入：resolve_node_ip（显式 > 已存 > 自报）在
    register_node 已加载的行上就地选定，结果覆盖 info.ip 后落库注册。"""

    async def _patch_hello_deps(self, monkeypatch, hub, resolved_ip: str | None):
        """屏蔽 on_hello 的 DB/升级依赖；resolved_ip=None 表示走真实选定。"""
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        if resolved_ip is not None:

            def _resolve(info, stored):
                return resolved_ip

            monkeypatch.setattr(bootstrap_mod, "resolve_node_ip", _resolve)

        async def _ok_cluster(cluster_id, db):
            return True

        registered: dict = {}

        async def _reg(node_id, cluster_id, info, db, resolve_ip=None):
            registered["info"] = info
            registered["resolve_ip"] = resolve_ip
            if resolve_ip is not None:
                info.ip = resolve_ip(info, None)
            from llmpod.services.node_service import Registration

            return Registration(node_id=node_id or "node-x", is_first=not node_id)

        async def _touch(node_id, db, version=None):
            return None

        async def _sync(node_id, gpus, db):
            return None

        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)
        monkeypatch.setattr(bootstrap_mod, "validate_cluster", _ok_cluster)
        monkeypatch.setattr(bootstrap_mod, "register_node", _reg)
        monkeypatch.setattr(bootstrap_mod, "touch_node_heartbeat", _touch)
        monkeypatch.setattr(bootstrap_mod, "sync_gpus", _sync)
        return registered

    async def _hello(
        self, wired_hub, monkeypatch, *, info_extra: dict, resolved_ip: str | None, node_id: str | None = "node-x"
    ):
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        hub, channel, _buf = wired_hub
        registered = await self._patch_hello_deps(monkeypatch, hub, resolved_ip)
        monkeypatch.setattr(bootstrap_mod, "bootstrap_signing_key", lambda _settings: "test-key")
        monkeypatch.setattr(bootstrap_mod, "verify_bootstrap_token", lambda *_args: True)

        class _Lifespan(_FakeLifespan):
            def __init__(self):
                super().__init__()
                self.node_token = "t"
                self.settings = type("S", (), {"node_heartbeat_interval": 10})()

        bootstrap_mod.setup_callbacks(channel, hub, _Lifespan())
        await channel.on_hello(
            {
                "node_id": node_id,
                "token": "t",
                "cluster_id": 1,
                "info": {"hostname": "h", "ip": "172.17.0.2", **info_extra},
            }
        )
        return hub, channel, registered

    async def test_real_resolution_overrides_self_report(self, wired_hub, monkeypatch):
        """真实选定路径（register_node 桩内调用 resolve_ip）：选定值覆盖自报值，
        落库与 hub 内存态一致。"""
        hub, channel, registered = await self._hello(wired_hub, monkeypatch, info_extra={}, resolved_ip=None)
        assert registered["resolve_ip"] is not None  # on_hello 确实传了选定器
        assert registered["info"].ip == "172.17.0.2"  # 未显式固定收下自报值（真实选定分支 3）
        assert hub.get("node-x").info.ip == "172.17.0.2"

    async def test_resolved_ip_replaces_self_report(self, wired_hub, monkeypatch):
        hub, channel, registered = await self._hello(wired_hub, monkeypatch, info_extra={}, resolved_ip="192.0.2.9")
        # 落库注册与 hub 内存态都是选定后的地址
        assert registered["info"].ip == "192.0.2.9"
        assert hub.get("node-x").info.ip == "192.0.2.9"

    async def test_heartbeat_keeps_pinned_ip(self, wired_hub, monkeypatch):
        """心跳重报自报 ip 不得顶掉选定值（本连接生命周期内保持）。"""
        hub, channel, registered = await self._hello(wired_hub, monkeypatch, info_extra={}, resolved_ip="192.0.2.9")
        await channel.on_heartbeat({"info": {"hostname": "h", "ip": "172.17.0.2", "gpus": []}})
        assert hub.get("node-x").info.ip == "192.0.2.9"

    async def test_heartbeat_without_hello_info_uses_self_report(self, wired_hub, monkeypatch):
        """hub 里没有 hello 期 info（异常路径）→ 心跳按自报值原样更新，不炸。"""
        hub, channel, registered = await self._hello(wired_hub, monkeypatch, info_extra={}, resolved_ip="192.0.2.9")
        hub.get("node-x").info = None
        await channel.on_heartbeat({"info": {"hostname": "h", "ip": "10.0.0.8"}})
        assert hub.get("node-x").info.ip == "10.0.0.8"

    async def test_first_registration_still_resolves(self, wired_hub, monkeypatch):
        """首次注册（无 node_id）：选定器照传（stored=None → 未显式固定收下自报值），
        首注册与重连走同一条选定路径。"""
        hub, channel, registered = await self._hello(
            wired_hub, monkeypatch, info_extra={}, resolved_ip=None, node_id=None
        )
        assert registered["resolve_ip"] is not None
        assert registered["info"].ip == "172.17.0.2"


class TestUpgradeRelayOnHello:
    """on_hello 的升级联动：落后即发 $node_upgrade 提议（image_ref 由官方镜像推导）。"""

    async def _run_hello(self, wired_hub, monkeypatch, *, info_extra: dict, auto_upgrade: bool = True):
        from llmpod.apps.manager import bootstrap as bootstrap_mod
        from llmpod.apps.manager import app as manager_app_mod
        from llmpod.config.settings import settings as global_settings

        hub, channel, _buf = wired_hub
        # 屏蔽数据库访问（validate_cluster / register_node / sync_gpus）
        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)
        # 升级联动经 dispatch.send_node_upgrade → node_online 读全局 node_hub，
        # 指到本测试的 wired hub（raw Hub 实例，_get_hub 的 getattr 兜底直取）
        monkeypatch.setattr(manager_app_mod, "node_hub", hub)
        # 自动路径受 node_auto_upgrade 开关闸门（生产默认关）；本组默认按开启测
        monkeypatch.setattr(global_settings, "node_auto_upgrade", auto_upgrade)

        async def _ok_cluster(cluster_id, db):
            return True

        async def _reg(node_id, cluster_id, info, db, resolve_ip=None):
            from llmpod.services.node_service import Registration

            return Registration(node_id="node-x", is_first=False)

        async def _touch(node_id, db, version=None):
            return None

        async def _sync(node_id, gpus, db):
            return None

        monkeypatch.setattr(bootstrap_mod, "validate_cluster", _ok_cluster)
        monkeypatch.setattr(bootstrap_mod, "register_node", _reg)
        monkeypatch.setattr(bootstrap_mod, "touch_node_heartbeat", _touch)
        monkeypatch.setattr(bootstrap_mod, "sync_gpus", _sync)

        class _Lifespan(_FakeLifespan):
            def __init__(self):
                super().__init__()
                self.node_token = "t"  # 与 $hello token 匹配
                self.settings = type("S", (), {"node_heartbeat_interval": 10})()

        bootstrap_mod.setup_callbacks(channel, hub, _Lifespan())
        await channel.on_hello(
            {
                "node_id": "node-x",
                "token": "t",
                "cluster_id": 1,
                "info": {"hostname": "h", **info_extra},
            }
        )
        return channel

    async def test_outdated_node_gets_upgrade_proposal(self, wired_hub, monkeypatch):
        import llmpod
        from llmpod.services.version_service import official_image_ref

        channel = await self._run_hello(wired_hub, monkeypatch, info_extra={"version": "0.0.1"})
        upgrades = [p for m, p in channel.sent if m == "$node_upgrade"]
        assert len(upgrades) == 1
        assert upgrades[0]["target_version"] == llmpod.__version__
        # image_ref 由官方镜像 + 当前版本推导（无部署配置）
        assert upgrades[0]["image_ref"] == official_image_ref()

    async def test_switch_off_no_proposal(self, wired_hub, monkeypatch):
        """node_auto_upgrade 关闭（生产默认）：hello 对账只标落后，不下发提议。"""
        channel = await self._run_hello(wired_hub, monkeypatch, info_extra={"version": "0.0.1"}, auto_upgrade=False)
        assert not [p for m, p in channel.sent if m == "$node_upgrade"]

    async def test_up_to_date_node_no_proposal(self, wired_hub, monkeypatch):
        import llmpod

        channel = await self._run_hello(wired_hub, monkeypatch, info_extra={"version": llmpod.__version__})
        assert not [p for m, p in channel.sent if m == "$node_upgrade"]

    async def test_upgrade_result_notification_audited(self, wired_hub, monkeypatch):
        """$node_upgrade_result 落系统审计（write_audit 被调用，参数形状正确）。"""
        hub, channel, _buf = wired_hub
        calls: list[dict] = []

        async def _fake_write_audit(**kw):
            calls.append(kw)

        from llmpod.services import audit_service

        monkeypatch.setattr(audit_service, "write_audit", _fake_write_audit)
        handler = channel.notification_handlers.get("$node_upgrade_result")
        assert handler is not None
        await handler({"node_id": "node-x", "result": "success", "target_version": "9.9.9"})
        assert calls and calls[0]["action"] == "upgrade"
        assert calls[0]["actor_type"] == "system"
        assert calls[0]["resource_type"] == "node"
        assert calls[0]["result"] == "success"
        # 身份取自通道（channel.node_id），不是帧内自报
        assert calls[0]["resource_id"] == "node-x"


class TestUpgradeResultAuth:
    """$node_upgrade_result 只信已注册通道身份：通知绑定在通道建立时就生效，
    注册前的任意连接可发帧伪造节点身份刷审计——帧内自报 node_id 不可信。"""

    async def _audit_spy(self, monkeypatch):
        calls: list[dict] = []

        async def _fake_write_audit(**kw):
            calls.append(kw)

        from llmpod.services import audit_service

        monkeypatch.setattr(audit_service, "write_audit", _fake_write_audit)
        return calls

    async def test_unregistered_channel_dropped(self, wired_hub, monkeypatch):
        hub, channel, _buf = wired_hub
        calls = await self._audit_spy(monkeypatch)
        channel.node_id = None  # $hello 校验通过前 close/发帧的任意连接
        handler = channel.notification_handlers.get("$node_upgrade_result")
        await handler({"node_id": "node-x", "result": "failed", "detail": "forged"})
        assert calls == []

    async def test_claimed_id_mismatch_dropped(self, wired_hub, monkeypatch):
        hub, channel, _buf = wired_hub  # channel.node_id == "node-x"
        calls = await self._audit_spy(monkeypatch)
        handler = channel.notification_handlers.get("$node_upgrade_result")
        await handler({"node_id": "node-victim", "result": "failed", "detail": "forged"})
        assert calls == []


class TestOnCloseUntrack:
    """通道 untrack 由框架侧 TunnelServer.serve_channel 无条件兜底——不依赖业务
    on_close 是否被覆盖（被拒握手通道 node_id 恒空、on_close 早退也不泄漏）。"""

    async def test_serve_channel_untracks_on_close(self):
        from llmpod.tunnel.core.server import TunnelServer

        server = TunnelServer()
        ch = type("Ch", (), {"node_id": None})()  # 被拒握手形态：注册前关闭、node_id 恒空

        async def _wait_closed():
            return None

        ch.wait_closed = _wait_closed
        server._channels.append(ch)
        await server.serve_channel(ch)
        assert server._channels == []

    async def test_serve_channel_untracks_even_if_close_raises(self, monkeypatch):
        """wait_closed 抛错（关闭路径异常）也走 finally 兜底 untrack。"""
        from llmpod.tunnel.core.server import TunnelServer

        server = TunnelServer()
        calls: list = []
        monkeypatch.setattr(server, "untrack", lambda c: calls.append(c))

        class _Ch:
            async def wait_closed(self):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await server.serve_channel(_Ch())
        assert calls

    async def test_registered_node_close_business_cleanup_still_runs(self, wired_hub, monkeypatch):
        """untrack 收归框架后，业务 on_close 的节点清理（hub 移除/缓冲清空）原样。"""
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)
        monkeypatch.setattr(bootstrap_mod, "mark_node_offline", _noop_mark_node_offline)
        hub, channel, buf = wired_hub
        buf.update("node-x", [_gpu("GPU-1")])
        await channel.on_close()
        assert hub.get("node-x") is None
        assert buf.get("node-x") == []


class TestHeartbeatRepropose:
    """心跳驱动的升级重提议：在线节点失败一次后不重连 hello 就停在旧版——
    心跳带版本补这一程；冷却内不重发、开关关不下发。"""

    def _wire(self, wired_hub, monkeypatch, *, auto_upgrade: bool):
        from llmpod.apps.manager import bootstrap as bootstrap_mod
        from llmpod.apps.manager import app as manager_app_mod
        from llmpod.config.settings import settings as global_settings

        hub, channel, _buf = wired_hub
        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)

        async def _touch(node_id, db, version=None):
            return None

        monkeypatch.setattr(bootstrap_mod, "touch_node_heartbeat", _touch)
        monkeypatch.setattr(manager_app_mod, "node_hub", hub)
        monkeypatch.setattr(global_settings, "node_auto_upgrade", auto_upgrade)
        monkeypatch.setattr(bootstrap_mod, "_UPGRADE_PROPOSE_AT", {})
        return channel

    def _proposals(self, channel):
        return [p for m, p in channel.sent if m == "$node_upgrade"]

    async def test_outdated_node_reproposed_after_cooldown(self, wired_hub, monkeypatch):
        import llmpod

        channel = self._wire(wired_hub, monkeypatch, auto_upgrade=True)
        await channel.on_heartbeat({"info": {"hostname": "h", "version": "0.0.1"}})
        first = self._proposals(channel)
        assert len(first) == 1 and first[0]["target_version"] == llmpod.__version__

        # 冷却内：下一跳不重发（提议每 10s 一跳只会刷屏，node 忙时也忽略）
        await channel.on_heartbeat({"info": {"hostname": "h", "version": "0.0.1"}})
        assert len(self._proposals(channel)) == 1

        # 冷却已过：再发一次
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        table = bootstrap_mod._UPGRADE_PROPOSE_AT
        table["node-x"] -= bootstrap_mod._UPGRADE_REPROPOSE_COOLDOWN + 1
        await channel.on_heartbeat({"info": {"hostname": "h", "version": "0.0.1"}})
        assert len(self._proposals(channel)) == 2

    async def test_switch_off_no_reproposal(self, wired_hub, monkeypatch):
        """node_auto_upgrade 关闭（生产默认）：心跳不触发任何提议。"""
        channel = self._wire(wired_hub, monkeypatch, auto_upgrade=False)
        await channel.on_heartbeat({"info": {"hostname": "h", "version": "0.0.1"}})
        assert self._proposals(channel) == []

    async def test_up_to_date_no_reproposal(self, wired_hub, monkeypatch):
        import llmpod

        channel = self._wire(wired_hub, monkeypatch, auto_upgrade=True)
        await channel.on_heartbeat({"info": {"hostname": "h", "version": llmpod.__version__}})
        assert self._proposals(channel) == []


class TestDuplicateKickTiming:
    """升级场景 duplicate：旧容器还活着 + 新容器注册，on_close 不得误标 offline。"""

    async def test_register_before_kick_keeps_node_online(self, wired_hub, monkeypatch):
        import llmpod
        from llmpod.apps.manager import bootstrap as bootstrap_mod

        hub, old_channel, _buf = wired_hub

        offline_calls: list[str] = []

        async def _mark_offline(node_id, db):
            offline_calls.append(node_id)

        async def _ok_cluster(cid, db):
            return True

        async def _reg(node_id, cluster_id, info, db, resolve_ip=None):
            from llmpod.services.node_service import Registration

            return Registration(node_id="node-x", is_first=False)

        async def _touch(node_id, db, version=None):
            return None

        async def _sync(node_id, gpus, db):
            return None

        monkeypatch.setattr(bootstrap_mod, "get_db_context", _fake_db_ctx)
        monkeypatch.setattr(bootstrap_mod, "mark_node_offline", _mark_offline)
        monkeypatch.setattr(bootstrap_mod, "validate_cluster", _ok_cluster)
        monkeypatch.setattr(bootstrap_mod, "register_node", _reg)
        monkeypatch.setattr(bootstrap_mod, "touch_node_heartbeat", _touch)
        monkeypatch.setattr(bootstrap_mod, "sync_gpus", _sync)

        # 新容器 channel 注册（模拟升级替换后新容器 $hello）
        new_channel = _FakeChannel(node_id="node-x")
        lifespan = _FakeLifespan()
        lifespan.node_token = "t"
        lifespan.settings = type("S", (), {"node_heartbeat_interval": 10})()
        bootstrap_mod.setup_callbacks(new_channel, hub, lifespan)

        await new_channel.on_hello(
            {
                "node_id": "node-x",
                "token": "t",
                "cluster_id": 1,
                "info": {"hostname": "h", "version": llmpod.__version__},
            }
        )

        # 关键断言：新 channel 接管 hub；旧 channel 被踢（close 触发 on_close）；
        # 但 on_close 守卫（hub 里已是新 channel）拦住了 mark_node_offline
        assert hub.get("node-x").channel is new_channel
        assert old_channel.closed is True
        assert offline_calls == []
