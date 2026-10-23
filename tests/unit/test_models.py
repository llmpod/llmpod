"""ORM 模型单元测试。"""

from __future__ import annotations

from llmpod.db.base import Base
from llmpod.models import (
    Cluster,
    Node,
    Gpu,
    Role,
    Permission,
    User,
    UserRole,
    SystemConfig,
    AuditLog,
    Model,
    ModelDeployment,
    Instance,
    DeploymentGpuAssignment,
    ModelEndpoint,
    ApiKey,
    Route,
    Wallet,
    BalanceTransaction,
)


class TestModelRegistration:
    """验证所有模型已正确注册到 SQLAlchemy metadata。"""

    def test_cluster_table_exists(self):
        assert "cluster" in Base.metadata.tables

    def test_node_table_exists(self):
        assert "node" in Base.metadata.tables

    def test_gpu_table_exists(self):
        assert "gpu" in Base.metadata.tables

    def test_tenant_table_exists(self):
        assert "tenant" in Base.metadata.tables

    def test_role_table_exists(self):
        assert "role" in Base.metadata.tables

    def test_permission_table_exists(self):
        assert "permission" in Base.metadata.tables

    def test_role_permission_table_exists(self):
        assert "role_permission" in Base.metadata.tables

    def test_user_table_exists(self):
        assert "user" in Base.metadata.tables

    def test_system_config_table_exists(self):
        assert "system_config" in Base.metadata.tables

    def test_audit_log_table_exists(self):
        assert "audit_log" in Base.metadata.tables

    def test_all_31_tables(self):
        """验证模型表注册完整。"""
        table_names = set(Base.metadata.tables.keys())
        expected = {
            # 基础
            "cluster",
            "node",
            "gpu",
            "tenant",
            "role",
            "permission",
            "role_permission",
            "user",
            "user_role",
            "system_config",
            "audit_log",
            # L2 资源模型（推理部署；四层：部署→副本→引擎实例→容器实例）
            "model",
            "model_deployment",
            "model_deployment_replica",
            "model_deployment_instance",
            "instance",
            "deployment_gpu_assignment",
            "model_endpoint",
            # L2 模型前置 / 统一任务底座（V1）
            "node_model",
            "task",
            "task_event",
            # 部署全流程日志
            "deploy_log",
            # API 网关配置态（ProxyServer）
            "api_key",
            "route",
            "route_binding",
            "tenant_model_block",
            # API 网关运行态（调用计量）
            "request_log",
            # 指标 5 分钟桶物化（scraper 写入）
            "metric_rollup",
            "metric_engine_rollup",
            # 钱包余额 + 流水账 + 冻结账（计费）
            "wallet",
            "balance_transaction",
            "balance_hold",
        }
        assert table_names == expected


class TestModelColumns:
    """验证模型列定义。"""

    def test_update_route_skip_list_covers_not_null_columns(self):
        """PATCH 的「传 null 就跳过」名单必须覆盖所有可更新且 NOT NULL 的列。

        漏一个的后果不是报错而是 422 内部错误：``setattr(None)`` → IntegrityError → 被
        errors.py 映射成「数据校验失败」+ PG 明细，而接口承诺的是「跳过、保持原值」。
        集合对比而非逐个断言——新增 NOT NULL 列时自动被覆盖。
        """
        from llmpod.apps.webserver.routes.admin.model import _NOT_NULL_COLUMNS, UpdateModelRequest

        settable = set(UpdateModelRequest.model_fields)
        not_null = {c.name for c in Model.__table__.columns if not c.nullable}
        assert not_null & settable <= _NOT_NULL_COLUMNS

    def test_cluster_columns(self):
        cols = {c.name for c in Cluster.__table__.columns}
        assert "id" in cols
        assert "name" in cols
        assert "type" in cols
        assert "status" in cols
        assert "spec" in cols
        assert "created_at" in cols

    def test_node_pk_is_varchar(self):
        """Node 主键应为 VARCHAR(64)，而非自增 INT。"""
        id_col = Node.__table__.columns["id"]
        assert id_col.type.python_type is str

    def test_node_has_cluster_id(self):
        cols = {c.name for c in Node.__table__.columns}
        assert "cluster_id" in cols

    def test_gpu_has_unique_uuid(self):
        """GPU 应有唯一的 uuid 列。"""
        uuid_col = Gpu.__table__.columns["uuid"]
        assert uuid_col.unique is True

    def test_gpu_has_node_id(self):
        cols = {c.name for c in Gpu.__table__.columns}
        assert "node_id" in cols
        assert "total_vram_gb" in cols

    def test_user_has_password_hash(self):
        cols = {c.name for c in User.__table__.columns}
        assert "password_hash" in cols
        assert "tenant_id" in cols
        # user 表不再持有 role_id，角色通过 user_role 关联表承载（见下）
        assert "role_id" not in cols

    def test_user_role_relation_table(self):
        """user_role 关联表应含 user_id 与 role_id。"""
        cols = {c.name for c in UserRole.__table__.columns}
        assert "user_id" in cols
        assert "role_id" in cols
        assert "user_role" in Base.metadata.tables

    def test_role_has_code(self):
        cols = {c.name for c in Role.__table__.columns}
        assert "code" in cols
        assert "is_builtin" in cols

    def test_permission_fields(self):
        cols = {c.name for c in Permission.__table__.columns}
        assert "scope" in cols
        assert "resource" in cols
        assert "action" in cols
        assert "description" in cols
        # 权限为 scope:resource:action 三元组（平台/租户域维度）

    def test_system_config_pk_is_key(self):
        """SystemConfig 主键应为 'key'（VARCHAR），而非 INT。"""
        key_col = SystemConfig.__table__.columns["key"]
        assert key_col.primary_key is True
        assert key_col.type.python_type is str

    def test_audit_log_fields(self):
        cols = {c.name for c in AuditLog.__table__.columns}
        assert "actor_type" in cols
        assert "actor_id" in cols
        assert "action" in cols
        assert "resource_type" in cols
        assert "resource_id" in cols
        assert "detail" in cols


class TestL2ResourceModels:
    """推理部署 L2 资源模型：PK 策略 + spec JSONB + 关键列。"""

    def test_model_deployment_string_pk(self):
        """ModelDeployment 主键 String(64)（非自增 int）。"""
        pk = ModelDeployment.__table__.columns["deployment_id"]
        assert pk.primary_key is True
        assert pk.type.python_type is str

    def test_model_deployment_spec_jsonb(self):
        cols = {c.name for c in ModelDeployment.__table__.columns}
        assert "spec" in cols
        assert "engine" in cols
        assert "status" in cols
        assert "coordinate_mode" not in cols  # coordinate_mode 进 spec jsonb，非核心列

    def test_instance_string_pk(self):
        pk = Instance.__table__.columns["instance_id"]
        assert pk.primary_key is True
        assert pk.type.python_type is str

    def test_instance_lifecycle_fields(self):
        """Instance 长期状态字段：rank（task resource_key 用）+ 容器回填 + 状态时间戳。"""
        cols = {c.name for c in Instance.__table__.columns}
        assert "rank" in cols
        assert "container_id" in cols
        assert "endpoint" in cols
        assert "started_at" in cols and "ready_at" in cols and "stopped_at" in cols
        assert "assigned_node" in cols

    def test_model_weight_fields(self):
        """Model 合并权重字段 + 下载配方：size_gb/format/quantization/hash + source_kind/source_subpath/hash_mode。"""
        cols = {c.name for c in Model.__table__.columns}
        assert "cached_nodes" not in cols  # 分发状态已迁 node_model 表
        assert "source_kind" in cols
        assert "source_subpath" in cols
        assert "hash_mode" in cols
        assert "source_auth_encrypted" in cols
        assert "hash" in cols
        assert "size_gb" in cols
        assert "quantization" in cols
        assert "format" in cols

    def test_deployment_gpu_assignment_unique(self):
        """(deployment_id, gpu_uuid) 唯一约束（权威来源）。"""
        uqs = {
            tuple(c.name for c in uc.columns)
            for uc in DeploymentGpuAssignment.__table__.constraints
            if getattr(uc, "name", "") == "uq_deployment_gpu"
        }
        assert ("deployment_id", "gpu_uuid") in uqs or ("gpu_uuid", "deployment_id") in uqs

    def test_model_endpoint_served_model(self):
        """ModelEndpoint 带 served_model（client 请求的 model 字段）。"""
        cols = {c.name for c in ModelEndpoint.__table__.columns}
        assert "served_model" in cols
        assert "api_base" in cols
        assert "health_status" in cols


class TestBillingModels:
    """钱包余额 + 流水账 + 预算/计费列。"""

    def test_wallet_columns(self):
        cols = {c.name for c in Wallet.__table__.columns}
        assert "tenant_id" in cols
        assert "balance" in cols

    def test_wallet_tenant_unique(self):
        idx = {ix.name: ix for ix in Wallet.__table__.indexes}
        assert "uq_wallet_tenant_id" in idx
        assert idx["uq_wallet_tenant_id"].unique is True
        assert tuple(c.name for c in idx["uq_wallet_tenant_id"].columns) == ("tenant_id",)

    def test_balance_transaction_columns(self):
        cols = {c.name for c in BalanceTransaction.__table__.columns}
        assert "tenant_id" in cols
        assert "type" in cols
        assert "amount" in cols
        assert "balance_after" in cols
        assert "meta" in cols

    def test_api_key_budget_columns(self):
        cols = {c.name for c in ApiKey.__table__.columns}
        assert "budget_limit" in cols
        assert "budget_spent" in cols

    def test_route_billing_price_columns(self):
        """定价三列（input/output/cached）；embeddings/rerank 复用 input 价。"""
        cols = {c.name for c in Route.__table__.columns}
        assert "input_price_per_mtok" in cols
        assert "output_price_per_mtok" in cols
        assert "cached_input_price_per_mtok" in cols
        # 单模型定价合并后不再有独立列
        assert "embeddings_price_per_mtok" not in cols
        assert "rerank_price_per_mtok" not in cols
