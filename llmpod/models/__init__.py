"""LLMPod ORM 模型包。

导入所有模型，以便 ``Base.metadata.create_all`` 和关系解析
能够通过 ``Base.metadata`` 发现所有表。
"""

from llmpod.db.base import Base
from llmpod.models.cluster import Cluster
from llmpod.models.node import Node
from llmpod.models.gpu import Gpu
from llmpod.models.tenant import Tenant
from llmpod.models.role import Role
from llmpod.models.permission import Permission
from llmpod.models.role_permission import RolePermission
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.models.system_config import SystemConfig
from llmpod.models.audit_log import AuditLog

# 推理部署资源模型
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.instance import Instance
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.node_model import NodeModel
from llmpod.models.task import Task
from llmpod.models.task_event import TaskEvent
from llmpod.models.deploy_log import DeployLog

# API 网关配置态（ProxyServer）
from llmpod.models.api_key import ApiKey
from llmpod.models.route import Route
from llmpod.models.route_binding import RouteBinding
from llmpod.models.tenant_model_block import TenantModelBlock

# API 网关运行态（调用计量）
from llmpod.models.request_log import RequestLog

# 指标 5 分钟桶物化（scraper 写入）
from llmpod.models.metric_rollup import MetricRollup
from llmpod.models.metric_engine_rollup import MetricEngineRollup

# 钱包余额 + 流水账（计费）
from llmpod.models.wallet import Wallet
from llmpod.models.balance_transaction import BalanceTransaction
from llmpod.models.balance_hold import BalanceHold

__all__ = [
    "Base",
    "Cluster",
    "Node",
    "Gpu",
    "Tenant",
    "Role",
    "Permission",
    "RolePermission",
    "User",
    "UserRole",
    "SystemConfig",
    "AuditLog",
    # 推理部署资源模型
    "Model",
    "ModelDeployment",
    "ModelDeploymentReplica",
    "ModelDeploymentInstance",
    "Instance",
    "DeploymentGpuAssignment",
    "ModelEndpoint",
    "NodeModel",
    "Task",
    "TaskEvent",
    "DeployLog",
    # API 网关配置态
    "ApiKey",
    "Route",
    "RouteBinding",
    "TenantModelBlock",
    # API 网关运行态
    "RequestLog",
    # 指标 5 分钟桶物化
    "MetricRollup",
    "MetricEngineRollup",
    # 钱包余额 + 流水账（计费）
    "Wallet",
    "BalanceTransaction",
    "BalanceHold",
]
