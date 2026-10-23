"""GPU 驱动/设备预检。

采集器在 ``probe_available()`` 里调用这些函数做闸门：先确认驱动装好、
设备对本进程可见，再决定是否真的去打开驱动。没有这层闸门时，无卡 /
未直通设备节点的节点会每个心跳都失败一次，既刷日志又白耗时间。

本模块只放**厂商无关**的探测逻辑（PCI 厂商扫描、设备节点枚举）：设备节点的
命名模式由调用方传入，这里不掺任何厂商知识，新增厂商无需改本文件。
"""

from __future__ import annotations

from pathlib import Path

_PCI_DEVICES_DIR = Path("/sys/bus/pci/devices")
_OS_RELEASE = Path("/etc/os-release")
_KERNEL_VERSION = Path("/proc/version")
_DEV_DIR = Path("/dev")


def is_wsl() -> bool:
    """是否跑在 WSL 下。

    WSL 的 ``/sys/bus/pci/devices`` 不反映真实 GPU，PCI 预检在上面会误判。
    两个来源都看——不同 WSL 发行版只在其中之一留痕（os-release 是发行版的，
    /proc/version 带微软内核后缀）。
    """
    for path in (_OS_RELEASE, _KERNEL_VERSION):
        try:
            content = path.read_text().lower()
        except OSError:
            continue
        if "microsoft" in content or "wsl" in content:
            return True
    return False


def pci_vendor_present(vendor_id: str) -> bool | None:
    """扫描 PCI 设备，确认指定厂商的硬件是否存在。

    ``vendor_id`` 形如 ``"0x10de"``（NVIDIA）/ ``"0x19e5"``（Ascend）。
    返回 ``True`` / ``False``；sysfs 不可读或处于 WSL 时返回 ``None``，
    表示「无法预检」——调用方应跳过该项检查，不能据此判定无卡。
    """
    if is_wsl():
        return None

    try:
        entries = list(_PCI_DEVICES_DIR.iterdir())
    except OSError:
        return None

    wanted = vendor_id.lower()
    for entry in entries:
        try:
            vendor = (entry / "vendor").read_text().strip().lower()
        except OSError:
            continue
        if vendor == wanted:
            return True
    return False


def device_nodes(patterns: tuple[str, ...]) -> list[str]:
    """列出 ``/dev`` 下匹配任一 glob 模式的设备节点（去重、按路径排序）。

    模式由厂商采集器自己给（各厂商命名不同）。空列表说明容器没做设备直通
    （或宿主机没装驱动），此时驱动侧也必然取不到数据，可直接跳过该厂商。
    """
    found: set[str] = set()
    for pattern in patterns:
        for path in _DEV_DIR.glob(pattern):
            found.add(str(path))
    return sorted(found)
