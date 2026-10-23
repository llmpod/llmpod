"""StopInstanceExecutor 单测：纯 stop（绝不删容器，fake Deployer）。

STOP_INSTANCE 执行器只停不删——容器删除是 delete 部署独有职责（cleanup_deployment_containers
label 扫除），不经本执行器。覆盖：缺 container_name / 纯 stop 全链 / ContainerGone 幂等成功 /
daemon 故障保守 FAILED / reconcile（确认消失=达成、已停=达成、运行中=None、inspect 异常=None）。
"""

from __future__ import annotations

import asyncio

from llmpod.apps.node.exceptions import ContainerGone
from llmpod.apps.node.executors.stop_instance import StopInstanceExecutor
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import ContainerState


class FakeDeployer:
    """可编程 Deployer 桩：记录调用；可注入 stop 异常与 inspect 结果。"""

    def __init__(self, inspect_ok=True, inspect_running=True, stop_error=None, inspect_error=None):
        self.inspect_running = inspect_running
        self.stop_error = stop_error
        self.inspect_error = inspect_error
        self.stop_calls = []
        self.remove_calls = []  # 断言用：执行器永不调 remove

    async def stop(self, name, timeout=10):
        self.stop_calls.append((name, timeout))
        if self.stop_error:
            raise self.stop_error

    async def remove(self, name, force=False):
        self.remove_calls.append((name, force))  # 不应被调用

    async def inspect(self, name):
        if self.inspect_error is not None:
            raise self.inspect_error
        return ContainerState(
            Status="running" if self.inspect_running else "exited",
            Running=self.inspect_running,
        )


def _task(container_name="llmpod-dep-i1", stop_timeout=10):
    return LocalTask(
        task_id="t1",
        type="STOP_INSTANCE",
        resource_type="INSTANCE",
        resource_key="instance:dep:0:stop",
        node_id="n1",
        status="RUNNING",
        payload={
            "deployment_id": "dep",
            "instance_id": "i1",
            "rank": 0,
            "container_name": container_name,
            "stop_timeout": stop_timeout,
        },
    )


def _collect():
    events: list[dict] = []

    async def report(
        status, progress=0, *, message=None, instance_status=None, container_id=None, endpoint=None, extra=None
    ):
        # 镜像 NodeAgent._emit 的合并语义：结构化标记并入 extra，便于断言
        merged = dict(extra or {})
        if instance_status is not None:
            merged["instance_status"] = instance_status
        if container_id is not None:
            merged["container_id"] = container_id
        if endpoint is not None:
            merged["endpoint"] = endpoint
        events.append({"status": status, "progress": progress, "message": message, "extra": merged or None})

    return events, report


class TestExecute:
    async def test_missing_container_name_failed(self):
        ex = StopInstanceExecutor(FakeDeployer())
        events, report = _collect()
        await ex.execute(_task(container_name=""), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED" and "缺少" in events[-1]["message"]

    async def test_stop_only_never_removes(self):
        """纯 stop：只调 stop，永不 remove，容器保留。"""
        dep = FakeDeployer()
        ex = StopInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())

        assert dep.stop_calls == [("llmpod-dep-i1", 10)]
        assert dep.remove_calls == []  # 执行器不删容器
        statuses = [e["status"] for e in events]
        assert statuses == ["RUNNING", "SUCCESS"]
        assert events[0]["extra"]["instance_status"] == "STOPPING"
        assert events[1]["extra"]["instance_status"] == "STOPPED"

    async def test_stop_gone_success(self):
        """stop 报 ContainerGone（容器已不在）→ 幂等 SUCCESS，不 remove。"""
        dep = FakeDeployer(stop_error=ContainerGone("容器不存在"))
        ex = StopInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "SUCCESS"
        assert events[-1]["extra"]["instance_status"] == "STOPPED"
        assert dep.remove_calls == []

    async def test_stop_daemon_error_failed(self):
        """stop 报 daemon 故障（非 NotFound）→ 保守 FAILED：容器状态未知，不得误判
        STOPPED——否则 Manager 释放 VRAM 后同卡双容器。"""
        dep = FakeDeployer(stop_error=RuntimeError("daemon timeout"))
        ex = StopInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "容器停止失败" in events[-1]["message"]
        assert dep.remove_calls == []

    async def test_cancel_before_stop_skips(self):
        """入口已取消 → CANCELLED，不启动停止动作（协作取消契约，与其他执行器一致）。"""
        dep = FakeDeployer()
        cancel = asyncio.Event()
        cancel.set()
        ex = StopInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, cancel)
        assert events[-1]["status"] == "CANCELLED"
        assert dep.stop_calls == []  # 取消优先，未执行停止

    async def test_cancel_during_stop_cancelled_not_success(self):
        """stop 进行期间到达的取消 → CANCELLED 而非 SUCCESS：SUCCESS 会打断 attempt
        失败链，把被取消的任务误记成功。容器此刻已停，状态通道仍报 STOPPED。"""
        cancel = asyncio.Event()

        class _SetCancelOnStop(FakeDeployer):
            async def stop(self, name, timeout=10):
                await super().stop(name, timeout)
                cancel.set()  # stop 返回时取消已到达

        ex = StopInstanceExecutor(_SetCancelOnStop())
        events, report = _collect()
        await ex.execute(_task(), None, report, cancel)
        assert events[-1]["status"] == "CANCELLED"
        assert events[-1]["extra"]["instance_status"] == "STOPPED"


class TestReconcile:
    async def test_container_gone_returns_success(self):
        """重启探测：容器确认消失（ContainerGone）→ SUCCESS(+STOPPED)。"""
        ex = StopInstanceExecutor(FakeDeployer(inspect_error=ContainerGone("容器不存在")))
        r = await ex.reconcile(_task())
        assert r is not None and r.status == "SUCCESS"
        assert r.instance_status == "STOPPED"

    async def test_inspect_daemon_error_none(self):
        """重启探测：inspect daemon 故障 → None（不臆断容器已停，等重新下发）。"""
        ex = StopInstanceExecutor(FakeDeployer(inspect_error=RuntimeError("daemon timeout")))
        assert await ex.reconcile(_task()) is None

    async def test_stopped_container_success(self):
        """容器仍在但已停（非 Running）→ SUCCESS（停止目标达成）。"""
        ex = StopInstanceExecutor(FakeDeployer(inspect_running=False))
        r = await ex.reconcile(_task())
        assert r is not None and r.status == "SUCCESS"
        assert r.instance_status == "STOPPED"

    async def test_running_container_none(self):
        """容器仍在运行 → None（等重新下发停止）。"""
        ex = StopInstanceExecutor(FakeDeployer(inspect_running=True))
        assert await ex.reconcile(_task()) is None

    async def test_no_container_name_none(self):
        ex = StopInstanceExecutor(FakeDeployer())
        assert await ex.reconcile(_task(container_name="")) is None
