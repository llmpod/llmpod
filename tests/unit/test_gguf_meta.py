"""GGUF 文件头 metadata 解析单测（纯字节流，无真实 GGUF 文件 / 无网络）。

手搓最小 GGUF 字节流（magic + version + tensor_count=0 + kv_count + 几个 KV），
验证：
- 能解析出 expert_count / general.architecture；
- 非 GGUF（magic 不符）/ 截断 → None（降级不抛）；
- 无监听 key → GGUFMetadata 全 None / is_moe=False。
"""

from __future__ import annotations

import struct
from pathlib import Path

from llmpod.apps.manager.model_source.gguf_meta import (
    GGUFMetadata,
    parse_gguf_metadata,
    parse_gguf_metadata_bytes,
)

# value_type 常量（与 gguf_meta 内部一致）
VT_UINT32 = 4
VT_STRING = 8
VT_UINT64 = 10


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _gguf_str(s: str) -> bytes:
    """GGUF string：uint64 len + utf-8 bytes。"""
    raw = s.encode("utf-8")
    return _u64(len(raw)) + raw


def _kv(key: str, vtype: int, value_bytes: bytes) -> bytes:
    """一个 metadata KV：string key + uint32 value_type + value bytes。"""
    return _gguf_str(key) + _u32(vtype) + value_bytes


def _build_gguf(kvs: list[bytes]) -> bytes:
    """拼一个最小 GGUF：magic + version=3 + tensor_count=0 + kv_count + KVs。"""
    out = b"GGUF"
    out += _u32(3)  # version
    out += _u64(0)  # tensor_count
    out += _u64(len(kvs))
    for kv in kvs:
        out += kv
    return out


class TestParseGGUFMetadata:
    def test_parse_expert_count_and_arch(self, tmp_path: Path):
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("general.architecture", VT_STRING, _gguf_str("llama")),
                _kv("llama.expert_count", VT_UINT32, _u32(8)),
                _kv("general.name", VT_STRING, _gguf_str("test-model")),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.expert_count == 8
        assert meta.architecture == "llama"
        assert meta.is_moe is True

    def test_non_string_arch_scalar_does_not_desync(self, tmp_path: Path):
        """`general.architecture` 是非字符串标量时，不能多消费一个值。

        回归：该分支曾写成「读出来不是 str 就再 skip 一次」，而非字符串标量在
        `_scalar` 里已经把值读掉了——多读的 4 字节落进下一个 key 的长度字段，
        之后所有键（含 general.file_type / block_count）全部丢失，而且因为解析器
        契约是「降级不抛」，拿到的是**部分结果**，调用方看不出少了东西。
        """
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("general.architecture", VT_UINT32, _u32(7)),  # 非字符串（外来转换器会这么写）
                _kv("general.file_type", VT_UINT32, _u32(15)),
                _kv("llama.expert_count", VT_UINT32, _u32(8)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.architecture is None  # 非字符串 → 不采纳，但也不该吃掉后面的键
        assert meta.file_type == 15
        assert meta.expert_count == 8

    def test_zero_experts_is_not_moe(self, tmp_path: Path):
        gguf = tmp_path / "model.gguf"
        body = _build_gguf([_kv("llama.expert_count", VT_UINT32, _u32(0))])
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.expert_count == 0
        assert meta.is_moe is False  # 0 专家 → 非 MoE

    def test_no_watched_keys_returns_none_fields(self, tmp_path: Path):
        # 合法 GGUF 但没有 expert_count / architecture → 两字段 None，is_moe=False
        gguf = tmp_path / "model.gguf"
        body = _build_gguf([_kv("general.name", VT_STRING, _gguf_str("test"))])
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.expert_count is None
        assert meta.architecture is None
        assert meta.is_moe is False

    def test_non_gguf_returns_none(self, tmp_path: Path):
        # 非 GGUF magic → None
        not_gguf = tmp_path / "model.bin"
        not_gguf.write_bytes(b"\x00" * 128 + b"NOTGGUF" + b"\x00" * 64)

        assert parse_gguf_metadata(not_gguf) is None

    def test_truncated_mid_table_returns_partial(self, tmp_path: Path):
        # KV 表声明 3 个但只够 1 个 → 截断在尾部；已读到的 expert_count 仍返回
        # （现实里巨大 tokenizer 数组超读上限时同理：不因尾部截断丢弃前段的 expert_count）
        gguf = tmp_path / "model.gguf"
        body = b"GGUF" + _u32(3) + _u64(0) + _u64(3) + _kv("llama.expert_count", VT_UINT32, _u32(4))
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None  # 截断不丢前段
        assert meta.expert_count == 4
        assert meta.is_moe is True

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert parse_gguf_metadata(tmp_path / "nope.gguf") is None

    def test_uint64_expert_count(self, tmp_path: Path):
        # expert_count 用 UINT64 类型写（部分转换器）→ 也能读
        gguf = tmp_path / "model.gguf"
        body = _build_gguf([_kv("expert_count", VT_UINT64, _u64(64))])
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.expert_count == 64
        assert meta.is_moe is True

    def test_arch_prefixed_expert_count(self, tmp_path: Path):
        # 不同架构用 <arch>.expert_count（llama / phimoe / qwen2 / deepseek2 ...），按后缀匹配
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("general.architecture", VT_STRING, _gguf_str("phimoe")),
                _kv("phimoe.expert_used_count", VT_UINT32, _u32(2)),
                _kv("phimoe.expert_count", VT_UINT32, _u32(16)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.architecture == "phimoe"
        assert meta.expert_count is not None and meta.expert_count > 0
        assert meta.is_moe is True

    def test_metadata_dataclass_defaults(self):
        m = GGUFMetadata()
        assert m.expert_count is None
        assert m.architecture is None
        assert m.file_type is None and m.quantization is None
        assert m.is_moe is False
        assert m.num_layers is None and m.num_kv_heads is None and m.head_dim is None

    def test_parse_arch_shape(self, tmp_path: Path):
        """架构形状四键：block_count / attention.head_count / head_count_kv / key_length。"""
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("general.architecture", VT_STRING, _gguf_str("qwen2")),
                _kv("qwen2.block_count", VT_UINT32, _u32(28)),
                _kv("qwen2.attention.head_count", VT_UINT32, _u32(28)),
                _kv("qwen2.attention.head_count_kv", VT_UINT32, _u32(4)),
                _kv("qwen2.attention.key_length", VT_UINT32, _u32(128)),
                _kv("qwen2.embedding_length", VT_UINT32, _u32(3584)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert (meta.num_layers, meta.num_attention_heads, meta.num_kv_heads, meta.head_dim) == (28, 28, 4, 128)

    def test_parse_max_context(self, tmp_path: Path):
        """上下文上限：llama.context_length → max_context。"""
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("llama.block_count", VT_UINT32, _u32(32)),
                _kv("llama.context_length", VT_UINT32, _u32(4096)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None and meta.max_context == 4096

    def test_head_dim_derived_from_embedding_over_heads(self, tmp_path: Path):
        """缺 key_length → embedding_length ÷ head_count 推导。"""
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("llama.block_count", VT_UINT32, _u32(32)),
                _kv("llama.attention.head_count", VT_UINT32, _u32(32)),
                _kv("llama.attention.head_count_kv", VT_UINT32, _u32(8)),
                _kv("llama.embedding_length", VT_UINT32, _u32(4096)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.head_dim == 128
        assert meta.num_layers == 32 and meta.num_attention_heads == 32 and meta.num_kv_heads == 8

    def test_head_dim_not_divisible_stays_none(self, tmp_path: Path):
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("llama.attention.head_count", VT_UINT32, _u32(30)),
                _kv("llama.embedding_length", VT_UINT32, _u32(4097)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.head_dim is None

    def test_zero_and_array_values_ignored(self, tmp_path: Path):
        """0 与非整数值不采纳（head_count_kv=0 表示未设置，不能当有效 KV head 数）。"""
        gguf = tmp_path / "model.gguf"
        body = _build_gguf(
            [
                _kv("llama.block_count", VT_UINT32, _u32(0)),
                _kv("llama.attention.head_count_kv", VT_UINT32, _u32(0)),
            ]
        )
        gguf.write_bytes(body)

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.num_layers is None and meta.num_kv_heads is None


class TestParseFileType:
    """量化类型取 general.file_type：编号 → 量化码，认不出的编号 → None（调用方兜底）。"""

    def test_file_type_maps_to_quant_code(self, tmp_path: Path):
        gguf = tmp_path / "model.gguf"
        gguf.write_bytes(
            _build_gguf(
                [
                    _kv("general.architecture", VT_STRING, _gguf_str("llama")),
                    _kv("general.file_type", VT_UINT32, _u32(15)),
                ]
            )
        )

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.file_type == 15
        assert meta.quantization == "q4_k_m"

    def test_unrecognized_file_type_stays_none(self, tmp_path: Path):
        """33（已从 gguf 移除的 Q4_0_4_4）/ 1024（GUESSED = 文件里没写）/ 未知值 → None。

        取值不同、语义一样：这编号描述不了这批权重的量化，交调用方退回文件名启发式。
        """
        for raw in (33, 1024, 9999):
            gguf = tmp_path / f"model-{raw}.gguf"
            gguf.write_bytes(_build_gguf([_kv("general.file_type", VT_UINT32, _u32(raw))]))

            meta = parse_gguf_metadata(gguf)

            assert meta is not None and meta.file_type == raw
            assert meta.quantization is None

    def test_absent_file_type_stays_none(self, tmp_path: Path):
        gguf = tmp_path / "model.gguf"
        gguf.write_bytes(_build_gguf([_kv("general.architecture", VT_STRING, _gguf_str("llama"))]))

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.file_type is None and meta.quantization is None

    def test_arch_scoped_file_type_is_not_the_quant(self, tmp_path: Path):
        """量化是全局 key，按完整 key 匹配——``<arch>.file_type`` 不能按末段串进来。"""
        gguf = tmp_path / "model.gguf"
        gguf.write_bytes(_build_gguf([_kv("llama.file_type", VT_UINT32, _u32(15))]))

        meta = parse_gguf_metadata(gguf)

        assert meta is not None
        assert meta.file_type is None

    def test_deeply_nested_array_degrades_instead_of_raising(self):
        """深嵌套 ARRAY 让 skip_value 爆栈时，也走「降级不抛」的契约。

        本模块对外的承诺是「读不了就返回 None / 部分结果」——漏掉 RecursionError
        会让调用方（HF 与 local 路径都没有兜底）把 500 抛给管理员。
        """
        VT_ARRAY = 9
        body = b"GGUF" + _u32(3) + _u64(0) + _u64(1)
        body += _gguf_str("general.x") + _u32(VT_ARRAY)
        for _ in range(1500):  # 深度足够超过默认递归上限
            body += _u32(VT_ARRAY) + _u64(1)
        body += _u32(0) + b"\x00"

        meta = parse_gguf_metadata_bytes(body)  # 不抛

        assert meta is not None and meta.file_type is None
