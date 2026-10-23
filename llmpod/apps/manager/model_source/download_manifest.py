"""local 源下载清单（manifest）生成。

manifest 是一次 local 源下载的「权威清单」：Node 按 manifest 逐文件下载、Range 续传、
size 校验、原子 rename。生成时刻冻结文件集合与大小，下载期间 Manager 侧文件被改也不影响
本次（不一致以 manifest 为准，宁可重下也不误判）。被 ``routes/public/distribution.py``
的 ``GET /manifest`` 端点消费。

与同包 ``manifest.py``（上传前预校验 ``validate_manifest``）方向相反：那份是「文件还
不在盘上」的声明清单预检，这份是「盘上已就绪」的下载权威清单。收录规则一致：root 下所有
常规文件（权重 + config + tokenizer 等），跳过符号链接与临时产物（``.part`` /
``.llmpod_complete`` / ``.merging`` / ``.tmp`` / ``.session.json``）。
"""

from __future__ import annotations

import os
from pathlib import Path

# manifest 契约模型住 wire 层（tunnel/schemas）：Manager 生成、Node 消费同源 import
from llmpod.tunnel.schemas.model_store import Manifest, ManifestFile

__all__ = ["Manifest", "ManifestFile", "build_manifest"]

# 不纳入清单的临时/标记文件（下载产物、上传残骸）
_EXCLUDE_SUFFIXES = (".part", ".merging", ".tmp", ".session.json")
_EXCLUDE_NAMES = {".llmpod_complete"}


def _keep(fname: str) -> bool:
    name = fname.lower()
    if name in _EXCLUDE_NAMES:
        return False
    return not name.endswith(_EXCLUDE_SUFFIXES)


def build_manifest(root: Path, model_id: int, revision: str | None = None) -> Manifest:
    """遍历 root 生成文件清单（跳过 symlink 与临时产物）。

    ``root`` 必须是已 resolve 的路径，可为目录或单文件——单文件源
    （source_url 直指一个 .gguf 等）生成只含根文件自身的清单（rel = 文件名，
    与目录源同形，Node 按清单路径落盘）。调用方负责路径安全（``safe_resolve``）。
    """
    if root.is_file():
        size = root.stat().st_size
        return Manifest(
            model_id=model_id,
            root=str(root),
            revision=revision,
            files=[ManifestFile(path=root.name, size=size)],
            total_bytes=size,
        )
    files: list[ManifestFile] = []
    total = 0
    for dirpath, _dirs, fnames in os.walk(root, followlinks=False):
        for fname in fnames:
            fp = Path(dirpath) / fname
            if os.path.islink(fp):  # 拒符号链接（防越权下发）
                continue
            if not _keep(fname):
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            rel = fp.relative_to(root).as_posix()
            files.append(ManifestFile(path=rel, size=size))
            total += size
    files.sort(key=lambda f: f.path)  # 稳定排序，便于 diff/断言
    return Manifest(model_id=model_id, root=str(root), revision=revision, files=files, total_bytes=total)
