"""通用切片上传服务（会话 / 分片 / 合并 / 清理）。

与「模型」解耦：纯切片 + 断点续传 + 合并 + 取消，零模型知识，``finalize``
只合并不 inspect（inspect 是模型层职责）。会话存 Redis（Hash + Set），
分片落 manager 盘 ``model_store_dir/upload/<name>/``。

- 状态机：pending → finalizing → completed / failed（失败即弃）；abort 仅 pending 可用。
- 删除三层保证：主动 abort 同步删 + TTL 后台扫描删 + 崩溃启动自愈删。
- 原子写：分片 ``.tmp → fsync → os.replace``；合并 ``.merging → fsync → os.replace``。
- 幂等：part 已收直接 ack 不重写；finalize completed 再调幂等返回上次结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

from llmpod.config.settings import settings
from llmpod.core.exceptions import (
    Conflict,
    ErrorCode,
    LLMPodValidationError,
    NotFound,
)
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.db import get_redis

log = logging.getLogger("llmpod.apps.webserver.services.upload")


# ============================================================
# service 返回类型（service 构造，router 作 response_model）
# ============================================================


class DeclareFileAck(BaseModel):
    """declare 返回的单文件元信息。"""

    file_id: str = Field(..., description="server 分配的文件顺序索引（f0/f1/...），后续 part 按此定位")
    name: str = Field(..., description="目标文件名（回显）")


class DeclareResponse(BaseModel):
    """declare 成功响应。"""

    sid: str = Field(..., description="会话 ID（不可猜，后续 part/status/finalize/abort 用）")
    staged_path: str = Field(..., description="staged 目录绝对路径，finalize 后前端拿去 inspect")
    status: str = Field(..., description="会话状态：pending")
    files: list[DeclareFileAck] = Field(..., description="各文件 file_id + 目标名清单")


class PartResponse(BaseModel):
    """part 上传成功响应。"""

    file_id: str = Field(..., description="文件索引")
    part_no: int = Field(..., description="分片序号")
    received: bool = Field(..., description="是否已收（幂等：已在也返回 True）")
    session_status: str = Field(..., description="会话当前状态（pending/finalizing/...）")


class FileStatus(BaseModel):
    """单文件的断点续传状态。"""

    file_id: str = Field(..., description="文件索引")
    name: str = Field(..., description="目标文件名")
    total_parts: int = Field(..., description="分片总数")
    received_parts: list[int] = Field(..., description="已收分片序号（升序）")
    missing_parts: list[int] = Field(..., description="缺失分片序号（断点续传只补传这些）")
    complete: bool = Field(..., description="是否已收齐（received_parts 数 == total_parts）")


class SessionStatusResponse(BaseModel):
    """会话状态查询响应。"""

    sid: str = Field(..., description="会话 ID")
    status: str = Field(..., description="会话状态：pending/finalizing/completed/aborted/failed")
    files: list[FileStatus] = Field(..., description="各文件断点续传状态")


class FinalizeFileAck(BaseModel):
    """finalize 返回的单文件合并结果。"""

    file_id: str = Field(..., description="文件索引")
    name: str = Field(..., description="合并后的目标文件名")
    size: int = Field(..., description="合并后文件大小（字节）")


class FinalizeResponse(BaseModel):
    """finalize 响应（合并异步触发）。

    - 首次调用：校验通过即后台合并，立即返回 ``status=finalizing`` + 空 ``files``；
      前端轮询 ``GET /{sid}`` 等 ``completed`` / ``failed``。
    - ``completed`` 后再调一次（幂等）：返回 ``status=completed`` + 文件清单。
    """

    sid: str = Field(..., description="会话 ID")
    staged_path: str = Field(..., description="staged 目录绝对路径，前端拿去 inspect")
    status: str = Field(..., description="finalizing（合并进行中，需轮询）/ completed")
    files: list[FinalizeFileAck] = Field(default_factory=list, description="合并后目标文件清单；finalizing 态为空")


class AbortResponse(BaseModel):
    """abort 响应。幂等：重复 abort 也返回 aborted。"""

    sid: str = Field(..., description="会话 ID")
    status: str = Field(..., description="aborted")


# Redis key 前缀（唯一定义处；其余 key 由 _k_* 组合，不在各处重复字面量）
_K_SESSION = "llmpod:upload:"  # llmpod:upload:{sid}（Hash）

# 会话状态
S_PENDING = "pending"
S_FINALIZING = "finalizing"
S_COMPLETED = "completed"
S_ABORTED = "aborted"
S_FAILED = "failed"

# 磁盘预留系数：declare 时要求剩余空间 >= 2.2 × 声明总大小（挡中途磁盘满）
_DISK_RESERVE_FACTOR = 2.2

# 文件名清洗：仅留 [A-Za-z0-9._-]，空 → 随机（防穿越）
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def upload_root() -> Path:
    """平台上传产物的落盘根（``{model_store_dir}/upload``，布局归本模块单一维护）。

    删模型级联清理等「判定目录是否平台上架产物」的调用方一律用本函数取根，
    布局调整（如按租户分子目录）时只改这里。
    """
    return Path(settings.model_store_dir).expanduser() / "upload"


def _sanitize(name: str | None) -> str:
    """清洗目录/文件名：仅留 [A-Za-z0-9._-]，空 → 12 位随机。"""
    raw = (name or "").strip()
    safe = _SANITIZE_RE.sub("_", raw).strip("._-") if raw else ""
    return safe or secrets.token_hex(6)


def _file_id(idx: int) -> str:
    """文件顺序索引 → file_id。"""
    return f"f{idx}"


def _part_filename(file_id: str, part_no: int) -> str:
    """分片落盘文件名（独立文件，便于并发与崩溃恢复）。"""
    return f"{file_id}.part-{part_no:05d}"


def _now() -> int:
    """当前 epoch 秒（供会话 created/expires_at）。"""
    return int(time.time())


# ============================================================
# 工具：Redis key 构造（统一从 _K_SESSION 组合，防字面量漂移）
# ============================================================


def _k_session(sid: str) -> str:
    return f"{_K_SESSION}{sid}"


def _k_file(sid: str, fid: str) -> str:
    return f"{_K_SESSION}{sid}:file:{fid}"


def _k_received(sid: str, fid: str) -> str:
    return f"{_K_SESSION}{sid}:file:{fid}:received"


def _k_tombstone(sid: str) -> str:
    return f"{_K_SESSION}{sid}:tombstone"


# ============================================================
# declare（建会话）
# ============================================================


async def declare_session(
    *,
    name: str,
    files_spec: list[dict],
) -> DeclareResponse:
    """建上传会话：通用预校验 + 写 Redis 会话 + 建空 staged 目录。

    ``files_spec`` 元素形如 ``{name, size, total_parts, part_size}``（不含分片 sha——
    sha 在每个分片上传时随 ``sha256`` 表单字段带，服务端重算比对）。
    通用预校验（非模型语义）：文件名合法、单分片 <= upload_part_max_bytes、
    总大小 <= upload_max_bytes、磁盘剩余 >= 2.2×总大小。
    """
    r = get_redis()
    # —— 通用预校验 ——
    total_size = 0
    seen_names: set[str] = set()
    file_acks: list[DeclareFileAck] = []
    file_specs_internal: list[dict] = []

    for idx, fs in enumerate(files_spec):
        fid = _file_id(idx)
        fname = fs["name"]
        if not fname or not fname.strip():
            raise _invalid("文件名不能为空", ErrorCode.VALIDATION_ERROR)
        # 目标名清洗（合并后落盘名），防穿越
        safe_name = _sanitize(os.path.basename(fname))
        if not safe_name:
            raise _invalid("文件名非法", ErrorCode.INVALID_FILENAME)
        if safe_name in seen_names:
            raise _invalid(f"文件名重复：{safe_name}", ErrorCode.INVALID_FILENAME)
        seen_names.add(safe_name)

        size = int(fs["size"])
        total_parts = int(fs["total_parts"])
        part_size = int(fs["part_size"])

        if size < 0:
            raise _invalid("文件大小不能为负", ErrorCode.VALIDATION_ERROR)
        if total_parts < 1:
            raise _invalid("分片数必须 >= 1", ErrorCode.VALIDATION_ERROR)
        if part_size < 1:
            raise _invalid("单分片大小必须 >= 1", ErrorCode.VALIDATION_ERROR)
        if part_size > settings.upload_part_max_bytes:
            raise _invalid(
                f"单分片 {part_size} 超上限 {settings.upload_part_max_bytes}",
                ErrorCode.VALIDATION_ERROR,
            )

        total_size += size
        file_acks.append(DeclareFileAck(file_id=fid, name=safe_name))
        file_specs_internal.append(
            {
                "file_id": fid,
                "name": safe_name,
                "size": size,
                "total_parts": total_parts,
                "part_size": part_size,
            }
        )

    if total_size > settings.upload_max_bytes:
        raise _conflict(
            f"总大小 {total_size} 超上传上限 {settings.upload_max_bytes}",
            ErrorCode.OVERSIZE,
        )

    # —— sid 先于目录生成：staged 目录并入 sid，同名/清洗后同名的并发会话不共用落盘目录
    sid = secrets.token_urlsafe(24)

    # —— 磁盘预检 ——
    staged_root = upload_root() / f"{_sanitize(name)}-{sid}"
    await asyncio.to_thread(_check_disk_space, staged_root.parent, total_size)
    await asyncio.to_thread(staged_root.mkdir, parents=True, exist_ok=True)

    # —— 写 Redis 会话 ——
    now = _now()
    expires_at = now + settings.upload_session_ttl_seconds

    pipe = r.pipeline()
    sess_key = _k_session(sid)
    pipe.hset(
        sess_key,
        mapping={
            "status": S_PENDING,
            "name": name,
            "staged_path": str(staged_root.resolve()),
            "created_at": str(now),
            "expires_at": str(expires_at),
            "total_files": str(len(file_specs_internal)),
        },
    )
    pipe.expire(sess_key, settings.upload_session_ttl_seconds)
    for fs in file_specs_internal:
        fk = _k_file(sid, fs["file_id"])
        pipe.hset(
            fk,
            mapping={
                "name": fs["name"],
                "size": str(fs["size"]),
                "total_parts": str(fs["total_parts"]),
                "part_size": str(fs["part_size"]),
            },
        )
        pipe.expire(fk, settings.upload_session_ttl_seconds)
        # received Set 初始为空，仅设 TTL
        rk = _k_received(sid, fs["file_id"])
        pipe.expire(rk, settings.upload_session_ttl_seconds)
    await pipe.execute()

    # —— 冗余落盘镜像（兜底恢复用）——
    await asyncio.to_thread(_write_session_mirror, staged_root, sid, name, expires_at, file_specs_internal)

    log.info(
        "upload session declared: sid=%s name=%s files=%d size=%d", sid, name, len(file_specs_internal), total_size
    )
    return DeclareResponse(
        sid=sid,
        staged_path=str(staged_root.resolve()),
        status=S_PENDING,
        files=file_acks,
    )


def _check_disk_space(parent: Path, total_size: int) -> None:
    """declare 磁盘预检：剩余空间 < 2.2×声明总大小 → disk_full。"""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(parent))
    except OSError as e:
        raise _conflict(f"磁盘预检失败：{e}", ErrorCode.DISK_FULL) from e
    need = int(total_size * _DISK_RESERVE_FACTOR)
    if usage.free < need:
        raise _conflict(
            f"磁盘剩余 {usage.free} 不足 {need}（{total_size} × {_DISK_RESERVE_FACTOR}）",
            ErrorCode.DISK_FULL,
        )


def _write_session_mirror(
    staged: Path,
    sid: str,
    name: str,
    expires_at: int,
    file_specs: list[dict],
) -> None:
    """写 .session.json 冗余镜像（Redis 丢时降级读它重建只读状态）。"""
    mirror = {
        "sid": sid,
        "name": name,
        "expires_at": expires_at,
        "files": [
            {
                "file_id": fs["file_id"],
                "name": fs["name"],
                "total_parts": fs["total_parts"],
            }
            for fs in file_specs
        ],
    }
    try:
        tmp = staged / ".session.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mirror, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, staged / ".session.json")
    except OSError as e:
        log.warning("write .session.json mirror failed for %s: %s", staged, e)


# ============================================================
# part（传分片）
# ============================================================


async def receive_part(
    *,
    sid: str,
    file_id: str,
    part_no: int,
    sha256: str,
    body: bytes,
) -> PartResponse:
    """收一个分片：取消标记检查 → sha 校验 → 原子落盘 → SADD received。

    幂等：已收直接 ack 不重写；finalize 中拒新 part。
    """
    r = get_redis()
    # —— 会话存在 + 状态校验 ——
    status = await _require_active_session(r, sid, allow_finalizing=False)
    file_meta = await r.hgetall(_k_file(sid, file_id))
    if not file_meta:
        # hgetall 对缺失键返回空 dict，直接据此判缺（省一次 exists 往返）
        raise _not_found("file", file_id, ErrorCode.SESSION_NOT_FOUND)
    total_parts = int(file_meta.get("total_parts", "0"))
    if not 1 <= part_no <= total_parts:
        raise _invalid(
            f"分片序号 {part_no} 越界（声明范围 1..{total_parts}）",
            ErrorCode.PART_OUT_OF_RANGE,
        )
    declared_size = int(file_meta.get("size", "0"))
    declared_part_size = int(file_meta.get("part_size", "0"))

    # —— 实际字节上限（declare 的声明值拦不住表单层伪造：单分片按声明 part_size
    #    与文件剩余量取紧者；尾片允许 < part_size，但累计不能超文件 size）——
    remaining = declared_size - (part_no - 1) * declared_part_size
    part_cap = min(declared_part_size, remaining) if remaining > 0 else 0
    if len(body) > max(part_cap, 0):
        raise _invalid(
            f"分片 {part_no} 实际 {len(body)} 字节超声明上限 {part_cap}（文件 {declared_size} B × "
            f"part_size {declared_part_size} B）",
            ErrorCode.OVERSIZE,
        )

    # —— 取消标记（拦截取消后迟到的分片）——
    if await r.exists(_k_tombstone(sid)):
        raise _conflict("会话已取消", ErrorCode.SESSION_ABORTED)

    # —— sha 比对（sha 来自本分片上传时的表单字段，非 declare 预声明）——
    expected = (sha256 or "").strip().lower()
    if not _is_sha256(expected):
        raise _invalid("sha256 格式非法（需 64 位十六进制）", ErrorCode.VALIDATION_ERROR)
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        raise _conflict(
            f"分片 {part_no} sha 不符（期望 {expected[:12]}…，实际 {actual[:12]}…）",
            ErrorCode.PART_SHA_MISMATCH,
        )

    # —— 幂等：已收直接 ack ——
    rk = _k_received(sid, file_id)
    if await r.sismember(rk, part_no):
        return PartResponse(file_id=file_id, part_no=part_no, received=True, session_status=status)

    # —— 原子落盘 ——
    staged_path_str = await r.hget(_k_session(sid), "staged_path")
    if not staged_path_str:
        raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND)
    staged_path = Path(staged_path_str)
    part_name = _part_filename(file_id, part_no)
    tmp_name = f"{part_name}.tmp"
    await asyncio.to_thread(_atomic_write, staged_path / tmp_name, staged_path / part_name, body)

    # —— 标记 received ——
    await r.sadd(rk, part_no)
    # received Set TTL 跟随会话（写后刷新，避免 TTL 短于主会话）
    await r.expire(rk, settings.upload_session_ttl_seconds)
    return PartResponse(file_id=file_id, part_no=part_no, received=True, session_status=status)


def _atomic_write(tmp: Path, dest: Path, body: bytes) -> None:
    """原子写：tmp → fsync → os.replace dest。崩溃只留 tmp，启动扫描删。"""
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)


# ============================================================
# status（查会话状态 / 断点续传依据）
# ============================================================


async def get_session_status(*, sid: str) -> SessionStatusResponse:
    """查会话状态：各文件 received/missing parts。sid 不可猜，GET 不加细权限。

    Redis 丢则降级读 .session.json + os.listdir 重建只读状态（能查能 abort，
    不能续传——续传要 Redis 的 sha 比对，丢了只能重传）。
    """
    r = get_redis()
    sess = await r.hgetall(_k_session(sid))
    if not sess:
        # 降级：读磁盘 .session.json
        return await _rebuild_status_from_disk(sid)

    status = sess.get("status", S_PENDING)
    total_files = int(sess.get("total_files", "0"))

    # 一次性 pipeline 收齐各文件的 hgetall + smembers，避免 N×2 RTT（断点续传热路径）
    fids = [_file_id(i) for i in range(total_files)]
    pipe = r.pipeline()
    for fid in fids:
        pipe.hgetall(_k_file(sid, fid))
        pipe.smembers(_k_received(sid, fid))
    results = await pipe.execute()

    files: list[FileStatus] = []
    for i, fid in enumerate(fids):
        fm = results[i * 2]
        if not fm:
            continue
        total_parts = int(fm.get("total_parts", "0"))
        received = sorted(int(x) for x in results[i * 2 + 1])
        missing = sorted(set(range(1, total_parts + 1)) - set(received))
        files.append(
            FileStatus(
                file_id=fid,
                name=fm.get("name", ""),
                total_parts=total_parts,
                received_parts=received,
                missing_parts=missing,
                complete=len(received) == total_parts,
            )
        )
    return SessionStatusResponse(sid=sid, status=status, files=files)


async def _rebuild_status_from_disk(sid: str) -> SessionStatusResponse:
    """Redis 丢时的弱兜底：复用 ``_find_staged_on_disk`` 定位目录，读 .session.json 重建只读状态。"""
    d = await _find_staged_on_disk(sid)
    if d is None:
        raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND)
    mirror_path = d / ".session.json"
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND) from None

    files: list[FileStatus] = []
    for fs in mirror.get("files", []):
        fid = fs["file_id"]
        total_parts = fs["total_parts"]
        # 扫已收分片文件名
        received = await asyncio.to_thread(_scan_received_parts, d, fid)
        missing = sorted(set(range(1, total_parts + 1)) - set(received))
        files.append(
            FileStatus(
                file_id=fid,
                name=fs["name"],
                total_parts=total_parts,
                received_parts=received,
                missing_parts=missing,
                complete=len(received) == total_parts,
            )
        )
    # Redis 丢了 → 续传不可能，标 failed 提示重传
    return SessionStatusResponse(sid=sid, status=S_FAILED, files=files)


def _list_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir()]


def _scan_received_parts(staged: Path, file_id: str) -> list[int]:
    """扫 staged 目录下 {file_id}.part-NNN 文件，返回已收分片序号（升序）。"""
    prefix = f"{file_id}.part-"
    received: list[int] = []
    try:
        for entry in staged.iterdir():
            if not entry.name.startswith(prefix) or entry.name.endswith(".tmp"):
                continue
            tail = entry.name[len(prefix) :]
            try:
                received.append(int(tail))
            except ValueError:
                continue
    except OSError:
        pass
    return sorted(received)


# ============================================================
# finalize（合并）
# ============================================================


async def finalize_session(*, sid: str) -> FinalizeResponse:
    """合并所有分片为目标文件 + 删分片（**异步**：校验后立即返回，后台合并）。

    - 全文件 complete 才合并（未收齐 → ``session_not_finalizable``，同步 409）；
    - 校验通过即置 ``finalizing`` + 后台 ``create_task`` 合并，**立即返回 ``finalizing``**；
    - 合并失败即弃：删盘 + 写取消标记 + 置 ``failed``（保留会话，轮询可见 failed）；
    - 幂等：``completed`` 再调返回上次结果（从磁盘重建）；``finalizing`` 再调 → 冲突。
    """
    r = get_redis()
    sess = await r.hgetall(_k_session(sid))
    if not sess:
        raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND)

    status = sess.get("status", S_PENDING)
    if status == S_COMPLETED:
        return await _rebuild_finalized_from_disk(r, sid, sess)
    if status == S_FINALIZING:
        raise _conflict("合并进行中", ErrorCode.SESSION_FINALIZING)
    if status == S_ABORTED:
        raise _conflict("会话已取消，不可合并", ErrorCode.SESSION_NOT_FINALIZABLE)
    if status == S_FAILED:
        raise _conflict("合并已失败，请重新创建会话上传", ErrorCode.FINALIZE_FAILED)

    # —— 校验所有文件 complete（同步，未收齐立即 409）——
    total_files = int(sess.get("total_files", "0"))
    # 一次性 pipeline 收齐各文件的 hgetall + scard（逐文件两跳 → 一跳；与
    # get_session_status 同款做法），校验在拿到全量后串行做
    fids = [_file_id(i) for i in range(total_files)]
    pipe = r.pipeline()
    for fid in fids:
        pipe.hgetall(_k_file(sid, fid))
        pipe.scard(_k_received(sid, fid))
    flat = await pipe.execute()
    file_metas: list[dict] = []
    for idx, fid in enumerate(fids):
        fm = flat[idx * 2]
        if not fm:
            raise _not_found("file", fid, ErrorCode.SESSION_NOT_FOUND)
        total_parts = int(fm.get("total_parts", "0"))
        received_count = flat[idx * 2 + 1]
        if received_count < total_parts:
            raise _conflict(
                f"文件 {fid} 未收齐（{received_count}/{total_parts}）",
                ErrorCode.SESSION_NOT_FINALIZABLE,
            )
        file_metas.append({"file_id": fid, **fm, "total_parts": total_parts})

    # —— 置 finalizing（拒新 part）+ 后台合并，立即返回 ——
    await r.hset(_k_session(sid), "status", S_FINALIZING)
    staged_path = Path(sess.get("staged_path", ""))
    _spawn_finalize_task(r, sid, staged_path, file_metas)

    log.info("upload session finalize kicked: sid=%s files=%d", sid, len(file_metas))
    return FinalizeResponse(
        sid=sid,
        staged_path=str(staged_path.resolve()),
        status=S_FINALIZING,
        files=[],
    )


# 后台合并任务引用持有集：``create_task`` 不持有引用会被事件循环 GC 回收
_bg_finalize_tasks: set[asyncio.Task] = set()


def _spawn_finalize_task(r: aioredis.Redis, sid: str, staged_path: Path, file_metas: list[dict]) -> None:
    """后台跑合并：成功置 completed；失败删盘+写取消标记+置 failed（保留会话供轮询可见）。"""
    task = asyncio.create_task(_run_finalize(r, sid, staged_path, file_metas))
    _bg_finalize_tasks.add(task)
    task.add_done_callback(_bg_finalize_tasks.discard)


async def _run_finalize(r: aioredis.Redis, sid: str, staged_path: Path, file_metas: list[dict]) -> None:
    """后台合并（失败即弃：删盘 + 写取消标记 + failed，不 purge 会话以便轮询读到 failed）。"""
    try:
        acks = await asyncio.to_thread(_merge_all_parts, staged_path, file_metas)
    except Exception as e:
        log.exception("finalize failed for sid=%s: %s", sid, e)
        await asyncio.to_thread(shutil.rmtree, str(staged_path), True, _ignore_rmtree_err)
        # 取消标记拦截迟到的分片；保留会话 + 置 failed，轮询可见（不 purge）
        await r.set(_k_tombstone(sid), "1", ex=settings.upload_session_tombstone_ttl_seconds)
        await r.hset(_k_session(sid), "status", S_FAILED)
        return
    # 成功：标 completed（会话主 key 继续按 TTL 过期，由后台清理删合并文件）
    await r.hset(_k_session(sid), "status", S_COMPLETED)
    log.info("upload session finalized: sid=%s files=%d", sid, len(acks))


def _merge_all_parts(staged: Path, file_metas: list[dict]) -> list[FinalizeFileAck]:
    """逐文件按 part_no 升序 cat 进 .merging → fsync → rename 目标名 → 删分片。

    合并后校验 ``merged_size == 声明 size``：不等说明分片切片边界不一致
    （客户端 slice 算错 gap/overlap），抛异常 → 由 ``finalize_session`` 走失败即弃。
    """
    acks: list[FinalizeFileAck] = []
    for fm in file_metas:
        fid = fm["file_id"]
        target_name = fm["name"]
        total_parts = int(fm["total_parts"])
        merging_tmp = staged / f"{fid}.merging"
        with open(merging_tmp, "wb") as out:
            for part_no in range(1, total_parts + 1):
                part_path = staged / _part_filename(fid, part_no)
                if not part_path.is_file():
                    raise FileNotFoundError(f"分片文件缺失：{part_path.name}")
                with open(part_path, "rb") as pf:
                    while True:
                        chunk = pf.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        target = staged / target_name
        os.replace(merging_tmp, target)
        size = target.stat().st_size
        declared = int(fm["size"])
        if size != declared:
            raise ValueError(
                f"文件 {fid}({target_name}) 合并大小 {size} 与声明 {declared} 不符"
                "（分片切片边界不一致，请检查分片计算）"
            )
        # 删该文件所有分片
        for part_no in range(1, total_parts + 1):
            try:
                (staged / _part_filename(fid, part_no)).unlink()
            except FileNotFoundError:
                pass
        acks.append(FinalizeFileAck(file_id=fid, name=target_name, size=size))
    return acks


async def _rebuild_finalized_from_disk(r: aioredis.Redis, sid: str, sess: dict) -> FinalizeResponse:
    """completed 态幂等返回：从磁盘重建合并文件清单。"""
    staged_path = Path(sess.get("staged_path", ""))
    acks: list[FinalizeFileAck] = []
    total_files = int(sess.get("total_files", "0"))
    for i in range(total_files):
        fid = _file_id(i)
        fm = await r.hgetall(_k_file(sid, fid))
        if not fm:
            continue
        target = staged_path / fm["name"]
        size = target.stat().st_size if target.is_file() else 0
        acks.append(FinalizeFileAck(file_id=fid, name=fm["name"], size=size))
    return FinalizeResponse(sid=sid, staged_path=str(staged_path.resolve()), status=S_COMPLETED, files=acks)


# ============================================================
# abort（取消，同步删）
# ============================================================


async def abort_session(*, sid: str) -> AbortResponse:
    """取消会话：删盘 + 删 Redis + 写短 TTL 取消标记。幂等。

    仅 pending 可 abort；finalizing/completed 拒绝（finalizing 正在合并，completed 是终态）。
    """
    r = get_redis()
    # 取消标记命中 → 幂等返回 aborted
    if await r.exists(_k_tombstone(sid)):
        return AbortResponse(sid=sid, status=S_ABORTED)

    sess = await r.hgetall(_k_session(sid))
    if not sess:
        # 磁盘也找不到 → 已清理，幂等；磁盘还在 → 删
        staged_path = await _find_staged_on_disk(sid)
        if staged_path is None:
            raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND)
        await asyncio.to_thread(shutil.rmtree, str(staged_path), True, _ignore_rmtree_err)
        return AbortResponse(sid=sid, status=S_ABORTED)

    status = sess.get("status", S_PENDING)
    if status == S_FINALIZING:
        raise _conflict("合并进行中，不可取消", ErrorCode.SESSION_FINALIZING)
    if status == S_COMPLETED:
        raise _conflict("会话已完成，不可取消", ErrorCode.SESSION_NOT_FINALIZABLE)

    staged_path = Path(sess.get("staged_path", ""))
    # 同步删盘
    await asyncio.to_thread(shutil.rmtree, str(staged_path), True, _ignore_rmtree_err)
    # 删 Redis 全部相关 key
    await _purge_session_keys(r, sid)
    # 写取消标记（5min，拦截迟到的分片）
    await r.set(_k_tombstone(sid), "1", ex=settings.upload_session_tombstone_ttl_seconds)
    log.info("upload session aborted: sid=%s", sid)
    return AbortResponse(sid=sid, status=S_ABORTED)


async def _find_staged_on_disk(sid: str) -> Path | None:
    """Redis 丢了但磁盘还在：扫 upload/* 找匹配 .session.json 的目录。"""
    root = upload_root()
    for d in await asyncio.to_thread(_list_dirs, root):
        mirror = d / ".session.json"
        if not mirror.is_file():
            continue
        try:
            m = json.loads(mirror.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("sid") == sid:
            return d
    return None


# ============================================================
# 后台清理（TTL 过期 + 孤儿分片目录）
# ============================================================


async def sweep_expired_sessions(r: aioredis.Redis) -> int:
    """扫 model_store_dir/upload/* 清过期/孤儿目录。返回清理数。

    挂 webserver 启动生命周期，每 upload_sweep_interval_seconds 跑一次。
    - Redis 会话还在但 expires_at 已过 → 删盘 + 删 Redis。
    - .session.json 的 sid Redis 无对应会话且已过期 → 删目录。
    - 无 .session.json 的孤儿分片目录，mtime 超 TTL → 删。
    - 顺手删 .tmp 残留（崩溃在 rename 前的）。
    """
    root = upload_root()
    if not await asyncio.to_thread(lambda: root.is_dir()):
        return 0
    cleaned = 0
    for d in await asyncio.to_thread(_list_dirs, root):
        try:
            cleaned += await _sweep_one_dir(r, d)
        except Exception as e:  # 单目录异常不影响整体扫描
            log.warning("sweep dir %s failed: %s", d, e)
    return cleaned


async def _sweep_one_dir(r: aioredis.Redis, d: Path) -> int:
    """处理单个上传目录。返回是否清理（0/1）。

    本地 IO（清 .tmp / stat / 读镜像）合并进一次 ``to_thread(_inspect_dir_sync)``，
    避免每目录 3-4 次串行线程跳转；仅在确实要删时再跳一次 ``rmtree``。
    """
    action, payload = await asyncio.to_thread(_inspect_dir_sync, d)
    if action == "skip":
        return 0
    if action == "delete_orphan":
        log.info("sweep orphan dir: %s (%s)", d, payload)
        await asyncio.to_thread(shutil.rmtree, str(d), True, _ignore_rmtree_err)
        return 1

    # action == "check_redis"：有镜像，需对账 Redis 决定去留
    mirror = payload
    sid = mirror.get("sid", "")
    expires_at = int(mirror.get("expires_at", 0))
    now = _now()
    redis_alive = bool(sid and await r.exists(_k_session(sid)))

    if redis_alive and expires_at > now:
        # 会话还在、未过期 → 保留（finalizing 态单独判：后台合并任务崩溃后
        # 会话永卡 finalizing——重调 409、abort 也拒，只能等 TTL 全删重传。
        # 合并持续写盘会刷新目录 mtime，mtime 滞留超阈值即合并已死 → 判弃：
        # 删盘 + 清键 + 取消标记，客户端下次轮询 session_not_found 走重新上传，
        # 快失败好过挂 409。状态以 Redis 会话哈希为准（镜像只在 declare 时写））
        sess_status = await r.hget(_k_session(sid), "status")
        if sess_status == S_FINALIZING:
            dir_age = _now() - int(d.stat().st_mtime)
            if dir_age > settings.upload_finalize_stale_seconds:
                log.info("sweep stale finalizing dir: sid=%s (age=%ds)", sid, dir_age)
                await _purge_session_keys(r, sid)
                await r.set(_k_tombstone(sid), "1", ex=settings.upload_session_tombstone_ttl_seconds)
                await asyncio.to_thread(shutil.rmtree, str(d), True, _ignore_rmtree_err)
                return 1
        return 0
    if redis_alive and expires_at <= now:
        # Redis 还在但已过期 → 删盘 + 删 Redis
        await _purge_session_keys(r, sid)
    # Redis 无 + 已过期 / Redis 无 + 镜像在 → 删目录
    log.info("sweep expired dir: sid=%s reason=%s", sid, "expired" if expires_at <= now else "redis_gone")
    await asyncio.to_thread(shutil.rmtree, str(d), True, _ignore_rmtree_err)
    return 1


def _inspect_dir_sync(d: Path) -> tuple[str, str | dict | None]:
    """单个目录的同步本地检查：清 .tmp + stat + 读 .session.json。

    返回 ``(action, payload)``：
    - ``("skip", None)``：无镜像且未过期（保留）
    - ``("delete_orphan", reason)``：无镜像+过期 / 镜像损坏（直接删）
    - ``("check_redis", mirror_dict)``：有镜像，需异步对账 Redis
    """
    _clean_tmp_files(d)
    mirror_path = d / ".session.json"
    if not mirror_path.is_file():
        if _is_orphan_stale(d):
            return ("delete_orphan", "no mirror, stale")
        return ("skip", None)
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return ("delete_orphan", f"bad .session.json: {e}")
    return ("check_redis", mirror)


def _clean_tmp_files(d: Path) -> None:
    """删崩溃残留的 ``.tmp`` 分片与 ``.merging`` 合并中间文件（同语义：写一半的中间态）。"""
    try:
        for entry in d.iterdir():
            if entry.name.endswith(".tmp") or entry.name.endswith(".merging"):
                try:
                    entry.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _is_orphan_stale(d: Path) -> bool:
    """孤儿目录（无 .session.json）mtime 超 TTL → 视为可删。"""
    try:
        age = _now() - int(d.stat().st_mtime)
    except OSError:
        return True
    return age > settings.upload_session_ttl_seconds


# ============================================================
# 周期清理任务（挂 webserver 生命周期）
# ============================================================


async def _sweep_once(r: aioredis.Redis) -> None:
    """单轮清理：清 .tmp/孤儿/超期目录。

    首轮由 every(immediate=True) 立即触发，启动即等同自愈——无需单独的
    startup_self_heal 步骤。
    """
    n = await sweep_expired_sessions(r)
    if n:
        log.info("upload sweep: cleaned %d dir(s)", n)


# ============================================================
# 清理任务生命周期（管理端 lifespan 调用，不在 app.py 内联）
# ============================================================


async def start_sweep_task(background_tasks: BackgroundTaskManager) -> None:
    """启动上传清理周期任务（首轮立即扫一次，等同启动自愈）。

    供 ``control_plane_lifespan`` 调用。Redis 未初始化时降级（记 warning，不注册），
    不阻塞管理端启动；任务由调用方的 BackgroundTaskManager 持有。
    """
    from llmpod.db import get_redis as _get_redis

    try:
        redis = _get_redis()
    except Exception as e:  # Redis 未初始化时上传清理不阻塞启动
        log.warning("upload sweep loop disabled: %s", e)
        return
    background_tasks.every(
        lambda: _sweep_once(redis),
        name="upload-sweep",
        interval=settings.upload_sweep_interval_seconds,
        immediate=True,
    )
    log.info("upload sweep loop started (interval=%ss)", settings.upload_sweep_interval_seconds)


# ============================================================
# 内部：异常构造 + 小工具
# ============================================================


def _invalid(msg: str, code: ErrorCode) -> LLMPodValidationError:
    """构造带 error_code 的校验异常（默认 VALIDATION_ERROR，部分用 INVALID_FILENAME）。"""
    return LLMPodValidationError(msg, error_code=code)


def _conflict(msg: str, code: ErrorCode) -> Conflict:
    """构造带 error_code 的 Conflict（上传层多个 409 子码复用此类）。"""
    return Conflict(msg, error_code=code)


def _not_found(resource: str, rid: str, code: ErrorCode) -> NotFound:
    """构造带 error_code 的 NotFound。"""
    return NotFound(resource, rid, error_code=code)


def _is_sha256(s: str) -> bool:
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower())


def _ignore_rmtree_err(func, path, excinfo):  # noqa: ARG001 — shutil.rmtree 的 onerror 签名固定，后两参确实不用
    """shutil.rmtree 的 onerror 回调：忽略清理失败（后台清理尽力而为）。"""
    log.debug("rmtree ignore: %s %s", path, excinfo)


async def _require_active_session(r: aioredis.Redis, sid: str, *, allow_finalizing: bool) -> str:
    """校验会话存在且活跃；返回 status。finalizing 在 part 路径拒绝。"""
    sess = await r.hgetall(_k_session(sid))
    if not sess:
        # 降级查磁盘（会话 Redis 丢了）
        if await _find_staged_on_disk(sid) is not None:
            raise _conflict(
                "会话已失效（Redis 丢失），请重新创建会话上传",
                ErrorCode.SESSION_EXPIRED,
            )
        raise _not_found("session", sid, ErrorCode.SESSION_NOT_FOUND)
    status = sess.get("status", S_PENDING)
    if not allow_finalizing and status == S_FINALIZING:
        raise _conflict("合并进行中，拒新分片", ErrorCode.SESSION_FINALIZING)
    if status == S_ABORTED:
        raise _conflict("会话已取消，不接受分片", ErrorCode.SESSION_ABORTED)
    if status in (S_FAILED, S_COMPLETED):
        raise _conflict(f"会话状态 {status}，不接受分片", ErrorCode.SESSION_EXPIRED)
    return status


async def _purge_session_keys(r: aioredis.Redis, sid: str) -> None:
    """删会话所有 Redis key（主 hash + 取消标记 + 各 file hash + received set）。"""
    pipe = r.pipeline()
    pipe.delete(_k_session(sid))
    pipe.delete(_k_tombstone(sid))
    # file key 数量不定，扫描 {session}{sid}:file:* 模式
    async for key in r.scan_iter(match=f"{_K_SESSION}{sid}:file:*"):
        pipe.delete(key)
    await pipe.execute()
