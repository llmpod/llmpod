"""DeploymentConfig.publish（PublishConfig）单测：发布配置校验 + spec round-trip。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import DeploymentConfig
from llmpod.apps.manager.deployment.schemas.config import PublishConfig


class TestPublishConfig:
    def test_defaults(self):
        p = PublishConfig()
        assert p.auto_publish is True
        assert p.name is None
        assert p.model_type is None
        assert p.description is None
        assert p.tags == []
        assert p.input_price_per_mtok is None
        assert p.output_price_per_mtok is None
        assert p.cached_input_price_per_mtok is None

    def test_name_stripped(self):
        p = PublishConfig(name="  my-alias  ")
        assert p.name == "my-alias"

    def test_name_blank_rejected(self):
        with pytest.raises(Exception):
            PublishConfig(name="   ")

    def test_name_too_long_rejected(self):
        with pytest.raises(Exception):
            PublishConfig(name="x" * 256)

    def test_tags_valid(self):
        p = PublishConfig(tags=["思考", "工具调用"])
        assert p.tags == ["思考", "工具调用"]

    def test_tags_unknown_rejected(self):
        with pytest.raises(Exception):
            PublishConfig(tags=["不存在的标签"])

    def test_pricing_non_negative_rejected(self):
        with pytest.raises(Exception):
            PublishConfig(input_price_per_mtok=-1)


class TestDeploymentConfigPublish:
    def test_publish_default_auto(self):
        cfg = DeploymentConfig()
        assert cfg.publish.auto_publish is True
        assert cfg.publish.name is None

    def test_publish_round_trip(self):
        cfg = DeploymentConfig(
            publish=PublishConfig(
                auto_publish=False,
                name="my-gpt",
                tags=["视觉"],
                input_price_per_mtok=1.5,
            )
        )
        dumped = cfg.model_dump()
        assert dumped["publish"]["name"] == "my-gpt"
        assert dumped["publish"]["auto_publish"] is False
        restored = DeploymentConfig.model_validate(dumped)
        assert restored.publish.name == "my-gpt"
        assert restored.publish.auto_publish is False
        assert restored.publish.input_price_per_mtok == 1.5

    def test_legacy_spec_without_publish_defaults(self):
        # 存量 spec 无 publish 键 → 默认自动发布（向后兼容）
        cfg = DeploymentConfig.model_validate({"topology": {"tp": 2}})
        assert cfg.publish.auto_publish is True
        assert cfg.publish.name is None


class TestCpuLaneConfig:
    """allow_cpu / offload_preference / kv_cache（CPU 部署的 spec 字段）。"""

    def test_defaults(self):
        cfg = DeploymentConfig()
        assert cfg.allow_cpu is False
        assert cfg.offload_preference == "auto"
        assert cfg.kv_cache.n_parallel is None

    def test_round_trip(self):
        cfg = DeploymentConfig.model_validate(
            {
                "topology": {"tp": 1},
                "allow_cpu": True,
                "offload_preference": "cpu_only",
                "kv_cache": {"n_parallel": 2},
            }
        )
        dumped = cfg.model_dump()
        restored = DeploymentConfig.model_validate(dumped)
        assert restored.allow_cpu is True
        assert restored.offload_preference == "cpu_only"
        assert restored.kv_cache.n_parallel == 2

    def test_invalid_preference_rejected(self):
        with pytest.raises(Exception):
            DeploymentConfig(offload_preference="bogus")

    def test_kv_placement_fields_rejected(self):
        """KV 落点不可配：旧 spec 里的 mode/ram_size/ram_ratio 会被拒（extra=forbid）。"""
        with pytest.raises(Exception):
            DeploymentConfig(kv_cache={"mode": "cpu"})
        with pytest.raises(Exception):
            DeploymentConfig(kv_cache={"ram_size": 8})
