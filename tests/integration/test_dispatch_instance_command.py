"""dispatch.instance_start_command 编排逻辑单测（不起 HTTP、纯读库）。

验证：Instance 行 → instance_start_key 算 resource_key → Task 行取 attempt 最大 →
StartInstancePayload.spec 渲染完整 docker run；无 task 行 / payload 非法 → found=False；
dep / instance 缺失 → NotFound。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task
from _fakes import FakeSession


def _dep(dep_id="dep-1"):
    return ModelDeployment(deployment_id=dep_id, cluster_id=1, name="d", model_id=1, engine="vllm")


def _inst(inst_id="inst-1", dep="dep-1", engine_instance_id="eng-1"):
    """Instance 行：spec 带 engine_instance_id（instance_start_key 键源）。"""
    return Instance(
        instance_id=inst_id,
        deployment_id=dep,
        role="full",
        rank=0,
        assigned_node="node-a",
        status="READY",
        spec={"engine_instance_id": engine_instance_id, "node_rank": 0},
    )


def _task(resource_key, attempt=1, status="SUCCESS", payload=None, created=None):
    return Task(
        id=f"t-{resource_key}-{attempt}",
        resource_type="INSTANCE",
        resource_key=resource_key,
        node_id="node-a",
        type="START_INSTANCE",
        status=status,
        attempt=attempt,
        payload=payload if payload is not None else {},
        created_at=created or datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
    )


def _payload(image="vllm/vllm-openai:v0.23.0", **spec_overrides):
    """合法 StartInstancePayload dump（spec=DeploySpec 快照）。"""
    from llmpod.tunnel.schemas import DeploySpec, StartInstancePayload

    spec = DeploySpec(image=image, name="llmpod-dep-1-eng-1", **spec_overrides)
    return StartInstancePayload(
        deployment_id="dep-1",
        instance_id="eng-1",
        container_name="llmpod-dep-1-eng-1",
        spec=spec,
    ).model_dump(mode="json")


class TestInstanceStartCommand:
    async def test_found_renders_full_docker_run(self):
        session = FakeSession()
        session.preload(_dep())
        session.preload(_inst())
        session.preload(_task("instance:eng-1:0", payload=_payload(gpus=[0, 1], network_mode="host")))

        from llmpod.apps.manager.dispatch import instance_start_command

        item = await instance_start_command(session, "dep-1", "inst-1")

        assert item["found"] is True
        assert item["command"].startswith("docker run -d")
        assert "--name llmpod-dep-1-eng-1" in item["command"]
        assert "--network host" in item["command"]
        assert '--gpus "device=0,1"' in item["command"]
        assert item["image"] == "vllm/vllm-openai:v0.23.0"
        assert item["container_name"] == "llmpod-dep-1-eng-1"
        assert item["task_status"] == "SUCCESS"
        assert item["attempt"] == 1
        assert item["dispatched_at"].startswith("2026-09-07T12:00:00")

    async def test_picks_max_attempt_row(self):
        """同 resource_key 多行（重试历史）取 attempt 最大的——即最近一次实际下发。"""
        session = FakeSession()
        session.preload(_dep())
        session.preload(_inst())
        session.preload(_task("instance:eng-1:0", attempt=1, status="FAILED", payload=_payload(image="old:v1")))
        session.preload(_task("instance:eng-1:0", attempt=2, status="SUCCESS", payload=_payload(image="new:v2")))

        from llmpod.apps.manager.dispatch import instance_start_command

        item = await instance_start_command(session, "dep-1", "inst-1")
        assert item["image"] == "new:v2"
        assert item["attempt"] == 2

    async def test_no_task_row_found_false(self):
        """无 START task 行（重启窗口/未下发过）→ found=False 不报错。"""
        session = FakeSession()
        session.preload(_dep())
        session.preload(_inst())

        from llmpod.apps.manager.dispatch import instance_start_command

        item = await instance_start_command(session, "dep-1", "inst-1")
        assert item == {
            "found": False,
            "command": None,
            "image": None,
            "container_name": None,
            "task_status": None,
            "attempt": None,
            "dispatched_at": None,
        }

    async def test_invalid_payload_found_false_keeps_task_meta(self):
        """payload 形状漂移解析失败 → found=False，但 task 状态/attempt 仍回显。"""
        session = FakeSession()
        session.preload(_dep())
        session.preload(_inst())
        session.preload(_task("instance:eng-1:0", status="FAILED", payload={"junk": True}))

        from llmpod.apps.manager.dispatch import instance_start_command

        item = await instance_start_command(session, "dep-1", "inst-1")
        assert item["found"] is False
        assert item["command"] is None
        assert item["task_status"] == "FAILED"
        assert item["attempt"] == 1

    async def test_missing_deployment_raises_404(self):
        from llmpod.apps.manager.dispatch import instance_start_command
        from llmpod.core.exceptions import NotFound

        with pytest.raises(NotFound):
            await instance_start_command(FakeSession(), "nope", "inst-1")

    async def test_missing_instance_raises_404(self):
        session = FakeSession()
        session.preload(_dep())
        session.preload(_inst("inst-other"))

        from llmpod.apps.manager.dispatch import instance_start_command
        from llmpod.core.exceptions import NotFound

        with pytest.raises(NotFound):
            await instance_start_command(session, "dep-1", "inst-1")
