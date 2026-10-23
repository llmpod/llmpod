"""GZip 流式排除回归测试。

守护范围：starlette ``GZipMiddleware`` 内置按响应 content-type 排除压缩的机制
（content_type_is_excluded，默认排除 text/event-stream）。本仓在
``llmpod/apps/webserver/middleware.py`` 把 ``application/x-ndjson`` 纳入排除清单——
否则 gzip 用 GzipFile 累积 chunk，只在响应结束（more_body=False）close 才 flush，
NDJSON 长流（tail-log/docker-pull）more_body 恒为 True，帧数据滞留 zlib 缓冲直到
连接关闭才吐出，浏览器强制带 Accept-Encoding: gzip（forbidden header）会永远收不到帧。

starlette 未把排除清单做成构造参数（社区已提需求未合入），本仓靠模块级补丁扩展；
若 starlette 升级改名/改机制，本测试会立即暴露。
"""

from __future__ import annotations

import asyncio
import os

import pytest
import starlette.middleware.gzip as _gz_module
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Message

from llmpod.apps.webserver import middleware as llmpod_mw

STREAM_CT = "application/x-ndjson"
SSE_CT = "text/event-stream"
JSON_CT = "application/json"


def _streaming_app(chunks: list[bytes], media_type: str = STREAM_CT):
    """最小 ASGI app：start + 逐帧 body(more_body)，模拟 NDJSON 长流。"""

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", media_type.encode())],
            }
        )
        for i, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(chunks) - 1,
                }
            )

    return app


def _buffered_app(body: bytes, media_type: str = JSON_CT):
    """一次性发完整 body（more_body=False），模拟普通 JSON API。"""

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", media_type.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


class _Collector:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)


def _start_headers(messages: list[Message]) -> dict:
    for m in messages:
        if m["type"] == "http.response.start":
            return {k: v for k, v in m["headers"]}
    return {}


def _bodies(messages: list[Message]) -> list[bytes]:
    return [m["body"] for m in messages if m["type"] == "http.response.body"]


def _scope(media_type: str = STREAM_CT, accept_gzip: bool = True) -> dict:
    headers = []
    if accept_gzip:
        headers.append((b"accept-encoding", b"gzip"))
    return {
        "type": "http",
        "method": "POST",
        "path": "/whatever",
        "headers": headers,
        "query_string": b"",
        "client": ("test", 12345),
        "server": ("test", 80),
    }


async def _noop_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class TestExcludedContentTypesRegistered:
    def test_ndjson_in_excluded_list(self):
        """导入 middleware 模块即把 ndjson 纳入 starlette 排除清单。"""
        assert "application/x-ndjson" in _gz_module.DEFAULT_EXCLUDED_CONTENT_TYPES

    def test_sse_still_excluded(self):
        assert "text/event-stream" in _gz_module.DEFAULT_EXCLUDED_CONTENT_TYPES

    def test_octet_stream_in_excluded_list(self):
        """模型分发 FileResponse（application/octet-stream）不压缩：权重高熵，
        gzip 压缩比≈1 却逐 chunk 烧 CPU，分发吞吐被打满单核拖垮。"""
        assert "application/octet-stream" in _gz_module.DEFAULT_EXCLUDED_CONTENT_TYPES

    def test_patch_applied_on_import(self):
        # 确认是 middleware 模块导入门面打补丁，而非测试自身副作用
        assert llmpod_mw is not None
        assert _gz_module.DEFAULT_EXCLUDED_CONTENT_TYPES == (
            "text/event-stream",
            "application/x-ndjson",
            "application/octet-stream",
        )


class TestOctetStreamBypassed:
    async def test_binary_stream_no_content_encoding(self):
        """模型权重流（octet-stream）不进 gzip：无 content-encoding 头、字节原样透传。"""
        chunks = [os.urandom(65536), os.urandom(65536)]
        mw = GZipMiddleware(_streaming_app(chunks, "application/octet-stream"), minimum_size=10)
        col = _Collector()
        await asyncio.wait_for(mw(_scope("application/octet-stream"), _noop_receive, col), timeout=5)

        headers = _start_headers(col.messages)
        assert b"content-encoding" not in headers
        assert b"".join(_bodies(col.messages)) == b"".join(chunks)


class TestStreamingResponseBypassed:
    @pytest.mark.parametrize("media_type", [STREAM_CT, SSE_CT])
    async def test_no_content_encoding(self, media_type):
        chunks = [b'{"event":"data","data":"tick1"}\n', b'{"event":"ping"}\n']
        mw = GZipMiddleware(_streaming_app(chunks, media_type), minimum_size=10)
        col = _Collector()
        await asyncio.wait_for(mw(_scope(media_type), _noop_receive, col), timeout=5)

        headers = _start_headers(col.messages)
        assert b"content-encoding" not in headers

    async def test_ndjson_chunks_pass_through_unchanged(self):
        """关键断言：每个帧按发送顺序原样透传，未被 gzip 缓冲合并或压缩。"""
        chunks = [b'{"event":"data","data":"a"}\n', b'{"event":"ping"}\n', b'{"event":"end"}\n']
        mw = GZipMiddleware(_streaming_app(chunks), minimum_size=10)
        col = _Collector()
        await asyncio.wait_for(mw(_scope(), _noop_receive, col), timeout=5)

        assert _bodies(col.messages) == chunks
        body = b"".join(_bodies(col.messages))
        assert not body.startswith(b"\x1f\x8b")  # 无 gzip magic

    async def test_empty_first_chunk_still_bypasses(self):
        """首帧为空（引擎编译期长时间无日志场景）：仍走 identity，不发 gzip 头。"""
        chunks = [b"", b'{"event":"ping"}\n']
        mw = GZipMiddleware(_streaming_app(chunks), minimum_size=10)
        col = _Collector()
        await asyncio.wait_for(mw(_scope(), _noop_receive, col), timeout=5)

        assert b"content-encoding" not in _start_headers(col.messages)
        assert b"".join(_bodies(col.messages)) == b"".join(chunks)


class TestNormalJsonStillCompresses:
    async def test_large_json_gets_gzipped(self):
        body = b'{"msg":"ok"}' * 200  # 远超 minimum_size
        mw = GZipMiddleware(_buffered_app(body), minimum_size=100)
        col = _Collector()
        await asyncio.wait_for(mw(_scope(JSON_CT), _noop_receive, col), timeout=5)

        headers = _start_headers(col.messages)
        assert headers.get(b"content-encoding") == b"gzip"
        import gzip

        assert gzip.decompress(b"".join(_bodies(col.messages))) == body

    async def test_small_json_not_compressed(self):
        body = b'{"msg":"ok"}'
        mw = GZipMiddleware(_buffered_app(body), minimum_size=100)
        col = _Collector()
        await asyncio.wait_for(mw(_scope(JSON_CT), _noop_receive, col), timeout=5)

        assert b"content-encoding" not in _start_headers(col.messages)
        assert b"".join(_bodies(col.messages)) == body
