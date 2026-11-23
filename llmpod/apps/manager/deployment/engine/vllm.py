"""vLLM 引擎适配器。

多机 worker 加 --headless，rank-0 先行（coordinate_mode=initialize_later）。
"""

from __future__ import annotations

import json

from llmpod.apps.manager.deployment.engine.base import (
    EngineAdapter,
    KVCacheDtypeOption,
    ParamSuggestion,
    fill_absent_args,
    hf_nccl_env,
    nvidia_device_config,
)
from llmpod.apps.manager.deployment.command import command_needs_shell
from llmpod.apps.manager.deployment.image_catalog import engine_vocab, version_key
from llmpod.apps.manager.deployment.schemas import DeviceConfig, EngineConfig, InstanceSpec, Topology
from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError

# 并发序列上限的平台默认值：它决定 CUDA graph 要捕获多少个 batch 尺寸（见 assemble_args）。
DEFAULT_MAX_NUM_SEQS = 32

# --compilation-config 的 cudagraph_mode 键 vLLM 0.11 起才有（更早是
# use_cudagraph/full_cuda_graph 那一套，收到未知键会被拒启）。
_CUDAGRAPH_MODE_MIN_VERSION = (0, 11, 0)

# 引擎自身等待 worker/engine 就绪的上限（VLLM_ENGINE_READY_TIMEOUT_S）。平台侧
# readiness 已改成「容器活着就一直等」，引擎内部不该更早放弃，故给个保底小时数；
# 用户把健康检查超时调得更大时随之放大。
_ENGINE_READY_TIMEOUT_FLOOR_S = 3600

# 合并结果里已有任一 CUDA graph / 编译档位开关时，平台不再补 --compilation-config：
# 同时给 --enforce-eager 会让 vLLM 语义冲突拒启；用户给了 --optimization-level 时再补
# 这条等于用显式值盖掉所选档位的 cudagraph_mode（vLLM 只给未显式设置的字段套档位默认值）。
# -O 是 --optimization-level 的短写（0.12 起；连写 -O0 / -O3 由 assemble_args 归并），
# -cc 是 --compilation-config 的短写。
_CUDAGRAPH_FLAGS = {"--compilation-config", "-cc", "-O", "--optimization-level", "--enforce-eager"}


class VLLMAdapter(EngineAdapter):
    """vLLM 适配器。。"""

    supported_formats = ["safetensors"]
    supported_gpu_archs = ["nvidia", "cann"]  # cann = Ascend NPU（vllm-ascend；按所选 cann 卡启用）
    supported_categories = [  # vLLM 实际能 serve 的（generate/embed/classify 三类任务）
        ModelType.LLM,
        ModelType.MULTIMODAL,
        ModelType.EMBEDDING,
        ModelType.RERANK,
    ]
    supported_operations = ["chat.completions", "embeddings", "rerank"]
    supported_parallel = ["tp", "pp"]  # ep 未测不声明；cann 的 pp 硬拒见 topology_conflict
    supports_multinode = True  # nvidia 走 torch-elastic；cann 的多机硬拒见 topology_conflict
    trust_remote_code_flag = "--trust-remote-code"
    supported_features = []
    # vLLM task 词汇（引擎自己的词，value 不用全局枚举）：generate→/v1/chat/completions；
    # embed→/v1/embeddings；classify(cross-encoder)→/v1/rerank+/v1/score。
    # transcription/render 需 vLLM-Omni 扩展（未来独立 adapter）。
    type_to_task = {
        ModelType.LLM: "generate",
        ModelType.MULTIMODAL: "generate",
        ModelType.EMBEDDING: "embed",
        ModelType.RERANK: "classify",
        ModelType.STT: "transcription",
        ModelType.TTS: "speech",
        ModelType.IMAGE: "render",
    }

    # 系统必控 flag（与 generate_start_args 硬编码集一致；用户 extra_args 同名被忽略并告警）
    protected_flags = [
        "--model",
        "--served-model-name",
        "--host",
        "--port",
        "--tensor-parallel-size",
        "--pipeline-parallel-size",
        "--max-model-len",
        "--runner",
        "--convert",
        "--task",
        "--nnodes",
        "--node-rank",
        "--master-addr",
        "--master-port",
        "--headless",
    ]  # 推荐自由参数（均非 protected）。--gpu-memory-utilization 不在 protected：它是用户熟悉的
    # 显存占比取值依据，可在前端 args 预览直接改（账本预留经 memory_utilization_from 跟随其值）。
    # chip 只保证「能拼出 flag」——实际可用性由镜像 tag 的引擎版本决定（如 KV 卸载两件套需
    # vLLM ≥0.11，旧镜像要走 LMCache 连接器路线）；Ascend(cann) 路径对 fp8 类特性支持有限。

    # 完整启动命令回显时换成占位符的参数（部署期逐实例回填）
    command_placeholder_flags = {
        "--model": "{model_path}",
        "--served-model-name": "{served_model_name}",
        "--port": "{port}",
        "--tensor-parallel-size": "{tp}",
        "--pipeline-parallel-size": "{pp}",
        "--nnodes": "{nnodes}",
        "--node-rank": "{node_rank}",
        "--master-addr": "{master_addr}",
        "--master-port": "{master_port}",
    }
    command_prefix = ["vllm", "serve"]
    multinode_command_placeholders = ["headless"]
    # 吃 JSON 值的引擎 flag：完整命令模式下值丢了引号会在引擎启动期崩，预检期先拦
    # （--compilation-config 的短写 -O 允许纯数字档位，由用户自行保证）
    command_json_value_flags = {
        "--compilation-config",
        "--hf-overrides",
        "--engram-config",
        "--speculative-config",
        "--mm-processor-kwargs",
        "--default-chat-template-kwargs",
        "--limit-mm-per-prompt",
        "--override-generation-config",
    }

    param_suggestions = [
        ParamSuggestion(name="--trust-remote-code", hint="信任远程代码"),
        ParamSuggestion(name="--quantization", hint="量化方案", choices=["awq", "gptq", "fp8"]),
        ParamSuggestion(name="--dtype", hint="计算精度", choices=["auto", "half", "bfloat16", "float16"]),
        ParamSuggestion(name="--enforce-eager", hint="禁用 CUDA graph"),
        # ---- 前缀缓存 ----
        ParamSuggestion(name="--enable-prefix-caching", hint="启用前缀缓存"),
        ParamSuggestion(name="--no-enable-prefix-caching", hint="关闭前缀缓存"),
        ParamSuggestion(
            name="--prefix-caching-hash-algo",
            hint="前缀块哈希算法",
            choices=["sha256", "xxhash", "sha256_cbor", "xxhash_cbor"],
        ),
        ParamSuggestion(name="--prefix-match-unit", hint="前缀命中最小 token 数"),
        # ---- KV cache 量化 ----
        ParamSuggestion(
            name="--kv-cache-dtype",
            hint="KV cache 存储精度",
            choices=["auto", "fp8", "fp8_e5m2", "fp8_e4m3"],
        ),
        ParamSuggestion(name="--kv-cache-dtype-skip-layers", hint="跳过量化的层"),
        # ---- KV cache 卸载至 CPU 内存 ----
        ParamSuggestion(name="--kv-offloading-size", hint="KV 卸载至 CPU 内存（GiB）"),
        ParamSuggestion(
            name="--kv-offloading-backend",
            hint="KV 卸载后端",
            choices=["native", "lmcache"],
        ),
        ParamSuggestion(name="--kv-transfer-config", hint="KV 连接器 JSON 配置"),
        # ---- n-gram 嵌入表（Qwen4Exp PLE）----
        # disk_offload 键上游 v0.34.0（2026-11-14 发布）才加，flag 本身 v0.30.0 才有；
        # catalog 现收的最高版本 0.32.0 及更早镜像带上它会因未知键 / 未知 flag 拒启
        ParamSuggestion(
            name="--engram-config",
            hint="n-gram嵌入表驻留位置",
            default='{"disk_offload":true}',
        ),
        # ---- 思考模式 / 投机解码 ----
        # chat template 渲染的默认入参（与请求级 chat_template_kwargs 合并，请求值优先）；
        # enable_thinking 是 Qwen3 / Qwen4Exp 系的思考开关，vLLM 的 qwen3 推理解析器也靠它。
        # JSON 值 flag：完整命令模式丢了引号由 command_json_value_flags 预检拦下。
        ParamSuggestion(
            name="--default-chat-template-kwargs",
            hint="思考模式默认开关",
            default='{"enable_thinking": false}',
        ),
        # 投机解码：ngram 按提示里的 n-gram 猜下一段、不需要草稿模型，任何模型都能开；
        # eagle / mtp 还要草稿模型路径，平台只挂主模型目录。num_speculative_tokens 是每步猜的 token 数
        ParamSuggestion(
            name="--speculative-config",
            hint="投机解码配置",
            default='{"method":"ngram","num_speculative_tokens":5}',
        ),
        # ---- 注意力 / 编译 / 调度 ----
        # 注意力后端名是 vLLM 的 AttentionBackendEnum 成员名（0.13 起才有该 flag）
        ParamSuggestion(
            name="--attention-backend",
            hint="注意力后端",
            choices=["FLASH_ATTN", "FLASHINFER", "TRITON_ATTN", "FLEX_ATTENTION"],
        ),
        # 编译与 CUDA graph 档位（-O 短写）：0=不编译不捕获、1=Piecewise 图、2/3=全量图（上游默认 2）。
        # 用户选了档位，平台就不再补 FULL_DECODE_ONLY 的 compilation-config（见 _CUDAGRAPH_FLAGS）
        ParamSuggestion(name="--optimization-level", hint="编译与 CUDA graph 档位", choices=["0", "1", "2", "3"]),
        # 0.17 起：interactivity 小批量低延迟（细粒度 CUDA graph）、throughput 高并发吞吐
        ParamSuggestion(
            name="--performance-mode",
            hint="性能取向",
            choices=["balanced", "interactivity", "throughput"],
        ),
        ParamSuggestion(name="--scheduling-policy", hint="调度策略", choices=["fcfs", "priority"]),
        # 多卡 NCCL 走自定义 all-reduce，个别机型/驱动组合会卡死在通信上，用它排除
        ParamSuggestion(name="--disable-custom-all-reduce", hint="关闭自定义 all-reduce"),
        # 流式多少个 token 吐一个 chunk（默认 1）；调大减少 SSE 帧数换吞吐
        ParamSuggestion(name="--stream-interval", hint="流式输出间隔（token 数）"),
        # 分块预填充默认开；关掉后 max_num_batched_tokens 必须不小于 max_model_len，否则拒启
        ParamSuggestion(name="--no-enable-chunked-prefill", hint="关闭分块预填充"),
        # ---- 多模态 ----
        # HF 预处理器入参：max_pixels 是 2080Ti 跑 Qwen3.6-35B 的实测可用值（默认值高得多）
        ParamSuggestion(
            name="--mm-processor-kwargs",
            hint="多模态预处理参数",
            default='{"max_pixels": 1003520}',
        ),
        # 每个请求每种模态的输入条数上限。V1 引擎默认 999，但 V0（默认走 V0 的旧镜像）
        # 每种模态只许 1 条——多图请求在旧镜像上必须显式给
        ParamSuggestion(
            name="--limit-mm-per-prompt",
            hint="每请求多模态输入上限",
            default='{"image": 16, "video": 2}',
        ),
        # ---- 采样 ----
        # 采样默认值的来源：auto=按模型 generation_config.json、vllm=忽略它用 vLLM 内置值
        ParamSuggestion(name="--generation-config", hint="采样默认值来源", choices=["auto", "vllm"]),
        # 覆盖模型 generation_config.json 里的采样默认值（temperature / top_p / repetition_penalty …）
        ParamSuggestion(
            name="--override-generation-config",
            hint="覆盖模型默认采样参数",
            default='{"temperature": 0.7}',
        ),
        # 请求 logprobs 的上限（默认 20）；客户端要更多 top logprobs 时调高
        ParamSuggestion(name="--max-logprobs", hint="logprobs 上限", default="20"),
        # ---- tool calling / 推理输出解析器 ----
        # 平台已按模型家族给默认值（Qwen3 / Qwen4Exp 系 → qwen3_coder + qwen3，其余 → hermes
        # 且不设推理解析器，见 _FAMILY_PARSERS）；这里列的是**手动微调**用的常用值，前端是
        # AutoComplete，可自行输入完整集合里的其它值。写错的名字会让 vLLM 启动期抛 KeyError
        # 直接拒启。
        ParamSuggestion(
            name="--tool-call-parser",
            hint="工具调用解析器",
            default="hermes",
            choices=[
                "hermes",
                "qwen3_coder",
                "qwen3_xml",
                "deepseek_v3",
                "glm45",
                "llama3_json",
                "llama4_json",
                "mistral",
                "kimi_k2",
                "minimax_m2",
                "openai",
                "pythonic",
                "granite",
                "internlm",
            ],
        ),
        ParamSuggestion(
            name="--reasoning-parser",
            hint="推理内容解析器",
            choices=[
                "qwen3",
                "deepseek_r1",
                "deepseek_v3",
                "glm45",
                "kimi_k2",
                "minimax_m2",
                "mistral",
                "openai_gptoss",
                "granite",
            ],
        ),
        # ---- 显存/调度常用 ----
        ParamSuggestion(name="--block-size", hint="KV 块大小（tokens）"),
        ParamSuggestion(name="--max-num-seqs", hint="单实例并发序列上限"),
        ParamSuggestion(name="--max-num-batched-tokens", hint="单个 batch 的 token 上限"),
        ParamSuggestion(name="--cpu-offload-gb", hint="权重卸载至 CPU（GiB/卡）"),
        ParamSuggestion(name="--kv-cache-memory-bytes", hint="每卡 KV cache 字节预算"),
    ]

    # KV cache 精度支持矩阵：前端 Step2 高级参数下拉 + 显存估算折算同源。
    # 首项空值 = 默认不量化（不写 flag）；fp8 系列把 KV 压到 1 字节/元素，容量约翻倍，
    # 需 CUDA 11.8+（Ascend 310P 不支持，由选项 hint 提示）。
    kv_cache_dtype_flags = ["--kv-cache-dtype"]
    kv_cache_dtype_options = [
        KVCacheDtypeOption(value="", label="默认(不量化)", hint="KV 按模型原始精度存储"),
        KVCacheDtypeOption(
            value="fp8_e4m3",
            label="FP8 E4M3",
            dtype_bytes=1.0,
            args=["--kv-cache-dtype", "fp8_e4m3"],
            hint="KV 约减半；精度更高（310P 不支持）",
        ),
        KVCacheDtypeOption(
            value="fp8_e5m2",
            label="FP8 E5M2",
            dtype_bytes=1.0,
            args=["--kv-cache-dtype", "fp8_e5m2"],
            hint="KV 约减半；动态范围大（310P 不支持）",
        ),
    ]

    entrypoint = None  # nvidia 镜像自带 ["vllm","serve"]；cann 镜像自带 bash-source 入口（exec "$@"）

    # ---- 容器/服务默认值（vLLM；从 orchestrator 提到此处，避免泄漏给其它引擎）----
    default_port = 8000
    default_master_port = 29501  # rendezvous（非 29500）
    default_network_mode = "host"  # 多机 NCCL 标准
    default_ipc = "host"
    default_shm_size = "16g"
    default_restart_policy = "no"
    # vLLM serve 自带 anthropic 兼容层（/v1/messages 与 openai 面同进程同端口，无需启动 flag）
    supported_protocols = ["openai", "anthropic"]
    default_protocol = "openai"
    # tool calling 解析器兜底值：对多数 instruct 模型合适，也是未知家族的默认值。
    # --enable-auto-tool-choice 必须配 --tool-call-parser（vLLM 启动即强校验），故不能为 None。
    # hermes 自 0.7.x 起注册在册（各版本 registry 均有），是全版本安全的兜底。
    default_tool_call_parser = "hermes"
    # 家族专属解析器（架构前缀小写 → (tool-call, reasoning)）。只收录有明确依据的家族：
    # Qwen3 / Qwen4Exp 把 ``<think>`` 写在 **prompt** 里、模型输出只剩 ``</think>``，因此它
    # 们的思考内容必须靠 ``--reasoning-parser qwen3`` 单独切出来，用通用 hermes + 不设推理
    # 解析器会让思考混进正文。反过来给非 Qwen3 模型塞 qwen3 解析器更有害——vLLM 的语法机
    # 起始即 reasoning 态（``vllm/parser/qwen3.py`` 的 ``initial_state``），输出里没有
    # think 标签时整段正文会被判成 reasoning、``content`` 返回 null（见 vLLM 自带用例
    # WITHOUT_THINK）。所以按架构门控，未知家族一律走通用兜底且不设推理解析器。
    # 名字是否存在于目标镜像的 registry 由 ``inference_images.json`` 的 ``engine_versions``
    # 词表判定（版本词表是文件不是代码：上游各版本增删/改名解析器，改数据即可）；
    # 词表里没有的名字逐项回落：工具解析器退 hermes、推理解析器不设（思考混正文是能力
    # 缺失，起不来才是事故）。
    # Qwen4Exp（Qwen3.8-Flash 一族，带 PLE n-gram 嵌入表）沿用 Qwen3 的思考与工具调用
    # 语法——chat template 同用 enable_thinking 开关，vLLM 也未注册独立的 qwen4 解析器，
    # 故与 Qwen3 给同一对；前缀 qwen4exp 覆盖 Qwen4ExpForCausalLM 与 VL 变体
    # （Qwen4ExpForConditionalGeneration）。
    _FAMILY_PARSERS: tuple[tuple[str, str, str | None], ...] = (
        ("qwen3", "qwen3_coder", "qwen3"),
        ("qwen4exp", "qwen3_coder", "qwen3"),
    )

    def parsers_for(self, architecture: str | None, engine_version: str | None = None) -> tuple[str, str | None]:
        """``(tool-call-parser, reasoning-parser)``：未知家族 → 通用兜底 + 不设推理解析器。

        家族解析器还按镜像版本查 ``engine_versions`` 词表（版本未知/未收录按最新词表）：
        名表里没有的名字，工具解析器回落 hermes、推理解析器退到不设。引擎键取
        ``engine_name``（注册表注入的唯一定义处，与镜像解析同键）。
        """
        arch = str(architecture or "").lower()
        tool, reasoning = self.default_tool_call_parser, None
        for prefix, family_tool, family_reasoning in self._FAMILY_PARSERS:
            if arch.startswith(prefix):
                tool, reasoning = family_tool, family_reasoning
                break
        vocab = engine_vocab(self.engine_name, engine_version)
        if vocab is not None:
            if vocab.tool_parsers is not None and tool not in vocab.tool_parsers:
                tool = self.default_tool_call_parser
            if vocab.reasoning_parsers is not None and reasoning not in vocab.reasoning_parsers:
                reasoning = None
        return tool, reasoning

    # 显存占比 flag：用户熟悉的首要依据（账本预留与注入均取它）
    memory_utilization_flag = "--gpu-memory-utilization"

    @classmethod
    def validate_cross_node(cls, tp: int, pp: int, nnodes: int) -> None:
        """vLLM torch-elastic：pp 参与跨机分摊——总并行度 tp×pp 整除 nnodes 即可
        （tp=1/pp=2/nnodes=2 每节点 1 卡合法）。"""
        if (tp * pp) % nnodes != 0:
            raise LLMPodValidationError(
                f"topology.tp×pp({tp}×{pp}) 必须能被 nnodes({nnodes}) 整除（每节点 tp×pp/nnodes 卡）"
            )

    def topology_conflict(self, arch: str, topology) -> str | None:
        """cann（vllm-ascend）的拓扑硬冲突：pp>1 与多机都不支持——与 ``_ascend_args`` 的
        组装事实同口径（cann 分支从未组装多机 rendezvous 五件套，放行只会拉起互不相识的
        单机容器各自挂死）。

        自动回落序据此剔除 cann（否则带 pp/多机的副本被调度落上去、等容器阶段才报错）；
        手选卡路径据此在创建期就拒，而不是占完卡、落完库再失败。
        """
        if arch != "cann" or topology is None:
            return None
        units = topology.per_copy_units()
        if any(int(u.nnodes or 1) > 1 for u in units):
            return "Ascend(cann) 暂不支持多机部署（多机启动参数未组装）"
        if all(max(1, int(u.pp or 1)) <= 1 for u in units):
            return None
        return "Ascend(cann) 暂不支持流水线并行（pp>1）"

    @classmethod
    def build_device_config(cls, instance: InstanceSpec, gpu_arch: str) -> DeviceConfig:
        if gpu_arch == "nvidia":
            return nvidia_device_config(instance)
        if gpu_arch == "cann":
            # Ascend：ascend-docker-runtime + /dev/davinci* + 宿主 driver/dcmi 挂载。
            # gpus=None（不走 nvidia device_requests）；NPU 由 runtime + extra_devices 注入。
            npu_ids = list(instance.assigned_gpus or [0])
            return DeviceConfig(
                runtime="ascend",
                gpus=None,
                driver_mounts=[
                    "/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64:ro",
                    "/usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info:ro",
                    "/usr/local/dcmi:/usr/local/dcmi:ro",
                    "/usr/local/bin/npu-smi:/usr/local/bin/npu-smi:ro",
                    "/etc/ascend_install.info:/etc/ascend_install.info:ro",
                ],
                extra_devices=(
                    [f"/dev/davinci{i}:/dev/davinci{i}:rwm" for i in npu_ids]
                    + [
                        "/dev/davinci_manager:/dev/davinci_manager:rwm",
                        "/dev/devmm_svm:/dev/devmm_svm:rwm",
                        "/dev/hisi_hdc:/dev/hisi_hdc:rwm",
                    ]
                ),
                privileged=False,
                cap_add=[],
            )
        raise ValueError(f"vLLM 适配器不支持该 GPU 架构: {gpu_arch}")

    @classmethod
    def build_env(
        cls,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: EngineConfig,
        health_timeout_s: int = 600,
    ) -> dict:
        # 引擎内部等待就绪的上限：与 health_check 联动但保底一小时（平台侧已改成
        # 容器活着就一直等，引擎不该比平台先放弃）
        ready_timeout = str(max(int(health_timeout_s), _ENGINE_READY_TIMEOUT_FLOOR_S))
        if (instance.gpu_arch or "nvidia") == "cann":
            # Ascend：只设适配器能定的那部分。显存分配策略（PYTORCH_NPU_ALLOC_CONF）留给
            # 镜像 catalog 声明——本层是 system 层，会盖掉 image 层，在这里写死等于让 catalog
            # 里按卡型分开调的声明全部失效。catalog 未命中（用户固定自带镜像）时 env 整层
            # 缺失，兜一条通用值（与 catalog 各条目同款，宁可保守不可裸奔——NPU 碎片化 OOM）
            env = {
                "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
                # 全部分配到的 NPU 逗号串——与 build_device_config 的 /dev/davinci* 挂载
                # 同一口径：只曝第一张会让 vllm-ascend 可见设备数 < tp，多卡部署初始化必败
                "ASCEND_VISIBLE_DEVICES": ",".join(str(i) for i in (instance.assigned_gpus or [0])),
                "VLLM_ENGINE_READY_TIMEOUT_S": ready_timeout,
            }
            return env
        env = hf_nccl_env(engine_config, instance, cls.default_nccl_ifname)
        env["VLLM_ENGINE_READY_TIMEOUT_S"] = ready_timeout
        return env

    # ---- 启动参数----

    def _ascend_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        engine_version: str | None = None,
    ) -> list[str]:
        """Ascend（vllm-ascend）启动参数。

        cann 镜像入口是 bash-source（source CANN env; exec "$@"），故参数须带 ``vllm serve`` 前缀。
        310P3 必须 ``--dtype half``（不支持 bf16）；generate 任务显式 ``--max-model-len``
        （默认 4096 防 OOM），embedding/rerank 不传（理由见 :meth:`_pooling_args`，词表
        同样按 ``engine_version`` 分档——vllm-ascend 版本号跟随上游 vLLM）。
        pp>1 / 多机直接拒：vllm-ascend 的 PP 支持不成熟且无多卡环境可验证，多机
        rendezvous 参数在本分支也未组装——静默丢 flag 会让
        调度/账本/引擎三方口径分裂（调度按 tp×pp 分卡、引擎只按 tp 跑），拒绝比跑错强。
        判**本实例**自己的形状而非整拓扑：异构副本里兄弟份用 pp>1 不该把 pp=1 的 cann 份
        连带拒掉（架构闸门 ``schedulable_archs`` 仍按整拓扑剔除 cann——那是「副本可能落到
        哪」的保守收窄，与本处的逐实例硬拒不冲突）。判据仍走 :meth:`topology_conflict`
        同一来源（拿本实例形状合成单份拓扑判）。
        tp>1 注入 ``--tensor-parallel-size``（多 NPU 单机，vllm-ascend 支持；单卡不注入）。
        """
        own = Topology(
            tp=max(1, int(instance.tp or 1)),
            pp=max(1, int(instance.pp or 1)),
            nnodes=max(1, int(instance.nnodes or 1)),
        )
        conflict = self.topology_conflict("cann", own)
        if conflict is not None:
            raise LLMPodValidationError(f"{conflict}；请单机 pp=1 或换 nvidia 部署")
        port = int(instance.port or self.default_port)
        model_path = instance.model_path
        served = instance.served_model_name or model_path
        task = instance.task
        category = self.category_for_task(task)
        args: list[str] = [
            "vllm",
            "serve",
            str(model_path),
            "--served-model-name",
            str(served),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--dtype",
            "half",
        ]
        tp = max(1, int(instance.tp))
        if tp > 1:
            args += ["--tensor-parallel-size", str(tp)]
        if category in (ModelType.EMBEDDING, ModelType.RERANK):
            args += self._pooling_args(task, engine_version)
        else:
            # 与 nvidia 路径同一理由：让 usage 带 cached_tokens（见 generate_start_args）
            args.append("--enable-prompt-tokens-details")
            ctx = int(topology.max_model_len) if topology.max_model_len else 4096
            args += ["--max-model-len", str(ctx)]
        args += self._vram_percent_args(instance)
        # 用户额外参数补缺式追加（系统 flag 已在 args 中，同名被跳过防覆盖）
        extra = list(engine_params.get("extra_args") or [])
        return fill_absent_args(args, extra)

    def _pooling_args(self, task: str, engine_version: str | None = None) -> list[str]:
        """embedding/rerank 任务的启动参数：vLLM 新版本弃用 --task，改用
        ``--runner pooling --convert <task>``（embed→/v1/embeddings、classify→
        /v1/rerank+/v1/score）。词表档位查 ``engine_versions`` 的 ``pooling_flags``
        （0.11 前的镜像不认 runner/convert、走旧词表 ``--task embed/classify``——两组
        取值自 0.7.3 起就都在；版本未知/未收录按新词表）。同时不传 --max-model-len：
        这两类模型 config 自带 max_position_embeddings（如 reranker 常为 512），
        硬传超限会拒启。

        classify 额外补 ``--hf-overrides``（ForCausalLM 架构 reranker 的在线
        转换头，vLLM 0.27.1 源码核实的两道门槛），仅新词表路径带——转换头协议
        是新版适配器 API，旧版 classify 按自带逻辑跑、强传未知键只会添乱：
        - ``num_labels=1``：``enable_scoring_api`` 仅在 classify num_labels==1
          时注册 /rerank、/score 路由（config 缺省时 transformers 补 2 → 404）；
        - ``method=from_2_way_softmax + classifier_from_token``：权重无分类头
          （只有 true/false token id，见模型 ``1_LogitScore/config.json``），
          让 vLLM 用 true/false token 的 lm_head 差在线构造 score 头
          （``adapters.SequenceClassificationConfig`` 同一约定，参考
          huggingface.co/Qwen/Qwen3-Reranker-0.6B/discussions/3）。
        token id 是 Qwen 系 reranker 的出厂值（"false"/"true"），跨词表稳定。
        """
        vocab = engine_vocab(self.engine_name, engine_version)
        if vocab is not None and vocab.pooling_flags == "task":
            return ["--task", task]
        args = ["--runner", "pooling", "--convert", task]
        if task == "classify":
            overrides = json.dumps(
                {
                    "num_labels": 1,
                    "method": "from_2_way_softmax",
                    "classifier_from_token": ["false", "true"],
                }
            )
            args += ["--hf-overrides", overrides]
        return args

    @staticmethod
    def _vram_percent_args(instance: InstanceSpec) -> list[str]:
        """注入 --gpu-memory-utilization（= 生效显存占比，对齐 ledger reserved）。

        生效值已由编排器解析好写进 ``instance.vram_percent``（用户 extra_args 优先 > 引擎默认）；
        恒注入让用户在前端 args 预览里看到这个熟悉的引擎 flag 并可直接改。
        """
        vram_percent = getattr(instance, "vram_percent", None) or VLLMAdapter.default_gpu_memory_utilization
        return ["--gpu-memory-utilization", str(vram_percent)]

    def assemble_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        image_entry,
        engine_params: dict,
    ) -> list[str]:
        """三层合并之后，再补平台默认的并发上限与 CUDA graph 模式。

        放在合并之后而不是 ``generate_start_args`` 里，是因为「用户/镜像是否已经声明」
        只有合并结果说了算；在生成侧判断看不到 image 层，会多出一套并行的让位逻辑。

        - ``--max-num-seqs``：决定 CUDA graph 要捕获多少个 batch 尺寸（vLLM 缺省 256
          → 50+ 个），直接关系冷启动时长；合并结果里没有就补 32。
        - ``--compilation-config {"cudagraph_mode": "FULL_DECODE_ONLY"}``：只捕获 decode
          图、prefill 不捕获，换更快的冷启动（代价是 prefill/mixed 少一层 piecewise
          图）。该键 vLLM 0.11 起才有，版本未知一律不补——未知键会被拒启，宁可少
          优化。已有任一 CUDA graph / 编译档位开关（用户或镜像声明）时也不补。
        """
        args = super().assemble_args(instance, topology, image_entry, engine_params)
        flags: set[str] = set()
        for token in args:
            if token.startswith("-"):
                # -O0 / -O3 / -O=2 连写形态归并到 -O（vLLM 没有别的 -O 开头短 flag）
                name = "-O" if token.startswith("-O") else token.split("=", 1)[0]
                flags.add(name)
        if "--max-num-seqs" not in flags:
            args += ["--max-num-seqs", str(DEFAULT_MAX_NUM_SEQS)]
        engine_version = image_entry.engine_version if image_entry else None
        known_recent = bool(engine_version) and version_key(engine_version) >= _CUDAGRAPH_MODE_MIN_VERSION
        is_ascend = (instance.gpu_arch or "nvidia") == "cann"  # vllm-ascend 走 --enforce-eager 语义
        if known_recent and not is_ascend and not flags & _CUDAGRAPH_FLAGS:
            args += ["--compilation-config", json.dumps({"cudagraph_mode": "FULL_DECODE_ONLY"})]
        return args

    def split_command(self, tokens: list[str], arch: str) -> tuple[list[str] | None, list[str]]:
        """cann 镜像入口是 bash-source 脚本，``vllm serve`` 须留在 args 里，不剥离前缀。"""
        if arch == "cann" and tokens[: len(self.command_prefix)] == self.command_prefix:
            return self.entrypoint, list(tokens)
        return super().split_command(tokens, arch)

    def command_run_argv(self, rendered: str, arch: str) -> tuple[list[str] | None, list[str]]:
        """cann 镜像入口是 bash-source 脚本，含 shell 语法的命令也走 args，保住入口。"""
        if arch == "cann" and command_needs_shell(rendered):
            return None, ["bash", "-c", rendered]
        return super().command_run_argv(rendered, arch)

    def command_placeholder_values(self, inst: InstanceSpec) -> dict[str, str]:
        return {"headless": "--headless" if inst.node_rank > 0 else ""}

    def command_template(self, tokens: list[str], inst: InstanceSpec) -> str:
        """多机模板补 ``{headless}``：head 渲染为空，worker 渲染成 ``--headless``。"""
        template = super().command_template(tokens, inst)
        if inst.nnodes > 1 and "{headless}" not in template:
            template = f"{template} {{headless}}"
        return template

    def generate_start_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        *,
        engine_version: str | None = None,
    ) -> list[str]:
        if (instance.gpu_arch or "nvidia") == "cann":
            return self._ascend_args(instance, topology, engine_params, engine_version)
        # 并行度取实例自带值（异构副本各份形状不同，部署级 topology 只等于首份模板）——
        # 用 topology 拼 flag 会让第二份按首份卡数启动（空占卡 / world size 与卡数不符）。
        # 实例值由展开/盖章保证 ≥1，不再回退 topology
        tp = max(1, int(instance.tp))
        pp = max(1, int(instance.pp))
        port = int(instance.port or 8000)
        model_path = instance.model_path
        # served-model-name 是 OpenAI API client 看到的模型名，≠ --model 路径
        served = instance.served_model_name or model_path

        args: list[str] = [
            "--model",
            str(model_path),
            "--served-model-name",
            str(served),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--tensor-parallel-size",
            str(tp),
        ]
        if pp > 1:
            args += ["--pipeline-parallel-size", str(pp)]

        # 引擎 task（由 _plan 经 adapter.task_for(model_type) 填到 instance.task）：
        # embedding/rerank 走 --runner pooling + --convert（见 _pooling_args）；
        # generate 走默认 auto 即可。
        task = instance.task
        category = self.category_for_task(task)

        # max_model_len（上下文长度）：影响 KV cache 占用 + vLLM --max-model-len。
        # embedding/rerank 不传（config 自带 max_position，硬传超限拒启，见 _pooling_args）。
        if topology.max_model_len and ModelType.has_kv_cache(category):
            args += ["--max-model-len", str(int(topology.max_model_len))]

        if category in (ModelType.EMBEDDING, ModelType.RERANK):
            args += self._pooling_args(task, engine_version)
        else:
            # usage 里带 prompt_tokens_details.cached_tokens：vLLM 默认不带，而平台的缓存
            # 命中计价 / 调用日志 / 指标都读这个字段——不给就一直按「未上报」记成空。
            # flag 自 0.6.6 起就有；V1 引擎 0.9.1 起才真往外报（更早版本带了也只是
            # 多个不生效的开关，无害），所以不分版本恒注入。
            args.append("--enable-prompt-tokens-details")
            # 默认启用 tool calling（仅 generate 任务；embed/classify 无 chat 工具端点）。
            # --enable-auto-tool-choice 仅在请求带 tools 时才注入 schema，普通 chat 不受影响。
            # 解析器默认按模型家族给（Qwen3 系走自己的 XML 与思考格式，见 _FAMILY_PARSERS，
            # 并按 engine_version 逐项判引入门槛——旧镜像没有的名字会被 vLLM 拒启）。
            # 家族默认只是兜底——同一个架构名下 JSON/XML 两种工具调用协议都存在，协议对不对
            # 只有用户知道自己的模型。用户覆盖由合并层保证：assemble_args 的 merge 对同名
            # flag 是**替换**语义（用户 extra_args 在最后一层），系统默认只会被换掉不会被
            # 顶成重复。本函数的 engine_params 直读分支仅供直接调用方（不经合并层）使用；
            # 成对补齐（--enable-auto-tool-choice）两条路径各自保证。
            extra_flags = {str(a) for a in (engine_params.get("extra_args") or [])}
            tool_parser, reasoning_parser = self.parsers_for(instance.model_architecture, engine_version)
            if "--tool-call-parser" in extra_flags:
                # 用户自带解析器：vLLM 要求 --enable-auto-tool-choice 与解析器成对
                if "--enable-auto-tool-choice" not in extra_flags:
                    args += ["--enable-auto-tool-choice"]
            else:
                args += ["--enable-auto-tool-choice", "--tool-call-parser", tool_parser]
            if reasoning_parser and "--reasoning-parser" not in extra_flags:
                args += ["--reasoning-parser", reasoning_parser]

        # 多机：rank-0 非 headless；worker(rank>0) 加 --headless。
        # elastic 五件套（--nnodes/--node-rank/--master-addr/--master-port/--headless 的 torch-elastic
        # 语义）0.12 才进 vLLM——0.9.0 上的 --headless 是 **DP 语义**（配 --data-parallel-start-rank），
        # 0.7.3 连 flag 都没有，旧镜像多机只有 Ray 路径（平台未实现）。判据与 inspect 回显
        # 同源（supports_multinode_at：引擎声明 ∧ 版本词表），组装期硬拒，不静默拼一堆
        # 旧镜像不认的 flag。
        nnodes = instance.nnodes
        if nnodes > 1:
            if not self.supports_multinode_at(engine_version):
                raise LLMPodValidationError(
                    f"vLLM {engine_version} 不支持平台的多机部署（elastic 参数 0.12 起可用；旧版多机仅 Ray 路径，未实现）"
                )
            node_rank = instance.node_rank
            master_addr = instance.master_addr
            master_port = int(instance.master_port or self.default_master_port)
            args += [
                "--nnodes",
                str(nnodes),
                "--node-rank",
                str(node_rank),
                "--master-addr",
                str(master_addr),
                "--master-port",
                str(master_port),
            ]
            if node_rank != 0:
                args.append("--headless")  # worker 不起 API server

        args += self._vram_percent_args(instance)

        # 用户引擎参数（最高覆盖）：仅追加用户显式声明的额外 flag（不覆盖系统必控项）
        extra = list(engine_params.get("extra_args") or [])
        return fill_absent_args(args, extra)
