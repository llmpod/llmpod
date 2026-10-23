"""系统信息采集：CPU、内存、swap、磁盘、负载、OS（使用 psutil）。

采集分为静态（hostname/ip/cpu_cores/cpu_arch/OS/uptime）与
运行时（cpu_percent/memory/swap/disk/load_avg）两类，统一由
collect_system_info() 返回 SystemInfo。$hello/$heartbeat 均使用。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import platform
import socket
import time

from llmpod.config.settings import settings
from llmpod.tunnel.schemas.system import NodeReport, SystemInfo

log = logging.getLogger("llmpod.apps.node.collectors.system")

_GB = 1024**3
_MB = 1024**2

# 容器/虚拟网桥接口名前缀：这些接口上的地址只在本机容器圈内有意义，不该进
# 上报的候选清单（见 _non_loopback_ips）。新增虚拟化栈照惯例往前缀里补。
# 与 scripts/addnode_template.sh 的 VIRTUAL_IFACE_PREFIXES 同步维护（引导脚本
# 交互确认与容器内上报用同一套过滤口径）。
# 注意只列 "br-"（docker 自建网络桥 br-<id>）：br0 这类管理员手建的 LAN 桥是
# 真实互联地址，不能滤。
_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "kube", "cni", "flannel", "calico", "tunl", "cbr0")


def collect_system_info() -> SystemInfo:
    """采集系统信息：静态 + 运行时指标。"""
    cpu_cores = _collect_cpu_cores()
    cpu_arch = platform.machine()

    memory_total_gb, memory_used_gb, memory_percent = _collect_memory()
    swap_total_gb, swap_used_gb, swap_percent = _collect_swap()
    cpu_percent, load_avg = _collect_cpu_runtime()
    disk_total_gb, disk_used_gb, disk_percent = _collect_disk()
    os_name, os_version, kernel_version = _collect_os_info()
    uptime_seconds = _collect_uptime()
    ip, ip_candidates, ip_explicit, net_ifname = _collect_static_addrs()

    return SystemInfo(
        # 基础（静态）
        hostname=settings.node_name or socket.gethostname(),
        ip=ip,
        ip_candidates=ip_candidates,
        ip_explicit=ip_explicit,
        net_ifname=net_ifname,
        cpu_cores=cpu_cores,
        cpu_arch=cpu_arch,
        memory_gb=round(memory_total_gb, 2),
        # 静态扩展：OS
        os_name=os_name,
        os_version=os_version,
        kernel_version=kernel_version,
        uptime_seconds=round(uptime_seconds, 1),
        # 运行时：CPU
        cpu_percent=round(cpu_percent, 1),
        cpu_load_avg_1=round(load_avg[0], 2),
        cpu_load_avg_5=round(load_avg[1], 2),
        cpu_load_avg_15=round(load_avg[2], 2),
        # 运行时：内存
        memory_used_gb=round(memory_used_gb, 2),
        memory_percent=round(memory_percent, 1),
        # 运行时：swap
        swap_total_gb=round(swap_total_gb, 2),
        swap_used_gb=round(swap_used_gb, 2),
        swap_percent=round(swap_percent, 1),
        # 运行时：磁盘
        disk_total_gb=round(disk_total_gb, 2),
        disk_used_gb=round(disk_used_gb, 2),
        disk_percent=round(disk_percent, 1),
    )


def collect_node_info() -> NodeReport:
    """聚合系统 + GPU 采集，返回 NodeReport。

    供 TunnelClient.info_collector 注入使用（$hello/$heartbeat）；与
    NodeServiceImpl.info() 共用，保证心跳和按需 RPC 上报口径一致。
    任何采集异常均被各子采集器内部兜底，因此本函数不会抛出。
    （引擎 /metrics 指标已改由管理端 scraper 直接抓取，不经心跳上报。）
    """
    # 延迟导入避免模块初始化期循环依赖
    import llmpod
    from llmpod.apps.node.collectors.gpu import collect_gpu_info

    sys_info = collect_system_info()
    gpu_info = collect_gpu_info()
    return NodeReport(
        **sys_info.model_dump(),
        gpus=gpu_info,
        capabilities=["docker", "shell", "inference"],
        version=llmpod.__version__,
    )


# ---- 静态信息 ----


def _collect_cpu_cores() -> int:
    """获取 CPU 逻辑核心数。"""
    try:
        import psutil

        return psutil.cpu_count(logical=True) or os.cpu_count() or 1
    except ImportError:
        return os.cpu_count() or 1


def _collect_os_info() -> tuple[str, str, str]:
    """获取操作系统名称、版本、内核版本。"""
    os_name = platform.system() or ""
    os_version = platform.release() or ""
    kernel_version = platform.version() or ""
    return os_name, os_version, kernel_version


def _collect_uptime() -> float:
    """获取系统启动至今的秒数。"""
    try:
        import psutil

        return time.time() - psutil.boot_time()
    except ImportError:
        return _read_proc_uptime()
    except Exception:
        return 0.0


def _read_proc_uptime() -> float:
    """psutil 不可用时的回退：读取 /proc/uptime。"""
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _get_local_ip(pairs: list[tuple[str, str]] | None = None) -> str:
    """获取本机对外 advertise 的局域网 IP（多机 rendezvous / API 转发用，须是别的节点可达的地址）。

    优先级：
    - 显式 ``settings.node_advertise_ip``（bridge 网络 / 非标准拓扑手动指定宿主可达 IP）；
    - UDP connect 探默认路由源 IP（host 网络下 = 宿主 LAN IP）；
    - 枚举非 loopback 网卡兜底；
    - 彻底拿不到 → 空串。**绝不回退 127.0.0.1**——loopback 无法被其他节点访问，
      回退它会让多机 rendezvous 静默指向错误地址。
    """
    explicit = (settings.node_advertise_ip or "").strip()
    if explicit:
        return explicit
    # 1. UDP connect 探默认路由接口（拿到「对外」源 IP）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    # 2. 枚举非 loopback 网卡兜底（8.8.8.8 不可达的内网环境；已过滤 loopback，直接取首个）
    for ip in _non_loopback_ips(pairs):
        return ip
    return ""


def _collect_static_addrs() -> tuple[str, list[str], bool, str]:
    """静态采集四件套 (ip, ip_candidates, ip_explicit, net_ifname)——候选清单与 ``ip`` 字段同源。

    显式配置（node_advertise_ip / 引导参数 node_ip）独占清单并置 explicit=True：
    管理员显式固定的地址 manager 采信不换选（含重新指定修正）。自动路径下 ``ip`` 即候选
    首位（UDP 默认路由源 IP，跨机可达性最好的一个），其余网卡地址按私网优先跟在
    后面（见 _non_loopback_ips 的排序）。候选清单仅诊断参考——manager 不再据此
    协商；未显式配置的节点由 manager 按自报 ``ip`` 收下并告警（未显式固定）。

    net_ifname = 持有 ``ip`` 的物理网卡名（见 _ifname_for_ip）：多机部署的
    NCCL/GLOO SOCKET_IFNAME 注入用。IP 由人工配置或路由探测选定，网卡名是它的函数。
    """
    explicit = (settings.node_advertise_ip or "").strip()
    if explicit:
        return explicit, [explicit], True, _ifname_for_ip(explicit)
    # 网卡枚举一轮一次，三个消费方（本机 IP 兜底 / 候选清单 / 网卡名反查）共用
    pairs = list(_physical_ipv4_addrs())
    ip = _get_local_ip(pairs)
    candidates = [ip] if ip else []
    for nic_ip in _non_loopback_ips(pairs):
        if nic_ip and nic_ip not in candidates:
            candidates.append(nic_ip)
    return ip, candidates, False, _ifname_for_ip(ip, pairs)


def _ifname_for_ip(ip: str, pairs: list[tuple[str, str]] | None = None) -> str:
    """反查持有该 IPv4 的物理网卡名（虚拟桥接口整块跳过）；未命中返回空串。

    只用 psutil——getaddrinfo 那类拿不到接口名的来源不参与，ifname 宁缺毋滥
    （空串时消费方回落用户显式配置与代码兜底）。node 容器与引擎容器同为
    host 网络（同一 netns），这里看到的名字与引擎内 Gloo/NCCL 认的逐字一致。
    """
    if not ip:
        return ""
    iface_pairs = pairs if pairs is not None else _physical_ipv4_addrs()
    for name, addr in iface_pairs:
        if addr == ip:
            return name
    return ""


def _physical_ipv4_addrs():
    """枚举物理网卡的 (网卡名, IPv4 地址) 对（虚拟桥接口整块跳过；含 127.* 剔除）。

    候选清单（_non_loopback_ips 的 psutil 段）与网卡名反查（_ifname_for_ip）共用
    这一份枚举——过滤口径只此一处。psutil 缺失/异常降级为空序列并告警。
    """
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith(_VIRTUAL_IFACE_PREFIXES):
                continue
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    yield name, a.address
    except Exception:
        log.warning("网卡枚举失败，地址候选与网卡名反查降级为空", exc_info=True)


def _is_private_ip(ip: str) -> bool:
    """是否私网地址（RFC1918 等；解析失败按公网处理）。"""
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def _non_loopback_ips(pairs: list[tuple[str, str]] | None = None):
    """枚举本机非 loopback IPv4 地址（psutil 优先，getaddrinfo 兜底；私网地址排前）。

    私网优先理由：节点互联走内网是常态，公网/特殊网卡地址排后面（清单作诊断
    参考时的阅读序）。容器/虚拟网桥接口（见 _VIRTUAL_IFACE_PREFIXES）的地址整块
    剔除：桥地址只有挂在该桥上的容器可达，其他机器路由不到，报上去只会误导
    排障（引导脚本的同名清单同口径）。getaddrinfo 兜底路径拿不到接口名，无法
    过滤（按主机名解析，极少命中桥地址）。
    """
    iface_pairs = pairs if pairs is not None else _physical_ipv4_addrs()
    raw = [addr for _name, addr in iface_pairs]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                raw.append(ip)
    except Exception:
        pass
    raw.sort(key=lambda ip: not _is_private_ip(ip))
    return raw


# ---- 运行时指标 ----


def _collect_memory() -> tuple[float, float, float]:
    """获取 (total_gb, used_gb, percent)。

    used 使用 psutil 的 used 语义（active 进程占用），无 psutil 时回退到 /proc/meminfo。
    """
    try:
        import psutil

        mem = psutil.virtual_memory()
        return mem.total / _GB, mem.used / _GB, mem.percent
    except ImportError:
        return _read_meminfo()
    except Exception:
        return 0.0, 0.0, 0.0


def _read_meminfo() -> tuple[float, float, float]:
    """psutil 不可用时的回退：读取 /proc/meminfo（only MemTotal/MemAvailable）。"""
    try:
        total_kb = 0
        avail_kb = 0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
        total_gb = total_kb / (1024**2)
        used_gb = (total_kb - avail_kb) / (1024**2)
        percent = (used_gb / total_gb * 100) if total_gb > 0 else 0.0
        return total_gb, used_gb, percent
    except Exception:
        return 0.0, 0.0, 0.0


def _collect_swap() -> tuple[float, float, float]:
    """获取 (swap_total_gb, swap_used_gb, swap_percent)。"""
    try:
        import psutil

        swap = psutil.swap_memory()
        return swap.total / _GB, swap.used / _GB, swap.percent
    except Exception:
        return 0.0, 0.0, 0.0


def _collect_cpu_runtime() -> tuple[float, tuple[float, float, float]]:
    """获取 (cpu_percent, (load_1, load_5, load_15))。

    cpu_percent 首次调用返回 0.0（需有 baseline），因此调用 interval=None 的非阻塞模式，
    返回自上次调用以来的使用率；首次调用回退到 load_avg 估算。
    """
    cpu_percent = 0.0
    try:
        import psutil

        cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        pass

    load_avg = (0.0, 0.0, 0.0)
    try:
        load = os.getloadavg()
        load_avg = (float(load[0]), float(load[1]), float(load[2]))
    except (AttributeError, OSError):
        # Windows 无 getloadavg，回退到 psutil
        try:
            import psutil

            load = psutil.getloadavg()
            load_avg = (float(load[0]), float(load[1]), float(load[2]))
        except Exception:
            pass

    return cpu_percent, load_avg


def _collect_disk() -> tuple[float, float, float]:
    """获取根分区的 (disk_total_gb, disk_used_gb, disk_percent)。"""
    try:
        import psutil

        usage = psutil.disk_usage("/")
        return usage.total / _GB, usage.used / _GB, usage.percent
    except Exception:
        return 0.0, 0.0, 0.0
