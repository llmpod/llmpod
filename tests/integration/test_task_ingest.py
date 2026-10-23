"""ManagerTaskServiceImpl 集成测试：report_task_event 投影 task/task_event/node_model
（含终态守卫、孤儿事件）+ sync_node_state drift 重发 cancel + report_instance_status
实例长期状态落库（终态/单调守卫）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import pytest

from _fakes import FakeSession

import llmpod.apps.manager.dispatch as dispatch_mod
from llmpod.apps.manager.services import task_ingest as ti_mod
from llmpod.apps.manager.services.task_ingest import ManagerTaskServiceImpl
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.deploy_log import DeployLog
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.models.task_event import TaskEvent
from llmpod.tunnel.schemas import InstanceStatusPayload, TaskEventPayload, TaskState


def _patch_db(monkeypatch, session):
    @asynccontextmanager
    async def _fake():
        yield session

    monkeypatch.setattr(ti_mod, "get_db_context", _fake)


def _patch_cancel(monkeypatch, cancels):
    async def _cancel(node_id, task_id):
        cancels.append((node_id, task_id))
        return {"ok": True}

    monkeypatch.setattr(dispatch_mod, "cancel_task", _cancel)


def _patch_redrive(monkeypatch, calls):
    """桩掉 PlacementService.redrive_pending（避免 sync_node_state 测试触达真库）。"""

    async def _redrive(self, node_id):
        calls.append(node_id)
        return 0

    monkeypatch.setattr(
        "llmpod.apps.manager.deployment.placement.PlacementService.redrive_pending",
        _redrive,
    )


def _task(session, status="RUNNING", **kw):
    defaults = dict(
        id="t1",
        resource_type="MODEL_PLACEMENT",
        resource_key="model:1:n1",
        node_id="n1",
        type="ENSURE_MODEL",
        status=status,
        attempt=1,
        payload={},
    )
    defaults.update(kw)
    return session.preload(Task(**defaults))


class TestAuthenticatedNodeOwnership:
    async def test_task_event_row_uses_authenticated_node_id(self, monkeypatch):
        session = FakeSession()
        task = _task(session, node_id="n1")
        _patch_db(monkeypatch, session)
        service = ManagerTaskServiceImpl(actor_node_id="n1")
        service._check_actor = lambda *_args: None  # 证明入库身份不取自负载字段
        await service.report_task_event(TaskEventPayload(task_id=task.id, node_id="forged", status="SUCCESS"))
        assert session.all(TaskEvent)[0].node_id == "n1"

    async def test_report_cannot_write_another_nodes_task(self, monkeypatch):
        session = FakeSession()
        task = _task(session, node_id="n2")
        _patch_db(monkeypatch, session)
        with pytest.raises(PermissionError):
            await ManagerTaskServiceImpl(actor_node_id="n1").report_task_event(
                TaskEventPayload(task_id=task.id, node_id="n1", status="FAILED")
            )
        assert task.status == "RUNNING"
        assert session.all(TaskEvent) == []

    async def test_sync_cannot_reset_another_nodes_running_task(self, monkeypatch):
        session = FakeSession()
        task = _task(session, node_id="n2")
        _patch_db(monkeypatch, session)
        service = ManagerTaskServiceImpl(actor_node_id="n1")
        with pytest.raises(PermissionError):
            await service.sync_node_state("n2", [])
        assert task.status == "RUNNING"
        # 即使误删 payload 断言，数据库查询仍强制使用通道身份。
        service._check_actor = lambda *_args: None

        async def no_redrive(_node_id):
            return None

        service._redrive_pending = no_redrive
        await service.sync_node_state("n2", [])
        assert task.status == "RUNNING"

    async def test_sync_cannot_replay_foreign_task(self, monkeypatch):
        session = FakeSession()
        task = _task(session, node_id="n2")
        _patch_db(monkeypatch, session)
        with pytest.raises(PermissionError):
            await ManagerTaskServiceImpl(actor_node_id="n1").sync_node_state(
                "n1", [TaskState(id=task.id, status="SUCCESS")]
            )
        assert task.status == "RUNNING"

    async def test_instance_must_match_node_and_deployment(self, monkeypatch):
        session = FakeSession()
        session.preload(Instance(instance_id="i1", deployment_id="dep1", assigned_node="n2", status="RUNNING", spec={}))
        _patch_db(monkeypatch, session)
        service = ManagerTaskServiceImpl(actor_node_id="n1")
        with pytest.raises(PermissionError):
            await service.report_instance_status(
                InstanceStatusPayload(instance_id="i1", deployment_id="dep1", node_id="n1", status="READY")
            )
        instance = await session.get(Instance, "i1")
        instance.assigned_node = "n1"
        with pytest.raises(PermissionError):
            await service.report_instance_status(
                InstanceStatusPayload(instance_id="i1", deployment_id="dep2", node_id="n1", status="READY")
            )
        assert instance.status == "RUNNING"

    async def test_instance_report_without_deployment_is_accepted(self, monkeypatch):
        """容器消失分支读不到容器标签，上报空部署编号——按契约放行。"""
        session = FakeSession()
        instance = Instance(instance_id="i1", deployment_id="dep1", assigned_node="n1", status="READY", spec={})
        session.preload(instance)
        _patch_db(monkeypatch, session)

        async def _no_terminal(_db, _dep_id):
            return False

        service = ManagerTaskServiceImpl(actor_node_id="n1")
        service._deployment_terminal = _no_terminal
        ack = await service.report_instance_status(
            InstanceStatusPayload(instance_id="i1", deployment_id="", node_id="n1", status="CRASHED")
        )
        assert ack.ok
        assert instance.status == "CRASHED"


class TestReportTaskEvent:
    async def test_downloading_updates_task_and_node_model(self, monkeypatch):
        session = FakeSession()
        _task(session, status="PENDING")
        session.preload(NodeModel(model_id=1, node_id="n1", status="absent", desired_state="present"))
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(
                task_id="t1",
                node_id="n1",
                status="RUNNING",
                progress=10,
                stage="downloading",
                extra={"bytes_done": 100, "bytes_total": 1000},
            )
        )
        assert ack.ok

        t = await session.get(Task, "t1")
        assert t.status == "RUNNING" and t.progress == 10
        assert t.stage == "downloading"  # 子阶段快照写回
        nm = session.all(NodeModel)[0]
        assert nm.status == "downloading"
        assert nm.bytes_done == 100 and nm.bytes_total == 1000
        assert len(session.all(TaskEvent)) == 1

    async def test_model_path_marks_present(self, monkeypatch):
        session = FakeSession()
        _task(session, status="RUNNING")
        session.preload(NodeModel(model_id=1, node_id="n1", status="verifying", desired_state="present"))
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(
                task_id="t1",
                node_id="n1",
                status="SUCCESS",
                progress=100,
                stage="finished",
                extra={"model_path": "/data/models/model-1"},
            )
        )

        nm = session.all(NodeModel)[0]
        assert nm.status == "present"
        assert nm.model_path == "/data/models/model-1"
        t = await session.get(Task, "t1")
        assert t.status == "SUCCESS" and t.finished_at is not None
        assert t.stage == "finished"

    async def test_failed_marks_failed_with_error(self, monkeypatch):
        session = FakeSession()
        _task(session, status="RUNNING")
        session.preload(NodeModel(model_id=1, node_id="n1", status="downloading", desired_state="present"))
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="t1", node_id="n1", status="FAILED", progress=50, message="disk full")
        )

        nm = session.all(NodeModel)[0]
        assert nm.status == "failed" and nm.last_error == "disk full"
        t = await session.get(Task, "t1")
        assert t.status == "FAILED" and t.last_error == "disk full"

    async def test_terminal_not_overwritten_by_stale_report(self, monkeypatch):
        """已 SUCCESS 的 task，stale RUNNING 报告不得回退 task 与 node_model。"""
        session = FakeSession()
        _task(session, status="SUCCESS", progress=100)
        session.preload(NodeModel(model_id=1, node_id="n1", status="present", desired_state="present"))
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING", progress=20)
        )

        t = await session.get(Task, "t1")
        assert t.status == "SUCCESS" and t.progress == 100  # 未被 stale 覆盖
        assert session.all(NodeModel)[0].status == "present"  # 未回退成 downloading
        assert len(session.all(TaskEvent)) == 1  # 审计事件仍记录

    async def test_progress_does_not_regress_on_stale_report(self, monkeypatch):
        """task.progress 单调——stale 低进度 RUNNING 报告不回退。"""
        session = FakeSession()
        _task(session, status="RUNNING", progress=80)
        session.preload(NodeModel(model_id=1, node_id="n1", status="downloading", desired_state="present"))
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING", progress=20)
        )

        t = await session.get(Task, "t1")
        assert t.progress == 80  # 不回退到 20

    async def test_orphan_event_still_recorded(self, monkeypatch):
        session = FakeSession()
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="ghost", node_id="n1", status="RUNNING")
        )
        assert ack.ok and "orphan" in (ack.message or "")
        evs = session.all(TaskEvent)
        assert len(evs) == 1 and evs[0].task_id == "ghost"

    async def test_stage_frozen_after_terminal(self, monkeypatch):
        """终态后 stage 冻结：stale 报告不得改写「死在哪一步」的最后快照。"""
        session = FakeSession()
        _task(session, status="FAILED", stage="downloading")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING", stage="verifying")
        )

        t = await session.get(Task, "t1")
        assert t.status == "FAILED" and t.stage == "downloading"  # 快照未动

    async def test_projector_error_isolated(self, monkeypatch):
        """单个 projector 异常不阻断状态机与其他投影（观测不反噬）。"""
        session = FakeSession()
        _task(session, status="PENDING")
        session.preload(NodeModel(model_id=1, node_id="n1", status="absent", desired_state="present"))
        _patch_db(monkeypatch, session)

        async def _boom(db, task, evt):
            raise RuntimeError("projector exploded")

        monkeypatch.setitem(ti_mod.task_projections.TASK_PROJECTORS, "MODEL_PLACEMENT", [_boom])

        ack = await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING", stage="downloading")
        )
        assert ack.ok
        t = await session.get(Task, "t1")
        assert t.status == "RUNNING" and t.stage == "downloading"  # 状态机照常推进

    async def test_image_pull_progress_projects_deploy_log(self, monkeypatch):
        """IMAGE_PULL 进度点经注册表落 deploy_log（task_id 真填、step=pull_image）。"""
        session = FakeSession()
        session.preload(
            Task(
                id="t2",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc123",
                node_id="n1",
                type="PULL_IMAGE",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        _deployment(session, status="creating")
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id="dep1",
                role="full",
                rank=0,
                assigned_node="n1",
                status="PENDING",
                spec={},
            )
        )
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_task_event(
            TaskEventPayload(
                task_id="t2",
                node_id="n1",
                status="RUNNING",
                progress=10,
                stage="pulling",
                extra={"bytes_done": 5 * 2**30, "bytes_total": 10 * 2**30},
            )
        )
        assert ack.ok
        logs = session.all(DeployLog)
        assert len(logs) == 1
        assert logs[0].task_id == "t2" and logs[0].step == "pull_image"
        assert logs[0].deployment_id == "dep1" and logs[0].node_id == "n1"
        assert logs[0].message == "已拉取 5.0GB / 10.0GB"
        # 节流清理（终态后 _LOG_THROTTLE 不残留）
        ti_mod.task_projections.reset_log_throttle()


class TestSyncNodeState:
    async def test_redispatches_cancel_on_drift(self, monkeypatch):
        """Manager=CANCELLED 而 node 仍 RUNNING → 重发 cancel。"""
        session = FakeSession()
        _task(session, status="CANCELLED")
        _patch_db(monkeypatch, session)
        cancels = []
        _patch_cancel(monkeypatch, cancels)
        _patch_redrive(monkeypatch, [])

        await ManagerTaskServiceImpl().sync_node_state("n1", [TaskState(id="t1", status="RUNNING")])

        assert cancels == [("n1", "t1")]

    async def test_no_cancel_when_in_sync(self, monkeypatch):
        session = FakeSession()
        _task(session, status="RUNNING")
        _patch_db(monkeypatch, session)
        cancels = []
        _patch_cancel(monkeypatch, cancels)
        _patch_redrive(monkeypatch, [])

        await ManagerTaskServiceImpl().sync_node_state("n1", [TaskState(id="t1", status="RUNNING")])

        assert cancels == []

    async def test_redrive_invoked_on_reconnect(self, monkeypatch):
        """node 重连 sync → 触发 redrive_pending(node_id)（遗留 PENDING 重发）。"""
        session = FakeSession()
        _task(session, status="RUNNING")
        _patch_db(monkeypatch, session)
        _patch_cancel(monkeypatch, [])
        redrive_calls = []
        _patch_redrive(monkeypatch, redrive_calls)

        await ManagerTaskServiceImpl().sync_node_state("n1", [TaskState(id="t1", status="RUNNING")])

        assert redrive_calls == ["n1"]

    async def test_replays_lost_terminal_report(self, monkeypatch):
        """node 已报终态但 Manager 仍 RUNNING（终态上报丢失）→ 重放终态，不再永久卡死。"""
        session = FakeSession()
        task = _task(
            session, status="RUNNING", resource_type="INSTANCE", resource_key="instance:i1:0", type="START_INSTANCE"
        )
        _patch_db(monkeypatch, session)
        _patch_cancel(monkeypatch, [])
        _patch_redrive(monkeypatch, [])

        await ManagerTaskServiceImpl().sync_node_state(
            "n1", [TaskState(id="t1", status="FAILED", last_error="容器崩溃")]
        )

        assert task.status == "FAILED"  # 重放生效
        assert task.last_error == "容器崩溃"
        assert task.finished_at is not None


def _instance(session, status="PENDING", **kw):
    defaults = dict(
        instance_id="i1",
        deployment_id="dep1",
        role="full",
        rank=0,
        assigned_node="n1",
        status=status,
        spec={},
    )
    defaults.update(kw)
    return session.preload(Instance(**defaults))


def _deployment(session, status="creating", phase="PULL_IMAGE"):
    return session.preload(
        ModelDeployment(
            deployment_id="dep1",
            cluster_id=1,
            name="d",
            model_id=1,
            engine="vllm",
            status=status,
            phase=phase,
        )
    )


class TestReportInstanceStatus:
    async def test_ready_writes_status_endpoint_timestamps(self, monkeypatch):
        session = FakeSession()
        _deployment(session)
        _instance(session, status="RUNNING")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1",
                instance_id="i1",
                node_id="n1",
                status="READY",
                container_id="c-abc",
                endpoint="http://10.0.1.5:8000",
            )
        )
        assert ack.ok

        inst = await session.get(Instance, "i1")
        assert inst.status == "READY"
        assert inst.container_id == "c-abc"
        assert inst.endpoint == "http://10.0.1.5:8000"
        assert inst.ready_at is not None

    async def test_starting_sets_started_at_and_clears_error(self, monkeypatch):
        session = FakeSession()
        _deployment(session)
        _instance(session, status="FAILED", last_error="上轮崩溃")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="STARTING")
        )

        inst = await session.get(Instance, "i1")
        assert inst.status == "STARTING"
        assert inst.started_at is not None
        assert inst.last_error is None  # 新一轮启动清空历史失败原因

    async def test_failed_writes_last_error(self, monkeypatch):
        session = FakeSession()
        _deployment(session)
        _instance(session, status="RUNNING")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1",
                instance_id="i1",
                node_id="n1",
                status="FAILED",
                message="readiness timeout",
            )
        )

        inst = await session.get(Instance, "i1")
        assert inst.status == "FAILED" and inst.last_error == "readiness timeout"

    async def test_stopped_is_terminal(self, monkeypatch):
        """STOPPED 终态不被 stale 上报覆盖。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="STOPPED")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="RUNNING")
        )
        assert ack.ok and "已停止" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "STOPPED"  # 未被覆盖

    async def test_rejects_starting_after_deployment_stopped(self, monkeypatch):
        """部署已 stopped/deleting → 拒收 STARTING/RUNNING（防 stale 上报复活实例）。"""
        session = FakeSession()
        _deployment(session, status="stopped")
        _instance(session, status="STOPPING")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="RUNNING")
        )
        assert ack.ok and "rejected" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "STOPPING"  # 未复活

    async def test_orphan_instance_ack_ok(self, monkeypatch):
        session = FakeSession()
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="ghost", node_id="n1", status="READY")
        )
        assert ack.ok and "不存在" in (ack.message or "")

    async def test_transition_projects_deploy_log(self, monkeypatch):
        """真实状态迁移（RUNNING→READY）→ 投影一条 deploy_log（per-node 实例生命周期）。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="RUNNING")
        _patch_db(monkeypatch, session)
        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="READY")
        )
        logs = session.all(DeployLog)
        assert len(logs) == 1
        lg = logs[0]
        assert lg.deployment_id == "dep1" and lg.node_id == "n1"
        assert lg.step == "head_health_check" and lg.status == "success" and lg.level == "info"
        assert lg.extra["instance_status"] == "READY"

    async def test_no_transition_no_deploy_log(self, monkeypatch):
        """同态重报（READY→READY）不投影——防节点周期重报刷重复行。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)
        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="READY")
        )
        assert session.all(DeployLog) == []

    async def test_failed_projects_error_log(self, monkeypatch):
        """迁移到 FAILED → 投影 level=error / status=failed 的 deploy_log。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="RUNNING")
        _patch_db(monkeypatch, session)
        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="FAILED", message="OOM")
        )
        logs = session.all(DeployLog)
        assert len(logs) == 1
        assert logs[0].level == "error" and logs[0].status == "failed"
        assert logs[0].step == "start_instance" and logs[0].message == "OOM"


class TestReportInstanceCrashed:
    """CRASHED 上报守卫：仅 READY/CRASHED 实例接受；重复上报仅刷新 last_error。"""

    async def test_ready_accepts_crashed(self, monkeypatch):
        """READY（曾对外服务）→ 接受 CRASHED + 记 last_error + 投影 deploy_log。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1",
                instance_id="i1",
                node_id="n1",
                status="CRASHED",
                message="容器异常退出（exit=137）",
            )
        )
        assert ack.ok

        inst = await session.get(Instance, "i1")
        assert inst.status == "CRASHED"
        assert inst.last_error == "容器异常退出（exit=137）"
        logs = session.all(DeployLog)
        assert len(logs) == 1
        assert logs[0].step == "running" and logs[0].status == "failed"
        assert logs[0].message == "容器异常退出（exit=137）"

    async def test_repeat_crashed_refreshes_error_without_new_log(self, monkeypatch):
        """CRASHED→CRASHED 重报（monitor TTL 到期）→ 仅刷新 last_error，不刷重复行。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)
        impl = ManagerTaskServiceImpl()
        await impl.report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED", message="第一次"
            )
        )
        await impl.report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED", message="第二次"
            )
        )

        inst = await session.get(Instance, "i1")
        assert inst.status == "CRASHED" and inst.last_error == "第二次"
        assert len(session.all(DeployLog)) == 1  # 只有第一次迁移的投影行

    async def test_rejects_when_starting(self, monkeypatch):
        """实例 STARTING（启动/停止窗口竞态）→ 拒收（ack ok + 状态未变）。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="STARTING")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        assert ack.ok and "rejected" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "STARTING"

    async def test_rejects_when_failed(self, monkeypatch):
        """实例 FAILED（启动失败走原通道）→ 拒收。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="FAILED", last_error="readiness timeout")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        assert ack.ok and "rejected" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "FAILED" and inst.last_error == "readiness timeout"

    async def test_rejects_when_stopping(self, monkeypatch):
        """实例 STOPPING（合法停止进行中）→ 拒收，不与 StopService 打架。"""
        session = FakeSession()
        _deployment(session, status="creating", phase="STOPPING")
        _instance(session, status="STOPPING")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        assert ack.ok and "rejected" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "STOPPING"

    async def test_rejects_when_stopped(self, monkeypatch):
        """实例已 STOPPED → 终态守卫先挡（同既有语义）。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="STOPPED")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        assert ack.ok and "已停止" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "STOPPED"

    async def test_rejects_when_deployment_terminal(self, monkeypatch):
        """部署已终态（delete 窗口内实例仍 READY）→ 拒收 CRASHED，不落误导性日志。"""
        session = FakeSession()
        _deployment(session, status="deleting")
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)

        ack = await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        assert ack.ok and "rejected" in (ack.message or "")
        inst = await session.get(Instance, "i1")
        assert inst.status == "READY"  # 状态未回退
        assert session.all(DeployLog) == []  # 不落误导性崩溃行

    async def test_crashed_log_level_is_error(self, monkeypatch):
        """READY→CRASHED 投影的 deploy_log 行 level=error（与启动失败同级，前端按
        level 过滤时运行期崩溃不降权）。"""
        session = FakeSession()
        _deployment(session)
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED", message="容器异常退出"
            )
        )

        logs = session.all(DeployLog)
        assert len(logs) == 1
        assert logs[0].level == "error"


class TestDeploymentHealthProjection:
    """运行期健康投影：CRASHED → 部署 degraded + 崩溃计数；全实例就绪 → healthy。

    覆盖 deployment_service.record_deployment_crash 与 report_instance_status →
    deployment_projection.project_upwards 的接线（仅 status=running 期维护；重报不重复计数）。
    """

    async def test_crash_sets_degraded_and_bumps_count(self, monkeypatch):
        """running 部署 + READY 实例 → 接受 CRASHED → 部署 degraded + crash_count=1。"""
        session = FakeSession()
        _deployment(session, status="running")
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(
                deployment_id="dep1",
                instance_id="i1",
                node_id="n1",
                status="CRASHED",
                message="容器异常退出（exit=137）",
            )
        )

        dep = await session.get(ModelDeployment, "dep1")
        assert dep.health_status == "degraded"
        assert dep.crash_count == 1
        assert dep.last_crash_at is not None

    async def test_repeat_crash_does_not_double_count(self, monkeypatch):
        """CRASHED→CRASHED 重报（monitor TTL 到期）不算第二次崩溃。"""
        session = FakeSession()
        _deployment(session, status="running")
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)
        impl = ManagerTaskServiceImpl()
        await impl.report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        await impl.report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        dep = await session.get(ModelDeployment, "dep1")
        assert dep.crash_count == 1

    async def test_ready_after_crash_restores_healthy(self, monkeypatch):
        """崩溃自愈后全实例就绪 → 部署回 healthy（生命周期 status 仍是 running）。"""
        session = FakeSession()
        _deployment(session, status="running")
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)
        impl = ManagerTaskServiceImpl()
        await impl.report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        dep = await session.get(ModelDeployment, "dep1")
        assert dep.health_status == "degraded"

        await impl.report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="READY")
        )
        dep = await session.get(ModelDeployment, "dep1")
        assert dep.health_status == "healthy"
        assert dep.status == "running"  # 生命周期不动
        assert dep.crash_count == 1  # 崩溃计数保留（历史可查）

    async def test_crash_during_creating_not_counted(self, monkeypatch):
        """creating 期实例崩溃（容器阶段收敛管辖）不投影运行期健康/计数。"""
        session = FakeSession()
        _deployment(session, status="creating")
        _instance(session, status="READY")
        _patch_db(monkeypatch, session)

        await ManagerTaskServiceImpl().report_instance_status(
            InstanceStatusPayload(deployment_id="dep1", instance_id="i1", node_id="n1", status="CRASHED")
        )
        dep = await session.get(ModelDeployment, "dep1")
        assert dep.health_status is None
        # 真库 NOT NULL default 0；FakeSession 不跑 server_default，None 也是「未计数」
        assert dep.crash_count in (None, 0)
