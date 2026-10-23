"""local 源下载器（Node 侧）：从 Manager 文件服务按 manifest 分文件 HTTP Range 续传。

镜像 :func:`_hf.download_hf` 的契约——逐文件下载（文件之间轮询 ``cancel_event``，命中即
``raise DownloadCancelled``），进度 ``stage="downloading"``（一等字段）+ ``extra`` 冻结键
``bytes_done``/``bytes_total``（第二次放行起附 ``bytes_per_sec`` EMA 速率，与镜像拉取同
口径；Manager 投影只读这些键）；文件按 **manifest 相对模型根的
路径**落 ``local_dir``（与 HF 的 repo 相对结构同一口径，验在位 / ``--mmproj`` 注入同判），
让 ``EnsureModelExecutor._verify_existing`` 接受。

续传：``.part`` 存在则从其 size 发 ``Range: bytes=N-``，server 忽略 Range（200）或起点
越界（416，源文件变小/残留超长）则从头重写；size 校验后 ``os.replace(.part, final)``
原子落定。已完成（size 匹配）的文件直接跳过；``.part`` 恰好写满（上轮 replace 前中断）
直接落定不再发请求。

安全：``download_token`` 仅入 query 参数（匹配文件服务路由），**绝不**入 ``report`` 的
``message``/``extra``/日志（``NodeAgent._scrub_secrets`` 也会兜底）；每条解析路径经
``safe_resolve`` 强制位于 ``local_dir`` 之下，拒符号链接。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from llmpod.apps.node.executors.base import ByteProgressReporter, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
from llmpod.config.settings import settings
from llmpod.tunnel.schemas import Manifest
from llmpod.utils.safe_path import safe_resolve
from llmpod.config.constants import EnsureModelStage, TaskStatus

log = logging.getLogger("llmpod.apps.node.executors._manager")


class ManagerDownloadError(RuntimeError):
    pass


async def download_manager(
    *,
    manager_base_url: str,
    download_token: str | None,
    model_id: int,
    local_dir: Path,
    report: ReportFn,
    cancel_event: asyncio.Event,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """从 Manager 文件服务下载全部文件到 ``local_dir``。

    ``manager_base_url`` 形如 ``http://host:8080/api/v1/distribution``（Manager 注入到 payload）。
    ``transport`` 仅供测试注入（如 ``httpx.ASGITransport``）；生产为 None 走真实 HTTP。

    令牌寿命由 TTL 保证（默认 24h > 任务看门狗 12h）：每次 attempt 下发时重铸，
    单窗口内不会过期，401 即真失败（token 串号/密钥轮换），按下载失败走既有
    attempt 重试（重试自带新 token）。
    """
    base = (manager_base_url or "").rstrip("/")
    if not base:
        raise ManagerDownloadError("local 源缺少 manager_base_url")
    qparams = {"model_id": str(model_id), "token": download_token or ""}
    dest_root = Path(local_dir)

    # read 必须有限：传输中途连接假死（NAT/网关黑洞、对端僵死不发 FIN）时 aiter_bytes
    # 才能超时抛错，NODE_DOWNLOAD_MAX_RETRIES 的断点续传重试才有机会介入——设 None
    # 会把本应几十秒自愈的瞬时故障拖到任务级看门狗（12h）才回收。read 超时作用于
    # 「等待每一批字节」而非整个 body，慢速但流动的传输不会误触发
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:
        raw = await _get_json(client, f"{base}/manifest", qparams)
        try:
            manifest = Manifest.model_validate(raw)
        except Exception as e:
            raise ManagerDownloadError(f"manifest 响应不合约: {e}") from e
        files = manifest.files
        # total_bytes 与清单同源生成；sum 兜底仅在清单异常（total=0）时生效
        total_bytes = manifest.total_bytes or sum(f.size for f in files)
        if not files:
            raise ManagerDownloadError("manifest 为空（无文件可下载）")

        # 期望文件清单**前置**写入（原子替换）：下载开始即落盘全部期望路径，
        # ``_verify_existing`` 逐项比对——中断/失败留下的部分目录会被正确判「未完成」
        # （而非被命名启发式误判 present），下一 attempt 按清单续传（已完成文件按
        # size 跳过）。清单与文件最终一致，无需收尾重写。
        dest_root.mkdir(parents=True, exist_ok=True)
        _mf_tmp = dest_root / (_MANIFEST_NAME + ".tmp")
        _mf_tmp.write_text(json.dumps({"files": [f.path for f in files]}))
        os.replace(_mf_tmp, dest_root / _MANIFEST_NAME)

        # 首报 downloading（冻结键）
        await report(
            TaskStatus.RUNNING, stage=EnsureModelStage.DOWNLOADING, extra={"bytes_done": 0, "bytes_total": total_bytes}
        )

        # 文件按**模型根相对路径**落盘（与 HF 下载器的 repo 相对结构同一口径）：manifest
        # 的 path 本就相对模型根（含变体目录前缀），再叠 source_subpath 会双重前缀，
        # 且配套件（mmproj）会被错误压进子目录——验在位与 --mmproj 注入都按根相对找
        bytes_done = 0
        reporter = ByteProgressReporter(report, bytes_total=total_bytes, stage=EnsureModelStage.DOWNLOADING)
        for idx, mf in enumerate(files):
            if cancel_event.is_set():
                raise DownloadCancelled()
            rel = mf.path
            size = mf.size

            dest_file = safe_resolve(dest_root, dest_root / rel)
            if dest_file is None:
                raise ManagerDownloadError(f"路径越界，拒绝写入：{rel}")
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            # 已完成（size 匹配）则跳过
            if dest_file.is_file():
                try:
                    if dest_file.stat().st_size == size:
                        bytes_done += size
                        await reporter.report(bytes_done, force=True)  # 跳过文件 = 检查点，不进速率 EMA
                        continue
                except OSError:
                    pass

            await _download_one(
                client,
                base,
                qparams,
                rel,
                dest_file,
                size,
                cancel_event,
                on_bytes=reporter.report,
                base_bytes=bytes_done,
            )
            bytes_done += size
            await reporter.report(bytes_done, force=True)  # 文件边界检查点：绕节拍、不进速率 EMA


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    r = await client.get(url, params=params)
    if r.status_code != 200:
        raise ManagerDownloadError(f"GET {url} 失败：HTTP {r.status_code}")
    try:
        return r.json()
    except Exception as e:
        # 200 但 body 非 JSON（反代/网关的错误页）——裸 json.JSONDecodeError 落进
        # local_tasks.last_error 毫无指向性，与下方 manifest 校验同走错误契约
        raise ManagerDownloadError(f"GET {url} 响应体非 JSON: {e}") from e


def _flush_and_fsync(f) -> None:
    """flush + fsync（同步、阻塞盘 flush）——供 ``to_thread`` 移出事件循环。"""
    f.flush()
    os.fsync(f.fileno())


async def _download_one(
    client: httpx.AsyncClient,
    base_url: str,
    qparams: dict,
    rel: str,
    dest_file: Path,
    size: int,
    cancel_event: asyncio.Event,
    *,
    on_bytes=None,
    base_bytes: int = 0,
) -> None:
    """单文件 Range 续传到 ``<dest_file>.part``，完成后 ``os.replace`` 原子落定。

    ``on_bytes``（可选 async 回调）每个 chunk 收到「本部署累计已下字节」= ``base_bytes``
    （之前文件之和）+ 本文件已写——单个 17GB 权重就是一个文件，只报文件边界等于全程无进度。
    节流由调用方（``ByteProgressReporter``）负责，本函数逐 chunk 调用。
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

    file_params = {**qparams, "path": rel}
    while True:
        headers = {}
        if have > 0:
            headers["Range"] = f"bytes={have}-"

        async with client.stream("GET", f"{base_url}/file", params=file_params, headers=headers) as resp:
            if resp.status_code == 404:
                raise ManagerDownloadError(f"文件不存在：{rel}")
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
                raise ManagerDownloadError(f"GET file {rel} 失败：HTTP {resp.status_code}")
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
                    raise ManagerDownloadError(f"Content-Range 起点不符：{rel}（期望 {have}，实得 {start}）")

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
        raise ManagerDownloadError(f"size 不匹配：{rel}（期望 {size}，实际 {pp.stat().st_size}）")
    os.replace(pp, dest_file)
