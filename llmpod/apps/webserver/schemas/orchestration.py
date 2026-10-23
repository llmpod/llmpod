"""编排侧（orchestration）webserver 自有 IN/OUT 模型。

webserver 表现层**不应**直接 import ``llmpod.tunnel.*``。本模块定义与 wire 契约
（``tunnel/schemas``）**解耦**的 HTTP 模型：IN 是按 web 特性裁剪的请求体，OUT 是镜像
wire 字段名的输出模型。门面（``apps/manager/dispatch.py``）返回 wire model，路由的
``success()`` 在边界 ``model_dump()`` 成 dict——OUT 模型从 dict 校验，字段名与 wire
``model_dump()`` key 一一对应，但类型归属在 webserver 侧。

不无脑照搬 wire 契约：
- ``ContainerRunRequest`` 裁剪为 web 子集（11 字段），去掉 ``driver_mounts`` / ``ipc`` /
  ``shm_size`` / ``runtime`` / ``privileged`` / ``cap_add`` / ``extra_devices`` 等
  深层 docker 旋钮（web 表单不暴露，自动化脚本如需走部署引擎而非此便捷端点）。
- ``OnlineNodeOut`` 为精简摘要（列表载荷轻量，全量详情走 /info）。
- ``GpuMetricOut`` / ``NodeInfoOut`` 全量镜像监控字段（监控保真，字段成本可忽略）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from llmpod.apps.manager.dispatch import NodeSummary
from llmpod.apps.webserver.schemas.out_models import GpuRuntimeOut


# ============================================================
# OUT —— 节点 / GPU 查询
# ============================================================


class OrchestrationStatusOut(BaseModel):
    """编排引擎状态摘要。"""

    status: str = Field(..., description="编排引擎运行状态，如 running")
    version: str = Field(..., description="编排引擎版本号")
    features: list[str] = Field(default_factory=list, description="已启用的能力标签")
    online_nodes: int = Field(..., description="当前在线节点数（hub 内存视图）")


class OnlineNodeOut(NodeSummary):
    """在线节点列表项（继承 manager ``NodeSummary`` 投影——字段唯一定义处，非 DB 行）。"""


class GpuMetricOut(GpuRuntimeOut):
    """GPU 实时指标（镜像 wire GpuInfo 全字段，监控保真）。

    摘要字段（显存 / 利用率 / 温度 / 功耗 / 时钟 / 进程数）继承自 ``GpuRuntimeOut``
    （out_models，字段唯一定义处），本类只补 uuid / index / 型号 / 编解码器 / PCIe 等
    全量监控字段。
    """

    uuid: str = Field(..., description="GPU 唯一标识（厂商驱动分配）")
    index: int = Field(..., description="节点内 GPU 索引（0-based）")
    name: str = Field(..., description="GPU 型号名")
    vendor: str = Field("", description="厂商标识：nvidia / ascend / ...")
    arch: str = Field("", description="软件栈名：nvidia / cann / cpu（驱动镜像选择/设备配置）")
    soc_model: str | None = Field(None, description="SoC 型号（Ascend：310p/910b/a3...；NVIDIA 不填）")
    # ---- 显存 ----
    total_vram_gb: float = Field(0.0, description="显存总量（GB）")
    # ---- 计算 ----
    encoder_utilization: float = Field(0.0, description="编码器利用率 0-100")
    decoder_utilization: float = Field(0.0, description="解码器利用率 0-100")
    # ---- 进程/带宽 ----
    pcie_rx_throughput_mb_s: float = Field(0.0, description="PCIe 接收吞吐（MB/s）")
    pcie_tx_throughput_mb_s: float = Field(0.0, description="PCIe 发送吞吐（MB/s）")
    # ---- 其它 ----
    driver_version: str = Field("", description="驱动版本")


class NodeInfoOut(BaseModel):
    """节点详细信息（镜像 wire NodeReport 全字段，监控保真）。

    含系统级指标（CPU/内存/swap/磁盘/负载）+ 逐卡 GPU 实时指标。
    """

    # ---- SystemInfo 字段（inline 展开）----
    hostname: str = Field("", description="主机名")
    ip: str = Field("", description="节点 IP")
    cpu_cores: int = Field(0, description="CPU 核数")
    cpu_arch: str = Field("", description="CPU 架构，如 x86_64 / aarch64")
    memory_gb: float = Field(0.0, description="总内存（GB）")
    os_name: str = Field("", description="操作系统名")
    os_version: str = Field("", description="操作系统版本")
    kernel_version: str = Field("", description="内核版本")
    uptime_seconds: float = Field(0.0, description="启动至今秒数")
    # ---- 运行时：CPU ----
    cpu_percent: float = Field(0.0, description="CPU 使用率 0-100")
    cpu_load_avg_1: float = Field(0.0, description="1 分钟平均负载")
    cpu_load_avg_5: float = Field(0.0, description="5 分钟平均负载")
    cpu_load_avg_15: float = Field(0.0, description="15 分钟平均负载")
    # ---- 运行时：内存 ----
    memory_used_gb: float = Field(0.0, description="已用内存（GB）")
    memory_percent: float = Field(0.0, description="内存使用率 0-100")
    # ---- 运行时：交换分区 ----
    swap_total_gb: float = Field(0.0, description="交换分区总量（GB）")
    swap_used_gb: float = Field(0.0, description="交换分区已用（GB）")
    swap_percent: float = Field(0.0, description="交换分区使用率 0-100")
    # ---- 运行时：磁盘（根分区）----
    disk_total_gb: float = Field(0.0, description="根分区总量（GB）")
    disk_used_gb: float = Field(0.0, description="根分区已用（GB）")
    disk_percent: float = Field(0.0, description="根分区使用率 0-100")
    # ---- GPU + capabilities ----
    gpus: list[GpuMetricOut] = Field(default_factory=list, description="逐卡 GPU 实时指标")
    capabilities: list[str] = Field(default_factory=list, description="节点能力标签，如 ['docker','shell','inference']")
    # ---- NodeService.info() 额外字段 ----
    node_id: str | None = Field(None, description="节点ID（RPC 按需查询时附加，心跳不填）")
    started: float | None = Field(None, description="节点进程启动时间戳")
    ts: float | None = Field(None, description="信息采集时间戳")


# ============================================================
# OUT —— 任务下发结果
# ============================================================


class ContainerRunResultOut(BaseModel):
    """容器运行任务结果（镜像 wire DeployResult）。"""

    success: bool = Field(..., description="是否运行成功")
    container_name: str = Field(..., description="容器名称")


class ShellResultOut(BaseModel):
    """shell 任务结果（镜像 wire ShellResult）。"""

    exit_code: int = Field(..., description="退出码（0=成功）")
    stdout: str = Field("", description="标准输出")
    stderr: str = Field("", description="标准错误")


# ============================================================
# IN —— 任务下发请求体
# ============================================================


class ContainerRunRequest(BaseModel):
    """运行容器请求体（wire DeploySpec 的 web 子集，11 字段）。

    裁掉 driver_mounts / ipc / shm_size / runtime / privileged / cap_add /
    extra_devices 等 deep docker 旋钮——web 表单不暴露；需要完整旋钮的自动化场景
    走部署引擎（model-deployments）而非此便捷端点。
    """

    image: str = Field(..., description="容器镜像，如 nginx:latest")
    name: str = Field(..., description="工作负载名（幂等 + label 反查用）")
    entrypoint: list[str] | None = Field(None, description="覆盖镜像 ENTRYPOINT（list）")
    args: list[str] | None = Field(None, description="启动参数（跟在 ENTRYPOINT 后）")
    env: dict[str, str] | None = Field(None, description="环境变量")
    gpus: list[int] | None = Field(
        None,
        description="GPU 设备索引，如 [0,1]；[] 空表=全部 GPU；不传=不分配 GPU",
    )
    ports: dict[str, int] | None = Field(None, description="端口映射 {host: container}")
    volumes: dict[str, str] | None = Field(None, description="数据卷 {host: container}")
    labels: dict[str, str] | None = Field(None, description="标签（value 必须 string）")
    network_mode: str | None = Field(None, description="网络模式，如 host / bridge")
    restart_policy: str | None = Field(None, description="重启策略 no / always / ...")


class ShellRequest(BaseModel):
    """shell 任务参数（对应 NodeService.execute_shell 独立 RPC）。"""

    command: str = Field(..., description="待执行的 shell 命令")
    timeout: int = Field(60, description="执行超时（秒）")
    cwd: str | None = Field(None, description="工作目录，可空")
    env: dict[str, str] | None = Field(None, description="额外环境变量键值对")


class DockerPullRequest(BaseModel):
    """docker pull 流式任务参数。"""

    image: str = Field(..., description="待拉取的镜像名，如 nginx:latest")


class TailLogRequest(BaseModel):
    """docker logs 流式任务参数。"""

    container_name: str = Field(..., description="目标容器名称")
    lines: int = Field(20, description="初始返回的尾部日志行数")


__all__ = [
    # OUT
    "OrchestrationStatusOut",
    "OnlineNodeOut",
    "GpuMetricOut",
    "NodeInfoOut",
    "ContainerRunResultOut",
    "ShellResultOut",
    # IN
    "ContainerRunRequest",
    "ShellRequest",
    "DockerPullRequest",
    "TailLogRequest",
]
