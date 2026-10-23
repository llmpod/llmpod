"""task_projections 单测：注册表分发形状 + deploy_log 进度点节流 + 归属部署解析。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.constants import NodeModelState
from llmpod.apps.manager.services import task_projections as tp
from llmpod.config.constants import TaskResourceType, TaskStatus
from llmpod.models.deploy_log import DeployLog
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.tunnel.schemas import TaskEventPayload

from tests.integration._fakes import FakeSession


@pytest.fixture(autouse=True)
def _clean_throttle():
    tp.reset_log_throttle()
    yield
    tp.reset_log_throttle()


def _evt(**kw):
    defaults = dict(task_id="t1", node_id="n1", status=TaskStatus.RUNNING, progress=0)
    defaults.update(kw)
    return TaskEventPayload(**defaults)


def _pull_task(session, resource_key="image:n1:abc123", node_id="n1"):
    return session.preload(
        Task(
            id="t1",
            resource_type=TaskResourceType.IMAGE_PULL,
            resource_key=resource_key,
            node_id=node_id,
            type="PULL_IMAGE",
            status="RUNNING",
            attempt=1,
            payload={},
        )
    )


def _model_task(session, model_id=1, node_id="n1"):
    return session.preload(
        Task(
            id="t1",
            resource_type=TaskResourceType.MODEL_PLACEMENT,
            resource_key=f"model:{model_id}:{node_id}",
            node_id=node_id,
            type="ENSURE_MODEL",
            status="RUNNING",
            attempt=1,
            payload={},
        )
    )


def _deployment(session, deployment_id="dep1", status="creating", phase="PULL_IMAGE", model_id=1):
    return session.preload(
        ModelDeployment(
            deployment_id=deployment_id,
            cluster_id=1,
            name="d",
            model_id=model_id,
            engine="vllm",
            status=status,
            phase=phase,
        )
    )


def _instance(session, deployment_id="dep1", node="n1"):
    return session.preload(
        Instance(
            instance_id=f"i-{deployment_id}",
            deployment_id=deployment_id,
            role="full",
            rank=0,
            assigned_node=node,
            status="PENDING",
            spec={},
        )
    )


class TestRegistry:
    def test_registered_projectors_by_resource_type(self):
        # 裸 str 查找命中 StrEnum key（等值）；未注册类型 miss 空列表不炸
        assert tp.TASK_PROJECTORS.get("MODEL_PLACEMENT") == [
            tp.project_node_model,
            tp.project_deploy_log_progress,
        ]
        assert tp.TASK_PROJECTORS.get("IMAGE_PULL") == [tp.project_deploy_log_progress]
        assert not tp.TASK_PROJECTORS.get("INSTANCE")  # 瞬时任务无投影
        assert not tp.TASK_PROJECTORS.get("UNKNOWN_TYPE")  # 未知类型 miss 不炸
        assert tp.TASK_PROJECTORS.get(TaskResourceType.MODEL_PLACEMENT) is tp.TASK_PROJECTORS.get("MODEL_PLACEMENT")


class TestDeployLogProgress:
    async def test_first_event_logs(self):
        session = FakeSession()
        _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, session.all(Task)[0], _evt(stage="pulling", progress=0))

        logs = session.all(DeployLog)
        assert len(logs) == 1
        assert logs[0].deployment_id == "dep1" and logs[0].node_id == "n1"
        assert logs[0].task_id == "t1"  # task_id 从此真填
        assert logs[0].step == "pull_image" and logs[0].status == "running"
        assert logs[0].extra["trigger"] == "system"

    async def test_same_stage_small_delta_throttled(self):
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=0))
        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=3))
        # Δ<5% 且 Δt<5s → 不落行
        assert len(session.all(DeployLog)) == 1

    async def test_delta_ge_5_logs(self):
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=0))
        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=5))

        assert len(session.all(DeployLog)) == 2

    async def test_stage_migration_always_logs(self):
        session = FakeSession()
        task = _model_task(session)
        _deployment(session, phase="STAGING_MODEL")
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="downloading", progress=2))
        # 同进度但阶段迁移 downloading→verifying → 必落
        await tp.project_deploy_log_progress(session, task, _evt(stage="verifying", progress=2))

        logs = session.all(DeployLog)
        assert len(logs) == 2
        assert all(lg.step == "staging_model" for lg in logs)

    async def test_bytes_message(self):
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(
            session,
            task,
            _evt(stage="pulling", progress=50, extra={"bytes_done": 5 * 2**30, "bytes_total": 10 * 2**30}),
        )
        logs = session.all(DeployLog)
        assert logs[0].message == "已拉取 5.0GB / 10.0GB"

    async def test_bytes_structured_into_extra(self):
        """字节结构化写进 deploy_log.extra（消息文本之外的可渲染字段，logs/nodes 透出）。"""
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(
            session,
            task,
            _evt(stage="pulling", progress=50, extra={"bytes_done": 5 * 2**30, "bytes_total": 10 * 2**30}),
        )
        logs = session.all(DeployLog)
        assert logs[0].extra["progress"] == 50
        assert logs[0].extra["bytes_done"] == 5 * 2**30
        assert logs[0].extra["bytes_total"] == 10 * 2**30

        # 无字节的进度点（registry 降级心跳）：extra 不含 bytes 键
        await tp.project_deploy_log_progress(
            session,
            task,
            _evt(stage="pulling", progress=60),
        )
        assert "bytes_done" not in session.all(DeployLog)[-1].extra

    async def test_interval_elapsed_logs_despite_small_delta(self, monkeypatch):
        """慢拉取 Δ<5% 但距上条 ≥5s → 落一条心跳行（时间维度节流）。"""
        clock = {"now": 100.0}
        monkeypatch.setattr(tp.time, "monotonic", lambda: clock["now"])
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=10))
        assert len(session.all(DeployLog)) == 1

        clock["now"] += 6.0  # 超过 5s 间隔，Δ 仍 <5%
        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=12))
        assert len(session.all(DeployLog)) == 2

    async def test_rate_in_message_and_extra(self, monkeypatch):
        """节点带 bytes_per_sec → 消息附「（xx MB/s）」且 extra 结构化携带。"""
        clock = {"now": 100.0}
        monkeypatch.setattr(tp.time, "monotonic", lambda: clock["now"])
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(
            session,
            task,
            _evt(
                stage="pulling",
                progress=50,
                extra={
                    "bytes_done": 5 * 2**30,
                    "bytes_total": 10 * 2**30,
                    "bytes_per_sec": 30 * 2**20,
                },
            ),
        )
        logs = session.all(DeployLog)
        assert logs[0].message == "已拉取 5.0GB / 10.0GB（30.0MB/s）"
        assert logs[0].extra["bytes_per_sec"] == 30 * 2**20

        clock["now"] += 1.0  # 间隔与增量都不足 → 静默
        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=13))
        assert len(session.all(DeployLog)) == 1

    async def test_verifying_message(self):
        session = FakeSession()
        task = _model_task(session)
        _deployment(session, phase="STAGING_MODEL")
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="verifying", progress=0))
        logs = session.all(DeployLog)
        assert logs[0].message == "校验中"

    async def test_download_start_message(self):
        session = FakeSession()
        task = _model_task(session)
        _deployment(session, phase="STAGING_MODEL")
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="downloading", progress=0))
        logs = session.all(DeployLog)
        assert logs[0].message == "开始下载"

    async def test_terminal_pops_throttle_and_skips(self):
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="pulling", progress=0))
        await tp.project_deploy_log_progress(
            session, task, _evt(status=TaskStatus.SUCCESS, progress=100, stage="finished")
        )

        assert task.id not in tp._LOG_THROTTLE  # 终态清理
        assert len(session.all(DeployLog)) == 1  # 终态不落行（成功由时间线负责）

    async def test_unknown_stage_ignored(self):
        session = FakeSession()
        task = _pull_task(session)
        _deployment(session)
        _instance(session)

        await tp.project_deploy_log_progress(session, task, _evt(stage="some-new-stage"))
        assert session.all(DeployLog) == []


class TestNodeModelProjection:
    """present 只认权威信号（stage=finished / task SUCCESS）；model_path 单独出现不翻状态。"""

    def _node_model(self, session, status="downloading"):
        return session.preload(
            NodeModel(
                model_id=1,
                node_id="n1",
                desired_state="present",
                status=status,
                local_dir="/data/models/model-1",
            )
        )

    async def test_stale_model_path_alone_not_present(self):
        """带 model_path 的非权威事件（如 node 重连同步旧任务）不得翻 present。"""
        session = FakeSession()
        task = _model_task(session)
        nm = self._node_model(session, status="downloading")

        await tp.project_node_model(
            session,
            task,
            _evt(status=TaskStatus.RUNNING, extra={"model_path": "/data/models/model-1"}),
        )

        assert nm.status == NodeModelState.DOWNLOADING  # 仍按 task 状态投影
        assert nm.model_path is None  # 陈旧路径不记录

    async def test_success_with_model_path_marks_present(self):
        session = FakeSession()
        task = _model_task(session)
        nm = self._node_model(session, status="downloading")

        await tp.project_node_model(
            session,
            task,
            _evt(status=TaskStatus.SUCCESS, extra={"model_path": "/data/models/model-1"}),
        )

        assert nm.status == NodeModelState.PRESENT
        assert nm.model_path == "/data/models/model-1"

    async def test_finished_stage_with_model_path_marks_present(self):
        session = FakeSession()
        task = _model_task(session)
        nm = self._node_model(session, status="verifying")

        await tp.project_node_model(
            session,
            task,
            _evt(status=TaskStatus.RUNNING, stage="finished", extra={"model_path": "/data/models/model-1"}),
        )

        assert nm.status == NodeModelState.PRESENT
        assert nm.model_path == "/data/models/model-1"


class TestResolveDeployments:
    async def test_only_matching_phase_and_status(self):
        session = FakeSession()
        _pull_task(session)
        _deployment(session, deployment_id="dep-pulling", status="creating", phase="PULL_IMAGE")
        _deployment(session, deployment_id="dep-running", status="running", phase="RUNNING")
        _deployment(session, deployment_id="dep-staging", status="creating", phase="STAGING_MODEL")
        _instance(session, deployment_id="dep-pulling")
        _instance(session, deployment_id="dep-running")
        _instance(session, deployment_id="dep-staging")

        task = session.all(Task)[0]
        assert await tp._resolve_deployments(session, task) == ["dep-pulling"]

    async def test_model_task_filters_model_id(self):
        session = FakeSession()
        _model_task(session, model_id=7)
        _deployment(session, deployment_id="dep-a", phase="STAGING_MODEL", model_id=7)
        _deployment(session, deployment_id="dep-b", phase="STAGING_MODEL", model_id=8)
        _instance(session, deployment_id="dep-a")
        _instance(session, deployment_id="dep-b")

        task = session.all(Task)[0]
        assert await tp._resolve_deployments(session, task) == ["dep-a"]

    async def test_no_instances_no_deployments(self):
        session = FakeSession()
        _pull_task(session)

        assert await tp._resolve_deployments(session, session.all(Task)[0]) == []
