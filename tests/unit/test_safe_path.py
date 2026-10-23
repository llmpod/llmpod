"""safe_resolve 路径安全单测：穿越 / 绝对逃逸 / symlink / 根本身。"""

from __future__ import annotations

import os

from llmpod.utils.safe_path import safe_resolve


def test_valid_subpath_returns_resolved(tmp_path):
    base = tmp_path
    (base / "sub").mkdir()
    f = base / "sub" / "a.txt"
    f.write_text("x")
    assert safe_resolve(base, base / "sub" / "a.txt") == f.resolve()


def test_absolute_outside_rejected(tmp_path):
    assert safe_resolve(tmp_path, "/etc/hosts") is None


def test_relative_traversal_rejected(tmp_path):
    target = (tmp_path / ".." / "..").resolve()  # 解析后跑到 base 之外
    assert safe_resolve(tmp_path, target) is None


def test_base_itself_allowed(tmp_path):
    """safe_resolve 允许 == base；调用方按需用 != base 做「严格之下」判断。"""
    assert safe_resolve(tmp_path, tmp_path) == tmp_path.resolve()


def test_symlink_escape_rejected(tmp_path):
    """base 内 symlink 指向 base 外 → resolve 跟随到界外 → None。"""
    base = tmp_path / "store"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    os.symlink(outside, base / "evil")
    assert safe_resolve(base, base / "evil") is None
