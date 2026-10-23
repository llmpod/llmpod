"""rank 解析 + 多机同子网校验单测。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.scheduler.rank import validate_same_subnet
from llmpod.core.exceptions import LLMPodValidationError


def test_same_subnet_passes():
    """同 /24 子网不抛（nvidia+ascend 双机场景）。"""
    validate_same_subnet({"n1": "192.0.2.29", "n2": "192.0.2.26"})


def test_cross_subnet_rejects():
    """跨子网（192.168.x vs 10.x）→ 拒绝。"""
    with pytest.raises(LLMPodValidationError, match="同一 /24 子网"):
        validate_same_subnet({"n1": "192.0.2.29", "n2": "10.0.0.5"})


def test_single_node_passes():
    validate_same_subnet({"n1": "192.0.2.29"})


def test_skips_empty_ip():
    """空 IP（拿不到）跳过，不当作跨子网证据。"""
    validate_same_subnet({"n1": "192.0.2.29", "n2": ""})


def test_skips_invalid_ip():
    validate_same_subnet({"n1": "192.0.2.29", "n2": "not-an-ip"})


def test_loopback_alone_passes_but_mixed_rejects():
    # 仅 loopback → 单一子网，不抛（但上游 rendezvous 会用 127.0.0.1 兜底暴露问题）
    validate_same_subnet({"n1": "127.0.0.1"})
    # loopback 与 LAN 混 → 跨子网拒绝
    with pytest.raises(LLMPodValidationError):
        validate_same_subnet({"n1": "127.0.0.1", "n2": "192.0.2.26"})


def test_custom_prefix():
    # /16 下 192.168.1.x 与 192.168.2.x 同网段
    validate_same_subnet({"n1": "192.168.1.10", "n2": "192.168.2.20"}, prefix=16)
    # /24 下则跨子网
    with pytest.raises(LLMPodValidationError):
        validate_same_subnet({"n1": "192.168.1.10", "n2": "192.168.2.20"}, prefix=24)


def test_subnet_warning_cross_subnet():
    from llmpod.apps.manager.deployment.scheduler.rank import subnet_warning

    w = subnet_warning({"n1": "192.0.2.29", "n2": "10.0.0.5"})
    assert w and "子网" in w


def test_subnet_warning_same_subnet_none():
    from llmpod.apps.manager.deployment.scheduler.rank import subnet_warning

    assert subnet_warning({"n1": "192.0.2.29", "n2": "192.0.2.26"}) is None


def test_deployment_config_force_field():
    from llmpod.apps.manager.deployment.schemas import DeploymentConfig

    assert DeploymentConfig().force is False
    assert DeploymentConfig(force=True).force is True
    # force 会 model_dump 进 spec（_plan 据此跳过启发式校验）
    assert "force" in DeploymentConfig(force=True).model_dump()
