"""路径安全工具（共享）：解析目标路径并强制其位于某根目录之下。

集中「resolve + 父目录包含」校验，杜绝目录穿越（``..``）、绝对路径逃逸与符号链接越权。
``Path.resolve()`` 会跟随符号链接，故指向 base 之外的 symlink 会被 resolve 到界外 → 校验失败。
Manager（文件服务/删除）与 Node（_manager 下载器落地）共用。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["safe_resolve"]


def safe_resolve(base: Path | str, target: Path | str) -> Path | None:
    """解析 ``target`` 并要求其位于 ``base`` 之下，否则返回 None。

    ``target`` 可为绝对或相对路径；相对时调用方应先 join 到 base（如 ``base / rel``）。
    返回解析后的绝对路径（含 ``base`` 自身）。严格「位于 base 之下、排除 base 本身」
    由调用方按需额外判断 ``result != base_r``。
    """
    base_r = Path(base).expanduser().resolve()
    cand = Path(target).expanduser().resolve()
    if not cand.is_relative_to(base_r):
        return None
    return cand
