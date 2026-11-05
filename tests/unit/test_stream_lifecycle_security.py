"""ASGI 发送取消时，先关闭流并结算，才执行预扣兜底。"""

import asyncio
from decimal import Decimal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.responses import StreamingResponse

from llmpod.apps.proxyserver.inference_handler import _attach_stream_close
from llmpod.apps.proxyserver import inference_handler as handler
from llmpod.apps.proxyserver.billing import HeldReservation
from llmpod.apps.proxyserver.limits import ReservedUsage
from llmpod.apps.proxyserver.protocol.base import ForwardResult, StreamEvent, _chain
from llmpod.apps.proxyserver.snapshot import RouteEntry


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


@pytest.mark.parametrize(
    ("asgi_version", "disconnect_at"),
    [("2.3", phase) for phase in ("body", "close", "usage", "billing", "eof")] + [("2.4", "body")],
)
async def test_disconnect_finishes_settlement_and_logs_once(monkeypatch, asgi_version, disconnect_at):
    """真实 ASGI 取消覆盖整个收尾：已交付内容收费一次、释放预占、写一条日志。"""
    reached, resume, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events, records, usage_calls = [], [], []
    usage = {"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40}

    async def checkpoint(phase):
        if disconnect_at == phase:
            reached.set()
            await resume.wait()
        await asyncio.sleep(0)

    class Upstream:
        def __init__(self):
            self.frames = iter(
                [
                    StreamEvent(wire=b"data: content\n\n", usage=usage, text="model output", finish_reason="stop"),
                    StreamEvent(wire=b"data: [DONE]\n\n"),
                ]
            )
            self.started = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.started and disconnect_at == "body":
                await never.wait()
            self.started = True
            frame = next(self.frames, None)
            if frame is None:
                raise StopAsyncIteration
            return frame

        async def aclose(self):
            await checkpoint("close")
            events.append("upstream-closed")

    async def receive():
        await reached.wait()
        events.append("disconnect")
        resume.set()
        return {"type": "http.disconnect"}

    async def send(message):
        if disconnect_at == "body" and message.get("body"):
            if asgi_version == "2.4":
                raise asyncio.CancelledError
            reached.set()

    counter = {"reserved": 100}

    async def incrby(key, amount):
        counter[key] += amount

    redis = SimpleNamespace(incrby=incrby)
    reserved = ReservedUsage(request_id="disconnect-test")
    reserved.add("reserved", 100)

    async def settle_usage(_redis, _ctx, _name, actual, reserve):
        await checkpoint("usage")
        await reserve.settle(_redis)
        usage_calls.append(actual)

    async def bill(*_args, **_kwargs):
        await checkpoint("billing")
        events.append("billed")

    billing = SimpleNamespace(settle=AsyncMock(side_effect=bill), release=AsyncMock())
    ctx = SimpleNamespace(key_id=1, tenant_id=2)
    entry = RouteEntry(1, "m1", "published", Decimal("2"), Decimal("4"))
    held = HeldReservation(billing, ctx, entry, Decimal("0.01"))
    metrics = SimpleNamespace(
        record_ttft=lambda *a, **k: None,
        outcome_labels=lambda *a: {},
        exit_request=lambda: events.append("exit"),
        record_outcome=lambda *a, **k: events.append("outcome"),
    )
    monkeypatch.setattr(handler, "settle_usage", settle_usage)
    monkeypatch.setattr(handler, "proxy_metrics", metrics)
    monkeypatch.setattr(handler, "_note_endpoint_success", lambda *a: None)
    monkeypatch.setattr(handler, "build_record", lambda *a, **kwargs: kwargs)
    monkeypatch.setattr(handler, "spawn_log", lambda _runtime, record: records.append(record))
    response = await handler.respond(
        SimpleNamespace(),
        redis,
        ctx,
        entry,
        held,
        reserved,
        SimpleNamespace(endpoint_id=10, served_model="internal"),
        SimpleNamespace(protocol="openai", records_logs=True),
        ForwardResult(stream=Upstream()),
        {"model": "m1"},
        "chat.completions",
        "disconnect-test",
        time.monotonic(),
        retry_count=0,
        route_reason="weighted",
    )
    _attach_stream_close(response, held, reserved, redis, "chat.completions", "disconnect-test", response.stream_exited)
    call = response({"type": "http", "asgi": {"spec_version": asgi_version}}, receive, send)
    if asgi_version == "2.4":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=2)
    else:
        await asyncio.wait_for(call, timeout=2)

    assert events.count("upstream-closed") == 1
    assert events.count("billed") == 1
    assert events.count("exit") == 1 and events.count("outcome") == 1
    assert usage_calls == [usage]
    assert counter["reserved"] == 0
    billing.settle.assert_awaited_once()
    assert billing.settle.call_args.args[2] == Decimal("0.000096")
    billing.release.assert_not_awaited()
    assert len(records) == 1 and records[0]["usage"] == usage
    assert records[0]["status"] == ("interrupted" if disconnect_at == "body" else "success")
