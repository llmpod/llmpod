"""部署配置 typed model（DeploymentConfig 及其子结构，ModelDeployment.spec 的规范类型）。

ModelDeployment.spec 是 JSONB 扩展槽——本组 model 给它建立结构契约（extra='forbid'），
让拼写错误（如 extra_agrs→extra_args）在入口即被拒，而非静默丢弃到部署期才暴露。
写库时 ``model_dump()`` 进 JSONB；读出时 ``model_validate`` 归一（补默认值）。

**删字段的代价**：spec 是持久化的，且读出时会被重新校验（list/detail 响应、编排器重规划、
扩容读取）——直接删字段会让带该键的存量行 ``extra_forbidden`` 报错（接口 500 / 编排器抛错）。
删字段的正确做法是二选一：先做数据迁移清掉该键，或保留为「只读兼容占位」并注明。
KV 落点字段（``mode`` / ``ram_size`` / ``ram_ratio`` / ``chunk_size``）按前者处理：发布前
直接删，存量行随库重置一起清掉——保留占位只能救「只有它一个多余键」的行，而这些行往往是
整组一起写入的，清数据才是唯一的解法。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmpod.config.enums import ModelTag

# 拓扑形状键（explicit_fields 快照口径：两写法七键）
_TOPOLOGY_KEYS = frozenset({"units", "copies", "tp", "pp", "ep", "replicas", "nnodes"})


class UnitTemplate(BaseModel):
    """每份副本内的引擎模板（PD 分离时 P/D 各一个单元；v1 只放行单个 full 单元）。

    不加 ge 约束：非法值（0/负）原样透传，由 ``validate_topology`` 按结构错误统一拒
    ——pydantic 层先炸会把错误类型从 LLMPodValidationError 换成 ValidationError。
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field("full", description="引擎角色：full（单体）| prefill | decode（PD 槽位，v1 闸住）")
    tp: int = Field(1, description="本引擎张量并行度")
    pp: int = Field(1, description="本引擎流水线并行度")
    ep: int = Field(1, description="本引擎专家并行度（MoE 卡数乘数）")
    nnodes: int = Field(1, description="本引擎跨机节点数（>1 展开为多分片）")

    def gpus_per_engine(self) -> int:
        """本引擎总卡数（tp×pp×ep；跨机时分布在 nnodes 个节点，每节点 ÷nnodes）。"""
        return self.tp * self.pp * self.ep


class Topology(BaseModel):
    """部署拓扑（模板 × 份数）：``units`` 每份内的引擎模板列表 + ``copies`` 份数。

    三种写法（权威序：copies 列表 > units×int > 旧四数）：
    - ``copies=[模板, 模板]``：**异构权威写法**——每项 = 该份的单 full 单元模板，
      份数 = 列表长，各份形状（tp/pp/nnodes）可不同；同形列表（各项相等）归一为 int 形态。
    - ``units`` + ``copies=int``：同形模板写法（所有份共用一个模板）。
    - 旧四数（tp/pp/ep/replicas/nnodes）= 单 full 模板 × copies 的语法糖。

    归一化后三套字段全有值且互相一致：异构时 units/旧四数 = 首份模板折算（兼容
    旧读点，读逐份口径用 ``per_copy_units``）。两写法同时给出且不一致直接拒（静默
    取一边会让读库与 API 各见一半）。``nnodes`` 表示单个引擎（及旧写法下单个副本）
    跨的节点数：>1 时每引擎展开为 nnodes 个分片（head + headless workers，卡数均分
    到各节点，整除性由引擎钩子判定）。``mode`` / ``coordinate_mode`` /
    ``allow_cross_node`` 为历史预留语义，展开不消费。
    """

    model_config = ConfigDict(extra="forbid")

    units: list[UnitTemplate] | None = Field(
        None, description="每份内的引擎模板（同形写法；归一化后恒非空 = 首份模板，异构时仅作兼容回显）"
    )
    copies: int | list[UnitTemplate] | None = Field(
        None,
        description="份数：int = 同形份数（与 units 配对）；list = 异构权威写法（每项 = 该份的单 full 单元模板，份数 = 列表长）",
    )
    # 旧四数写法：字段保留 Optional（0/负值由 validate_topology 结构校验拒，pydantic 层
    # ge=1 会在 Topology(tp=4, nnodes=0) 这类「测结构校验本身」的用例前先炸成 ValidationError，
    # 错误类型就换了——归一 validator 把合法值兜到 ≥1，非法值原样透传给结构校验）
    tp: int | None = Field(None, description="张量并行度（旧写法；归一后 = 第一 full 单元 tp）")
    pp: int | None = Field(None, description="流水线并行度（旧写法）")
    ep: int | None = Field(None, description="专家并行度（旧写法，默认 1）")
    replicas: int | None = Field(None, description="副本数（旧写法；归一后 = copies）")
    nnodes: int | None = Field(None, description="每引擎跨节点数（旧写法；归一后 = 第一单元 nnodes）")
    max_model_len: int | None = Field(
        None,
        description="最大上下文长度（tokens）：影响 KV cache 估算 + vLLM --max-model-len；不传则由引擎默认",
    )
    # 历史预留（expand_standard 未消费）
    mode: str | None = Field(None, description="拓扑模式（预留，未消费）")
    coordinate_mode: str | None = Field(None, description="协调模式（预留，未消费）")
    allow_cross_node: bool | None = Field(None, description="是否允许跨节点并行（预留，未消费）")
    # 用户原始 payload 显式给出的拓扑键（validator 回填会让 model_fields_set 失真，
    # 「是否显式指定 tp」等判定读这里；程序化构造（无 dict 输入）为 None）
    explicit_fields: set[str] | None = Field(None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_units_copies(cls, data: Any) -> Any:
        """三种写法归一：copies 列表（异构权威）> units×int（同形模板）> 旧四数；冲突即拒。

        归一后 units/copies/tp/pp/ep/replicas/nnodes 全部有值且互相一致——异构时
        units/旧四数 = 首份模板折算（旧读点零改动，逐份口径读 ``per_copy_units``）；
        同形副本统一归一为 ``copies=int``（dump 形态与历史完全一致）。用 before 模式
        在原始 dict 上回填：after 模式里赋值会把派生字段标记进 ``model_fields_set``，
        破坏「用户是否显式指定 tp」的判定。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # 用户显式给出的键（回填前快照）——tp 推导守卫等判定用
        data["explicit_fields"] = {k for k in data if k in _TOPOLOGY_KEYS}
        # None 才兜 1；0/负值原样透传给 validate_topology 按结构错误拒（兜成 1 会把
        # nnodes=0 静默合法化——跨机部署被悄悄变单机）

        def _or_default(name: str) -> Any:
            v = data.get(name)
            return 1 if v is None else v

        copies_raw = data.get("copies")
        has_units = data.get("units") is not None

        # ---- 写法一：copies 为模板列表（异构权威，每项 = 该份的单 full 单元）----
        if isinstance(copies_raw, list):
            if not copies_raw:
                raise ValueError("copies 列表不能为空")
            entries = [u if isinstance(u, UnitTemplate) else UnitTemplate(**u) for u in copies_raw]
            if any(u.role != "full" for u in entries):
                raise ValueError("多单元 / prefill / decode 模板暂未开放（每份仅支持单个 full 单元）")
            head = entries[0]
            if has_units:
                units = data["units"]
                units = [u if isinstance(u, UnitTemplate) else UnitTemplate(**u) for u in units]
                # units 与异构写法并存只允许兼容回显（= 首份模板，即本类 dump 的回环形态）
                if units != [head]:
                    raise ValueError("copies 列表（异构写法）与 units 冲突：units 只能缺省或等于首份模板")
            data["units"] = [head]
            # 同形列表归一 int 形态（dump 与同形写法逐字节一致，异构保留列表）
            data["copies"] = len(entries) if all(u == head for u in entries) else entries
            implied = {"tp": head.tp, "pp": head.pp, "ep": head.ep, "replicas": len(entries), "nnodes": head.nnodes}
            for name, implied_v in implied.items():
                given = data.get(name)
                if given is not None and given != implied_v:
                    raise ValueError(f"copies 列表与旧四数写法冲突：{name} 应为 {implied_v}（首份折算），实给 {given}")
            for name, implied_v in implied.items():
                # None（未给/显式 null）都按折算值回填——setdefault 会让显式 null 穿透
                # （JS 端把可空旧键序列化成 null 很常见），破坏「归一后各字段全有值」的
                # 模块不变量（下游 DeploymentSummary(replicas: int) 读到 null 直接 500）
                if data.get(name) is None:
                    data[name] = implied_v
            return data

        # ---- 写法二：units × int copies（同形模板）/ 写法三：旧四数 ----
        has_copies = copies_raw is not None
        if has_units != has_copies:
            raise ValueError("units 与 copies 必须同时给出（模板化写法）或同时缺省（旧四数写法）")
        if has_units:
            units = data["units"]
            if not units:
                raise ValueError("units 不能为空列表")
            units = [u if isinstance(u, UnitTemplate) else UnitTemplate(**u) for u in units]
            if len(units) > 1 or any(u.role != "full" for u in units):
                raise ValueError("多单元 / prefill / decode 模板暂未开放（v1 仅支持单个 full 单元）")
            head = units[0]
            implied = {"tp": head.tp, "pp": head.pp, "ep": head.ep, "replicas": data["copies"], "nnodes": head.nnodes}
            # 只比对显式给出的旧字段（缺省 = 未提供，不构成冲突）
            for name, implied_v in implied.items():
                given = data.get(name)
                if given is not None and given != implied_v:
                    raise ValueError(f"units 与旧四数写法冲突：{name} 应为 {implied_v}（units 折算），实给 {given}")
            data["units"] = units
            for name, implied_v in implied.items():
                # None（未给/显式 null）都按折算值回填——setdefault 会让显式 null 穿透，
                # 破坏「归一后各字段全有值且一致」的模块不变量
                if data.get(name) is None:
                    data[name] = implied_v
        else:
            head = UnitTemplate(
                tp=_or_default("tp"), pp=_or_default("pp"), ep=_or_default("ep"), nnodes=_or_default("nnodes")
            )
            data["units"] = [head]
            copies = _or_default("replicas")
            data["copies"] = copies
            for name, implied_v in (
                ("replicas", copies),
                ("tp", head.tp),
                ("pp", head.pp),
                ("ep", head.ep),
                ("nnodes", head.nnodes),
            ):
                # None（未给/显式 null）都按折算值回填——setdefault 会让显式 null 穿透
                # （JS 端把可空旧键序列化成 null 很常见），破坏「归一后各字段全有值」
                # 的模块不变量（下游 DeploymentSummary(replicas: int) 读到 null 直接 500）
                if data.get(name) is None:
                    data[name] = implied_v
        return data

    @property
    def is_homogeneous(self) -> bool:
        """是否同形副本（copies 为 int，含全部历史形态）。异构（copies 为模板列表）时
        scale 拒绝、tp 推导跳过——逐份形状一律改读 ``per_copy_units``。"""
        return not isinstance(self.copies, list)

    def per_copy_units(self) -> list[UnitTemplate]:
        """逐份模板视图（异构副本权威口径）：copies 列表原样返回；同形 = 首单元 × copies。

        展开/校验/逐份显存/手选配对一律经此读逐份形状，不直接摸 copies 原始类型。
        """
        if isinstance(self.copies, list):
            return list(self.copies)
        return [self.units[0]] * max(1, int(self.copies or 1))

    def gpus_per_engine(self) -> int:
        """首份每引擎总卡数（tp×pp×ep）。异构副本各份不同——逐份口径用
        ``[u.gpus_per_engine() for u in per_copy_units()]``；本方法仅供同形读点。"""
        return self.units[0].gpus_per_engine()

    def total_gpus(self) -> int:
        """部署总卡数 = Σ 逐份每引擎卡数（异构副本各份累加；同形 = 每份卡数 × 份数）。"""
        return sum(u.gpus_per_engine() for u in self.per_copy_units())

    def with_updates(self, **changes: int) -> "Topology":
        """改写拓扑形状并保持两套字段一致（model_copy 直改单一字段会让 units 与旧四数漂移，
        dump 出的字典再被 model_validate 会按「冲突」拒）。

        只接受形状键（tp/pp/ep/nnodes/replicas/copies）；单位字段同步进第一 full 单元，
        份数同步两侧。不经 validator（改写只发生在已归一的对象上，键集受控）。
        异构副本（copies 列表）无「单一形状」可改——整体重建，不由本方法改写。
        """
        if not self.is_homogeneous:
            raise ValueError("异构副本拓扑不支持 with_updates 改写（改形状请整体重建）")
        allowed = {"tp", "pp", "ep", "nnodes", "replicas", "copies"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"with_updates 不支持: {sorted(unknown)}")
        unit_changes = {k: v for k, v in changes.items() if k in {"tp", "pp", "ep", "nnodes"}}
        merged = dict(changes)
        if unit_changes:
            units = [u.model_copy(update=unit_changes) for u in (self.units or [])]
            merged["units"] = units
        if "copies" in changes:
            merged["replicas"] = changes["copies"]
        elif "replicas" in changes:
            merged["copies"] = changes["replicas"]
        return self.model_copy(update=merged)


def set_topology_copies(topo: dict, copies: int) -> dict:
    """把份数写进原始 spec 的 topology dict（replicas/copies 两套口径同步，其余键保留）。

    scale 落库的配套写法：spec 是 dict 不能走 with_updates，两键必须一起动（归一层
    冲突校验见 Topology._normalize_units_copies）。**旧四数写法的 dict（无 units/copies）
    只写 replicas**——补一个 copies 会造出「有 copies 无 units」的非法混合形态，
    下次读回（DeploymentConfig.model_validate）直接 ValidationError。
    """
    merged = dict(topo)
    merged["replicas"] = copies
    if "copies" in merged or "units" in merged:
        merged["copies"] = copies
    return merged


class HealthCheck(BaseModel):
    """健康检查配置。"""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(
        600,
        description="健康检查总超时（秒）；wait_while_running 为真时只作告警阈值，容器退出才判失败",
    )
    interval_seconds: int = Field(10, description="健康检查轮询间隔（秒）")
    wait_while_running: bool = Field(
        True,
        description="超时后容器仍在运行则继续等待（大模型冷编译/图捕获常超出超时）",
    )


class EngineConfig(BaseModel):
    """引擎级环境配置（hf_endpoint / nccl_ifname），随 spec 持久化。

    原名 ``SystemConfig``，因与 ORM 表 ``models/system_config.py:SystemConfig`` 同名易混，
    改名 ``EngineConfig``（语义即"传给推理引擎的系统环境"，字段名同步 ``engine_config``）。
    """

    model_config = ConfigDict(extra="forbid")

    hf_endpoint: str | None = Field(None, description="HuggingFace 镜像端点（HF_ENDPOINT 环境变量）")
    nccl_ifname: str | None = Field(
        None,
        description="分布式通信网络接口名（写入 NCCL_SOCKET_IFNAME 与 GLOO_SOCKET_IFNAME，多机部署用；留空时按所在节点上报的网卡名自动选取）",
    )


class GpuSelector(BaseModel):
    """GPU 选择器：自动调度（空）/ 指定节点（node_id）/ 手动选卡（gpu_uuids）。

    node_id 与 gpu_uuids 互斥。手动选卡支持单节点与跨机：跨机时 gpu_uuids 须分布在 nnodes 个
    节点、每节点 tp×pp//nnodes 张（详见 scheduler 的 _validate_manual_selection）。
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str | None = Field(None, description="指定调度到的节点ID（单机自动分卡；与 gpu_uuids/node_ids 互斥）")
    gpu_uuids: list[str] | None = Field(
        None,
        description="手动指定 GPU UUID 列表（与 node_id/node_ids 互斥；同形时总数=tp×pp，跨机分布在 nnodes 节点）",
    )
    node_ids: list[str] | None = Field(
        None,
        description=(
            "节点集（与 node_id/gpu_uuids 互斥）：显式 cpu_only 时为每个副本指定节点"
            "（第 i 份副本放 node_ids[i]，长度须等于副本数，同一节点可指定多份）；"
            "其余=跨机选定节点集（每节点自动分 tp×pp//nnodes 张卡；len=nnodes）"
        ),
    )


class PublishConfig(BaseModel):
    """部署自动发布配置（对外服务名 + 定价/标签/描述，随 spec 持久化）。

    只决定「对外服务名」（Route.name，租户调用 model= 用的名字）；引擎内部
    ``--served-model-name`` / 端点 served_model 仍用 ``model.name``——API 网关
    已把两者解耦（改写上游、回填响应），前端无感。定价用 float（spec 存 JSONB，
    Decimal 无法 json 序列化），落 Route 列时转回 ``Decimal(str(x))``。
    """

    model_config = ConfigDict(extra="forbid")

    auto_publish: bool = Field(True, description="部署达成后是否自动发布对外路由；false 只部署不发布（端点仍建）")
    name: str | None = Field(None, max_length=255, description="对外服务名（租户调用 model= 字段）；None→model.name")
    model_type: str | None = Field(None, max_length=50, description="模型类别；None→model.model_type")
    description: str | None = Field(None, description="模型描述（模型中心展示，可空）")
    tags: list[str] = Field(default_factory=list, description="能力标签（受控词表 ModelTag，纯展示）")
    input_price_per_mtok: float | None = Field(
        None, ge=0, description="输入 token 定价（每百万 token / 元；空=不计费）"
    )
    output_price_per_mtok: float | None = Field(None, ge=0, description="输出 token 定价（每百万 / 元；空=不计费）")
    cached_input_price_per_mtok: float | None = Field(
        None, ge=0, description="缓存命中输入定价（每百万 / 元；空=不计费）"
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("对外服务名不能为空白")
        return v

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: list[str]) -> list[str]:
        allowed = ModelTag.values()
        unknown = sorted(set(v) - set(allowed))
        if unknown:
            raise ValueError(f"未知标签: {'、'.join(unknown)}；可用: {'、'.join(allowed)}")
        return v


class KVCacheConfig(BaseModel):
    """KV cache 估算配置（精度档仍是引擎 flag，走 engine_params.extra_args）。

    KV 缓存的落点（显存 / 本机内存）不可配：系统只在两端取解，KV 恒与权重同侧；纯 CPU 档
    没有卡，KV 本来就在主机内存。需要自定义时在部署参数 extra_args 追加对应 flag
    （如 llama.cpp ``--no-kv-offload``）——管理器不替引擎 flag 记账，也就不会给出与真实
    占用不符的容量判断。
    """

    model_config = ConfigDict(extra="forbid")

    n_parallel: int | None = Field(None, gt=0, description="并发序列数（KV 真公式用；None=默认 4）")


class CpuLaneConfig(BaseModel):
    """纯 CPU 部署请求字段（部署配置与 inspect 预检共用同一份定义）。

    字段、默认值与校验只此一处：两处各写一份会让「inspect 收下的偏好、下发时
    DeploymentConfig 却拒掉」这类分歧只在部署期才暴露。
    """

    allow_cpu: bool = Field(
        False, description="允许调度落到纯 CPU 部署（无卡，权重与 KV 都在主机内存）；手选 GPU 时禁走"
    )
    offload_preference: str = Field(
        "auto",
        description="部署偏好：auto | gpu_only | gpu_main | cpu_main | cpu_only（系统只在全量上卡/纯 CPU 两端取解，偏好决定优先哪端）",
    )
    kv_cache: KVCacheConfig = Field(default_factory=KVCacheConfig, description="KV cache 估算配置（并发序列数）")

    @property
    def cpu_lane_requested(self) -> bool:
        """是否请求了纯 CPU 部署（``allow_cpu`` 或显式 ``cpu_only``）——见
        :func:`resource.wants_cpu_lane`，部署判据的唯一定义处。"""
        from llmpod.apps.manager.deployment import resource

        return resource.wants_cpu_lane(self.allow_cpu, self.offload_preference)

    @field_validator("offload_preference")
    @classmethod
    def _check_preference(cls, v: str) -> str:
        from llmpod.apps.manager.deployment import resource

        if v not in resource.PREFERENCES:
            raise ValueError(f"offload_preference 只支持 {list(resource.PREFERENCES)}，收到 {v!r}")
        return v


class DeploymentConfig(CpuLaneConfig):
    """用户部署配置（ModelDeployment.spec 的 typed model，CRD 扩展槽）。

    所有子结构 typed + extra='forbid'；engine_params 为引擎特定自由 dict（透传给适配器）。
    """

    model_config = ConfigDict(extra="forbid")

    topology: Topology = Field(default_factory=Topology, description="部署拓扑（tp/pp/replicas 等）")
    engine_params: dict[str, Any] = Field(
        default_factory=dict, description="引擎特定自由参数（透传给适配器，如 vLLM 的 --max-model-len）"
    )
    health_check: HealthCheck = Field(default_factory=HealthCheck, description="健康检查配置（超时/间隔）")
    engine_config: EngineConfig = Field(
        default_factory=EngineConfig, description="引擎级系统环境配置（hf_endpoint/nccl_ifname）"
    )
    gpu_selector: GpuSelector | None = Field(None, description="GPU 选择器（指定节点等），不传则自动调度")
    image: str | None = Field(None, description="推理引擎容器镜像（覆盖默认镜像）")
    image_tag: str | None = Field(None, description="镜像 tag（与 image 二选一或组合）")
    model_path: str | None = Field(None, description="模型权重在节点上的本地路径（或挂载路径）")
    force: bool = Field(
        False, description="强制部署：忽略预检/同子网等启发式约束（显存不足/跨子网仍会尝试，由调度器/NCCL 最终裁决）"
    )
    publish: PublishConfig = Field(
        default_factory=PublishConfig, description="部署自动发布配置（对外名/定价/标签/描述）"
    )
