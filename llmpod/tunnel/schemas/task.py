"""Task wire 契约 Pydantic model（统一任务底座 Manager↔Node）。

对应 ``tunnel/contract.py`` 的 ``TaskService``（Manager→Node：ensure_task/cancel_task）
与 ``ManagerTaskService``（Node→Manager：report_task_event/sync_node_state）。

- ``TaskPayload``：``ensure_task`` 入参——``task`` 表的 wire 投影（**不含 secrets**；
  ENSURE_MODEL 的 source/model 信息在 ``payload`` 字段内。
- ``TaskState``：node 上报的本地任务状态（``sync_node_state`` 上报）。
- ``TaskEventPayload``：``report_task_event`` 入参；``extra`` 不含 secrets，
  ``message`` 必须 scrub。
- ``Ack``：通用应答。

secrets 边界：凭证**只**经 ``TaskService.ensure_task`` 的独立 ``secrets`` 参数内存传递，
永不进入 ``TaskPayload`` / ``task_event`` / node 本地表 / 日志。

属主边界：``Task`` 无属主列——``resource_type + resource_key + node_id`` 是唯一归属语义；
deployment↔task 关系经 Instance 表 join（见 ``PlacementService.recompute_demand``）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from llmpod.config.constants import InstanceStatus, TaskStatus
from llmpod.tunnel.schemas.deploy import DeploySpec


class Ack(BaseModel):
    """通用应答。"""

    model_config = ConfigDict(extra="ignore")

    ok: bool = True
    message: str | None = None


class TaskPayload(BaseModel):
    """``task`` 表的 wire 投影（``ensure_task`` 入参；不含 secrets/timestamps/属主）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    resource_type: str
    resource_key: str
    node_id: str
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 1
    progress: int = 0
    timeout_sec: int | None = None
    payload: dict = Field(default_factory=dict)


class TaskState(BaseModel):
    """node 本地任务状态（``sync_node_state`` 上报）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: TaskStatus
    progress: int = 0
    resource_key: str | None = None
    last_error: str | None = None
    extra: dict | None = None


class TaskEventPayload(BaseModel):
    """``report_task_event`` 。extra/message 不含 secrets。"""

    model_config = ConfigDict(extra="ignore")

    task_id: str
    node_id: str
    status: TaskStatus
    # 子阶段（第二层）：str 而非枚举——开放词表按任务类型演进，未知值容忍不炸
    stage: str | None = None
    progress: int = 0
    resource_type: str | None = None
    resource_key: str | None = None
    message: str | None = None
    extra: dict | None = None


class InstanceStatusPayload(BaseModel):
    """``report_instance_status`` 入参：实例长期状态迁移上报（Node→Manager）。

    Task 成功 ≠ 实例长期健康——容器阶段的状态（启动/就绪/失败/停止）经本通道落
    ``instance`` 表，驱动部署 phase 推进。``message`` 必须 scrub secrets。
    """

    model_config = ConfigDict(extra="ignore")

    deployment_id: str
    instance_id: str
    node_id: str
    status: InstanceStatus
    container_id: str | None = None
    endpoint: str | None = None
    message: str | None = None


# ===================== 任务配方（TaskPayload.payload 的 typed 内层）=====================
#
# TaskPayload.payload 本身保持 dict（JSONB 自由载荷语义），但各任务类型的配方字段是
# Manager 组装、Node 消费的双端契约——两端各拼/各读裸 dict 时 key 漂移只会以静默
# 默认值暴露（如 readiness.timeout_sec 拼错 → node 永远 600s 兜底），故按类型建模：
# Manager 构造 model 后 model_dump 进 payload，Node 侧 model_validate 反向校验。


class ReadinessConfig(BaseModel):
    """START_INSTANCE readiness 探活配置（Manager 组装、Node 消费）。

    默认值即 Node 侧兜底语义（0 是合法值——如 interval_sec=0 供测试瞬时轮询，
    故不能用 ``or`` 兜底，缺省统一收敛到本 model 的字段默认值）。
    """

    model_config = ConfigDict(extra="ignore")

    mode: Literal["http", "stable_running"] = "http"  # 缺省 http（兼容不含 mode 的旧 payload）
    url: str | None = None  # http 模式探活 URL（GET 200 即就绪）
    initial_delay_sec: float = 0.0
    interval_sec: float = 10.0
    timeout_sec: float = 600.0


class PullImagePayload(BaseModel):
    """PULL_IMAGE 任务配方。"""

    model_config = ConfigDict(extra="ignore")

    image: str


class StartInstancePayload(BaseModel):
    """START_INSTANCE 任务配方：DeploySpec 快照 + readiness 探活配置（无 secrets）。"""

    model_config = ConfigDict(extra="ignore")

    deployment_id: str
    instance_id: str
    rank: int = 0
    role: str | None = None
    container_name: str
    spec: DeploySpec  # 嵌套 wire 契约：两端校验/序列化由 pydantic 承担
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    endpoint: str | None = None  # headless worker 无服务端点


class StopInstancePayload(BaseModel):
    """STOP_INSTANCE 任务配方（纯停不删容器）。"""

    model_config = ConfigDict(extra="ignore")

    deployment_id: str
    instance_id: str
    rank: int = 0
    container_name: str
    stop_timeout: int = 10


class EnsureModelPayload(BaseModel):
    """ENSURE_MODEL 任务配方（模型分发：来源/校验/落盘位置）。"""

    model_config = ConfigDict(extra="ignore")

    model_id: int
    source: str | None = None
    source_url: str | None = None
    source_kind: str | None = None  # 封闭词表（single_gguf/gguf_shards/transformers_folder/...）
    source_subpath: str | None = None
    extra_files: list[str] = Field(
        default_factory=list,
        description="配套文件（repo 相对路径，如 GGUF 视觉投影器 mmproj-*.gguf）：随主模型一起下载并校验",
    )
    revision: str | None = None
    hash_mode: str | None = None
    hash_value: str | None = None
    local_dir: str
    desired_state: str = "present"
    # local 源：Manager 文件服务 origin（非密，Node 出站拉取 manifest/file 用）
    manager_base_url: str | None = None
    # 模型体积（字节，可选）：Node 下载前磁盘预检按「模型体积×1.2 + 余量」判，
    # 未携带时回落固定阈值（旧版 manager 下发的存量任务）
    total_bytes: int | None = None
