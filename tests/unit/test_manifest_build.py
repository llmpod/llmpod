"""build_manifest 单测：收录常规文件、跳过 symlink 与临时产物、POSIX 路径与总字节。"""

from __future__ import annotations

import os

from llmpod.apps.manager.model_source.download_manifest import build_manifest


def test_collects_all_regular_files_and_sizes(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"x" * 100)
    sub = tmp_path / "tk"
    sub.mkdir()
    (sub / "tokenizer.json").write_bytes(b"y" * 10)

    m = build_manifest(tmp_path, model_id=7, revision="abc")
    assert {f.path for f in m.files} == {"config.json", "model.safetensors", "tk/tokenizer.json"}
    assert m.total_bytes == 100 + 10 + 2
    assert m.revision == "abc"


def test_skips_symlink(tmp_path):
    """symbolic link 不纳入清单（防越权下发）。"""
    (tmp_path / "real.bin").write_bytes(b"abc")
    os.symlink(tmp_path / "real.bin", tmp_path / "link.bin")
    m = build_manifest(tmp_path, model_id=1)
    paths = {f.path for f in m.files}
    assert "real.bin" in paths
    assert "link.bin" not in paths


def test_excludes_temp_artifacts(tmp_path):
    """.part / .llmpod_complete / .merging / .tmp / .session.json 不纳入。"""
    for name in ("a.part", "b.merging", "c.tmp", ".session.json"):
        (tmp_path / name).write_text("x")
    (tmp_path / ".llmpod_complete").write_text("done")
    (tmp_path / "keep.safetensors").write_bytes(b"zz")
    m = build_manifest(tmp_path, model_id=1)
    assert {f.path for f in m.files} == {"keep.safetensors"}
