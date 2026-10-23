"""local 源内容指纹：采样哈希（而非全量 sha256）。

换 checkpoint 时所有权重分片一起变，取最大分片足以代表——比全量哈希快一个数量级。
清单 (name, size) 兜住增删/改名/改大小。目录名是缓存键，宁可漏（只改 config/tokenizer
或只改某个非最大分片时不换目录）也不乱重下。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from llmpod.apps.manager.model_source.download_manifest import _keep
from llmpod.apps.manager.model_source.inference import WEIGHT_EXTS

_CHUNK = 1 << 20  # 1MB 流式读


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_content_hash(root: Path) -> str | None:
    """local 源内容指纹 = sha256(排序清单 (name,size) + "|" + 最大权重文件 sha256)。

    目录不可读 / 无任何文件 → None（不阻断建模，退化回仅形状 digest）。无权重文件（纯
    config/tokenizer）→ 清单指纹兜底（仍能感知文件增删/大小变化）。
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return None

    entries: list[tuple[str, int]] = []
    weight_sizes: list[tuple[int, Path]] = []  # (size, path)：复用走查已取的 size，避免二次 stat
    for dirpath, _dirs, fnames in os.walk(root, followlinks=False):
        for fname in fnames:
            fp = Path(dirpath) / fname
            if os.path.islink(fp) or not _keep(fname):
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            entries.append((fp.relative_to(root).as_posix(), size))
            if fname.lower().endswith(WEIGHT_EXTS):
                weight_sizes.append((size, fp))

    if not entries:
        return None

    entries.sort(key=lambda x: x[0])
    manifest_hash = hashlib.sha256("\n".join(f"{name}:{size}" for name, size in entries).encode()).hexdigest()

    biggest = max(weight_sizes, key=lambda t: t[0], default=None)
    content_hash = _sha256_file(biggest[1]) if biggest is not None else ""
    return hashlib.sha256(f"{manifest_hash}|{content_hash}".encode()).hexdigest()
