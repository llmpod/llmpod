"""hello 期节点互联 IP 选定单元测试（apps/manager/node_addr.py）。

纯本地选定、无网络 IO：显式配置重新指定 > 已存合法值沿用（不重协商，漂移留痕）
> 自报值兜底（未显式固定收下+告警；自报值不可用置空，不把已知坏地址写库）三条
主路径 + 候选清洗细节。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from llmpod.apps.manager import node_addr
from llmpod.tunnel.schemas.system import NodeReport

_LOGGER = "llmpod.apps.manager.node_addr"


def _report(ip: str, explicit: bool = False) -> NodeReport:
    return NodeReport(hostname="h", ip=ip, ip_explicit=explicit)


def _warnings(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.levelno >= logging.WARNING]


class TestExplicitPinned:
    def test_explicit_ip_adopted_over_stored(self, caplog):
        """显式配置是操作者指令：与已存值不同也重新指定（修正错误地址的唯一入口）。"""
        info = _report("10.1.1.5", explicit=True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, "192.0.2.9") == "10.1.1.5"
        assert any("重新指定" in m for m in _warnings(caplog))

    def test_explicit_ip_first_registration(self, caplog):
        """首次注册（无已存值）：显式配置直接采用，无告警路径。"""
        info = _report("10.1.1.5", explicit=True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, None) == "10.1.1.5"
        assert _warnings(caplog) == []

    def test_explicit_same_as_stored_no_warning(self, caplog):
        """显式配置与已存值一致（重连常态）：安静沿用。"""
        info = _report("10.1.1.5", explicit=True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, "10.1.1.5") == "10.1.1.5"
        assert _warnings(caplog) == []

    def test_explicit_vs_stored_compares_cleaned(self, caplog):
        """已存值带空白（历史数据/外部写入）不触发假「重新指定」告警：比较用清洗值。"""
        info = _report("10.1.1.5", explicit=True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, " 10.1.1.5 ") == "10.1.1.5"
        assert _warnings(caplog) == []

    @pytest.mark.parametrize("bad", ["0.0.0.0", "fd00::9", "not-an-ip", "255.255.255.255", ""])
    def test_explicit_unusable_ip_falls_back(self, caplog, bad):
        """固定值不可用 → 忽略并告警：有已存值沿用已存值；没有则置空
        （register_node 兜 0.0.0.0 哨兵），坏值绝不原样落库。"""
        info = _report(bad, explicit=True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, "192.0.2.9") == "192.0.2.9"
            caplog.clear()
            assert node_addr.resolve_node_ip(info, None) == ""
        assert any("不可用" in m for m in _warnings(caplog))


class TestStoredPinned:
    def test_stored_valid_ip_kept_over_self_report(self, caplog):
        """已存合法值沿用：重连自报值变了也不换（不做一致性校验——拓扑变化属预期，
        重协商只会引入换址风险）。"""
        info = _report("203.0.113.7")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, "192.0.2.9") == "192.0.2.9"

    def test_self_report_drift_logged(self, caplog):
        """自报值与已存值漂移留 info 痕迹：沿用是既定口径，但漂移不能无声
        （换网/DHCP/丢 pin env 的可见信号，修正入口 = 重跑引导脚本重新指定）。"""
        info = _report("203.0.113.7")
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            node_addr.resolve_node_ip(info, "192.0.2.9")
        assert any("不一致" in r.message for r in caplog.records)

    def test_stored_invalid_value_not_honored(self, caplog):
        """已存值非法（历史遗留 0.0.0.0 行）不算「已固定」→ 走自报值兜底。"""
        info = _report("192.0.2.9")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, "0.0.0.0") == "192.0.2.9"


class TestUnpinnedFallback:
    def test_no_stored_no_explicit_accepts_self_report_with_warning(self, caplog):
        """未显式固定节点（无合法已存值且未显式配置）：收下自报值并告警。"""
        info = _report("192.168.1.10")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, None) == "192.168.1.10"
        assert any("未显式固定" in m and "192.168.1.10" in m for m in _warnings(caplog))

    def test_accepted_self_report_becomes_stored_next_time(self, caplog):
        """收下后即成为已存值：下次重连走已存值沿用分支，不再告警。"""
        info = _report("192.168.1.10")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            node_addr.resolve_node_ip(info, None)
            caplog.clear()
            assert node_addr.resolve_node_ip(info, "192.168.1.10") == "192.168.1.10"
        assert _warnings(caplog) == []

    @pytest.mark.parametrize("bad", ["0.0.0.0", "fd00::9", "not-an-ip", ""])
    def test_unusable_self_report_emptied_not_stored(self, caplog, bad):
        """自报值不可用 → 置空落库（register_node 兜 0.0.0.0）并告警：已知必断连
        的值写进 Node.ip 会带偏 api_base/master_addr/metrics target，还可能撑破
        String(45) 让注册 flush 失败。"""
        info = _report(bad)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert node_addr.resolve_node_ip(info, None) == ""
        assert any("不可用" in m for m in _warnings(caplog))


class TestIpCleaning:
    """_valid_candidate 清洗口径（与 core.netaddr.clean_advertise_ip 同一实现）：
    与 bootstrap_params 校验、引导脚本 valid_ipv4 同口径（改须同步）；下游按
    IPv4 无中括号拼 URL，非法值不写 Node.ip。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" 10.0.0.8 ", "10.0.0.8"),
            ("10.0.0.8", "10.0.0.8"),
            ("0.0.0.0", ""),
            ("0.1.2.3", ""),
            ("127.0.0.1", ""),
            ("169.254.1.1", ""),
            ("224.0.0.1", ""),  # 组播
            ("239.1.1.1", ""),  # 组播上界
            ("240.0.0.1", ""),  # 保留段（is_reserved）
            ("255.255.255.255", ""),  # 广播
            ("fd00::9", ""),
            ("not-an-ip", ""),
            ("10.0.0.08", ""),  # 前导零：ipaddress 解析拒绝
            ("", ""),
        ],
    )
    def test_cleaning(self, raw, expected):
        assert node_addr._valid_candidate(raw) == expected

    # 黄金向量（上面 parametrize 的去空白版）：脚本 valid_ipv4 的比对源
    _VECTORS = [
        ("10.0.0.8", True),
        ("0.0.0.0", False),
        ("0.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("224.0.0.1", False),
        ("239.1.1.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("10.0.0.08", False),
        ("fd00::9", False),
        ("not-an-ip", False),
        ("300.1.1.1", False),
    ]

    def test_bash_valid_ipv4_matches_python_authority(self, tmp_path):
        """引导脚本 valid_ipv4 与 Python 权威实现比对：跨语言无法共享实现，用同一组
        向量固定口径——任何一侧改规则，这组比对先红。"""
        import re
        import subprocess

        template = Path(__file__).resolve().parents[2] / "scripts" / "addnode_template.sh"
        func = re.search(r"^valid_ipv4\(\).*?^\}$", template.read_text(), re.M | re.S)
        assert func, "addnode_template.sh 里找不到 valid_ipv4 函数"
        script = tmp_path / "valid_ipv4.sh"
        script.write_text(func.group(0) + '\nfor ip in "$@"; do valid_ipv4 "$ip" && echo ok || echo rej; done\n')
        result = subprocess.run(
            ["bash", str(script), *[ip for ip, _ in self._VECTORS]],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        bash_verdicts = [line == "ok" for line in result.stdout.splitlines()]
        assert bash_verdicts == [expected for _, expected in self._VECTORS]
        # Python 权威同组向量结论一致（含 bash 无法表达的空白清洗由上表覆盖）
        for ip, expected in self._VECTORS:
            assert bool(node_addr._valid_candidate(ip)) == expected, ip
