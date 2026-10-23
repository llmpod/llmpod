"""Ascend DCMI（DaVinci Card Management Interface）的 ctypes 绑定。

DCMI 是 Ascend 驱动自带的 C 接口（``libdcmi.so``），``npu-smi`` 本身也是它的
封装。这里只绑定采集需要的那些函数，用 ``ctypes`` 直调，不引第三方依赖。

本模块只负责「调用 + 判错」，不做降级决策：所有函数在驱动返回非 0 时抛
:class:`DcmiError`，调用方按 :func:`is_degradable` 决定是记 0 还是整体失败。

**唯一定义处是驱动自带的 ``dcmi_interface_api.h``**（机器上在
``/usr/local/dcmi/dcmi_interface_api.h``）：下面的结构体字段、常量名与取值全部
照它逐字段抄录，不是从任何 Python 封装转写的。结构体布局由驱动的写入 ABI 决定，
字段名/顺序/类型都不能改，改一处就是错位的读写。

字符串字段一律用 ``c_char * N`` 内联数组而不是 ``c_char_p``——后者在结构体里是
指针，会改变内存布局；也不用 ``c_ubyte * N``，因为 ctypes 只为 ``c_char`` 数组
返回可解码的 ``bytes``。结构体不加 ``_pack_``，走自然对齐。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import c_char, c_int, c_uint, c_ulong, c_ulonglong

log = logging.getLogger("llmpod.apps.node.collectors.gpu.dcmi")

# ---- 常量（取自 dcmi_interface_api.h）----

MAX_CHIP_NAME_LEN = 32  # 芯片名/型号字段长度
MAX_LENTH = 256  # 电子标签字段长度（头文件原拼写）
MAX_CARDS = 64  # dcmi_get_card_list 的入参容量上限
DRIVER_VERSION_LEN = 64  # dcmi_get_driver_version 要求的缓冲区长度

DCMI_OK = 0

# 驱动返回码中表示「这台机器 / 这个容器答不了」的那一类：调用方应降级
# 而不是整体失败。其余码（超时、设备不存在等）说明驱动本身有问题，照抛。
# 取值来自头文件的 DCMI_ERR_CODE_*：DCMI_ERROR_CODE_BASE = -8000，
# 依次 BASE-2 / BASE-13 / BASE-255。
DCMI_ERR_OPER_NOT_PERMITTED = -8002
DCMI_ERR_NOT_SUPPORT_IN_CONTAINER = -8013
DCMI_ERR_NOT_SUPPORT = -8255

# 下面两个不是驱动返回码，是本模块用于表达「库 / 符号取不到」的本地码
DCMI_ERR_FUNCTION_NOT_FOUND = -99998
DCMI_ERR_LIBRARY_NOT_FOUND = -99999

_DEGRADABLE_CODES = frozenset(
    {
        DCMI_ERR_OPER_NOT_PERMITTED,
        DCMI_ERR_NOT_SUPPORT_IN_CONTAINER,
        DCMI_ERR_NOT_SUPPORT,
        DCMI_ERR_FUNCTION_NOT_FOUND,
    }
)

# dcmi_get_device_utilization_rate 的 input_type：头文件里的名字是
# DCMI_UTILIZATION_RATE_AICORE（同一组宏还有 DDR/AICPU/CTRLCPU/HBM 等）
DCMI_UTILIZATION_RATE_AICORE = 2

# 库搜索顺序：先交给 dlopen 按系统搜索路径找（驱动装好后 ldconfig 能解析到），
# 再逐个试驱动包自己带的安装位——容器挂载的未必是同一份，驱动版本之间位置也有别。
# 这两条路径在 310P3 测试机（驱动 25.2.3）上都实际存在。
_LIBRARY_PATHS = (
    "libdcmi.so",
    "/usr/local/Ascend/driver/lib64/driver/libdcmi.so",
    "/usr/local/dcmi/libdcmi.so",
)


class DcmiError(Exception):
    """一次 DCMI 调用失败。``code`` 是驱动返回码，或本模块的 -9999x 本地码。"""

    def __init__(self, code: int, func: str):
        self.code = code
        self.func = func
        super().__init__(f"{func} failed with code {code}")


def is_degradable(error: DcmiError) -> bool:
    """该错误是否属于「本机/本容器不支持」，可以退化成默认值继续采集。"""
    return error.code in _DEGRADABLE_CODES


# ---- C 结构体 ----
#
# 每个类对应 dcmi_interface_api.h 里一个同名 struct，字段逐条照抄。测试用
# ctypes.sizeof 锁定了布局尺寸（132 / 32 / 112 / 1280），改动这里而尺寸变了
# 就说明抄错了字段。


class _ChipInfoV2(ctypes.Structure):
    """struct dcmi_chip_info_v2：芯片型号与 AICore 数。"""

    _fields_ = [
        ("chip_type", c_char * MAX_CHIP_NAME_LEN),
        ("chip_name", c_char * MAX_CHIP_NAME_LEN),
        ("chip_ver", c_char * MAX_CHIP_NAME_LEN),
        ("aicore_cnt", c_uint),
        ("npu_name", c_char * MAX_CHIP_NAME_LEN),
    ]


class _HbmInfo(ctypes.Structure):
    """struct dcmi_hbm_info：片上内存，大小/占用单位 MB。"""

    _fields_ = [
        ("memory_size", c_ulonglong),
        ("freq", c_uint),
        ("memory_usage", c_ulonglong),
        ("temp", c_int),
        ("bandwith_util_rate", c_uint),
    ]


class _MemoryInfoV3(ctypes.Structure):
    """struct dcmi_get_memory_info_stru：通用内存信息，大小/可用单位 MB。

    末位 ``reserve`` 是驱动写入时的缓冲带，不能省——少了它驱动会越界写。
    """

    _fields_ = [
        ("memory_size", c_ulonglong),
        ("memory_available", c_ulonglong),
        ("freq", c_uint),
        ("hugepagesize", c_ulong),
        ("hugepages_total", c_ulong),
        ("hugepages_free", c_ulong),
        ("utiliza", c_uint),
        ("reserve", c_char * 60),
    ]


class _ElabelInfo(ctypes.Structure):
    """struct dcmi_elabel_info：板卡电子标签，含出厂序列号。"""

    _fields_ = [
        ("product_name", c_char * MAX_LENTH),
        ("model", c_char * MAX_LENTH),
        ("manufacturer", c_char * MAX_LENTH),
        ("manufacturer_date", c_char * MAX_LENTH),
        ("serial_number", c_char * MAX_LENTH),
    ]


# ---- 库加载 ----


_library = None
_library_lock = threading.Lock()
_ready = False


def _load_library():
    """加载 ``libdcmi.so``（进程内单例）。全都是找不到时抛本地码错误。"""
    global _library

    if _library is not None:
        return _library

    with _library_lock:
        if _library is not None:
            return _library
        for path in _LIBRARY_PATHS:
            try:
                _library = ctypes.CDLL(path)
            except OSError:
                continue
            log.debug("loaded DCMI library from %s", path)
            return _library
        raise DcmiError(DCMI_ERR_LIBRARY_NOT_FOUND, "load")


def _func(name: str):
    """取库中的函数对象。

    走 ``getattr`` 并翻译成 :class:`DcmiError`，是为了让「符号缺失」与驱动
    返回码走同一条判错路径——旧驱动少某个函数时，调用方能按码降级。
    """
    library = _load_library()
    try:
        return getattr(library, name)
    except AttributeError:
        raise DcmiError(DCMI_ERR_FUNCTION_NOT_FOUND, name) from None


def _check(code: int, name: str) -> None:
    if code != DCMI_OK:
        raise DcmiError(code, name)


def init() -> None:
    """初始化 DCMI（幂等）。

    只缓存成功：失败不缓存，好让上层的可用性重试（隔一段时间再探）能真正
    重新初始化一次驱动，而不是永远卡在首次失败上。
    """
    global _ready
    if _ready:
        return
    _check(_func("dcmi_init")(), "dcmi_init")
    _ready = True


# ---- 查询 ----


def get_driver_version() -> str:
    """驱动版本，等同 ``npu-smi`` 的 Software Version（如 ``25.2.3``）。"""
    name = "dcmi_get_driver_version"
    buffer = ctypes.create_string_buffer(DRIVER_VERSION_LEN)
    _check(_func(name)(buffer, c_uint(DRIVER_VERSION_LEN)), name)
    return _text(buffer.value)


def get_card_list() -> list[int]:
    """在位的卡 ID 列表。

    卡 ID 不保证是 ``0..N-1`` 的连续序号（例如单卡机上可能是 8），
    必须用这里返回的值去索引设备。
    """
    name = "dcmi_get_card_list"
    count = c_int()
    cards = (c_int * MAX_CARDS)()
    _check(_func(name)(ctypes.byref(count), cards, MAX_CARDS), name)
    return list(cards[: count.value])


def get_device_count(card_id: int) -> int:
    """卡内芯片（die）数量，芯片编号为 ``0..count-1``。"""
    name = "dcmi_get_device_num_in_card"
    count = c_int()
    _check(_func(name)(card_id, ctypes.byref(count)), name)
    return count.value


def get_chip_name(card_id: int, device_id: int) -> str:
    """芯片名（如 ``310P3`` / ``910B4``）。"""
    name = "dcmi_get_device_chip_info_v2"
    info = _ChipInfoV2()
    _check(_func(name)(card_id, device_id, ctypes.byref(info)), name)
    return _text(info.chip_name)


def get_memory_mb(card_id: int, device_id: int) -> tuple[int, int]:
    """显存信息，返回 ``(总容量 MB, 已用 MB)``。

    先问 HBM——带片上内存的型号（910 系列）只有它能直接给出已用量。无 HBM 的
    型号（310P/310B 等）返回「不支持」，属预期而非故障，此时退回通用内存查询，
    用它给出的总量与可用量相减得到已用量。
    """
    try:
        name = "dcmi_get_device_hbm_info"
        hbm = _HbmInfo()
        _check(_func(name)(card_id, device_id, ctypes.byref(hbm)), name)
        if hbm.memory_size > 0:
            return int(hbm.memory_size), int(hbm.memory_usage)
    except DcmiError as error:
        log.debug("HBM info unavailable on card %s device %s: %s", card_id, device_id, error)

    name = "dcmi_get_device_memory_info_v3"
    info = _MemoryInfoV3()
    _check(_func(name)(card_id, device_id, ctypes.byref(info)), name)
    total_mb = int(info.memory_size)
    return total_mb, total_mb - int(info.memory_available)


def get_utilization(card_id: int, device_id: int) -> int:
    """AICore 占用率（%）。"""
    name = "dcmi_get_device_utilization_rate"
    rate = c_uint()
    _check(_func(name)(card_id, device_id, DCMI_UTILIZATION_RATE_AICORE, ctypes.byref(rate)), name)
    return rate.value


def get_temperature(card_id: int, device_id: int) -> int:
    """芯片温度（℃）。"""
    name = "dcmi_get_device_temperature"
    temperature = c_int()
    _check(_func(name)(card_id, device_id, ctypes.byref(temperature)), name)
    return temperature.value


def get_power_draw_w(card_id: int, device_id: int) -> float:
    """当前功耗（W）。

    换算系数 10 来自华为接口文档标注的「返回单位 0.1W」——头文件本身没写单位，
    而我们手上的 310P3 对该查询直接返回「不支持」，所以在真机上还没验证过。
    换到支持读功耗的型号（910 系列等）时应先核对一下量级。
    """
    name = "dcmi_get_device_power_info"
    raw = c_int()
    _check(_func(name)(card_id, device_id, ctypes.byref(raw)), name)
    return raw.value / 10.0


def get_card_serial(card_id: int) -> str:
    """卡（板卡）的出厂序列号。

    这是**板卡**级标识：一块板卡上的多颗芯片共用同一个序列号，读它来确定
    采集单元的稳定 ID 时，一个采集单元对应一块板卡。
    """
    name = "dcmi_get_card_elabel_v2"
    elabel = _ElabelInfo()
    _check(_func(name)(card_id, ctypes.byref(elabel)), name)
    return _text(elabel.serial_number)


def _text(raw: bytes) -> str:
    """C 定长字符串转 ``str``。

    ctypes 读 ``c_char * N`` 字段时已在首个 NUL 处截断，这里只做解码；驱动
    填了非 UTF-8 字节时用替换字符兜底，不让一个脏字段打断整轮采集。
    """
    return raw.decode(errors="replace")
