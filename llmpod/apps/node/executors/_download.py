"""共享 HTTP Range 续传原语（Node 侧）。

从 manager 文件下载器抽出，供 manager 与 ModelScope 下载器共用：单文件写到
``<dest>.part``，存在残片则发 ``Range: bytes=N-`` 续传，服务端忽略 Range（200）或
起点越界（416）则从头重写；206 必须校验 ``Content-Range`` 起点与请求偏移一致，
完成后校验大小并 ``os.replace`` 原子落定。

调用方负责 URL / 查询参数 / 错误类型；本模块不关心来源协议。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

from llmpod.apps.node.executors.ensure_model import DownloadCancelled
from llmpod.config.settings import settings


class HttpDownloadError(RuntimeError):
    """HTTP 下载失败的默认错误类型；调用方可传入自己的子类。"""


def _flush_and_fsync(f) -> None:
    """flush + fsync（同步、阻塞盘 flush）——供 ``to_thread`` 移出事件循环。"""
    f.flush()
    os.fsync(f.fileno())


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
) -> None:
    """单文件 Range 续传到 ``<dest_file>.part``，完成后 ``os.replace`` 原子落定。

    ``label`` 只用于错误文案（调用方传仓库相对路径等可定位信息）。
    ``on_bytes``（可选 async 回调）每个 chunk 收到「本次下载累计已写字节」=
    ``base_bytes``（之前文件之和）+ 本文件已写——单个 17GB 权重就是一个文件，
    只报文件边界等于全程无进度。节流由调用方负责，本函数逐 chunk 调用。
    ``extra_headers`` 是调用方额外请求头（如 ModelScope token），内部再叠加 Range。
    ``error_cls`` 让 manager / ModelScope 各自保留自己的错误类型。
    """
    pp = Path(str(dest_file) + ".part")
    part_exists = pp.exists()
    have = pp.stat().st_size if part_exists else 0
    if part_exists and have == size:
        # 上一轮已写满但在 replace 前中断（fsync 后进程被杀）：直接落定。
        # 不短路则发出 Range: bytes=size- → 服务端必返 416，重试稳定三连败后
        # _cleanup_part 把已完整的权重删掉全量重下。
        # part_exists 必须显式判：0 字节文件（空 README 等）没有 .part 时
        # have==0==size 也会成立，对不存在的 .part replace 会直接抛错
        os.replace(pp, dest_file)
        return

    while True:
        request_headers = dict(extra_headers or {})
        if have > 0:
            request_headers["Range"] = f"bytes={have}-"

        async with client.stream("GET", url, params=params, headers=request_headers) as resp:
            if resp.status_code == 404:
                raise error_cls(f"文件不存在：{label}")
            if resp.status_code == 416 and have > 0:
                # Range 起点越界（.part 比当前文件长，典型为两轮之间源文件变更变小）。
                # 416 的 body 是空的——必须删 .part 重发不带 Range 的请求从头重写；
                # 走通用错误分支则每次重试拿到同一 .part、每次 416，续传死锁
                pp.unlink(missing_ok=True)
                have = 0
                continue
            if resp.status_code == 200 and have > 0:
                # server 忽略 Range → 从头重写
                have = 0
            elif resp.status_code not in (200, 206):
                raise error_cls(f"GET file {label} 失败：HTTP {resp.status_code}")
            if resp.status_code == 206:
                # 206 必须从请求的偏移开始：确有 CDN 应答 206 却从 0 发数据（manager 侧
                # remote_gguf 对同款风险已设防），盲信会在偏移处 append 错位数据——
                # 最终 size 校验只能拦总量不符，内容错位且总长恰好对上时整份静默损坏
                cr = resp.headers.get("Content-Range", "")
                try:
                    start = int(cr.split(" ", 1)[1].split("-", 1)[0])
                except (IndexError, ValueError):
                    start = -1
                if start != have:
                    raise error_cls(f"Content-Range 起点不符：{label}（期望 {have}，实得 {start}）")

            chunk_size = settings.download_chunk_size
            written = 0
            with open(pp, "ab" if have > 0 else "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size):
                    if cancel_event.is_set():
                        raise DownloadCancelled()
                    # 写盘走 to_thread 与 fsync 同口径：1MiB 同步写在 NFS/写回压力下
                    # 可阻塞事件循环数十至数百毫秒（心跳漏报、其它 RPC 被阻塞）
                    await asyncio.to_thread(f.write, chunk)
                    written += len(chunk)
                    if on_bytes is not None:
                        await on_bytes(base_bytes + have + written)
                # flush + fsync 移出事件循环（fsync 阻塞盘 flush，慢盘上可能数百 ms~秒级；
                # 同步调用会卡住整个节点 event loop——心跳漏报、其它 RPC/上报被阻塞）
                await asyncio.to_thread(_flush_and_fsync, f)
        break

    if pp.stat().st_size != size:
        raise error_cls(f"size 不匹配：{label}（期望 {size}，实际 {pp.stat().st_size}）")
    os.replace(pp, dest_file)
