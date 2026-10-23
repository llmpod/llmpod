"""集群拓扑响应模型（前端节点图可视化用）。

后端返回实体 + 关联 id（不返回边/布局坐标）——前端 buildGraph 负责连线与布局。
id 用后端原生类型（混合 int + UUID str），engine 用字符串做唯一标识。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ClusterSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    # 恢复 K8s 时描述同步加回 "| k8s"（与 cluster 模型列 comment 保持同口径）
    type: str = Field(description="集群类型：docker")
    status: str = Field(description="active | inactive")


class NodeSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="节点 UUID（主键）")
    cluster_id: int
    hostname: str
    ip: str
    status: str = Field(description="online | offline")
    memory_gb: float = Field(0.0, description="总内存 GB（system_buffer 实时，缺省回退 DB）")
    available_memory_gb: float = Field(0.0, description="可用内存 GB（总 - 已用）")
    memory_used_gb: float = Field(0.0, description="已用内存 GB（实时，离线为 0）")
    memory_percent: float = Field(0.0, description="内存使用率 0-100（实时，离线为 0）")
    cpu_percent: float = Field(0.0, description="CPU 使用率 0-100（实时，离线为 0）")


class GpuSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    uuid: str
    node_id: str = Field(description="所属节点 UUID")
    index: int
    name: str
    arch: str = Field(description="nvidia | cann | cpu")
    total_vram_gb: float
    used_vram_gb: float = Field(0.0, description="已用显存 GB（实时）")
    available_vram_gb: float
    util: float = Field(0.0, description="实时利用率 0-100")
    status: str = Field(description="online | stale | offline")


class ReplicaSummary(BaseModel):
    """部署副本轻量摘要（集群拓扑用；引擎明细只在部署详情接口）。"""

    model_config = ConfigDict(extra="ignore")

    seq: int = Field(description="份内序号（0..copies-1）")
    status: str = Field(description="provisioning | serving | down（投影链权威值）")
    node_ids: list[str] = Field(default_factory=list, description="该份容器分片所在节点（去重有序）")


class DeploymentSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    deployment_id: str = Field(description="部署 UUID")
    cluster_id: int
    name: str
    status: str = Field(description="creating | running | failed | stopped | deleting")
    phase: str = Field(description="编排子态")
    engine: str = Field(description="引擎标识（关联 EngineSummary.engine）")
    model_id: int = Field(description="关联 ModelSummary.id")
    gpu_uuids: list[str] = Field(default_factory=list, description="占用 GPU UUID 列表")
    replicas: int = 1
    nnodes: int = 1
    api_endpoint: str | None = None
    health_status: str | None = Field(
        None, description="健康灯（投影链实值）：healthy=全副本 serving | degraded=部分 down；非 running 期为 None"
    )
    instance_count: int = 0
    replica_summaries: list[ReplicaSummary] = Field(
        default_factory=list, description="副本层摘要（四层化前的旧数据为空列表）；节点浮层「此节点承载副本」的数据源"
    )


class EngineSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    engine: str = Field(description="引擎唯一标识（同时也是关联键）")
    display_name: str
    image: str = Field(description="当前镜像 ref")
    supported_categories: list[str] = Field(default_factory=list)
    supported_gpu_archs: list[str] = Field(default_factory=list, description="支持的软件栈：nvidia / cann / cpu")
    supported_formats: list[str] = Field(default_factory=list, description="支持的权重格式：safetensors / gguf")
    deployable: bool = Field(True, description="参数生成是否已实现（占位引擎为 False）")
    supported_parallel: list[str] = Field(default_factory=list, description="并行：tp / pp / ep")
    supports_multinode: bool = Field(
        True, description="是否支持多机部署（nnodes>1）；False = 单机引擎，前端禁用多机控件"
    )
    protected_flags: list[str] = Field(
        default_factory=list, description="系统必控 flag（用户 extra_args 同名会被忽略并告警）"
    )
    param_suggestions: list[dict] = Field(
        default_factory=list, description="推荐参数 chip：{name/hint/default/choices}"
    )
    kv_cache_dtype_options: list[dict] = Field(
        default_factory=list,
        description="KV cache 精度选项：{value/label/dtype_bytes/kv_ratio/args/hint}；首项 value 为空串=默认不量化",
    )


class ModelSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    model_type: str = Field(description="llm | embedding | rerank | ...")
    size_gb: float
    format: str = Field(description="safetensors | gguf | ...")


class ClusterTopology(BaseModel):
    """单集群拓扑（前端 buildGraph 组装节点图）。"""

    model_config = ConfigDict(extra="ignore")

    cluster: ClusterSummary
    nodes: list[NodeSummary] = Field(default_factory=list)
    gpus: list[GpuSummary] = Field(default_factory=list)
    deployments: list[DeploymentSummary] = Field(default_factory=list)
    engines: list[EngineSummary] = Field(default_factory=list)
    models: list[ModelSummary] = Field(default_factory=list)
