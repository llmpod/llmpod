"""safetensors 头扫描单测：n-gram 嵌入表（Qwen4Exp PLE 表）字节数统计。

真实分片的头在数百 KB 量级、张量名含 ``.ple.ple_embedding.ngram_embedding.``；
测试用手工构造的最小 safetensors 头驱动（8 字节 LE 头长 + JSON，不写数据区）。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from llmpod.utils.safetensors_meta import ngram_table_bytes, sum_ngram_table_bytes


def _write_safetensors(path: Path, tensors: dict, offsets_form: str = "list") -> None:
    """构造最小 safetensors：只有头（张量名 → data_offsets），数据区不存在。"""

    def offsets(start: int, end: int):
        if offsets_form == "list":
            return [start, end]
        return {"start": start, "end": end}

    header = {"__metadata__": {"format": "pt"}}
    for name, (start, end) in tensors.items():
        header[name] = {"dtype": "BF16", "shape": [1], "data_offsets": offsets(start, end)}
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw)


class TestNgramTableBytes:
    def test_counts_only_ngram_tensors(self, tmp_path):
        f = tmp_path / "a.safetensors"
        _write_safetensors(
            f,
            {
                "model.layers.0.ple.ple_embedding.ngram_embedding.shard_0.weight": (0, 800_000_384),
                "model.layers.0.ple.ple_embedding.ngram_embedding.shard_1.weight": (800_000_384, 1_600_000_768),
                "model.layers.0.ple.key_proj.weight": (1_600_000_768, 1_610_000_768),  # PLE 投影：常驻显存不计
                "model.layers.0.self_attn.q_proj.weight": (1_610_000_768, 1_620_000_768),
            },
        )
        assert ngram_table_bytes(f) == 1_600_000_768

    def test_dict_offsets_form_tolerated(self, tmp_path):
        """个别工具产出 dict 形态的 data_offsets，一并容忍。"""
        f = tmp_path / "a.safetensors"
        _write_safetensors(
            f,
            {"model.layers.1.ple.ple_embedding.ngram_embedding.weight": (0, 1024)},
            offsets_form="dict",
        )
        assert ngram_table_bytes(f) == 1024

    def test_sum_across_files(self, tmp_path):
        _write_safetensors(tmp_path / "a.safetensors", {"x.ple.ple_embedding.ngram_embedding.w": (0, 512)})
        _write_safetensors(tmp_path / "b.safetensors", {"y.ple.ple_embedding.ngram_embedding.w": (0, 100)})
        _write_safetensors(tmp_path / "c.safetensors", {"z.self_attn.w": (0, 4096)})
        assert sum_ngram_table_bytes(sorted(tmp_path.glob("*.safetensors"))) == 612

    def test_degrades_to_zero_on_bad_files(self, tmp_path):
        """非 safetensors / 截断 / 头超长 / 顶层非 dict：一律 0（保守回全量权重口径）。"""
        (tmp_path / "garbage.safetensors").write_bytes(b"not a safetensors file")
        (tmp_path / "truncated.safetensors").write_bytes(struct.pack("<Q", 10**9) + b"{}")
        assert ngram_table_bytes(tmp_path / "garbage.safetensors") == 0
        assert ngram_table_bytes(tmp_path / "truncated.safetensors") == 0
        assert ngram_table_bytes(tmp_path / "missing.safetensors") == 0

    def test_no_ngram_tensors_returns_zero(self, tmp_path):
        f = tmp_path / "plain.safetensors"
        _write_safetensors(f, {"model.layers.0.mlp.up_proj.weight": (0, 65536)})
        assert ngram_table_bytes(f) == 0
