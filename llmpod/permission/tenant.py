"""租户端（租户域，scope=tenant）权限常量声明。"""

from __future__ import annotations

from llmpod.permission._common import SCOPE_TENANT, _r
from llmpod.permission.menu import _menu
from llmpod.permission.registry import PermSpec

# ===== 跨域拆分：user / tenant 资源的租户侧语义（平台侧见 platform 模块）=====
TENANT_USER_READ = _r(SCOPE_TENANT, "user", "read", "查看本租户用户")
TENANT_USER_CREATE = _r(SCOPE_TENANT, "user", "create", "创建本租户用户")
TENANT_USER_UPDATE = _r(SCOPE_TENANT, "user", "update", "更新本租户用户")
TENANT_USER_DELETE = _r(SCOPE_TENANT, "user", "delete", "删除本租户用户")

TENANT_TENANT_READ = _r(SCOPE_TENANT, "tenant", "read", "查看本租户概况")

# ===== 本租户角色与授权管理（租户端 /tenant/access/*）=====
TENANT_ACCESS_READ = _r(SCOPE_TENANT, "access", "read", "查看本租户角色与权限")
TENANT_ACCESS_UPDATE = _r(SCOPE_TENANT, "access", "update", "管理本租户角色与授权")

# ===== 本租户 API 网关资源（租户端自助管理：Key / 日志 / 账单 / 可用模型）=====
# 与平台侧同名资源（platform 模块的 api_key / request_log / wallet）分属两个 scope，
# 权限互不相通：租户侧只能看见/操作自己租户作用域内的数据。
TENANT_API_KEY_READ = _r(SCOPE_TENANT, "api_key", "read", "查看本租户 API Key")
TENANT_API_KEY_CREATE = _r(SCOPE_TENANT, "api_key", "create", "签发本租户 API Key")
TENANT_API_KEY_UPDATE = _r(SCOPE_TENANT, "api_key", "update", "禁用/启用/删除/调整本租户 API Key")
TENANT_REQUEST_LOG_READ = _r(SCOPE_TENANT, "request_log", "read", "查看本租户调用日志")
TENANT_AUDIT_LOG_READ = _r(SCOPE_TENANT, "audit_log", "read", "查看本租户审计日志")
TENANT_WALLET_READ = _r(SCOPE_TENANT, "wallet", "read", "查看本租户余额与流水")
TENANT_MODEL_READ = _r(SCOPE_TENANT, "model", "read", "查看本租户可用模型（已发布且未被平台封禁）")
TENANT_DASHBOARD_READ = _r(SCOPE_TENANT, "dashboard", "read", "查看本租户数据看板（调用统计/趋势/消费汇总）")

# ===== 租户端菜单（scope=tenant）=====
# 声明只进菜单注册表（非权限）；required = 可见性判定（OR：持任一即可见；
# 空 = 租户域全员可见）。登录页 /tenant/login 无需登录、非菜单，不声明。
_menu(SCOPE_TENANT, "/tenant/console", "控制台", (TENANT_TENANT_READ,))
_menu(SCOPE_TENANT, "/tenant/model-mark", "模型市场", (TENANT_MODEL_READ,))
_menu(SCOPE_TENANT, "/tenant/playground", "在线调试")
_menu(SCOPE_TENANT, "/tenant/api-key", "API 密钥", (TENANT_API_KEY_READ,))
_menu(SCOPE_TENANT, "/tenant/statistics", "调用统计", (TENANT_DASHBOARD_READ,))
_menu(SCOPE_TENANT, "/tenant/cost", "费用中心", (TENANT_WALLET_READ,))
_menu(SCOPE_TENANT, "/tenant/team", "团队管理", (TENANT_USER_READ,))
_menu(SCOPE_TENANT, "/tenant/logs", "审计日志", (TENANT_AUDIT_LOG_READ,))
_menu(SCOPE_TENANT, "/tenant/auth", "角色权限", (TENANT_ACCESS_READ,))
_menu(SCOPE_TENANT, "/tenant/config", "个人设置")
_menu(SCOPE_TENANT, "/tenant/about", "关于")

# ===== 租户域内置角色 → 权限映射 =====
# tenant_admin（租户管理员）：本租户全量操作权限，可经 access 接口管理本租户角色。
# tenant_operator（租户运营）：本租户只读为主的基础权限。
ROLE_PERMISSIONS: dict[str, list[PermSpec]] = {
    "tenant_admin": [
        TENANT_USER_READ,
        TENANT_USER_CREATE,
        TENANT_USER_UPDATE,
        TENANT_USER_DELETE,
        TENANT_TENANT_READ,
        TENANT_ACCESS_READ,
        TENANT_ACCESS_UPDATE,
        TENANT_API_KEY_READ,
        TENANT_API_KEY_CREATE,
        TENANT_API_KEY_UPDATE,
        TENANT_REQUEST_LOG_READ,
        TENANT_AUDIT_LOG_READ,
        TENANT_WALLET_READ,
        TENANT_MODEL_READ,
        TENANT_DASHBOARD_READ,
    ],
    "tenant_operator": [
        TENANT_USER_READ,
        TENANT_TENANT_READ,
        TENANT_ACCESS_READ,
        TENANT_REQUEST_LOG_READ,
        TENANT_AUDIT_LOG_READ,
        TENANT_WALLET_READ,
        TENANT_MODEL_READ,
        TENANT_DASHBOARD_READ,
    ],
}
