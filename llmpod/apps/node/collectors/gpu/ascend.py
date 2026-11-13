"""华为 Ascend NPU 采集器：走 DCMI 结构化接口（见 ``dcmi.py``）。

DCMI 是驱动自带的 C 接口，``npu-smi`` 本身也是它的封装，所以直接调它拿的是
结构化字段，不必解析命令行输出的文本表格；也不再需要每次采集 fork 子进程。

采集单元是一块卡（DCMI 的 ``card_id``，与 ``npu-smi`` 的 NPU ID 一致）。卡内
多颗芯片（die）的显存按总量求和，利用率/温度取峰值——与本系统 GPU 表「一行
一块卡」的口径一致。

采集指标：

- ``uuid``：板卡出厂序列号（读不到时回退 ``ascend-{card_id}-{name}``）
- ``name`` / ``soc_model``：芯片名（``310P3`` / ``910B4``）
- ``driver_version``：驱动版本（等同 ``npu-smi`` 的 Software Version）
- ``temperature`` / ``power_draw_w``
- ``used_vram_gb`` / ``total_vram_gb`` / ``memory_utilization``
- ``utilization``：AICore 占用率（Ascend 公开指标里唯一的算力利用率）

风扇、时钟、PCIe 吞吐等字段 DCMI 不直接提供（或需特权容器才能读），保持默认 0。
部分型号（310P/310B）不支持查询功耗，此时 ``power_draw_w`` 记 0。
"""

from __future__ import annotations

import logging
import os
import re

from llmpod.apps.node.collectors.gpu import dcmi
from llmpod.apps.node.collectors.gpu.base import GpuCollectorBase
from llmpod.apps.node.collectors.gpu.probe import device_nodes
from llmpod.tunnel.schemas.system import GpuInfo

log = logging.getLogger("llmpod.apps.node.collectors.gpu.ascend")

_MB_PER_GB = 1024.0

# Ascend 设备节点：裸卡 /dev/davinciN，vNPU /dev/vdavinciN
_DAVINCI_NODE_PATTERNS = ("davinci[0-9]*", "vdavinci[0-9]*")

# 驱动认为「当前进程不算普通容器」所需的有效能力位：bit 0..37 全置位
# （0x3fffffffff）。这是驱动内核模块里写死的判定，不是我们加的限制；掩码随
# 驱动版本走，升级驱动后需要复核。
_REQUIRED_CAP_EFFECTIVE = 0x3FFFFFFFFF

_CGROUP_MEMORY_PATH = "/sys/fs/cgroup/memory"


def check_admin_container() -> str:
    """当前进程在 Ascend 驱动眼里是否算「管理员容器」；算则返回空串，不算返回原因。

    驱动判定「是不是普通容器」要三条同时成立：进程 uid 为 0、有效能力集覆盖
    bit 0..37、``/sys/fs/cgroup/memory`` 不是只读挂载。缺任何一条，DCMI 初始化
    就会把本容器登记进驱动的容器表，见 :class:`AscendCollector` 的类注释。

    只看 ``/proc`` 与 ``/sys``，不调用驱动的任何接口：驱动那边查容器标志的
    ioctl 本身就会完成登记，问它等于先踩坑再问有没有坑。
    """
    uid = os.getuid()
    if uid != 0:
        return f"进程 uid 是 {uid}，不是 root"

    cap_effective = _cap_effective()
    if cap_effective is None:
        return "读不到 /proc/self/status 里的 CapEff"
    if (cap_effective & _REQUIRED_CAP_EFFECTIVE) != _REQUIRED_CAP_EFFECTIVE:
        return f"有效能力集不足（CapEff={cap_effective:#x}）"

    try:
        flags = os.statvfs(_CGROUP_MEMORY_PATH).f_flag
    except OSError:
        # cgroup v2 宿主没有这个控制器目录。驱动查不到只读挂载时同样不会据此
        # 认定普通容器，这里按放行处理；驱动在 cgroup v2 上的这一格尚未在
        # Ascend 机器上实测，属于按推断放行。
        return ""
    if flags & os.ST_RDONLY:
        return f"{_CGROUP_MEMORY_PATH} 是只读挂载"
    return ""


def _cap_effective() -> int | None:
    """读 ``/proc/self/status`` 的 CapEff；读不到返回 None。"""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as status:
            for line in status:
                if line.startswith("CapEff:"):
                    return int(line.split(":", 1)[1], 16)
    except OSError:
        return None
    return None


# Ascend SoC 型号规范化（芯片名 → 镜像 tag 后缀）：310P3→310p / 910B4→910b /
# 910A→910a / A3→a3（尾巴修订号去掉，匹配 vllm-ascend tag 后缀）
_SOC_NORMALIZE = [
    (re.compile(r"310P\d*", re.IGNORECASE), "310p"),
    (re.compile(r"910B\d*", re.IGNORECASE), "910b"),
    (re.compile(r"910A\d*", re.IGNORECASE), "910a"),
    (re.compile(r"A3\b", re.IGNORECASE), "a3"),
]


def _normalize_soc(name: str) -> str:
    """规范化 Ascend SoC 型号为镜像 tag 后缀。未知型号兜底取首个 token 小写。"""
    if not name:
        return ""
    for pattern, replacement in _SOC_NORMALIZE:
        if pattern.search(name):
            return replacement
    return name.strip().split()[0].lower()


class AscendCollector(GpuCollectorBase):
    """华为 Ascend NPU 采集器（基于 DCMI）。

    ``probe_available()`` 逐层确认：有 davinci 设备节点（容器做了设备直通）
    → 本进程在驱动眼里不是普通容器（节点容器必须带 ``--privileged``）→ DCMI
    能初始化成功（驱动真的在响应）。只看 ``/dev`` 不够——节点可能挂载了设备
    节点但驱动侧不可用；而特权这一条更不能省，缺了它会连带弄坏同机的推理
    引擎，见 :func:`check_admin_container`。
    """

    vendor = "ascend"
    arch = "cann"  # 软件栈名（Ascend 的软件栈叫 CANN）

    def probe_available(self) -> bool:
        if not device_nodes(_DAVINCI_NODE_PATTERNS):
            log.warning("no davinci device nodes under /dev, Ascend NPU collection disabled")
            return False

        reason = check_admin_container()
        if reason:
            _log_missing_privilege(reason)
            return False

        try:
            dcmi.init()
        except dcmi.DcmiError as error:
            log.warning("DCMI init failed, Ascend NPU collection disabled: %s", error)
            return False
        return True

    def collect(self) -> list[GpuInfo]:
        """采集所有在位的 Ascend 卡；一块都取不到时返回空列表。"""
        reason = check_admin_container()
        if reason:
            _log_missing_privilege(reason)
            return []

        try:
            dcmi.init()
            driver_version = dcmi.get_driver_version()
            card_ids = dcmi.get_card_list()
        except dcmi.DcmiError as error:
            log.warning("Ascend NPU collection failed: %s", error)
            return []

        gpus: list[GpuInfo] = []
        for index, card_id in enumerate(card_ids):
            try:
                gpus.append(self._collect_card(card_id, index, driver_version))
            except dcmi.DcmiError as error:
                # 单卡失败不影响同机其它卡——驱动偶发单卡异常时仍能上报其余卡
                log.warning("Ascend card %s collection failed: %s", card_id, error)
        return gpus

    # ---- 内部辅助 ----

    def _collect_card(self, card_id: int, index: int, driver_version: str) -> GpuInfo:
        """采集一块卡：卡内各芯片的显存求和、利用率与温度取峰值、功耗求和。"""
        device_count = dcmi.get_device_count(card_id)

        name = ""
        used_mb = 0
        total_mb = 0
        aicore_pct = 0
        temperature = 0
        power_draw_w = 0.0

        for device_id in range(device_count):
            chip_name = dcmi.get_chip_name(card_id, device_id)
            if not name:
                name = chip_name

            device_total_mb, device_used_mb = dcmi.get_memory_mb(card_id, device_id)
            total_mb += device_total_mb
            used_mb += device_used_mb

            aicore_pct = max(aicore_pct, self._read_utilization(card_id, device_id))
            temperature = max(temperature, self._read_temperature(card_id, device_id))
            power_draw_w += self._read_power_draw_w(card_id, device_id)

        memory_utilization = (used_mb / total_mb * 100) if total_mb > 0 else 0.0

        return GpuInfo(
            uuid=self._read_card_uuid(card_id, name),
            index=index,
            name=name,
            vendor=self.vendor,
            arch=self.arch,
            soc_model=_normalize_soc(name),  # SoC（310P3→310p），写库进 spec jsonb
            # 显存
            used_vram_gb=round(used_mb / _MB_PER_GB, 2),
            total_vram_gb=round(total_mb / _MB_PER_GB, 2),
            memory_utilization=round(memory_utilization, 1),
            # 计算
            utilization=round(aicore_pct, 1),
            # 散热/功耗
            temperature=temperature,
            power_draw_w=round(power_draw_w, 1),
            driver_version=driver_version,
        )

    @staticmethod
    def _read_card_uuid(card_id: int, name: str) -> str:
        """板卡序列号作 uuid，读不到时合成一个稳定的回落值。

        序列号来自电子标签，跨重启稳定，而 GPU 表按 uuid 做 upsert，所以它
        必须是稳定值；合成值带上 card_id，保证同一节点内不重号。
        """
        serial = ""
        try:
            serial = dcmi.get_card_serial(card_id)
        except dcmi.DcmiError as error:
            log.warning("Ascend card %s serial unavailable: %s", card_id, error)
        return serial or f"ascend-{card_id}-{name}"

    @staticmethod
    def _read_utilization(card_id: int, device_id: int) -> int:
        """AICore 占用率；机型/容器不支持时记 0，不影响该卡其余字段。"""
        try:
            return dcmi.get_utilization(card_id, device_id)
        except dcmi.DcmiError as error:
            _log_degraded("AICore utilization", card_id, device_id, error)
            return 0

    @staticmethod
    def _read_temperature(card_id: int, device_id: int) -> int:
        """芯片温度；机型/容器不支持时记 0。"""
        try:
            return dcmi.get_temperature(card_id, device_id)
        except dcmi.DcmiError as error:
            _log_degraded("temperature", card_id, device_id, error)
            return 0

    @staticmethod
    def _read_power_draw_w(card_id: int, device_id: int) -> float:
        """当前功耗；310P/310B 等型号不支持该项查询，此时记 0。"""
        try:
            return dcmi.get_power_draw_w(card_id, device_id)
        except dcmi.DcmiError as error:
            _log_degraded("power", card_id, device_id, error)
            return 0.0


# 上一次报过的「容器不够特权」原因：同一个原因只告警一次。特权只能靠重建容器
# 改变，进程会随之重启、这里的状态归零，所以不需要在成功时清理。
_last_missing_reason: str | None = None


def _log_missing_privilege(reason: str) -> None:
    """记录一次「容器不够特权」的判定：只告警，不初始化 DCMI。

    初始化就会把本容器登记进驱动的容器表，挡掉同机其它容器的推理引擎——
    宁可这一轮不采集。误配的节点每轮探测都会走到这里，同一个原因只在第一次
    告警，之后降为 debug，免得六十秒一条刷满日志。
    """
    global _last_missing_reason
    if reason == _last_missing_reason:
        log.debug("Ascend NPU 采集仍不可用：%s", reason)
        return
    _last_missing_reason = reason
    log.warning(
        "Ascend NPU 采集不可用：节点容器缺少特权标志（%s）。"
        "请用当前引导脚本重建节点容器（引导脚本给所有节点容器都加 --privileged）；"
        "镜像自动升级沿用旧容器的启动参数，补不上这个标志。",
        reason,
    )


def _log_degraded(metric: str, card_id: int, device_id: int, error: dcmi.DcmiError) -> None:
    """记录一次指标降级：不支持的走 debug（机型常态），其余走 warning。"""
    if dcmi.is_degradable(error):
        log.debug("%s unavailable on card %s device %s: %s", metric, card_id, device_id, error)
        return
    log.warning("%s read failed on card %s device %s: %s", metric, card_id, device_id, error)
