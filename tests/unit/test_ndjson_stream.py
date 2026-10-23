"""_stream_ndjson 心跳单测：静默发 ping 帧 + 心跳超时不得杀死底层生成器。

背景：starlette ≥1.3 走 ASGI ≥2.4 快路径只在 send 时感知客户端断开——安静流
（引擎编译期无日志）必须靠心跳帧保活 send 路径，否则断开后 node 轮询空转到复活
窗口。实现上不能用 ``wait_for(anext)``（超时 cancel 会注入并杀死还在轮询的生成器），
须 ``asyncio.wait(timeout)`` 挂起同一个 anext 任务。
"""

from __future__ import annotations

import asyncio
import json

import pytest

import llmpod.apps.webserver.routes.admin.orchestration as orch


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    monkeypatch.setattr(orch, "_NDJSON_HEARTBEAT_S", 0.05)


def _events(frames: list[str]) -> list[dict]:
    return [json.loads(f) for f in frames]


class TestNdjsonHeartbeat:
    async def test_silent_source_emits_ping_and_survives(self):
        """静默超时发 ping，且生成器不被心跳超时杀死——之后的数据照常吐出。"""

        async def slow_source():
            await asyncio.sleep(0.12)  # 跨 >2 个心跳间隔
            yield "hello"
            await asyncio.sleep(0.12)  # 再静默
            yield "again"

        out = _events([f async for f in orch._stream_ndjson(slow_source())])

        events = [f["event"] for f in out]
        assert events[0] == "ping"
        assert {"event": "data", "data": "hello"} in out
        assert {"event": "data", "data": "again"} in out  # 心跳没杀死生成器
        assert events[-1] == "end"

    async def test_fast_source_no_ping(self):
        async def fast_source():
            yield "a"
            yield "b"

        out = _events([f async for f in orch._stream_ndjson(fast_source())])

        assert [f["event"] for f in out] == ["data", "data", "end"]

    async def test_error_frame(self):
        async def boom():
            yield "ok"
            raise RuntimeError("boom")

        out = _events([f async for f in orch._stream_ndjson(boom())])

        assert [f["event"] for f in out] == ["data", "error"]
        assert out[-1]["message"] == "boom"

    async def test_consumer_close_cancels_underlying_generator(self):
        """消费者中途关闭（前端 abort）：底层生成器经 anext 取消被终止，不留后台。"""
        polled = []

        async def source():
            try:
                while True:
                    polled.append(1)
                    await asyncio.sleep(0.03)
                    yield "tick"
            finally:
                polled.append("stopped")

        agen = orch._stream_ndjson(source())
        it = agen.__aiter__()
        await it.__anext__()  # tick
        await asyncio.sleep(0.1)
        await it.__anext__()  # 再收一个（中间应有 ping）
        await agen.aclose()  # 消费者关闭
        await asyncio.sleep(0.15)  # 给取消传播留时间
        n_at_close = polled.count(1)
        await asyncio.sleep(0.2)
        assert polled[-1] == "stopped"  # 底层生成器已终止
        assert polled.count(1) == n_at_close  # 之后无新轮询
