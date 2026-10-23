"""ASGI 发送取消时，先关闭流并结算，才执行预扣兜底。"""

import asyncio

import pytest
from starlette.responses import StreamingResponse

from llmpod.apps.proxyserver.inference_handler import _attach_stream_close
from llmpod.apps.proxyserver.protocol.base import StreamEvent, _chain


class Hold:
    def __init__(self, events):
        self.events = events

    async def close(self, **_kw):
        self.events.append("hold-close")


class Reserve:
    def __init__(self, events):
        self.events = events

    async def release(self, _redis):
        self.events.append("reserve-release")


async def _run_response(response):
    sent = 0

    async def receive():
        await asyncio.Future()

    async def send(message):
        nonlocal sent
        if message["type"] == "http.response.body" and message.get("body"):
            sent += 1
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await response(
            {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "GET", "path": "/"},
            receive,
            send,
        )
    assert sent == 1


async def test_ablation_send_cancel_skips_generator_finally_without_close_hook():
    events = []

    async def body():
        try:
            yield b"generated-output"
        finally:
            events.append("settled")

    response = StreamingResponse(body())
    await _run_response(response)
    assert events == []  # 控制组：ASGI 取消不自动执行生成器 finally
    await response.body_iterator.aclose()


async def test_cancel_closes_iterator_before_release():
    events = []

    async def body():
        try:
            yield b"generated-output"
        finally:
            events.append("settled")

    response = StreamingResponse(body())
    exited = {"exited": True}
    _attach_stream_close(response, Hold(events), Reserve(events), object(), "chat", "r1", exited)
    await _run_response(response)
    assert events == ["settled", "hold-close", "reserve-release"]


async def test_chain_close_reaches_upstream_generator():
    closed = []

    async def upstream():
        try:
            yield StreamEvent(wire=b"first")
            await asyncio.Future()
        finally:
            closed.append(True)

    source = upstream()
    first = await source.__anext__()
    chained = _chain(first, source)
    assert (await chained.__anext__()).wire == b"first"
    await chained.aclose()
    assert closed == [True]
