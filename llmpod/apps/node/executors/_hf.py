"""HuggingFace 下载器（Node 侧）：逐文件 ``hf_hub_download``，支持 cancel + 字节级进度 + 续传。

设计权衡（vs 单次 ``snapshot_download``）：单次 ``snapshot_download`` 是阻塞整体调用，放
``asyncio.to_thread`` 后**无法**中途 cancel（线程不可杀），也难逐文件报进度。本下载器按文件
枚举（``HfApi.repo_info(files_metadata=True)``，一次拿文件名+大小）逐个 ``hf_hub_download``，
在**文件之间**轮询 ``cancel_event``：

- 真正可取消（命中 cancel 后不再发起后续文件下载）；
- 真正可报进度（``bytes_done/bytes_total``，**单文件内部**也报——见下）；
- ``hf_hub_download`` 内置 HTTP Range 续传 + 完整文件跳过（幂等重试 / 断点续传）。

**进度上报契约**：``stage="downloading"``（一等字段）+ ``extra`` 冻结键
``bytes_done``（int）、``bytes_total``（int|None）、``bytes_per_sec``（int，节流放行点
附 EMA 速率，与镜像拉取同口径）。**不**含文件名/计数等非冻结键——Manager 消费方（投影）
只读这些键。``verifying``/``finished`` 阶段由 ``EnsureModelExecutor`` 在下载前后上报，
本模块只负责 downloading。

**单文件内的进度**：``hf_hub_download`` 不暴露回调，但接受 ``tqdm_class``——注入一个
子类把本文件累计字节写进共享 dict，事件循环侧的轮询协程按固定节奏读出并交节流器上报。
一个 17GB 的 GGUF 只算「一个文件」，只报文件边界的话前端整段下载都是 0%——用户会以为
下载没开始。

**cancel 粒度 = 每个 chunk**：文件之间轮询 ``cancel_event``；进行中的单文件靠注入的 tqdm
钩子检查（每 chunk 一次）并抛 :class:`DownloadCancelled`——一个 17GB 的权重就是一个文件，
只按文件边界取消等于「下载中途停不下来」。半成品由 ``hf_hub_download`` 的续传机制托管
（非 ``.part``），取消时由 ``EnsureModelExecutor`` 统一清理。

**安全**：``hf_token`` 仅经 ``token`` 参数注入 ``HfApi``，**绝不**入 ``local_tasks`` / 日志 /
``report`` 的 ``message`` 或 ``extra``。本模块不记录 token 值。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from llmpod.apps.node.executors.base import ByteProgressReporter, ReportFn
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
from llmpod.config.settings import settings
from llmpod.config.constants import EnsureModelStage, TaskStatus

log = logging.getLogger("llmpod.apps.node.executors._hf")

# 工作线程下载期间的进度轮询节奏（秒）：tqdm 回调不能直接 await，由轮询协程转述；
# 与 ByteProgressReporter 的节拍对齐（再快也全被压掉，纯浪费协程唤醒）。
_PROGRESS_POLL_SECONDS = 2.0


def _repo_id(source_url: str) -> str:
    """``source_url`` → HF repo_id（``org/name``）。

    bare repo id（``Qwen/Qwen2.5-0.5B``）原样返回；完整 URL（含 ``/tree/main/Q4_K_M`` 等）
    取 path 前两段（``org/name``），忽略 verb/rev/subpath——与 manager 侧 ``parse_hf_ref`` 同构，
    Node 不依赖 manager，故本地复刻 repo_id 提取。
    """
    s = (source_url or "").strip()
    if s.startswith(("http://", "https://")):
        parts = [p for p in urlparse(s).path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return s.split("?")[0]


def _endpoint_from_url(source_url: str, fallback: str) -> str:
    """``source_url`` 若为完整 http(s) URL → 取 ``scheme://host`` 作为 HF endpoint（**优先于**
    settings）；bare repo id → ``fallback``（settings）。

    与 manager 侧 ``parse_hf_ref`` 同构：用户贴 ``https://hf-mirror.com/Qwen/...`` 等镜像 URL 时，
    **node 下载也走该镜像**——不必另在 node 配 ``LLMPOD_MODEL_SOURCE_HF_ENDPOINT``。否则 inspect
    走镜像成功、下载却连 huggingface.co 失败的不一致。
    """
    s = (source_url or "").strip()
    if s.startswith(("http://", "https://")):
        p = urlparse(s)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    return fallback


def _allow_patterns(source_subpath: str | None, extra_files: list[str] | None = None) -> list[str] | None:
    """``source_subpath`` 空 → ``None``（全量）；非空 → ``[subpath, subpath + '/**', *extra_files]``。

    ``extra_files``：配套文件（如 GGUF 视觉投影器 ``mmproj-*.gguf``）——主模型按 subpath 收窄时
    仍要一起下发，否则容器缺件起不来。**无 subpath 时不收窄**：全量本来就含配套件，若把
    extras 当唯一白名单，裸 repo id 添加视觉模型就只下一个 mmproj（主权重缺失仍报 SUCCESS）。
    """
    sp = (source_subpath or "").strip().strip("/")
    if not sp:
        return None
    extras = [f.strip().strip("/") for f in (extra_files or []) if f and f.strip()]
    return [sp, f"{sp}/**", *extras]


def _matches(name: str, patterns: list[str] | None) -> bool:
    """是否命中任一 allow pattern（``None`` = 全量放行）。"""
    if not patterns:
        return True
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# 仓库内常见噪音/元数据文件：HF 镜像端可能拒绝访问（403），且对推理无用。
# 下载时整体跳过（不含模型权重 / tokenizer / config）。
_JUNK_BASENAMES: frozenset[str] = frozenset({".ds_store", ".gitattributes", ".gitignore", ".gitmodules", "thumbs.db"})


def _is_junk(name: str) -> bool:
    """是否为下载时跳过的事务性噪音文件（macOS 元数据 / git 隐藏文件 / 系统垃圾）。

    判定只看 basename（大小写不敏感）；目录型噪音（如 ``imgs/.DS_Store``）靠 basename
    命中，不依赖路径层级。仅作下载跳过，不参与 manifest 文件清单。
    """
    base = name.rsplit("/", 1)[-1].lower()
    return not base or base.startswith("._") or base in _JUNK_BASENAMES


async def download_hf(
    *,
    source_url: str,
    revision: str | None,
    source_subpath: str | None,
    token: str | None,
    local_dir: Path,
    report: ReportFn,
    cancel_event: asyncio.Event,
    extra_files: list[str] | None = None,
) -> None:
    """逐文件下载 repo 到 ``local_dir``；cancel 命中抛 :class:`DownloadCancelled`。

    ``token`` 仅用于构造 ``HfApi``（鉴权 + 镜像 endpoint），不入任何上报/日志。
    进度 ``stage=downloading`` + ``extra`` 冻结键 ``bytes_done/bytes_total``（节流放行点
    附 ``bytes_per_sec`` EMA 速率，与镜像拉取同口径）。
    ``extra_files``：配套文件（如 GGUF 视觉投影器），与主模型一起下载。
    """
    from huggingface_hub import HfApi

    repo_id = _repo_id(source_url)
    rev = revision or "main"
    patterns = _allow_patterns(source_subpath, extra_files)

    # endpoint：用户贴镜像 URL（含 scheme://host）→ 优先用其 host；bare repo id → settings 兜底。
    # 与 manager inspect 同构，避免「inspect 走镜像、下载连 huggingface.co」的不一致。
    endpoint = _endpoint_from_url(source_url, settings.model_source_hf_endpoint)
    # token（gated repo）+ endpoint 绑定到单个 HfApi 实例
    api = HfApi(endpoint=endpoint, token=token)

    # 1. 枚举文件清单 + 大小（files_metadata=True；一次拿 names+sizes 供 bytes_total）；
    #    allow（source_subpath）与 junk 过滤在此一并完成
    info = await asyncio.to_thread(api.repo_info, repo_id, revision=rev, files_metadata=True)
    sibs = [
        s
        for s in (getattr(info, "siblings", None) or [])
        if (fn := _sibling_rfilename(s)) and _matches(fn, patterns) and not _is_junk(fn)
    ]
    if not sibs:
        raise RuntimeError(f"仓库 {repo_id}@{rev} 无匹配文件（source_subpath={source_subpath!r}）")
    files = [(_sibling_rfilename(s), _sibling_size(s)) for s in sibs]
    # bytes_total：已知大小之和；全未知/0 → None（消费方写 NULL）
    bytes_total = sum(sz for _, sz in files if isinstance(sz, int) and sz >= 0) or None
    total = len(files)

    await report(
        TaskStatus.RUNNING,
        0,
        stage=EnsureModelStage.DOWNLOADING,
        extra={"bytes_done": 0, "bytes_total": bytes_total},
    )
    log.info("HF download start: repo=%s rev=%s files=%d bytes_total=%s", repo_id, rev, total, bytes_total)

    # 2. 逐文件下载：文件之间轮询 cancel；hf_hub_download 内置 Range 续传 + 完整跳过。
    #    reporter 整次下载共用（跨文件连续）：节流状态与速率 EMA 不因文件边界清零；
    #    文件基座（本文件开始前的累计字节）由本循环持有——轮询报 base + 本文件已下，
    #    边界 force 检查点报累计真值（跳过节拍但不污染速率 EMA）
    reporter = ByteProgressReporter(report, bytes_total=bytes_total, stage=EnsureModelStage.DOWNLOADING)
    bytes_done = 0
    for idx, (rel, size) in enumerate(files):
        if cancel_event.is_set():
            log.info("HF download cancelled: repo=%s done=%d/%d", repo_id, idx, total)
            raise DownloadCancelled()
        await _download_one_file(
            api,
            repo_id=repo_id,
            rel=rel,
            rev=rev,
            local_dir=local_dir,
            reporter=reporter,
            base_bytes=bytes_done,
            cancel_event=cancel_event,
        )
        if isinstance(size, int) and size >= 0:
            bytes_done += size
        # 文件边界检查点：字节累计真值（含被跳过的已存在文件），force 绕过节拍——
        # 单调守卫与速率基线仍由 reporter 统一持有，wire 上不会出现回退
        await reporter.report(bytes_done, force=True)

    await report(
        TaskStatus.RUNNING,
        100,
        stage=EnsureModelStage.DOWNLOADING,
        extra={"bytes_done": bytes_done, "bytes_total": bytes_total},
    )
    log.info("HF download done: repo=%s files=%d bytes_done=%s", repo_id, total, bytes_done)

    # 全部下载成功 → 落盘期望文件清单（相对 local_dir，含 processor/tokenizer 与 layers-N 分片）。
    # ``_verify_existing`` 以此作权威完整性判定；半下载/中断不会走到这里，故无清单。
    (local_dir / _MANIFEST_NAME).write_text(json.dumps({"files": [rel for rel, _ in files]}))


def _progress_bar_cls(state: dict, cancel_event: asyncio.Event) -> type:
    """构造绑定到 ``state`` 的 tqdm 子类：``update`` 时把本文件累计字节写进 ``state["cur"]``。

    下载跑在 ``asyncio.to_thread`` 的工作线程里，tqdm 回调也在该线程——只写一个普通 dict
    的 int 值（GIL 下原子），事件循环侧的轮询协程读取后转成 ``report``，避免跨线程调度协程。

    同时承担**协作式取消**：下载线程里没有 await 点，单文件中途停不下来（``hf_hub_download``
    不暴露 cancel 参数），而一个 17GB 的权重就是一个文件——靠这个每 chunk 的回调检查
    ``cancel_event`` 并抛 :class:`DownloadCancelled`。该异常能穿透 ``hf_hub_download``
    （其重试只吞 httpx 网络异常），由 ``EnsureModelExecutor`` 捕获后报 CANCELLED 并清半成品。
    ``asyncio.Event.is_set()`` 只读一个 bool 属性，跨线程读取安全。
    """
    from tqdm.auto import tqdm

    class _ProgressTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._seen = 0

        def update(self, n=None):
            if cancel_event.is_set():
                raise DownloadCancelled()
            result = super().update(n)
            # 进度条被 disable（HF_HUB_DISABLE_PROGRESS_BARS / 非主进程）时 self.n 不增，
            # 故另行按传入增量累计，取两者较大值——无论如何都要有字节数。
            self._seen += 1 if n is None else int(n)
            state["cur"] = max(self._seen, int(self.n or 0))
            return result

    return _ProgressTqdm


async def _poll_progress(state: dict, reporter: ByteProgressReporter, base_bytes: int) -> None:
    """工作线程下载期间的进度转述：按固定节奏把「文件基座 + 本文件已下」交节流器上报。"""
    while True:
        await asyncio.sleep(_PROGRESS_POLL_SECONDS)
        await reporter.report(base_bytes + state.get("cur", 0))


async def _download_one_file(
    api,
    *,
    repo_id: str,
    rel: str,
    rev: str,
    local_dir: Path,
    reporter: ByteProgressReporter,
    base_bytes: int,
    cancel_event: asyncio.Event,
) -> None:
    """单文件 ``hf_hub_download`` + 字节级进度 + 协作式取消（tqdm 钩子 → 轮询协程 → 节流上报）。

    只报 downloading 阶段的中间进度（经 ``reporter`` 节流）；文件边界与终值由调用方上报。
    ``base_bytes`` 是**本文件开始前**的整仓累计字节（调用方持有基座）——轮询报
    ``base_bytes + 本文件已下``，reporter 侧不存在「再叠加一次已含本文件的累计」的双计。
    取消经 tqdm 钩子抛出，由调用方（``EnsureModelExecutor``）捕获为 CANCELLED。
    """
    state = {"cur": 0}  # 本文件已下字节（tqdm 钩子写）
    poller = asyncio.create_task(_poll_progress(state, reporter, base_bytes))
    try:
        await asyncio.to_thread(
            api.hf_hub_download,
            repo_id=repo_id,
            filename=rel,
            revision=rev,
            local_dir=str(local_dir),
            tqdm_class=_progress_bar_cls(state, cancel_event),
        )
    finally:
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller


def _sibling_rfilename(s) -> str:
    """``RepoSibling``（dataclass）或 ``{rfilename}`` dict → 文件名。"""
    rf = getattr(s, "rfilename", None)
    if rf is not None:
        return str(rf)
    if isinstance(s, dict):
        return str(s.get("rfilename") or s.get("filename") or "")
    return ""


def _sibling_size(s) -> int | None:
    """``RepoSibling`` 或 dict → 文件大小（bytes）；缺失/非数字 → None。"""
    sz = getattr(s, "size", None)
    if sz is None and isinstance(s, dict):
        sz = s.get("size")
    if isinstance(sz, bool) or not isinstance(sz, int):  # bool 是 int 子类，排除
        return None
    return sz if sz >= 0 else None
