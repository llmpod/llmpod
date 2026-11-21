"""节点侧模型目录探测与受控删除。

只读探测返回**原始事实**（配置文件与普通文件清单），模型语义判断与 ``ModelFiles``
装配留在 manager——与「节点是运行时无关执行器」的定位一致。

安全边界（探测与删除共用）：
- 只接受绝对路径；
- 路径不得包含符号链接；
- 必须落在允许的模型根目录内（``model_store_dir`` + ``model_import_roots``），
  且不能直接选择根目录本身；
- 删除只接受「看起来是模型目录」的目标（含 config 或权重文件）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from llmpod.config.constants import MODEL_WEIGHT_EXTS
from llmpod.tunnel.schemas import ModelDirEntry, ModelDirProbe, RemoveModelDirResult
from llmpod.utils.safetensors_meta import sum_ngram_table_bytes

# 单个目录的普通文件数上限：异常目录（如误选 $HOME）不能把通信帧撑爆。
_MAX_FILES = 10_000

_CONFIG_NAMES = ("config.json", "model_index.json")


def _allowed_roots(store_dir: str, import_roots: str | None) -> list[Path]:
    raw = [store_dir, *(import_roots or "").split(",")]
    roots: list[Path] = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        try:
            resolved = Path(item).expanduser().resolve(strict=False)
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _has_symlink_component(path: Path) -> bool:
    """路径自身或任一祖先是否为符号链接（根目录除外）。"""
    current = path
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _validate(raw: str, roots: list[Path]) -> tuple[Path | None, str | None]:
    """校验并返回解析后的绝对路径；不合法时返回 (None, 原因)。"""
    if not raw or not raw.strip():
        return None, "未提供目录路径"
    path = Path(raw.strip())
    if not path.is_absolute():
        return None, "必须使用绝对路径"
    if _has_symlink_component(path):
        return None, "拒绝符号链接路径"
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None, "路径不可解析"
    for root in roots:
        if resolved == root:
            return None, "不能直接选择模型根目录本身"
        if resolved.is_relative_to(root):
            return resolved, None
    return None, "路径不在允许的模型根目录内"


def _read_config(root: Path) -> tuple[dict | None, str | None, str | None]:
    """读 config.json / model_index.json；返回 (config, name, 降级说明)。"""
    for name in _CONFIG_NAMES:
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink():
            return None, None, f"{name} 是符号链接，已跳过"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, ValueError) as e:
            return None, None, f"{name} 解析失败：{e}"
        if not isinstance(loaded, dict):
            return None, None, f"{name} 顶层不是 JSON object"
        return loaded, name, None
    return None, None, None


def _walk_files(root: Path) -> tuple[list[ModelDirEntry], int, str | None]:
    """递归列出普通文件（跳过符号链接），返回 (清单, 总字节, 截断说明)。"""
    entries: list[ModelDirEntry] = []
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            entries.append(ModelDirEntry(path=rel, size=size))
            total += size
            if len(entries) >= _MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    return entries, total, ("文件数超过上限，清单已截断" if truncated else None)


def probe_model_dir_sync(
    path: str,
    *,
    store_dir: str,
    import_roots: str | None = None,
) -> ModelDirProbe:
    """只读探测节点上的模型目录（同步实现，调用方负责跳线程）。"""
    roots = _allowed_roots(store_dir, import_roots)
    resolved, error = _validate(path, roots)
    if resolved is None:
        return ModelDirProbe(exists=False, message=error or "路径不可解析")
    if not resolved.exists():
        return ModelDirProbe(exists=False, message="目录不存在或不可达")
    if not resolved.is_dir():
        return ModelDirProbe(exists=False, is_dir=False, message="请选择模型目录，不支持单文件")

    config, config_name, config_message = _read_config(resolved)
    entries, total, walk_message = _walk_files(resolved)
    # n-gram 嵌入表字节（显存预算扣减项）：只扫 safetensors 文件头；单文件读头失败
    # 计 0，与「目录里本来就没有」同口径——都回到不扣减的保守侧
    ngram_bytes = sum_ngram_table_bytes([resolved / e.path for e in entries if e.path.lower().endswith(".safetensors")])
    message = config_message or walk_message
    return ModelDirProbe(
        exists=True,
        is_dir=True,
        config_name=config_name,
        config=config,
        files=entries,
        total_bytes=total,
        ngram_table_bytes=ngram_bytes,
        message=message,
    )


def _looks_like_model(entries: list[ModelDirEntry]) -> bool:
    """目录内是否存在 config 或权重文件（删除前的护栏）。"""
    for entry in entries:
        name = entry.path.rsplit("/", 1)[-1].lower()
        if name in _CONFIG_NAMES or name.endswith(MODEL_WEIGHT_EXTS):
            return True
    return False


def remove_model_dir_sync(
    path: str,
    *,
    store_dir: str,
    import_roots: str | None = None,
) -> RemoveModelDirResult:
    """受控删除节点上的模型目录（同步实现，调用方负责跳线程）。"""
    roots = _allowed_roots(store_dir, import_roots)
    resolved, error = _validate(path, roots)
    if resolved is None:
        return RemoveModelDirResult(removed=False, path=path, message=error or "路径不可解析")
    if not resolved.exists():
        return RemoveModelDirResult(removed=False, path=str(resolved), message="目录不存在")
    if not resolved.is_dir():
        return RemoveModelDirResult(removed=False, path=str(resolved), message="目标不是目录")

    entries, _total, _message = _walk_files(resolved)
    if not _looks_like_model(entries):
        return RemoveModelDirResult(
            removed=False,
            path=str(resolved),
            message="目录内没有 config 或权重文件，拒绝删除",
        )
    try:
        shutil.rmtree(resolved)
    except OSError as e:
        return RemoveModelDirResult(removed=False, path=str(resolved), message=f"删除失败：{e}")
    return RemoveModelDirResult(removed=True, path=str(resolved))
