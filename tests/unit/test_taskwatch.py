"""taskwatch.supervise 单测：崩溃/意外返回重启、CancelledError 透传。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from llmpod.core.taskwatch import BackgroundTaskManager, supervise


async def test_crash_restarts_factory():
    runs = 0

    async def flaky():
        nonlocal runs
        runs += 1
        if runs < 3:
            raise RuntimeError("boom")
        raise asyncio.CancelledError  # 第三轮由外部「关闭」

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(supervise(flaky, name="t", restart_delay=0.01), timeout=2)
    assert runs == 3  # 崩溃两轮后第三轮拉起


async def test_unexpected_return_also_restarts():
    runs = 0

    async def returns():
        nonlocal runs
        runs += 1
        if runs < 2:
            return  # 意外返回（循环不该退出）
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(supervise(returns, name="t", restart_delay=0.01), timeout=2)
    assert runs == 2


async def test_cancelled_propagates_without_restart():
    runs = 0

    async def inner():
        nonlocal runs
        runs += 1
        raise asyncio.CancelledError

    task = asyncio.create_task(supervise(inner, name="t", restart_delay=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert runs == 1  # 不重启


async def test_manager_stops_all_loops_before_returning():
    manager = BackgroundTaskManager()
    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    def loop(index):
        async def run():
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped[index].set()

        return run

    manager.start(loop(0), name="first")
    manager.start(loop(1), name="second")
    await asyncio.gather(*(event.wait() for event in started))
    await manager.stop()
    assert all(event.is_set() for event in stopped)
    await manager.stop()
    with pytest.raises(RuntimeError, match="closed"):
        manager.start(loop(0), name="later")


async def test_manager_every_is_serial_and_survives_round_error():
    manager = BackgroundTaskManager()
    entered = asyncio.Event()
    release = asyncio.Event()
    third = asyncio.Event()
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        elif calls == 2:
            raise RuntimeError("one round failed")
        else:
            third.set()

    manager.every(work, name="periodic", interval=0.01, immediate=True)
    await entered.wait()
    await asyncio.sleep(0.03)
    assert calls == 1
    release.set()
    await asyncio.wait_for(third.wait(), timeout=1)
    await manager.stop()
    assert calls == 3


async def test_manager_every_delays_first_round_and_rejects_duplicate():
    manager = BackgroundTaskManager()
    started = asyncio.Event()

    async def work():
        started.set()

    manager.every(work, name="delayed", interval=0.03)
    with pytest.raises(ValueError, match="duplicate"):
        manager.start(work, name="delayed")
    await asyncio.sleep(0)
    assert not started.is_set()
    await asyncio.wait_for(started.wait(), timeout=1)
    await manager.stop()


async def test_manager_critical_loop_restarts_and_cancellation_does_not_restart(caplog):
    manager = BackgroundTaskManager()
    restarted = asyncio.Event()
    runs = 0

    async def loop():
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("crash")
        restarted.set()
        await asyncio.Event().wait()

    with caplog.at_level(logging.CRITICAL, logger="llmpod.core.taskwatch"):
        manager.start(loop, name="critical", restart_delay=0.01, failure_level=logging.CRITICAL)
        await asyncio.wait_for(restarted.wait(), timeout=1)
        await manager.stop()
    assert runs == 2
    assert any(record.levelno == logging.CRITICAL and "critical" in record.message for record in caplog.records)
