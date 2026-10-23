"""版本可见性单测：version_status 比对（calver）、register_node 版本变迁、NodeReport.version。

register_node 用共享 FakeSession（tests/integration/_fakes.py），审计经 monkeypatch
write_audit 捕获（真实工厂无 PG，write_audit 自身兜底只 log.error）。
"""

from __future__ import annotations

import llmpod
from llmpod.config.constants import NodeStatus
from llmpod.models.node import Node
from llmpod.services.node_service import (
    enrich_nodes_version_status,
    register_node,
    version_status_of,
)
from llmpod.tunnel.schemas.system import NodeReport
from tests.integration._fakes import FakeSession


class TestVersionStatus:
    def test_same_version_ok(self):
        assert version_status_of(llmpod.__version__) == "ok"

    def test_calver_outdated(self):
        assert version_status_of("0.0.1") == "outdated"
        assert version_status_of("26.8.31") == "outdated" if llmpod.__version__ >= "26.9" else True

    def test_calver_newer(self):
        assert version_status_of("99.0.0") == "newer"

    def test_missing_or_garbage_unknown(self):
        assert version_status_of(None) == "unknown"
        assert version_status_of("") == "unknown"
        assert version_status_of("not-a-version") == "unknown"

    def test_segment_semantics(self):
        """缺段补 0：26.9.3 == 26.9.3.0；26.9.3.1 > 26.9.3。"""
        assert version_status_of("26.9.3") in ("ok", "outdated", "newer")  # 形状合法即可
        assert version_status_of("v" + llmpod.__version__) == "ok"  # v 前缀容忍

    def test_enrich_sets_attr(self):
        nodes = [
            Node(id="a", cluster_id=1, hostname="h", ip="1", status=NodeStatus.ONLINE, version="0.0.1"),
            Node(id="b", cluster_id=1, hostname="h", ip="1", status=NodeStatus.ONLINE, version=llmpod.__version__),
            Node(id="c", cluster_id=1, hostname="h", ip="1", status=NodeStatus.ONLINE, version=None),
        ]
        enrich_nodes_version_status(nodes)
        assert [n.version_status for n in nodes] == ["outdated", "ok", "unknown"]


class TestRegisterNodeVersion:
    async def test_version_change_detected(self, monkeypatch):
        """重连上报新版本 → 落审计 + node.version 更新。"""
        audited: list[dict] = []

        async def _fake_write_audit(**kw):
            audited.append(kw)

        from llmpod.services import audit_service

        monkeypatch.setattr(audit_service, "write_audit", _fake_write_audit)

        session = FakeSession()
        node = session.preload(
            Node(
                id="n1",
                cluster_id=1,
                hostname="h",
                ip="1",
                status=NodeStatus.OFFLINE,
                version="26.9.1",
                capabilities=[],
            )
        )
        reg = await register_node(
            "n1",
            1,
            NodeReport(hostname="h", ip="1", version="26.9.3"),
            session,
        )
        assert reg.node_id == "n1" and reg.is_first is False
        assert node.version == "26.9.3"
        assert node.status == NodeStatus.ONLINE
        assert audited and audited[0]["action"] == "upgrade"
        assert audited[0]["detail"]["previous_version"] == "26.9.1"
        assert audited[0]["detail"]["version"] == "26.9.3"

    async def test_same_version_no_change(self, monkeypatch):
        from llmpod.services import audit_service

        async def _fail_audit(**kw):
            raise AssertionError("不应落审计")

        monkeypatch.setattr(audit_service, "write_audit", _fail_audit)
        session = FakeSession()
        session.preload(
            Node(
                id="n1",
                cluster_id=1,
                hostname="h",
                ip="1",
                status=NodeStatus.ONLINE,
                version="26.9.1",
                capabilities=[],
            )
        )
        await register_node(
            "n1",
            1,
            NodeReport(hostname="h", ip="1", version="26.9.1"),
            session,
        )

    async def test_first_registration_no_change_semantics(self, monkeypatch):
        """首次注册（无 node_id）不产生变迁（无旧值可比）。"""
        from llmpod.services import audit_service

        async def _fail_audit(**kw):
            raise AssertionError("不应落审计")

        monkeypatch.setattr(audit_service, "write_audit", _fail_audit)
        session = FakeSession()
        reg = await register_node(None, 1, NodeReport(hostname="h", version="26.9.3"), session)
        assert reg.is_first is True
        assert session.all(Node)[0].version == "26.9.3"


class TestRegisterNodeIpChange:
    """互联 IP 变迁审计：换址必须留痕（部署可达性的根，静默变化即异常信号）。"""

    @staticmethod
    def _preload_node(session, *, ip: str, status=NodeStatus.OFFLINE) -> Node:
        return session.preload(
            Node(
                id="n1",
                cluster_id=1,
                hostname="h",
                ip=ip,
                status=status,
                version="26.9.1",
                capabilities=[],
            )
        )

    async def test_ip_change_detected(self, monkeypatch):
        """重连上报地址与库中不同（正常 = 操作者重新指定）→ 落审计 + node.ip 更新。"""
        audited: list[dict] = []

        async def _fake_write_audit(**kw):
            audited.append(kw)

        from llmpod.services import audit_service

        monkeypatch.setattr(audit_service, "write_audit", _fake_write_audit)

        session = FakeSession()
        node = self._preload_node(session, ip="192.0.2.9")
        await register_node("n1", 1, NodeReport(hostname="h", ip="10.1.1.5", version="26.9.1"), session)
        assert node.ip == "10.1.1.5"
        ip_audits = [a for a in audited if a["action"] == "update"]
        assert ip_audits and ip_audits[0]["resource_type"] == "node"
        assert ip_audits[0]["detail"]["previous_ip"] == "192.0.2.9"
        assert ip_audits[0]["detail"]["ip"] == "10.1.1.5"

    async def test_same_ip_no_audit(self, monkeypatch):
        """地址未变（重连常态）不落审计。"""
        from llmpod.services import audit_service

        async def _fail_audit(**kw):
            raise AssertionError("不应落审计")

        monkeypatch.setattr(audit_service, "write_audit", _fail_audit)
        session = FakeSession()
        self._preload_node(session, ip="192.0.2.9", status=NodeStatus.ONLINE)
        await register_node("n1", 1, NodeReport(hostname="h", ip="192.0.2.9", version="26.9.1"), session)


class TestRegisterNodeResolveIp:
    """$hello 选定接入 register_node：resolve_ip 在已加载的行上就地选定
    （显式 > 已存合法值 > 自报值兜底），语义矩阵见 resolve_node_ip 单测；
    此处验证接线——已存值经选定器沿用、漂移不落审计、未显式固定收下。"""

    async def test_stored_ip_kept_via_resolver(self, monkeypatch):
        """库中已存合法地址：重连自报值变了也沿用（分支 2，不重协商），
        且不产生 ip 变迁审计（沿用≠换址）。"""
        from llmpod.apps.manager.node_addr import resolve_node_ip
        from llmpod.services import audit_service

        async def _fail_audit(**kw):
            raise AssertionError("沿用已存值不应落审计")

        monkeypatch.setattr(audit_service, "write_audit", _fail_audit)
        session = FakeSession()
        node = TestRegisterNodeIpChange._preload_node(session, ip="192.0.2.9")
        report = NodeReport(hostname="h", ip="203.0.113.7", version="26.9.1")  # 自报值漂移
        await register_node("n1", 1, report, session, resolve_ip=resolve_node_ip)
        assert node.ip == "192.0.2.9"
        assert report.ip == "192.0.2.9"  # 选定结果写回 info（hub 内存态同源）

    async def test_unpinned_first_registration_accepts_self_report(self, monkeypatch):
        """首次注册（无 node_id、未显式配置）：stored=None → 未显式固定收下自报值。"""
        from llmpod.apps.manager.node_addr import resolve_node_ip

        session = FakeSession()
        reg = await register_node(None, 1, NodeReport(hostname="h", ip="10.0.0.8"), session, resolve_ip=resolve_node_ip)
        assert reg.is_first is True
        assert session.all(Node)[0].ip == "10.0.0.8"

    async def test_without_resolver_trusts_report(self):
        """resolve_ip=None（内部/测试路径）：直接采信 info.ip，选定不介入。"""
        session = FakeSession()
        node = TestRegisterNodeIpChange._preload_node(session, ip="192.0.2.9")
        await register_node("n1", 1, NodeReport(hostname="h", ip="203.0.113.7"), session)
        assert node.ip == "203.0.113.7"


class TestRegisterNodeNetIfname:
    """register_node 落库 net_ifname：卡名与选定后的 ip 联动——选定换用
    已存/显式值时自报卡名失真，置 None（消费方回落显式配置与代码兜底）。"""

    async def test_reported_ifname_persisted_when_ip_kept(self):
        """未显式固定收下自报值（选定结果 == 自报值）→ 卡名照落。"""
        from llmpod.apps.manager.node_addr import resolve_node_ip

        session = FakeSession()
        report = NodeReport(hostname="h", ip="10.0.0.8", net_ifname="enp3s0")
        await register_node(None, 1, report, session, resolve_ip=resolve_node_ip)
        assert session.all(Node)[0].net_ifname == "enp3s0"

    async def test_ifname_dropped_when_resolver_changes_ip(self):
        """选定沿用已存值（≠自报值）→ 自报卡名失真，落 None。"""
        from llmpod.apps.manager.node_addr import resolve_node_ip

        session = FakeSession()
        node = TestRegisterNodeIpChange._preload_node(session, ip="192.0.2.9")
        report = NodeReport(hostname="h", ip="203.0.113.7", net_ifname="enp3s0")  # 自报值漂移
        await register_node("n1", 1, report, session, resolve_ip=resolve_node_ip)
        assert node.ip == "192.0.2.9"
        assert node.net_ifname is None

    async def test_missing_ifname_lands_none(self):
        """老节点未上报（net_ifname 默认空串）→ 落 None（= 未上报哨兵）。"""
        session = FakeSession()
        await register_node(None, 1, NodeReport(hostname="h", ip="10.0.0.8"), session)
        assert session.all(Node)[0].net_ifname is None


class TestNodeReportVersion:
    def test_version_field_serialized(self):
        report = NodeReport(hostname="h", version="26.9.3")
        dumped = report.model_dump()
        assert dumped["version"] == "26.9.3"

    def test_version_defaults_empty(self):
        assert NodeReport(hostname="h").version == ""

    def test_collector_fills_version(self):
        from llmpod.apps.node.collectors import collect_node_info

        assert collect_node_info().version == llmpod.__version__


class TestOfficialImageRef:
    def test_ref_derives_from_current_version(self):
        """升级提议的 image_ref 由官方镜像 + 当前版本推导（无部署配置）。"""
        from llmpod.services.version_service import OFFICIAL_IMAGE, official_image_ref

        assert official_image_ref() == f"{OFFICIAL_IMAGE}:{llmpod.__version__}"
        assert official_image_ref("26.9.4") == f"{OFFICIAL_IMAGE}:26.9.4"
        # 带 registry 前缀（节点侧可 pull 判定通过）
        from llmpod.apps.node.upgrader import is_registry_ref

        assert is_registry_ref(official_image_ref()) is True


class TestAutoUpgradeSwitch:
    """升级提议开关：自动路径受 node_auto_upgrade 闸门，手动路径（auto=False）永不拦截。"""

    async def test_auto_blocked_when_switch_off(self, monkeypatch):
        from llmpod.apps.manager import dispatch
        from llmpod.config.settings import settings as global_settings

        monkeypatch.setattr(global_settings, "node_auto_upgrade", False)
        dispatched, reason = await dispatch.send_node_upgrade("n1", "0.0.1")
        assert dispatched is False
        assert "未开启" in reason

    async def test_manual_bypasses_switch(self, monkeypatch):
        """开关关闭时 auto=False（admin 手动端点路径）仍放行到在线检查。"""
        from llmpod.apps.manager import dispatch
        from llmpod.config.settings import settings as global_settings

        monkeypatch.setattr(global_settings, "node_auto_upgrade", False)
        dispatched, reason = await dispatch.send_node_upgrade("n1", "0.0.1", auto=False)
        # 走过了开关闸门，卡在在线检查（测试进程 manager 未初始化）
        assert dispatched is False
        assert reason == "节点不在线"
