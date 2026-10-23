"""task_ops.ensure_task_row 集成测试：active 去重 / attempt 递增与上限 / PENDING 重发。

FakeSession（单 where + Python 过滤）+ 预置历史 task 行。
"""

from __future__ import annotations

from _fakes import FakeSession

from llmpod.apps.manager.deployment.task_ops import MAX_AUTO_ATTEMPTS, ensure_task_row, task_to_payload
from llmpod.models.task import Task


def _terminal(session, attempt, status="FAILED"):
    return session.preload(
        Task(
            id=f"t{attempt}",
            resource_type="IMAGE_PULL",
            resource_key="image:n1:abc",
            node_id="n1",
            type="PULL_IMAGE",
            status=status,
            attempt=attempt,
            payload={"image": "img:v1"},
        ),
    )


async def _ensure(session, **kw):
    defaults = dict(
        resource_type="IMAGE_PULL",
        resource_key="image:n1:abc",
        node_id="n1",
        task_type="PULL_IMAGE",
        payload={"image": "img:v1"},
    )
    defaults.update(kw)
    return await ensure_task_row(session, **defaults)


class TestEnsureTaskRow:
    async def test_creates_first_attempt(self):
        session = FakeSession()
        task, need_dispatch, denial = await _ensure(session)
        await session.flush()
        assert need_dispatch is True
        assert task.status == "PENDING" and task.attempt == 1
        assert task.payload == {"image": "img:v1"}

    async def test_active_running_not_redispatched(self):
        session = FakeSession()
        session.preload(
            Task(
                id="t1",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                type="PULL_IMAGE",
                status="RUNNING",
                attempt=1,
                payload={},
            )
        )
        task, need_dispatch, denial = await _ensure(session)
        assert task is not None and task.id == "t1"
        assert need_dispatch is False  # RUNNING = node 在跑，不打扰

    async def test_pending_redispatched(self):
        session = FakeSession()
        session.preload(
            Task(
                id="t1",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                type="PULL_IMAGE",
                status="PENDING",
                attempt=1,
                payload={},
            )
        )
        task, need_dispatch, denial = await _ensure(session)
        assert task is not None and task.id == "t1"
        assert need_dispatch is True  # 下发失败遗留 → 重发

    async def test_attempt_increments_after_terminal(self):
        session = FakeSession()
        _terminal(session, 1)
        task, need_dispatch, denial = await _ensure(session)
        assert need_dispatch is True and task.attempt == 2

    async def test_over_attempt_cap_returns_none(self):
        session = FakeSession()
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal(session, i)
        task, need_dispatch, denial = await _ensure(session)
        assert task is None and need_dispatch is False

    async def test_success_history_does_not_trigger_cap(self):
        """SUCCESS 历史不清零 attempt 编号（唯一索引单调），但不应触发重试上限。

        修前：attempt=max+1 后按 attempt>MAX 判超限——连续 SUCCESS（用户手动 stop/start）
        累积 attempt，3 次后正常操作被拒。修后：超限只看末尾连续 FAILED，SUCCESS 打断
        失败链 → 第 4 次操作仍建 task（attempt 单调递增到 4 但不拒绝）。
        """
        session = FakeSession()
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            _terminal(session, i, status="SUCCESS")
        task, need_dispatch, denial = await _ensure(session)
        assert task is not None and task.attempt == 4
        assert need_dispatch is True

    async def test_fail_streak_resets_after_success(self):
        """末尾 SUCCESS 打断失败链 → 连续失败归零，不误触上限（即便历史有 FAILED）。"""
        session = FakeSession()
        _terminal(session, 1, status="FAILED")
        _terminal(session, 2, status="FAILED")
        _terminal(session, 3, status="SUCCESS")  # 最新一次成功，打断
        task, _, _d = await _ensure(session)
        assert task is not None and task.attempt == 4  # 不因前两次 FAILED 拒绝

    async def test_other_resource_key_isolated(self):
        """不同 resource_key 的 task 不影响本键的 attempt 计算。"""
        session = FakeSession()
        session.preload(
            Task(
                id="t9",
                resource_type="IMAGE_PULL",
                resource_key="image:n2:abc",
                node_id="n2",
                type="PULL_IMAGE",
                status="FAILED",
                attempt=3,
                payload={},
            )
        )
        task, _, _d = await _ensure(session)
        assert task.attempt == 1  # 只看本 resource_key 的历史


class TestTaskToPayload:
    def test_wire_projection(self):
        from llmpod.tunnel.schemas import TaskPayload

        t = Task(
            id="t1",
            resource_type="IMAGE_PULL",
            resource_key="image:n1:abc",
            node_id="n1",
            type="PULL_IMAGE",
            status="PENDING",
            attempt=2,
            progress=0,
            timeout_sec=None,
            payload={"image": "img:v1"},
        )
        p = task_to_payload(t)
        assert isinstance(p, TaskPayload)
        assert p.model_dump() == {
            "id": "t1",
            "type": "PULL_IMAGE",
            "resource_type": "IMAGE_PULL",
            "resource_key": "image:n1:abc",
            "node_id": "n1",
            "status": "PENDING",
            "attempt": 2,
            "progress": 0,
            "timeout_sec": None,
            "payload": {"image": "img:v1"},
        }


class TestHangTimeoutDefaults:
    """看门狗默认值接线：不显式传 timeout_sec 时按任务类型兜底，防 executor 挂死无人管。"""

    async def test_pull_image_default_armed(self):
        session = FakeSession()
        task, _, _ = await _ensure(session)
        assert task.timeout_sec == 2 * 3600

    async def test_stop_instance_default_armed(self):
        session = FakeSession()
        task, _, _ = await _ensure(
            session, resource_type="INSTANCE", resource_key="instance:e1:0:stop", task_type="STOP_INSTANCE"
        )
        assert task.timeout_sec == 300

    async def test_explicit_value_wins(self):
        session = FakeSession()
        task, _, _ = await _ensure(session, timeout_sec=42)
        assert task.timeout_sec == 42


class TestPullHangTimeoutBuckets:
    """PULL 看门狗按镜像体积分桶：小镜像快失败、大镜像给足时间、封顶 2h。"""

    def test_unknown_size_falls_back_to_default(self):
        from llmpod.apps.manager.deployment.task_ops import pull_hang_timeout

        assert pull_hang_timeout(None) == 2 * 3600
        assert pull_hang_timeout(0) == 2 * 3600

    def test_small_image_hits_floor(self):
        from llmpod.apps.manager.deployment.task_ops import pull_hang_timeout

        assert pull_hang_timeout(0.5) == 1800  # 地板 30min
        assert pull_hang_timeout(1) == 2700  # 30min + 1×15min

    def test_large_image_capped(self):
        from llmpod.apps.manager.deployment.task_ops import pull_hang_timeout

        assert pull_hang_timeout(20) == 2 * 3600  # 封顶
        assert pull_hang_timeout(5) == 1800 + 5 * 900


class TestExhaustCooldown:
    """超限冷却：末条 FAILED 距今超冷却窗 → 重开一轮（键跨部署共享的失败传染衰减）。"""

    async def test_cooldown_elapsed_reopens(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(hours=2)
        session = FakeSession()
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            t = Task(
                id=f"t{i}",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                type="PULL_IMAGE",
                status="FAILED",
                attempt=i,
                payload={"image": "img:v1"},
                finished_at=old,
            )
            session.preload(t)
        task, _, denial = await _ensure(session, exhaust_cooldown_s=3600)
        assert denial is None and task is not None
        assert task.attempt == MAX_AUTO_ATTEMPTS + 1  # 重开一轮

    async def test_within_cooldown_still_exhausted(self):
        from datetime import datetime, timedelta, timezone

        session = FakeSession()
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            session.preload(
                Task(
                    id=f"t{i}",
                    resource_type="IMAGE_PULL",
                    resource_key="image:n1:abc",
                    node_id="n1",
                    type="PULL_IMAGE",
                    status="FAILED",
                    attempt=i,
                    payload={"image": "img:v1"},
                    finished_at=recent,
                )
            )
        task, _, denial = await _ensure(session, exhaust_cooldown_s=3600)
        assert denial == "exhausted" and task is None

    async def test_no_cooldown_param_keeps_terminal_semantics(self):
        from datetime import datetime, timedelta, timezone

        session = FakeSession()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        for i in range(1, MAX_AUTO_ATTEMPTS + 1):
            session.preload(
                Task(
                    id=f"t{i}",
                    resource_type="IMAGE_PULL",
                    resource_key="image:n1:abc",
                    node_id="n1",
                    type="PULL_IMAGE",
                    status="FAILED",
                    attempt=i,
                    payload={"image": "img:v1"},
                    finished_at=old,
                )
            )
        # 不传 exhaust_cooldown_s（START/STOP 语义）：超限即终态，不因时间衰减
        task, _, denial = await _ensure(session)
        assert denial == "exhausted" and task is None
