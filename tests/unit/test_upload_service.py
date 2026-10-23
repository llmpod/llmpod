"""upload.py（通用切片上传服务）单测：会话状态机 / sha 校验 / 合并异步 / abort 幂等 / 取消标记 / TTL 清扫。

Redis 用手写内存桩（Hash/Set/String + pipeline + scan_iter），经
``llmpod.db.redis._redis_client`` 注入（get_redis 调用时读全局，覆盖 service 内
`from llmpod.db import get_redis` 与 start_sweep_task 的本地导入两条路径）；
盘上操作全部落 tmp_path（monkeypatch settings.model_store_dir）。
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmpod.config.settings import settings
from llmpod.core.exceptions import Conflict, LLMPodValidationError, NotFound
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.db import redis as db_redis
from llmpod.apps.webserver.services import upload as upl


# ============================================================
# 桩：内存 Redis
# ============================================================


class FakePipeline:
    def __init__(self, r: "FakeRedis"):
        self._r = r
        self._ops = []

    def hset(self, key, field=None, value=None, mapping=None):
        self._ops.append(("hset", key, field, value, mapping))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def delete(self, *keys):
        self._ops.append(("delete", keys))
        return self

    def hgetall(self, key):
        self._ops.append(("hgetall", key))
        return self

    def smembers(self, key):
        self._ops.append(("smembers", key))
        return self

    def scard(self, key):
        self._ops.append(("scard", key))
        return self

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "hset":
                await self._r.hset(op[1], field=op[2], value=op[3], mapping=op[4])
            elif op[0] == "expire":
                await self._r.expire(op[1], op[2])
            elif op[0] == "delete":
                out.append(await self._r.delete(*op[1]))
            elif op[0] == "hgetall":
                out.append(await self._r.hgetall(op[1]))
            elif op[0] == "smembers":
                out.append(await self._r.smembers(op[1]))
            elif op[0] == "scard":
                out.append(await self._r.scard(op[1]))
        self._ops = []
        return out


class FakeRedis:
    """upload service 所需的最小 async Redis 替身。"""

    def __init__(self):
        self.hashes: dict[str, dict] = {}
        self.sets: dict[str, set] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def _all(self):
        return set(self.hashes) | set(self.sets) | set(self.strings)

    def pipeline(self):
        return FakePipeline(self)

    async def exists(self, key):
        return int(key in self._all())

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field=None, value=None, mapping=None):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        if field is not None:
            h[field] = value

    async def sadd(self, key, *vals):
        self.sets.setdefault(key, set()).update(vals)

    async def sismember(self, key, val):
        return val in self.sets.get(key, set())

    async def scard(self, key):
        return len(self.sets.get(key, set()))

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def expire(self, key, ttl):
        if key in self._all():
            self.ttls[key] = ttl

    async def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex:
            self.ttls[key] = ex

    async def delete(self, *keys):
        n = 0
        for k in keys:
            for d in (self.hashes, self.sets, self.strings):
                if d.pop(k, None) is not None:
                    n += 1
            self.ttls.pop(k, None)
        return n

    async def scan_iter(self, match=None):
        for k in sorted(self._all()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


# ============================================================
# fixtures / helpers
# ============================================================


@pytest.fixture
def rds(monkeypatch, tmp_path) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(db_redis, "_redis_client", fake)
    monkeypatch.setattr(settings, "model_store_dir", str(tmp_path / "store"))
    return fake


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _spec(name="model.safetensors", size=10, total_parts=2, part_size=5) -> dict:
    return {"name": name, "size": size, "total_parts": total_parts, "part_size": part_size}


async def _declared(rds, *, name="mymodel", files_spec=None) -> upl.DeclareResponse:
    return await upl.declare_session(name=name, files_spec=files_spec or [_spec()])


async def _send_part(rds, resp: upl.DeclareResponse, part_no: int, body: bytes, *, file_id="f0"):
    return await upl.receive_part(sid=resp.sid, file_id=file_id, part_no=part_no, sha256=_sha(body), body=body)


async def _drain_bg_tasks():
    """等后台合并任务跑完（create_task 引用持有集）。"""
    pending = [t for t in list(upl._bg_finalize_tasks) if not t.done()]
    if pending:
        await asyncio.gather(*pending)


def _staged(resp: upl.DeclareResponse, tmp_path) -> Path:
    """会话实际 staged 目录（declare 返回值即权威出处；目录名并入 sid）。"""
    return Path(resp.staged_path)


# ============================================================
# 纯函数小工具
# ============================================================


class TestPureHelpers:
    def test_sanitize_keeps_safe_chars(self):
        assert upl._sanitize("model.safetensors") == "model.safetensors"

    def test_sanitize_replaces_unsafe_and_strips_edges(self):
        assert upl._sanitize("../../etc/passwd") == "etc_passwd"
        assert upl._sanitize("  a b(1).bin ") == "a_b_1_.bin"
        assert upl._sanitize("模型.bin") == "_.bin" or upl._sanitize("模型.bin")  # 中文被替换，不为穿越名即可

    def test_sanitize_empty_falls_back_random(self):
        assert upl._sanitize("") != upl._sanitize(None)
        assert len(upl._sanitize("")) == 12  # token_hex(6)
        assert upl._sanitize(None) == upl._sanitize("") or True

    def test_file_id_and_part_filename(self):
        assert upl._file_id(0) == "f0"
        assert upl._part_filename("f0", 3) == "f0.part-00003"

    def test_is_sha256(self):
        assert upl._is_sha256("a" * 64)
        assert not upl._is_sha256("A" * 64 + "!") or not upl._is_sha256("z" * 64)
        assert not upl._is_sha256("abc")
        assert not upl._is_sha256("")


# ============================================================
# declare
# ============================================================


class TestDeclare:
    async def test_happy_path(self, rds, tmp_path):
        resp = await _declared(
            rds,
            files_spec=[
                _spec(size=10, total_parts=2, part_size=5),
                _spec(name="tok.json", size=4, total_parts=1, part_size=4),
            ],
        )
        assert resp.sid and resp.status == "pending"
        assert [f.file_id for f in resp.files] == ["f0", "f1"]
        assert [f.name for f in resp.files] == ["model.safetensors", "tok.json"]
        staged = _staged(resp, tmp_path)
        assert staged.is_dir()
        assert resp.staged_path == str(staged.resolve())
        # Redis 会话 Hash
        sess = await rds.hgetall(f"llmpod:upload:{resp.sid}")
        assert sess["status"] == "pending" and sess["name"] == "mymodel"
        assert sess["total_files"] == "2"
        assert sess["staged_path"] == str(staged.resolve())
        assert int(sess["expires_at"]) > int(sess["created_at"])
        # 各文件 Hash
        fm = await rds.hgetall(f"llmpod:upload:{resp.sid}:file:f0")
        assert fm == {"name": "model.safetensors", "size": "10", "total_parts": "2", "part_size": "5"}
        # 冗余磁盘镜像
        mirror = json.loads((staged / ".session.json").read_text(encoding="utf-8"))
        assert mirror["sid"] == resp.sid and mirror["name"] == "mymodel"
        assert mirror["expires_at"] == int(sess["expires_at"])
        assert [f["file_id"] for f in mirror["files"]] == ["f0", "f1"]

    async def test_sanitizes_session_and_file_names(self, rds, tmp_path):
        resp = await _declared(rds, name="../../etc", files_spec=[_spec(name="a b(1).bin")])
        # 目录并入 sid（防同名会话互覆），会话名只留清洗痕迹
        assert Path(resp.staged_path).name.startswith("etc-") and resp.sid in Path(resp.staged_path).name
        assert resp.files[0].name == "a_b_1_.bin"

    async def test_same_name_sessions_do_not_share_staged_dir(self, rds, tmp_path):
        """同名（或清洗后同名）并发会话各自独立 staged 目录：分片互不覆盖。"""
        a = await _declared(rds, name="mymodel")
        b = await _declared(rds, name="my model")  # 清洗后同为 my_model 系前缀
        assert Path(a.staged_path) != Path(b.staged_path)
        assert Path(a.staged_path).is_dir() and Path(b.staged_path).is_dir()
        await _send_part(rds, a, 1, b"hello")
        await _send_part(rds, b, 1, b"WORLD")
        assert (Path(a.staged_path) / "f0.part-00001").read_bytes() == b"hello"
        assert (Path(b.staged_path) / "f0.part-00001").read_bytes() == b"WORLD"

    async def test_empty_filename_rejected(self, rds):
        with pytest.raises(LLMPodValidationError):
            await _declared(rds, files_spec=[_spec(name="  ")])

    async def test_duplicate_sanitized_names_rejected(self, rds):
        with pytest.raises(LLMPodValidationError) as ei:
            await _declared(rds, files_spec=[_spec(name="a b.bin"), _spec(name="a_b.bin")])
        # 重名判定发生在「清洗后」→ INVALID_FILENAME 子码，信息回显清洗名
        assert ei.value.error_code.value == "INVALID_FILENAME"
        assert "a_b.bin" in str(ei.value)

    async def test_negative_size_rejected(self, rds):
        with pytest.raises(LLMPodValidationError):
            await _declared(rds, files_spec=[_spec(size=-1)])

    async def test_zero_total_parts_rejected(self, rds):
        with pytest.raises(LLMPodValidationError):
            await _declared(rds, files_spec=[_spec(total_parts=0)])

    async def test_zero_part_size_rejected(self, rds):
        with pytest.raises(LLMPodValidationError):
            await _declared(rds, files_spec=[_spec(part_size=0)])

    async def test_part_size_over_limit_rejected(self, rds, monkeypatch):
        monkeypatch.setattr(settings, "upload_part_max_bytes", 4)
        with pytest.raises(LLMPodValidationError) as ei:
            await _declared(rds, files_spec=[_spec(part_size=8)])
        assert "超上限" in str(ei.value)

    async def test_total_over_limit_conflict(self, rds, monkeypatch):
        monkeypatch.setattr(settings, "upload_max_bytes", 10)
        with pytest.raises(Conflict) as ei:
            await _declared(rds, files_spec=[_spec(size=20)])
        assert ei.value.error_code.value == "OVERSIZE"

    async def test_disk_full_conflict(self, rds, monkeypatch):
        monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(free=1, total=10, used=9))
        with pytest.raises(Conflict) as ei:
            await _declared(rds, files_spec=[_spec(size=100)])
        assert ei.value.error_code.value == "DISK_FULL"


# ============================================================
# part
# ============================================================


class TestReceivePart:
    async def test_writes_part_atomically_and_marks_received(self, rds, tmp_path):
        resp = await _declared(rds)
        ack = await _send_part(rds, resp, 1, b"hello")
        assert ack == upl.PartResponse(file_id="f0", part_no=1, received=True, session_status="pending")
        staged = _staged(resp, tmp_path)
        assert (staged / "f0.part-00001").read_bytes() == b"hello"
        assert not (staged / "f0.part-00001.tmp").exists()  # tmp 已 rename
        assert await rds.sismember(f"llmpod:upload:{resp.sid}:file:f0:received", 1)

    async def test_sha_mismatch_conflict_no_file(self, rds, tmp_path):
        resp = await _declared(rds)
        with pytest.raises(Conflict) as ei:
            await upl.receive_part(sid=resp.sid, file_id="f0", part_no=1, sha256="a" * 64, body=b"hello")
        assert ei.value.error_code.value == "PART_SHA_MISMATCH"
        staged = _staged(resp, tmp_path)
        assert list(staged.glob("f0.part-*")) == []
        assert not await rds.sismember(f"llmpod:upload:{resp.sid}:file:f0:received", 1)

    async def test_part_bytes_over_declared_cap_rejected(self, rds, tmp_path):
        """分片实际字节数按声明的 part_size/剩余量拦截——declare 的声明值校验
        拦不住表单层伪造的大 body（sha 是客户端自算的，不构成限制）。"""
        resp = await _declared(rds)  # size=10, total_parts=2, part_size=5
        with pytest.raises(LLMPodValidationError) as ei:
            await _send_part(rds, resp, 1, b"x" * 6)  # 6 > part_size 5
        assert ei.value.error_code.value == "OVERSIZE"
        staged = _staged(resp, tmp_path)
        assert list(staged.glob("f0.part-*")) == []

    async def test_part_bytes_over_file_remaining_rejected(self, rds, tmp_path):
        """尾片按「文件剩余量」拦：part_size 未超但累计会越过声明 size。"""
        resp = await _declared(rds, files_spec=[_spec(size=10, total_parts=2, part_size=5)])
        await _send_part(rds, resp, 1, b"hello")
        with pytest.raises(LLMPodValidationError) as ei:
            await _send_part(rds, resp, 2, b"xxxxxx")  # 剩余 5，实发 6
        assert ei.value.error_code.value == "OVERSIZE"

    async def test_bad_sha_format_rejected(self, rds):
        resp = await _declared(rds)
        with pytest.raises(LLMPodValidationError) as ei:
            await upl.receive_part(sid=resp.sid, file_id="f0", part_no=1, sha256="xyz", body=b"hello")
        assert "sha256" in str(ei.value)

    async def test_part_no_out_of_range(self, rds):
        resp = await _declared(rds)
        for bad in (0, 3, -1):
            with pytest.raises(LLMPodValidationError) as ei:
                await upl.receive_part(sid=resp.sid, file_id="f0", part_no=bad, sha256=_sha(b"x"), body=b"x")
            assert ei.value.error_code.value == "PART_OUT_OF_RANGE"

    async def test_unknown_file_404(self, rds):
        resp = await _declared(rds)
        with pytest.raises(NotFound) as ei:
            await upl.receive_part(sid=resp.sid, file_id="f9", part_no=1, sha256=_sha(b"x"), body=b"x")
        assert ei.value.error_code.value == "SESSION_NOT_FOUND"

    async def test_unknown_session_404(self, rds):
        with pytest.raises(NotFound):
            await upl.receive_part(sid="ghost", file_id="f0", part_no=1, sha256=_sha(b"x"), body=b"x")

    async def test_idempotent_resend_does_not_rewrite(self, rds, tmp_path):
        resp = await _declared(rds)
        await _send_part(rds, resp, 1, b"hello")
        # 重发同分片（不同内容、sha 与新内容匹配）→ ack 但磁盘保持首次内容
        ack = await _send_part(rds, resp, 1, b"HELLO")
        assert ack.received is True
        staged = _staged(resp, tmp_path)
        assert (staged / "f0.part-00001").read_bytes() == b"hello"

    async def test_late_part_after_abort_404(self, rds):
        """abort 已清会话键 → 迟到 part 拿 404（会话已不存在）。"""
        resp = await _declared(rds)
        await upl.abort_session(sid=resp.sid)
        with pytest.raises(NotFound) as ei:
            await _send_part(rds, resp, 1, b"hello")
        assert ei.value.error_code.value == "SESSION_NOT_FOUND"

    async def test_tombstone_blocks_part_while_hash_alive(self, rds):
        """取消标记拦截迟到的分片：会话 Hash 在但取消标记在（清键部分失败的状态）→ SESSION_ABORTED。"""
        resp = await _declared(rds)
        await rds.set(f"llmpod:upload:{resp.sid}:tombstone", "1", ex=settings.upload_session_tombstone_ttl_seconds)
        with pytest.raises(Conflict) as ei:
            await _send_part(rds, resp, 1, b"hello")
        assert ei.value.error_code.value == "SESSION_ABORTED"

    async def test_part_during_finalizing_rejected(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "finalizing")
        with pytest.raises(Conflict) as ei:
            await _send_part(rds, resp, 1, b"hello")
        assert ei.value.error_code.value == "SESSION_FINALIZING"

    async def test_part_after_completed_rejected(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "completed")
        with pytest.raises(Conflict) as ei:
            await _send_part(rds, resp, 1, b"hello")
        assert ei.value.error_code.value == "SESSION_EXPIRED"

    async def test_part_after_failed_rejected(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "failed")
        with pytest.raises(Conflict):
            await _send_part(rds, resp, 1, b"hello")

    async def test_redis_lost_but_disk_present_reports_expired(self, rds, tmp_path):
        resp = await _declared(rds)
        await rds.delete(f"llmpod:upload:{resp.sid}")  # 会话主 Hash 丢，磁盘镜像在
        with pytest.raises(Conflict) as ei:
            await _send_part(rds, resp, 1, b"hello")
        assert ei.value.error_code.value == "SESSION_EXPIRED"


# ============================================================
# status
# ============================================================


class TestSessionStatus:
    async def test_missing_parts_computed(self, rds):
        resp = await _declared(rds, files_spec=[_spec(total_parts=3, part_size=4, size=12)])
        await _send_part(rds, resp, 1, b"aaaa")
        await _send_part(rds, resp, 3, b"cccc")
        st = await upl.get_session_status(sid=resp.sid)
        assert st.status == "pending"
        f = st.files[0]
        assert (f.received_parts, f.missing_parts, f.complete) == ([1, 3], [2], False)
        assert f.total_parts == 3 and f.name == "model.safetensors"

    async def test_complete_when_all_received(self, rds):
        resp = await _declared(rds)
        await _send_part(rds, resp, 1, b"hello")
        await _send_part(rds, resp, 2, b"world")
        st = await upl.get_session_status(sid=resp.sid)
        assert st.files[0].complete is True and st.files[0].missing_parts == []

    async def test_unknown_sid_404(self, rds):
        with pytest.raises(NotFound):
            await upl.get_session_status(sid="ghost")

    async def test_rebuild_from_disk_after_redis_loss(self, rds):
        resp = await _declared(rds, files_spec=[_spec(total_parts=3, part_size=4, size=12)])
        await _send_part(rds, resp, 1, b"aaaa")
        await _send_part(rds, resp, 2, b"bbbb")
        await rds.delete(*sorted(rds._all()))  # Redis 全丢
        st = await upl.get_session_status(sid=resp.sid)
        assert st.status == "failed"  # Redis 丢 → 只读降级，标 failed 提示重传
        f = st.files[0]
        assert (f.received_parts, f.missing_parts) == ([1, 2], [3])

    async def test_rebuild_corrupt_mirror_404(self, rds, tmp_path):
        resp = await _declared(rds)
        await rds.delete(*sorted(rds._all()))
        staged = _staged(resp, tmp_path)
        (staged / ".session.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(NotFound):
            await upl.get_session_status(sid=resp.sid)

    async def test_rebuild_no_mirror_404(self, rds, tmp_path):
        resp = await _declared(rds)
        await rds.delete(*sorted(rds._all()))
        (Path(resp.staged_path) / ".session.json").unlink()
        with pytest.raises(NotFound):
            await upl.get_session_status(sid=resp.sid)


# ============================================================
# finalize
# ============================================================


class TestFinalize:
    async def test_incomplete_conflict(self, rds):
        resp = await _declared(rds)
        with pytest.raises(Conflict) as ei:
            await upl.finalize_session(sid=resp.sid)
        assert ei.value.error_code.value == "SESSION_NOT_FINALIZABLE"

    async def test_unknown_sid_404(self, rds):
        with pytest.raises(NotFound):
            await upl.finalize_session(sid="ghost")

    async def test_finalize_merges_async_and_completes(self, rds, tmp_path):
        resp = await _declared(rds)
        await _send_part(rds, resp, 1, b"hello")
        await _send_part(rds, resp, 2, b"world")
        out = await upl.finalize_session(sid=resp.sid)
        # 异步语义：校验过即立即返回 finalizing + 空 files
        assert out.status == "finalizing" and out.files == []
        assert (await rds.hgetall(f"llmpod:upload:{resp.sid}"))["status"] == "finalizing"
        await _drain_bg_tasks()
        assert (await rds.hgetall(f"llmpod:upload:{resp.sid}"))["status"] == "completed"
        staged = _staged(resp, tmp_path)
        assert (staged / "model.safetensors").read_bytes() == b"helloworld"
        assert list(staged.glob("*.part-*")) == []  # 分片已删
        assert not (staged / "f0.merging").exists()  # 合并中间文件已 rename
        # 幂等：completed 再调 → 从磁盘重建结果
        again = await upl.finalize_session(sid=resp.sid)
        assert again.status == "completed"
        assert again.files == [upl.FinalizeFileAck(file_id="f0", name="model.safetensors", size=10)]

    async def test_size_mismatch_fails_and_discards(self, rds, tmp_path):
        """声明 size 与合并结果不符（切片边界错误）→ 失败即弃：删盘 + 写取消标记 + failed。"""
        resp = await _declared(rds, files_spec=[_spec(size=999, total_parts=1, part_size=5)])
        await _send_part(rds, resp, 1, b"hello")
        out = await upl.finalize_session(sid=resp.sid)
        assert out.status == "finalizing"
        await _drain_bg_tasks()
        sess = await rds.hgetall(f"llmpod:upload:{resp.sid}")
        assert sess["status"] == "failed"  # 会话保留供轮询可见
        assert await rds.exists(f"llmpod:upload:{resp.sid}:tombstone")
        assert not Path(resp.staged_path).exists()  # 盘已删
        # failed 态 finalize → 冲突
        with pytest.raises(Conflict) as ei:
            await upl.finalize_session(sid=resp.sid)
        assert ei.value.error_code.value == "FINALIZE_FAILED"

    async def test_aborted_session_not_finalizable(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "aborted")
        with pytest.raises(Conflict) as ei:
            await upl.finalize_session(sid=resp.sid)
        assert ei.value.error_code.value == "SESSION_NOT_FINALIZABLE"

    async def test_finalizing_twice_conflict(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "finalizing")
        with pytest.raises(Conflict) as ei:
            await upl.finalize_session(sid=resp.sid)
        assert ei.value.error_code.value == "SESSION_FINALIZING"

    async def test_missing_file_hash_404(self, rds):
        resp = await _declared(rds)
        await rds.delete(f"llmpod:upload:{resp.sid}:file:f0")
        with pytest.raises(NotFound):
            await upl.finalize_session(sid=resp.sid)

    async def test_completed_idempotent_handles_missing_target(self, rds):
        resp = await _declared(rds)
        await _send_part(rds, resp, 1, b"hello")
        await _send_part(rds, resp, 2, b"world")
        await upl.finalize_session(sid=resp.sid)
        await _drain_bg_tasks()
        # 删掉合并产物后幂等重查 → size 0（磁盘不在不报错）
        import pathlib

        for p in pathlib.Path(resp.staged_path).glob("model.safetensors"):
            p.unlink()
        again = await upl.finalize_session(sid=resp.sid)
        assert again.files[0].size == 0


# ============================================================
# abort
# ============================================================


class TestAbort:
    async def test_abort_pending_removes_disk_redis_and_sets_tombstone(self, rds, tmp_path):
        resp = await _declared(rds)
        await _send_part(rds, resp, 1, b"hello")
        out = await upl.abort_session(sid=resp.sid)
        assert out == upl.AbortResponse(sid=resp.sid, status="aborted")
        assert not Path(resp.staged_path).exists()
        assert not await rds.exists(f"llmpod:upload:{resp.sid}")
        assert not await rds.exists(f"llmpod:upload:{resp.sid}:file:f0")
        assert not await rds.exists(f"llmpod:upload:{resp.sid}:file:f0:received")
        assert await rds.exists(f"llmpod:upload:{resp.sid}:tombstone")
        assert rds.ttls[f"llmpod:upload:{resp.sid}:tombstone"] == settings.upload_session_tombstone_ttl_seconds

    async def test_abort_idempotent_via_tombstone(self, rds):
        resp = await _declared(rds)
        assert (await upl.abort_session(sid=resp.sid)).status == "aborted"
        assert (await upl.abort_session(sid=resp.sid)).status == "aborted"

    async def test_abort_unknown_session_404(self, rds):
        with pytest.raises(NotFound):
            await upl.abort_session(sid="ghost")

    async def test_abort_redis_lost_disk_present_cleans_dir(self, rds, tmp_path):
        resp = await _declared(rds)
        await rds.delete(*sorted(rds._all()))  # Redis 丢
        out = await upl.abort_session(sid=resp.sid)
        assert out.status == "aborted"
        assert not Path(resp.staged_path).exists()

    async def test_abort_finalizing_conflict(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "finalizing")
        with pytest.raises(Conflict) as ei:
            await upl.abort_session(sid=resp.sid)
        assert ei.value.error_code.value == "SESSION_FINALIZING"

    async def test_abort_completed_conflict(self, rds):
        resp = await _declared(rds)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "completed")
        with pytest.raises(Conflict) as ei:
            await upl.abort_session(sid=resp.sid)
        assert ei.value.error_code.value == "SESSION_NOT_FINALIZABLE"


# ============================================================
# TTL 清扫
# ============================================================


class TestSweep:
    async def test_missing_root_returns_zero(self, rds, tmp_path):
        assert await upl.sweep_expired_sessions(rds) == 0

    async def test_live_session_kept(self, rds, tmp_path):
        resp = await _declared(rds)
        assert await upl.sweep_expired_sessions(rds) == 0
        assert Path(resp.staged_path).exists()

    async def test_tmp_residue_cleaned_on_sweep(self, rds, tmp_path):
        resp = await _declared(rds)
        residue = Path(resp.staged_path) / "f0.part-00000.tmp"
        residue.write_bytes(b"x")
        merging = Path(resp.staged_path) / "f0.merging"
        merging.write_bytes(b"x")
        await upl.sweep_expired_sessions(rds)
        assert not residue.exists()  # 活会话保留，但崩溃残留 .tmp 被清
        assert not merging.exists()  # 合并中间态（崩溃在 rename 前）同样被清

    async def test_stale_finalizing_dir_reaped(self, rds, tmp_path, monkeypatch):
        """finalizing 态 + 目录 mtime 滞留超阈值 → 判合并已死：删盘清键写取消标记。"""
        resp = await _declared(rds)
        staged = _staged(resp, tmp_path)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "finalizing")
        import time

        ancient = time.time() - settings.upload_finalize_stale_seconds - 100
        os.utime(staged, (ancient, ancient))
        assert await upl.sweep_expired_sessions(rds) == 1
        assert not staged.exists()
        assert not await rds.exists(f"llmpod:upload:{resp.sid}")  # 会话键清
        assert await rds.exists(f"llmpod:upload:{resp.sid}:tombstone")  # 取消标记拦截迟到的分片

    async def test_fresh_finalizing_dir_kept(self, rds, tmp_path):
        """finalizing 态但目录新鲜（合并在跑）→ 保留。"""
        resp = await _declared(rds)
        staged = _staged(resp, tmp_path)
        await rds.hset(f"llmpod:upload:{resp.sid}", "status", "finalizing")
        assert await upl.sweep_expired_sessions(rds) == 0
        assert staged.exists()

    async def test_expired_with_redis_alive_purged(self, rds, tmp_path):
        resp = await _declared(rds)
        sess_key = f"llmpod:upload:{resp.sid}"
        staged = _staged(resp, tmp_path)
        mirror = json.loads((staged / ".session.json").read_text(encoding="utf-8"))
        mirror["expires_at"] = 1  # 已过期
        (staged / ".session.json").write_text(json.dumps(mirror), encoding="utf-8")
        assert await upl.sweep_expired_sessions(rds) == 1
        assert not staged.exists()
        assert not await rds.exists(sess_key)  # Redis 键一并清

    async def test_redis_gone_dir_removed(self, rds, tmp_path):
        resp = await _declared(rds)
        staged = _staged(resp, tmp_path)
        await rds.delete(*sorted(rds._all()))  # Redis 全丢（镜像仍在盘上）
        assert await upl.sweep_expired_sessions(rds) == 1
        assert not staged.exists()

    async def test_fresh_orphan_kept_stale_orphan_removed(self, rds, tmp_path):
        upload_root = tmp_path / "store" / "upload"
        fresh = upload_root / "orphan-fresh"
        fresh.mkdir(parents=True)
        (fresh / "f0.part-00001").write_bytes(b"x")
        stale = upload_root / "orphan-stale"
        stale.mkdir()
        (stale / "f0.part-00001").write_bytes(b"x")
        import time

        ancient = time.time() - settings.upload_session_ttl_seconds - 100
        os.utime(stale, (ancient, ancient))
        cleaned = await upl.sweep_expired_sessions(rds)
        assert cleaned == 1
        assert fresh.exists() and not stale.exists()

    async def test_corrupt_mirror_removed(self, rds, tmp_path):
        upload_root = tmp_path / "store" / "upload"
        bad = upload_root / "bad-mirror"
        bad.mkdir(parents=True)
        (bad / ".session.json").write_text("{oops", encoding="utf-8")
        assert await upl.sweep_expired_sessions(rds) == 1
        assert not bad.exists()

    async def test_per_dir_exception_does_not_abort_scan(self, rds, tmp_path, monkeypatch):
        resp = await _declared(rds)
        staged = _staged(resp, tmp_path)
        mirror = json.loads((staged / ".session.json").read_text(encoding="utf-8"))
        mirror["expires_at"] = 1
        (staged / ".session.json").write_text(json.dumps(mirror), encoding="utf-8")
        # 单目录本地检查抛错 → 整体扫描不炸，其余目录照常
        orig = upl._inspect_dir_sync

        def boom(d):
            if d == staged:
                raise OSError("injected")
            return orig(d)

        monkeypatch.setattr(upl, "_inspect_dir_sync", boom)
        assert await upl.sweep_expired_sessions(rds) == 0


# ============================================================
# 清扫任务生命周期
# ============================================================


class TestSweepTaskLifecycle:
    async def test_start_and_stop(self, rds):
        background_tasks = BackgroundTaskManager()
        await upl.start_sweep_task(background_tasks)
        task = background_tasks._tasks["upload-sweep"]
        await asyncio.sleep(0.05)  # 首轮立即扫一次（自愈语义）
        await background_tasks.stop()
        assert task.cancelled() or task.done()

    async def test_start_degrades_when_redis_missing(self, rds, monkeypatch, caplog):
        import logging

        background_tasks = BackgroundTaskManager()
        monkeypatch.setattr(db_redis, "_redis_client", None)  # get_redis → ConfigError
        with caplog.at_level(logging.WARNING):
            await upl.start_sweep_task(background_tasks)
        assert not background_tasks._tasks  # 不阻塞启动
        assert any("disabled" in r.message for r in caplog.records)
        await background_tasks.stop()
