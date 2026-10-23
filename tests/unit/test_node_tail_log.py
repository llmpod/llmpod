"""tail_log follow 长连接单测：backlog 回看 + 跟随 + 重建重开 + 首触历史 + 干净关闭。

设计：docker SDK ``logs(follow=True, stream=True)`` 一次拿 backlog(tail=lines) + 持续跟随，
游标由 docker 内部维护（无手工时间戳游标——含闭边界去重集合被清空导致末行隔轮重发那类
历史坑不再存在）。容器重建（同名先清再起 → 新 id）回看新 backlog；同一容器重开（stop/
restart 窗口，id 未变）tail=0 只跟新行不重吐旧日志。中断/完成在 finally ``gen.close()``
干净关闭 follow 连接。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from llmpod.apps.node.services import node as node_mod
from llmpod.apps.node.services.node import NodeServiceImpl


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(node_mod, "_TAIL_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(node_mod, "_TAIL_MISSING_POLL_INTERVAL_S", 0)
    # 复活窗口默认小值快速收敛（用例按需覆盖）；否则等待态会真等 600s
    monkeypatch.setattr(node_mod, "_TAIL_REVIVE_WINDOW_S", 0.2)


def _svc() -> NodeServiceImpl:
    return NodeServiceImpl(SimpleNamespace(node_id="n1"))


class _FakeContainer:
    """fake container：``.id`` / ``.status`` / ``.logs(**kw)`` → 按 tail 截断的 chunk 序列。"""

    def __init__(self, id_: str, status: str, chunks: list[bytes]):
        self.id = id_
        self.status = status
        self._chunks = chunks
        self.calls: list[dict] = []

    def logs(self, **kwargs):
        self.calls.append(kwargs)
        tail = kwargs.get("tail") or 0
        chunks = self._chunks[-tail:] if tail else []
        return (c for c in chunks)  # tail=0 → 空生成器（只跟新行）


class _BlockingIter:
    """模拟 follow 长连接阻塞等新日志：``__next__`` 阻塞直到 ``close()``（跨线程）。"""

    def __init__(self, chunks: tuple[bytes, ...] = ()):
        self._q = list(chunks)
        self._closed = threading.Event()
        self.closed = False

    def __next__(self):
        if self._q:
            return self._q.pop(0)
        while not self._closed.is_set():
            time.sleep(0.01)
        raise StopIteration

    def close(self):
        self._closed.set()
        self.closed = True


class _FakeContainers:
    def __init__(self, docker):
        self._docker = docker

    def get(self, name):
        if self._docker._seq:
            item = self._docker._seq.pop(0)
            if item is NotFound:
                raise NotFound("gone")
            self._docker._last = item
            return item
        if self._docker._tail is NotFound:
            raise NotFound("gone")
        return self._docker._last


class _FakeDocker:
    """``get_docker_client()`` 替身：按序返回容器，耗尽后按 ``tail`` 兜底（默认 NotFound）。"""

    def __init__(self, seq, tail=NotFound):
        self._seq = list(seq)
        self._tail = tail
        self._last = None

    @property
    def containers(self):
        return _FakeContainers(self)


def _install(monkeypatch, docker):
    """共享同一 _FakeDocker 实例（tail_log 每次 ``get_docker_client()`` 都拿同一对象）。"""
    monkeypatch.setattr(node_mod, "get_docker_client", lambda: docker)


async def _collect(svc, lines_arg: int = 20) -> list[str]:
    return [ln async for ln in svc.tail_log("c1", lines=lines_arg)]


class TestFollowTailLog:
    async def test_first_touch_exited_backlog_then_end(self, monkeypatch):
        """首触 exited：回看一次 backlog（follow=False）即结束，不重开。"""
        c = _FakeContainer("c1", "exited", [b"old-1\n", b"old-2\n"])
        _install(monkeypatch, _FakeDocker([c]))

        out = await _collect(_svc())

        assert out == ["old-1", "old-2"]
        assert c.calls[0]["follow"] is False  # 历史只看一次，不 follow

    async def test_running_backlog_then_recreate_new_backlog(self, monkeypatch):
        """running：backlog + 跟随；流结束（容器重建，新 id）→ 回看新 backlog。"""
        c1 = _FakeContainer("c1", "running", [b"a\n", b"b\n"])
        c2 = _FakeContainer("c2", "running", [b"x\n"])
        _install(monkeypatch, _FakeDocker([c1, c2]))
        monkeypatch.setattr(node_mod, "_TAIL_REVIVE_WINDOW_S", 0.01)

        out = [ln for ln in await _collect(_svc()) if not ln.startswith("[llmpod]")]

        assert out == ["a", "b", "x"]
        assert c1.calls[0]["tail"] == 20
        assert c2.calls[0]["tail"] == 20  # 新 id → 回看新 backlog

    async def test_same_container_reopen_no_retail(self, monkeypatch):
        """同一容器重开（stop/restart 窗口，id 未变）：tail=0 只跟新行，不重吐旧日志。"""
        c = _FakeContainer("c1", "running", [b"a\n", b"b\n"])
        _install(monkeypatch, _FakeDocker([c, c]))  # 第二次仍是同 id
        monkeypatch.setattr(node_mod, "_TAIL_REVIVE_WINDOW_S", 0.01)

        out = [ln for ln in await _collect(_svc()) if not ln.startswith("[llmpod]")]

        assert out == ["a", "b"]  # 同 id 重开不重吐 a/b
        assert c.calls[0]["tail"] == 20
        assert c.calls[1]["tail"] == 0  # 同 id 重开 tail=0
        assert all(call["follow"] for call in c.calls)

    async def test_not_found_diag_then_revive_end(self, monkeypatch):
        """容器不存在：吐诊断行，复活窗口内继续，超时结束。"""
        _install(monkeypatch, _FakeDocker([]))
        monkeypatch.setattr(node_mod, "_TAIL_REVIVE_WINDOW_S", -1)

        out = await _collect(_svc())

        assert any(ln.startswith("[llmpod]") for ln in out)

    async def test_cancellation_closes_follow_stream(self, monkeypatch):
        """取消（消费者断开）→ finally 调 gen.close()，不留后台 follow 连接/线程。"""
        it = _BlockingIter((b"a\n",))
        c = _FakeContainer("c1", "running", [])
        c.logs = lambda **kw: it  # 阻塞迭代器模拟长连接等待新日志
        _install(monkeypatch, _FakeDocker([c]))

        task = asyncio.create_task(_collect(_svc()))
        await asyncio.sleep(0.05)  # 让 task 进入 follow 阻塞（to_thread 里 next()）
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert it.closed  # gen.close() 已调用，连接关闭、阻塞线程解阻塞
