"""core.advertise 对外地址推导：合法形态逐条锁定，非法形态必须当场抛 ConfigError。"""

import pytest

from llmpod.config.settings import settings
from llmpod.core.advertise import manager_http_origin, manager_ws_candidates, manager_ws_url
from llmpod.core.exceptions import ConfigError


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    """锁定推导依赖的三项配置，免得本机 .env 改了值就飘。"""
    monkeypatch.setattr(settings, "node_manager_ip", "")
    monkeypatch.setattr(settings, "webserver_port", 8080)
    monkeypatch.setattr(settings, "manager_ws_path", "/ws/node")


def test_remote_plaintext_address_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "node_manager_ip", "manager.example")
    assert manager_ws_url() == "ws://manager.example:8080/ws/node"
    assert manager_http_origin() == "http://manager.example:8080"


def test_tls_primary_does_not_generate_plaintext_lan_candidate(monkeypatch):
    monkeypatch.setattr(settings, "node_manager_ip", "wss://manager.example")
    monkeypatch.setattr(settings, "node_manager_ip_lan", "10.0.0.5")
    assert all(url.startswith("wss://") for url in manager_ws_candidates())


class TestBareHost:
    def test_empty_falls_back_to_localhost(self):
        """留空 = localhost + webserver_port，同机开发够用。"""
        assert manager_ws_url() == "ws://localhost:8080/ws/node"
        assert manager_http_origin() == "http://localhost:8080"

    def test_ip_gets_port_appended(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "192.0.2.29")
        assert manager_ws_url() == "ws://192.0.2.29:8080/ws/node"
        assert manager_http_origin() == "http://192.0.2.29:8080"

    def test_domain_gets_port_appended(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "mgr.example.com")
        assert manager_http_origin() == "http://mgr.example.com:8080"

    def test_bracketed_ipv6_gets_port_appended(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "[2001:db8::1]")
        assert manager_http_origin() == "http://[2001:db8::1]:8080"

    def test_port_follows_webserver_port(self, monkeypatch):
        """docker 部署改了对外端口，拼出的地址跟着走。"""
        monkeypatch.setattr(settings, "node_manager_ip", "10.0.0.5")
        monkeypatch.setattr(settings, "webserver_port", 18080)
        assert manager_ws_url() == "ws://10.0.0.5:18080/ws/node"


class TestFullUrl:
    def test_https_maps_to_wss_and_keeps_no_port(self, monkeypatch):
        """反代 + TLS：http 侧保 https，ws 侧映射 wss，端口不补（由反代决定）。"""
        monkeypatch.setattr(settings, "node_manager_ip", "https://mgr.example.com")
        assert manager_ws_url() == "wss://mgr.example.com/ws/node"
        assert manager_http_origin() == "https://mgr.example.com"

    def test_wss_maps_to_https(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "wss://mgr.example.com")
        assert manager_http_origin() == "https://mgr.example.com"

    def test_explicit_port_is_kept(self, monkeypatch):
        """完整 URL 自带端口 = 反代监听非默认口，原样保留、不再补。"""
        monkeypatch.setattr(settings, "node_manager_ip", "https://mgr.example.com:8443")
        assert manager_ws_url() == "wss://mgr.example.com:8443/ws/node"

    def test_trailing_slash_is_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "https://mgr.example.com/")
        assert manager_http_origin() == "https://mgr.example.com"


class TestInvalidValues:
    """误填形态全部在生成侧拦截，而不是拼出废地址发到目标机。"""

    @pytest.mark.parametrize("value", ["192.0.2.29:18080", "mgr.example.com:8080"])
    def test_bare_host_with_port(self, monkeypatch, value):
        """旧配置的 host:port 整个抄进来 → 双端口废地址，必须拦。"""
        monkeypatch.setattr(settings, "node_manager_ip", value)
        with pytest.raises(ConfigError, match="不要带端口"):
            manager_ws_url()

    @pytest.mark.parametrize("value", ["0.0.0.0", "::"])
    def test_unroutable_wildcard(self, monkeypatch, value):
        """监听通配地址照抄进来 → 节点拨自己，必须拦。"""
        monkeypatch.setattr(settings, "node_manager_ip", value)
        with pytest.raises(ConfigError, match="不是对外地址"):
            manager_http_origin()

    def test_bare_ipv6_without_brackets(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "2001:db8::1")
        with pytest.raises(ConfigError, match="加方括号"):
            manager_ws_url()

    def test_trailing_slash_on_bare_host(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "192.168.1.20/")
        with pytest.raises(ConfigError, match="不要带路径或斜杠"):
            manager_ws_url()

    def test_unknown_scheme(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "ftp://mgr.example.com")
        with pytest.raises(ConfigError, match="scheme 只认"):
            manager_ws_url()

    def test_url_with_path_prefix(self, monkeypatch):
        """反代挂在子路径下：路径会被静默丢弃，宁可在配置侧报错。"""
        monkeypatch.setattr(settings, "node_manager_ip", "https://mgr.example.com/llmpod")
        with pytest.raises(ConfigError, match="不支持路径前缀"):
            manager_http_origin()

    def test_url_with_unroutable_host(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "https://0.0.0.0")
        with pytest.raises(ConfigError, match="不是对外地址"):
            manager_http_origin()

    def test_url_without_host(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "https://")
        with pytest.raises(ConfigError, match="缺主机部分"):
            manager_http_origin()


class TestWsCandidates:
    """候选清单：内网直连优先、主地址兜底压轴、去重与形态校验。"""

    @pytest.fixture(autouse=True)
    def _pin_lan(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip_lan", "")

    def test_lan_empty_is_primary_only(self, monkeypatch):
        """LAN 未配 = 清单只剩主地址，行为与单地址时代一致。"""
        monkeypatch.setattr(settings, "node_manager_ip", "43.129.1.2")
        assert manager_ws_candidates() == ["ws://43.129.1.2:8080/ws/node"]

    def test_lan_hosts_lead_primary_last(self, monkeypatch):
        """内网候选在前、主地址兜底压轴——引导脚本按序探活，先内网后兜底。"""
        monkeypatch.setattr(settings, "node_manager_ip", "43.129.1.2")
        monkeypatch.setattr(settings, "node_manager_ip_lan", "192.168.1.10, 10.0.0.5")
        assert manager_ws_candidates() == [
            "ws://192.168.1.10:8080/ws/node",
            "ws://10.0.0.5:8080/ws/node",
            "ws://43.129.1.2:8080/ws/node",
        ]

    def test_duplicate_with_primary_deduped(self, monkeypatch):
        """LAN 与主地址重合只留一份：探活清单里出现两遍同一地址没有意义。"""
        monkeypatch.setattr(settings, "node_manager_ip", "192.168.1.10")
        monkeypatch.setattr(settings, "node_manager_ip_lan", "192.168.1.10")
        assert manager_ws_candidates() == ["ws://192.168.1.10:8080/ws/node"]

    def test_entries_share_primary_validation(self, monkeypatch):
        """候选形态非法同样当场抛：废地址发给节点只会变成无限重连。"""
        monkeypatch.setattr(settings, "node_manager_ip", "43.129.1.2")
        monkeypatch.setattr(settings, "node_manager_ip_lan", "192.168.1.10:9090")
        with pytest.raises(ConfigError, match="不要带端口"):
            manager_ws_candidates()

    def test_all_empty_falls_back_to_localhost(self, monkeypatch):
        monkeypatch.setattr(settings, "node_manager_ip", "")
        assert manager_ws_candidates() == ["ws://localhost:8080/ws/node"]


class TestBareHostCharset:
    """杂字符主机名（粘贴带括号/引号等）必须启动期拦截，不进候选/端点。"""

    @pytest.mark.parametrize("value", ["118.126.100.27}", "192.168.1.5]", "mgr.example.com'"])
    def test_stray_punctuation_rejected(self, monkeypatch, value):
        monkeypatch.setattr(settings, "node_manager_ip", value)
        with pytest.raises(ConfigError, match="字母/数字/点/连字符"):
            manager_ws_url()

    def test_lan_entry_with_junk_rejected(self, monkeypatch):
        """候选清单逐条同校验：脏条目让 bootstrap 渲染整场失败，宁可当场报。"""
        monkeypatch.setattr(settings, "node_manager_ip", "43.129.1.2")
        monkeypatch.setattr(settings, "node_manager_ip_lan", "192.168.1.10}, 10.0.0.5")
        with pytest.raises(ConfigError, match="字母/数字/点/连字符"):
            manager_ws_candidates()
