"""ModelScope 下载器（Node 侧）：自建 HTTP，逐文件 Range 续传。

不引入 ModelScope SDK：文件清单走公开 API，文件内容走 resolve URL（支持 Range），
复用 :func:`_download.download_resumable` 的续传 / 取消 / 进度 / 哈希校验 / 原子落定。
私有仓库把 token 同时放进 ``Authorization: Bearer`` 与 ``m_session_id`` cookie。

落盘约定与 HF/manager 下载器一致：文件按 **仓库相对路径** 写入 ``local_dir``，
下载开始前原子写入期望 manifest，``_verify_existing`` 据此判定中断目录未完成。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

from llmpod.apps.node.executors._download import download_resumable, hash_file
from llmpod.apps.node.executors._hf import _allow_patterns, _is_junk, _matches
from llmpod.apps.node.executors.base import ByteProgressReporter, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
from llmpod.config.constants import EnsureModelStage, TaskStatus
from llmpod.config.settings import settings
from llmpod.utils.modelscope_listing import ModelScopeListingError, list_repo_files
from llmpod.utils.modelscope_ref import (
    MODELSCOPE_DEFAULT_REVISION,
    ModelScopeFile,
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
        try:
            listing = await list_repo_files(
                client, endpoint, repo_id, request_rev, headers=auth_headers, subpath=subpath
            )
        except ModelScopeListingError as e:
            raise ModelScopeDownloadError(str(e)) from e
        all_files = listing.files
        if not all_files:
            raise ModelScopeDownloadError(f"ModelScope 仓库 {repo_id} 没有可下载文件")
        if listing.walked:
            log.info(
                "ModelScope 仓库 %s 走分目录补枚举，得到 %d 个文件", repo_id, len(all_files)
            )

        resolve_rev = explicit_rev or listing.short_commit or request_rev
        files = _select_files(all_files, subpath, extra_files)
        total_bytes = sum(size for _path, size, _sha in files) or None

        dest_root = Path(local_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        # 下载开始前原子写期望清单：中断/失败留下的部分目录被 _verify_main 判「未完成」，
        # 下一 attempt 按 size 跳过已完成文件（与 manager 下载器同口径）
        _write_manifest(dest_root, [path for path, _size, _sha in files])

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
        for idx, (rel, size, sha256) in enumerate(files):
            if cancel_event.is_set():
                log.info("ModelScope download cancelled: repo=%s done=%d/%d", repo_id, idx, len(files))
                raise DownloadCancelled()

            dest_file = safe_resolve(dest_root, dest_root / rel)
            if dest_file is None:
                raise ModelScopeDownloadError(f"路径越界，拒绝写入：{rel}")
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            # 已存在且哈希一致：视为上一轮下完的文件。清单没给哈希时不跳过（重下，
            # 响应头 X-Linked-Etag 仍兜底）——只比大小会让同长度的旧版本/位翻转
            # 永久通过，判据见 _existing_file_ok
            if dest_file.is_file() and await _existing_file_ok(dest_file, size, sha256):
                bytes_done += size
                await reporter.report(bytes_done, force=True)
                continue

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
                expected_sha256=sha256,
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


def _auth_headers(token: str | None) -> dict[str, str]:
    """ModelScope 私有仓库鉴权头：Bearer 覆盖新接口，``m_session_id`` cookie 覆盖旧接口。"""
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}", "Cookie": f"m_session_id={token}"}


async def _existing_file_ok(path: Path, size: int, sha256: str | None) -> bool:
    """已存在的目标文件能否当作「已完成」跳过。

    大小不符一律重下；**清单没给哈希时同样不跳过**——只比大小的跳过会让长度恰好
    相同的旧版本文件（或位翻转为同长度）永久通过，而那正是校验阶段唯一会放过的
    东西。重下走 download_resumable：其响应头 ``X-Linked-Etag`` 兜底（LFS 对象）
    仍然生效。清单不带合法 sha256 的基本只有非 LFS 小文件（LFS 对象恒带 sha256），
    重下成本可忽略。
    """
    if sha256 is None:
        return False
    try:
        if path.stat().st_size != size:
            return False
    except OSError:
        return False
    digest = hashlib.sha256()
    await asyncio.to_thread(hash_file, digest, path)
    return digest.hexdigest() == sha256


def _select_files(
    files: list[ModelScopeFile], subpath: str | None, extra_files: list[str] | None
) -> list[ModelScopeFile]:
    """按子路径/配套件收窄、跳过噪音与内部状态文件，并校验大小已知。"""
    patterns = _allow_patterns(subpath, extra_files)
    out: list[ModelScopeFile] = []
    seen: set[str] = set()
    for item in files:
        path = item.path
        if path in seen or path == _MANIFEST_NAME or path.endswith(".part"):
            continue
        if not _matches(path, patterns) or _is_junk(path):
            continue
        if item.size is None:
            raise ModelScopeDownloadError(f"文件大小缺失，无法校验下载完整性：{path}")
        seen.add(path)
        out.append(item)

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
