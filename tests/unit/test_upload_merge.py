"""_merge_all_parts 合并逻辑单测（纯文件 IO，无 DB/Redis）。

合并/清理这类「扫盘+对账」纯函数另写单测，不进 HTTP 层。覆盖：
- 正确合并（merged_size == 声明 size）+ 分片删除 + 内容按序拼接；
- 合并大小校验（merged_size != 声明 size → 抛异常，由 finalize_session 走失败即弃）；
- 分片文件缺失 → FileNotFoundError（既有行为，锁住）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmpod.apps.webserver.services.upload import _merge_all_parts, _part_filename


def _write_part(staged: Path, fid: str, part_no: int, data: bytes) -> None:
    (staged / _part_filename(fid, part_no)).write_bytes(data)


def _meta(fid: str, name: str, size: int, total_parts: int, part_size: int = 20) -> dict:
    # 形如 finalize_session 组装：size/part_size 来自 Redis hgetall（字符串）
    return {
        "file_id": fid,
        "name": name,
        "size": str(size),
        "total_parts": total_parts,
        "part_size": str(part_size),
    }


class TestMergeAllParts:
    def test_merge_size_match_ok(self, tmp_path: Path):
        _write_part(tmp_path, "f0", 1, b"x" * 20)
        _write_part(tmp_path, "f0", 2, b"y" * 10)  # 末片小

        acks = _merge_all_parts(tmp_path, [_meta("f0", "model.safetensors", size=30, total_parts=2)])

        assert len(acks) == 1
        assert acks[0].file_id == "f0"
        assert acks[0].size == 30
        # 内容按 part_no 升序拼接
        assert (tmp_path / "model.safetensors").read_bytes() == b"x" * 20 + b"y" * 10
        # 分片已删
        assert not (tmp_path / _part_filename("f0", 1)).exists()
        assert not (tmp_path / _part_filename("f0", 2)).exists()

    def test_merge_size_mismatch_raises(self, tmp_path: Path):
        # 实际 20+10=30，但声明 size=99 → 切片边界不一致 → 抛异常
        _write_part(tmp_path, "f0", 1, b"x" * 20)
        _write_part(tmp_path, "f0", 2, b"y" * 10)

        with pytest.raises(ValueError, match="合并大小"):
            _merge_all_parts(tmp_path, [_meta("f0", "model.safetensors", size=99, total_parts=2)])

    def test_merge_missing_part_raises(self, tmp_path: Path):
        # 声明 2 片但只写 1 片 → 合并时 FileNotFoundError（既有行为）
        _write_part(tmp_path, "f0", 1, b"x" * 20)

        with pytest.raises(FileNotFoundError):
            _merge_all_parts(tmp_path, [_meta("f0", "m.bin", size=30, total_parts=2)])
