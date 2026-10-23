"""ENSURE_MODEL 执行器：Node 自治模型分发。

声明式语义——Manager 只下发配方 + 收集上报；Node 拥有下载/校验/取消的运行时生命周期。
secrets 仅内存（executor 收到即用，不入 local_tasks/日志）。

执行流：
1. **check existing**：``local_dir`` 已就位且符合 ``source_kind`` → 直接 SUCCESS + model_path
   （无需 secrets/下载；覆盖「重启恢复」「重复下发」短路）。
2. **cancel checkpoint + 磁盘预检**（free < ``download_min_free_gb`` → FAILED ``INSUFFICIENT_DISK``）。
3. **按 source 下载**：``huggingface`` → HF downloader（依赖 ``huggingface_hub``，
   缺失则 FAILED）；``local`` → 不 present 即 FAILED（手动预置）；其他 source → FAILED。
4. **verify**（轻量，按 source_kind）→ model_path → SUCCESS。
5. **全程支持 cancel**：检测到 ``cancel_event`` → 清 ``.part`` + 报 CANCELLED。

executor 只负责模型逻辑 + 进度上报（``report`` callback）；持久化 + Manager 上报由
``NodeAgent`` 的 report 包装器接管。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from llmpod.apps.node.executors.base import Executor, ReconcileResult, ReportFn, parse_payload
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import EnsureModelPayload
from llmpod.config.settings import settings
from llmpod.config.constants import EnsureModelStage, TaskStatus
from llmpod.config.enums import ModelSource

log = logging.getLogger("llmpod.apps.node.executors.ensure_model")

_GB = 1024**3

# Node 本地瞬时重试：单次 task 内下载失败重试 N 次（不增 manager
# attempt）；hf_hub_download 内置 Range 续传，重试从断点接续而非重头。仅重试下载（verify
# 失败是确定性的，重试无益）；DownloadCancelled 不重试（用户取消）。
NODE_DOWNLOAD_MAX_RETRIES = 3
NODE_DOWNLOAD_RETRY_BACKOFF = 2.0


class DownloadCancelled(Exception):
    """downloader 检测到 ``cancel_event`` 时抛出（executor 据此清 ``.part`` + 报 CANCELLED）。"""


# 下载器签名：async (payload: EnsureModelPayload, secrets, local_dir, report, cancel_event) -> None
# 完成时模型应已落到 local_dir（executor 随后 verify）；取消时抛 DownloadCancelled。
DownloaderFn = Callable[..., Awaitable[None]]


class EnsureModelExecutor(Executor):
    """ENSURE_MODEL 执行器。

    ``downloader`` 可注入（测试用）；默认按 source 分流（HF 走专用 downloader）。
    """

    def __init__(
        self,
        *,
        store_dir: str | None = None,
        min_free_gb: float | None = None,
        downloader: DownloaderFn | None = None,
        download_retries: int | None = None,
        download_retry_backoff: float | None = None,
    ):
        self._store_dir = store_dir or settings.model_store_dir
        self._min_free_gb = settings.download_min_free_gb if min_free_gb is None else min_free_gb
        self._downloader: DownloaderFn = downloader or self._default_downloader
        self._download_retries = NODE_DOWNLOAD_MAX_RETRIES if download_retries is None else download_retries
        self._download_retry_backoff = (
            NODE_DOWNLOAD_RETRY_BACKOFF if download_retry_backoff is None else download_retry_backoff
        )

    async def execute(
        self,
        task: LocalTask,
        secrets: dict | None,
        report: ReportFn,
        cancel_event: asyncio.Event,
    ) -> None:
        """运行到终态（SUCCESS/FAILED/CANCELLED），全程经 ``report`` 上报。"""
        payload = parse_payload(task, EnsureModelPayload)
        if payload is None:
            await report(TaskStatus.FAILED, message="ENSURE_MODEL 配方无效")
            return
        raw_dir = payload.local_dir or ""
        local_dir = Path(raw_dir)
        # source 在 _default_downloader 内按 payload 取（execute 不再用 source 分流）
        source_kind = payload.source_kind or "unknown"
        source_subpath = payload.source_subpath

        # 注意：bool(Path("")) 为 True（PurePath 无 __bool__/__len__），且 Path("") 规范化为
        # "."、exists() 为真——必须按原始串判空，否则空 local_dir 会让 _verify_existing 扫描
        # 进程 CWD（误判在位 model_path="."，或把模型下载进 CWD 污染工作目录）。
        if not raw_dir:
            await report(TaskStatus.FAILED, message="ENSURE_MODEL 缺 local_dir")
            return

        # 进度上报经包装：统一注入冻结local_dir（node 实际落盘目录）——manager
        # 投影读 extra["local_dir"] 写 node_model.local_dir。downloader 与
        # cancel-checkpoint 复用同一包装，保证每条 task_event 都带冻结键。
        _raw_report = report

        async def _report(status, progress=0, *, stage=None, message=None, extra=None):
            ex = dict(extra or {})
            ex.setdefault("local_dir", str(local_dir))
            await _raw_report(status, progress, stage=stage, message=message, extra=ex)

        # 1. check existing（重启恢复 / 重复下发短路；无需 secrets）
        model_path = _verify_existing(local_dir, source_kind, source_subpath, payload.extra_files)
        if model_path:
            log.info("ensure_model %s: 已就位 → %s", task.task_id[:8], model_path)
            await _report(TaskStatus.SUCCESS, 100, stage=EnsureModelStage.FINISHED, extra={"model_path": model_path})
            return

        # 下载前目录是否已有内容（手动预置 / 上一 attempt 续传产物）。取消清理据此
        # 分流：空目录起下载才允许整删（纯本次产物），已有内容只清 ``.part``——
        # 否则取消会连预置文件一起 rmtree 掉
        dir_had_content = local_dir.exists() and any(local_dir.iterdir())

        # 2. cancel checkpoint
        if await self._abort_if_cancelled(cancel_event, _report, local_dir):
            return
        # 3. 磁盘预检
        if not self._has_free_space(payload.total_bytes):
            await _report(TaskStatus.FAILED, message=f"INSUFFICIENT_DISK: 剩余空间 < {self._min_free_gb}GB")
            return

        # 4. 下载（local 源走 Manager 文件服务；huggingface 走 HF）—— Node 本地瞬时重试
        # （不增 manager attempt；hf/manager 续传均从断点接续）
        if await self._abort_if_cancelled(cancel_event, _report, local_dir):
            return
        await _report(TaskStatus.RUNNING, stage=EnsureModelStage.DOWNLOADING)
        last_err: Exception | None = None
        downloaded = False
        for attempt in range(1, self._download_retries + 1):
            if cancel_event.is_set():
                break
            try:
                await self._downloader(payload, secrets, local_dir, _report, cancel_event)
                downloaded = True
                break
            except DownloadCancelled:
                _cleanup_cancelled(local_dir, dir_had_content)
                await _report(TaskStatus.CANCELLED, message="任务已取消")
                return
            except Exception as e:
                last_err = e
                log.warning(
                    "ensure_model %s: 下载第 %d/%d 次失败: %s",
                    task.task_id[:8],
                    attempt,
                    self._download_retries,
                    e,
                )
                if attempt < self._download_retries:
                    await asyncio.sleep(self._download_retry_backoff)
        if cancel_event.is_set():
            _cleanup_cancelled(local_dir, dir_had_content)
            await _report(TaskStatus.CANCELLED, message="任务已取消")
            return
        if not downloaded:
            # 失败保留部分产物：只清 ``.part`` 中间态，已完成文件 + 期望清单留下——
            # 下一 attempt 按清单续传（已完成文件按 size 跳过），不再整轮重下。
            # 部分目录不会被误判 present：manager 源下载开始即前置写入期望清单，
            # ``_verify_existing`` 逐项比对判「未完成」；HF 源有 ``.cache/huggingface``
            # 半下载守卫。取消走上面的整目录清理（用户意图是释放磁盘）。
            _cleanup_part(local_dir)
            await _report(
                TaskStatus.FAILED,
                message=f"下载失败（重试 {self._download_retries} 次仍失败）: {last_err}",
            )
            return

        # 6. verify 下载结果（先报 verifying 阶段状态机 downloading→verifying→finished）
        await _report(TaskStatus.RUNNING, stage=EnsureModelStage.VERIFYING)
        verified = _verify_existing(local_dir, source_kind, source_subpath, payload.extra_files)
        if not verified:
            _cleanup_part(local_dir)
            await _report(
                TaskStatus.FAILED,
                message="下载后 verify 失败（主模型或配套文件缺失/不符合 source_kind）",
            )
            return
        await _report(TaskStatus.SUCCESS, 100, stage=EnsureModelStage.FINISHED, extra={"model_path": verified})

    async def reconcile(self, task: LocalTask) -> ReconcileResult | None:
        """无 secrets、无副作用检在位：返回已达成终态（SUCCESS + model_path）或 None。

        Executor ABC 契约——供 ``NodeAgent._recover`` 重启恢复复用（无需 secrets，仅看磁盘）。
        """
        payload = parse_payload(task, EnsureModelPayload)
        if payload is None:
            return None
        raw_dir = payload.local_dir or ""
        if not raw_dir:  # 按原始串判空（bool(Path("")) 为 True）
            return None
        path = _verify_existing(
            Path(raw_dir), payload.source_kind or "unknown", payload.source_subpath, payload.extra_files
        )
        if not path:
            return None
        return ReconcileResult(TaskStatus.SUCCESS, extra={"model_path": path})

    async def on_cancel(self, task: LocalTask) -> None:
        """取消收口：清 .part 中间残留（rmtree/rglob 走线程池，不卡事件循环）。"""
        payload = parse_payload(task, EnsureModelPayload)
        raw_dir = (payload.local_dir if payload else "") or ""
        if raw_dir:  # 按原始串判空（Path("")→"."，避免扫 CWD）
            await asyncio.to_thread(_cleanup_part, Path(raw_dir))

    # ---- cancel checkpoint ----

    @staticmethod
    async def _abort_if_cancelled(cancel_event: asyncio.Event, report: ReportFn, local_dir: Path) -> bool:
        if cancel_event.is_set():
            _cleanup_part(local_dir)
            await report(TaskStatus.CANCELLED, message="任务已取消")
            return True
        return False

    # ---- 磁盘预检 ----

    # 体积余量系数：下载产物 ≈ 模型本体 + 压缩/元数据开销；再加 2GB 固定余量垫
    # 其他并发写入。无体积信息（旧 payload / HF 源）回落固定阈值。
    _SIZE_RESERVE_FACTOR = 1.2
    _SIZE_RESERVE_MARGIN_BYTES = 2 * _GB

    def _required_free_bytes(self, total_bytes: int | None) -> int:
        floor = int(self._min_free_gb * _GB)
        if not total_bytes or total_bytes <= 0:
            return floor
        return max(floor, int(total_bytes * self._SIZE_RESERVE_FACTOR) + self._SIZE_RESERVE_MARGIN_BYTES)

    def _has_free_space(self, total_bytes: int | None = None) -> bool:
        need = self._required_free_bytes(total_bytes)
        try:
            usage = shutil.disk_usage(self._store_dir)
        except OSError:
            return True  # 路径暂不存在：不阻断（下载步骤会创建并暴露真因）
        return usage.free >= need

    # ---- 默认 downloader（按 source 分流；HF 走专用 downloader）----

    async def _default_downloader(self, payload: EnsureModelPayload, secrets, local_dir, report, cancel_event) -> None:
        source = (payload.source or "").lower()
        if ModelSource.is_hf_source(source):
            await self._download_hf(payload, secrets, local_dir, report, cancel_event)
            return
        if ModelSource.is_manager_source(source):
            await self._download_manager(payload, secrets, local_dir, report, cancel_event)
            return
        raise ValueError(f"不支持的下载 source: {source}")

    async def _download_hf(self, payload: EnsureModelPayload, secrets, local_dir, report, cancel_event) -> None:
        """HF 下载：委托 ``_hf.download_hf``（逐文件 ``hf_hub_download`` + cancel + 粗粒度进度）。

        ``hf_token`` 仅从 ``secrets`` 取出经参数注入下载器，**不**落 local_tasks/日志/payload。
        """
        from llmpod.apps.node.executors._hf import download_hf

        secrets = secrets or {}
        await download_hf(
            source_url=payload.source_url or "",
            revision=payload.revision,
            source_subpath=payload.source_subpath,
            token=secrets.get("hf_token"),
            local_dir=local_dir,
            report=report,
            cancel_event=cancel_event,
            extra_files=payload.extra_files,
        )

    async def _download_manager(self, payload: EnsureModelPayload, secrets, local_dir, report, cancel_event) -> None:
        """local 源下载：委托 ``_manager.download_manager``（Manager 文件服务 manifest + 逐文件 Range 续传）。

        ``download_token`` 仅从 ``secrets`` 取出注入下载器，**不**落 local_tasks/日志/payload。
        ``manager_base_url`` 是非密配置（在 payload 里）。
        """
        from llmpod.apps.node.executors._manager import download_manager

        secrets = secrets or {}
        await download_manager(
            manager_base_url=payload.manager_base_url or "",
            download_token=secrets.get("download_token"),
            model_id=payload.model_id,
            local_dir=local_dir,
            report=report,
            cancel_event=cancel_event,
        )


# ---- verify / cleanup（纯函数，可单测）----


# verify 用权重扩展名（与 manager 侧 inference.WEIGHT_EXTS 对齐，本地复刻避免 node 依赖 manager 模块）
_VERIFY_WEIGHT_EXTS = (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt", ".onnx")
# 下载器下载完成时落盘的文件清单（相对 local_dir）——verify 的完整性判定依据。
# 逐文件下载（hf_hub_download / manager 文件服务）在**文件之间**可能被中断/取消，而权重名
# 形如 ``layers-0.safetensors``（无 ``-of-N``）时命名启发式无法推断期望分片总数；清单把
# 「下载器实际枚举到的全部文件」显式落盘，verify 逐项比对即可防半下载被误判 present。
_MANIFEST_NAME = ".llmpod-manifest.json"

# 分片命名的两个捕获组别搞混：``model-00001-of-00003.gguf`` 里 group(2)=00003 是
# **总数**（_SHARD_OF 用于期望完整性），group(1)=00001 才是**序号**（_SHARD_SEQ
# 用于挑首片）。两组曾经混用，排序退化成字典序。
_SHARD_OF = re.compile(r"-of-(\d+)\.[A-Za-z0-9]+$", re.IGNORECASE)
_SHARD_SEQ = re.compile(r"-(\d+)-of-(\d+)\.[A-Za-z0-9]+$", re.IGNORECASE)


def _manifest_rel_paths(local_dir: Path) -> list[str] | None:
    """读取下载器落盘的期望文件清单（相对 ``local_dir``）；无/损坏清单 → None。"""
    mf = local_dir / _MANIFEST_NAME
    if not mf.is_file():
        return None
    try:
        data = json.loads(mf.read_text())
        files = data.get("files")
        if isinstance(files, list):
            return [str(f) for f in files if str(f).strip("/")]
    except Exception:
        return None
    return None


def _expected_shard_total(names: list[str]) -> int | None:
    """从权重文件名 ``-of-N`` 提取期望分片总数；无该命名 → None。"""
    ofs = [int(m.group(1)) for n in names if (m := _SHARD_OF.search(n))]
    return max(ofs) if ofs else None


def _weights_complete(weights: list[str]) -> bool:
    """权重文件是否完整：有 ``-of-N`` 命名 → 要求全部 N 片在位；否则 ≥1 个权重文件即可。

    防 H1（半下载误判在位）：``config.json`` + 0 权重、或只下 ``-00001-of-00003`` 首 1 片
    → 判定未完成，触发重下而非误报 present。
    """
    if not weights:
        return False
    expected = _expected_shard_total(weights)
    return expected is None or len(weights) >= expected


def _collect_weights(root: Path) -> list[str]:
    """递归收集权重文件名，跳过隐藏目录（``.huggingface`` 等）与 ``.part`` 残片。

    diffusers 权重常落在子目录（``transformer/`` ``vae/`` ``unet/`` ``text_encoder/``）而非根级——
    故 folder kind 的权重完整性判定需**递归**，否则完整 diffusers 下载（根级无权重）会被误判
    未完成（H1 的镜像问题：过严 false-negative）。标志文件（config/model_index）仍在根级判定。
    """
    out: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        # 跳过隐藏目录（.huggingface/download/...）下的非模型文件
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        if p.name.lower().endswith(_VERIFY_WEIGHT_EXTS):
            out.append(p.name)
    return out


def _verify_existing(
    local_dir: Path,
    source_kind: str,
    source_subpath: str | None = None,
    extra_files: list[str] | None = None,
) -> str | None:
    """主模型 + 配套文件（``extra_files``，如视觉投影器 mmproj）全部就位 → 返回 model_path。

    主文件判定见 :func:`_verify_main`；配套文件任一缺失即视为未就位（容器会缺件起不来，
    宁可重下——hf_hub_download 幂等跳过已完成的文件）。
    """
    path = _verify_main(local_dir, source_kind, source_subpath, extra_files)
    if path is None:
        return None
    # 配套件与主模型同按 **repo 相对路径**落盘：HF 下载逐文件按 repo 相对位置写入
    # （allow patterns 只过滤不改写路径——extras 不在 subpath 下时落在 local_dir 根），
    # manager 文件服务下载器同理。判在位须同一口径、不叠 subpath 前缀——否则文件
    # 下齐了也恒判缺失，反复重下直到 attempt 耗尽；extras 自带完整 repo 前缀时也
    # 直接命中，无双重前缀问题
    for rel in extra_files or []:
        candidate = local_dir / rel.strip("/")
        if not candidate.is_file():
            return None
    return path


def _main_ggufs(names: list[str], extras: list[str] | None = None) -> list[str]:
    """主权重 GGUF——配套件不算主模型：以声明清单（``extra_files``）为准，命名约定
    （"mmproj" 字样）兜底无清单的调用方。清单是契约：配套件扩到别的投影器/格式
    只需改清单，不必依赖文件名魔法。"""
    declared = {PurePosixPath(f).name for f in (extras or []) if f}
    return [n for n in names if n.lower().endswith(".gguf") and n not in declared and "mmproj" not in n.lower()]


def _first_gguf(ggufs: list[str]) -> str | None:
    """主权重 GGUF 中的首片：``-NNNNN-of-`` 序号最小者优先，无序号按字典序。

    llama.cpp 的 ``-m`` 只给首分片路径（llama-server 自会按 ``-of-N`` 命名收齐
    其余分片）；多分片 metadata 也只写在首片头。
    """
    if not ggufs:
        return None

    def _shard_no(name: str) -> int:
        # 无序号的文件（imatrix / 单文件模型等）排有编号分片之后——首片必然带序号
        m = _SHARD_SEQ.search(name)
        return int(m.group(1)) if m else 1 << 30

    return min(ggufs, key=lambda n: (_shard_no(n), n))


def _resolve_gguf_model_path(eff: Path, source_kind: str, ggufs: list[str]) -> str | None:
    """GGUF 形态的主模型**文件**解析（single→唯一 gguf；shards→首分片文件）。

    ``ggufs`` 由调用方算好传入（完整性判定与路径解析共用同一份，不重复扫目录）。
    完整性已由调用方判过，这里只负责把 model_path 落到文件上——返回目录会让
    llama-server ``-m <目录>`` 读 GGUF magic 失败（ExitCode=1 崩溃循环）。
    """
    if source_kind == "single_gguf":
        return str(eff / ggufs[0]) if len(ggufs) == 1 else None
    first = _first_gguf(ggufs)
    return str(eff / first) if first else None


def _verify_main(
    local_dir: Path, source_kind: str, source_subpath: str | None = None, extra_files: list[str] | None = None
) -> str | None:
    """``local_dir``（+ 可选 subpath 子目录/文件）已就位 → 返回 model_path；否则 None。

    - ``source_subpath`` 非空 → hf_hub_download 保留 repo 相对结构，模型落在
      ``local_dir/<subpath>``（变体目录或直链文件）；空 → ``local_dir`` 本身。
    - folder kind（transformers/diffusers）→ 根级标志文件 + **完整**权重集（递归，含子目录）；
      gguf_shards → **全部** ``-of-N`` 分片在位；single_gguf → 恰 1 个 ``.gguf``；
      unknown → 至少 1 个真实模型文件。
    - 跳过 HF 的 ``.huggingface`` 元数据目录、``.part`` 残片及其他隐藏项（H1：避免半下载误判）。
    """
    sub = (source_subpath or "").strip("/")
    eff = (local_dir / sub) if sub else local_dir
    # 防御：Path("") 规范化为 "."、exists() 为真——会命中 CWD。调用方应已按原始串判空，
    # 此处再兜一道，避免空路径误扫 CWD（误判在位）。
    if str(eff) in ("", ".") or not eff.exists():
        return None
    # 直链 subpath（单文件，如 /resolve/main/.../x.gguf）→ 校验该文件本身
    if eff.is_file():
        if source_kind == "single_gguf" and eff.name.lower().endswith(".gguf"):
            return str(eff)
        return None
    if not eff.is_dir():
        return None
    # 下载器落盘的清单存在 → 权威完整性判定：逐项比对全部期望文件（含 processor/tokenizer 等非权重
    # 文件与 ``layers-N`` 分片），任一缺失即未完成。manager 文件服务下载器在**下载开始**时前置写入
    # 期望清单（断点续传的部分目录据此判「未完成」而非走命名启发式）；HF 下载器在**全部成功**后写入
    # ——两种时机的清单语义一致：清单内文件全在位才算完整。
    # 清单只证明「完整」，model_path 仍按 source_kind 解析：GGUF 形态必须落到主模型
    # **文件**（引擎 -m 直吃该路径），folder 形态才返回目录。
    rels = _manifest_rel_paths(local_dir)
    if rels is not None:
        if not all((local_dir / r).is_file() for r in rels):
            return None
        if source_kind in ("single_gguf", "gguf_shards"):
            names = [p.name for p in eff.iterdir() if not p.name.startswith(".")]
            return _resolve_gguf_model_path(eff, source_kind, _main_ggufs(names, extra_files))
        return str(eff)
    # HF 下载器管理过的目录（``local_dir/.cache/huggingface`` 存在）但没有清单 → 必为半下载/
    # 中断或旧版（无清单）下载产物：命名启发式对 ``layers-N``（无 ``-of-N``）推断不出期望分片
    # 总数，不得据此判 present——强制重下（幂等续传，完成后落清单再放行）。pre-seeded 手动
    # 预置的目录无此缓存标记，仍走下方启发式。
    if (local_dir / ".cache" / "huggingface").is_dir():
        return None
    # 根级真实文件名（标志文件判定 + single_gguf/gguf_shards/unknown）；跳过隐藏项与 .part
    names = [p.name for p in eff.iterdir() if not p.name.startswith(".") and not p.name.endswith(".part")]
    if source_kind == "single_gguf":
        return _resolve_gguf_model_path(eff, "single_gguf", _main_ggufs(names, extra_files))
    if source_kind == "gguf_shards":
        # 完整性：全部 ``-of-N`` 分片在位；model_path = 首分片**文件**（引擎 -m 直吃）
        ggufs = _main_ggufs(names, extra_files)
        return _resolve_gguf_model_path(eff, "gguf_shards", ggufs) if _weights_complete(ggufs) else None
    if source_kind == "transformers_folder":
        if "config.json" not in names:
            return None
        return str(eff) if _weights_complete(_collect_weights(eff)) else None
    if source_kind == "diffusers_folder":
        if "model_index.json" not in names:
            return None
        return str(eff) if _weights_complete(_collect_weights(eff)) else None
    # unknown：至少 1 个真实文件（保守判定；unknown/diffusers 的拒绝规则后续再收紧）
    return str(eff) if names else None


def _cleanup_part(local_dir: Path) -> None:
    """清下载中间态（``.part`` 等）；best-effort。"""
    # 防御：Path("") → "."，rglob("*.part") 会扫 CWD 误删无关 .part 文件。
    if str(local_dir) in ("", ".") or not local_dir.exists():
        return
    try:
        for p in local_dir.rglob("*.part"):
            p.unlink(missing_ok=True)
    except OSError as e:
        log.debug("cleanup_part %s: %s", local_dir, e)


def _cleanup_download(local_dir: Path) -> None:
    """取消后清掉整个 partial 目录（释放磁盘）。

    仅对「下载前目录为空」的任务安全（目录里只可能是本次半下载产物）——调用方
    须先用 :func:`_cleanup_cancelled` 分流：目录在下载前已有内容（手动预置 /
    上一 attempt 续传产物）时整删会连坐删掉用户文件。失败路径不走此处——改清
    ``.part`` 保留已完成文件供下一 attempt 续传（见 execute 的 FAILED 分支）。
    """
    if str(local_dir) in ("", ".") or not local_dir.exists():
        return
    try:
        shutil.rmtree(local_dir)
    except OSError as e:
        log.debug("cleanup_download %s: %s", local_dir, e)


def _cleanup_cancelled(local_dir: Path, dir_had_content: bool) -> None:
    """取消清理分流：空目录起下载 → 整目录清（纯本次产物，释放磁盘）；下载前
    已有内容 → 只清 ``.part``（预置文件与续传产物保留，失败路径同语义）。"""
    if dir_had_content:
        _cleanup_part(local_dir)
    else:
        _cleanup_download(local_dir)
