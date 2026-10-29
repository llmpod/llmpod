"""模型源识别的纯函数：**提取** + type 声明。

设计：
- type 不靠 config 语义猜测（无识别标准）——``model_type`` 由用户声明 + HF ``pipeline_tag`` 建议 +
  引擎部署时确认三层决定。本模块只做两件事：
  1. **提取**：从文件清单 / config 字面值提取 format/size/quant/moe/架构原样字符串（可靠）；
  2. **type 建议**：HF ``pipeline_tag`` → ``model_type`` 的稳定映射表（HF task 词汇表，不随模型涨）。
- 被 HF/OSS/local 探测器共用，避免知识漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 视为权重文件的扩展名（统计大小 / 判定格式用）
WEIGHT_EXTS: tuple[str, ...] = (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt", ".onnx")


# ============ type 建议（HF pipeline_tag → model_type） ============
# HF 的 task 词汇表有限且稳定；映射不了 / 无 tag → None（用户选）。
# 来源：HF top-600 下载模型实测 pipeline_tag 采样。
TASK_TO_TYPE: dict[str, str] = {
    # llm
    "text-generation": "llm",
    "text2text-generation": "llm",
    # multimodal
    "image-text-to-text": "multimodal",
    "image-to-text": "multimodal",
    "visual-question-answering": "multimodal",
    "document-question-answering": "multimodal",
    "any-to-any": "multimodal",
    "audio-text-to-text": "multimodal",
    # embedding
    "feature-extraction": "embedding",
    "sentence-similarity": "embedding",
    "image-feature-extraction": "embedding",
    # rerank
    "text-ranking": "rerank",
    # stt
    "automatic-speech-recognition": "stt",
    # tts
    "text-to-speech": "tts",
    "text-to-audio": "tts",
    # image
    "text-to-image": "image",
    "image-to-image": "image",
}


def type_from_pipeline_tag(tag: str | None) -> str | None:
    """HF pipeline_tag → 我们 model_type（建议值）；映射不了 / 空 → None（用户选）。"""
    return TASK_TO_TYPE.get((tag or "").strip().lower())


# ============ 提取 ============


def infer_format(filenames: list[str]) -> str | None:
    """按权重文件扩展名推断格式。优先级 safetensors > gguf > pytorch > onnx。"""
    names = [n.lower() for n in filenames]
    if any(n.endswith(".safetensors") for n in names):
        return "safetensors"
    if any(n.endswith(".gguf") for n in names):
        return "gguf"
    if any(n.endswith((".bin", ".pt", ".pth", ".ckpt")) for n in names):
        return "pytorch"
    if any(n.endswith(".onnx") for n in names):
        return "onnx"
    return None


def infer_quant(config: dict, filenames: list[str], gguf_meta=None) -> str | None:
    """量化方案：``config['quantization_config']`` → GGUF 文件头 → 文件名启发式。

    ``gguf_meta`` 是鸭子类型的 ``gguf_meta.GGUFMetadata | None``（同 :func:`arch_shape`)——
    GGUF 没有 config，文件头记的是转换器写下的**基准量化类型**，与占比最大的那批张量一致，
    比文件名可靠：文件名可能带根本不是量化类型的营销词（如 ``Q5_K_L``，llama.cpp 的 ftype
    里没有它，而该文件实测主导张量是 Q4_K）。文件头没写 / 超出读取上限 / 编号未知
    （``quantization`` 为 None）时才退回文件名。
    """
    qc = config.get("quantization_config") or {}
    if isinstance(qc, dict):
        method = qc.get("quant_method")
        if isinstance(method, str) and method:
            return method.lower()
    header_quant = getattr(gguf_meta, "quantization", None)
    if header_quant:
        return header_quant
    # 只看权重文件：tokenizer.json / vocab.json 之类也会带 f16/f32 字样，混进来会盖过真正的
    # 权重名；mmproj 是视觉投影器（另一份权重，精度常写作 F16/F32），同样不是主模型的量化。
    weight_names: list[str] = []
    for name in filenames:
        if not name.lower().endswith(WEIGHT_EXTS):
            continue
        if is_mmproj(name):
            continue
        weight_names.append(name)
    blob = " ".join(weight_names).lower()
    m = re.search(r"\b(awq|gptq|exl2|aqlm|bitsandbytes|bnb)\b", blob)
    if m:
        token = m.group(1)
        return "bitsandbytes" if token == "bnb" else token
    # gguf 量化码（Q4_K_M / Q8_0 / IQ3_XS ...）。两条约束：
    # - bf16 必须与 f16 并列：否则 "bf16" 里的子串 "f16" 会先被扫到，把 bf16 报成 f16；
    # - 两侧用「非字母数字」定界而非 \b：\b 不认下划线，``model_q8_0.gguf`` 会漏；
    #   而只认 \b 又会放过散列名里的巧合子串（``model-0af164d3.gguf`` 被判成 f16）。
    m = re.search(r"(?<![a-z0-9])(i?q\d+(?:_[a-z0-9]+)+|bf16|f16|f32)(?![a-z0-9])", blob)
    if m:
        return m.group(1).lower()
    return None


def infer_moe(config: dict) -> bool:
    """是否 MoE：top-level 或 text_config/model_config 含专家数字段。"""
    keys = ("num_experts", "num_local_experts", "num_experts_per_tok")
    if any(k in config for k in keys):
        return True
    for sub in (config.get("text_config"), config.get("model_config")):
        if isinstance(sub, dict) and any(k in sub for k in keys):
            return True
    return False


# LLM 参数在子 dict 里的复合 config（多模态/omni 模型）
_LLM_SUBCONFIGS: tuple[str, ...] = ("text_config", "llm_config", "language_config", "model_config")


def _llm_configs(config: dict) -> list[dict]:
    """顶层 + LLM 子 config（多模态把语言模型参数放子 dict，顶层只有视觉/音频配置）。"""
    out = [config]
    for key in _LLM_SUBCONFIGS:
        sub = config.get(key)
        if isinstance(sub, dict):
            out.append(sub)
    return out


def _first_positive_int(configs: list[dict], keys: tuple[str, ...]) -> int | None:
    """按 config 顺序、键顺序取首个正整数；都没有返回 None。"""
    for cfg in configs:
        for key in keys:
            value = cfg.get(key)
            if isinstance(value, bool):  # bool 是 int 子类，显式排除
                continue
            if isinstance(value, int) and value > 0:
                return value
    return None


def infer_num_layers(config: dict) -> int | None:
    """层数（``num_hidden_layers``；部分模型用 ``n_layer``/``num_layers``）。"""
    return _first_positive_int(_llm_configs(config), ("num_hidden_layers", "n_layer", "num_layers"))


def infer_kv_layers(config: dict) -> int | None:
    """有 KV cache 的层数：混合线性注意力模型只有部分层存 KV。

    取值顺序：``layer_types`` 里非 ``linear_attention`` 的层数（全注意力与滑动窗口
    注意力都存 KV）→ ``full_attention_interval`` 折算（每 N 层 1 层全注意力，取整
    除法）→ 全注意力模型等于总层数。KV 估算按这个数算：按全层算会高估（Qwen3.5 是
    4 层 1 全注意力），漏掉滑动窗口层会低估。
    """
    layers = infer_num_layers(config)
    for cfg in _llm_configs(config):
        layer_types = cfg.get("layer_types")
        if isinstance(layer_types, list) and layer_types:
            kv_layers = sum(1 for item in layer_types if item != "linear_attention")
            if kv_layers > 0:
                return kv_layers
        interval = _first_positive_int([cfg], ("full_attention_interval",))
        if interval and interval > 1 and layers:
            # 第 interval 层起每 N 层一个全注意力层，个数是 layers // interval
            return layers // interval
    return layers


def infer_num_attention_heads(config: dict) -> int | None:
    """attention head 数（``num_attention_heads`` / llama.cpp 口径的 ``n_head``）。

    TP 整除校验的第一约束（vLLM 在 config 期硬拒 ``num_attention_heads % tp != 0``）。
    """
    return _first_positive_int(_llm_configs(config), ("num_attention_heads", "n_head"))


def infer_num_kv_heads(config: dict) -> int | None:
    """KV head 数（``num_key_value_heads``）；MHA 模型不写该键时等于 attention heads。"""
    configs = _llm_configs(config)
    return _first_positive_int(configs, ("num_key_value_heads", "n_head_kv")) or infer_num_attention_heads(config)


# 上下文上限的候选键：与 vLLM ``_get_and_verify_max_len`` 同口径，取其中最小的一个。
# 只认模型自带值（不含 rope_scaling 扩展后的值）——它永远是引擎推导值的下界，据此钳制不会误拒。
_MAX_CONTEXT_KEYS: tuple[str, ...] = (
    "max_position_embeddings",
    "n_positions",
    "max_seq_len",
    "seq_length",
    "model_max_length",
    "max_target_positions",
    "max_sequence_length",
    "max_seq_length",
    "seq_len",
)


def infer_max_context(config: dict) -> int | None:
    """模型自带的上下文上限（各候选键取最小）；一个都没有返回 None。

    用途是把推荐/请求里的 ``max_model_len`` 钳到模型真能接受的范围：引擎对「超过模型自带
    上限」是硬拒（vLLM 在 config 期抛），而我们默认推荐的 8192 对短上下文模型（如 4096）
    恰好越界。
    """
    values = (
        value
        for cfg in _llm_configs(config)
        for key in _MAX_CONTEXT_KEYS
        if isinstance(value := cfg.get(key), int) and not isinstance(value, bool) and value > 0
    )
    return min(values, default=None)


def needs_trust_remote_code(config: dict) -> bool:
    """模型是否带自定义代码（config ``auto_map``）——加载它需要显式信任远程代码。

    与 HF/vLLM 自己的判据同源（``auto_map`` 指向仓库里的自定义 modeling 文件）。命中时
    引擎会以「需要 --trust-remote-code」拒绝启动，属于部署前就能告知、却要等容器起来才
    暴露的一类失败。
    """
    return bool(config.get("auto_map"))


@dataclass(frozen=True)
class ArchShape:
    """架构形状：KV 真公式、逐层 offload、TP 整除校验的共同输入；取不到的为 None。"""

    num_layers: int | None = None
    num_attention_heads: int | None = None
    num_kv_heads: int | None = None
    head_dim: int | None = None
    max_context: int | None = None
    kv_layers: int | None = None


def arch_shape(config: dict, gguf_meta=None, *, prefer_gguf: bool = False) -> ArchShape:
    """取架构形状：``prefer_gguf`` 决定 config.json 与 GGUF 头同时存在时谁优先。

    两个探测器（HF 远程 / local 本地）此前各写一遍同样的取值，加一个形状字段要改两处，
    收敛到这里。``gguf_meta`` 鸭子类型（``gguf_meta.GGUFMetadata | None``）——为空则整个
    走 config 侧，取不到留 None。

    优先序之所以由调用方定：HF 探的是仓库，仓库里带 .gguf 时那个头才是将被部署的产物；
    local 探的是 manager 自己的盘，显式 config.json 是更权威的描述。两者同时存在本就罕见。
    """
    if not prefer_gguf and config:
        return ArchShape(
            num_layers=infer_num_layers(config),
            num_attention_heads=infer_num_attention_heads(config),
            num_kv_heads=infer_num_kv_heads(config),
            head_dim=infer_head_dim(config),
            max_context=infer_max_context(config),
            kv_layers=infer_kv_layers(config),
        )
    if gguf_meta is None:
        return ArchShape()
    return ArchShape(
        num_layers=getattr(gguf_meta, "num_layers", None),
        num_attention_heads=getattr(gguf_meta, "num_attention_heads", None),
        num_kv_heads=getattr(gguf_meta, "num_kv_heads", None),
        head_dim=getattr(gguf_meta, "head_dim", None),
        max_context=getattr(gguf_meta, "max_context", None),
    )


def infer_head_dim(config: dict) -> int | None:
    """每 head 维度：显式 ``head_dim`` 优先，否则 ``hidden_size ÷ num_attention_heads``。"""
    configs = _llm_configs(config)
    explicit = _first_positive_int(configs, ("head_dim", "key_length"))
    if explicit:
        return explicit
    hidden = _first_positive_int(configs, ("hidden_size", "n_embd", "d_model"))
    heads = infer_num_attention_heads(config)
    if hidden and heads and hidden % heads == 0:
        return hidden // heads
    return None


def is_mmproj(name: str) -> bool:
    """单文件名是否为视觉投影器：含 ``mmproj`` 即认定（大小写不敏感）。

    llama.cpp 系 VLM（Qwen-VL / LLaVA / Gemma-VL / ...）把视觉编码器单独打成
    ``mmproj-*.gguf``，附在主模型 .gguf 之外——这是 GGUF 视觉能力最可靠的文件级
    信号。局限：纯音频/omni 多模态（如 Nemotron-Omni）不走 mmproj，判不出来。
    """
    return "mmproj" in name.lower()


def has_mmproj(names: list[str]) -> bool:
    """文件名列表是否含视觉投影器（任一命中 :func:`is_mmproj` 即视觉模型）。"""
    return any(is_mmproj(n) for n in names or [])


# llama.cpp 的 --mmproj 只能加载 GGUF 格式；这些扩展名是明确的他格式权重/配置，
# 选中即加载失败、容器崩溃循环（不如不注入、纯文本可服务）
_NON_GGUF_EXTENSIONS = (".pt", ".pth", ".bin", ".safetensors", ".json", ".onnx", ".msgpack")


def pick_mmproj(names: list[str]) -> str | None:
    """挑视觉投影器文件（repo 相对路径）；无可加载的 → None。

    识别按文件名含 ``mmproj``，但排除他格式扩展名（见上）——llama.cpp 读的是
    GGUF 魔数，``.pt`` / ``.safetensors`` 等真投影器权重注入 ``--mmproj`` 只会
    让容器起不来。GGUF 内容的命名变体（``*.mmproj`` / 无扩展名）保留识别：
    llama.cpp 不校验扩展名。仓库只有他格式投影器时返回 None，部署侧退化为
    不注入 ``--mmproj`` 的纯文本服务（``is_vision`` 信号仍由 has_mmproj 给出）。

    优先 F16（llama.cpp 最常验证的精度）→ BF16 → 其它。注意 ``bf16`` 含子串
    ``f16``，必须先判 BF16 再判 F16。
    """
    candidates = []
    for n in names or []:
        low = n.lower()
        if "mmproj" in low and not low.endswith(_NON_GGUF_EXTENSIONS):
            candidates.append(n)
    if not candidates:
        return None

    def _rank(name: str) -> tuple[int, str]:
        low = name.lower()
        if "bf16" in low:
            return (1, low)
        if "f16" in low:
            return (0, low)
        return (2, low)

    return sorted(candidates, key=_rank)[0]


def classify_success(fmt: str | None, size_gb: float | None) -> bool:
    """探测成功性收口：``size`` 与 ``format`` 全缺 → 严重失败（``success=False``），前端禁添加。

    - ``format`` 都没认出（连权重扩展名都没匹配到）→ 无法判定可部署性 → False；
    - ``format`` 有但 ``size`` 缺 → 个别字段缺，可人工补，放行 True；
    - 两者皆有 → 正常 True。
    多变体 GGUF repo 的 ``success=False`` 由探测器装配点直接置 False（带 message 提示带变体重试），
    不走本函数（那条路径 size 必然 None，但 format=gguf 已知，按本函数会误判 True）。
    """
    if fmt is None and size_gb is None:
        return False
    return True


def first_gguf_shard(weight_files: list[str]) -> str | None:
    """权重清单里第 0 个 GGUF 分片的相对路径（metadata 只在首片头）。

    多分片命名 ``model-00001-of-00003.gguf`` → 取序号最小者；单文件直接取。
    **排除 mmproj 视觉投影器**（它不是主模型，头里没有 expert_count；多变体时要探真模型）。
    无（非 mmproj 的）GGUF / 清单空 → None。
    """
    ggufs = [w for w in weight_files if w.lower().endswith(".gguf") and "mmproj" not in w.lower()]
    if not ggufs:
        return None
    if len(ggufs) == 1:
        return ggufs[0]

    # 多分片：按文件名里的 -NNNNN-of- 排序取最小序号
    def _shard_idx(name: str) -> int:
        m = _GGUF_SHARD_SUFFIX.search(name)
        if m:
            digits = m.group(0)
            nums = [int(x) for x in re.findall(r"\d+", digits)]
            return nums[0] if nums else 0
        return 0

    return min(ggufs, key=_shard_idx)


def infer_architecture(config: dict, model_index: dict | None = None) -> str | None:
    """身份标识原样字符串：config['architectures'][0] → config['model_type'] → config['format'] → diffusers _class_name。"""
    archs = config.get("architectures")
    if isinstance(archs, list) and archs:
        return str(archs[0])
    if isinstance(archs, str) and archs:
        return archs
    mt = config.get("model_type")
    if isinstance(mt, str) and mt:
        return mt
    fmt = config.get("format")
    if isinstance(fmt, str) and fmt:
        return fmt
    if isinstance(model_index, dict):
        cn = model_index.get("_class_name")
        if isinstance(cn, str) and cn:
            return cn
    return None


def sum_weight_gb(siblings: list[dict]) -> float | None:
    """累加权重文件大小（GB）。siblings 元素形如 ``{rfilename, size}``。无权重文件返回 None。

    per-file size 缺失/null/非数字 → 跳过该文件（不置 found），避免把「size 未知」
    当 0 累加导致整体返回 0.0（0.0 会沿 feasibility 放大为 deployable=True 的误判）。
    """
    total = 0
    found = False
    for s in siblings:
        name = (s.get("rfilename") or s.get("filename") or "").lower()
        if not name.endswith(WEIGHT_EXTS):
            continue
        size = s.get("size")
        if size is None:
            continue  # 缺失 size 不算 found，避免 0.0
        try:
            total += int(size)
            found = True
        except (TypeError, ValueError):
            continue
    return round(total / (1024**3), 2) if found else None


def _sibling_name(s) -> str:
    """单个 sibling（``{rfilename}`` dict 或 str）→ 文件名。"""
    if isinstance(s, dict):
        return s.get("rfilename") or s.get("filename") or ""
    return str(s)


def _sibling_names(siblings: list) -> list[str]:
    """siblings 可能是 ``[{rfilename}]`` 或 ``[str]``，统一取文件名列表。"""
    names: list[str] = []
    for s in siblings or []:
        n = _sibling_name(s)
        if n:
            names.append(n)
    return names


def detect_source_kind(siblings: list) -> str:
    """盘上布局（node 下载/校验策略判据）。

    判定优先级：
    1. ``model_index.json`` → ``diffusers_folder``（diffusers 标志文件）；
    2. 纯 gguf（无 config.json / 无其他权重）→ ``single_gguf``（1 个）/ ``gguf_shards``（>1，
       含 ``-00001-of-00002`` 分片或并列多 gguf）；
    3. ``config.json`` → ``transformers_folder``（node 整仓下载、verify config.json 在位）；
    4. 其余 → ``unknown``。

    与 :func:`detect_layout`（更细的文件存在性布局，含 onnx/custom_script 等）互补——本函数
    只回答「node 怎么下、怎么 verify」。被 HF / local 探测器共用，避免知识漂移。
    """
    names = [n.lower() for n in _sibling_names(siblings)]
    has_config = "config.json" in names
    has_model_index = "model_index.json" in names
    ggufs = [n for n in names if n.endswith(".gguf")]
    has_other_weight = any(n.endswith((".safetensors", ".bin", ".pt", ".pth", ".onnx")) for n in names)

    if has_model_index:
        return "diffusers_folder"
    if ggufs and not has_config and not has_other_weight:
        return "gguf_shards" if len(ggufs) > 1 else "single_gguf"
    if has_config:
        return "transformers_folder"
    return "unknown"


def structure_from_source_kind(source_kind: str | None) -> str:
    """source_kind → 旧式展示形态（``single_file`` | ``directory``）。

    ``single_gguf``/``gguf_shards``（散落 gguf 文件）→ ``single_file``；其余（文件夹布局）→
    ``directory``。仅用于 ``ModelFiles.structure`` 回显展示，**不**是 node 分发契约
    （那是 :func:`detect_source_kind` 的返回值本身）。
    """
    return "single_file" if source_kind in ("single_gguf", "gguf_shards") else "directory"


# gguf 分片后缀（-00001-of-00002 / -00001-of-00005），用于把同变体多分片归一组
_GGUF_SHARD_SUFFIX = re.compile(r"-\d+-of-\d+\.gguf$", re.IGNORECASE)


def gguf_variant_keys(names: list[str]) -> set[str]:
    """GGUF 文件按「变体」分组的键集合。

    去掉分片后缀（``-00001-of-00002``）后，同变体的多分片归同一键；不同量化/不同文件是不同键。
    用于检测多变体 repo：键数 > 1 → 无法自动判定大小（需用户带变体路径）。

    **mmproj 视觉投影器不计入变体**——它是配套文件不是备选模型（model.gguf + mmproj.gguf
    是单变体 VLM，不是多变体）。
    """
    keys: set[str] = set()
    for n in names or []:
        low = n.lower()
        if not low.endswith(".gguf"):
            continue
        if "mmproj" in low:  # 视觉投影器非模型变体，跳过
            continue
        # 去分片后缀 → 变体键（多分片归一组）
        key = _GGUF_SHARD_SUFFIX.sub(".gguf", n)
        keys.add(key)
    return keys


def detect_layout(names: list[str]) -> str:
    """目录/文件布局：diffusers | transformers | gguf | onnx | custom_config | custom_script | custom | unknown。

    纯文件存在性判定（config.json / model_index.json / 权重扩展名 / inference.py），无语义。
    """
    low = [n.lower() for n in names]
    has_config = "config.json" in low
    has_model_index = "model_index.json" in low
    has_gguf = any(n.endswith(".gguf") for n in low)
    has_safetensors = any(n.endswith(".safetensors") for n in low)
    has_onnx = any(n.endswith(".onnx") for n in low)
    has_pth = any(n.endswith((".pth", ".pt", ".bin")) for n in low)
    has_inference_py = any(n.endswith("inference.py") for n in low)
    if has_model_index and has_safetensors:
        return "diffusers"
    if has_config and has_safetensors:
        return "transformers"
    if has_gguf:
        return "gguf"
    if has_onnx:
        return "onnx"
    if has_config:
        return "custom_config"
    if has_inference_py and has_pth:
        return "custom_script"
    if has_pth:
        return "custom"
    return "unknown"
