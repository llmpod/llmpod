"""model_source 探测器网络路径单测：oss.py + remote_gguf.py（httpx.MockTransport 全桩，零网络）。

GGUF 头字节流手搓（同 test_gguf_meta 范式）。AsyncClient 经 monkeypatch 注入
MockTransport——不改被测代码、不触真实对象存储。
"""

from __future__ import annotations

import struct

import httpx
import pytest

from llmpod.apps.manager.model_source import oss as oss_mod
from llmpod.apps.manager.model_source import remote_gguf
from llmpod.apps.manager.model_source.gguf_meta import _MAX_HEADER_READ, _MAX_HEADER_READ_TOTAL
from llmpod.apps.manager.model_source.oss import OSSInspector, _filename_from_url

# ---- GGUF 字节流构造（value_type 与 gguf_meta 内部一致）----

VT_UINT32 = 4
VT_STRING = 8


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _gstr(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u64(len(raw)) + raw


def _kv(key: str, vtype: int, value: bytes) -> bytes:
    return _gstr(key) + _u32(vtype) + value


def _gguf_header(*, expert_count: int = 8, layers: int = 32) -> bytes:
    out = b"GGUF" + _u32(3) + _u64(0) + _u64(4)
    out += _kv("general.architecture", VT_STRING, _gstr("llama"))
    out += _kv("llama.expert_count", VT_UINT32, _u32(expert_count))
    out += _kv("llama.block_count", VT_UINT32, _u32(layers))
    out += _kv("llama.head_count", VT_UINT32, _u32(32))
    return out


GGUF_BYTES = _gguf_header()
TOTAL_BYTES = 2 * 1024**3 + 12345  # ~2 GB（真实权重远大于头部）


def _gguf_file_type_at_tail(file_type: int = 15) -> bytes:
    """量化类型在 KV 表尾部的 GGUF：中间塞一段 > _MAX_HEADER_READ 的字符串把它顶出首段。

    复刻真实布局——转换器最后才写 ``general.file_type``，大词表模型因此落在首段之外。
    """
    padding = _kv("general.padding", VT_STRING, _gstr("x" * _MAX_HEADER_READ))
    return (
        b"GGUF"
        + _u32(3)
        + _u64(0)
        + _u64(5)
        + _kv("general.architecture", VT_STRING, _gstr("llama"))
        + _kv("llama.expert_count", VT_UINT32, _u32(8))
        + _kv("llama.block_count", VT_UINT32, _u32(32))
        + padding
        + _kv("general.file_type", VT_UINT32, _u32(file_type))
    )


def _byte_range(value: str) -> tuple[int, int]:
    """``bytes=A-B`` → ``(A, B)``（只处理闭区间形式）。"""
    start, _, end = value.removeprefix("bytes=").partition("-")
    return int(start), int(end)


def _serve_by_range(body: bytes, seen: list[str]):
    """按请求的 Range 返回对应切片（206），并把 Range 头记进 ``seen``。"""

    def handler(req: httpx.Request) -> httpx.Response:
        rng = req.headers.get("range") or ""
        seen.append(rng)
        start, end = _byte_range(rng)
        return _stream(206, body[start : end + 1])

    return handler


def _stream(status: int, body: bytes, headers: dict | None = None) -> httpx.Response:
    """aiter_raw 可消费的 MockTransport 响应：content 用「每次调用新建的 async 生成器」

    （AsyncClient.send(stream=True) 要求 AsyncByteStream；同步生成器会 AssertionError）。
    """

    async def gen():
        yield body

    return httpx.Response(status, content=gen(), headers=headers or {})


def _patch_client(monkeypatch, module, handler):
    """给 module 命名空间里的 httpx.AsyncClient 注入 MockTransport。"""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)


class _Recorder:
    """记录 (method, path, headers) 序列的 handler 包装。"""

    def __init__(self, responder):
        self.calls: list[tuple[str, str, dict]] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.headers)))
        return self._responder(request)


def _head_ok(request: httpx.Request) -> httpx.Response:
    if request.method == "HEAD":
        return httpx.Response(200, headers={"content-length": str(TOTAL_BYTES)})
    return _stream(206, GGUF_BYTES)  # GET 走 aiter_raw → 需 async 流内容


# ============================================================
# _filename_from_url（纯函数）
# ============================================================


class TestFilenameFromUrl:
    def test_last_segment(self):
        assert _filename_from_url("https://oss.x.com/bucket/dir/model.gguf?sig=abc") == "model.gguf"

    def test_percent_decoded(self):
        assert _filename_from_url("https://oss.x.com/%E6%A8%A1%E5%9E%8B.gguf") == "模型.gguf"

    def test_empty_path(self):
        assert _filename_from_url("https://oss.x.com") == ""


# ============================================================
# OSSInspector
# ============================================================


@pytest.fixture
def inspector() -> OSSInspector:
    return OSSInspector()


class TestOSSHappyPath:
    async def test_head_content_length_and_gguf_header(self, inspector, monkeypatch):
        rec = _Recorder(_head_ok)
        _patch_client(monkeypatch, oss_mod, rec)
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/model-q4_k_m.gguf")
        assert rec.calls[0][0] == "HEAD"
        # .gguf → 追加 Range GET 探头部
        assert rec.calls[1][0] == "GET"
        assert rec.calls[1][2].get("range") == f"bytes=0-{_MAX_HEADER_READ - 1}"
        assert out.success is True
        assert out.structure == "single_file"
        assert out.format == "gguf" and out.layout == "gguf"
        assert out.size_gb == round(TOTAL_BYTES / 1024**3, 2)
        assert out.quantization == "q4_k_m"
        assert out.weight_files == ["model-q4_k_m.gguf"]
        # MoE 与架构形状来自 GGUF 头（单文件无 config）
        assert out.is_moe is True
        assert out.num_layers == 32
        assert out.num_attention_heads == 32
        assert out.has_config is False
        assert out.suggested_type is None  # 非 HF 源：type 由用户选
        assert "人工" in (out.message or "")

    async def test_revision_passthrough(self, inspector, monkeypatch):
        _patch_client(monkeypatch, oss_mod, _Recorder(_head_ok))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/m.gguf", revision="r9")
        assert out.revision == "r9"

    async def test_non_gguf_skips_header_probe(self, inspector, monkeypatch):
        rec = _Recorder(lambda req: httpx.Response(200, headers={"content-length": "1073741824"}))
        _patch_client(monkeypatch, oss_mod, rec)
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/model.safetensors")
        assert [c[0] for c in rec.calls] == ["HEAD"]  # 无 Range GET
        assert out.format == "safetensors"
        assert out.size_gb == 1.0
        assert out.is_moe is False
        assert out.num_layers is None  # 无 config 无 GGUF 头 → 形状缺


class TestOSSSizeExtraction:
    async def test_head_404_falls_back_to_range_get(self, inspector, monkeypatch):
        """HEAD 被拒（403）→ streamed Range GET，size 取 content-range 的 /total。"""

        def responder(req: httpx.Request) -> httpx.Response:
            if req.method == "HEAD":
                return httpx.Response(403)
            return httpx.Response(
                206,
                headers={"content-range": f"bytes 0-0/{TOTAL_BYTES}", "content-length": "1"},
            )

        rec = _Recorder(responder)
        _patch_client(monkeypatch, oss_mod, rec)
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/model.safetensors")
        assert rec.calls[0][0] == "HEAD" and rec.calls[1][0] == "GET"
        assert rec.calls[1][2].get("range") == "bytes=0-0"
        assert out.size_gb == round(TOTAL_BYTES / 1024**3, 2)

    async def test_range_fallback_gguf_still_probes_header(self, inspector, monkeypatch):
        def responder(req: httpx.Request) -> httpx.Response:
            if req.method == "HEAD":
                return httpx.Response(404)
            return _stream(
                206,
                GGUF_BYTES,
                headers={"content-range": f"bytes 0-{len(GGUF_BYTES) - 1}/{TOTAL_BYTES}"},
            )

        _patch_client(monkeypatch, oss_mod, _Recorder(responder))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/m.gguf")
        assert out.is_moe is True
        assert out.size_gb == round(TOTAL_BYTES / 1024**3, 2)

    async def test_content_range_without_total_ignored(self, inspector, monkeypatch):
        """content-range 无 /total（畸形）→ 不误用分片大小，size 置 None。"""
        rec = _Recorder(lambda req: httpx.Response(206, headers={"content-range": "bytes 0-0", "content-length": "1"}))
        _patch_client(monkeypatch, oss_mod, rec)
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/model.safetensors")
        assert out.size_gb is None
        assert out.success is True  # format 有 + size 缺 → 软失败放行

    async def test_no_length_headers_size_none(self, inspector, monkeypatch):
        _patch_client(monkeypatch, oss_mod, _Recorder(lambda req: httpx.Response(200)))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/model.safetensors")
        assert out.size_gb is None

    async def test_all_missing_is_hard_failure(self, inspector, monkeypatch):
        """format 与 size 全缺 → success=False（前端禁添加）。"""
        _patch_client(monkeypatch, oss_mod, _Recorder(lambda req: httpx.Response(200)))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/blob")
        assert out.success is False


class TestOSSFailures:
    async def test_http_error_soft_failure(self, inspector, monkeypatch):
        def responder(req):
            return httpx.Response(404)  # HEAD 与 GET 皆 404

        _patch_client(monkeypatch, oss_mod, _Recorder(responder))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/m.gguf")
        assert out.success is False
        assert "无法访问对象存储地址" in out.message
        assert "HTTPStatusError" in out.message

    async def test_connect_error_soft_failure(self, inspector, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("refused")

        _patch_client(monkeypatch, oss_mod, _Recorder(handler))
        out = await inspector.inspect(source_url="https://oss.example.com/bkt/m.gguf")
        assert out.success is False
        assert "ConnectError" in out.message


# ============================================================
# remote_gguf.probe_gguf_metadata_remote
# ============================================================


class TestProbeGGUFRemote:
    async def test_parses_header_from_206(self, monkeypatch):
        seen_range = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen_range["v"] = req.headers.get("range")
            return _stream(206, GGUF_BYTES)

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://hf.example/org/m/resolve/main/m.gguf")
        assert seen_range["v"] == f"bytes=0-{_MAX_HEADER_READ - 1}"
        assert meta is not None
        assert meta.expert_count == 8 and meta.is_moe is True
        assert meta.architecture == "llama"
        assert meta.num_layers == 32

    async def test_server_ignoring_range_bounded_read(self, monkeypatch):
        """服务器无视 Range 返 200 全量 → 有界读只取头部上限，仍解析成功。"""
        big_body = GGUF_BYTES + b"\x00" * (_MAX_HEADER_READ + 1024)  # 大于上限
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["range"] = req.headers.get("range")
            return _stream(200, big_body)

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")
        assert meta is not None and meta.expert_count == 8

    async def test_non_2xx_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, remote_gguf, lambda req: httpx.Response(403, text="gated"))
        async with httpx.AsyncClient() as client:
            assert await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf") is None

    async def test_non_gguf_bytes_returns_none(self, monkeypatch):
        _patch_client(monkeypatch, remote_gguf, lambda req: _stream(200, b"\x00" * 256))
        async with httpx.AsyncClient() as client:
            assert await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.bin") is None

    async def test_network_error_returns_none(self, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("boom")

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            assert await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf") is None

    async def test_truncated_header_partial_parse(self, monkeypatch):
        """KV 表声明 4 个但只传前 2 个的截断流 → 已读到的 expert_count 不丢。"""
        partial = b"GGUF" + _u32(3) + _u64(0) + _u64(4) + _kv("llama.expert_count", VT_UINT32, _u32(5))
        _patch_client(monkeypatch, remote_gguf, lambda req: _stream(206, partial))
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")
        assert meta is not None and meta.expert_count == 5

    async def test_continuation_reaches_file_type_at_kv_tail(self, monkeypatch):
        """量化类型在 KV 表尾部、首段够不着 → 续拉一段补齐（206 拼接）。"""
        body = _gguf_file_type_at_tail(15)
        seen: list[str] = []
        _patch_client(monkeypatch, remote_gguf, _serve_by_range(body, seen))
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert seen == [
            f"bytes=0-{_MAX_HEADER_READ - 1}",
            f"bytes={_MAX_HEADER_READ}-{_MAX_HEADER_READ_TOTAL - 1}",
        ]
        assert meta is not None
        assert meta.file_type == 15 and meta.quantization == "q4_k_m"
        assert meta.num_layers == 32  # 首段拿到的架构形状仍在

    async def test_continuation_on_range_ignoring_server(self, monkeypatch):
        """续段又收到 200（服务器无视 Range）= 内容是从头给的，不能与首段拼接。"""
        body = _gguf_file_type_at_tail(15)
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers.get("range") or "")
            return _stream(200, body)  # 无视 Range：每次都从头给全量

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert len(seen) == 2
        assert meta is not None
        assert meta.file_type == 15 and meta.quantization == "q4_k_m"

    async def test_continuation_with_wrong_content_range_keeps_first_segment(self, monkeypatch):
        """206 的 Content-Range 起点对不上 → 整段作废，退回首段结果。

        回归：`_fetch_range` 原先只信状态行。这里续段的内容其实是**对的**，只有
        `Content-Range` 报了个错起点——校验在就必须把它拦下（宁可少解一个量化类型，
        也不要基于一段来源不可信的字节去改 `general.architecture`，它是唯一 last-wins
        的字段）。
        """
        body = _gguf_file_type_at_tail(15)
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            start, end = _byte_range(req.headers.get("range") or "")
            if calls["n"] == 1:
                return _stream(206, body[start : end + 1])
            # 内容是对的续段，但头里报的起点是 0
            return _stream(206, body[start : end + 1], {"Content-Range": f"bytes 0-{end - start}/{TOTAL_BYTES}"})

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert calls["n"] == 2
        assert meta is not None
        assert meta.file_type is None and meta.quantization is None  # 不可信的续段不采纳
        assert meta.num_layers == 32  # 首段结果不丢

    async def test_continuation_accepts_matching_content_range(self, monkeypatch):
        """Content-Range 起点对得上 → 正常拼接（新校验不能把正常路径拦掉）。"""
        body = _gguf_file_type_at_tail(15)

        def handler(req: httpx.Request) -> httpx.Response:
            start, end = _byte_range(req.headers.get("range") or "")
            return _stream(206, body[start : end + 1], {"Content-Range": f"bytes {start}-{end}/{len(body)}"})

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert meta is not None
        assert meta.file_type == 15 and meta.quantization == "q4_k_m"

    async def test_continuation_failure_keeps_first_segment(self, monkeypatch):
        """续拉失败 → 首段结果照用（量化退回 None），绝不比不续拉更差。"""
        body = _gguf_file_type_at_tail(15)
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _stream(206, body[:_MAX_HEADER_READ])
            raise httpx.ConnectError("boom")

        _patch_client(monkeypatch, remote_gguf, handler)
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert calls["n"] == 2
        assert meta is not None
        assert meta.file_type is None and meta.quantization is None
        assert meta.num_layers == 32  # 首段结果不丢

    async def test_no_continuation_when_first_segment_not_full(self, monkeypatch):
        """首段没读满 = 文件就这么大，后面没有 KV 了 → 不续拉（省一次请求）。"""
        seen: list[str] = []
        _patch_client(monkeypatch, remote_gguf, _serve_by_range(GGUF_BYTES, seen))
        async with httpx.AsyncClient() as client:
            meta = await remote_gguf.probe_gguf_metadata_remote(client, "https://x/m.gguf")

        assert len(seen) == 1
        assert meta is not None and meta.expert_count == 8
