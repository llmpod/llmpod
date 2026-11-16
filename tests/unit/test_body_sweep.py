"""BodyStore 轮转 sweep 循环测试（apps/proxyserver/background.sweep_body_store_once）。

- 单轮：清点 → 策略选块 → 删除；空目录零动作；
- 单轮限量：选中数超上限时只删上限个并报「有积压」，下一轮继续；
- 自调度循环：积压期隔快速重试间隔，清完回常规间隔（宽松时距断言，不测精确节拍）。
"""

from __future__ import annotations

import asyncio
import os
import time

import llmpod.apps.proxyserver.background as background
from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
from llmpod.apps.proxyserver.retention import TimeRetentionPolicy
from llmpod.core.taskwatch import BackgroundTaskManager


def _seed_old_block(store: LocalFileBodyStore, content: str, age_days: float) -> None:
    """写入块并回拨 mtime 到指定块龄。"""
    h = store.put(content)
    path = store._root / "blocks" / h[:2] / f"{h}.gz"
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


class TestSweepOnce:
    async def test_removes_expired_and_reports_no_backlog(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        _seed_old_block(store, "old", age_days=10)
        store.put("fresh")
        policy = TimeRetentionPolicy(days=7)

        backlog = await background.sweep_body_store_once(store, policy)

        assert backlog is False
        listed = store.list_blocks()
        assert len(listed) == 1  # 只剩 fresh

    async def test_empty_store_is_noop(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)

        backlog = await background.sweep_body_store_once(store, TimeRetentionPolicy(days=7))

        assert backlog is False

    async def test_capped_pass_reports_backlog_and_drains_next_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(background, "_SWEEP_MAX_REMOVALS_PER_PASS", 2)
        store = LocalFileBodyStore(root=tmp_path)
        for i in range(5):
            _seed_old_block(store, f"old-{i}", age_days=10)
        policy = TimeRetentionPolicy(days=7)

        first = await background.sweep_body_store_once(store, policy)
        assert first is True  # 选中 5 > 上限 2 → 有积压
        assert len(store.list_blocks()) == 3

        second = await background.sweep_body_store_once(store, policy)
        assert second is True  # 选中 3 仍超上限 2
        assert len(store.list_blocks()) == 1

        third = await background.sweep_body_store_once(store, policy)
        assert third is False  # 选中 1 ≤ 上限 → 积压清完
        assert store.list_blocks() == []

    async def test_nothing_selected_keeps_all(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        store.put("fresh-only")
        policy = TimeRetentionPolicy(days=7)

        backlog = await background.sweep_body_store_once(store, policy)

        assert backlog is False
        assert len(store.list_blocks()) == 1


class TestSelfScheduling:
    async def test_loop_drains_backlog_and_stops(self, tmp_path, monkeypatch):
        """自调度循环吃掉积压（上限 1/轮 × 10ms 快速重试 → 秒级清完），停机干净。"""
        monkeypatch.setattr(background, "_SWEEP_MAX_REMOVALS_PER_PASS", 1)
        monkeypatch.setattr(background, "_SWEEP_BACKLOG_RETRY_SECONDS", 0.01)
        store = LocalFileBodyStore(root=tmp_path)
        for i in range(3):
            _seed_old_block(store, f"old-{i}", age_days=10)

        manager = BackgroundTaskManager()
        try:
            background._start_body_sweep(
                manager, store, TimeRetentionPolicy(days=7), interval_seconds=3600
            )
            await asyncio.sleep(0.5)  # 3 块 × 上限 1/轮 × 10ms 重试 → 秒级清完
            assert len(store.list_blocks()) == 0
        finally:
            await manager.stop()
