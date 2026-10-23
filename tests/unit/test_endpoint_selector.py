"""端点选择器单元测试：加权随机 + 优先级组 + 进程内健康冷却。

确定性：注入 clock 与 seeded random，锁定选择/冷却/恢复行为。
"""

from __future__ import annotations

import random

from tests.unit._fakes import FixedClock, route_target
from llmpod.apps.proxyserver.routing import EndpointSelector


class TestSelect:
    def test_weighted_pick_within_top_priority_group(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(42))
        targets = (
            route_target(1, weight=1, priority=5),
            route_target(2, weight=1, priority=5),
            route_target(3, weight=9, priority=1),
        )

        picks = {sel.select(targets).target for _ in range(200)}
        # 高优先级组（5）可用时绝不选低组（1）
        assert {p.endpoint_id for p in picks} <= {1, 2}
        # 权重相等 → 两组都会被选到（seeded，200 次覆盖）
        assert {p.endpoint_id for p in picks} == {1, 2}

    def test_falls_to_lower_priority_when_top_all_cooldown(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(42), failure_threshold=1)
        targets = (route_target(1, priority=5), route_target(2, priority=1))

        sel.report_failure(1)  # threshold=1 → 立即冷却

        assert sel.select(targets).target.endpoint_id == 2

    def test_all_cooldown_returns_none(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(42), failure_threshold=1)
        targets = (route_target(1),)

        sel.report_failure(1)

        assert sel.select(targets) is None

    def test_empty_targets_returns_none(self):
        sel = EndpointSelector(clock=FixedClock(), rng=random.Random(0))
        assert sel.select(()) is None


class TestCooldownLifecycle:
    def test_consecutive_failures_below_threshold_not_cooldown(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(0), failure_threshold=3)
        targets = (route_target(1),)

        sel.report_failure(1)
        sel.report_failure(1)

        assert sel.select(targets) is not None  # 未达阈值仍可选

    def test_success_resets_failure_streak(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(0), failure_threshold=2)
        targets = (route_target(1),)

        sel.report_failure(1)
        sel.report_success(1)  # 清零
        sel.report_failure(1)  # 重新计数，未达 2

        assert sel.select(targets) is not None

    def test_cooldown_expires_after_window(self):
        clock = FixedClock()
        sel = EndpointSelector(clock=clock, rng=random.Random(0), failure_threshold=1, cooldown_seconds=60)
        targets = (route_target(1),)

        sel.report_failure(1)
        assert sel.select(targets) is None

        clock.advance(61)
        assert sel.select(targets) is not None  # 窗口过后恢复
