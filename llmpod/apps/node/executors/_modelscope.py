"""ModelScope 下载器（Node 侧）：自建 HTTP，逐文件 Range 续传。

不引入 ModelScope SDK：文件清单走公开 API，文件内容走 resolve URL（支持 Range），
复用 :func:`_download.download_resumable` 的续传 / 取消 / 进度 / 原子落定。
仅支持公开模型；不读取 token。

落盘约定与 HF/manager 下载器一致：文件按 **仓库相对路径** 写入 ``local_dir``，
下载开始前原子写入期望 manifest，``_verify_existing`` 据此判定中断目录未完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from llmpod.apps.node.executors._download import download_resumable
from llmpod.apps.node.executors._hf import _allow_patterns, _is_junk, _matches
from llmpod.apps.node.executors.base import ByteProgressReporter, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
from llmpod.config.constants import EnsureModelStage, TaskStatus
from llmpod.config.settings import settings
from llmpod.utils.modelscope_ref import (
    MODELSCOPE_DEFAULT_REVISION,
    extract_modelscope_files,
    latest_modelscope_short_id,
    modelscope_files_api_url,
    modelscope_resolve_url,
    parse_modelscope_ref,
)
from llmpod.utils.safe_path import safe_resolve

log = logging.getLogger("llmpod.apps.node.executors._modelscope")


class ModelScopeDownloadError(RuntimeError):
    pass


async def download_modelscope(
    *,
    source_url: str,
    revision: str | None,
    source_subpath: str | None,
    local_dir: Path,
    report: ReportFn,
    cancel_event: asyncio.Event,
    token: str | None = None,
    extra_files: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """下载 ModelScope 仓库到 ``local_dir``；cancel 命中抛 ``DownloadCancelled``。

    ``token`` 用于私有仓库（同时发 Bearer 与 ``m_session_id`` cookie），只经内存参数
    传递，不入日志/上报；为空时按公开仓库下载。
    ``transport`` 仅供测试注入；生产为 None 走真实 HTTP。
    """
    try:
        repo_id, endpoint, url_subpath, url_rev = parse_modelscope_ref(source_url)
    except ValueError as e:
        raise ModelScopeDownloadError(str(e)) from e
    endpoint = (endpoint or settings.model_source_modelscope_endpoint).rstrip("/")
    # URL 内显式 revision 优先于 payload；都未指定时用来源默认分支，稍后钉到具体 commit
    explicit_rev = url_rev or (revision or "").strip() or None
    request_rev = explicit_rev or MODELSCOPE_DEFAULT_REVISION
    # URL 子路径优先于 payload（用户贴的是具体变体链接时以链接为准）
    subpath = url_subpath or (source_subpath or "").strip("/") or None
    auth_headers = _auth_headers(token)

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:
        data = await _get_json(
            client,
            modelscope_files_api_url(endpoint, repo_id),
            params={"Revision": request_rev, "Recursive": "true"},
            label=repo_id,
            headers=auth_headers,
        )
        all_files = extract_modelscope_files(data)
        if not all_files:
            raise ModelScopeDownloadError(f"ModelScope 仓库 {repo_id} 没有可下载文件")

        resolve_rev = explicit_rev or latest_modelscope_short_id(data) or request_rev
        files = _select_files(all_files, subpath, extra_files)
        total_bytes = sum(size for _path, size in files) or None

        dest_root = Path(local_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        # 下载开始前原子写期望清单：中断/失败留下的部分目录被 _verify_main 判「未完成」，
        # 下一 attempt 按 size 跳过已完成文件（与 manager 下载器同口径）
        _write_manifest(dest_root, [path for path, _size in files])

        await report(
            TaskStatus.RUNNING,
            0,
            stage=EnsureModelStage.DOWNLOADING,
            extra={"bytes_done": 0, "bytes_total": total_bytes},
        )
        log.info(
            "ModelScope download start: repo=%s rev=%s files=%d bytes_total=%s",
            repo_id,
            resolve_rev,
            len(files),
            total_bytes,
        )

        reporter = ByteProgressReporter(report, bytes_total=total_bytes, stage=EnsureModelStage.DOWNLOADING)
        bytes_done = 0
        for idx, (rel, size) in enumerate(files):
            if cancel_event.is_set():
                log.info("ModelScope download cancelled: repo=%s done=%d/%d", repo_id, idx, len(files))
                raise DownloadCancelled()

            dest_file = safe_resolve(dest_root, dest_root / rel)
            if dest_file is None:
                raise ModelScopeDownloadError(f"路径越界，拒绝写入：{rel}")
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.is_file():
                try:
                    if dest_file.stat().st_size == size:
                        bytes_done += size
                        await reporter.report(bytes_done, force=True)
                        continue
                except OSError:
                    pass

            await download_resumable(
                client,
                url=modelscope_resolve_url(endpoint, repo_id, resolve_rev, rel),
                dest_file=dest_file,
                size=size,
                cancel_event=cancel_event,
                label=rel,
                on_bytes=reporter.report,
                base_bytes=bytes_done,
                extra_headers=auth_headers,
                error_cls=ModelScopeDownloadError,
            )
            bytes_done += size
            await reporter.report(bytes_done, force=True)

        await report(
            TaskStatus.RUNNING,
            100,
            stage=EnsureModelStage.DOWNLOADING,
            extra={"bytes_done": bytes_done, "bytes_total": total_bytes},
        )
        log.info("ModelScope download done: repo=%s files=%d bytes_done=%s", repo_id, len(files), bytes_done)


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict,
    label: str,
    headers: dict[str, str] | None = None,
) -> dict:
    """GET + HTTP 2xx + JSON + 业务码校验；失败抛中文下载错误。"""
    try:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ModelScopeDownloadError(
            f"无法访问 ModelScope 仓库 {label}（{type(e).__name__}）；"
            "请检查仓库是否存在、是否公开，或私有仓库 token 是否有效"
        ) from e

    if not isinstance(data, dict):
        raise ModelScopeDownloadError(f"ModelScope 仓库 {label} 响应不是 JSON 对象")
    code = data.get("Code")
    if str(code) != "200" or data.get("Success") is False:
        message = str(data.get("Message") or "")[:200]
        raise ModelScopeDownloadError(f"ModelScope 仓库 {label} 返回错误 {code}: {message}")
    return data


def _auth_headers(token: str | None) -> dict[str, str]:
    """ModelScope 私有仓库鉴权头：Bearer 覆盖新接口，``m_session_id`` cookie 覆盖旧接口。"""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Cookie": f"m_session_id={token}"}


def _select_files(
    files: list[tuple[str, int | None]], subpath: str | None, extra_files: list[str] | None
) -> list[tuple[str, int]]:
    """按子路径/配套件收窄、跳过噪音与内部状态文件，并校验大小已知。"""
    patterns = _allow_patterns(subpath, extra_files)
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for path, size in files:
        if path in seen or path == _MANIFEST_NAME or path.endswith(".part"):
            continue
        if not _matches(path, patterns) or _is_junk(path):
            continue
        if size is None:
            raise ModelScopeDownloadError(f"文件大小缺失，无法校验下载完整性：{path}")
        seen.add(path)
        out.append((path, size))

    if not out:
        raise ModelScopeDownloadError(
            f"ModelScope 仓库没有匹配文件（subpath={subpath!r}，extra_files={extra_files or []}）"
        )
    return out


def _write_manifest(root: Path, rels: list[str]) -> None:
    tmp = root / (_MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps({"files": rels}))
    os.replace(tmp, root / _MANIFEST_NAME)


__all__ = ["ModelScopeDownloadError", "download_modelscope"]
