"""远程 GGUF 头部 metadata 探测（HTTP Range 只拉文件头，不下载权重）。

GGUF 的 MoE 信息（``llama.expert_count`` 等）与量化类型（``general.file_type``）写在文件头
metadata 的 KV 表里，config.json 没有。远程 GGUF（HF ``/resolve/`` 直链 / OSS 直链）要识别它们，
只能 Range 请求拉文件最前面一段（和抓 config.json 一样是抓 metadata，不是权重张量），
交给 :mod:`gguf_meta` 解析。

量化类型在 KV 表**尾部**，首段常常够不着，所以分两档取：先拉首段 ``_MAX_HEADER_READ``
（与历史行为、成本一致），只有「首段读满且 ``file_type`` 仍未知」才续拉一段补齐到
``_MAX_HEADER_READ_TOTAL``。续拉失败退回首段结果——比不续拉只多一次请求，不会更差。

设计：
- 用 ``Range: bytes=A-B`` 只请求所需区间；即便服务器忽略 Range 返回 200，也用流式有界读，
  拿够字节数即关流，绝不把整文件灌入内存；
- 任何失败（网络错 / 非 206&200 / 解析失败）→ ``None``（best-effort，不阻断 inspect）。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from llmpod.apps.manager.model_source import gguf_meta

log = logging.getLogger("llmpod.apps.manager.model_source.remote_gguf")


def _range_start_mismatch(header: str | None, offset: int) -> bool:
    """``Content-Range: bytes <start>-<end>/<total>`` 报了**确认对不上**的起点。

    只在能读出起点且确实不等于 ``offset`` 时返回 True。头缺失或格式不认得时返回
    False（放行）——那时无从校验，拦下来只会把「服务器就是没给这个头」的正常情况
    判成失败。所以这个断言比名字里的 ``mismatch`` 弱一点：它证明的是「已知不对」，
    不是「已知对」。
    """
    if not header:
        return False
    parts = header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bytes":
        return False
    span = parts[1].split("/", 1)[0]
    start, _, _end = span.partition("-")
    try:
        return int(start) != offset
    except ValueError:
        return False


async def _fetch_range(client: httpx.AsyncClient, url: str, offset: int, length: int) -> tuple[int, bytes] | None:
    """Range 取 ``[offset, offset+length)`` → ``(status_code, bytes)``；失败 / 非 2xx → ``None``。

    status 要交回调用方：206 = 字节确实是请求的区间；200 = 服务器忽略了 Range、又从文件头
    给了内容——续拉时若照搬会与首段拼重，调用方需按 200/206 分别处理。
    有界读（拿够 length 即停）防服务器无视 Range 返 200 时把整文件灌进内存。

    206 另校验 ``Content-Range`` 的起点：状态行说「部分内容」不等于给的就是**我们要的
    那一段**（应答 206 却从 0 开始的 CDN 存在）。起点确认对不上就按失败处理——调用方
    会退回首段结果，比拼出一段错位数据安全。头缺失时不拦（无法校验，保持历史行为）。
    """
    try:
        req = client.build_request("GET", url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        resp = await client.send(req, stream=True)
        try:
            if resp.status_code not in (200, 206):
                return None
            if resp.status_code == 206 and _range_start_mismatch(resp.headers.get("Content-Range"), offset):
                log.warning(
                    "remote gguf range mismatch for %s: asked offset=%d, got Content-Range=%r",
                    url,
                    offset,
                    resp.headers.get("Content-Range"),
                )
                return None
            collected = bytearray()
            async for chunk in resp.aiter_raw():
                collected += chunk
                if len(collected) >= length:
                    break
        finally:
            await resp.aclose()
    except Exception as e:
        log.warning("remote gguf header fetch failed for %s: %s", url, e)
        return None
    return resp.status_code, bytes(collected[:length])


async def probe_gguf_metadata_remote(client: httpx.AsyncClient, url: str):
    """Range 拉远程 GGUF 文件头 → ``GGUFMetadata``（MoE + 架构形状 + 量化类型），失败 → ``None``。

    先取首段 ``gguf_meta._MAX_HEADER_READ`` 字节。首段读满（说明后面可能还有）且量化类型
    仍未解出时，再续拉一段到 ``_MAX_HEADER_READ_TOTAL``——量化类型写在 KV 表尾部，
    大词表模型首段够不着。续拉任何异常都退回首段解析结果。
    """
    first = await _fetch_range(client, url, 0, gguf_meta._MAX_HEADER_READ)
    if first is None:
        return None
    _status, head_bytes = first  # 首段不看 200/206：它总是从文件头开始，没有拼重问题
    # 解析放线程：整表遍历 KV 是纯 CPU（最密可达秒级），管理端的事件循环同时驱动
    # node 反向 WS 心跳与 reconciler，不该被它占住。
    head = await asyncio.to_thread(gguf_meta.parse_gguf_metadata_bytes, head_bytes)
    if head is None:
        return None
    # 读到量化类型了，或首段没读满（文件就这么多，再拉也没有）→ 收工
    if head.file_type is not None or len(head_bytes) < gguf_meta._MAX_HEADER_READ:
        return head

    more = await _fetch_range(client, url, len(head_bytes), gguf_meta._MAX_HEADER_READ_TOTAL - len(head_bytes))
    if more is None:
        return head  # 续拉失败：首段结果照用（量化退回文件名兜底），不比历史行为差
    status, chunk = more
    # 206：chunk 确实是首段之后的续段（_fetch_range 已校验 Content-Range 起点），拼起来；
    # 200：服务器又从头给，首段作废，只认这一次
    combined = head_bytes + chunk if status == 206 else chunk
    extended = await asyncio.to_thread(gguf_meta.parse_gguf_metadata_bytes, combined)
    # 走到这里 extended 基本不会为 None（206 下 combined 以 GGUF 魔数开头，解析必然成功，
    # 且是首段结果的超集）；真正会为 None 的是 200 分支——chunk 是整文件重发，内容异常时
    # 解析不出魔数。那一支靠这个兜底退回首段结果。
    return extended if extended is not None else head


__all__ = ["probe_gguf_metadata_remote"]
