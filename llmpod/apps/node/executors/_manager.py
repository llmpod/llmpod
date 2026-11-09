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

from llmpod.apps.node.executors._download import download_resumable
from llmpod.apps.node.executors.base import ByteProgressReporter, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
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

            await download_resumable(
                client,
                url=f"{base}/file",
                params={**qparams, "path": rel},
                dest_file=dest_file,
                size=size,
                cancel_event=cancel_event,
                label=rel,
                on_bytes=reporter.report,
                base_bytes=bytes_done,
                error_cls=ManagerDownloadError,
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
