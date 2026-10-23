"""工具函数单元测试。"""

from __future__ import annotations

from llmpod.utils.crypto import generate_token, generate_uuid
from llmpod.utils.time import now_utc, format_datetime
from llmpod.utils.docker_naming import container_name, deployment_container_name


class TestCrypto:
    def test_generate_token_length(self):
        token = generate_token()
        assert len(token) > 30  # URL 安全的 base64 编码

    def test_generate_token_unique(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_generate_uuid_format(self):
        uid = generate_uuid()
        parts = uid.split("-")
        assert len(parts) == 5
        assert len(uid) == 36  # 标准 UUID 格式


class TestTime:
    def test_now_utc_is_timezone_aware(self):
        dt = now_utc()
        assert dt.tzinfo is not None

    def test_format_datetime_none(self):
        assert format_datetime(None) is None

    def test_format_datetime_value(self):
        dt = now_utc()
        result = format_datetime(dt)
        assert isinstance(result, str)
        assert "T" in result  # ISO 格式


class TestDockerNaming:
    def test_container_name_format(self):
        name = container_name(1, "abcdef12-3456-7890", "engine")
        assert name == "llmpod-1-abcdef12-engine"

    def test_deployment_container_name_format(self):
        """部署容器名：llmpod-{dep[:8]}-{instance_id}（deployment_id 取前 8 字符）。"""
        name = deployment_container_name("abcdef12-3456-7890-abcd", "inst-001")
        assert name == "llmpod-abcdef12-inst-001"

    def test_short_node_id(self):
        name = container_name(1, "abc", "test")
        assert name == "llmpod-1-abc-test"
