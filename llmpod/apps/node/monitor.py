"""容器运行期 liveness 监测（Node 侧，周期扫描 + 上报 CRASHED）。

START_INSTANCE 执行器只在启动期探活，READY 之后容器死活无人盯——本模块补运行期
监测：每 ``scan_interval`` 秒 list 托管容器（``llmpod.managed=true``），list 摘要
state 为 running 的直接判 healthy（docker 语义 State.Status=="running" ⟹
Running=true 且 Restarting=false，无需再 inspect），其余逐容器 inspect，识别
「曾 healthy 后异常退出 / 重启循环 / 被 docker rm 删除」，经
``ManagerLink.report_instance_status`` 上报 ``CRASHED``，由 Manager 侧恢复循环
（``reconcile_runtime_instances``）自动重启。

状态机内存态（以 instance_id 为键的 ``_Tracked`` 记录，受 TTL / 保留窗口约束，
内存有界）：
- ``last_known``：上次分类（healthy/restarting/down），判定迁移用基线；
- ``latched``：曾 healthy 迁出后锁定的「可上报资格」——healthy 恢复即清除。资格若
  只靠 ``last_known`` 基线，延迟上报（restarting grace）会被「从未见过 healthy」
  规则永久跳过，故显式锁定；
- ``reported``：最近上报时间戳，TTL 内去重 / 到期重试（覆盖上报被 Manager 侧
  守卫拒收后实例状态随后变化为 READY 的竞态）；
- ``restarting_since``：restarting grace 起点（daemon 自愈窗口，超时才算僵死循环）；
- ``gone_since``：容器从 daemon 消失（docker rm）的起始时刻（保留窗口内可上报）；
- ``never_healthy_since``：「从未见过 healthy」的观察起点。

「从未见过 healthy」的容器有观察期（``NEVER_HEALTHY_GRACE``）：期内不上报（避开启动
窗口——task reconcile 正在拉容器，此时上报也会被 Manager 侧守卫拒收）；超期仍非
healthy 则照报——node 重启首扫撞上已崩容器时，实例在 Manager 侧仍 READY，历史
START task 已终态不会被重新驱动，本通道是唯一自愈路径。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from llmpod.apps.node.exceptions import ContainerGone, TaskExecutionError
from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.manager_link import ManagerLink
from llmpod.config.constants import InstanceStatus
from llmpod.tunnel.schemas import ContainerState, ContainerSummary, InstanceStatusPayload
from llmpod.utils.docker_naming import LABEL_DEPLOYMENT_ID, LABEL_INSTANCE_ID, LABEL_MANAGED

log = logging.getLogger("llmpod.apps.node.monitor")

# 上报去重 TTL（秒）：期内同实例不重报；到期后仍非 healthy 可重报（拒收竞态自愈）
REPORT_TTL = 60.0
# restarting grace（秒）：daemon 自愈窗口，超时仍 restarting 视为僵死重启循环
RESTARTING_GRACE = 10.0
# 容器消失（docker rm）保留窗口（秒）：过窗静默清理（竞态早已过去，收尾内存）
GONE_RETENTION = 600.0
# 「从未见过 healthy」的观察期（秒）：超期仍非 healthy 则视为崩溃上报。覆盖 node 重启
# 首扫撞已崩容器的盲区——实例在 Manager 侧仍 READY 且历史 START task 已终态，无人驱动
NEVER_HEALTHY_GRACE = 120.0

# 分类结果
_HEALTHY = "healthy"
_RESTARTING = "restarting"
_DOWN = "down"

# list 摘要 state=="running" 时无需 inspect 的判定替身（classify 对它必返 healthy：
# Running=true、Restarting=false）
_RUNNING_STATE = ContainerState(Status="running", Running=True)


def classify(state: ContainerState) -> str:
    """inspect State → healthy / restarting / down。

    ``Restarting`` 是 ``restart_policy=on-failure`` 下 daemon 自愈中（当前全 adapter 均
    为 no，纯防御分支，但语义保持正确）；``RestartCount`` 历史累计不参与判定——daemon
    自愈成功的容器计数永不清零，按它判会反复误杀健康容器（与启动期 gate 的差异：
    gate 在启动窗口内，RestartCount>0 即崩溃循环成立）。其余（exited/created/dead/
    removing/paused）均 down。
    """
    if bool(state.Restarting):
        return _RESTARTING
    if bool(state.Running):
        return _HEALTHY
    return _DOWN


@dataclass
class _Tracked:
    """单实例的监测状态机内存态（时间戳均为 time.monotonic）。"""

    last_known: str | None = None
    latched: bool = False
    reported: float | None = None
    # 上报协程在途标记：往返可能超过扫描间隔，去重不能只看 reported（ack 后才记）
    in_flight: bool = False
    restarting_since: float | None = None
    gone_since: float | None = None
    never_healthy_since: float | None = None
    # 首次上报尚未成功（reported 恒 None）时的失败节流起点：防持续失败退化成
    # 每轮扫描（3s）重试；成功上报后不再使用
    fail_since: float | None = None


@dataclass
class _TrackedMap:
    """instance_id → _Tracked（缺键自动建；集中封装六个平行字段的存取）。"""

    _items: dict[str, _Tracked] = field(default_factory=dict)

    def get(self, inst_id: str) -> _Tracked:
        if inst_id not in self._items:
            self._items[inst_id] = _Tracked()
        return self._items[inst_id]

    def peek(self, inst_id: str) -> _Tracked | None:
        return self._items.get(inst_id)

    def discard(self, inst_id: str) -> None:
        self._items.pop(inst_id, None)

    def keys(self) -> set[str]:
        return set(self._items)


class ContainerLivenessMonitor:
    """周期扫描托管容器 + 异常上报（注入 deployer 与 ManagerLink）。"""

    def __init__(
        self,
        deployer: Deployer,
        manager_link: ManagerLink,
        scan_interval: float = 3.0,
    ):
        self._deployer = deployer
        self._manager_link = manager_link
        self._scan_interval = scan_interval
        self._tracked = _TrackedMap()
        # ensure_future 的 Task 强引用（事件循环仅持弱引用，防上报协程被 GC 静默消失）
        self._pending_reports: set[asyncio.Task] = set()

    async def run(self) -> None:
        """后台循环；单轮异常仅记日志（对齐 NodeAgent.run 容错，不致命）。"""
        while True:
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("liveness scan 异常（跳过本轮）: %s", e)
            await asyncio.sleep(self._scan_interval)

    async def _scan(self) -> None:
        containers = await self._deployer.list(label_filter=[f"{LABEL_MANAGED}=true"], all=True)
        seen: set[str] = set()
        for summary in containers:
            inst_id = self._instance_id(summary)
            if not inst_id:
                continue  # 缺 instance 标签的托管容器（非推理实例）不参与
            seen.add(inst_id)
            if summary.state == "running":
                # list 摘要的 State.Status=="running" ⟹ Running=true 且 Restarting=false，
                # classify 必返 healthy——稳态跳过 inspect（每轮 0 次 daemon 往返）
                self._on_present(inst_id, summary, _RUNNING_STATE)
                continue
            state = await self._inspect_state(summary)
            if state is None:
                continue  # inspect 失败（NotFound→gone 由下轮 list 收敛；daemon 抖动）不误报
            self._on_present(inst_id, summary, state)

        for inst_id in self._tracked.keys() - seen:
            self._on_gone(inst_id)

    # ---- 单容器状态机 ----

    def _eligible(self, t: _Tracked, now: float) -> bool:
        """异常容器是否可上报 CRASHED（present / gone 两分支共用）。

        资格来源：曾 healthy 迁出（latched），或从未 healthy 但超观察期（node 重启首扫
        撞已崩容器的唯一自愈路径——Manager 侧实例仍 READY 且历史 START task 已终态）。
        观察期内（启动窗口）不上报：task reconcile 正在拉容器，且 Manager 守卫也会拒收。
        """
        if t.latched or t.reported is not None:
            return True
        since = t.never_healthy_since
        if since is None:
            t.never_healthy_since = now
            return False
        if now - since < NEVER_HEALTHY_GRACE:
            return False
        t.latched = True  # 超期视为已崩溃，赋予上报资格（含 gone 分支的消失）
        return True

    def _on_present(self, inst_id: str, summary: ContainerSummary, state: ContainerState) -> None:
        t = self._tracked.get(inst_id)
        cat = classify(state)
        now = time.monotonic()
        if cat == _HEALTHY:
            # 恢复即重置全部资格/计时/去重：允许未来再次异常时重新上报
            t.latched = False
            t.reported = None
            t.restarting_since = None
            t.gone_since = None
            t.never_healthy_since = None
            t.last_known = _HEALTHY
            return

        # 回到 list 即清消失计时：gone→present→gone 的第二次消失从新窗口起算
        t.gone_since = None
        if t.last_known == _HEALTHY:
            t.latched = True  # healthy→非 healthy 迁移：锁定可上报资格
        t.last_known = cat

        if not self._eligible(t, now):
            return
        if t.in_flight or (t.reported is not None and now - t.reported < REPORT_TTL):
            return  # 上报在途，或本周期已上报且未到 TTL：去重

        if cat == _DOWN:
            self._spawn_report(inst_id, summary, f"容器异常退出（exit={state.ExitCode}）")
            return
        # restarting：grace 内为 daemon 自愈窗口，不报；超窗仍 restarting 才是僵死循环
        if t.restarting_since is None:
            t.restarting_since = now
        elif now - t.restarting_since > RESTARTING_GRACE:
            self._spawn_report(inst_id, summary, "容器重启循环（restarting）")

    def _on_gone(self, inst_id: str) -> None:
        """容器从 daemon 消失（docker rm / 部署级清理）分支。"""
        t = self._tracked.get(inst_id)
        now = time.monotonic()
        if t.last_known == _HEALTHY:
            t.latched = True
        if not self._eligible(t, now):
            return  # 观察期内的消失（启动/清理竞态）不误报
        if t.gone_since is None:
            t.gone_since = now
        elif now - t.gone_since > GONE_RETENTION:
            # 仅在至少一次上报成功后（reported 有时间戳）才收尾内存；从未上报成功不清理
            # ——通道离线跨过保留窗口时报告不得丢，直到送达或容器回到 list
            if t.reported is not None:
                self._tracked.discard(inst_id)
                return
        if t.in_flight or (t.reported is not None and now - t.reported < REPORT_TTL):
            return  # 上报在途，或已上报未到 TTL：去重；保留基线供 TTL 到期重试
        # 从未上报成功（reported 恒 None）的持续失败会退化成每轮扫描重试——
        # 按失败时刻起算同等 TTL 节流，避免通道长时间离线时按 3s 空转
        if t.reported is None:
            if t.fail_since is None:
                t.fail_since = now
                self._spawn_report(inst_id, None, "容器从 daemon 消失")
                return
            if now - t.fail_since < REPORT_TTL:
                return
            t.fail_since = now
        self._spawn_report(inst_id, None, "容器从 daemon 消失")

    # ---- 上报 ----

    def _spawn_report(self, inst_id: str, summary: ContainerSummary | None, message: str) -> None:
        """调起一次上报协程（持强引用防 GC；done 后自动移出集合）。

        发起即置 ``in_flight``：上报往返可能超过扫描间隔，只看 ``reported``
        （ack 后才记时间戳）会让下一轮扫描重复起上报协程。
        """
        t = self._tracked.peek(inst_id)
        if t is not None:
            t.in_flight = True
        task = asyncio.ensure_future(self._report(inst_id, summary, message))
        self._pending_reports.add(task)
        task.add_done_callback(self._pending_reports.discard)

    async def _report(self, inst_id: str, summary: ContainerSummary | None, message: str) -> None:
        """上报 CRASHED；ack（含 Manager 守卫拒收的 ok=True）即记时间戳，None 下轮重试。

        gone 分支容器已不在 list（labels 随之丢失），``deployment_id`` 留空——
        Manager 侧按 ``instance_id`` 定位实例行，deploy_log 投影的部署定位在
        接受分支里用实例行自身的 ``deployment_id``（见 task_ingest CRASHED 分支）。
        """
        labels = summary.labels if summary else {}
        evt = InstanceStatusPayload(
            deployment_id=labels.get(LABEL_DEPLOYMENT_ID) or "",
            instance_id=inst_id,
            node_id=self._manager_link.node_id,
            status=InstanceStatus.CRASHED,
            container_id=summary.id if summary else None,
            message=message,
        )
        ack = await self._manager_link.report_instance_status(evt)
        t = self._tracked.peek(inst_id)
        if t is not None:
            t.in_flight = False
            if ack is not None:
                t.reported = time.monotonic()
                t.fail_since = None  # 上报成功：失败节流终止，回到 TTL 口径
        if ack is None:
            log.debug("liveness CRASHED 上报失败（下轮重试）instance=%s", inst_id[:8])

    # ---- helpers ----

    @staticmethod
    def _instance_id(summary: ContainerSummary) -> str | None:
        return summary.labels.get(LABEL_INSTANCE_ID) or None

    async def _inspect_state(self, summary: ContainerSummary) -> ContainerState | None:
        """inspect 单容器；NotFound（容器消失）与 daemon 抖动均返回 None 跳过本轮。

        NotFound 交给下一轮 list 的 gone 分支处理（避免在 present 分支误判 down）；
        其他异常对齐 readiness 循环「inspect 失败 ≠ 容器退出」的既有做法。
        """
        try:
            return await self._deployer.inspect(summary.name)
        except (TaskExecutionError, ContainerGone) as e:
            log.debug("liveness inspect %s 失败（跳过本轮）: %s", summary.name, e)
            return None
