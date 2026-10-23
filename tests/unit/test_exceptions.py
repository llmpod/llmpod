"""核心异常单元测试。"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import (
    LLMPodError,
    ConfigError,
    AuthError,
    PermissionDenied,
    NotFound,
    Conflict,
    LLMPodValidationError,
)
from llmpod.apps.node.exceptions import (
    NodeError,
    NodeConnectionError,
    TaskExecutionError,
)
from llmpod.tunnel.entity.errors import RemoteError, NodeOffline


class TestExceptions:
    def test_llmpod_error_base(self):
        with pytest.raises(LLMPodError):
            raise LLMPodError("test")

    def test_config_error_inherits(self):
        with pytest.raises(LLMPodError):
            raise ConfigError("bad config")

    def test_auth_error(self):
        err = AuthError("invalid token")
        assert str(err) == "invalid token"

    def test_permission_denied(self):
        err = PermissionDenied("no access", resource="cluster", action="delete")
        assert err.resource == "cluster"
        assert err.action == "delete"

    def test_not_found(self):
        err = NotFound("Cluster", 42)
        assert "集群" in str(err)
        assert "42" in str(err)

    def test_conflict(self):
        with pytest.raises(Conflict):
            raise Conflict("duplicate")

    def test_validation_error(self):
        with pytest.raises(LLMPodValidationError):
            raise LLMPodValidationError("invalid input")


class TestNodeExceptions:
    """Node 专属异常：继承 LLMPodError（基类）+ NodeError（node 基类）。"""

    def test_node_error_is_llmpod_error(self):
        with pytest.raises(LLMPodError):
            raise NodeError("node failure")

    def test_node_connection_error(self):
        with pytest.raises(NodeError):
            raise NodeConnectionError("offline")

    def test_task_execution_error(self):
        with pytest.raises(NodeError):
            raise TaskExecutionError("docker failed")


class TestRemoteError:
    def test_with_traceback(self):
        err = RemoteError("failed", tb="line 42: error", code=-32000)
        assert err.message == "failed"
        assert err.tb == "line 42: error"
        assert err.code == -32000

    def test_default_code(self):
        err = RemoteError("test")
        assert err.code == -32000


class TestNodeOffline:
    def test_basic(self):
        with pytest.raises(NodeOffline):
            raise NodeOffline("node not connected")
