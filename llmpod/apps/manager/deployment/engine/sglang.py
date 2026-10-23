"""SGLang 引擎适配器。

SGLang 服务 OpenAI chat / embeddings / rerank 端点（与 vLLM 同构），但参数词汇不同：
rerank 直接是 ``--is-embedding``（cross-encoder 由 /v1/rerank 暴露），非 vLLM 的 --task classify。
镜像 ``lmsysorg/sglang`` 无 ENTRYPOINT（CMD /bin/bash），必须显式传 entrypoint
``python3 -m sglang.launch_server``；参数只带 flag，不带 ``sglang serve`` 前缀。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine.base import (
    EngineAdapter,
    KVCacheDtypeOption,
    ParamSuggestion,
    fill_absent_args,
    hf_nccl_env,
    nvidia_device_config,
)
from llmpod.apps.manager.deployment.image_catalog import engine_vocab
from llmpod.apps.manager.deployment.schemas import DeviceConfig, EngineConfig, InstanceSpec, Topology
from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError


class SGLangAdapter(EngineAdapter):
    """SGLang 适配器（nvidia）。"""

    supported_formats = ["safetensors"]
    supported_gpu_archs = ["nvidia"]
    supported_categories = [ModelType.LLM, ModelType.MULTIMODAL, ModelType.EMBEDDING, ModelType.RERANK]
    supported_operations = ["chat.completions", "embeddings", "rerank"]
    supported_parallel = ["tp", "pp"]  # ep 未测不声明
    supports_multinode = True  # --nnodes/--node-rank/--dist-init-addr（词表按版本再判）
    trust_remote_code_flag = "--trust-remote-code"
    supported_features = []
    deployable = True
    # SGLang task 词汇（引擎自己的词，与 vLLM 不同：rerank 就是 rerank，非 classify）
    type_to_task = {
        ModelType.LLM: "generate",
        ModelType.MULTIMODAL: "generate",
        ModelType.EMBEDDING: "embed",
        ModelType.RERANK: "rerank",
    }

    # 系统必控 flag（与 generate_start_args 硬编码集一致；用户 extra_args 同名被忽略并告警）
    protected_flags = [
        "--model-path",
        "--served-model-name",
        "--host",
        "--port",
        "--tp-size",
        "--pp-size",
        "--context-length",
        "--is-embedding",
        "--nnodes",
        "--node-rank",
        "--dist-init-addr",
    ]
    # 推荐自由参数（均非 protected）。--mem-fraction-static 不在 protected：它是用户熟悉的显存占比
    # 取值依据，可在前端 args 预览直接改（账本预留经 memory_utilization_from 跟随其值）。
    # chip 只保证「能拼出 flag」——实际可用性由镜像 tag 的引擎版本决定（slru/priority 等为
    # 较新版本取值）；HiCache 三件套（开关 + hicache-size 或 hicache-ratio）须同时添加。
    param_suggestions = [
        ParamSuggestion(name="--dtype", hint="计算精度", choices=["auto", "half", "bfloat16", "float16"]),
        ParamSuggestion(name="--trust-remote-code", hint="信任远程代码（自定义模型架构）"),
        ParamSuggestion(name="--quantization", hint="量化方案", choices=["awq", "gptq", "fp8", "marlin"]),
        ParamSuggestion(name="--attention-backend", hint="注意力后端", choices=["flashinfer", "triton", "torch_sdpa"]),
        # ---- tool calling / 推理输出解析器 ----
        # 平台已默认注入 auto（按模型自己的 chat template 检测）；这里是手动指定时用的常用值
        # （SGLang 词表与 vLLM 不同：如 deepseek-r1 带连字符、Qwen2.5 系为 qwen25）。
        ParamSuggestion(
            name="--tool-call-parser",
            hint="工具调用解析器；默认 auto（按 chat template 检测）。须匹配模型家族，可自行输入其它值",
            choices=[
                "qwen25",
                "qwen3_coder",
                "glm45",
                "llama3",
                "mistral",
                "kimi_k2",
                "deepseekv3",
                "pythonic",
                "hermes",
            ],
        ),
        ParamSuggestion(
            name="--reasoning-parser",
            hint="推理内容解析器；默认 auto（按 chat template 检测）。不设时思考过程会混进正文",
            choices=["qwen3", "deepseek-r1", "deepseek-v3", "glm45", "kimi_k2", "mistral", "gpt-oss"],
        ),
        ParamSuggestion(
            name="--sampling-backend",
            hint="采样后端（flashinfer 异常时回退 pytorch）",
            choices=["flashinfer", "pytorch"],
        ),
        # ---- 前缀缓存（RadixAttention 默认开启）----
        ParamSuggestion(
            name="--disable-radix-cache",
            hint="关闭 RadixAttention 前缀缓存（默认开启；无前缀复用场景可关省开销）",
        ),
        ParamSuggestion(
            name="--radix-eviction-policy",
            hint="radix 树淘汰策略",
            choices=["lru", "lfu", "slru", "priority"],
        ),
        ParamSuggestion(
            name="--page-size",
            hint="页大小（tokens），影响缓存粒度与碎片率；一般不动",
        ),
        # ---- KV cache 量化 ----
        ParamSuggestion(
            name="--kv-cache-dtype",
            hint="KV cache 存储精度：fp8 系列使 KV 容量约翻倍；异常时退回 auto",
            choices=["auto", "fp8_e4m3", "fp8_e5m2", "bf16"],
        ),
        # ---- KV cache 卸载至主机内存（HiCache 层级缓存）----
        ParamSuggestion(
            name="--enable-hierarchical-cache",
            hint="HiCache 总开关：GPU↔主机内存分级 KV 缓存；须配 --hicache-size 或 --hicache-ratio",
        ),
        ParamSuggestion(
            name="--hicache-size",
            hint="host 内存 KV 池固定大小（GB，设置后覆盖 --hicache-ratio）；须配 --enable-hierarchical-cache",
        ),
        ParamSuggestion(
            name="--hicache-ratio",
            hint="host KV 池大小 = GPU KV 池 × 此比例（默认 2.0；与 --hicache-size 二选一）",
        ),
        ParamSuggestion(
            name="--hicache-write-policy",
            hint="HiCache 写策略",
            choices=["write_through", "write_back", "write_through_selective"],
        ),
        ParamSuggestion(
            name="--hicache-io-backend",
            hint="GPU↔CPU KV 传输 IO 后端",
            choices=["kernel", "direct", "kernel_ascend"],
        ),
        ParamSuggestion(
            name="--hicache-mem-layout",
            hint="host 内存池布局（一般不动）",
            choices=["page_first", "layer_first"],
        ),
        ParamSuggestion(
            name="--hicache-storage-backend",
            hint="高级：L3 持久存储后端（file/mooncake/nixl/hf3fs/aibrix 等），可另配 extra-config JSON",
        ),
    ]

    # KV cache 精度支持矩阵：前端 Step2 高级参数下拉 + 显存估算折算同源。
    # 首项空值 = 默认不量化（不写 flag）；fp8 系列把 KV 压到 1 字节/元素，容量约翻倍；
    # bf16 是显式指定（占用与默认同档），列出供用户固定精度。
    kv_cache_dtype_flags = ["--kv-cache-dtype"]
    kv_cache_dtype_options = [
        KVCacheDtypeOption(value="", label="默认(不量化)", hint="KV 按模型原始精度存储"),
        KVCacheDtypeOption(
            value="fp8_e4m3",
            label="FP8 E4M3",
            dtype_bytes=1.0,
            args=["--kv-cache-dtype", "fp8_e4m3"],
            hint="KV 约减半；动态范围小、精度更高",
        ),
        KVCacheDtypeOption(
            value="fp8_e5m2",
            label="FP8 E5M2",
            dtype_bytes=1.0,
            args=["--kv-cache-dtype", "fp8_e5m2"],
            hint="KV 约减半；动态范围大、精度略低",
        ),
        KVCacheDtypeOption(
            value="bf16",
            label="BF16",
            dtype_bytes=2.0,
            args=["--kv-cache-dtype", "bf16"],
            hint="显式指定 BF16 存储（占用与默认同档）",
        ),
    ]

    # SGLang 镜像无 ENTRYPOINT（CMD /bin/bash），必须显式传；参数不带 `sglang serve` 前缀
    entrypoint = ["python3", "-m", "sglang.launch_server"]

    # ---- 容器/服务默认值（SGLang；对齐 vLLM：host 网络 + IPC + shm 供 NCCL）----
    default_port = 8000
    default_master_port = 5001  # 多机 dist-init 端口（--dist-init-addr ip:port）
    default_network_mode = "host"
    default_ipc = "host"
    default_shm_size = "16g"
    default_restart_policy = "no"
    # SGLang http_server 自带 anthropic 兼容层（/v1/messages 与 openai 面同进程同端口，无需启动 flag）
    supported_protocols = ["openai", "anthropic"]
    default_protocol = "openai"
    # 显存占比 flag：用户熟悉的首要依据（账本预留与注入均取它）
    memory_utilization_flag = "--mem-fraction-static"

    @classmethod
    def validate_cross_node(cls, tp: int, pp: int, nnodes: int) -> None:
        """SGLang 多机分片规则（``engine.py _calculate_rank_ranges`` 的源码语义）：
        ``pp_size_per_node = max(pp//nnodes, 1)``、``nnodes_per_pp_rank = max(nnodes//pp, 1)``、
        ``tp_size_per_node = tp // nnodes_per_pp_rank``——**仅 (tp×pp) 整除 nnodes 不够**，
        分区不能有取整失真：pp 整除 nnodes（每节点多个 stage），或 nnodes 整除 pp 且 tp
        整除 nnodes/pp（一个 stage 跨多节点）。tp=2/pp=3/nnodes=2 这类「总卡数整除但
        分不开」的形状，引擎会静默按另一个每节点卡数拉起（多起进程炸 device ordinal /
        少起 stage 挂 rendezvous），调度预留与引擎实跑对不上，必须在此拒。
        """
        pp_fits_in_nodes = pp % nnodes == 0
        stage_spans_nodes = nnodes % pp == 0 and tp % (nnodes // pp) == 0
        if not (pp_fits_in_nodes or stage_spans_nodes):
            raise LLMPodValidationError(
                f"topology.tp×pp({tp}×{pp}) 须能按节点均分到 nnodes({nnodes})："
                f"pp 整除 nnodes，或 nnodes 整除 pp 且 tp 整除 nnodes/pp（SGLang 按 stage 分节点）"
            )

    @classmethod
    def build_device_config(cls, instance: InstanceSpec, gpu_arch: str) -> DeviceConfig:
        if gpu_arch == "nvidia":
            return nvidia_device_config(instance)
        raise NotImplementedError(f"SGLang 适配器不支持该 GPU 架构: {gpu_arch}")

    @classmethod
    def build_env(
        cls,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: EngineConfig,
        health_timeout_s: int = 600,
    ) -> dict:
        return hf_nccl_env(engine_config, instance, cls.default_nccl_ifname)

    def generate_start_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        *,
        engine_version: str | None = None,
    ) -> list[str]:
        # 并行度取实例自带值（异构副本各份形状不同，部署级 topology 只等于首份模板）
        tp = max(1, int(instance.tp))
        pp = max(1, int(instance.pp))
        port = int(instance.port or self.default_port)
        model_path = instance.model_path
        # served-model-name 是 OpenAI API client 看到的模型名，≠ --model-path 路径
        served = instance.served_model_name or model_path

        args: list[str] = [
            "--model-path",
            str(model_path),
            "--served-model-name",
            str(served),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--tp-size",
            str(tp),
        ]
        if pp > 1:
            # --pp-size 0.4.7 起才有：更早镜像没有 pp 概念，传了必炸 argparse（烧完下载和
            # 卡租约才死）。判据与 inspect 的 supported_parallel_at 同源（supported_parallel
            # 白名单按版本折算），显示门与组装门不会漂
            if "pp" not in self.supported_parallel_at(engine_version):
                raise LLMPodValidationError(
                    f"SGLang {engine_version} 不支持流水线并行（--pp-size 0.4.7 起才有）；请升镜像或 pp=1"
                )
            args += ["--pp-size", str(pp)]

        # 引擎 task（_plan 经 adapter.task_for(model_type) 填到 instance.task）：
        # embed / rerank 均走 SGLang --is-embedding（embedding 服务器同时暴露
        # /v1/embeddings、/v1/rerank、/v1/score）。generate 走默认，无需显式传。
        task = instance.task
        category = self.category_for_task(task)

        # 上下文长度（影响 KV cache 占用 + SGLang --context-length）。
        # embedding/rerank（无 KV cache）不传——config 自带 max_position，硬传超限拒启
        # （对齐 vLLM 的 has_kv_cache 守卫）。
        if topology.max_model_len and ModelType.has_kv_cache(category):
            args += ["--context-length", str(int(topology.max_model_len))]

        if category in (ModelType.EMBEDDING, ModelType.RERANK):
            args.append("--is-embedding")
        else:
            # 解析器交给引擎自检：``auto`` 按模型自己的 chat template 判定，检测不出来时
            # 只警告并禁用（sglang ``entrypoints/engine.py``），不是硬失败。因此任何模型都能
            # 安全带上——不需要我们维护「家族 → 解析器名」映射，也避开了各后端词表不同
            # （SGLang 的 ``qwen25`` / ``deepseek-r1`` vs vLLM 的 ``qwen3_coder`` / ``deepseek_r1``）。
            # 不设时开启思考的模型会把思考内容混进正文，工具调用也解析不出来。
            # ``auto`` 是否在 choices 里由 ``inference_images.json`` 的 ``engine_versions``
            # 词表判定（0.5.12 才加入，0.4.x/0.5.11 传 auto 会被 argparse 拒启；0.4.2 连
            # ``--reasoning-parser`` flag 都没有）。旧镜像两个 flag 都不注入：解析器缺失
            # 只是思考混正文/工具不可用，起不来才是事故。要精确解析器，用户在引擎参数里
            # 自己给（两个 flag 非 protected）。版本未知/未收录按最新词表（维持既有行为）。
            vocab = engine_vocab(self.engine_name, engine_version)
            if vocab is None or vocab.parser_auto:
                args += ["--tool-call-parser", "auto", "--reasoning-parser", "auto"]

        # 多机：SGLang 用 --nnodes/--node-rank/--dist-init-addr（非 vLLM 的 master-addr/port）
        nnodes = instance.nnodes
        if nnodes > 1:
            if not self.supports_multinode_at(engine_version):
                raise LLMPodValidationError(
                    f"SGLang {engine_version} 不支持平台的多机部署参数（请使用支持 --dist-init-addr 的镜像或 nnodes=1）"
                )
            node_rank = instance.node_rank
            master_addr = instance.master_addr
            master_port = int(instance.master_port or self.default_master_port)
            args += [
                "--nnodes",
                str(nnodes),
                "--node-rank",
                str(node_rank),
                "--dist-init-addr",
                f"{master_addr}:{master_port}",
            ]

        # 显存占比：--mem-fraction-static（对齐 ledger reserved）；生效值已写进 instance.vram_percent
        vram_percent = getattr(instance, "vram_percent", None) or SGLangAdapter.default_gpu_memory_utilization
        args += ["--mem-fraction-static", str(vram_percent)]

        # Prometheus 指标开关：SGLang 各版本默认 enable_metrics=False，不开则 /metrics
        # 404，中央 scraper 采不到 KV 水位/排队。该 flag 自 0.4.x 起存在；词表标明
        # 更早版本时跳过，避免 argparse 因未知 flag 拒绝启动。
        vocab = engine_vocab(self.engine_name, engine_version)
        if vocab is None or vocab.metrics_flag:
            args.append("--enable-metrics")

        # 用户引擎参数（最高覆盖）：仅追加用户显式声明的额外 flag（不覆盖系统必控项）
        extra = list(engine_params.get("extra_args") or [])
        return fill_absent_args(args, extra)
