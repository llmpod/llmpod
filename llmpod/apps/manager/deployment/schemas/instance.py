"""InstanceSpec：编排器内部 instance 的数据模型（Manager 进程内）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InstanceSpec(BaseModel):
    """单个推理实例的编排规格 Manager内部使用。"""

    model_config = ConfigDict(extra="forbid")

    # ---- expand_topology 产出 ----
    role: str = "full"
    tp: int = 1
    pp: int = 1
    replica_idx: int = 0
    nnodes: int = 1
    node_rank: int = 0

    # ---- _plan 填充 ----
    instance_id: str = ""
    # 所属引擎实例（→ model_deployment_instance.instance_id）：同引擎的多机分片共享
    # 一个值，四层挂载的权威键（容器行经 deployment_service.persist_instances 落到
    # Instance.model_deployment_instance_id 列）
    engine_instance_id: str = ""
    # 实例全局序号（拓扑展开顺序 0..N-1）；task resource_key 与 head/worker 区分用。
    # 与 node_rank（多机 rendezvous 序号）语义不同——replicas 展开 node_rank 全为 0，
    # 不能当唯一键。
    rank: int = 0

    # ---- GpuScheduler.assign 填充 ----
    assigned_node: str | None = None
    gpu_arch: str | None = None  # 实际部署 arch（cann/nvidia；adapter 据此选镜像/设备/参数）
    target_gpu_model: str | None = None  # 目标卡型号（catalog 匹配用）
    target_driver_version: str | None = None  # 目标卡驱动/CANN 版本（catalog 匹配用）
    assigned_gpus: list[int] | None = None
    assigned_gpu_uuids: list[str] | None = None

    # ---- RankResolver.fill 填充 ----
    master_addr: str | None = None
    master_port: int | None = None
    # 所在节点的物理网卡名（持有 Node.ip，多机部署 SOCKET_IFNAME 注入用；由
    # _stamp_assigned_instances 按调度结果盖章，随 spec JSONB 持久化——reconciler
    # 重发 START 无需查库，与 master_addr 同待遇）
    node_ifname: str | None = None

    # ---- _plan 填充 ----
    model_path: str | None = None
    # 模型架构（config.json architectures[0]，如 Qwen3ForCausalLM）：按模型家族选解析器
    # （Qwen3 系的思考/工具格式与通用兜底不兼容），由 _plan 从 Model 行盖章
    model_architecture: str | None = None
    mmproj_path: str | None = None  # 视觉投影器（GGUF 多模态）路径；与 model_path 同目录，adapter 注入 --mmproj
    served_model_name: str | None = None
    port: int | None = None
    task: str = "generate"  # 引擎 task（adapter.task_for(model_type)）：generate/embed/classify/rerank/...；用于推理引擎控制路由参数 如--task
    vram_percent: float | None = (
        None  # 生效显存占比（编排器解析自用户引擎 flag/显式 vram_percent/引擎默认；adapter 据此设 --gpu-memory-utilization）
    )

    # ---- CPU 部署（_plan 反解后填充；随 Instance.spec JSONB 持久化）----
    offload_layers: int | None = None  # 上 GPU 的层数（0=纯 CPU）；adapter 注入 --n-gpu-layers
    ram_claim_gb: float | None = None  # 本实例的内存 claim（ResourceLedger 记账 + 重启重建依据）
