"""调度器 I/O model：GpuCandidate（输入候选）/ PlacementPrefs（调度偏好）/ GpuAssignment（输出分配）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GpuCandidate(BaseModel):
    """调度候选 GPU（在线 GPU 投影，供 GpuScheduler 消费）。"""

    model_config = ConfigDict(extra="forbid")

    uuid: str
    node_id: str
    index: int
    arch: str
    total_vram_gb: float
    status: str
    name: str = ""  # GPU 型号（catalog 匹配用）
    driver_version: str | None = None  # 驱动/CANN 版本（catalog 匹配用）
    # 心跳实时已用显存（含平台外进程）：容量过滤的「物理可用」闸门用。None = 无实时
    # 数据（节点未上报/心跳未起）——此时乐观按整卡空闲，与账本口径一致。
    used_vram_gb: float | None = None


class PlacementPrefs(BaseModel):
    """调度软偏好与架构回落序（建部署 / 扩容 / 预检三处唯一的装配口径）。

    三个字段都是「不改变可行性、只改变可行解之间选谁」的输入，缺省即「无偏好、单架构」。
    收成一个对象是为了让调用方**一次装配、一处传递**：散成三个可选 kwarg 时，漏传其中之一
    不会报错，只会静默退化（历史 bug：建部署没传 ``arch_order`` → 混插集群永远只算默认架构）。

    - ``arch_order``：逐引擎架构回落序（非空时首选架构放不下才落后面的，异构副本用）；
      ``None``=未给（只用单架构 ``arch``），空元组=上游架构闸门淘汰了全部候选（内核直接否决）。
    - ``locality_node_ids``：已有模型文件的节点（本地性加分，调度到该节点即免一次下发）。
    - ``card_state``：卡的部署痕迹（uuid → ``CardState``），无痕迹的卡优先被选中。
    """

    model_config = ConfigDict(extra="forbid")

    arch_order: tuple[str, ...] | None = None
    locality_node_ids: frozenset[str] = frozenset()
    card_state: dict[str, str] = Field(default_factory=dict)


class NodeCandidate(BaseModel):
    """CPU / 混合 offload 的调度候选节点（在线节点投影，内存口径）。"""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    hostname: str = ""
    ip: str = ""
    memory_gb: float = 0.0  # 节点总内存（system_buffer 实时值优先，缺省回退 DB Node.memory_gb）


class GpuAssignment(BaseModel):
    """单 instance 的 GPU 分配结果。"""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    node_id: str
    gpu_uuids: list[str]
    vram_gb: float
    gpu_memory_utilization: float | None = None  # 生效显存占比（编排器解析；持久化写默认 0.9）
    allocated: dict[str, float] = {}  # uuid → 预留 GB（逐卡 = 生效占比×total）
