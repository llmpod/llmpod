"""DCMI ctypes 绑定（``gpu/dcmi.py``）单元测试。

用假函数替身替代 ``libdcmi.so``，只验绑定层自己的逻辑：结构体布局、错误码
翻译、降级判定、库/符号缺失的处理路径。真实驱动的返回值不在这里验。
"""

from __future__ import annotations

import ctypes

import pytest

from llmpod.apps.node.collectors.gpu import dcmi


class _FakeLibrary:
    """函数名 → Python 可调用对象的替身，用来顶掉真实 ``libdcmi.so``。"""

    def __init__(self, functions: dict):
        self.functions = functions

    def __getattr__(self, name: str):
        try:
            return self.functions[name]
        except KeyError:
            raise AttributeError(name) from None


@pytest.fixture
def fake_functions(monkeypatch):
    """把 ``dcmi._func`` 指向替身函数表，并复位模块级状态。

    返回可写的函数表：用例往里塞 ``函数名 -> 可调用`` 即可。
    """
    functions: dict = {}
    library = _FakeLibrary(functions)
    monkeypatch.setattr(dcmi, "_func", lambda name: getattr(library, name))
    monkeypatch.setattr(dcmi, "_ready", False)
    return functions


# ---- 结构体布局（对齐 dcmi_interface_api.h，错位会让驱动越界写）----


def test_struct_sizes_match_header():
    assert ctypes.sizeof(dcmi._ChipInfoV2) == 132
    assert ctypes.sizeof(dcmi._HbmInfo) == 32
    assert ctypes.sizeof(dcmi._MemoryInfoV3) == 112
    assert ctypes.sizeof(dcmi._ElabelInfo) == 1280


def test_structs_are_naturally_aligned():
    """不能加 _pack_：压缩会把 8 字节字段顶错位。"""
    assert ctypes.alignment(dcmi._HbmInfo) == 8
    assert ctypes.alignment(dcmi._MemoryInfoV3) == 8


# ---- 判错与降级码 ----


def test_check_rejects_non_zero():
    with pytest.raises(dcmi.DcmiError) as excinfo:
        dcmi._check(dcmi.DCMI_ERR_NOT_SUPPORT, "dcmi_get_device_power_info")

    assert excinfo.value.code == dcmi.DCMI_ERR_NOT_SUPPORT
    assert excinfo.value.func == "dcmi_get_device_power_info"


def test_check_accepts_ok():
    dcmi._check(dcmi.DCMI_OK, "dcmi_init")


@pytest.mark.parametrize(
    "code",
    [
        dcmi.DCMI_ERR_NOT_SUPPORT,
        dcmi.DCMI_ERR_NOT_SUPPORT_IN_CONTAINER,
        dcmi.DCMI_ERR_OPER_NOT_PERMITTED,
        dcmi.DCMI_ERR_FUNCTION_NOT_FOUND,
    ],
)
def test_is_degradable_true(code):
    assert dcmi.is_degradable(dcmi.DcmiError(code, "f")) is True


@pytest.mark.parametrize("code", [-8006, -8008, -8009])
def test_is_degradable_false_for_driver_faults(code):
    """超时/设备不存在这类码说明驱动本身有问题，不该偷偷降级。"""
    assert dcmi.is_degradable(dcmi.DcmiError(code, "f")) is False


def test_text_decodes_and_replaces_bad_bytes():
    assert dcmi._text(b"310P3") == "310P3"
    assert dcmi._text(b"310\xffP3") == "310�P3"


# ---- 库加载 ----


def test_load_library_returns_first_available(monkeypatch):
    """按候选路径顺序试，第一个能加载的就用它。"""
    attempted: list[str] = []
    sentinel = object()

    def fake_cdll(path):
        attempted.append(path)
        if path == "libdcmi.so":
            raise OSError("not found")
        return sentinel

    monkeypatch.setattr(dcmi, "_library", None)
    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)

    assert dcmi._load_library() is sentinel
    assert attempted == ["libdcmi.so", "/usr/local/Ascend/driver/lib64/driver/libdcmi.so"]


def test_load_library_raises_local_code_when_all_missing(monkeypatch):
    monkeypatch.setattr(dcmi, "_library", None)

    def fake_cdll(path):
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)

    with pytest.raises(dcmi.DcmiError) as excinfo:
        dcmi._load_library()
    assert excinfo.value.code == dcmi.DCMI_ERR_LIBRARY_NOT_FOUND


def test_func_translates_missing_symbol(monkeypatch):
    """符号不存在时抛本地码，调用方能按码降级而不是撞 AttributeError。"""
    monkeypatch.setattr(dcmi, "_library", _FakeLibrary({}))

    with pytest.raises(dcmi.DcmiError) as excinfo:
        dcmi._func("dcmi_does_not_exist")
    assert excinfo.value.code == dcmi.DCMI_ERR_FUNCTION_NOT_FOUND


# ---- 初始化 ----


def test_init_is_idempotent(fake_functions):
    calls: list[int] = []

    def fake_init() -> int:
        calls.append(1)
        return 0

    fake_functions["dcmi_init"] = fake_init

    dcmi.init()
    dcmi.init()

    assert calls == [1]


def test_init_failure_is_not_cached(fake_functions):
    """失败不缓存：可用性重试要能真的重新初始化一次驱动。"""
    results = [dcmi.DCMI_ERR_NOT_SUPPORT, dcmi.DCMI_OK]
    fake_functions["dcmi_init"] = lambda: results.pop(0)

    with pytest.raises(dcmi.DcmiError):
        dcmi.init()

    dcmi.init()  # 第二次应当重新调用并成功


# ---- 查询映射 ----


def test_get_card_list_reads_only_returned_count(fake_functions):
    """卡 ID 不保证是 0..N-1，且只取 count 指定的前若干个。"""

    def fake(card_num, card_list, list_len):
        assert list_len == dcmi.MAX_CARDS
        card_num._obj.value = 2
        card_list[0] = 8
        card_list[1] = 9
        card_list[2] = 99  # 超出 count，必须被丢掉
        return 0

    fake_functions["dcmi_get_card_list"] = fake

    assert dcmi.get_card_list() == [8, 9]


def test_get_driver_version(fake_functions):
    def fake(buffer, length):
        assert length.value == dcmi.DRIVER_VERSION_LEN
        buffer.value = b"25.2.3"
        return 0

    fake_functions["dcmi_get_driver_version"] = fake

    assert dcmi.get_driver_version() == "25.2.3"


def test_get_memory_prefers_hbm(fake_functions):
    def fake_hbm(card_id, device_id, info):
        info._obj.memory_size = 65536
        info._obj.memory_usage = 1024
        return 0

    fake_functions["dcmi_get_device_hbm_info"] = fake_hbm

    assert dcmi.get_memory_mb(0, 0) == (65536, 1024)


@pytest.mark.parametrize("hbm_outcome", ["unsupported", "zero_size"])
def test_get_memory_falls_back_to_generic_query(fake_functions, hbm_outcome):
    """无 HBM 的型号（310P/310B 等）要退回通用内存查询。

    两种表现都得覆盖：老机型对 HBM 查询直接报「不支持」，另一些调用成功但报 0。
    """

    def fake_hbm(card_id, device_id, info):
        if hbm_outcome == "zero_size":
            info._obj.memory_size = 0
            return 0
        return dcmi.DCMI_ERR_NOT_SUPPORT

    def fake_memory_v3(card_id, device_id, info):
        info._obj.memory_size = 21527
        info._obj.memory_available = 19677
        return 0

    fake_functions["dcmi_get_device_hbm_info"] = fake_hbm
    fake_functions["dcmi_get_device_memory_info_v3"] = fake_memory_v3

    assert dcmi.get_memory_mb(8, 0) == (21527, 1850)


def test_get_power_draw_converts_tenth_watt(fake_functions):
    """驱动返回 0.1W 单位。"""

    def fake(card_id, device_id, power):
        power._obj.value = 128
        return 0

    fake_functions["dcmi_get_device_power_info"] = fake

    assert dcmi.get_power_draw_w(8, 0) == 12.8


def test_get_chip_name(fake_functions):
    def fake(card_id, device_id, info):
        info._obj.chip_name = b"310P3"
        return 0

    fake_functions["dcmi_get_device_chip_info_v2"] = fake

    assert dcmi.get_chip_name(8, 0) == "310P3"


def test_get_card_serial(fake_functions):
    def fake(card_id, elabel):
        elabel._obj.serial_number = b"SN10SYNTH000027068"
        return 0

    fake_functions["dcmi_get_card_elabel_v2"] = fake

    assert dcmi.get_card_serial(8) == "SN10SYNTH000027068"


def test_get_utilization_asks_for_aicore(fake_functions):
    """input_type 必须是 AICore，否则拿到的是别的口径的利用率。"""
    seen: list[int] = []

    def fake(card_id, device_id, input_type, rate):
        seen.append(input_type)
        rate._obj.value = 42
        return 0

    fake_functions["dcmi_get_device_utilization_rate"] = fake

    assert dcmi.get_utilization(8, 0) == 42
    assert seen == [dcmi.DCMI_UTILIZATION_RATE_AICORE]


def test_get_temperature(fake_functions):
    def fake(card_id, device_id, temperature):
        temperature._obj.value = 79
        return 0

    fake_functions["dcmi_get_device_temperature"] = fake

    assert dcmi.get_temperature(8, 0) == 79


def test_query_raises_on_driver_error(fake_functions):
    fake_functions["dcmi_get_device_temperature"] = lambda card_id, device_id, out: -8006

    with pytest.raises(dcmi.DcmiError) as excinfo:
        dcmi.get_temperature(8, 0)
    assert excinfo.value.code == -8006
