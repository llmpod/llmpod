"""llama.cpp 引擎适配器（GGUF；nvidia GPU 全量/多卡 + 纯 CPU）。

llama-server 服务 GGUF 单文件，OpenAI 兼容 chat / embeddings 端点。
权重上卡由 ``--n-gpu-layers`` 控制：系统只给两端——0=纯 CPU、999=全量上卡（版本无关的大数写法）；
中间档（部分层上卡）系统不推荐，用户要自己调就在部署参数里改这一项。
多卡走 llama.cpp 默认的 layer 切分（按加载时空闲显存比例整层分配），系统不做 ``--tensor-split``。
host 网络使端口直绑宿主（与 vLLM 一致，healthcheck 可达）；纯 CPU 时 ``build_device_config``
返回 ``gpus=None``（不申请设备）。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine.base import (
    EngineAdapter,
    KVCacheDtypeOption,
    _flag_value,
    has_flag,
    nvidia_device_config,
)
from llmpod.apps.manager.deployment.resource import NGL_ALL
from llmpod.apps.manager.deployment.schemas import (
    DeviceConfig,
    EngineConfig,
    InstanceSpec,
    Topology,
)
from llmpod.config.enums import ModelType

# 非 f16 档的 KV 量化类型：量化 V cache 必须开 flash-attn，否则 llama-server 启动即拒
# （``V cache quantization requires flash_attn``）。
_UNQUANTIZED_CACHE_TYPES = {"f16", "bf16", "f32"}


def _needs_flash_attn(args: list[str]) -> bool:
    """argv 中 K/V cache 类型是否有量化档（长/短 flag 都认）。"""
    for flag in ("--cache-type-k", "-ctk", "--cache-type-v", "-ctv"):
        value = _flag_value(args, flag)
        if value and value.lower() not in _UNQUANTIZED_CACHE_TYPES:
            return True
    return False


class LlamaCppAdapter(EngineAdapter):
    """llama.cpp 适配器（GGUF）。"""

    supported_formats = ["gguf"]
    supported_gpu_archs = ["nvidia", "cpu"]
    # 多模态：GGUF 仓库带 mmproj 视觉投影器时由 ``--mmproj`` 启用（见 generate_start_args）
    supported_categories = [ModelType.LLM, ModelType.MULTIMODAL, ModelType.EMBEDDING]
    supported_operations = ["chat.completions", "embeddings"]
    supported_parallel = []
    # 单机引擎：无 rendezvous / 无集合通信，多机（nnodes>1）恒拒（validate_cross_node）
    supports_multinode = False
    # 多卡走 layer 切分（见模块 docstring）：权重按层整块分派，不按 attention head 切，
    # 因此 heads % tp == 0 不是硬约束——按 vLLM 口径校验会把能跑的部署拦住
    tp_splits_by_heads = False
    supported_features = []
    deployable = True
    # 系统必控 flag：身份/监听地址由编排器给（用户 extra_args 同名会被忽略）——**必须包含
    # ``--model``/``--alias``**：前端把预检回显的整条命令（含 ``<model-N>`` 占位路径）
    # 当作用户参数回传，不拦住就会用占位路径覆盖真实权重路径，容器启动即 GGUF 打开失败。
    # ``--n-gpu-layers`` / ``--no-kv-offload`` **不在此列**——系统只给两端（0=纯 CPU、999=全量
    # 上卡），KV 落点也由引擎默认（随权重）；要混合层数或 KV 下沉，用户自己在部署参数里加。
    protected_flags = ["--model", "--host", "--port", "--alias"]
    type_to_task = {
        ModelType.LLM: "generate",
        ModelType.MULTIMODAL: "generate",  # VLM 走 chat/completions（图像输入），与 LLM 同任务
        ModelType.EMBEDDING: "embed",
    }

    # KV cache 精度支持矩阵：前端 Step2 高级参数下拉 + 显存估算折算同源。
    # llama.cpp 用 --cache-type-k/v 成对设置（K/V 类型不一致会退掉融合 FA 路径），量化 V
    # 还依赖 --flash-attn——依赖 flag 一并放进选项 args，用户只选一次即可。
    # 反解主 flag 取 K 侧长名（系统下发的选项一律用它；K/V 由本表成对下发，取一侧即代表
    # 档位）；短名 -ctk 一并列入，用户手写 `-ctk q4_0 -ctv q4_0` 时同样命中，不退成 f16 高估。
    kv_cache_dtype_flags = ["--cache-type-k", "-ctk"]
    kv_cache_dtype_options = [
        KVCacheDtypeOption(value="", label="默认(F16)", hint="KV 不量化，不额外传参"),
        KVCacheDtypeOption(
            value="q8_0",
            label="Q8_0",
            dtype_bytes=1.0,
            args=["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--flash-attn"],
            hint="KV 约减半；质量损失可忽略，量化 V 需 flash-attn（已一并加入）",
        ),
        KVCacheDtypeOption(
            value="q4_0",
            label="Q4_0",
            dtype_bytes=0.5,
            args=["--cache-type-k", "q4_0", "--cache-type-v", "q4_0", "--flash-attn"],
            hint="KV 约四分之一；长上下文精度损失较明显，量化 V 需 flash-attn（已一并加入）",
        ),
    ]

    entrypoint = None  # 镜像自带 llama-server

    # 容器/服务默认值
    default_port = 8080
    default_network_mode = "host"  # 端口直绑宿主（build_run_spec 不映射 ports），healthcheck 可达

    @classmethod
    def build_device_config(cls, instance: InstanceSpec, gpu_arch: str) -> DeviceConfig:
        if gpu_arch == "nvidia":
            return nvidia_device_config(instance)
        if gpu_arch == "cpu":
            # 纯 CPU：不申请任何 GPU 设备。必须显式给 gpus=None（[] 在 Node 侧等于「全部 GPU」）
            return DeviceConfig(gpus=None)
        raise NotImplementedError(f"llamacpp 不支持的软件栈：{gpu_arch}")

    @classmethod
    def build_env(
        cls,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: EngineConfig,
        health_timeout_s: int = 600,
    ) -> dict:
        # GGUF 本地文件：禁 hub phone-home（部分 config 字段会触发网络请求）
        return {"HF_HUB_OFFLINE": "1"}

    def generate_start_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        *,
        engine_version: str | None = None,
    ) -> list[str]:
        port = int(instance.port or self.default_port)
        model_path = instance.model_path
        served = instance.served_model_name or model_path

        args: list[str] = [
            "--model",
            str(model_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--alias",
            str(served),
        ]

        # 上下文长度（影响 KV/内存占用 + llama-server --ctx-size）
        if topology.max_model_len:
            args += ["--ctx-size", str(int(topology.max_model_len))]

        # GPU 相关参数只在确实用 GPU 时下发：纯 CPU 部署（gpu_arch=cpu）不传 --n-gpu-layers
        # ——无卡部署里该 flag 没有语义。
        if instance.gpu_arch != "cpu":
            # GPU offload 层数：系统只给两端（0=纯 CPU、NGL_ALL=全量上卡，版本无关的大数
            # 写法——llama.cpp 各版本都把大数钳到全部层，而精确层数语义跨版本不一致）。
            # 层数由编排器按部署形态盖章；未盖章的老行（盖章前的 spec）按有无卡兜底。
            ngl = instance.offload_layers
            if ngl is None:
                ngl = NGL_ALL if instance.assigned_gpus else 0
            args += ["--n-gpu-layers", str(max(0, int(ngl)))]

        # 视觉投影器（GGUF 多模态）：与主模型同目录下发；不带则视觉输入不可用
        if instance.mmproj_path:
            args += ["--mmproj", str(instance.mmproj_path)]

        # embedding 模型开 embedding 端点
        task = instance.task
        if self.category_for_task(task) == ModelType.EMBEDDING:
            args.append("--embedding")

        # 用户额外参数（llama-server 无被系统覆盖的必控项，直接追加）
        return args + list(engine_params.get("extra_args") or [])

    def assemble_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        image_entry,
        engine_params: dict,
    ) -> list[str]:
        """三层合并前兜底补 flash-attn：量化 V cache 不配它 llama-server 起不来。

        选项 args 已自带该 flag，这里兜的是用户在部署参数里手写/手改量化档的情形
        （如只敲了 ``--cache-type-k q8_0``）。在合并前补齐，inspect 回显与真实部署同源。
        """
        params = dict(engine_params or {})
        extra = list(params.get("extra_args") or [])
        if _needs_flash_attn(extra) and not has_flag(extra, "--flash-attn"):
            params["extra_args"] = extra + ["--flash-attn"]
        return super().assemble_args(instance, topology, image_entry, params)
