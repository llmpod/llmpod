"""Model ORM 模型（模型配方：元数据 + 权重信息 + 下载配方，单文件合一）。"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Model(Base, IntIdMixin, TimestampMixin):
    """模型配方（元数据 + 权重信息 + 下载配方合一，单文件）。

    Model 只表达"模型是什么 + 怎么获取"（配方），**不**承载节点缓存状态——分发状态
    由 ``node_model`` 表表达（部署期关注）。权重溯源字段均可选，创建模型不强制填。

    下载配方 = source + source_url + source_kind + source_subpath + revision + hash
    + hash_mode + source_auth_encrypted。node 拿到即可拉取，与"manager 上有没有"解耦。
    ``source_auth_encrypted`` 为应用层加密的密文，API 永不回显。
    """

    __tablename__ = "model"

    # ---- 元数据 ----
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="模型名称")
    model_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="模型类别（category，字符串非枚举）：llm | embedding | rerank | image | stt | tts",
    )
    architecture: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="模型架构，如 qwen2 / llama3（可空）"
    )
    is_moe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="是否 MoE 架构"
    )
    is_vision: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="是否视觉模型"
    )

    # ---- 权重信息（原 ModelVersion，合并入此；皆可空）----
    size_gb: Mapped[float | None] = mapped_column(Float, nullable=True, comment="权重大小（GB，可空）")
    format: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="格式：safetensors | gguf | ...（可空）"
    )
    quantization: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="量化方案：awq | gptq | none（可空）"
    )
    # 架构形状：KV cache 真公式、逐层 offload、TP 整除校验的输入（探测端从 GGUF 头/config.json 提取；缺失时降级）
    num_layers: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="层数（offload 层数粒度 + KV cache 真公式，可空）"
    )
    num_attention_heads: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="attention head 数（TP 整除校验；vLLM 要求能被 tp 整除，可空）"
    )
    num_kv_heads: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="KV head 数（GQA 下 ≠ attention heads，可空）"
    )
    head_dim: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每 head 维度（可空）")
    max_context: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="模型自带上下文上限（推荐 max_model_len 的钳制上界，可空）"
    )
    needs_trust_remote_code: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="模型带自定义代码（config auto_map）→ 部署需显式加 --trust-remote-code",
    )
    hash: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="内容指纹（local 采样哈希：清单+最大权重分片；重传改权重即变，可空）"
    )

    # ---- 下载配方 ----
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="来源（provenance）：huggingface | modelscope | oss | local（可空）"
    )
    source_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="来源 URL / repo id / 本地路径（可空）"
    )
    source_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment="盘上布局（node 下载策略判据）：unknown | single_gguf | gguf_shards | transformers_folder | diffusers_folder",
    )
    source_subpath: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="多 variant 仓选哪个变体的子路径（可空）"
    )
    mmproj_file: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="视觉投影器文件（repo 相对路径，如 mmproj-F16.gguf）；GGUF 多模态下发/--mmproj 注入用，无=NULL",
    )
    revision: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="来源版本标识/commit-hash（可空，溯源用）"
    )
    hash_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
        comment="hash 字释读方式：none | revision | etag | sha256 | fingerprint（local 采样内容指纹；默认 none）",
    )
    source_auth_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="拉取凭证密文（应用层加密；API 永不回显；按 source 分形态，如 {hf_token}/{access_key_id,...}）",
    )

    __table_args__ = (
        Index("idx_model_name", "name"),
        CheckConstraint(
            "source_kind IN ('unknown', 'single_gguf', 'gguf_shards', 'transformers_folder', 'diffusers_folder')",
            name="ck_model_source_kind",
        ),
        CheckConstraint(
            "hash_mode IN ('none', 'revision', 'etag', 'sha256', 'fingerprint')",
            name="ck_model_hash_mode",
        ),
    )

    def __repr__(self) -> str:
        return f"<Model id={self.id} name={self.name!r}>"
