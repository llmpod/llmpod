"""tunnel.schemas.task 单测：wire 契约校验 + extra=ignore 兼容（registry coerce_params 对称）。"""

from __future__ import annotations

import pytest

from llmpod.tunnel.schemas import Ack, TaskEventPayload, TaskPayload, TaskState


class TestTaskPayload:
    def test_minimal_with_defaults(self):
        p = TaskPayload(
            id="t1",
            type="ENSURE_MODEL",
            resource_type="MODEL_PLACEMENT",
            resource_key="model:1:n1",
            node_id="n1",
        )
        assert p.status == "PENDING"
        assert p.attempt == 1 and p.progress == 0
        assert p.payload == {}

    def test_extra_fields_ignored(self):
        # node/registry 回传可能带额外字段（forward 兼容）；extra=ignore 不报错
        p = TaskPayload.model_validate(
            {
                "id": "t1",
                "type": "ENSURE_MODEL",
                "resource_type": "MODEL_PLACEMENT",
                "resource_key": "model:1:n1",
                "node_id": "n1",
                "created_at": "2026-08-08T00:00:00Z",  # wire 多余字段
                "payload": {"model_id": 1, "source": "huggingface"},
            }
        )
        assert p.payload["source"] == "huggingface"

    def test_missing_required_rejected(self):
        with pytest.raises(Exception):
            TaskPayload.model_validate({"id": "t1"})  # 缺 type/resource_type/...


class TestTaskEventPayload:
    def test_required_fields(self):
        e = TaskEventPayload(task_id="t1", node_id="n1", status="RUNNING")
        assert e.progress == 0 and e.stage is None and e.extra is None

    def test_extra_payload_kept(self):
        e = TaskEventPayload.model_validate(
            {
                "task_id": "t1",
                "node_id": "n1",
                "status": "SUCCESS",
                "stage": "finished",
                "extra": {"model_path": "/data/models/model-1"},
            }
        )
        assert e.stage == "finished"
        assert e.extra["model_path"] == "/data/models/model-1"


class TestTaskStateAndAck:
    def test_task_state_minimal(self):
        s = TaskState(id="t1", status="RUNNING")
        assert s.progress == 0 and s.last_error is None

    def test_ack_defaults(self):
        a = Ack()
        assert a.ok is True and a.message is None
        a2 = Ack.model_validate({"ok": False, "message": "busy"})
        assert a2.ok is False
