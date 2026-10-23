"""模型清单预校验纯函数（上传前提示，无 IO）。

输入一份「声明清单」（文件名 + 大小，文件还不在盘上），按 8 条规则产出
warnings（``fatal`` / ``warn`` / ``info``）。复用 :mod:`inference` 的
``WEIGHT_EXTS`` 与 layout 判定。

被 ``POST /api/v1/admin/models/validate-manifest`` 消费，与上传层解耦：上传层
零模型知识（``/admin/uploads/*`` 只分片+合并，不 inspect），模型语义预检是前端
编排的责任（前端选完文件 → validate-manifest 给提示 → 再 declare 上传）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from llmpod.apps.manager.model_source import inference

# 权重分片命名：model-00001-of-00003.safetensors / model-00001-of-00002.gguf
# 捕获 prefix / part_no / total / ext；不匹配（单文件 model.safetensors）→ None
_SHARD_RE = re.compile(
    r"^(?P<prefix>.+?)-(?P<part>\d+)-of-(?P<total>\d+)\.(?P<ext>\w+)$",
    re.IGNORECASE,
)

# 大文件阈值（字节）：超过此值且扩展名非权重/已知辅助 → 视为可疑权重，触发
# unsupported_ext（避免把 README.md / .gitattributes 等小文本误报）。
_LARGE_FILE_BYTES = 1 << 20  # 1 MiB

# 已知非权重辅助扩展名（config / tokenizer / 文档 / 元数据），不触发 unsupported_ext。
# 权重扩展名见 ``inference.WEIGHT_EXTS``（.safetensors/.gguf/.bin/.pt/.pth/.ckpt/.onnx）。
_KNOWN_AUX_EXTS: frozenset[str] = frozenset(
    {
        ".json",  # config.json / tokenizer_config.json / *.index.json ...
        ".txt",  # merges.txt / vocab.txt ...
        ".md",  # README.md
        ".model",  # sentencepiece
        ".vocab",
        ".spm",
        ".bpe",
        ".merges",
        ".tiktoken",
        ".gitattributes",
        ".bin.index.json",  # pytorch_model.bin.index.json（取末段 .json 已覆盖，这里冗余兜底）
    }
)

SEVERITY_FATAL = "fatal"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


class ManifestFile(BaseModel):
    """声明清单中的一个文件：名 + 大小（字节）。文件还不在盘上。"""

    name: str = Field(..., description="文件名（含扩展名，可含子路径，仅取末段做扩展名判定）")
    size: int = Field(..., description="文件大小（字节）")


class ManifestWarning(BaseModel):
    """一条预校验提示。"""

    code: str = Field(..., description="提示码（见规则表），前端可据此展示/阻断")
    message: str = Field(..., description="人类可读说明，前端直接展示")
    severity: str = Field(..., description="fatal（应阻止继续）| warn（提示可继续）| info")
    file: str | None = Field(default=None, description="关联文件名（无具体文件为 null）")


def _ext(name: str) -> str:
    """取文件名小写扩展名（含点）；末段 basename，防目录穿越成分干扰。

    在原始 base 上找 dot、切片后再 lower——先 lower 再找会在 Unicode 变长字符
    （如 İ→i̇）上错位，且返回原始大小写（MODEL.GGUF → ".GGUF"）会让下方
    == ".gguf" 精确比较与大写辅助文件白名单判定失效。
    """
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot >= 0 else ""


def _parse_shard(name: str) -> re.Match | None:
    """匹配权重分片命名，返回正则 match 或 None。"""
    base = name.rsplit("/", 1)[-1]
    return _SHARD_RE.match(base)


def validate_manifest(
    files: list[ManifestFile],
    max_bytes: int,
) -> list[ManifestWarning]:
    """对一份声明清单做模型语义预校验，返回 warnings（按规则优先级排序）。

    纯函数、零 IO。``max_bytes`` 来自 ``settings.upload_max_bytes``。
    """
    warnings: list[ManifestWarning] = []
    names = [f.name for f in files]
    # 识别类检测取末段 basename 再比较——name 文档明确允许子路径，
    # 完整路径精确比较会让 subdir/config.json 永远漏识别
    basenames = [n.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower() for n in names]
    total_size = sum(f.size for f in files if f.size > 0)
    is_directory = len(files) > 1

    has_config = "config.json" in basenames
    has_model_index = "model_index.json" in basenames

    weight_files = [n for n in names if n.lower().endswith(inference.WEIGHT_EXTS)]

    # 1. oversize（fatal）—— 先判，超上限其余规则意义不大
    if total_size > max_bytes:
        warnings.append(
            ManifestWarning(
                code="oversize",
                message=f"总大小 {total_size} 字节超上传上限 {max_bytes} 字节",
                severity=SEVERITY_FATAL,
            )
        )

    # 2. no_weight_file（fatal）
    if not weight_files:
        warnings.append(
            ManifestWarning(
                code="no_weight_file",
                message="未检测到权重文件（.safetensors/.gguf/.bin/.pt/.pth/.ckpt/.onnx），模型无法被推理引擎加载",
                severity=SEVERITY_FATAL,
            )
        )

    # 3. no_config_or_index（warn）—— 目录形态但缺识别文件
    if is_directory and not has_config and not has_model_index:
        warnings.append(
            ManifestWarning(
                code="no_config_or_index",
                message="缺少 config.json / model_index.json，inspect 可能无法识别架构与类型，建议补充",
                severity=SEVERITY_WARN,
            )
        )

    # 4. safetensors_shard_incomplete（fatal）
    warnings.extend(_check_shard_completeness(names, "safetensors", "safetensors_shard_incomplete", SEVERITY_FATAL))

    # 5. gguf_split_incomplete（warn）
    warnings.extend(_check_shard_completeness(names, "gguf", "gguf_split_incomplete", SEVERITY_WARN))

    # 6. single_gguf_no_config（info）—— 单文件 gguf 通常不需 config
    if not is_directory and len(weight_files) == 1 and _ext(weight_files[0]) == ".gguf":
        warnings.append(
            ManifestWarning(
                code="single_gguf_no_config",
                message="GGUF 单文件通常不需 config.json，可继续",
                severity=SEVERITY_INFO,
                file=weight_files[0],
            )
        )

    # 7. mixed_layouts（warn）—— transformers + diffusers 标记同存
    if has_config and has_model_index:
        warnings.append(
            ManifestWarning(
                code="mixed_layouts",
                message="同时存在 config.json 与 model_index.json（transformers/diffusers 布局）"
                "，inspect 将按优先级识别，请确认",
                severity=SEVERITY_WARN,
            )
        )

    # 8. unsupported_ext（warn）—— 大文件但扩展名非权重/已知辅助
    warnings.extend(_check_unsupported_ext(files))

    return warnings


def _check_shard_completeness(
    names: list[str],
    ext: str,
    code: str,
    severity: str,
) -> list[ManifestWarning]:
    """检查 ``.<ext>`` 分片是否齐全：按 ``(前缀, -of-YYYY- 声明的 total)`` 分组，缺片则提示。

    仅 ``-NNN-of-YYYY.<ext>`` 命名的分片参与（确定 total 才能判缺）。单文件
    ``model.<ext>`` 不匹配，不参与。``code`` 由调用方传（safetensors 用 shard_incomplete，
    gguf 用 split_incomplete），避免本函数特判扩展名。
    """
    groups: dict[tuple[str, int], list[re.Match]] = {}  # (prefix, total) → [match, ...]
    for n in names:
        m = _parse_shard(n)
        if m is None or m.group("ext").lower() != ext:
            continue
        total = int(m.group("total"))
        groups.setdefault((m.group("prefix"), total), []).append(m)

    out: list[ManifestWarning] = []
    for (prefix, total), matches in groups.items():
        # 只认 1..total 的合法编号：part=0 / part>total 的异常声明虚增集合大小，
        # 会把真实缺片掩盖成「齐全」
        parts = {int(m.group("part")) for m in matches if 1 <= int(m.group("part")) <= total}
        if len(parts) >= total:
            continue
        missing = sorted(set(range(1, total + 1)) - parts)
        # 用第一个 match 作模板，按其位宽重建缺失文件名（组键已含 prefix，
        # 同组内所有 match 同前缀，不会拼出别的模型的文件名）
        template = matches[0]
        part_str = template.group("part")
        total_str = template.group("total")
        missing_names = [f"{prefix}-{str(p).zfill(len(part_str))}-of-{total_str}.{ext}" for p in missing]
        out.append(
            ManifestWarning(
                code=code,
                message=f"{ext} 分片不完整（声明 {len(parts)}/{total}），缺 {', '.join(missing_names)}",
                severity=severity,
            )
        )
    return out


def _check_unsupported_ext(files: list[ManifestFile]) -> list[ManifestWarning]:
    """大文件但扩展名非权重/已知辅助 → unsupported_ext（warn）。

    只对「够大、疑似权重 blob」的文件提示，避免把 README.md 等小文本误报。
    同一扩展名只提示一次，``file`` 取该扩展名的首个示例。
    """
    seen: dict[str, str] = {}  # ext → 示例文件名
    for f in files:
        if f.size < _LARGE_FILE_BYTES:
            continue
        ext = _ext(f.name)
        if not ext:
            continue
        if ext in inference.WEIGHT_EXTS or ext in _KNOWN_AUX_EXTS:
            continue
        seen.setdefault(ext, f.name)

    return [
        ManifestWarning(
            code="unsupported_ext",
            message=f"存在非标准权重扩展名 {ext}，可能无法被识别",
            severity=SEVERITY_WARN,
            file=example,
        )
        for ext, example in seen.items()
    ]
