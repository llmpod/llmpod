"""节点系统信息 + GPU 信息 wire 契约。

``SystemInfo`` 和 ``GpuInfo`` 是 Node↔Manager 共享的强类型契约：
- Node 侧 collector 产出 → ``$hello``/``$heartbeat`` info payload → Manager 侧 ``model_validate`` 恢复类型
- ``NodeService.info()`` RPC 返回值也用此契约

3 端（manager / node / tunnel）共享的唯一定义处。
"""

from __future__ import annotations

from pydantic import BaseModel

# $register_nack 的注销理由（wire 常量）：manager 拒绝已注销节点的重连时发送，
# node 侧精确匹配此值触发自清理。字符串是双方共享契约——改它必须两端同步发版。
REGISTER_NACK_DECOMMISSIONED = "node decommissioned"


class SystemInfo(BaseModel):
    """系统级信息：CPU、内存、主机名、IP + 运行时资源指标。"""

    # ---- 基础（静态）信息 ----
    hostname: str
    ip: str
    # 地址协商候选（有序去重，顺序即优先级，index 0 = ip 自报值）：显式配置独占，
    # 否则 [UDP 默认路由源 IP, *其余网卡地址（私网优先）]。仅诊断参考——manager
    # 不再据此探活协商（探活不辨身份，猜错 = 引擎 api_base 与多机 rendezvous
    # 拿到打不通的地址）；Node.ip 的选定口径见 apps/manager/node_addr.py。
    ip_candidates: list[str] = []
    # ip 为显式配置固定（node_advertise_ip / 引导参数 node_ip）：manager 采信该值
    # （含重新指定修正），不掺自报/候选换选。
    ip_explicit: bool = False
    # 持有上述 ip 的物理网卡名（虚拟桥已过滤）：多机分布式通信的 SOCKET_IFNAME
    # 注入用。探测不到（IP 不在本机网卡上 / 极简容器环境）为空串，消费方回落
    # 显式配置与代码兜底。
    net_ifname: str = ""
    cpu_cores: int
    cpu_arch: str
    memory_gb: float  # 总内存 (GB)，保留原字段名以兼容 DB/注册流程
    # ---- 静态扩展：操作系统 ----
    os_name: str = ""
    os_version: str = ""
    kernel_version: str = ""
    uptime_seconds: float = 0.0  # 启动至今秒数
    # ---- 运行时：CPU ----
    cpu_percent: float = 0.0  # CPU 使用率 0-100
    cpu_load_avg_1: float = 0.0  # 1 分钟平均负载
    cpu_load_avg_5: float = 0.0  # 5 分钟平均负载
    cpu_load_avg_15: float = 0.0  # 15 分钟平均负载
    # ---- 运行时：内存 ----
    memory_used_gb: float = 0.0
    memory_percent: float = 0.0  # 内存使用率 0-100
    # ---- 运行时：交换分区 ----
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_percent: float = 0.0
    # ---- 运行时：磁盘（根分区） ----
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_percent: float = 0.0


class GpuInfo(BaseModel):
    """GPU 运行时信息。"""

    uuid: str
    index: int
    name: str
    vendor: str = ""  # 厂商标识：nvidia / ascend / ...（用于多厂商区分）
    arch: str = ""  # 软件栈名：nvidia / cann / cpu（驱动镜像选择/设备配置，所有 collector 必填）
    soc_model: str | None = None  # SoC 型号（Ascend 关注：310p/910b/a3...；NVIDIA 不填，写库进 spec jsonb）
    # ---- 显存 ----
    used_vram_gb: float = 0.0
    total_vram_gb: float = 0.0
    memory_utilization: float = 0.0  # 显存利用率 0-100
    # ---- 计算 ----
    utilization: float = 0.0  # GPU 核心利用率 0-100
    encoder_utilization: float = 0.0  # 编码器利用率 0-100
    decoder_utilization: float = 0.0  # 解码器利用率 0-100
    # ---- 散热/功耗 ----
    temperature: int = 0  # 温度 (℃)
    fan_speed: int = 0  # 风扇转速百分比 0-100
    power_draw_w: float = 0.0  # 当前功耗 (W)
    power_limit_w: float = 0.0  # 功耗上限 (W)
    # ---- 时钟 ----
    sm_clock_mhz: int = 0  # SM 时钟频率
    mem_clock_mhz: int = 0  # 显存时钟频率
    # ---- 进程/带宽 ----
    process_count: int = 0  # GPU 上下文进程数
    pcie_rx_throughput_mb_s: float = 0.0  # PCIe 接收吞吐 (MB/s)
    pcie_tx_throughput_mb_s: float = 0.0  # PCIe 发送吞吐 (MB/s)
    # ---- 其它 ----
    driver_version: str = ""


class NodeReport(BaseModel):
    """``$hello`` / ``$heartbeat`` 的 info payload + ``NodeService.info()`` 返回值。

    Node 侧 collector 产出此 model → ``.model_dump()`` 序列化为 JSON →
    Manager 侧 ``NodeReport.model_validate(params["info"])`` 恢复类型。
    """

    # ---- SystemInfo 字段（inline 展开而非嵌套，兼容现有 wire 格式）----
    hostname: str = ""
    ip: str = ""
    ip_candidates: list[str] = []
    ip_explicit: bool = False
    # 持有 ip 的物理网卡名（SystemInfo 同款字段 inline 展开；空=未探测到）
    net_ifname: str = ""
    cpu_cores: int = 0
    cpu_arch: str = ""
    memory_gb: float = 0.0
    os_name: str = ""
    os_version: str = ""
    kernel_version: str = ""
    uptime_seconds: float = 0.0
    cpu_percent: float = 0.0
    cpu_load_avg_1: float = 0.0
    cpu_load_avg_5: float = 0.0
    cpu_load_avg_15: float = 0.0
    memory_used_gb: float = 0.0
    memory_percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_percent: float = 0.0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_percent: float = 0.0
    # ---- GPU + capabilities ----
    gpus: list[GpuInfo] = []
    capabilities: list[str] = []
    # ---- 版本：node 运行的 llmpod.__version__（$hello/$heartbeat 均带；
    # manager 比对自身版本得到 version_status，升级联动以此为准）----
    version: str = ""
    # ---- NodeService.info() 额外字段（RPC 按需查询时附加，心跳不填）----
    node_id: str | None = None
    started: float | None = None
    ts: float | None = None
