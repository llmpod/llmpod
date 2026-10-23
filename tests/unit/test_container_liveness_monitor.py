"""ContainerLivenessMonitor 单测：分类函数 + 状态机（崩溃/重启循环/被删/TTL 重报）。

fake Deployer/ManagerLink 注入，直接驱动 ``_scan`` 单轮，按上报记录断言。时间用
monkeypatch ``time.monotonic`` 推进（不真实 sleep）。容器退出/重启时须同步翻
``summary.state``（list 摘要态——稳态跳 inspect 的门）；``_flush`` 让
``ensure_future`` 调起的上报协程跑完。
"""

from __future__ import annotations

from llmpod.apps.node.monitor import (
    GONE_RETENTION,
    NEVER_HEALTHY_GRACE,
    REPORT_TTL,
    RESTARTING_GRACE,
    ContainerLivenessMonitor,
    classify,
)
from llmpod.tunnel.schemas import Ack, ContainerState, ContainerSummary


async def _flush():
    """让 ``ensure_future`` 调起的上报协程跑完（两跳：调度 + await link）。"""
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _summary(name="c1", inst_id="i1", dep_id="dep1", state="running"):
    return ContainerSummary(
        name=name,
        id=f"cid-{name}",
        state=state,
        labels={
            "llmpod.managed": "true",
            "llmpod.deployment-id": dep_id,
            "llmpod.instance-id": inst_id,
        },
    )


def _state(running=True, restarting=False, restart_count=0, exit_code=0):
    return ContainerState(
        Status="running" if running else "exited",
        Running=running,
        Restarting=restarting,
        RestartCount=restart_count,
        ExitCode=exit_code,
    )


class FakeDeployer:
    def __init__(self, containers=None, inspect_error=None):
        self.containers = list(containers or [])  # list[ContainerSummary]
        self.inspect_error = inspect_error
        self.list_filters = []

    async def list(self, label_filter=None, all=True):
        self.list_filters.append((label_filter, all))
        return list(self.containers)

    async def inspect(self, name):
        if self.inspect_error is not None:
            raise self.inspect_error
        return self._states[name]


class FakeLink:
    def __init__(self, ok=True):
        self.node_id = "n1"
        self.reports = []
        self.ok = ok

    async def report_instance_status(self, evt):
        self.reports.append(evt)
        return Ack(ok=self.ok) if self.ok else None


def _make(containers, states, link=None):
    d = FakeDeployer(containers)
    d._states = states
    return ContainerLivenessMonitor(d, link or FakeLink(), scan_interval=0.01), d


class TestClassify:
    def test_running_healthy(self):
        assert classify(_state(running=True)) == "healthy"

    def test_exited_down(self):
        assert classify(_state(running=False, exit_code=1)) == "down"

    def test_restarting_flag(self):
        assert classify(_state(running=True, restarting=True)) == "restarting"

    def test_restart_count(self):
        """RestartCount 是历史累计（daemon 自愈成功后永不清零）——Running 即 healthy，
        不按计数误杀曾自愈过的健康容器。"""
        assert classify(_state(running=True, restart_count=2)) == "healthy"

    def test_restarting_flag_with_count(self):
        assert classify(_state(running=True, restarting=True, restart_count=2)) == "restarting"


class TestHealthyToDown:
    async def test_healthy_to_down_reports(self):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        await mon._scan()  # 建基线 healthy
        assert link.reports == []

        d._states["c1"] = _state(running=False, exit_code=137)
        s.state = "exited"  # list 摘要随容器退出翻状态（稳态跳 inspect 的门）
        await mon._scan()
        await _flush()

        assert len(link.reports) == 1
        evt = link.reports[0]
        assert evt.status == "CRASHED"
        assert evt.instance_id == "i1"
        assert evt.deployment_id == "dep1"
        assert "137" in (evt.message or "")

    async def test_dedup_within_ttl_and_recheck_after(self, monkeypatch):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()  # healthy 基线

        d._states["c1"] = _state(running=False, exit_code=1)
        s.state = "exited"
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1

        # TTL 内重扫：不重报
        now += 10
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1

        # TTL 过期仍 down：重报（拒收竞态自愈）
        now += REPORT_TTL
        await mon._scan()
        await _flush()
        assert len(link.reports) == 2

    async def test_recovery_resets_reporting(self):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        await mon._scan()

        d._states["c1"] = _state(running=False, exit_code=1)
        s.state = "exited"
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1

        # 恢复 healthy：清 _reported，下次异常重新上报（不受旧 TTL 挤压）
        d._states["c1"] = _state(running=True)
        s.state = "running"
        await mon._scan()
        d._states["c1"] = _state(running=False, exit_code=2)
        s.state = "exited"
        await mon._scan()
        await _flush()
        assert len(link.reports) == 2

    async def test_never_seen_healthy_silent_within_grace(self, monkeypatch):
        """node 重启首扫撞上已崩容器（从未见过 healthy）→ 观察期内不上报
        （启动窗口交 task reconcile，双通道不打架）。"""
        link = FakeLink()
        s = _summary(state="exited")
        mon, d = _make([s], {"c1": _state(running=False)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()
        now += NEVER_HEALTHY_GRACE / 2
        await mon._scan()
        await _flush()
        assert link.reports == []

    async def test_never_seen_healthy_reports_after_grace(self, monkeypatch):
        """观察期超期仍非 healthy → 照报（Manager 侧实例仍 READY 且历史 task 已终态，
        本通道是唯一自愈路径——node 重启首扫盲区）。"""
        link = FakeLink()
        s = _summary(state="exited")
        mon, d = _make([s], {"c1": _state(running=False, exit_code=137)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()
        now += NEVER_HEALTHY_GRACE + 1
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1
        evt = link.reports[0]
        assert evt.status == "CRASHED"
        assert "137" in (evt.message or "")

    async def test_restarting_grace_then_report(self, monkeypatch):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()  # healthy 基线

        d._states["c1"] = _state(running=True, restarting=True)
        s.state = "restarting"
        await mon._scan()
        await _flush()
        assert link.reports == []  # grace 首轮只记起点

        now += RESTARTING_GRACE / 2
        await mon._scan()
        await _flush()
        assert link.reports == []  # grace 内不报

        now += RESTARTING_GRACE
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1
        assert "重启循环" in (link.reports[0].message or "")

    async def test_restarting_recovers_within_grace(self, monkeypatch):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()

        d._states["c1"] = _state(running=True, restarting=True)
        s.state = "restarting"
        await mon._scan()
        now += 2
        # daemon 自愈成功
        d._states["c1"] = _state(running=True)
        s.state = "running"
        await mon._scan()
        now += RESTARTING_GRACE * 2
        d._states["c1"] = _state(running=True, restarting=True)
        s.state = "restarting"
        await mon._scan()
        # grace 起点被 healthy 恢复清掉，重新起窗：本轮只记起点不报
        await _flush()
        assert link.reports == []


class TestGone:
    async def test_healthy_then_removed_reports(self):
        """曾 healthy 后从 list 消失（docker rm）→ 报 CRASHED（盲区修复）。"""
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        await mon._scan()

        d.containers = []  # docker rm
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1
        evt = link.reports[0]
        assert evt.status == "CRASHED"
        assert "daemon" in (evt.message or "")

    async def test_down_then_removed_silent_within_grace(self, monkeypatch):
        """本就 down 的容器被 rm → 观察期内静默不误报。"""
        link = FakeLink()
        s = _summary(state="exited")
        mon, d = _make([s], {"c1": _state(running=False)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()
        await _flush()
        assert link.reports == []

        d.containers = []
        now += NEVER_HEALTHY_GRACE / 2
        await mon._scan()
        await _flush()
        assert link.reports == []

    async def test_never_healthy_removed_reports_after_grace(self, monkeypatch):
        """从未 healthy 的容器消失且超观察期 → 报 CRASHED（同 present 分支的盲区自愈）。"""
        link = FakeLink()
        s = _summary(state="exited")
        mon, d = _make([s], {"c1": _state(running=False)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()

        d.containers = []
        now += NEVER_HEALTHY_GRACE + 1
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1
        assert "daemon" in (link.reports[0].message or "")

    async def test_gone_retry_after_ttl_and_cleanup_after_retention(self, monkeypatch):
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()

        d.containers = []
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1

        # TTL 内不重报；TTL 过期重报（被拒收后的竞态自愈）
        now += REPORT_TTL + 1
        await mon._scan()
        await _flush()
        assert len(link.reports) == 2

        # 超过 gone 保留窗口且已成功上报过 → 静默清理收尾
        now += GONE_RETENTION + 1
        await mon._scan()
        assert mon._tracked.peek("i1") is None

    async def test_gone_retention_keeps_state_while_delivery_failing(self, monkeypatch):
        """通道离线（ack=None）跨过保留窗口：不清理，报告持续重试直到送达。"""
        link = FakeLink(ok=False)
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()

        d.containers = []
        # 连续多轮全部投递失败（远超保留窗口）
        for _ in range(3):
            now += GONE_RETENTION
            await mon._scan()
            await _flush()
        t = mon._tracked.peek("i1")
        assert t is not None and t.latched  # 状态保留，未清理

        # 通道恢复（换成 ok 的 link）→ 下一轮即成功上报
        link.ok = True
        now += REPORT_TTL + 1
        await mon._scan()
        await _flush()
        assert len(link.reports) >= 1
        assert link.reports[-1].status == "CRASHED"

    async def test_gone_present_gone_restarts_window(self, monkeypatch):
        """gone→回 list（down）→再 gone：消失计时从新窗口起算，第二次消失仍能上报。"""
        link = FakeLink()
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        now = 1000.0
        monkeypatch.setattr("llmpod.apps.node.monitor.time.monotonic", lambda: now)
        await mon._scan()

        d.containers = []  # 第一次消失
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1

        now += GONE_RETENTION - 5  # 窗口内回来（down 态：资格仍在，靠 reported TTL 去重）
        d.containers = [s]
        d._states["c1"] = _state(running=False, exit_code=1)
        s.state = "exited"
        await mon._scan()
        await _flush()
        # down 在 list 且 TTL 已过期 → 重报一次（非首次）
        assert len(link.reports) == 2

        now += 5
        d.containers = []  # 再次消失：旧 gone_since 已被 present 分支清掉
        now += REPORT_TTL + 1
        await mon._scan()
        await _flush()
        assert len(link.reports) == 3
        assert "daemon" in (link.reports[2].message or "")


class TestReportFailure:
    async def test_none_ack_no_timestamp_retry_next_round(self):
        """channel 离线（ack=None）→ 不记 reported 时间戳，下轮重试。"""
        link = FakeLink(ok=False)  # 返回 None
        s = _summary()
        mon, d = _make([s], {"c1": _state(running=True)}, link)
        await mon._scan()

        d._states["c1"] = _state(running=False, exit_code=1)
        s.state = "exited"
        await mon._scan()
        await _flush()
        assert len(link.reports) == 1
        t = mon._tracked.peek("i1")
        assert t is not None and t.reported is None  # 未记时间戳

        await mon._scan()  # 下轮即重试（无 TTL 挡）
        await _flush()
        assert len(link.reports) == 2


class TestInspectError:
    async def test_inspect_failure_skips_round(self):
        """inspect 抛异常（daemon 抖动/NotFound）→ 跳过本轮不误报。"""
        from llmpod.apps.node.exceptions import TaskExecutionError

        link = FakeLink()
        s = _summary(state="exited")  # 非 running 才走 inspect 路径
        mon = ContainerLivenessMonitor(
            FakeDeployer([s]),
            link,
            scan_interval=0.01,
        )
        mon._deployer._states = {"c1": _state(running=True)}
        await mon._scan()  # inspect 判 healthy，建基线
        await _flush()

        mon._deployer.inspect_error = TaskExecutionError("docker inspect failed")
        await mon._scan()
        await _flush()
        assert link.reports == []
        t = mon._tracked.peek("i1")
        assert t is not None and t.last_known == "healthy"  # 基线未被误更新


class TestSteadyStateFastPath:
    async def test_running_summary_skips_inspect(self):
        """list 摘要 state=="running" 直接判 healthy，不 inspect（稳态零 daemon 往返）。"""
        link = FakeLink()
        s = _summary()  # state="running"
        d = FakeDeployer([s])
        d._states = {}
        mon = ContainerLivenessMonitor(d, link, scan_interval=0.01)
        await mon._scan()  # 无 _states：inspect 被调会 KeyError

        t = mon._tracked.peek("i1")
        assert t is not None and t.last_known == "healthy"

    async def test_exited_summary_inspects(self):
        """非 running 摘要走 inspect 取详情（exit code 进上报）。"""
        link = FakeLink()
        s = _summary(state="exited")
        mon, d = _make([s], {"c1": _state(running=False, exit_code=3)}, link)
        now = 1000.0
        import llmpod.apps.node.monitor as mon_mod

        real = mon_mod.time.monotonic
        try:
            mon_mod.time.monotonic = lambda: now
            await mon._scan()
            now += NEVER_HEALTHY_GRACE + 1
            await mon._scan()
            await _flush()
        finally:
            mon_mod.time.monotonic = real
        assert len(link.reports) == 1
        assert "exit=3" in (link.reports[0].message or "")


class TestUnlabeled:
    async def test_missing_instance_label_skipped(self):
        """缺 instance 标签的托管容器不参与状态机。"""
        link = FakeLink()
        s = ContainerSummary(name="c2", id="x", state="exited", labels={"llmpod.managed": "true"})
        mon, d = _make([s], {}, link)
        await mon._scan()
        assert mon._tracked.keys() == set()
        assert link.reports == []
