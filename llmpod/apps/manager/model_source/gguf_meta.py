"""GGUF 文件头 metadata 解析（纯 stdlib，只读头部，不全文件扫描）。

GGUF 是自描述二进制格式：固定 magic ``GGUF`` + 紧贴文件开头的 metadata KV 表。
MoE 的专家数（``llama.expert_count`` / ``expert_count``）写在 metadata 里，**不写在
config.json**——故 GGUF 路径的 MoE 判定只能靠解析文件头，不能靠 config。

设计：
- 只读文件前面的 metadata 区，不扫全文件——即便 70GB 的 GGUF 也只读头部。该区大小随词表
  膨胀（现代大词表模型可达十几 MB），量化类型更是在 KV 表尾部，故分「首段 / 总上限」两档；
- 不引入 ``gguf`` 库依赖，纯 stdlib 按 spec 实现状态机；
- 读不到 / 非 GGUF / 格式损坏 → 返回 ``None``（降级，不抛，调用方兜底）；
- 多分片 GGUF（``-00001-of-NNNN.gguf``）：metadata 只在第 0 片，调用方负责定位第 0 片传入。

GGUF spec（https://github.com/ggerganov/ggml/blob/master/docs/gguf.md）摘要：
    magic           = b'GGUF' (0x46554747 LE)
    version         : uint32
    tensor_count    : uint64
    metadata_kv_count : uint64
    [metadata_kv]   : repeat × metadata_kv_count
        key         : string (uint64 len + utf-8 bytes)
        value_type   : uint32 (enum: 0..11)
        value        : 按 type 读（string/uint32/int32/float32/bool/uint64/int64/...）

本模块只捞 inspect 关心的 key（expert_count / general.architecture / 架构形状 block_count ·
head_count_kv · key_length / 量化类型 general.file_type），其余跳过（按 value_type 读定长
字节后丢弃，不缓存全部 KV）。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

log_module = __name__

# GGUF magic（little-endian uint32 = 0x46554747，即字节 b'GGUF'）
_GGUF_MAGIC = b"GGUF"

# metadata value type 枚举（spec 表）
# 0=UINT8 1=INT8 2=UINT16 3=INT16 4=UINT32 5=INT32 6=FLOAT32 7=BOOL
# 8=STRING 9=ARRAY 10=UINT64 11=INT64 12=FLOAT64
_VT_UINT8, _VT_INT8, _VT_UINT16, _VT_INT16 = 0, 1, 2, 3
_VT_UINT32, _VT_INT32, _VT_FLOAT32, _VT_BOOL = 4, 5, 6, 7
_VT_STRING, _VT_ARRAY, _VT_UINT64, _VT_INT64, _VT_FLOAT64 = 8, 9, 10, 11, 12

# 头部首段读取上限。架构形状 / MoE 键都写在 metadata 前段（实测三者偏移均在 4 MiB 内），
# 这一段就够；只有 ``general.file_type`` 例外——它在 KV 表尾部，见下方 _MAX_HEADER_READ_TOTAL。
_MAX_HEADER_READ = 4 * 1024 * 1024

# 读满首段仍未拿到 general.file_type 时的总读取上限（远程按续段拉，本地一次读）。
# file_type 是转换器最后写入的 KV 之一，位置随词表大小漂移——实测：592 B / 5.9 MB /
# 10.9 MB / 15.8 MB。32 MiB 覆盖已知样本并留一倍余量；超过则放弃（量化退回文件名启发式，
# 即本次改动前的行为），不影响其余字段。**必须 ≥ _MAX_HEADER_READ**（续段长度按两者之差算）。
_MAX_HEADER_READ_TOTAL = 32 * 1024 * 1024

# 监听的 key（小写比对）：
# expert_count 的前缀随架构变（llama.expert_count / phimoe.expert_count / qwen2.expert_count /
# deepseek2.expert_count / 裸 expert_count），按 key 末段（rsplit('.')）匹配，不硬编码前缀。
_K_ARCH = "general.architecture"

# 单条 metadata string（tokenizer 词条、license 全文等）的长度合理性上界：超过即认定长度字段
# 损坏。这是**校验**阈值，与上面那对**读取**阈值不是一回事，别合并——把校验阈值放大到
# 32 MiB 等于接受荒谬长度。
_MAX_STRING_LEN = 4 * 1024 * 1024

# 量化类型是全局 key（不随架构加前缀），按**完整 key** 匹配——不能混进下面按末段匹配的表，
# 否则 ``<arch>.file_type`` 这类同名末段会被误当量化。
_K_FILE_TYPE = "general.file_type"

# ggml/llama_ftype 枚举 → 量化码（小写，与文件名启发式同一词汇表）。
# 来源：llama.cpp ``include/llama.h`` 的 ``llama_ftype`` 枚举（LLAMA_FTYPE_*，约 117-160 行）。
# 已从 gguf 移除、不再出现的编号（4=Q4_1_SOME_F16 / 5=Q4_2 / 6=Q4_3 / 33,34,35=Q4_0_*）
# 与 1024=LLAMA_FTYPE_GUESSED（"文件里没写"）一并缺席——查不到即视为未知，交调用方兜底。
_FILE_TYPE_NAMES: dict[int, str] = {
    0: "f32",
    1: "f16",
    2: "q4_0",
    3: "q4_1",
    7: "q8_0",
    8: "q5_0",
    9: "q5_1",
    10: "q2_k",
    11: "q3_k_s",
    12: "q3_k_m",
    13: "q3_k_l",
    14: "q4_k_s",
    15: "q4_k_m",
    16: "q5_k_s",
    17: "q5_k_m",
    18: "q6_k",
    19: "iq2_xxs",
    20: "iq2_xs",
    21: "q2_k_s",
    22: "iq3_xs",
    23: "iq3_xxs",
    24: "iq1_s",
    25: "iq4_nl",
    26: "iq3_s",
    27: "iq3_m",
    28: "iq2_s",
    29: "iq2_m",
    30: "iq4_xs",
    31: "iq1_m",
    32: "bf16",
    36: "tq1_0",
    37: "tq2_0",
    38: "mxfp4",
    39: "nvfp4",
    40: "q1_0",
    41: "q2_0",
}

# 架构形状 key（同样按末段匹配，前缀随架构变）：末段 → GGUFMetadata 字段。
# embedding_length 只用于推导 head_dim（部分模型不写 key_length）。
_INT_KEYS: dict[str, str] = {
    "block_count": "num_layers",
    "head_count": "num_attention_heads",
    "head_count_kv": "num_kv_heads",
    "key_length": "head_dim",
    "context_length": "max_context",
    "embedding_length": "_embedding_length",
}


@dataclass
class GGUFMetadata:
    """从 GGUF 头解析出的、inspect 关心的字段。None = 该 key 不存在或读失败。"""

    expert_count: int | None = None
    architecture: str | None = None
    # 量化类型（general.file_type 原值）。None = 文件里没写 / 超出读取上限。
    file_type: int | None = None
    # 架构形状：KV cache 真公式 + 逐层 offload + TP 整除校验的输入（缺任一即退启发式/跳过校验）
    num_layers: int | None = None
    num_attention_heads: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None
    max_context: int | None = None

    @property
    def is_moe(self) -> bool:
        """expert_count > 0 即视为 MoE。"""
        return self.expert_count is not None and self.expert_count > 0

    @property
    def quantization(self) -> str | None:
        """量化码（如 ``q4_k_m``）；编号未知 / 已从 gguf 移除 / 文件没写 → None。

        文件头记录的是**主导张量的类型**，即这批权重实际被压成什么，比文件名可靠：
        文件名可能带根本不是量化类型的营销词（如 ``Q5_K_L``——llama.cpp 的 ftype 里没有它），
        而混合量化文件的 file_type 给的是基准类型，与主导张量一致。
        """
        return _FILE_TYPE_NAMES.get(self.file_type)


class _GGUFReader:
    """GGUF 头字节流读取器：定长读，越界抛 EOFError（调用方降级 None）。

    封装一个 buffer + pos，按 spec 读定长小端整数 / 字符串。只在传入的头部 buffer 上操作，
    不会越过初始化时读入的字节——超长 metadata（KV 数声明异常大）会在第一次越界时被 EOFError 拦。
    """

    def __init__(self, buf: bytes):
        self._buf = buf
        self._pos = 0

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._buf):
            raise EOFError("gguf header truncated")
        chunk = self._buf[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._take(8))[0]

    def string(self) -> str:
        """GGUF string：uint64 len + utf-8 bytes。"""
        n = self.u64()
        if n > _MAX_STRING_LEN:  # 荒谬长度 → 损坏
            raise EOFError("gguf string length implausible")
        raw = self._take(n)
        # errors="replace" 保证任意字节都能解出（坏字节替换为 U+FFFD），无需兜底分支
        return raw.decode("utf-8", errors="replace")

    def _scalar(self, vtype: int) -> object:
        """按 value_type 读一个标量值（不处理 ARRAY——见 _skip_value）。"""
        if vtype == _VT_UINT8:
            return struct.unpack("<B", self._take(1))[0]
        if vtype == _VT_INT8:
            return struct.unpack("<b", self._take(1))[0]
        if vtype == _VT_UINT16:
            return struct.unpack("<H", self._take(2))[0]
        if vtype == _VT_INT16:
            return struct.unpack("<h", self._take(2))[0]
        if vtype == _VT_UINT32:
            return struct.unpack("<I", self._take(4))[0]
        if vtype == _VT_INT32:
            return struct.unpack("<i", self._take(4))[0]
        if vtype == _VT_FLOAT32:
            return struct.unpack("<f", self._take(4))[0]
        if vtype == _VT_BOOL:
            return struct.unpack("<?", self._take(1))[0]
        if vtype == _VT_UINT64:
            return struct.unpack("<Q", self._take(8))[0]
        if vtype == _VT_INT64:
            return struct.unpack("<q", self._take(8))[0]
        if vtype == _VT_FLOAT64:
            return struct.unpack("<d", self._take(8))[0]
        if vtype == _VT_STRING:
            return self.string()
        raise ValueError(f"unsupported scalar value_type: {vtype}")

    def skip_value(self, vtype: int) -> None:
        """跳过任意 value（含 ARRAY：先读 element_type + uint64 len + 跳过 N 个元素）。"""
        if vtype == _VT_ARRAY:
            elem_type = self.u32()
            count = self.u64()
            for _ in range(count):
                self.skip_value(elem_type)
            return
        self._scalar(vtype)  # 读出即丢


def parse_gguf_metadata_bytes(buf: bytes) -> GGUFMetadata | None:
    """从字节流解析 GGUF 头 → ``GGUFMetadata``（expert_count / architecture / 架构形状 / 量化类型）。

    - 非 GGUF（magic 不符）/ 头部截断 / KV 损坏 → ``None``（降级，不抛）；
    - 读到的 key 才填，读不到为 None（调用方据 expert_count 判 MoE，None 即「未知」）。
    本函数不做 IO，local 文件读与远程 Range 下载都先拿成 buf 再调它。
    """
    import logging

    log = logging.getLogger(log_module)
    if len(buf) < 8 or not buf.startswith(_GGUF_MAGIC):
        return None  # 非 GGUF

    r = _GGUFReader(buf)
    meta = GGUFMetadata()
    seen: dict[str, int] = {}
    try:
        r._pos = 4  # 跳过 magic
        _version = r.u32()  # 版本不消费，仅推进；超 v3 不阻拦，KV 读法向后兼容
        _tensor_count = r.u64()  # tensor 区在 metadata 之后，不读，仅推进
        kv_count = r.u64()

        for _ in range(kv_count):
            key = r.string()
            vtype = r.u32()
            key_lower = key.lower()

            # expert_count / expert_used_count：裸 key 或 <arch>.<key>（llama/phimoe/qwen2/...），
            # 按「末段」匹配。读出值（数组跳过）；记首个读到的整数值（含 0），is_moe 由属性判 >0。
            # 读标量；数组读不出标量值但流必须推进（skip 恰好一次）
            def _read_scalar() -> object:
                if vtype == _VT_ARRAY:
                    r.skip_value(vtype)
                    return None
                return r._scalar(vtype)

            last_seg = key_lower.rsplit(".", 1)[-1]
            if last_seg in ("expert_count", "expert_used_count"):
                val = _read_scalar()
                if isinstance(val, int) and not isinstance(val, bool) and meta.expert_count is None:
                    meta.expert_count = val
            elif last_seg in _INT_KEYS:
                val = _read_scalar()
                if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    seen.setdefault(_INT_KEYS[last_seg], val)
            elif key_lower == _K_ARCH:
                val = _read_scalar()
                if isinstance(val, str):
                    meta.architecture = val
            elif key_lower == _K_FILE_TYPE:
                # 量化类型：按完整 key 精确匹配（见 _K_FILE_TYPE 注释）。整数标量、首次出现为准。
                val = _read_scalar()
                if isinstance(val, int) and not isinstance(val, bool) and meta.file_type is None:
                    meta.file_type = val
            else:
                r.skip_value(vtype)
    except (EOFError, ValueError, struct.error, RecursionError) as e:
        # 头部截断（metadata 区超出读取上限，常见于巨大 tokenizer 数组）：
        # 不丢弃已拿到的 expert_count/architecture——它们通常在 KV 表前段，先于 tokenizer 写入。
        # 返回部分结果（expert_count 可能已捕获）；只有 magic 头都没对齐时 meta 为空。
        # RecursionError 一并收：skip_value 对嵌套 ARRAY 是递归的，构造出来的深嵌套头能让它爆栈。
        # 本函数的契约是「读不了就降级，不抛」——漏掉它会让调用方（无兜底的 HF/local 路径）500。
        log.warning("gguf header truncated/partial: %s（已拿到的部分仍返回）", e)
        if r._pos <= 4:  # 连 version 都没读到 → 真·损坏，非截断
            return None
    meta.num_layers = seen.get("num_layers")
    meta.num_attention_heads = seen.get("num_attention_heads")
    meta.num_kv_heads = seen.get("num_kv_heads")
    meta.max_context = seen.get("max_context")
    # head_dim：显式 key_length 优先，否则 embedding_length ÷ head_count（不整除即留空）
    head_dim = seen.get("head_dim")
    embedding_length, head_count = seen.get("_embedding_length"), seen.get("num_attention_heads")
    if head_dim is None and embedding_length and head_count and embedding_length % head_count == 0:
        head_dim = embedding_length // head_count
    meta.head_dim = head_dim
    return meta


def parse_gguf_metadata(path: Path) -> GGUFMetadata | None:
    """读本地 GGUF 文件头 → ``GGUFMetadata``。

    - 文件不可读 → ``None``（降级，不抛）；
    - 本地读盘无传输成本，直接按 ``_MAX_HEADER_READ_TOTAL`` 一次读满（量化类型在 KV 表尾部，
      见该常量注释）；只读头部，不全文件扫描。其余见 :func:`parse_gguf_metadata_bytes`。
    """
    import logging

    log = logging.getLogger(log_module)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            buf = f.read(_MAX_HEADER_READ_TOTAL)
    except OSError as e:
        log.warning("gguf meta read failed for %s: %s", path, e)
        return None
    return parse_gguf_metadata_bytes(buf)


__all__ = ["GGUFMetadata", "parse_gguf_metadata", "parse_gguf_metadata_bytes"]
