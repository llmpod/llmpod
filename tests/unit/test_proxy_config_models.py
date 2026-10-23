"""API 网关配置态 ORM 守卫。

锁定表结构关键事实：ApiKey 只存 SHA-256 哈希（唯一）、Route 唯一名 + per-token 定价 +
failover、RouteBinding (route, endpoint) 唯一带权重/优先级、ModelEndpoint (deployment,
protocol) 唯一、TenantModelBlock (tenant, route) 唯一。schema 归管理端 init_db.py，
此处只断言 metadata 注册正确。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint

from llmpod.db.base import Base
from llmpod.models import ApiKey, ModelEndpoint, Route, RouteBinding, TenantModelBlock


def _table(name: str):
    assert name in Base.metadata.tables, f"表 {name} 未注册进 Base.metadata"
    return Base.metadata.tables[name]


def _unique_cols(t) -> set:
    uniq = {tuple(c.name for c in idx.columns) for idx in t.indexes if idx.unique}
    uniq |= {tuple(c.name for c in idx.columns) for idx in t.constraints if getattr(idx, "unique", False)}
    return uniq


class TestApiKeyTable:
    def test_columns(self):
        t = _table("api_key")
        expected = {
            "id",
            "key_hash",
            "tenant_id",
            "name",
            "allowed_models",
            "rpm_limit",
            "tpm_limit",
            "total_token_quota",
            "model_token_quotas",
            "status",
            "expires_at",
            "created_at",
            "updated_at",
        }
        assert expected <= set(t.c.keys())

    def test_key_hash_unique(self):
        assert ("key_hash",) in _unique_cols(_table("api_key"))

    def test_status_check(self):
        t = _table("api_key")
        checks = [c for c in t.constraints if isinstance(c, CheckConstraint)]
        assert any("status" in str(c.sqltext) for c in checks)


class TestRouteTable:
    def test_columns(self):
        t = _table("route")
        expected = {
            "id",
            "name",
            "status",
            "input_price_per_mtok",
            "output_price_per_mtok",
            "cached_input_price_per_mtok",
            "failover_enabled",
            "failover_max_retries",
            "created_at",
            "updated_at",
        }
        assert expected <= set(t.c.keys())

    def test_name_unique(self):
        assert ("name",) in _unique_cols(_table("route"))


class TestRouteBindingTable:
    def test_columns(self):
        t = _table("route_binding")
        expected = {"id", "route_id", "endpoint_id", "weight", "priority", "enabled", "created_at", "updated_at"}
        assert expected <= set(t.c.keys())

    def test_route_endpoint_unique(self):
        assert ("route_id", "endpoint_id") in _unique_cols(_table("route_binding"))


class TestModelEndpointTable:
    def test_columns(self):
        t = _table("model_endpoint")
        expected = {
            "id",
            "deployment_id",
            "protocol",
            "api_base",
            "path",
            "served_model",
            "api_key",
            "name",
            "provider_name",
            "enabled",
            "health_status",
            "last_synced_at",
            "created_at",
            "updated_at",
        }
        assert expected <= set(t.c.keys())

    def test_deployment_protocol_unique(self):
        # 四层化后：本地端点一副本一协议一行（replica 维唯一，多副本各一行）
        assert ("deployment_id", "model_deployment_replica_id", "protocol") in _unique_cols(_table("model_endpoint"))


class TestTenantModelBlockTable:
    def test_columns(self):
        t = _table("tenant_model_block")
        expected = {"id", "tenant_id", "route_id", "status", "created_at", "updated_at"}
        assert expected <= set(t.c.keys())

    def test_tenant_route_unique(self):
        assert ("tenant_id", "route_id") in _unique_cols(_table("tenant_model_block"))


def test_models_exported():
    """模型从 llmpod.models 包导出（init_db create_all 依赖包导入）。"""
    from llmpod import models

    for cls in (ApiKey, Route, RouteBinding, ModelEndpoint, TenantModelBlock):
        assert cls.__tablename__ in Base.metadata.tables
        assert getattr(models, cls.__name__) is cls
