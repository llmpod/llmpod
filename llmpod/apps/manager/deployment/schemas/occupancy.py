"""GPU 声明占用模型（status-aware 占用解析的产出）。

``deployment_service.resolve_gpu_occupancy`` 的返回元素类型，inspect 回显 / create
预检 / restart 冲突明细三处共用——同一领域概念单一形状，不再各自手拼 dict。

``CardSummary`` 是另一档：不只是「占了躲少」，而是「这张卡现在什么情况」（卡上现有哪些部署、
各自什么状态、是不是同一个模型），给用户挑选调度位置看。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OccupiedBy(BaseModel):
    """单条占用归因（哪个部署在某卡上声明了多少显存）。"""

    model_config = ConfigDict(extra="ignore")

    deployment_id: str
    deployment_name: str = ""  # restart 冲突明细场景可不填（仅按 assignment 行归因）
    declared_gb: float = 0.0


class CardOccupant(BaseModel):
    """卡上现有的一个部署（含已停止/失败的，供用户判断这张卡的历史与现状）。"""

    model_config = ConfigDict(extra="ignore")

    deployment_id: str
    deployment_name: str = ""
    status: str = ""  # DeploymentStatus 值：running/stopped/failed/creating/deleting
    active: bool = False  # 是否仍在占用这张卡（creating/running）
    same_model: bool = False  # 是否就是本次要部署的这个模型
    declared_gb: float = 0.0


class CardSummary(BaseModel):
    """单卡现状（调度偏好 + 前端选卡决策共用的事实）。

    - ``state``：部署痕迹档位（``CardState``）；无痕迹为 None —— 与卡痕迹降权同源
    - ``occupants``：卡上现有部署明细（活跃的排前面，含已停止/失败的）

    「这台机器已有该模型文件」是节点属性、不在本模型里（见 ``ClusterGpuInfo.model_local``）。
    """

    model_config = ConfigDict(extra="ignore")

    uuid: str
    state: str | None = None
    occupants: list[CardOccupant] = Field(default_factory=list)


class GpuOccupancy(BaseModel):
    """单卡聚合占用（declared 之和 + 归因明细 + 占比）。"""

    model_config = ConfigDict(extra="ignore")

    declared_gb: float = 0.0
    occupied_pct: float = 0.0  # declared_gb / total_vram_gb（total 未知为 0）
    occupied_by: list[OccupiedBy] = Field(default_factory=list)  # 空 = 零占用

    def remaining_gb(self, total_vram_gb: float, default_util: float) -> float:
        """按容量口径（util headroom）扣除声明占用后的剩余可分配显存。

        ``default_util`` 传 ``resource_ledger.DEFAULT_GPU_MEMORY_UTILIZATION``（容量口径唯一定义处；函数内
        import 避免 schemas→ledger 循环依赖）。
        """
        return total_vram_gb * default_util - self.declared_gb

    def occupant_names(self) -> str:
        """占用方部署名拼接（""、分隔；前端/issue 提示用）。"""
        return "、".join(o.deployment_name for o in self.occupied_by)
