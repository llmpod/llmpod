"""系统信息采集 collectors/system.py 补充测试（psutil 桩 / 导入失败注入，无 GPU 依赖）。

既有 tests/unit/test_collectors.py 覆盖了 happy path；本文件专打降级与兜底分支：
- psutil 缺失（sys.modules 置 None → import 抛 ImportError）→ /proc 回退；
- psutil 调用抛异常 → 归零兜底；
- load_avg 的 Windows 回退链（os.getloadavg 失败 → psutil.getloadavg → 归零）；
- _get_local_ip 三级优先级（显式 settings > UDP 探测 > 网卡枚举 > 空串）；
- collect_node_info 聚合（GPU 采集打桩为空，不触任何 GPU 路径）。

psutil 桩经 sys.modules 注入（本进程未装 psutil 时 `import psutil` 直接命中桩）。
"""

from __future__ import annotations

import socket
import sys
import time
import types

import pytest

from llmpod.apps.node.collectors import system as sys_mod
from llmpod.config.settings import settings

_GB = 1024**3


class _FakePsutil(types.ModuleType):
    """可控行为的 psutil 替身：默认正常返回，测试内再逐方法覆写/置炸。"""

    def __init__(self):
        super().__init__("psutil")
        self.cpu_count_value: int | None = 8
        self.boot_time_value: float = time.time() - 3600

    def cpu_count(self, logical=True):
        return self.cpu_count_value

    def virtual_memory(self):
        return types.SimpleNamespace(total=16 * _GB, used=8 * _GB, percent=50.0)

    def swap_memory(self):
        return types.SimpleNamespace(total=2 * _GB, used=_GB, percent=50.0)

    def cpu_percent(self, interval=None):
        return 12.5

    def getloadavg(self):
        return (1.0, 2.0, 3.0)

    def boot_time(self):
        return self.boot_time_value

    def disk_usage(self, path):
        return types.SimpleNamespace(total=100 * _GB, used=40 * _GB, percent=40.0)

    def net_if_addrs(self):
        lo = types.SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")
        eth = types.SimpleNamespace(family=socket.AF_INET, address="192.168.1.10")
        return {"lo": [lo], "eth0": [eth]}


@pytest.fixture
def fake_psutil(monkeypatch):
    mod = _FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", mod)
    monkeypatch.setattr(settings, "node_advertise_ip", "", raising=False)
    return mod


class _BoomPsutil(_FakePsutil):
    """所有方法全炸的 psutil：验证调用侧 except Exception 归零兜底。"""

    def _boom(self, *a, **k):
        raise RuntimeError("psutil exploded")

    cpu_count = _boom
    virtual_memory = _boom
    swap_memory = _boom
    cpu_percent = _boom
    getloadavg = _boom
    boot_time = _boom
    disk_usage = _boom
    net_if_addrs = _boom


class _NoNetworkSocket:
    """mock socket：UDP connect 抛异常（模拟 8.8.8.8 不可达）。"""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def connect(self, *a):
        raise OSError("network unreachable")

    def getsockname(self):
        return ("0.0.0.0", 0)


class TestStaticAddrCandidates:
    """_collect_static_addrs 四件套（ip / 候选清单 / explicit 标志 / IP 所在网卡名）。"""

    def test_explicit_config_pins_single_candidate(self, fake_psutil, monkeypatch):
        monkeypatch.setattr(settings, "node_advertise_ip", "10.1.2.3")
        ip, candidates, explicit, net_ifname = sys_mod._collect_static_addrs()
        assert ip == "10.1.2.3"
        assert candidates == ["10.1.2.3"]
        assert explicit is True
        # 显式指定的 IP 不在本机网卡上 → 网卡名空串（消费方回落显式配置与兜底）
        assert net_ifname == ""

    def test_auto_detect_ip_leads_candidates(self, fake_psutil, monkeypatch):
        """UDP 探测值 = ip = 候选首位；网卡地址（私网排序）跟在后面。"""
        monkeypatch.setattr(settings, "node_advertise_ip", "")

        class _OkSocket:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def connect(self, *a):
                return None

            def getsockname(self):
                return ("172.20.0.9", 54321)

        monkeypatch.setattr(sys_mod.socket, "socket", _OkSocket)
        ip, candidates, explicit, net_ifname = sys_mod._collect_static_addrs()
        assert ip == "172.20.0.9"
        assert candidates[0] == "172.20.0.9"
        assert "192.168.1.10" in candidates  # 网卡枚举进候选
        assert explicit is False
        assert net_ifname == ""  # UDP 源 IP 不在网卡枚举里（隧道/别名场景）→ 空串

    def test_private_nic_ips_sorted_before_public(self, fake_psutil, monkeypatch):
        """网卡枚举私网地址排前：公网/特殊地址只作协商兜底。"""
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)  # 关 UDP 探测
        lo = types.SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")
        pub = types.SimpleNamespace(family=socket.AF_INET, address="8.8.8.8")
        priv = types.SimpleNamespace(family=socket.AF_INET, address="192.168.1.10")
        fake_psutil.net_if_addrs = lambda: {"eth0": [pub], "eth1": [priv], "lo": [lo]}
        ip, candidates, explicit, net_ifname = sys_mod._collect_static_addrs()
        assert ip == "192.168.1.10"  # 私网网卡领头（无 UDP 探测）
        assert candidates.index("192.168.1.10") < candidates.index("8.8.8.8")
        assert net_ifname == "eth1"  # 自报 ip 的持有卡

    def test_virtual_bridge_ips_excluded_from_candidates(self, fake_psutil, monkeypatch):
        """docker0/br-* 等虚拟桥地址不进候选：其他机器路由不到它们，manager 探活
        还会撞上自己宿主机的同名桥（RST 也算可达）误判成节点可达。"""
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)  # 关 UDP 探测，走网卡枚举
        docker0 = types.SimpleNamespace(family=socket.AF_INET, address="172.17.0.1")
        br_user = types.SimpleNamespace(family=socket.AF_INET, address="172.19.0.1")
        eth = types.SimpleNamespace(family=socket.AF_INET, address="192.168.1.10")
        fake_psutil.net_if_addrs = lambda: {
            "docker0": [docker0],  # 字母序本会排在 eth0 前面
            "br-1a2b3c4d5e6f": [br_user],
            "eth0": [eth],
        }
        ip, candidates, explicit, net_ifname = sys_mod._collect_static_addrs()
        assert "172.17.0.1" not in candidates
        assert "172.19.0.1" not in candidates
        assert ip == "192.168.1.10"  # docker0 被滤掉，不会顶替自报值
        assert candidates == ["192.168.1.10"]
        assert explicit is False
        assert net_ifname == "eth0"

    def test_admin_created_lan_bridge_kept(self, fake_psutil, monkeypatch):
        """br0（无连字符）是管理员手建的 LAN 桥，真实互联地址，不过滤。"""
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)
        br0 = types.SimpleNamespace(family=socket.AF_INET, address="192.168.1.20")
        eth = types.SimpleNamespace(family=socket.AF_INET, address="192.168.1.10")
        fake_psutil.net_if_addrs = lambda: {"br0": [br0], "eth0": [eth]}
        ip, candidates, _, net_ifname = sys_mod._collect_static_addrs()
        assert "192.168.1.20" in candidates
        assert set(candidates) == {"192.168.1.10", "192.168.1.20"}  # br0 未被误滤
        assert net_ifname == "br0"  # LAN 桥是合法的互联网卡（真实互联地址）

    def test_all_fail_empty_candidates_not_explicit(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)
        monkeypatch.setattr(sys_mod.socket, "getaddrinfo", lambda *a, **k: [])
        ip, candidates, explicit, net_ifname = sys_mod._collect_static_addrs()
        assert ip == ""
        assert candidates == []
        assert explicit is False
        assert net_ifname == ""


class TestIfnameForIp:
    """_ifname_for_ip：持有 IP 的物理网卡名反查（虚拟桥跳过，未命中空串）。"""

    def test_hit_physical_nic(self, fake_psutil):
        assert sys_mod._ifname_for_ip("192.168.1.10") == "eth0"

    def test_bridge_only_hit_returns_empty(self, fake_psutil):
        """IP 只长在虚拟桥上 → 不认（桥地址跨机不可达，报了误导注入）。"""
        docker0 = types.SimpleNamespace(family=socket.AF_INET, address="172.17.0.1")
        fake_psutil.net_if_addrs = lambda: {"docker0": [docker0]}
        assert sys_mod._ifname_for_ip("172.17.0.1") == ""

    def test_miss_returns_empty(self, fake_psutil):
        assert sys_mod._ifname_for_ip("10.9.9.9") == ""

    def test_empty_ip_returns_empty(self, fake_psutil):
        assert sys_mod._ifname_for_ip("") == ""

    def test_psutil_failure_returns_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert sys_mod._ifname_for_ip("192.168.1.10") == ""


class TestCollectSystemInfoHappyPath:
    def test_full_collection_with_fake_psutil(self, fake_psutil, monkeypatch):
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)  # 走网卡枚举取 IP
        monkeypatch.setattr(sys_mod.os, "getloadavg", lambda: (1.0, 2.0, 3.0))
        info = sys_mod.collect_system_info()
        assert info.cpu_cores == 8
        assert info.memory_gb == 16.0 and info.memory_used_gb == 8.0 and info.memory_percent == 50.0
        assert info.swap_total_gb == 2.0 and info.swap_used_gb == 1.0 and info.swap_percent == 50.0
        assert info.cpu_percent == 12.5
        assert (info.cpu_load_avg_1, info.cpu_load_avg_5, info.cpu_load_avg_15) == (1.0, 2.0, 3.0)
        assert info.disk_total_gb == 100.0 and info.disk_used_gb == 40.0 and info.disk_percent == 40.0
        assert info.ip == "192.168.1.10"  # 网卡枚举兜底（过滤 loopback）
        assert info.ip_candidates and info.ip_candidates[0] == info.ip  # 候选首位 = 自报值
        assert info.ip_explicit is False
        assert info.hostname == socket.gethostname()
        assert info.uptime_seconds >= 3599  # boot_time 桩 = 1h 前
        assert info.os_name  # platform.system() 非空


class TestLocalIpPriority:
    def test_explicit_advertise_ip_wins_over_all(self, fake_psutil, monkeypatch):
        monkeypatch.setattr(settings, "node_advertise_ip", "10.1.2.3")
        assert sys_mod._get_local_ip() == "10.1.2.3"

    def test_udp_probe_first_when_reachable(self, fake_psutil, monkeypatch):
        """UDP connect 成功且拿到非 loopback 源 IP → 直接采用，不枚举网卡。"""
        monkeypatch.setattr(settings, "node_advertise_ip", "")

        class _OkSocket:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def connect(self, *a):
                return None

            def getsockname(self):
                return ("172.20.0.9", 54321)

        monkeypatch.setattr(sys_mod.socket, "socket", _OkSocket)
        assert sys_mod._get_local_ip() == "172.20.0.9"

    def test_udp_probe_loopback_result_rejected(self, fake_psutil, monkeypatch):
        """UDP 探到 127.x 视为无效 → 落到网卡枚举。"""

        class _LoopbackSocket(_NoNetworkSocket):
            def connect(self, *a):
                return None

            def getsockname(self):
                return ("127.0.0.2", 54321)

        monkeypatch.setattr(sys_mod.socket, "socket", _LoopbackSocket)
        assert sys_mod._get_local_ip() == "192.168.1.10"

    def test_all_sources_fail_returns_empty_not_loopback(self, monkeypatch):
        """psutil 缺失（网卡枚举无源）+ getaddrinfo 空 → 空串，绝不回退 127.0.0.1。"""
        monkeypatch.setitem(sys.modules, "psutil", None)
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.socket, "socket", _NoNetworkSocket)
        monkeypatch.setattr(sys_mod.socket, "getaddrinfo", lambda *a, **k: [])
        assert sys_mod._get_local_ip() == ""  # 绝不回退 127.0.0.1

    def test_getaddrinfo_fallback_when_net_if_addrs_boom(self, monkeypatch):
        """psutil 不可用 + getaddrinfo 可用 → 走 getaddrinfo 兜底。"""
        monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil → ImportError
        monkeypatch.setattr(settings, "node_advertise_ip", "")

        class _NoNet(_NoNetworkSocket):
            pass

        monkeypatch.setattr(sys_mod.socket, "socket", _NoNet)
        monkeypatch.setattr(sys_mod.socket, "gethostname", lambda: "host1")
        monkeypatch.setattr(
            sys_mod.socket,
            "getaddrinfo",
            lambda host, port, family: [(socket.AF_INET, None, None, "", ("10.0.0.77", 0))],
        )
        assert sys_mod._get_local_ip() == "10.0.0.77"


class TestPsutilMissingFallbacks:
    """sys.modules["psutil"]=None → import psutil 抛 ImportError → /proc 回退。"""

    def test_cpu_cores_falls_back_to_os(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        assert sys_mod._collect_cpu_cores() == (sys_mod.os.cpu_count() or 1)

    def test_uptime_falls_back_to_proc(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        uptime = sys_mod._collect_uptime()
        assert uptime > 0  # 容器内 /proc/uptime 可读
        assert abs(uptime - sys_mod._read_proc_uptime()) < 5  # 两次读数近似

    def test_memory_falls_back_to_meminfo(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", None)
        total, used, percent = sys_mod._collect_memory()
        assert total > 0 and used > 0
        assert 0 < percent <= 100

    def test_read_meminfo_parses_proc_format(self, fake_psutil, monkeypatch):
        """_read_meminfo 的解析逻辑（MemTotal/MemAvailable → GB/百分比）。"""

        class _FakeFile:
            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(self._lines)

        lines = [
            "MemTotal:       2097152 kB\n",  # 2 GB
            "MemFree:         1048576 kB\n",
            "MemAvailable:    1048576 kB\n",  # 1 GB available
        ]
        monkeypatch.setattr("builtins.open", lambda *a, **k: _FakeFile(lines))
        total_gb, used_gb, percent = sys_mod._read_meminfo()
        assert total_gb == 2.0
        assert used_gb == 1.0
        assert percent == 50.0

    def test_read_proc_uptime_bad_file_returns_zero(self, fake_psutil, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no /proc")

        monkeypatch.setattr("builtins.open", _boom)
        assert sys_mod._read_proc_uptime() == 0.0


class TestPsutilExceptionFallbacks:
    def test_all_collectors_zero_on_psutil_errors(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _BoomPsutil())
        monkeypatch.setattr(settings, "node_advertise_ip", "")
        monkeypatch.setattr(sys_mod.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError("no loadavg")))
        assert sys_mod._collect_memory() == (0.0, 0.0, 0.0)
        assert sys_mod._collect_swap() == (0.0, 0.0, 0.0)
        assert sys_mod._collect_disk() == (0.0, 0.0, 0.0)
        assert sys_mod._collect_uptime() == 0.0
        # cpu_percent 炸 → 0；load_avg 走 psutil.getloadavg 回退也炸 → (0,0,0)
        assert sys_mod._collect_cpu_runtime() == (0.0, (0.0, 0.0, 0.0))

    def test_loadavg_falls_back_to_psutil(self, fake_psutil, monkeypatch):
        """os.getloadavg 缺失（Windows）→ psutil.getloadavg 回退。"""

        def _no_attr():
            raise AttributeError("no getloadavg on windows")

        monkeypatch.setattr(sys_mod.os, "getloadavg", _no_attr)
        cpu_percent, load_avg = sys_mod._collect_cpu_runtime()
        assert cpu_percent == 12.5
        assert load_avg == (1.0, 2.0, 3.0)


class TestCollectNodeInfo:
    def test_aggregates_system_and_stub_gpu(self, fake_psutil, monkeypatch):
        """NodeReport 聚合：GPU 采集打桩为空（本用例不依赖任何 GPU/驱动）。"""
        import llmpod

        async def _no_gpu():
            return []

        monkeypatch.setattr("llmpod.apps.node.collectors.gpu.collect_gpu_info", lambda: [])
        report = sys_mod.collect_node_info()
        assert report.cpu_cores == 8
        assert report.gpus == []
        assert "docker" in report.capabilities
        assert report.version == llmpod.__version__
