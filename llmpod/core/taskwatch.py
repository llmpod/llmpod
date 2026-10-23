"""进程常驻后台循环：监督重启及生命周期管理。

适用面：本身「永不返回」的长驻周期循环。单轮异常由循环自身或 every 兜底；
supervise 只兜「循环整体退出」——任务对象被 cancel 之外的
任何路径结束（未捕获异常、意外 return）都会静默停摆，直到进程重启。
CancelledError 透传（进程关闭的正常路径）。

每个进程生命周期持有自己的 BackgroundTaskManager；业务派生的单次任务仍由
业务模块管理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("llmpod.core.taskwatch")


async def supervise(
    factory: Callable[[], Awaitable[object]],
    *,
    name: str,
    restart_delay: float = 5.0,
    failure_level: int | None = None,
) -> None:
    """``factory``：零参（或闭包）工厂，每次重启调用、返回新协程——已死协程不能重新 await。"""
    while True:
        try:
            await factory()
            level = failure_level if failure_level is not None else logging.WARNING
            log.log(level, "%s 后台循环意外返回，%.1fs 后重启", name, restart_delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            level = failure_level if failure_level is not None else logging.ERROR
            log.log(level, "%s 后台循环崩溃，%.1fs 后重启", name, restart_delay, exc_info=True)
        await asyncio.sleep(restart_delay)


class BackgroundTaskManager:
    """持有同一生命周期内的常驻循环，关闭时先取消全部任务再等待退出。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    def start(
        self,
        factory: Callable[[], Awaitable[object]],
        *,
        name: str,
        restart_delay: float = 5.0,
        failure_level: int | None = None,
    ) -> asyncio.Task:
        if self._closed:
            raise RuntimeError("background task manager is closed")
        if name in self._tasks:
            raise ValueError(f"duplicate background task: {name}")
        if restart_delay <= 0:
            raise ValueError("restart_delay must be positive")
        task = asyncio.create_task(
            supervise(factory, name=name, restart_delay=restart_delay, failure_level=failure_level),
            name=name,
        )
        self._tasks[name] = task
        return task

    def every(
        self,
        work: Callable[[], Awaitable[object]],
        *,
        name: str,
        interval: float,
        immediate: bool = False,
    ) -> asyncio.Task:
        """串行执行单轮工作；每轮结束后等待 interval，失败留给下一轮。

        默认首轮先等 interval 再执行；``immediate=True`` 则立即跑首轮
        （启动自愈语义，如上传清理）。
        """
        if interval <= 0:
            raise ValueError("interval must be positive")

        async def loop() -> None:
            if not immediate:
                await asyncio.sleep(interval)
            while True:
                try:
                    await work()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("%s 后台任务单轮失败", name)
                await asyncio.sleep(interval)

        return self.start(loop, name=name)

    async def stop(self) -> None:
        """幂等关闭：取消所有任务后统一 join，避免资源先于任务销毁。

        关闭是永久的——之后再 ``start`` 抛 RuntimeError（每个生命周期各建新实例，不复用）。
        """
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
