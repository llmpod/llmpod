"""``_download.download_resumable`` 单测（httpx.MockTransport 全桩，零网络）。

重点覆盖续传判定与完整性校验——这些规则都是从真实服务端行为（ModelScope 非 LFS
文件对 Range 回 200 只给后半截、LFS CDN 给 X-Linked-Etag）反推出来的，写错一条的
后果是「重试在两种长度之间来回震荡」或「把损坏内容当成功落盘」。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from llmpod.apps.node.executors._download import HttpDownloadError, download_resumable

BODY = b"0123456789abcdef"
SHA = hashlib.sha256(BODY).hexdigest()


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def _run(dest: Path, handler, *, size: int = len(BODY), sha: str | None = None, url: str = "http://test/f"):
    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        await download_resumable(
            client,
            url=url,
            dest_file=dest,
            size=size,
            cancel_event=asyncio.Event(),
            label="f",
            expected_sha256=sha,
        )


def _part(dest: Path) -> Path:
    return Path(str(dest) + ".part")


# ---- 续传判定 ----


async def test_resume_206_appends_from_offset(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY[:5])
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY[start:],
        )

    await _run(dest, handler)

    assert dest.read_bytes() == BODY
    assert seen == ["bytes=5-"]


async def test_resume_200_with_matching_content_range_appends(tmp_path):
    """ModelScope 非 LFS 文件：回 200 但只给请求的那一段，Content-Range 指出起点。"""
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY[:5])
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(
            200,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY[start:],
        )

    await _run(dest, handler)

    assert dest.read_bytes() == BODY
    # 一次拿到正确后半截即可收工，不需要再来一次全量请求
    assert seen == ["bytes=5-"]


async def test_resume_200_without_content_range_restarts_without_range(tmp_path):
    """200 且没有 Content-Range：body 语义不明，丢弃后不带 Range 重新请求。

    直接拿它覆盖残片是旧实现的 bug——若它其实只是后半截，得到的文件长度对不上，
    且每次重试都重复同一个错误。
    """
    dest = tmp_path / "f"
    _part(dest).write_bytes(b"XXXXX")
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        if request.headers.get("range"):
            # 假装服务端忽略了 Range，但只回了后半截（危险形态）
            return httpx.Response(200, content=BODY[5:])
        return httpx.Response(200, content=BODY)

    await _run(dest, handler)

    assert dest.read_bytes() == BODY
    assert seen == ["bytes=5-", None]


async def test_206_wrong_start_rejected(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY[:5])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206, headers={"Content-Range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"}, content=BODY
        )

    with pytest.raises(HttpDownloadError, match="Content-Range 起点不符"):
        await _run(dest, handler)


async def test_416_discards_part_and_restarts(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY + b"EXTRA")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range"):
            return httpx.Response(416)
        return httpx.Response(200, content=BODY)

    await _run(dest, handler)

    assert dest.read_bytes() == BODY


async def test_part_longer_than_expected_is_dropped(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY + b"EXTRA")
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        return httpx.Response(200, content=BODY)

    await _run(dest, handler)

    assert dest.read_bytes() == BODY
    assert seen == [None]


async def test_full_part_finalizes_without_request(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("写满的残片不应再发请求")

    await _run(dest, handler, sha=SHA)

    assert dest.read_bytes() == BODY


async def test_full_part_with_wrong_content_is_rejected(tmp_path):
    """长度对但内容坏的残片不能因为「长度对了」就装箱。"""
    dest = tmp_path / "f"
    _part(dest).write_bytes(b"X" * len(BODY))

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发请求")

    with pytest.raises(HttpDownloadError, match="内容校验失败"):
        await _run(dest, handler, sha=SHA)

    assert not _part(dest).exists()


# ---- 完整性校验 ----


async def test_sha256_verified_on_success(tmp_path):
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)

    await _run(dest, handler, sha=SHA)

    assert dest.read_bytes() == BODY


async def test_sha256_mismatch_deletes_part(tmp_path):
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"Y" * len(BODY))

    with pytest.raises(HttpDownloadError, match="内容校验失败"):
        await _run(dest, handler, sha=SHA)

    assert not dest.exists()
    assert not _part(dest).exists()


async def test_sha256_covers_resumed_prefix(tmp_path):
    """续传只下后半截，哈希必须把磁盘上已有的前缀算进去。"""
    dest = tmp_path / "f"
    _part(dest).write_bytes(BODY[:5])

    async def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY[start:],
        )

    await _run(dest, handler, sha=SHA)

    assert dest.read_bytes() == BODY


async def test_sha256_mismatch_after_resume_deletes_part(tmp_path):
    dest = tmp_path / "f"
    _part(dest).write_bytes(b"X" * 5)

    async def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY[start:],
        )

    with pytest.raises(HttpDownloadError, match="内容校验失败"):
        await _run(dest, handler, sha=SHA)

    assert not _part(dest).exists()


async def test_x_linked_etag_used_when_no_expected_sha(tmp_path):
    """LFS CDN 用 X-Linked-Etag 给对象哈希，清单没哈希时拿它兜底。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Linked-Etag": SHA}, content=b"Y" * len(BODY))

    with pytest.raises(HttpDownloadError, match="内容校验失败"):
        await _run(dest, handler)


async def test_non_hex_etag_is_not_treated_as_sha(tmp_path):
    """FileResponse 的 ETag 是 inode 标记，不是哈希，不能拿来当校验依据。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"12345-16-67890"'}, content=BODY)

    await _run(dest, handler)

    assert dest.read_bytes() == BODY


async def test_announced_total_mismatch_rejected(tmp_path):
    """源文件在两轮之间变了：早失败好过下完一个大小不对的文件。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "99"}, content=BODY)

    with pytest.raises(HttpDownloadError, match="服务端声明的总长与清单不符"):
        await _run(dest, handler)


async def test_short_body_keeps_part_for_resume(tmp_path):
    """连接提前结束：残片留着，下次接着下。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(len(BODY))}, content=BODY[:6])

    with pytest.raises(HttpDownloadError, match="响应不完整"):
        await _run(dest, handler)

    assert _part(dest).read_bytes() == BODY[:6]


async def test_declared_total_overflow_rejected_before_write(tmp_path):
    """Content-Length 已超出清单：写盘前就拒，不留半份垃圾。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY + b"EXTRA")

    with pytest.raises(HttpDownloadError, match="服务端声明的总长与清单不符"):
        await _run(dest, handler)


async def test_stream_overrun_without_length_rejected(tmp_path):
    """分块响应没有 Content-Length：只能靠逐块累计拦超出清单的部分。"""
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield BODY
            yield b"EXTRA"

        return httpx.Response(200, content=gen())

    with pytest.raises(HttpDownloadError, match="响应超出清单大小"):
        await _run(dest, handler)


async def test_zero_byte_file(tmp_path):
    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    await _run(dest, handler, size=0, sha=hashlib.sha256(b"").hexdigest())

    assert dest.exists() and dest.stat().st_size == 0


async def test_accept_encoding_identity_requested(tmp_path):
    """请求压缩会让 Content-Length 与落盘字节不可比，大小校验失去意义。"""
    dest = tmp_path / "f"
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=BODY)

    await _run(dest, handler)

    assert seen == ["identity"]


async def test_transport_error_wrapped_in_caller_type(tmp_path):
    """连接被对端掐断要转成调用方的错误类型。

    裸抛 httpx 异常会绕过各下载器的错误契约，落到用户眼前是
    「RemoteProtocolError: peer closed connection...」这种没有文件名的英文栈。
    大权重在 CDN 侧断连是实测会发生的情况。
    """

    class MyError(RuntimeError):
        pass

    dest = tmp_path / "f"

    async def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield BODY[:4]
            raise httpx.RemoteProtocolError("peer closed connection")

        return httpx.Response(200, headers={"Content-Length": str(len(BODY))}, content=gen())

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(MyError, match="传输中断"):
            await download_resumable(
                client,
                url="http://test/f",
                dest_file=dest,
                size=len(BODY),
                cancel_event=asyncio.Event(),
                label="f.bin",
                error_cls=MyError,
            )


async def test_cancel_mid_body(tmp_path):
    from llmpod.apps.node.executors.ensure_model import DownloadCancelled

    dest = tmp_path / "f"
    cancel = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield BODY[:4]
            cancel.set()
            yield BODY[4:]

        return httpx.Response(200, content=gen())

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(DownloadCancelled):
            await download_resumable(
                client,
                url="http://test/f",
                dest_file=dest,
                size=len(BODY),
                cancel_event=cancel,
                label="f",
            )
