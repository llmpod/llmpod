"""local 源内容指纹 compute_content_hash 单测（采样哈希：清单 + 最大权重文件）。"""

from __future__ import annotations

from pathlib import Path

from llmpod.apps.manager.model_source.content_hash import compute_content_hash


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_non_dir_returns_none(tmp_path):
    assert compute_content_hash(tmp_path / "missing") is None


def test_empty_dir_returns_none(tmp_path):
    assert compute_content_hash(tmp_path) is None


def test_deterministic_same_content_same_hash(tmp_path):
    _write(tmp_path / "config.json", b"{}")
    _write(tmp_path / "model.safetensors", b"weights-aaa")
    assert compute_content_hash(tmp_path) == compute_content_hash(tmp_path)


def test_content_change_changes_hash(tmp_path):
    _write(tmp_path / "config.json", b"{}")
    _write(tmp_path / "model.safetensors", b"weights-aaa")
    before = compute_content_hash(tmp_path)
    _write(tmp_path / "model.safetensors", b"weights-bbb")  # 覆盖权重
    after = compute_content_hash(tmp_path)
    assert before is not None and after is not None
    assert before != after


def test_file_set_change_changes_hash(tmp_path):
    _write(tmp_path / "config.json", b"{}")
    _write(tmp_path / "model.safetensors", b"weights")
    before = compute_content_hash(tmp_path)
    _write(tmp_path / "tokenizer.json", b"tok")  # 加文件
    assert compute_content_hash(tmp_path) != before


def test_largest_weight_file_represents(tmp_path):
    # 最大权重文件变化 → hash 变（换 checkpoint 的场景）
    _write(tmp_path / "config.json", b"{}")
    _write(tmp_path / "model-00001-of-00002.safetensors", b"a" * 100)
    _write(tmp_path / "model-00002-of-00002.safetensors", b"b" * 1000)
    before = compute_content_hash(tmp_path)
    _write(tmp_path / "model-00002-of-00002.safetensors", b"c" * 1000)  # 最大文件变了
    assert compute_content_hash(tmp_path) != before


def test_skips_temp_files(tmp_path):
    _write(tmp_path / "model.safetensors", b"weights")
    _write(tmp_path / "model.safetensors.part", b"partial")  # 临时残片不计
    a = compute_content_hash(tmp_path)
    (tmp_path / "model.safetensors.part").unlink()
    b = compute_content_hash(tmp_path)
    assert a == b
