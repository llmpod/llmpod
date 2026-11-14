"""共享 HTTP Range 续传原语（Node 侧）。

从 manager 文件下载器抽出，供 manager 与 ModelScope 下载器共用：单文件写到
``<dest>.part``，存在残片则发 ``Range: bytes=N-`` 续传，服务端忽略 Range（200 且无
``Content-Range``）或起点越界（416）则从头重写；206 必须校验 ``Content-Range`` 起点
与请求偏移一致，完成后校验大小（可选再校验 sha256）并 ``os.replace`` 原子落定。

**200 也可能只回了请求的那一段**：ModelScope 对非 LFS 文件（config.json 等）收到
``Range`` 时回 200，body 只有请求的后半截，同时带正确的 ``Content-Range``。判据因此
看 ``Content-Range`` 而不是状态码——起点等于残片长就按续传追加，否则才从头重写。旧
实现在这里只看 200 就当「服务端忽略了 Range」，把后半截写到文件开头，大小校验必然
不符，重试在「残片长」和「残片长+后半截」之间来回震荡、永不收敛。

完整性判定三层：声明的总长必须等于清单大小（早在写盘前拦「源文件变了」）、实收字节
必须等于声明长度（短包保留 ``.part`` 供下次续传）、写满后大小与 sha256 双校验。sha256
优先取调用方给的清单值，缺省时用响应头 ``X-Linked-Etag``（LFS CDN 会给对象哈希，非
64 位十六进制的 ETag 如 FileResponse 的 inode 标记不认）。

调用方负责 URL / 查询参数 / 错误类型；本模块不关心来源协议。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path

import httpx

from llmpod.apps.node.executors.ensure_model import DownloadCancelled
from llmpod.config.settings import settings

# 响应头里出现的 64 位十六进制 ETag 才当对象哈希用
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
# Content-Range 严格形态：``bytes <start>-<end>/<total|*>``
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class HttpDownloadError(RuntimeError):
    """HTTP 下载失败的默认错误类型；调用方可传入自己的子类。"""


def _flush_and_fsync(f) -> None:
    """flush + fsync（同步、阻塞盘 flush）——供 ``to_thread`` 移出事件循环。"""
    f.flush()
    os.fsync(f.fileno())


def _parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    """``Content-Range`` → ``(start, end, total)``；``total`` 未知（``*``）为 None。

    形态不符（含 ``end < start``）返回 None——宁可当成没给，也不能按猜出来的偏移续写。
    """
    if not value:
        return None
    m = _CONTENT_RANGE.fullmatch(value.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2))
    if end < start:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return start, end, total


def _declared_total(resp: httpx.Response, offset: int) -> int | None:
    """服务端声明的文件总长：优先 ``Content-Range`` 的 total，否则 offset+``Content-Length``。

    有 ``Content-Encoding``（非 identity）时 ``Content-Length`` 是压缩后的长度，不能用来
    核对落盘字节，直接不认。请求侧已带 ``Accept-Encoding: identity``，正常不会走到这里。
    """
    cr = _parse_content_range(resp.headers.get("Content-Range"))
    if cr is not None and cr[2] is not None:
        return cr[2]
    encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    length = resp.headers.get("Content-Length")
    if length is None or not length.strip().isdigit():
        return None
    return offset + int(length)


def _sha_from_header(resp: httpx.Response) -> str | None:
    """响应头 ``X-Linked-Etag`` 若是 64 位十六进制，即为对象 sha256（LFS CDN 惯例）。"""
    value = (resp.headers.get("X-Linked-Etag") or "").strip().strip('"')
    return value.lower() if _SHA256_HEX.fullmatch(value) else None


def _seed_digest(digest: "hashlib._Hash", path: Path, length: int) -> None:
    """把磁盘上已有残片的前 ``length`` 字节喂给摘要器（续传时前缀不重新下载）。"""
    remaining = length
    with open(path, "rb") as f:
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                return
            digest.update(chunk)
            remaining -= len(chunk)


def hash_file(digest: "hashlib._Hash", path: Path) -> None:
    """整份文件喂给摘要器（写满的残片装箱前校验）。"""
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)


async def download_resumable(
    client: httpx.AsyncClient,
    *,
    url: str,
    dest_file: Path,
    size: int,
    cancel_event: asyncio.Event,
    label: str,
    params: dict | None = None,
    on_bytes=None,
    base_bytes: int = 0,
    extra_headers: dict[str, str] | None = None,
    error_cls: type[RuntimeError] = HttpDownloadError,
    expected_sha256: str | None = None,
) -> None:
    """单文件 Range 续传到 ``<dest_file>.part``，完成后 ``os.replace`` 原子落定。

    ``label`` 只用于错误文案（调用方传仓库相对路径等可定位信息）。
    ``on_bytes``（可选 async 回调）每个 chunk 收到「本次下载累计已写字节」=
    ``base_bytes``（之前文件之和）+ 本文件已写——单个 17GB 权重就是一个文件，
    只报文件边界等于全程无进度。节流由调用方负责，本函数逐 chunk 调用。
    ``extra_headers`` 是调用方额外请求头（如 ModelScope token），内部再叠加 Range。
    ``error_cls`` 让 manager / ModelScope 各自保留自己的错误类型。
    ``expected_sha256`` 为清单里的对象哈希；不给时用响应头 ``X-Linked-Etag`` 兜底。
    哈希不符会删掉残片（留着只会让下次重试接着失败），由上层重下。
    """
    pp = Path(str(dest_file) + ".part")
    part_exists = pp.exists()
    have = pp.stat().st_size if part_exists else 0
    if part_exists and have > size:
        # 残片比目标还长（源文件变小 / 换了来源）：不可能是正确前缀，直接丢弃
        pp.unlink(missing_ok=True)
        part_exists = False
        have = 0
    if part_exists and have == size:
        # 上一轮已写满但在 replace 前中断（fsync 后进程被杀）：直接落定。
        # 不短路则发出 Range: bytes=size- → 服务端必返 416，重试稳定三连败后
        # _cleanup_part 把已完整的权重删掉全量重下。
        # part_exists 必须显式判：0 字节文件（空 README 等）没有 .part 时
        # have==0==size 也会成立，对不存在的 .part replace 会直接抛错
        # 落定前仍要过哈希：写满但内容是坏的残片不能因为「长度对了」就装箱
        if expected_sha256:
            digest = hashlib.sha256()
            await asyncio.to_thread(hash_file, digest, pp)
            if digest.hexdigest() != expected_sha256.strip().lower():
                pp.unlink(missing_ok=True)
                raise error_cls(f"内容校验失败（sha256 不符）：{label}（已删除残片）")
        os.replace(pp, dest_file)
        return

    # 只在确实要比对哈希时才算：一个几百 GB 的仓库全程跑 sha256 是白烧 CPU，
    # 而清单没给哈希、响应头也没有对象哈希时算出来也没人比。``digest`` 可能在拿到
    # 响应头之后才建立（见循环里的 ``X-Linked-Etag`` 兜底）。
    expected_sha256 = (expected_sha256 or "").strip().lower() or None
    digest: "hashlib._Hash | None" = None
    if expected_sha256:
        digest = hashlib.sha256()
        if have > 0:
            # 续传：残片前缀不再下载，但哈希必须把它算进去
            await asyncio.to_thread(_seed_digest, digest, pp, have)

    while True:
        request_headers = dict(extra_headers or {})
        # 强制不压缩：Content-Length 要与落盘字节可比，否则大小校验失去意义
        request_headers.setdefault("Accept-Encoding", "identity")
        if have > 0:
            request_headers["Range"] = f"bytes={have}-"

        # 传输层异常（连接被对端掐断、TLS 中断、读超时）必须转成调用方的错误类型：
        # 裸抛 httpx 异常会绕过各下载器的错误契约，用户看到的是
        # "RemoteProtocolError: peer closed connection..." 这种没有文件名的英文栈。
        # 4.78GB 的权重在 CDN 侧断连是实测会发生的，不是理论情况。
        # 异常照样往外抛（由上层重试 + 从 .part 续传），只是换了类型和文案。
        try:
            stream_ctx = client.stream("GET", url, params=params, headers=request_headers)
        except httpx.HTTPError as e:
            raise error_cls(f"请求失败：{label}（{type(e).__name__}: {e}）") from e

        async with stream_ctx as resp:
            if resp.status_code == 404:
                raise error_cls(f"文件不存在：{label}")
            if resp.status_code == 416 and have > 0:
                # Range 起点越界（.part 比当前文件长，典型为两轮之间源文件变更变小）。
                # 416 的 body 是空的——必须删 .part 重发不带 Range 的请求从头重写；
                # 走通用错误分支则每次重试拿到同一 .part、每次 416，续传死锁
                pp.unlink(missing_ok=True)
                have = 0
                if digest is not None:
                    digest = hashlib.sha256()
                continue
            if resp.status_code not in (200, 206):
                raise error_cls(f"GET file {label} 失败：HTTP {resp.status_code}")

            cr = _parse_content_range(resp.headers.get("Content-Range"))
            if resp.status_code == 206:
                # 206 必须从请求的偏移开始：确有 CDN 应答 206 却从 0 发数据（manager 侧
                # remote_gguf 对同款风险已设防），盲信会在偏移处 append 错位数据
                if cr is None or cr[0] != have:
                    raise error_cls(
                        f"Content-Range 起点不符：{label}（期望 {have}，实得 {cr[0] if cr else '缺失'}）"
                    )
                mode, offset = "ab", have
            elif have > 0 and cr is not None and cr[0] == have:
                # 200 但只回了请求的那一段（ModelScope 非 LFS 文件就是这样）。
                # 按续传追加；当「忽略 Range」从头重写会把后半截写到文件开头
                mode, offset = "ab", have
            elif have > 0:
                # 请求了 Range，回了 200，且没有可用的 Content-Range：这份 body 的
                # 语义不明——既可能真是完整文件，也可能只回了后半截（此时
                # Content-Length 仍是整文件长度，光看它对不出来）。拿它去覆盖已有
                # 残片，一旦是后者就得到一个字节数对不上的文件，且每次重试都重复
                # 同一个错误、永不收敛。改为丢弃这份 body，不带 Range 重新请求一次。
                have = 0
                if digest is not None:
                    digest = hashlib.sha256()
                continue
            else:
                # 本来就没请求 Range：完整文件，从头写
                mode, offset = "wb", 0

            announced = _declared_total(resp, offset)
            if announced is not None and announced != size:
                # 源文件在两轮之间变了 / 清单过期：继续下只会得到一个大小不对的文件
                raise error_cls(
                    f"服务端声明的总长与清单不符：{label}（声明 {announced}，清单 {size}）"
                )
            # 清单没给哈希时，LFS CDN 的 X-Linked-Etag 兜底（非 LFS 的 ETag 是
            # inode 标记，_sha_from_header 只认 64 位十六进制）
            if expected_sha256 is None:
                expected_sha256 = _sha_from_header(resp)
                if expected_sha256:
                    digest = hashlib.sha256()
                    if offset > 0:
                        await asyncio.to_thread(_seed_digest, digest, pp, offset)

            chunk_size = settings.download_chunk_size
            written = 0
            with open(pp, mode) as f:
                try:
                    async for chunk in resp.aiter_bytes(chunk_size):
                        if cancel_event.is_set():
                            raise DownloadCancelled()
                        if offset + written + len(chunk) > size:
                            # 超出清单大小：写下去只会得到一个更长但仍错误的文件
                            raise error_cls(f"响应超出清单大小：{label}（清单 {size}）")
                        # 写盘走 to_thread 与 fsync 同口径：1MiB 同步写在 NFS/写回压力下
                        # 可阻塞事件循环数十至数百毫秒（心跳漏报、其它 RPC 被阻塞）
                        await asyncio.to_thread(f.write, chunk)
                        written += len(chunk)
                        if digest is not None:
                            digest.update(chunk)
                        if on_bytes is not None:
                            await on_bytes(base_bytes + offset + written)
                except httpx.HTTPError as e:
                    # 传输中断同上面的请求阶段：换成调用方的错误类型再抛。
                    # 已经写进 .part 的字节原样保留，下次续传接着来
                    raise error_cls(
                        f"传输中断：{label}（收到 {offset + written} 字节，{type(e).__name__}）"
                    ) from e
                # flush + fsync 移出事件循环（fsync 阻塞盘 flush，慢盘上可能数百 ms~秒级；
                # 同步调用会卡住整个节点 event loop——心跳漏报、其它 RPC/上报被阻塞）
                await asyncio.to_thread(_flush_and_fsync, f)

            if announced is not None and offset + written != announced:
                # 短包：连接提前结束。.part 保留着已收到的字节，下次续传接着来，
                # 不要在这里删掉——删了就得从 0 重下（大文件上是几分钟的浪费）
                raise error_cls(
                    f"响应不完整：{label}（声明 {announced} 字节，实收 {offset + written}）"
                )
        break

    actual = pp.stat().st_size
    if actual != size:
        raise error_cls(f"size 不匹配：{label}（期望 {size}，实际 {actual}）")
    if expected_sha256:
        if digest is None:
            # 没在流式里建摘要的路径（响应头没带哈希、此刻才补上）：整文件算一次
            digest = hashlib.sha256()
            await asyncio.to_thread(hash_file, digest, pp)
        if digest.hexdigest() != expected_sha256:
            # 内容损坏：残片留着只会让下次接着校验同一份坏数据，删掉重下
            pp.unlink(missing_ok=True)
            raise error_cls(f"内容校验失败（sha256 不符）：{label}")
    os.replace(pp, dest_file)
