"""StartInstanceExecutor 单测：起容器 + readiness 探活全链（fake Deployer + 注入 prober）。

覆盖：deploy→STARTING/RUNNING→READY+SUCCESS；readiness 超时（含最后一次探测信息）；
容器中途退出 / restart-loop / 超时 → **只 stop 不 rm** 保留容器供 RPC 取全量日志
（FAILED message 提示「详见容器日志」，不再内嵌日志尾）；cancel → stop+rm → STOPPED+CANCELLED；
reconcile：健康容器 SUCCESS / 未就绪 None / 已退出 None。
"""

from __future__ import annotations

import asyncio

from llmpod.apps.node.executors.start_instance import StartInstanceExecutor
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import ContainerState, DeployResult


class FakeDeployer:
    """可编程 Deployer 桩：记录调用 + 可注入 inspect 序列 / logs / 异常。"""

    def __init__(self, running=True, logs_text="ENGINE-CRASH-STDERR-TRACEBACK", inspect_seq=None):
        self.running = running
        self.logs_text = logs_text
        self.inspect_seq = list(inspect_seq or [])  # 逐次弹出；耗尽后按 running 标志返回
        self.deploy_calls = []
        self.stop_calls = []
        self.remove_calls = []

    async def deploy(self, spec):
        self.deploy_calls.append(spec.name)
        return DeployResult(success=True, container_name=spec.name)

    async def stop(self, name, timeout=10):
        self.stop_calls.append(name)

    async def remove(self, name, force=False):
        self.remove_calls.append((name, force))

    async def inspect(self, name):
        if self.inspect_seq:
            return self.inspect_seq.pop(0)
        return ContainerState(
            Status="running" if self.running else "exited",
            Running=self.running,
            Restarting=False,
            RestartCount=0,
            ExitCode=0 if self.running else 1,
        )

    async def logs(self, name, tail=200):
        return self.logs_text


_SPEC = {
    "image": "vllm/vllm-openai:v0.23.0",
    "name": "llmpod-dep-i1",
    "args": ["--model", "/data/m"],
    "labels": {"llmpod.managed": "true"},
}
_READINESS = {
    "url": "http://10.0.1.5:8000/health",
    "initial_delay_sec": 0,
    "interval_sec": 0,
    "timeout_sec": 5,
}


def _task(spec=None, readiness=None):
    return LocalTask(
        task_id="t1",
        type="START_INSTANCE",
        resource_type="INSTANCE",
        resource_key="instance:dep:0",
        node_id="n1",
        status="RUNNING",
        payload={
            "deployment_id": "dep",
            "instance_id": "i1",
            "rank": 0,
            "role": "full",
            "container_name": "llmpod-dep-i1",
            "spec": spec if spec is not None else dict(_SPEC),
            "readiness": readiness if readiness is not None else dict(_READINESS),
            "endpoint": "http://10.0.1.5:8000",
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
        ex = StartInstanceExecutor(FakeDeployer())
        events, report = _collect()
        await ex.execute(
            _task(spec=None).__class__(
                task_id="t1",
                type="START_INSTANCE",
                resource_type="INSTANCE",
                resource_key="instance:dep:0",
                node_id="n1",
                status="RUNNING",
                payload={
                    "deployment_id": "dep",
                    "instance_id": "i1",
                    "container_name": "",
                    "spec": {"image": "x", "name": "llmpod-dep-i1"},
                },
            ),
            None,
            report,
            asyncio.Event(),
        )
        assert events[-1]["status"] == "FAILED" and "缺少" in events[-1]["message"]
        # instance_status 走命名参数通道：失败终态必须驱动 instance 行落 FAILED，
        # 否则实例行滞留 STARTING 被自愈空耗重试（本文件其余失败分支同款）
        assert events[-1]["extra"]["instance_status"] == "FAILED"

    async def test_predeploy_inspect_daemon_error_conservative_fail(self):
        """pre-deploy inspect 的 daemon 级异常 ≠ 容器未运行：保守 FAILED，不 deploy。

        若误判「未运行」，恢复后的 deploy 会 409-replace 在服容器中断推理；
        daemon 仍不可用则误报失败释放卡 → 同卡双容器。与 stop / readiness 循环
        的保守口径一致（仅 ContainerGone 才当未运行）。
        """

        class _DaemonDown(FakeDeployer):
            async def inspect(self, name):
                raise RuntimeError("docker daemon timeout")

        dep = _DaemonDown()
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())

        assert events[-1]["status"] == "FAILED"
        assert "容器状态查询失败" in events[-1]["message"]
        assert events[-1]["extra"]["instance_status"] == "FAILED"
        assert dep.deploy_calls == []  # 状态未知绝不替换容器

    async def test_predeploy_inspect_gone_deploys(self):
        """inspect 报 ContainerGone（容器确实不存在）→ 正常走 deploy。"""
        from llmpod.apps.node.exceptions import ContainerGone

        class _Gone(FakeDeployer):
            async def inspect(self, name):
                # deploy 后 readiness 循环还要 inspect——首次 gone 走 deploy，
                # 之后稳定 running 供探活判定
                if not self.deploy_calls:
                    raise ContainerGone("no such container")
                return _stable_state(running=True)

        async def prober(url):
            return True

        dep = _Gone()
        ex = StartInstanceExecutor(dep, prober=prober)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert dep.deploy_calls == ["llmpod-dep-i1"]
        assert events[-1]["status"] == "SUCCESS"

    async def test_full_chain_deploy_ready_success(self):
        # already_running 门首次 inspect 返回未运行 → 走 deploy 路径；其后用默认 running 探活
        dep = FakeDeployer(inspect_seq=[_stable_state(running=False)])
        probed: list[str] = []

        async def prober(url):
            probed.append(url)
            return True

        ex = StartInstanceExecutor(dep, prober=prober)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())

        assert dep.deploy_calls == ["llmpod-dep-i1"]
        statuses = [e["status"] for e in events]
        assert statuses == ["RUNNING", "RUNNING", "SUCCESS"]
        assert events[0]["extra"]["instance_status"] == "STARTING"
        assert events[1]["extra"]["instance_status"] == "RUNNING"
        assert events[2]["extra"]["instance_status"] == "READY"
        assert events[2]["extra"]["endpoint"] == "http://10.0.1.5:8000"
        assert probed == ["http://10.0.1.5:8000/health"]
        assert dep.stop_calls == [] and dep.remove_calls == []  # 成功不清理

    async def test_crash_during_readiness_failed_keeps_container(self):
        """容器探活期间退出 → FAILED；只 stop 不 rm 保留容器供 RPC 取全量日志（不内嵌日志尾）。"""
        dep = FakeDeployer()
        events, report = _collect()
        tick = {"n": 0}

        async def prober(url):
            tick["n"] += 1
            if tick["n"] >= 1:
                dep.running = False  # 本轮探活后崩溃 → 下一 tick 的存活判定捕获
            return False

        ex = StartInstanceExecutor(dep, prober=prober)
        await ex.execute(_task(), None, report, asyncio.Event())

        assert events[-1]["status"] == "FAILED"
        assert events[-1]["extra"]["instance_status"] == "FAILED"
        assert "引擎崩溃" in events[-1]["message"]
        assert "详见容器日志" in events[-1]["message"]
        # 崩溃保留容器：只 stop 不 rm（清理由部署级 label 扫除/重试 409-replace 兜底）
        assert dep.stop_calls == ["llmpod-dep-i1"]
        assert dep.remove_calls == []

    async def test_readiness_timeout_failed(self):
        readiness = {
            "url": "http://10.0.1.5:8000/health",
            "initial_delay_sec": 0,
            "interval_sec": 0.01,
            "timeout_sec": 0.02,
        }
        dep = FakeDeployer()

        async def prober(url):
            return False

        ex = StartInstanceExecutor(dep, prober=prober)
        events, report = _collect()
        await ex.execute(_task(readiness=readiness), None, report, asyncio.Event())

        assert events[-1]["status"] == "FAILED"
        assert "readiness 超时" in events[-1]["message"]
        assert "http://10.0.1.5:8000/health" in events[-1]["message"]
        # instance_status 走命名参数通道（extra 里的同名键无消费方，实例行不会落
        # FAILED 就没人重下发该分片）——本文件其余失败分支同款
        assert events[-1]["extra"]["instance_status"] == "FAILED"
        # 超时保留容器供 RPC 取日志排查「卡在哪」：只 stop 不 rm
        assert dep.stop_calls == ["llmpod-dep-i1"]
        assert dep.remove_calls == []

    async def test_cancel_before_deploy(self):
        dep = FakeDeployer()
        cancel = asyncio.Event()
        cancel.set()
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, cancel)
        assert events[-1]["status"] == "CANCELLED"
        assert dep.deploy_calls == []

    async def test_cancel_during_readiness_stop_rm(self):
        """探活期 cancel → stop+rm 收尾 → STOPPED+CANCELLED。"""
        dep = FakeDeployer()
        cancel = asyncio.Event()

        async def prober(url):
            cancel.set()  # 第一次探测后触发取消
            return False

        ex = StartInstanceExecutor(dep, prober=prober)
        events, report = _collect()
        await ex.execute(_task(), None, report, cancel)

        assert events[-1]["status"] == "CANCELLED"
        assert events[-1]["extra"]["instance_status"] == "STOPPED"
        assert dep.stop_calls == ["llmpod-dep-i1"]
        assert dep.remove_calls == [("llmpod-dep-i1", True)]

    async def test_deploy_failure_reports_failed(self):
        class _Boom(FakeDeployer):
            async def deploy(self, spec):
                raise RuntimeError("docker daemon down")

        # already_running 门：容器未在运行才走 deploy（_Boom.deploy 抛错）
        ex = StartInstanceExecutor(_Boom(running=False))
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "docker daemon down" in events[-1]["message"]
        assert events[-1]["extra"]["instance_status"] == "FAILED"


class TestReconcile:
    async def test_healthy_container_returns_success_ready(self):
        """重启探测：容器 Running 且探活通过 → SUCCESS(+READY)，不误杀健康容器。"""

        async def prober(url):
            return True

        ex = StartInstanceExecutor(FakeDeployer(running=True), prober=prober)
        r = await ex.reconcile(_task())
        assert r is not None and r.status == "SUCCESS"
        assert r.instance_status == "READY"
        assert r.endpoint == "http://10.0.1.5:8000"

    async def test_running_but_not_ready_returns_none(self):
        """半加载容器（Running 未就绪）→ None（等重新下发接续，不误判部署完成）。"""

        async def prober(url):
            return False

        ex = StartInstanceExecutor(FakeDeployer(running=True), prober=prober)
        assert await ex.reconcile(_task()) is None

    async def test_exited_container_returns_none(self):
        ex = StartInstanceExecutor(FakeDeployer(running=False))
        assert await ex.reconcile(_task()) is None

    async def test_no_container_name_none(self):
        ex = StartInstanceExecutor(FakeDeployer())
        assert (
            await ex.reconcile(
                _task(spec=None).__class__(
                    task_id="t1",
                    type="START_INSTANCE",
                    resource_type="INSTANCE",
                    resource_key="instance:dep:0",
                    node_id="n1",
                    status="RUNNING",
                    payload={},
                )
            )
            is None
        )


def _stable_state(running=True, restarting=False, restart_count=0):
    return ContainerState(
        Status="running" if running else "exited",
        Running=running,
        Restarting=restarting,
        RestartCount=restart_count,
        ExitCode=0 if running else 1,
    )


_STABLE_READINESS = {
    "mode": "stable_running",
    "initial_delay_sec": 0,
    "interval_sec": 0,
    "timeout_sec": 5,
}


class TestExecuteStable:
    """stable_running 模式（headless worker：无 HTTP 端口，容器存活稳定即就绪）。"""

    async def test_stable_ready_after_consecutive_checks(self):
        """连续 3 次稳定判定 → READY + SUCCESS（不探 HTTP）。"""
        dep = FakeDeployer(inspect_seq=[_stable_state()] * 3)
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(readiness=dict(_STABLE_READINESS)), None, report, asyncio.Event())
        assert events[-1]["status"] == "SUCCESS"
        assert events[-1]["extra"]["instance_status"] == "READY"
        assert dep.stop_calls == [] and dep.remove_calls == []

    async def test_stable_flap_resets_counter(self):
        """稳定→抖动→重新计数：需再连续 3 次才成功。"""
        seq = [
            _stable_state(),
            _stable_state(),  # 2 连
            _stable_state(restarting=True),  # 抖动归零
            _stable_state(),
            _stable_state(),
            _stable_state(),  # 重新 3 连
        ]
        dep = FakeDeployer(inspect_seq=seq)
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(readiness=dict(_STABLE_READINESS)), None, report, asyncio.Event())
        assert events[-1]["status"] == "SUCCESS"

    async def test_stable_restart_fast_fail(self):
        """RestartCount>0 → 立即 FAILED（restart-loop 快失败），不等超时；保留容器不内嵌日志尾。"""
        # 首次 inspect（already_running 门）running → 跳过 deploy；readiness 首检 rc>0 快失败
        dep = FakeDeployer(inspect_seq=[_stable_state(), _stable_state(restart_count=2)], logs_text="RESTART-LOOP-LOG")
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        readiness = dict(_STABLE_READINESS)
        readiness["timeout_sec"] = 999  # 若走超时路径会很久——快失败应立即返回
        await ex.execute(_task(readiness=readiness), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "反复重启" in events[-1]["message"]
        assert "RestartCount=2" in events[-1]["message"]
        assert "详见容器日志" in events[-1]["message"]
        # 失败保留容器：只 stop 不 rm
        assert dep.stop_calls == ["llmpod-dep-i1"]
        assert dep.remove_calls == []

    async def test_stable_crash_keeps_container(self):
        """存活判定优先于稳定判定：容器退出 → 崩溃 FAILED；保留容器供 RPC 取全量日志。"""
        dep = FakeDeployer(inspect_seq=[_stable_state(), _stable_state(running=False)], logs_text="WORKER-CRASH-LOG")
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(readiness=dict(_STABLE_READINESS)), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "引擎崩溃" in events[-1]["message"]
        assert "详见容器日志" in events[-1]["message"]
        assert dep.stop_calls == ["llmpod-dep-i1"]
        assert dep.remove_calls == []

    async def test_stable_timeout(self):
        """始终抖动（Restarting）未达连续稳定 → 超时 FAILED。"""
        dep = FakeDeployer(inspect_seq=[_stable_state(restarting=True)] * 10)
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        readiness = dict(_STABLE_READINESS)
        readiness["timeout_sec"] = 0.05
        readiness["interval_sec"] = 0.02  # 每 tick 睡 20ms，3 tick 后撞超时
        await ex.execute(_task(readiness=readiness), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "稳定运行" in events[-1]["message"]

    async def test_stable_cancel(self):
        """deploy 前 cancel → CANCELLED（容器未起，无 STOPPED 容器态上报）。"""
        dep = FakeDeployer(inspect_seq=[_stable_state()])
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        cancel = asyncio.Event()
        cancel.set()
        await ex.execute(_task(readiness=dict(_STABLE_READINESS)), None, report, cancel)
        assert events[-1]["status"] == "CANCELLED"
        assert dep.deploy_calls == []

    async def test_stable_cancel_during_wait_stop_rm(self):
        """稳定等待期 cancel → stop+rm 收尾 → STOPPED+CANCELLED。"""
        dep = FakeDeployer(inspect_seq=[_stable_state(), _stable_state()])
        ex = StartInstanceExecutor(dep)
        events, report = _collect()
        cancel = asyncio.Event()
        readiness = dict(_STABLE_READINESS)
        readiness["interval_sec"] = 5  # 第一次判定后进入等待，期间触发取消
        task = _task(readiness=readiness)

        async def _set_cancel():
            await asyncio.sleep(0.05)
            cancel.set()

        waiter = asyncio.create_task(_set_cancel())
        await ex.execute(task, None, report, cancel)
        await waiter
        assert events[-1]["status"] == "CANCELLED"
        assert events[-1]["extra"]["instance_status"] == "STOPPED"
        assert dep.remove_calls == [("llmpod-dep-i1", True)]


class TestReconcileStable:
    async def test_stable_running_returns_success(self):
        """stable 模式：存活稳定（RestartCount==0）→ SUCCESS(+READY)。"""
        ex = StartInstanceExecutor(FakeDeployer(inspect_seq=[_stable_state()]))
        r = await ex.reconcile(_task(readiness=dict(_STABLE_READINESS)))
        assert r is not None and r.status == "SUCCESS"
        assert r.instance_status == "READY"

    async def test_stable_restarted_returns_none(self):
        """重启过的容器不算健康 → None（重发后新 attempt 快失败）。"""
        ex = StartInstanceExecutor(FakeDeployer(inspect_seq=[_stable_state(restart_count=1)]))
        assert await ex.reconcile(_task(readiness=dict(_STABLE_READINESS))) is None

    async def test_stable_exited_returns_none(self):
        ex = StartInstanceExecutor(FakeDeployer(inspect_seq=[_stable_state(running=False)]))
        assert await ex.reconcile(_task(readiness=dict(_STABLE_READINESS))) is None
