"""ensure_present 部署期复核语义单测：present 不再盲信短路 + attempt 按连续失败计数。

背景：node_model.status=present 曾是 Manager 短路依据，但磁盘真相（目录被清/文件被删）
只有 node 看得见——脏 present 直通容器导致空权重上线。修后每次部署 ensure 一个复核
任务，由 node 侧 check-existing 快路径裁决；attempt 若按 max+1 无脑累加，部署几次即
被误判超限，故改为「最近连续 FAILED 数 + 1」。
"""

from __future__ import annotations

import datetime
from contextlib import asynccontextmanager

from llmpod.apps.manager.deployment import placement as pl
from llmpod.apps.manager.deployment.placement import (
    PlacementService,
    _next_attempt,
)
from llmpod.apps.manager.deployment.task_ops import trailing_fail_count
from llmpod.apps.manager.deployment.constants import NodeModelState
from llmpod.config.constants import TaskResourceType
from llmpod.models.model import Model
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task

from tests.integration._fakes import FakeSession

_T0 = datetime.datetime(2026, 8, 18, 1, 0, 0, tzinfo=datetime.timezone.utc)


def _task(status: str, attempt: int, i: int) -> Task:
    return Task(
        id=f"t-{i}",
        resource_type=TaskResourceType.MODEL_PLACEMENT,
        resource_key="model:7:n1",
        node_id="n1",
        type="ENSURE_MODEL",
        status=status,
        attempt=attempt,
        payload={},
        created_at=_T0 + datetime.timedelta(minutes=i),
    )


class TestNextAttempt:
    def test_empty_is_one(self):
        assert _next_attempt([]) == 1

    def test_monotonic_max_plus_one(self):
        """编号单调（max+1）：(resource_key, attempt) 唯一索引禁止复用历史编号。"""
        assert _next_attempt([_task("SUCCESS", 1, 0), _task("FAILED", 2, 1)]) == 3
        assert _next_attempt([_task("SUCCESS", 1, 0)]) == 2


class TestTrailingFailCount:
    def test_empty_is_zero(self):
        assert trailing_fail_count([]) == 0

    def test_all_success_is_zero(self):
        assert trailing_fail_count([_task("SUCCESS", 1, 0), _task("SUCCESS", 2, 1)]) == 0

    def test_trailing_failures_counted(self):
        rows = [_task("SUCCESS", 1, 0), _task("FAILED", 2, 1), _task("FAILED", 3, 2)]
        assert trailing_fail_count(rows) == 2

    def test_three_trailing_failures_hit_cap(self):
        rows = [_task("SUCCESS", 1, 0)] + [_task("FAILED", n, n) for n in (2, 3, 4)]
        assert trailing_fail_count(rows) == 3  # >= MAX_AUTO_ATTEMPTS → 调用方标 FAILED 拒建


class TestEnsurePresentReverify:
    def _setup(self, nm_status: str, history: list[Task]):
        session = FakeSession()
        m = Model(
            name="x",
            model_type="llm",
            source="huggingface",
            source_url="u",
            source_kind="transformers_folder",
            hash_mode="none",
        )
        m.id = 7
        session.preload(m)
        nm = session.preload(
            NodeModel(
                model_id=7,
                node_id="n1",
                desired_state="absent",
                status=nm_status,
                local_dir="/data/models/model-7",
            )
        )
        for t in history:
            session.preload(t)
        return session, nm

    async def _ensure(self, session, dispatched):
        @asynccontextmanager
        async def _ctx():
            yield session

        orig = pl.get_db_context
        pl.get_db_context = lambda: _ctx()
        try:
            svc = PlacementService()

            async def _disp(node_id, payload, secrets=None):
                dispatched.append((node_id, payload))

            svc._dispatch_ensure_payload = _disp
            return await svc.ensure_present(7, "n1")
        finally:
            pl.get_db_context = orig

    async def test_present_still_creates_reverify_task(self):
        """present 只是缓存声明：仍建复核任务（node check-existing 秒级裁决真伪）。"""
        session, nm = self._setup("present", [_task("SUCCESS", 1, 0)])
        dispatched: list = []

        tid = await self._ensure(session, dispatched)

        assert tid is not None  # 不再返回 None 短路
        new = [t for t in session.all(Task) if t.id == tid][0]
        assert new.attempt == 2  # 编号单调 max+1（唯一索引禁复用历史编号）
        assert len(dispatched) == 1
        assert nm.desired_state == "present"

    async def test_active_task_reused_not_duplicated(self):
        session, _nm = self._setup("downloading", [_task("PENDING", 1, 0)])
        dispatched: list = []

        tid = await self._ensure(session, dispatched)

        assert tid == "t-0"  # 复用既有 active 任务
        assert len(session.all(Task)) == 1
        assert len(dispatched) == 1  # PENDING → redrive 重发

    async def test_consecutive_failures_over_cap_refuses(self):
        session, nm = self._setup(
            "absent",
            [
                _task("SUCCESS", 1, 0),
                _task("FAILED", 2, 1),
                _task("FAILED", 3, 2),
                _task("FAILED", 4, 3),
            ],
        )
        dispatched: list = []

        tid = await self._ensure(session, dispatched)

        assert tid is None
        assert nm.status == NodeModelState.FAILED
        assert not dispatched
