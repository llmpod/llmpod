"""领域枚举常量。

用 ``StrEnum``（py3.12 原生）：值即字符串，可直接当 dict key / JSON 序列化 / ORM 列值，
同时给类型注解 + 拼写期可查（typo → AttributeError，非静默）。

- ``ModelType``：模型类别（= ``Model.model_type`` 列），驱动端点协议族。**全局统一**。
- ``ModelSource``：模型来源（= ``Model.source`` 列 / inspect source 入参）。

注：引擎 task 词汇（vLLM ``classify`` vs SGLang ``rerank``）**不设枚举**——它是每个引擎的
本地词汇，非跨引擎统一；``EngineAdapter.type_to_task`` 的 value 用字符串字面量诚实表达"引擎自己的词"。
"""

from __future__ import annotations

from enum import StrEnum


class ModelType(StrEnum):
    """模型类别（category = Model.model_type）。驱动 API 网关端点协议 + 引擎 task。"""

    LLM = "llm"
    MULTIMODAL = "multimodal"  # 多模态
    EMBEDDING = "embedding"
    RERANK = "rerank"
    IMAGE = "image"  # 文生图（diffusion / vLLM-Omni）
    STT = "stt"  # 语音转文字
    TTS = "tts"  # 文字转语音

    @classmethod
    def is_vision(cls, model_type: str | None) -> bool:
        """是否视觉类模型（image / multimodal）。未知/空 → False。

        视觉类别的判定集中在枚举（唯一定义处），避免路由层散落魔法字符串集合
        （新增视觉类别只改此处）。
        """
        return model_type in (cls.IMAGE.value, cls.MULTIMODAL.value)

    @classmethod
    def has_kv_cache(cls, model_type: str | None) -> bool:
        """该类别模型推理是否产生自回归 KV cache。未知/空 → True（保守按有估）。

        embedding（池化向量）/ rerank（交叉编码器打分）是一次前向、非自回归，
        无 KV cache——显存估算只算权重，context 长度不抬高需求。判定集中在
        枚举（唯一定义处）：将来若某类别引擎行为分化（如 rerank 出独立参数），
        在此扩展类别级分支即可，引擎侧不感知。
        """
        return model_type not in (cls.EMBEDDING.value, cls.RERANK.value)

    @classmethod
    def is_generative(cls, model_type: str | None) -> bool:
        """是否对话生成类模型（llm / multimodal）。未知/空 → False。

        生成类才有 chat 面（openai /v1/chat/completions 与 anthropic /v1/messages
        都挂在引擎的 chat serving 上）；embedding/rerank 等池化服务只有各自的
        openai 端点。协议面裁剪（engine.protocols_for）与 path 映射
        （category.CATEGORY_PATHS / ANTHROPIC_MESSAGE_PATH）共用本判定。
        """
        return model_type in (cls.LLM.value, cls.MULTIMODAL.value)


class ModelSource(StrEnum):
    """模型来源（Model.source 列 / inspect source 入参）。

    ``HF`` 是 ``HUGGINGFACE`` 的短写别名（source 字段历史容忍值）。本地/上传走 Manager
    文件服务下发的统一用 ``LOCAL``（权重在 manager 盘，经文件服务分发）。
    """

    HUGGINGFACE = "huggingface"
    HF = "hf"  # HUGGINGFACE 别名
    OSS = "oss"
    LOCAL = "local"

    @classmethod
    def is_manager_source(cls, source: str | None) -> bool:
        """是否走 Manager 文件服务下发（本地权重在 manager 盘）。"""
        return (source or "").lower() == cls.LOCAL

    @classmethod
    def is_hf_source(cls, source: str | None) -> bool:
        """是否走 HF 下载（huggingface/hf 别名）。"""
        return (source or "").lower() in (cls.HUGGINGFACE, cls.HF)


class ModelTag(StrEnum):
    """模型能力标签（``Route.tags`` 的受控词表；纯展示，系统不处理）。

    全局统一词表：管理端/前端只从本枚举取值，避免「思考 / 支持思考」「工具 / 工具调用」
    这类近义词散落（用户手填会五花八门）。新增标签 = 在此加成员，唯一定义处。
    """

    THINKING = "思考"  # 思考/推理
    TOOL_CALL = "工具调用"  # 函数调用/工具
    VISION = "视觉"  # 图像/视觉输入
    IMAGE_OUTPUT = "图像生成"  # 图像输出（多模态生成）
    WEB_SEARCH = "联网搜索"  # 联网检索

    @classmethod
    def values(cls) -> list[str]:
        """全部合法标签值（声明顺序，供校验与前端渲染统一取值）。"""
        return [m.value for m in cls]


class AffinityFailoverPolicy(StrEnum):
    """会话亲和的换端策略（= ``settings.affinity_failover_policy``）。

    亲和把会话绑定到某个上游，为的是复用它的前缀缓存；那个上游转发失败时，是保缓存
    还是保可用，由本策略决定：

    - ``REBIND``（默认）：可用性优先。照常跨端点换端重试，成功后把绑定改绑到真正
      服务成功的那个——这一次的缓存断了，后续轮次重新回到一致；
    - ``STRICT``：缓存优先。亲和命中的请求不换端，失败直接返回；绑定端点被熔断摘出
      后同样不甩到别的上游。

    取值写错时按默认 ``REBIND`` 处理（启动期告警，不拦启动）。settings 侧因此保持
    ``str`` 而不收紧为枚举类型：这里容错优先于拼写期校验。
    """

    REBIND = "rebind"
    STRICT = "strict"

    @classmethod
    def values(cls) -> list[str]:
        """全部合法取值（声明顺序，供启动期校验与对账单测统一取值）。"""
        return [m.value for m in cls]
