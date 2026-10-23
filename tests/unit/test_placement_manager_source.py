"""PlacementService local 源注入单测：_build_payload 加 manager_base_url；_is_manager_source 判定。"""

from __future__ import annotations

from llmpod.apps.manager.deployment import placement as pl
from llmpod.apps.manager.deployment.placement import PlacementService
from llmpod.models.model import Model


def _model(source: str) -> Model:
    m = Model(name="x", model_type="llm", source=source)
    m.id = 7
    m.source_kind = "transformers_folder"
    m.hash_mode = "none"
    return m


def test_is_manager_source():
    assert pl._is_manager_source(_model("local")) is True
    assert pl._is_manager_source(_model("huggingface")) is False
    assert pl._is_manager_source(_model(None)) is False


def test_build_payload_local_has_manager_base_url(monkeypatch):
    from llmpod.config.settings import settings

    monkeypatch.setattr(settings, "node_manager_ip", "wss://manager.example")
    p = PlacementService._build_payload(_model("local"), "/data/models/model-7")
    assert p.manager_base_url is not None
    assert p.manager_base_url.endswith("/api/v1/distribution")
    assert p.model_id == 7
    assert p.source == "local"


def test_build_payload_huggingface_has_no_manager_base_url():
    p = PlacementService._build_payload(_model("huggingface"), "/data/models/model-7")
    assert p.manager_base_url is None
    assert p.source == "huggingface"


def test_distribution_base_url_ignores_node_controlled_host(monkeypatch):
    """节点可控的 Host 头不能改写模型文件下载目的地。"""
    import llmpod.apps.manager.app as manager_app

    class _Node:
        advertise_origin = "http://192.0.2.29:8080"

    class _Hub:
        def get(self, node_id):
            return _Node()

    monkeypatch.setattr(manager_app, "node_hub", _Hub())
    from llmpod.config.settings import settings

    monkeypatch.setattr(settings, "node_manager_ip", "wss://manager.example")
    url = pl._distribution_base_url()
    assert url == "https://manager.example/api/v1/distribution"


def test_distribution_base_url_falls_back_to_settings(monkeypatch):
    """节点未连（hub 查不到）→ 仍取 manager 对外配置地址（按 IP 拼 http origin）。"""
    import llmpod.apps.manager.app as manager_app

    class _Node:
        advertise_origin = None

    class _Hub:
        def get(self, node_id):
            return _Node()

    monkeypatch.setattr(manager_app, "node_hub", _Hub())
    from llmpod.config.settings import settings

    monkeypatch.setattr(settings, "node_manager_ip", "wss://manager.example")
    monkeypatch.setattr(settings, "webserver_port", 8080)
    url = pl._distribution_base_url()
    assert url == "https://manager.example/api/v1/distribution"
