"""引擎适配器基类。

适配器是 arch/引擎知识的唯一载体：Manager 适配器把 instance/topology
翻译成 docker_run spec（args/env/device_config/image），Node 只做通用执行。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field, computed_field

from llmpod.apps.manager.deployment.schemas import (
    DeviceConfig,
    EngineConfig,
    InstanceSpec,
    Topology,
)
from llmpod.apps.manager.deployment import parallel
from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.resource_ledger import DEFAULT_GPU_MEMORY_UTILIZATION
from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError

# —— 所需显存估算（feasibility + estimate_vram_gb 同源，保证 inspect/预检/部署口径一致）——
# 架构元数据齐备时走 KV 真公式，缺失时退回「权重的 KV_BASE_RATIO 倍 @KV_BASE_CONTEXT」启发式
# （常量与计算都在 deployment/resource.py，调用方直接从那里导入）。


def estimate_required_vram(
    model_size_gb: float | None,
    context_size: int | None,
    has_kv_cache: bool = True,
    *,
    dtype_bytes: float | None = None,
    arch_meta: resource.KVArchMeta | None = None,
    n_parallel: int | None = None,
) -> float | None:
    """所需显存 = 权重 + KV_cache(context_size)（全量 offload、KV 落显存的特例口径）。

    - ``model_size_gb=None``（未知）→ None（跳过显存判定）；
    - ``context_size=None``（inspect 时未知）→ KV=0，乐观裸权重；
    - deploy 时 context 已知 → 加 KV，避免长上下文跑起来被拒；
    - ``has_kv_cache=False``（embedding/rerank 等非自回归类别）→ KV 恒 0：
      按 `ModelType.has_kv_cache` 判定，此类模型只有权重没有 KV cache；
    - KV 精度按 ``dtype_bytes``（KV 元素字节数，未给按 f16）折算，收敛到
      :func:`resource.kv_cache_gb`。
    """
    if model_size_gb is None:
        return None
    kv = resource.kv_cache_gb(
        model_size_gb,
        context_size,
        has_kv_cache=has_kv_cache,
        dtype_bytes=dtype_bytes if dtype_bytes is not None else resource.DEFAULT_KV_DTYPE_BYTES,
        arch_meta=arch_meta,
        n_parallel=n_parallel,
    )
    return round(model_size_gb + kv, 2)


def _flag_value(args: list[str], flag: str) -> str | None:
    """取扁平 argv 中 ``--flag value`` / ``--flag=value`` 的值；未出现返回 None。

    同名 flag 多条时取**末条**——与 argparse/``StartArgs`` 的生效规则一致（后写覆盖），
    账本预留与真实命令行必须读到同一个值，否则「账本一个数、引擎另一个数」。
    """
    if not flag:
        return None
    value: str | None = None
    for i, tok in enumerate(args):
        if not isinstance(tok, str):
            continue
        if tok == flag:
            value = args[i + 1] if i + 1 < len(args) else None
        elif tok.startswith(flag + "="):
            value = tok[len(flag) + 1 :]
    return value


def parse_memory_utilization(extra_args: list[str] | None, flag: str) -> float | None:
    """从扁平 extra_args 解析显存占比 flag 的值（``--flag 0.5`` 或 ``--flag=0.5``）。

    flag 为空 / 未出现 / 非数值 / 越界（≤0 或 >1）一律返回 None（按未提供处理，回退默认）——
    显存占比是首要依据，越界值宁可回退默认也不污染账本预留。
    """
    raw = _flag_value(list(extra_args or []), flag)
    if raw is None:
        return None
    try:
        f = float(raw)
    except (ValueError, TypeError):
        return None
    return f if 0.0 < f <= 1.0 else None


class ParamSuggestion(BaseModel):
    """引擎推荐参数（前端预设 chip 来源：用户可一键加到 extra_args）。"""

    name: str = Field(..., description="参数名，如 --gpu-memory-utilization")
    hint: str = Field("", description="用途说明")
    default: str | None = Field(None, description="默认值（无则空）")
    choices: list[str] = Field(default_factory=list, description="可选值（空表=自由输入）")


class KVCacheDtypeOption(BaseModel):
    """KV cache 精度选项（引擎支持矩阵的唯一定义处）。

    前端 Step2「高级参数」下拉直接渲染本表；后端估算按 ``dtype_bytes`` 折算 KV 项——
    选项表、下发 flag、估算系数同源，改一处三处同步。

    - ``value``：选项值（前端 v-model）；空串表示「默认不量化」，不参与 flag 反解匹配；
    - ``args``：选中后写入 ``engine_params.extra_args`` 的原生 flag 序列（llama.cpp 的
      K/V 成对设置与 flash-attn 依赖一并给出，避免用户只改一半）；
    - ``dtype_bytes``：KV 元素字节数（f16/bf16 = 2、fp8/q8_0 = 1、q4_0 = 0.5），KV 真公式
      与启发式兜底都读它；``kv_ratio`` 是它相对 f16 的派生倍率（仅前端标注用）；
    - ``hint``：选项级说明（精度/兼容性提示，前端作副文案）。
    """

    value: str = Field(..., description="选项值（空串=默认不量化）")
    label: str = Field(..., description="展示名")
    dtype_bytes: float = Field(resource.DEFAULT_KV_DTYPE_BYTES, description="KV 元素字节数")
    args: list[str] = Field(default_factory=list, description="选中后写入 extra_args 的原生 flag 序列")
    hint: str = Field("", description="选项说明")

    @computed_field(description="KV 占用倍率（相对 f16，由 dtype_bytes 派生）")
    @property
    def kv_ratio(self) -> float:
        """KV 占用倍率（相对 f16）——由 dtype_bytes 派生，不单独维护。"""
        return round(self.dtype_bytes / resource.DEFAULT_KV_DTYPE_BYTES, 4)


class EngineAdapter(ABC):
    """推理引擎适配器基类。子类声明能力 + 实现参数生成。"""

    # ---- 能力声明（前置校验 + 镜像选择用）----
    # 注册名（image_catalog 的引擎键）：由 engine 注册表在 get_adapter 时注入，子类不自行声明
    # ——两处各写一个字符串迟早漂移，而漂移的后果是镜像查不到（架构被静默剔出回落序）。
    engine_name: str = ""
    supported_formats: list[str] = []  # 权重格式：safetensors / gguf / ...
    supported_gpu_archs: list[str] = []  # 软件栈：nvidia / cann / cpu
    supported_categories: list[str] = []  # llm / embedding / rerank / image / stt / tts（= Model.model_type）
    supported_operations: list[str] = []  # chat.completions / embeddings / ...
    supported_parallel: list[str] = []  # tp / pp / ep
    # 是否支持多机部署（nnodes>1）。默认 False（fail-closed）：多机是拉起级能力，新引擎
    # 忘了声明不该被静默放行——与 supported_parallel 的空表默认同极性。单机引擎无需额外
    # 覆写（validate_topology 的 nnodes>1 分支按本声明拒）；引擎知识（各自分摊语义）见
    # validate_cross_node 钩子。
    supports_multinode: bool = False
    # tp 是否按 attention head 切分权重（决定 ``heads % tp == 0`` 是不是硬约束）。vLLM/SGLang
    # 系按 head 切；llama.cpp 走 layer 切分，多卡不要求整除——照 vLLM 口径拦会误拒能跑的部署。
    tp_splits_by_heads: bool = True
    # 信任远程代码的 flag（None = 该引擎没这个概念）。模型带 config auto_map 时注入它才能加载；
    # 不注入则引擎启动期直接报错，属于部署前可预告的一类失败。
    trust_remote_code_flag: str | None = None
    supported_features: list[str] = []  # pd / speculative / ...
    # 参数生成是否已实现（占位引擎置 False）：拓扑/inspect 据此判断引擎是否真正可部署
    deployable: bool = True
    # 系统必控 flag：用户 extra_args 中同名 flag 会被忽略（fill_absent_args / merge_start_args
    # 跳过）并告警，列在这里供前端提示 + create 校验。与 generate_start_args 硬编码集保持一致（防漂移）。
    protected_flags: list[str] = []
    # 推荐参数（前端预设 chip）；仅放非 protected 的自由参数（protected 项由结构字段控制）。
    param_suggestions: list[ParamSuggestion] = []
    # KV cache 精度支持矩阵（前端 Step2 高级参数下拉 + 估算折算同源）。首个为默认不量化档；
    # 引擎无此能力时留空表（前端不渲染该控件）。
    kv_cache_dtype_options: list[KVCacheDtypeOption] = []
    # 本引擎 KV 精度档位所在的 flag「主写法在前」：与 kv_cache_dtype_options[].value 配对，
    # 用于从 extra_args 反解生效档（如 vLLM/SGLang 的 --kv-cache-dtype；llama.cpp 的
    # --cache-type-k 及其短名 -ctk）。系统下发只用第一个，其余是用户手写等价写法时的兜底。
    kv_cache_dtype_flags: list[str] = []
    # model_type → 引擎 task（引擎自己的词汇，非跨引擎统一）：vLLM rerank→classify，SGLang rerank→rerank。
    # 编排器据此填 instance.task → 引擎启动参数 --task。generate 为默认（无需显式传）。
    type_to_task: dict[str, str] = {}

    # ---- 派生能力（通用层只问这些，不写引擎名/格式字面量）----

    @classmethod
    def supports_cpu_lane(cls) -> bool:
        """是否支持 CPU / 内存部署（``allow_cpu``）：权重可拆到内存、可纯 CPU 部署。

        由 ``supported_gpu_archs`` 含 ``cpu`` 派生——声明了 cpu 软件栈即声明了这条部署路径
        （llama.cpp 支持，vLLM/SGLang 不支持）。路由准入、部署形态求解、预检分支都以此为准。
        """
        return "cpu" in (cls.supported_gpu_archs or [])

    @classmethod
    def offload_gpu_archs(cls) -> list[str]:
        """逐层 offload / CPU 部署评估里计入显存的卡架构（排除 ``cpu`` 伪架构）。

        llama.cpp → ``["nvidia"]``：它用不了 cann 卡，评估时只累加 nvidia 显存。
        """
        return [arch for arch in (cls.supported_gpu_archs or []) if arch != "cpu"]

    # None=沿用镜像 ENTRYPOINT；SGLang 等无 ENTRYPOINT 的必须显式传
    entrypoint: list[str] | None = None

    # 多机分片启动顺序（reconciler 消费，不硬编码）：head_first（默认——head 当
    # rendezvous 服务端，workers 连它）| workers_first（特殊引擎用：head 启动即注册
    # 全部 workers 地址，须 workers 先就位）。改动启动门控语义时读此属性。
    startup_order: str = "head_first"

    # ---- 容器/服务默认值（子类按引擎覆盖；None/0=不设置，避免把 vLLM 默认泄漏到其它引擎）----
    default_port: int = 0  # 服务端口（vLLM 8000）
    default_master_port: int = 0  # 多机 rendezvous 端口（vLLM 29501）
    default_network_mode: str | None = None  # 容器网络模式（vLLM host）
    default_ipc: str | None = None  # IPC（vLLM host）
    default_shm_size: str | None = None  # /dev/shm（vLLM 16g）
    default_restart_policy: str | None = None  # 容器重启策略（vLLM no）
    default_nccl_ifname: str = "eth0"  # 多机分布式通信网卡名最后兜底（真实取值优先级：用户 engine_config.nccl_ifname > 所在节点上报的网卡名 > 本值）
    # 引擎默认显存利用率（vLLM --gpu-memory-utilization / SGLang --mem-fraction-static 的出厂值）。
    # 用户未设引擎 flag 时，账本按 total×此值整卡预留（独占），inspect 回显此值给前端作默认。
    # 与账本容量上限同源（resource_ledger.DEFAULT_GPU_MEMORY_UTILIZATION，留 10% 给 CUDA
    # context）——唯一定义处，改一处同步。
    default_gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION
    # 本引擎「显存占比」flag 名（用户熟悉的引擎参数，作为显存占比取值依据；空串=引擎无此 flag）
    memory_utilization_flag: str = ""
    # 本引擎对外协议（生成一个 ModelEndpoint 行/协议；权威来源——多协议支持改这里，不动 DB）。
    # anthropic 仅 generate 类模型有效（引擎侧 /v1/messages 挂在 chat serving 上），
    # embedding/rerank 等 pooling 服务无 anthropic 面。
    supported_protocols: list[str] = []
    default_protocol: str | None = None  # 首个端点行的 protocol（openai）

    @classmethod
    def protocols_for(cls, model_type: str) -> list[str]:
        """模型类别实际可用的协议集：supported_protocols 按类别裁剪。

        anthropic 面（/v1/messages）只在生成类模型上存在（ModelType.is_generative
        唯一定义处）；其余类别仅 openai（embeddings/rerank 等无 anthropic 兼容层）。
        """
        if "anthropic" in cls.supported_protocols and not ModelType.is_generative(model_type):
            return [p for p in cls.supported_protocols if p != "anthropic"]
        return list(cls.supported_protocols)

    @property
    def default_arch(self) -> str:
        """arch 兜底（唯一定义处）：取首个 supported_gpu_archs，无则 nvidia。"""
        return self.supported_gpu_archs[0] if self.supported_gpu_archs else "nvidia"

    @property
    def arch_order(self) -> list[str]:
        """arch 试探顺序（默认 arch 优先，其余受支持 arch 按声明序）。

        tp 推导 / 目标 arch 解析 / 推荐选卡共用同一优先级——按它排序推导，
        解析 / 推荐按它回退（默认 arch 无合格卡时退回其它受支持 arch）。
        """
        return [self.default_arch] + [a for a in self.supported_gpu_archs if a != self.default_arch]

    def topology_conflict(self, arch: str, topology) -> str | None:
        """该架构服务不了这套拓扑时返回原因（默认无冲突，引擎按自身限制覆盖）。

        两个用途同一判据：自动回落序据此剔除候选（``schedulable_archs``），手选/hand-picked
        路径据此给出人读原因。例：vllm-ascend 的 pp>1 直接拒（见 ``_ascend_args``）——不拦
        的话副本会被调度落上去，等容器阶段才报错（调度/账本/引擎三方口径分裂）。
        """
        return None

    def assert_arch_serves_topology(self, arch: str, topology) -> None:
        """:meth:`topology_conflict` 的 raise 版：冲突即抛「所选卡架构不支持当前拓扑：…」。

        create 路由（同步 422）、编排器 ``_plan``（异步兜底）、inspect 手选校验（捕获
        转 c.fail）三处共用——判据与文案的唯一定义处，改一处全链路同步。
        """
        conflict = self.topology_conflict(arch, topology)
        if conflict is not None:
            raise LLMPodValidationError(f"所选卡架构不支持当前拓扑：{conflict}")

    def schedulable_archs(
        self,
        topology=None,
        *,
        gpu_pool=None,
        engine_version: str | None = None,
        explicit_image: str | None = None,
        must_include: str | None = None,
    ) -> list[str]:
        """可调度架构序：``arch_order`` 收窄到「本拓扑服务得了 + 池内确有匹配镜像的卡」。

        建部署 / 在线扩容 / 预检共用同一入口——三处各写一遍回落序必漂移（预检说能落
        cann、create 落不到）。``gpu_pool`` 为空（未采集 / 无卡）时只做拓扑过滤。

        ``explicit_image``（用户固定镜像）时只保留 ``must_include``（缺省 = 默认架构）：
        catalog 无从判断这个镜像能在哪些架构上跑，跨架构回落在 cann 上拉起 CUDA 镜像会
        「容器起来、引擎初始化崩、NPU 还被占着」。要跨架构就用「手选卡」（部署期按所选卡
        arch 推断）。

        ``must_include`` 是给**已有部署的扩容/预检**用的：镜像已经在本部署的 arch 上跑起来了，
        那个 arch 就是唯一确定的答案——套用「只留默认架构」会让固定 cann 镜像的部署永远扩不
        了容（拿 nvidia 找卡，池里全是 NPU）。传本部署当前 arch 即可。**未固定镜像时它不参
        与**：镜像由 catalog 按 arch 解析，新副本落在哪个架构都有对应镜像，跨架构扩容
        （nvidia 份 + cann 份）是允许的形态。
        """
        order = [a for a in self.arch_order if self.topology_conflict(a, topology) is None]
        if explicit_image:
            pinned = must_include or self.default_arch
            return [a for a in order if a == pinned]
        if not gpu_pool:
            return order
        from llmpod.apps.manager.deployment.assess import archs_with_image  # 循环导入：assess 依赖本模块

        # explicit_image 分支已提前返回，此处恒为 None——不传，避免读代码的人以为
        # 「固定镜像时由 catalog 决定架构」（实际判据是上面的 pinned）
        usable = archs_with_image(
            gpu_pool,
            engine=self.engine_name,
            engine_version=engine_version,
        )
        return [a for a in order if a in usable]

    # ---- overridable 钩子（默认实现，子类按需覆盖）----

    @classmethod
    def validate_category(cls, model_type: str) -> None:
        """校验模型类别（category = Model.model_type）被本引擎支持；不支持 raise LLMPodValidationError。

        路由层（create_deployment）与编排层（_plan）共用此校验，避免同一业务规则两处手写漂移
        （历史：路由抛 LLMPodValidationError→422，编排抛 LLMPodError→400，消息体重复）。
        错误文案取枚举值（'llm' 等）而非 Python repr（``<ModelType.LLM: 'llm'>``），
        对外信息只给用户认识的词表。
        """
        if model_type not in cls.supported_categories:
            supported = ", ".join(str(c) for c in cls.supported_categories)
            raise LLMPodValidationError(
                f"引擎 {cls.engine_name or cls.__name__} 不支持模型类别 {str(model_type)!r}（支持：{supported}）"
            )

    @classmethod
    def task_for(cls, model_type: str) -> str:
        """model_type → 引擎 task（未声明 → generate，vLLM/SGLang 默认生成）。"""
        return cls.type_to_task.get(model_type, "generate")

    @classmethod
    def category_for_task(cls, task: str) -> str | None:
        """引擎 task → model_type（type_to_task 反查；非本引擎词汇 → None）。

        启动参数生成分支按**模型类别**判断（ModelType.has_kv_cache 等类别级判定），
        而非引擎本地 task 词——task→类别经此反查，类别级语义变化不动各引擎词表。
        """
        for model_type, t in cls.type_to_task.items():
            if t == task:
                return model_type
        return None

    @classmethod
    def build_device_config(cls, instance: InstanceSpec, gpu_arch: str) -> DeviceConfig:
        """arch-keyed DeviceConfig：runtime/gpus/driver_mounts/privileged/cap_add。

        gpus 为宿主设备索引列表（[] 空表=全部 GPU）；由 Deployer 翻译成 runtime 的设备请求。
        子类按 arch 返回 :class:`DeviceConfig`；默认 raise（强制子类实现）。
        """
        raise NotImplementedError(f"{cls.__name__}.build_device_config not implemented for {gpu_arch}")

    @classmethod
    def health_check(cls) -> tuple[str, Callable[[Any], bool]]:
        """返回 (path, predicate)。默认 /health 200（vLLM 对所有 task 一致）。"""
        return "/health", lambda r: getattr(r, "status_code", 0) == 200

    @classmethod
    def expand_topology(cls, topology: Topology, cluster_view: dict | None = None) -> list[InstanceSpec]:
        """拓扑展开（默认 standard：逐份消费各自 tp/pp/nnodes，每 replica 一个 instance）。

        llama.cpp 等非对称异构覆盖此方法。透传 ``cls`` 给结构校验（跨机整除钩子）。
        """
        from llmpod.apps.manager.deployment.scheduler.topology import expand_standard

        return expand_standard(topology, adapter=cls)

    @classmethod
    def build_env(
        cls,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: EngineConfig,
        health_timeout_s: int = 600,
    ) -> dict:
        """构建容器环境变量（NCCL + HF_ENDPOINT 等）。子类覆盖。

        ``health_timeout_s`` 由编排器从 ``DeploymentConfig.health_check.timeout_seconds`` 传入，
        与引擎 ready timeout 联动；``engine_config`` 默认值兜底（如 nccl_ifname）在本方法内做
        （唯一定义处），调用方只透传原始值。
        """
        return {}

    @classmethod
    def validate_cross_node(cls, tp: int, pp: int, nnodes: int) -> None:
        """跨机整除校验钩子（``validate_topology`` 逐份调用；非法 raise LLMPodValidationError）。

        默认保守：tp 整除 nnodes（tp 为唯一跨机分摊维度）。引擎语义不同时覆盖——如
        vLLM torch-elastic 允许 pp 参与分摊（每节点 tp×pp÷nnodes 卡）。单机引擎不用
        覆盖本钩子：``validate_topology`` 的 nnodes>1 分支先按 ``supports_multinode``
        声明拒（声明与结构校验共享同一事实，钩子只管各引擎的分摊算术）。
        """
        if tp % nnodes != 0:
            raise LLMPodValidationError(f"topology.tp({tp}) 必须能被 nnodes({nnodes}) 整除（tp 为唯一跨机分摊维度）")

    def supports_multinode_at(self, engine_version: str | None) -> bool:
        """引擎声明 ∧ 所固定镜像版本的多机词表（版本未收录 = 按最新词表，不降档）。

        inspect 回显（前端 nnodes 门控）与 vLLM 旧镜像组装期硬拒共用同一判据——
        两处各写一份迟早漂成「显示放行、组装才拒」。实例方法（``engine_name`` 由
        注册表注入到实例，classmethod 拿不到）。
        """
        from llmpod.apps.manager.deployment.image_catalog import engine_vocab

        if not self.supports_multinode:
            return False
        vocab = engine_vocab(self.engine_name, engine_version)
        return vocab is None or vocab.multinode

    def supported_parallel_at(self, engine_version: str | None) -> list[str]:
        """该引擎版本的有效并行词表：镜像词表判为不存在的项剔除（如 SGLang 0.4.2/0.4.6
        无 --pp-size）。inspect 的 pp 门控与 SGLang 组装期硬拒同源——否则前端绿灯、
        提交后烧完下载和卡租约才在组装期炸。版本未收录 = 按最新全保留。
        """
        from llmpod.apps.manager.deployment.image_catalog import engine_vocab

        vocab = engine_vocab(self.engine_name, engine_version)
        if vocab is None or vocab.pp:
            return list(self.supported_parallel)
        return [p for p in self.supported_parallel if p != "pp"]

    @classmethod
    def validate_parallel(
        cls, topology: "Topology", *, architecture: str | None = None, model_type: str | None = None
    ) -> None:
        """逐份校验并行词汇被本引擎声明支持（``supported_parallel`` 白名单）。

        pp>1 且未声明 "pp" → raise（创建路由 422 早拒 + 编排兜底，force 可跳过——
        引擎实际接受与否由引擎最终裁决）。只校验已知并行度；其它自由参数透传引擎
        报错，不做白名单围栏。

        ``architecture``/``model_type``：模型侧信息，用于 pp 的模型级门槛
        （``parallel.pp_reject_reason``，只做负向排除，判断不了就放行）。模型级原因先报——
        它更具体、且在引擎将来开放 pp 之后依然成立；引擎未声明只是「还没测」。
        """
        for seq, u in enumerate(topology.per_copy_units()):
            pp = max(1, int(u.pp or 1))
            if pp <= 1:
                continue
            tag = f"（第 {seq + 1} 份）" if not topology.is_homogeneous else ""
            reason = parallel.pp_reject_reason(pp, architecture=architecture, model_type=model_type)
            if reason:
                raise LLMPodValidationError(f"{tag}{reason}；确要尝试请 force=true（由引擎裁决）")
            if "pp" not in cls.supported_parallel:
                raise LLMPodValidationError(
                    f"引擎 {cls.__name__} 未声明支持流水线并行{tag}：pp={pp}，"
                    f"声明的能力为 {cls.supported_parallel or ['(无)']}；确要尝试请 force=true（由引擎裁决）"
                )

    @classmethod
    def estimate_vram_gb(
        cls,
        size_gb: float | None,
        topology: Topology,
        model_type: str | None = None,
        *,
        dtype_bytes: float | None = None,
        arch_meta: resource.KVArchMeta | None = None,
        n_parallel: int | None = None,
    ) -> float:
        """首份单卡所需显存（per-gpu）= (权重 + KV_cache(max_model_len)) / (tp × pp)。

        与 ``feasibility.assess_feasibility`` 同源 ``estimate_required_vram``，保证
        inspect/预检/部署口径一致。TP 与 PP 下权重与 KV 均按 tp×pp 分摊（PP 把层切成
        pp 段、每段 tp 卡，总卡数=tp×pp）。``model_type`` 已知且该类别无 KV cache
        （`ModelType.has_kv_cache`，如 embedding/rerank）时 KV 恒 0；KV 精度按
        ``dtype_bytes`` 折算。异构副本逐份口径用 :meth:`estimate_vram_gb_for_unit`。
        """
        return cls.estimate_vram_gb_for_unit(
            size_gb,
            topology.units[0],
            topology.max_model_len,
            model_type,
            dtype_bytes=dtype_bytes,
            arch_meta=arch_meta,
            n_parallel=n_parallel,
        )

    @classmethod
    def estimate_vram_gb_for_unit(
        cls,
        size_gb: float | None,
        unit,
        max_model_len: int | None,
        model_type: str | None = None,
        *,
        dtype_bytes: float | None = None,
        arch_meta: resource.KVArchMeta | None = None,
        n_parallel: int | None = None,
    ) -> float:
        """单份每卡显存（异构副本逐份口径）：(权重 + KV) / (该份 tp×pp)。

        ``unit`` 为 UnitTemplate（Topology.per_copy_units 的元素）；口径与
        :meth:`estimate_vram_gb` 同源（权重与 KV 均按 tp×pp 均摊，PP 激活内存偏差由
        显存占比默认 10% headroom 吸收）。
        """
        has_kv = ModelType.has_kv_cache(model_type)
        req = estimate_required_vram(
            size_gb,
            max_model_len,
            has_kv_cache=has_kv,
            dtype_bytes=dtype_bytes,
            arch_meta=arch_meta,
            n_parallel=n_parallel,
        )
        if req is None:
            raise LLMPodValidationError("estimate_vram_gb 缺少 size_gb（model.size_gb 为空），无法估算 VRAM")
        parallel = max(1, int(unit.tp or 1)) * max(1, int(unit.pp or 1))
        return round(req / parallel, 2)

    def kv_arch_inputs(self, model, engine_params: dict | None) -> dict:
        """KV 求解里「由引擎 + 模型决定」的两件输入：生效精度档字节数、架构形状。

        三个 KV 输入各有默认值，拼漏一项不会报错、只会静默退回默认口径（如漏精度档就按
        f16 算 KV），于是预检/预览/调度三处各算各的。集中在此装配，第三个输入
        （``n_parallel``）随调用场景不同，由调用方补。
        """
        return {
            "dtype_bytes": self.kv_cache_dtype_bytes_from(engine_params),
            "arch_meta": resource.KVArchMeta.from_model(model),
        }

    def vram_per_gpu(self, model, topology, *, engine_params: dict | None = None, kv_cache=None) -> float:
        """本部署配置下**每卡**显存需求（GiB）：KV 精度档、架构形状、并发序列数都从
        ``model`` + 部署配置推出，调用方不再逐项手拼。

        异构副本逐份口径用 :meth:`vram_per_gpu_by_copy`。
        """
        return self.estimate_vram_gb(
            model.size_gb,
            topology,
            model_type=model.model_type,
            n_parallel=getattr(kv_cache, "n_parallel", None),
            **self.kv_arch_inputs(model, engine_params),
        )

    def vram_per_gpu_by_copy(
        self, model, topology, *, engine_params: dict | None = None, kv_cache=None
    ) -> dict[str, float] | None:
        """异构副本**逐份**每卡显存需求（``{份序号: GiB}``）；同形返回 None（走单值口径）。

        同形副本各份 tp/pp 一致，用单值 :meth:`vram_per_gpu` 即可；异构各份不同，容量过滤
        与记账都得按各自份的口径。
        """
        if topology.is_homogeneous:
            return None
        # 两件输入与份序号无关，提到推导式外算一次（逐份重算只是白算）
        kv_inputs = self.kv_arch_inputs(model, engine_params)
        n_parallel = getattr(kv_cache, "n_parallel", None)
        return {
            str(i): self.estimate_vram_gb_for_unit(
                model.size_gb,
                unit,
                topology.max_model_len,
                model_type=model.model_type,
                n_parallel=n_parallel,
                **kv_inputs,
            )
            for i, unit in enumerate(topology.per_copy_units())
        }

    def raw_memory_utilization(self, engine_params: dict | None) -> float | None:
        """用户引擎 flag 的原始显存占比（未钳制）：未设 / 非法 / 越界返回 None。"""
        return parse_memory_utilization((engine_params or {}).get("extra_args") or [], self.memory_utilization_flag)

    def memory_utilization_from(self, engine_params: dict | None) -> float:
        """生效显存占比 = 用户引擎 flag（如 ``--gpu-memory-utilization``），未设则引擎默认。

        引擎自己的显存 flag 是唯一定义处（vLLM ``--gpu-memory-utilization`` /
        SGLang ``--mem-fraction-static``）；账本预留与注入 flag 均取它，不再另设系统字段。
        越界/非法值按未提供处理（回退默认）。
        """
        parsed = self.raw_memory_utilization(engine_params)
        if parsed is not None:
            # 账本预留上限 = 引擎默认占比（0.9，留 10% 给 CUDA context/framework）。用户显式
            # 调高（如 0.95）会让 GpuScheduler.card_fits 永远算不过（需 util×total > 账本容量
            # total×0.9）而报误导性「无可用卡」——按上限记账，引擎仍按其显式 flag 用满。
            return min(parsed, self.default_gpu_memory_utilization)
        return self.default_gpu_memory_utilization

    def effective_memory_utilization(self, engine_params: dict | None, stamped_values: list[float | None]) -> float:
        """存量实例在场时的生效显存占比（扩缩/重启预留与预览共用的单一口径）。

        取值序：config 引擎 flag（经 :meth:`memory_utilization_from`，未设给引擎默认）
        → 存量实例 spec 已落盘的 ``vram_percent``。后者优先级更高：引擎参数
        （``generate_start_args``）读的就是实例 spec 里的值，重启/扩容若按 config 重解，
        「引擎按 0.4 跑、账本按 0.9 记」两头分叉——同卡放不下假冲突、预留虚高挤掉
        别的部署。典型来源：规划期落盘、旧版 ``gpu_selector.vram_percent`` 历史配置。
        存量值有分歧（多副本各份不同）时维持 config 口径，不擅自选边。
        """
        util = self.memory_utilization_from(engine_params)
        stamped = {u for u in stamped_values if u}
        if len(stamped) == 1:
            util = next(iter(stamped))
        return util

    def kv_cache_dtype_bytes_from(self, engine_params: dict | None) -> float:
        """生效 KV 元素字节数 = 从 extra_args 反解命中的精度档（未量化/未命中 → f16 字节数）。

        KV 精度以引擎原生 flag 落 extra_args（与显存占比 flag 同模式，用户可在部署参数
        列表直接改），估算按 ``kv_cache_dtype_options`` 反查 ``dtype_bytes``。匹配的是「flag +
        选项值」而非整段 args——用户删掉附带的依赖 flag（如 llama.cpp 的 ``--flash-attn``）
        不该让估算口径漂移。``kv_cache_dtype_flags`` 列同一档位的全部写法（主名 + 短名），
        逐个试、多命中取**较大**字节数（自相矛盾的写法按保守侧算）；漏认等价写法会退成
        f16 高估。手写未收录取值（如 ``--cache-type-k q4_1``）同样回退 f16 字节数：
        宁可高估 KV，不可低估导致部署后 OOM。
        """
        flags = [f for f in self.kv_cache_dtype_flags if f]
        if not flags:
            return resource.DEFAULT_KV_DTYPE_BYTES
        extra = list((engine_params or {}).get("extra_args") or [])
        by_value = {o.value: o.dtype_bytes for o in self.kv_cache_dtype_options if o.value}
        hits = [by_value[v] for f in flags if (v := _flag_value(extra, f)) and v in by_value]
        return max(hits) if hits else resource.DEFAULT_KV_DTYPE_BYTES

    def kv_gb_for(
        self,
        model,
        engine_params: dict | None,
        *,
        context_size: int | None,
        n_parallel: int | None = None,
    ) -> float:
        """本模型在本引擎生效口径下的 KV cache 占用（GiB）。

        一次组装三件输入：模型类别（是否自回归）、本引擎反解的 KV 精度档、模型架构形状。
        调用方不再各自拼 :func:`resource.kv_cache_gb` 的参数——拼漏一个（如精度档或
        n_parallel）就会让部署形态求解、预检与逐卡估算口径漂移。
        """
        return resource.kv_cache_gb(
            model.size_gb,
            context_size,
            has_kv_cache=ModelType.has_kv_cache(model.model_type),
            n_parallel=n_parallel,
            **self.kv_arch_inputs(model, engine_params),
        )

    @abstractmethod
    def generate_start_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_params: dict,
        *,
        engine_version: str | None = None,
    ) -> list[str]:
        """生成容器启动参数（跟在 ENTRYPOINT 后）。子类必须实现。

        ``engine_version`` 是命中镜像条目的引擎版本：版本敏感的注入（解析器名、
        任务词表）据此分档——旧镜像缺新词表时降级，防启动期 KeyError/argparse 拒启。
        未知（None）按最新词表。
        """

    def assemble_args(
        self,
        instance: InstanceSpec,
        topology: Topology,
        image_entry,
        engine_params: dict,
    ) -> list[str]:
        """三层覆盖合并 args：image(floor) < system(adapter) < user(engine_params)。

        catalog 解析出的 image_entry 按 engine 交本适配器应用；protected
        系统必控 flag 免疫用户覆盖。各 adapter 默认用本实现，无需重写。

        system 层的版本敏感注入按 ``image_entry.engine_version`` 门控（自带镜像/
        条目无版本 → None → 最新词表）。
        """
        engine_version = image_entry.engine_version if image_entry else None
        system = self.generate_start_args(instance, topology, {}, engine_version=engine_version)
        image_args = (image_entry.params.get("args") if image_entry else []) or []
        user_args = list((engine_params or {}).get("extra_args") or [])
        return merge_start_args(image_args, system, user_args, set(self.protected_flags))

    def assemble_env(
        self,
        instance: InstanceSpec,
        topology: Topology,
        engine_config: "EngineConfig",
        image_entry,
        engine_params: dict,
        health_timeout_s: int = 600,
    ) -> dict:
        """三层覆盖合并 env：image < system(adapter.build_env) < user。"""
        image_env = (image_entry.params.get("env") if image_entry else {}) or {}
        system_env = self.build_env(instance, topology, engine_config, health_timeout_s)
        user_env = (engine_params or {}).get("env") or {}
        return {**image_env, **system_env, **user_env}


# ---- 启动参数三层覆盖（image < system < user）：解析成结构 → 字典合并 → 一次性渲染 ----
def _flag_name(token: str) -> str:
    """flag token 的名字维度：``--flag=value`` 归一成 ``--flag``（argparse 两种写法等价）。

    供 ``check_protected_overlap`` 等按 flag 名比对的调用方使用。
    """
    if token.startswith("-") and "=" in token:
        return token.split("=", 1)[0]
    return token


def has_flag(extra_args, flag: str) -> bool:
    """extra_args 里是否已带该 flag（按名字维度比，``--flag=value`` ≡ ``--flag``）。

    创建时补 flag（ensure_trust_remote_code）与 inspect 的「是否将自动加」提示
    共用，保证两处判定同口径——裸成员判断会把连写形态误报成「未给」。
    """
    return any(_flag_name(str(a)) == flag for a in extra_args or ())


def ensure_trust_remote_code(adapter: "EngineAdapter", model, engine_params: dict) -> dict:
    """模型声明 ``needs_trust_remote_code`` 且引擎有对应 flag 时，给 extra_args 补上它。

    自定义架构（config ``auto_map``，如 jina/mxbai 系 reranker）不给信任 flag 引擎
    必拒启（vLLM 架构注册表直接 ValueError），只在 inspect 提示会让这类模型永远
    部署不出去。模型行上的声明是 inspect 从 config 落库、且管理端可显式改写的
    授权信号——创建部署时据此落实；用户已在 extra_args 给出同名 flag 时不重复加。
    """
    flag = adapter.trust_remote_code_flag
    if not flag or not model.needs_trust_remote_code:
        return engine_params
    extra = [str(a) for a in (engine_params or {}).get("extra_args") or []]
    if has_flag(extra, flag):
        return engine_params
    return {**(engine_params or {}), "extra_args": extra + [flag]}


@dataclass
class StartArgs:
    """引擎启动参数的结构形态：flag 表 + 位置参数。

    - ``flags``：flag 名 → 值（``None`` = 无值开关）。同名只存一条——同层后写覆盖
      先写（与 argparse 生效规则一致），渲染出的 argv 无歧义；
    - ``positionals``：不带 ``-`` 的游离 token（如 ``vllm serve`` 的子命令位），按
      层序保留，渲染时置于 flag 之前（子命令型 CLI 要求它们在前）；
    - 已知局限：以 ``-`` 开头的值（负数等）无法与 flag 区分，须用 ``--top-k=-1``
      连写传。
    """

    flags: dict[str, str | None] = field(default_factory=dict)
    positionals: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, tokens: list[str]) -> "StartArgs":
        """token 流 → 结构。三个分支对应三种写法，缺一不可：

        - 位置参数（``vllm serve`` 的子命令位）；
        - 连写 ``--flag=v``：值直接取 ``=`` 后，**不前瞻**——这是传以 ``-`` 开头的值
          （如 ``--top-k=-1``）的唯一入口（若先展开成两 token，前瞻会把 ``-1`` 当
          flag 丢掉）；
        - 分写 ``--flag v`` / 无值开关：需要**单 token 前瞻**——下一个 token 不以
          ``-`` 开头才算值。

        没有可用的库替代：argparse/click/getopt 都是 schema 先行（须先声明哪些 flag
        带值；``parse_known_args`` 对未声明 flag 只回吐扁平列表，丢掉 flag→值配对），
        shlex 只做分词。schemaless 场景下「下一个 token 是不是值」只能靠前瞻猜，
        而前瞻不消费——iterator 做不到 peek，所以下标循环就是这个 LL(1) 解析器的
        最简形态。
        """
        args = cls()
        i = 0
        while i < len(tokens):
            token = str(tokens[i])
            if not token.startswith("-"):
                args.positionals.append(token)
                i += 1
                continue
            name, eq, inline = token.partition("=")
            if eq:
                args.flags[name] = inline
                i += 1
            elif i + 1 < len(tokens) and not str(tokens[i + 1]).startswith("-"):
                args.flags[name] = str(tokens[i + 1])
                i += 2
            else:
                args.flags[name] = None
                i += 1
        return args

    def absorb(self, other: "StartArgs", *, fill_only: bool = False, skip: set[str] = frozenset()) -> None:
        """把另一层并进来（就地）。flag 合并的唯一写点：

        - 覆盖（缺省）：同名以 other 为准——dict 重赋值不挪位，「替换保值置 / 新名尾
          追加」天然成立，无需手工挪 token；
        - 补缺（``fill_only``，floor 层用）：只填本层没设的 flag；
        - ``skip``：系统必控 flag，该层整条丢弃（用户层用）。

        位置参数不参与同名合并，按层序追加。
        """
        for name, value in other.flags.items():
            if name in skip or (fill_only and name in self.flags):
                continue
            self.flags[name] = value
        self.positionals.extend(other.positionals)

    def render(self) -> list[str]:
        """结构 → argv：位置参数在前，flag 按插入序，值统一分写 ``--flag v``
        （与 ``--flag=v`` 对 argparse 等价，不保留输入侧写法形态）。"""
        out = list(self.positionals)
        for name, value in self.flags.items():
            out.append(name)
            if value is not None:
                out.append(value)
        return out


def fill_absent_args(base_args: list[str], extra_args: list[str]) -> list[str]:
    """extra 只补 base 没设的 flag（base 优先）。

    vLLM/SGLang 的 ``generate_start_args`` 直用：系统 flag 全在 base 中，用户
    extra_args 同名一律不覆盖——这一层里系统全权决定（比 protected 更严）。
    """
    args = StartArgs.parse(base_args)
    args.absorb(StartArgs.parse(extra_args), fill_only=True)
    return args.render()


def merge_start_args(
    image_args: list[str], system_args: list[str], user_args: list[str], protected: set[str] | None = None
) -> list[str]:
    """三层覆盖：image(floor) < system < user；protected 系统必控 flag 免疫用户覆盖。

    同名判定按 flag 名字维度（``--flag=value`` ≡ ``--flag value``）。protected 命中
    的用户参数整条丢弃（系统真值不被覆盖；「被忽略了」的告警由
    ``check_protected_overlap`` 负责）。
    """
    args = StartArgs.parse(system_args)
    args.absorb(StartArgs.parse(image_args), fill_only=True)
    args.absorb(StartArgs.parse(user_args), skip=set(protected or ()))
    return args.render()


def nvidia_device_config(instance: InstanceSpec) -> DeviceConfig:
    """标准 nvidia DeviceConfig（vLLM/SGLang/llama.cpp 共用）。

    assigned_gpus = 宿主设备索引（空表=全部 GPU）；无驱动挂载/特权。

    runtime 显式固定 ``nvidia``（等价 ``docker run --runtime=nvidia``）：device_requests
    只负责把可见卡收窄到 assigned_gpus，驱动库/设备节点的注入靠运行时——部分节点只装了
    nvidia-container-runtime、没配 docker 的 device-request 钩子，光给 device_requests
    时引擎容器起来看不到卡（报无 CUDA 设备）。两者叠加即显式 runtime + 限定卡号。
    """
    return DeviceConfig(
        runtime="nvidia",
        gpus=list(instance.assigned_gpus or []),
        driver_mounts=[],
        privileged=False,
        cap_add=[],
    )


def hf_nccl_env(engine_config: EngineConfig, instance: InstanceSpec, default_nccl_ifname: str) -> dict[str, str]:
    """HF 离线 + 多机分布式 rendezvous 环境（vLLM/SGLang 共用；引擎特定变量由子类追加）。

    多机时注入 NCCL 与 Gloo 两个 SOCKET_IFNAME（同值同源）：NCCL 管数据通信，Gloo 管
    torch 分布式的 CPU 控制通信组——后者不注入会按 hostname 猜地址，容器内解析不出就
    回落 127.0.0.1，跨机 rank 互联连回环直接被拒（引擎 exit 1）。

    网卡名取值链在此收口（单一实现，两个适配器不再各自抄写）：
    用户 ``engine_config.nccl_ifname`` > 所在节点上报的网卡名
    （``instance.node_ifname``，持有 Node.ip）> ``default_nccl_ifname`` 兜底。
    系统注入值 NCCL 侧带 ``=`` 前缀强制精确匹配（默认前缀匹配会把 eth0:1 等子
    接口圈进来），用户显式值原样透传（可能自带 ``^`` 排除语法）。Gloo 只认单个
    裸接口名（列表/排除/IP 语法全不收），恒不加修饰。
    """
    env: dict[str, str] = {
        # 国内 huggingface.co 默认不可达；本地预置模型仍可能 phone-home 拉 config
        "HF_ENDPOINT": engine_config.hf_endpoint or "https://hf-mirror.com",
        "HF_HUB_OFFLINE": "1",
    }
    if instance.nnodes > 1:
        ifname = engine_config.nccl_ifname or instance.node_ifname or default_nccl_ifname
        env["NCCL_SOCKET_IFNAME"] = ifname if engine_config.nccl_ifname else f"={ifname}"
        env["GLOO_SOCKET_IFNAME"] = ifname
        env["NCCL_DEBUG"] = "INFO"
    return env
