"""WebServer 表现层 schemas：响应封装、分页、请求参数、ORM 衍生 DTO。"""

from llmpod.apps.webserver.schemas.response import ResultBase, Result, PageData, PaginatedResponse, success, error
from llmpod.apps.webserver.schemas.request import (
    PaginationParams,
)
from llmpod.apps.webserver.schemas.out_models import (
    orm_schema,
    UserOut,
    TenantOut,
    GpuOut,
    ClusterOut,
    NodeOut,
    SystemConfigOut,
    PermissionOut,
    RoleOut,
    UserDetailOut,
    RoleDetailOut,
    NodeDetailOut,
    GpuDetailOut,
    ClusterDetailOut,
    TenantDetailOut,
)

__all__ = [
    "ResultBase",
    "Result",
    "PageData",
    "PaginatedResponse",
    "success",
    "error",
    "PaginationParams",
    "orm_schema",
    "UserOut",
    "TenantOut",
    "GpuOut",
    "ClusterOut",
    "NodeOut",
    "SystemConfigOut",
    "PermissionOut",
    "RoleOut",
    "UserDetailOut",
    "RoleDetailOut",
    "NodeDetailOut",
    "GpuDetailOut",
    "ClusterDetailOut",
    "TenantDetailOut",
]
