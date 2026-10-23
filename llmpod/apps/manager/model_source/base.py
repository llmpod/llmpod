"""模型源识别框架基础：识别结果 schema + Inspector 抽象基类。

设计（模型 type 声明与端点适配）：
- ``ModelFiles`` 是 ``POST /admin/models/inspect`` 的响应体——**纯文件结构 + 原样身份标记 +
  type 建议**。type 建议来自 HF ``pipeline_tag``（映射不了 / 非 HF 源为 None，用户选）。
- 探测只做**提取**，不判语义类别、不判引擎兼容（那是部署时引擎的事）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field


class ModelFiles(BaseModel):
    """模型源识别结果：文件 + 原样身份标记 + type 建议。"""

    model_config = ConfigDict(extra="ignore")

    # —— 探测状态（inspect 响应用；失败也返回已拿到的部分）——
    success: bool = Field(
        default=True,
        description="探测是否成功：False=严重失败（网络不通/仓库不存在/size+format 全缺/多变体 GGUF 无法判定），前端应禁添加；True=可用（个别字段缺可人工补，见 message）",
    )
    message: str | None = Field(default=None, description="失败原因 / 警告，前端直接展示")

    # —— 文件结构事实——
    structure: str | None = Field(default=None, description="结构形态（展示用）：single_file | directory")
    source_kind: str | None = Field(
        default=None,
        description="盘上布局（node 下载/校验判据）：single_gguf|gguf_shards|transformers_folder|diffusers_folder|unknown；None=未推导（落库缺省 unknown）",
    )
    source_subpath: str | None = Field(
        default=None,
        description="源仓库内子路径（GGUF 多变体 repo 的变体目录/文件；空=None 全量）。落 Model.source_subpath，"
        "下发供 node allow_patterns 收窄 + verify 定位（hf_hub_download 保留 repo 相对结构）",
    )
    format: str | None = Field(default=None, description="权重格式：safetensors|gguf|pytorch|onnx|custom")
    layout: str | None = Field(
        default=None, description="布局：transformers|diffusers|gguf|onnx|custom_config|custom_script|..."
    )
    has_config: bool = Field(default=False, description="是否有可读的 config")
    weight_files: list[str] | None = Field(default=None, description="权重文件名清单（回显参考，不落库）")

    # —— 身份标记（原样提取，不做语义判断）——
    architecture: str | None = Field(
        default=None,
        description="身份标识原样字符串：config.architectures[0] / model_type / 自定义 config format / diffusers _class_name",
    )
    pipeline_class: str | None = Field(default=None, description="diffusers model_index._class_name（如 FluxPipeline）")
    pipeline_tag: str | None = Field(default=None, description="HF 元数据 task 标签（仅 HF 源）")
    is_moe: bool = Field(default=False, description="是否 MoE（config num_experts 字段）")
    is_vision: bool = Field(default=False, description="是否视觉/多模态：GGUF 探仓库是否含 mmproj 视觉投影器文件")
    mmproj_file: str | None = Field(
        default=None,
        description="视觉投影器文件（repo 相对路径，如 mmproj-F16.gguf）；GGUF 多模态部署下发/注入用，无=None",
    )
    quantization: str | None = Field(default=None, description="量化方案：awq|gptq|q4_k_m|none|...")
    size_gb: float | None = Field(default=None, description="权重大小（GB）")
    revision: str | None = Field(default=None, description="来源版本/commit-hash")

    # —— 架构形状（KV cache 真公式 + 逐层 offload + TP 整除校验的输入；提取不到留 None）——
    num_layers: int | None = Field(default=None, description="层数（GGUF block_count / config num_hidden_layers）")
    num_attention_heads: int | None = Field(
        default=None, description="attention head 数（GGUF head_count / config num_attention_heads）；TP 整除校验用"
    )
    num_kv_heads: int | None = Field(
        default=None,
        description="KV head 数（GGUF head_count_kv / config num_key_value_heads，GQA 下 ≠ attention heads）",
    )
    head_dim: int | None = Field(
        default=None, description="每 head 维度（GGUF key_length / config head_dim，缺则 hidden_size÷heads）"
    )
    max_context: int | None = Field(
        default=None,
        description="模型自带上下文上限（GGUF context_length / config max_position_embeddings 等取最小）；钳制 max_model_len 用",
    )
    needs_trust_remote_code: bool = Field(
        default=False, description="模型带自定义代码（config auto_map）→ 部署需显式 --trust-remote-code"
    )

    # —— type 建议（来自 pipeline_tag；None = 用户选）——
    suggested_type: str | None = Field(
        default=None,
        description="建议 model_type（HF pipeline_tag → llm/embedding/rerank/multimodal/...；None=用户选）",
    )


class ModelInspector(ABC):
    """模型源探测器抽象。按 ``source`` 分流实现（HF / OSS / local）。"""

    @abstractmethod
    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        """探测模型源（**不下载** 权重），返回识别结果。

        - 成功：``success=True`` + 文件事实/身份标记/suggested_type；
        - 部分成功：返回已能拿到的字段 + ``message`` 警告；
        - 探测失败：返回 ``success=False`` + ``message``，**不抛异常**（软失败，前端可人工填）。
        """
