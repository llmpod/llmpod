"""reconcile_runtime_instances / _backfill_ready / controller RUNNING 分支集成测试。

FakeSession + monkeypatch 桩（断开真实 PG）：覆盖崩溃实例重启（复用 _ensure_start_tasks）、
attempt 耗尽 fail、PENDING 重发（node 离线自愈）、lost-report backfill、head-first 门控、
反停止竞态守卫。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from _fakes import FakeSession

from llmpod.apps.manager.deployment import reconciler, task_ops
from llmpod.config.constants import TaskStatus, TaskType
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task


def _patch_db(monkeypatch, session):
    @asynccontextmanager
    async def _fake():
        yield session

    monkeypatch.setattr(reconciler, "get_db_context", _fake)


def _model(session) -> Model:
    return session.preload(Model(id=1, name="m", model_type="llm"))


def _dep(session, status="running", phase="RUNNING") -> ModelDeployment:
    return session.preload(
        ModelDeployment(
            deployment_id="dep1",
            cluster_id=1,
            name="d",
            model_id=1,
            engine="vllm",
            status=status,
            phase=phase,
            spec={},
        )
    )


def _inst(session, iid="i1", rank=0, node_rank=0, status="READY", node="n1", spec=None) -> Instance:
    s = {
        "instance_id": iid,
        "engine_instance_id": "eng-1",
        "rank": rank,
        "node_rank": node_rank,
        "gpu_arch": "nvidia",
        "master_addr": "10.0.0.1",
        "port": 8000,
    }
    s.update(spec or {})
    return session.preload(
        Instance(
            instance_id=iid,
            deployment_id="dep1",
            model_deployment_instance_id="eng-1",
            role="full",
            rank=rank,
            assigned_node=node,
            status=status,
            spec=s,
        )
    )


def _start_task(session, rank=0, status="SUCCESS", attempt=1, node="n1", **kw) -> Task:
    defaults = dict(
        id=f"t{rank}-{attempt}",
        resource_type="INSTANCE",
        resource_key=f"instance:eng-1:{rank}",
        node_id=node,
        type=TaskType.START_INSTANCE,
        status=status,
        attempt=attempt,
        payload={},
    )
    defaults.update(kw)
    return session.preload(Task(**defaults))


def _patch_ensure(monkeypatch, calls):
    """桩 _ensure_start_tasks（record 调用；返回非 None 即视为成功）。"""

    async def _fake(dep, model, config, adapter, rows):
        calls.append([r.instance_id for r in rows])
        return {f"instance:eng-1:{r.rank}": None for r in rows}

    monkeypatch.setattr(reconciler, "_ensure_start_tasks", _fake)


def _patch_fail(monkeypatch, errors):
    async def _fake(deployment_id, error=None, message=None):
        errors.append((deployment_id, error, message))

    monkeypatch.setattr(reconciler, "fail_deployment", _fake)


class TestReconcileRuntimeInstances:
    async def test_crashed_head_restarts_head_only(self, monkeypatch):
        """RUNNING 部署 + CRASHED head → 只驱动 head（head-first 门控）。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, "i1", rank=0, status="CRASHED")
        _inst(session, "i2", rank=1, node_rank=1, status="READY", node="n2")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        assert calls == [["i1"]]

    async def test_not_running_noop(self, monkeypatch):
        """部署非 RUNNING（已终态/收敛中）→ no-op。"""
        session = FakeSession()
        _dep(session, status="failed")
        _model(session)
        _inst(session, status="CRASHED")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")
        assert calls == []

    async def test_stopping_instance_blocks(self, monkeypatch):
        """任一实例 STOPPING/STOPPED → 本轮不驱动（反停止竞态）。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, "i1", status="CRASHED")
        _inst(session, "i2", rank=1, status="STOPPING")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")
        assert calls == []

    async def test_full_restart_resets_siblings_and_drives_all(self, monkeypatch):
        """worker 崩 ⇒ 引擎整份判死：已就绪分片重置回 PENDING；head 因被重置判「未起」，
        本轮先驱动 head 组（含死分片所在组等 head rendezvous），下轮起 workers。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        # 同引擎 eng-1 的三机分片：head READY + worker CRASHED + worker READY
        _inst(session, "i1", rank=0, status="READY")
        _inst(session, "i2", rank=1, node_rank=1, status="CRASHED", node="n2")
        _inst(session, "i3", rank=2, node_rank=2, status="READY", node="n3")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        # 整份重启把 head/worker 已就绪分片都重置回 PENDING；head 刚被重置判「未起」，
        # 本轮只驱动 head 组（i1），死分片 i2 与重置出的 i3 随 workers 组下轮驱动
        assert calls == [["i1"]]
        i1 = await session.get(Instance, "i1")
        i3 = await session.get(Instance, "i3")
        assert i1.status == "PENDING" and i3.status == "PENDING"  # 已就绪分片被重置

    async def test_workers_driven_when_head_container_running(self, monkeypatch):
        """head 容器已 RUNNING（rendezvous 已起，/health 等 workers 连上）→ 死 worker 同轮被驱动。

        回归：旧门控按「head 组还有待驱动成员」拦截 workers——head /health 需 workers 连上
        才 200，head 恒非 READY 恒占驱动轮次，worker 永不重启、每轮烧 attempt 直至
        fail_deployment。新判据只看 head「容器已启动」。
        """
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, "i1", rank=0, status="RUNNING")
        _inst(session, "i2", rank=1, node_rank=1, status="CRASHED", node="n2")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        # head 容器 RUNNING 即放行：死 worker i2 本轮就进驱动集（i1 RUNNING 由
        # _ensure_start_tasks 跳过，不重发）
        assert calls == [["i1", "i2"]]

    async def test_workers_wait_until_head_container_running(self, monkeypatch):
        """head 容器未 RUNNING（PENDING/STARTING/CRASHED）→ 本轮只驱动 head，workers 等下轮。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, "i1", rank=0, status="CRASHED")
        _inst(session, "i2", rank=1, node_rank=1, status="CRASHED", node="n2")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        assert calls == [["i1"]]  # head 未起，workers 不抢跑（rendezvous 未就绪）

    async def test_stuck_foreign_head_does_not_block_healthy_worker(self, monkeypatch):
        """他份起不来的 head 不在驱动集内，不挡健康副本 worker 的自愈。

        回归：旧门控在**整个部署**的首起组上判「还有未起成员」——A 份 head 卡死
        （节点失联/退避窗）会让 first_booting 恒真，B 份崩掉的 worker 永不被重发，
        健康副本失去自愈。门控只看本轮驱动集内的首起组成员。
        """
        import datetime as _dt

        session = FakeSession()
        _dep(session)
        _model(session)
        # A 份：head CRASHED 且在退避窗内（最近一次启动失败）→ 本轮不可驱动
        _inst(session, "ha", rank=0, status="CRASHED")
        t = _start_task(session, rank=0, status="FAILED", attempt=1)
        t.resource_key = "instance:eng-1:0"
        t.finished_at = _dt.datetime.now(_dt.timezone.utc)  # 刚失败 → 退避窗内
        # B 份：head 容器已 RUNNING（rendezvous 已起）、worker CRASHED 待重发
        session.preload(
            Instance(
                instance_id="hb",
                deployment_id="dep1",
                model_deployment_instance_id="eng-b",
                role="full",
                rank=1,
                assigned_node="n2",
                status="RUNNING",
                spec={"engine_instance_id": "eng-b", "replica_idx": 1},
            )
        )
        session.preload(
            Instance(
                instance_id="wb",
                deployment_id="dep1",
                model_deployment_instance_id="eng-b",
                role="full",
                rank=2,
                assigned_node="n3",
                status="CRASHED",
                spec={"engine_instance_id": "eng-b", "node_rank": 1, "replica_idx": 1},
            )
        )
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        # B 份 worker 进驱动集（其 head 容器 RUNNING 放行次起组；hb 由
        # _ensure_start_tasks 跳过不重发）；A 份 head 退避中不在集内、不挡人
        assert calls == [["hb", "wb"]]

    async def test_cross_replica_engine_not_reset(self, monkeypatch):
        """副本=隔离边界：replica1 引擎死不重置 replica0 引擎的已就绪分片。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        # 列 model_deployment_instance_id 优先于 spec（spec 覆盖不了挂载列）
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="dep1",
                model_deployment_instance_id="eng-a",
                role="full",
                rank=0,
                assigned_node="n1",
                status="READY",
                spec={"engine_instance_id": "eng-a", "node_rank": 0},
            )
        )
        _inst(session, "i2", rank=1, status="CRASHED", spec={"engine_instance_id": "eng-b"})
        i2 = await session.get(Instance, "i2")
        i2.model_deployment_instance_id = "eng-b"
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        assert calls == [["i2"]]  # 只驱动死引擎自身
        i1 = await session.get(Instance, "i1")
        assert i1.status == "READY"  # 异引擎（他副本）分片不受牵连

    async def test_backoff_gate_skips_recent_failure(self, monkeypatch):
        """指数退避门：末尾连续失败后未满退避间隔 → 本轮不驱动。"""
        import datetime as _dt

        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, status="CRASHED")
        _start_task(session, rank=0, status="FAILED", attempt=1)
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)
        # task.finished_at 为「刚刚」→ 距上次失败 < 10s 退避下限
        t = next(iter(session._store.get(Task, {}).values()))
        t.finished_at = _dt.datetime.now(_dt.timezone.utc)

        await reconciler.reconcile_runtime_instances("dep1")

        assert calls == []  # 退避窗内不重试

    async def test_backoff_elapsed_drives(self, monkeypatch):
        """退避已满（上次失败远早于退避间隔）→ 正常驱动。"""
        import datetime as _dt

        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, status="CRASHED")
        task = _start_task(session, rank=0, status="FAILED", attempt=1)
        task.finished_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=3600)
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        assert calls == [["i1"]]

    async def test_attempt_exhausted_stops(self, monkeypatch):
        """_ensure_start_tasks 返回 None（超预算已 fail）→ 不再驱动 workers。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, "i1", rank=0, status="READY")
        _inst(session, "i2", rank=1, node_rank=1, status="CRASHED", node="n2")
        _patch_db(monkeypatch, session)

        async def _exhausted(dep, model, config, adapter, rows):
            return None

        monkeypatch.setattr(reconciler, "_ensure_start_tasks", _exhausted)

        # 不应抛异常（None 已被 return 消化）
        await reconciler.reconcile_runtime_instances("dep1")

    async def test_pending_instance_triggers(self, monkeypatch):
        """PENDING 实例（node 离线遗留）也在触发集内——恢复循环负责重发。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, status="PENDING")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")
        assert calls == [["i1"]]

    async def test_backfill_ready_fixes_lost_report(self, monkeypatch):
        """瞬态实例 + START task SUCCESS → 补写 READY（lost-report 兜底）。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, status="STARTING")
        _start_task(session, rank=0, status="SUCCESS")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        inst = await session.get(Instance, "i1")
        assert inst.status == "READY"
        assert calls == []  # backfill 后无 abnormal → 不建 START

    async def test_backfill_does_not_resurrect_crashed(self, monkeypatch):
        """CRASHED 实例不被 backfill 复活（仅瞬态允许补写）。"""
        session = FakeSession()
        _dep(session)
        _model(session)
        _inst(session, status="CRASHED")
        _start_task(session, rank=0, status="SUCCESS")
        _patch_db(monkeypatch, session)
        calls: list[list[str]] = []
        _patch_ensure(monkeypatch, calls)

        await reconciler.reconcile_runtime_instances("dep1")

        inst = await session.get(Instance, "i1")
        assert inst.status == "CRASHED"
        assert calls == [["i1"]]  # 仍走重启


class TestRuntimeFailDeployment:
    """运行期 attempt 耗尽的失败收敛：running 部署也要能标 failed（否则永久卡
    running+STOPPING，VRAM/assignment 泄漏）。"""

    async def test_fail_deployment_marks_running_failed(self, monkeypatch):
        """fail_deployment 对 RUNNING 部署 → status=failed（mark_failed_if_active
        的可失败集合含 running）。"""
        session = FakeSession()
        _dep(session, status="running", phase="RUNNING")
        _model(session)
        _inst(session, status="CRASHED")
        _patch_db(monkeypatch, session)

        # dispatch_stop 桩（不触真 StopService）
        async def _fake_stop(self, deployment_id):
            return None

        from llmpod.apps.manager.deployment.stop import StopService

        monkeypatch.setattr(StopService, "dispatch_stop", _fake_stop)

        await reconciler.fail_deployment("dep1", error="连续 3 次崩溃", message="部署异常终止（已达最大重试次数）")

        dep2 = await session.get(ModelDeployment, "dep1")
        assert dep2.status == "failed"
        assert dep2.phase == "STOPPING"
        assert "崩溃" in (dep2.last_error or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "FAILED"

    async def test_fail_deployment_does_not_touch_stopped(self, monkeypatch):
        """用户显式终态（stopped）不被失败收敛覆盖（mark_failed_if_active 守卫）。"""
        session = FakeSession()
        _dep(session, status="stopped", phase="STOPPING")
        _model(session)
        _inst(session, status="STOPPED")
        _patch_db(monkeypatch, session)

        async def _fake_stop(self, deployment_id):
            return None

        from llmpod.apps.manager.deployment.stop import StopService

        monkeypatch.setattr(StopService, "dispatch_stop", _fake_stop)

        await reconciler.fail_deployment("dep1", error="late failure")

        dep2 = await session.get(ModelDeployment, "dep1")
        assert dep2.status == "stopped"
        inst = await session.get(Instance, "i1")
        assert inst.status == "STOPPED"


class TestEnsureStartTasksCrashRelaxation:
    """_ensure_start_tasks 幂等短路的 CRASHED 放宽：历史 SUCCESS 不再代表活容器。"""

    async def test_crashed_instance_not_shortcircuited(self, monkeypatch):
        session = FakeSession()
        dep = _dep(session, status="creating", phase="START_HEAD")
        _model(session)
        _inst(session, status="CRASHED")
        _start_task(session, rank=0, status="SUCCESS")
        _patch_db(monkeypatch, session)
        dispatched: list[str] = []

        async def _fake_dispatch(t):
            dispatched.append(t.id)

        async def _fake_ensure_row(db, **kw):
            new = Task(
                id="t-new",
                resource_type="INSTANCE",
                resource_key=kw["resource_key"],
                node_id=kw["node_id"],
                type=kw["task_type"],
                status=TaskStatus.PENDING,
                attempt=2,
                payload={},
            )
            session.preload(new)
            return new, True, None

        def _fake_payload(dep, model, adapter, config, inst):
            from llmpod.tunnel.schemas import DeploySpec, StartInstancePayload

            return StartInstancePayload(
                deployment_id="dep1",
                instance_id="i1",
                rank=0,
                role="full",
                container_name="c1",
                spec=DeploySpec(image="img", name="c1"),
            )

        monkeypatch.setattr(reconciler, "_start_payload", _fake_payload)
        monkeypatch.setattr(task_ops, "ensure_task_row", _fake_ensure_row)
        monkeypatch.setattr(task_ops, "dispatch_task", _fake_dispatch)

        latest = await reconciler._ensure_start_tasks(dep, None, None, _FakeAdapter(), session.all(Instance))

        assert dispatched == ["t-new"]  # 建了新 attempt 并下发
        assert latest is not None

    async def test_exhausted_raises_without_failing_deployment_when_flagged(self, monkeypatch):
        """``fail_on_exhausted=False``（在线扩容）：重试耗尽就地抛错，**不动整个部署**。

        首建期（默认 True）走 ``fail_deployment`` 整部署停摆；扩容路径要的是「只回滚新份」，
        由调用方的失败收敛处理——见 test_deployment_scale 的扩容重试耗尽用例。
        """
        session = FakeSession()
        dep = _dep(session, status="creating", phase="START_HEAD")
        _inst(session, status="STARTING")
        _patch_db(monkeypatch, session)

        async def _no_task(db, **kw):
            return None, False, "exhausted"  # ensure_task_row 拒建（耗尽）→ 触发耗尽分支

        def _fake_payload(dep, model, adapter, config, inst):
            from llmpod.tunnel.schemas import DeploySpec, StartInstancePayload

            return StartInstancePayload(
                deployment_id="dep1",
                instance_id="i1",
                rank=0,
                role="full",
                container_name="c1",
                spec=DeploySpec(image="img", name="c1"),
            )

        monkeypatch.setattr(task_ops, "ensure_task_row", _no_task)
        monkeypatch.setattr(reconciler, "_start_payload", _fake_payload)

        with pytest.raises(reconciler.LLMPodError):
            await reconciler._ensure_start_tasks(
                dep, None, None, _FakeAdapter(), session.all(Instance), fail_on_exhausted=False
            )
        assert (await session.get(ModelDeployment, "dep1")).status == "creating"  # 未被标 failed

    async def test_ready_instance_still_shortcircuited(self, monkeypatch):
        """READY 实例 + 历史 SUCCESS → 仍被幂等短路（不建新 task）。"""
        session = FakeSession()
        dep = _dep(session, status="creating", phase="START_HEAD")
        _inst(session, status="READY")
        _start_task(session, rank=0, status="SUCCESS")
        _patch_db(monkeypatch, session)

        created: list[dict] = []

        async def _fake_ensure_row(db, **kw):
            created.append(kw)
            return None, False

        monkeypatch.setattr(task_ops, "ensure_task_row", _fake_ensure_row)

        latest = await reconciler._ensure_start_tasks(dep, None, None, _FakeAdapter(), session.all(Instance))

        assert created == []  # 短路未建
        assert latest is not None


class _FakeAdapter:
    """最小 adapter 桩（build_run_spec 路径走不到——payload 由 _fake_ensure_row 桩掉）。"""

    default_arch = "nvidia"


class TestControllerRuntimeScan:
    """controller._scan_once RUNNING 分支：非 READY 实例触发 spawn。"""

    async def test_scan_spawns_runtime_reconcile(self, monkeypatch):
        from llmpod.apps.manager.deployment import controller as ctrl

        session = FakeSession()
        _dep(session)  # running
        _inst(session, status="CRASHED")
        # 另一个健康部署（实例全 READY，不应命中）
        session.preload(
            ModelDeployment(
                deployment_id="dep2",
                cluster_id=1,
                name="d2",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={},
            )
        )
        session.preload(
            Instance(
                instance_id="i2",
                deployment_id="dep2",
                role="full",
                rank=0,
                assigned_node="n2",
                status="READY",
                spec={},
            )
        )
        # creating 部署（实例 STARTING）不应命中 runtime 分支——启动期由 advance 驱动
        session.preload(
            ModelDeployment(
                deployment_id="dep3",
                cluster_id=1,
                name="d3",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="START_HEAD",
                spec={},
            )
        )
        session.preload(
            Instance(
                instance_id="i3",
                deployment_id="dep3",
                role="full",
                rank=0,
                assigned_node="n3",
                status="STARTING",
                spec={},
            )
        )

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr(ctrl, "get_db_context", _fake_db)
        spawned: list[str] = []

        async def _fake_reconcile(did):
            spawned.append(did)

        monkeypatch.setattr(reconciler, "reconcile_runtime_instances", _fake_reconcile)
        # creating 部署的容器阶段推进桩（防 advance_deployment 触真库）
        monkeypatch.setattr(reconciler, "advance_deployment", self._noop_advance)

        # 扩缩驱动桩（SCALING 分支不触 scale 真逻辑）
        async def _fake_drive(did):
            spawned.append(f"scale:{did}")

        from llmpod.apps.manager.deployment import scale as scale_mod

        monkeypatch.setattr(scale_mod, "drive_scaling", _fake_drive)

        # 拦 create_task（同步签名）：包成最小 Task 形状返回，协程用真 create_task 驱动
        real_create_task = asyncio.create_task

        def _fake_create_task(coro, name=None):
            real_create_task(coro, name=name)  # 后台驱动（返回值不入 _scan_once 的回调链）

            class _Wrap:
                def add_done_callback(self, cb):
                    pass

            return _Wrap()

        monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
        c = ctrl.DeploymentController(scan_interval=0.01, startup_grace=0)
        await c._scan_once()
        # 后台协程让事件循环跑完
        for _ in range(4):
            await asyncio.sleep(0)

        assert spawned == ["dep1"]  # dep2 全 READY、dep3 非 RUNNING 均不触发

    @staticmethod
    async def _noop_advance(deployment_id):
        return None
