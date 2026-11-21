"""safetensors 头扫描：按张量名统计字节数。

用途是显存预算纠偏：Qwen4Exp 一族的 PLE n-gram 嵌入表在推理引擎默认配置下驻留
内存（vLLM ``cpu_offload`` 缺省开）或磁盘，不进显存——探测期扫出这部分字节后，
预检/推荐把「总权重 − n-gram 表」作为显存侧权重。

只读每个文件头部的 JSON（张量名与 data_offsets），不触碰数据区；单文件头超上限
或解析失败按 0 计（宁可少扣不少扣，回到全量口径是安全侧）。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

# 张量名含该片段即视为 n-gram 嵌入表（Qwen4Exp：layers.N.ple.ple_embedding.ngram_embedding.*）。
# 模式钉在实现里而不是架构名上：表存在与否由权重文件自己说话，架构字符串的写法
# 随上游版本漂移，张量命名至今稳定。
NGRAM_TABLE_MARK = ".ple.ple_embedding.ngram_embedding."

# 单文件头长度上限：真实大分片的头在数百 KB 量级，超限说明文件形态异常，跳过不扫。
_MAX_HEADER_BYTES = 64 * 1024 * 1024


def ngram_table_bytes(path: Path) -> int:
    """单个 safetensors 文件里 n-gram 嵌入表张量的总字节数。

    字节数取各张量 ``data_offsets`` 的 end−start（数据区内的精确跨度），与 dtype
    无关；非 safetensors / 头读不出 / JSON 解析失败一律返回 0。
    """
    try:
        with open(path, "rb") as f:
            raw_len = f.read(8)
            if len(raw_len) != 8:
                return 0
            header_len = struct.unpack("<Q", raw_len)[0]
            if header_len <= 0 or header_len > _MAX_HEADER_BYTES:
                return 0
            header = json.loads(f.read(header_len))
    except (OSError, ValueError):
        return 0
    if not isinstance(header, dict):
        return 0

    total = 0
    for name, meta in header.items():
        if name == "__metadata__" or not isinstance(meta, dict):
            continue
        if NGRAM_TABLE_MARK not in name:
            continue
        # data_offsets 按规范是 [start, end] 列表；个别工具产出的 dict 形态一并容忍
        offsets = meta.get("data_offsets")
        if isinstance(offsets, dict):
            start, end = offsets.get("start"), offsets.get("end")
        elif isinstance(offsets, (list, tuple)) and len(offsets) == 2:
            start, end = offsets[0], offsets[1]
        else:
            continue
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            total += end - start
    return total


def sum_ngram_table_bytes(paths: list[Path]) -> int:
    """一组 safetensors 文件的 n-gram 嵌入表总字节数（逐文件独立降级，失败计 0）。"""
    return sum(ngram_table_bytes(p) for p in paths)
