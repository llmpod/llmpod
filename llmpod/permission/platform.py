"""管理端（平台域，scope=platform）权限常量声明。

平台域权限覆盖管理端 ``/admin/*`` 的操作（cluster/node/gpu/system/access/user/tenant）
"""

from __future__ import annotations

from llmpod.permission._common import SCOPE_PLATFORM, _r
from llmpod.permission.menu import _menu
from llmpod.permission.registry import PermSpec

# ===== 平台独占操作权限（管理端 /admin/*）=====
CLUSTER_READ = _r(SCOPE_PLATFORM, "cluster", "read", "查看集群")
CLUSTER_CREATE = _r(SCOPE_PLATFORM, "cluster", "create", "创建集群")
CLUSTER_UPDATE = _r(SCOPE_PLATFORM, "cluster", "update", "更新集群")
CLUSTER_DELETE = _r(SCOPE_PLATFORM, "cluster", "delete", "删除集群")

NODE_READ = _r(SCOPE_PLATFORM, "node", "read", "查看节点")
NODE_UPDATE = _r(SCOPE_PLATFORM, "node", "update", "节点运维（含 tail-log 日志追踪）")
# 节点执行类独立权限：shell/docker-run/docker-pull 直接触发节点侧代码/容器执行，
# 与常规运维（改备注/状态/日志读取）分权授予；tail-log 只读日志，留在 UPDATE
NODE_EXEC = _r(SCOPE_PLATFORM, "node", "exec", "节点执行（Shell/容器/镜像操作）")
NODE_DELETE = _r(SCOPE_PLATFORM, "node", "delete", "注销节点")
NODE_BOOTSTRAP = _r(SCOPE_PLATFORM, "node", "bootstrap", "签发节点引导令牌（短期）")

GPU_READ = _r(SCOPE_PLATFORM, "gpu", "read", "查看 GPU")

# ===== 模型与部署（推理部署最小闭环）=====
MODEL_READ = _r(SCOPE_PLATFORM, "model", "read", "查看模型")
MODEL_CREATE = _r(SCOPE_PLATFORM, "model", "create", "创建模型/版本")
MODEL_UPDATE = _r(SCOPE_PLATFORM, "model", "update", "更新模型")
MODEL_DELETE = _r(SCOPE_PLATFORM, "model", "delete", "删除模型")

# 通用大文件切片上传（/admin/uploads/*）：与模型解耦的独立上传服务，
# 任何文件集合都可经此分片+断点续传落地到 manager 盘。
UPLOAD_WRITE = _r(SCOPE_PLATFORM, "upload", "write", "切片上传/合并/取消")

# 在线调试（/admin/inference/* 透传）：端点注入平台 Key 直调模型（跳过租户计费），
# 是「花平台的钱跑推理」的入口，须与普通浏览权限区分开单独授权。
INFERENCE_DEBUG = _r(SCOPE_PLATFORM, "inference_debug", "exec", "在线调试调用模型（注入平台 Key）")

MODEL_DEPLOYMENT_READ = _r(SCOPE_PLATFORM, "model_deployment", "read", "查看部署")
MODEL_DEPLOYMENT_CREATE = _r(SCOPE_PLATFORM, "model_deployment", "create", "创建部署")
MODEL_DEPLOYMENT_UPDATE = _r(SCOPE_PLATFORM, "model_deployment", "update", "停止/重启部署")
MODEL_DEPLOYMENT_DELETE = _r(SCOPE_PLATFORM, "model_deployment", "delete", "删除部署")

# API 网关配置态（ProxyServer）：Key 签发/生命周期、模型发布管理、租户模型授权
API_KEY_READ = _r(SCOPE_PLATFORM, "api_key", "read", "查看 API Key")
API_KEY_CREATE = _r(SCOPE_PLATFORM, "api_key", "create", "签发 API Key")
API_KEY_UPDATE = _r(SCOPE_PLATFORM, "api_key", "update", "禁用/启用/删除/调整 Key 限流配额")
PUBLISH_READ = _r(SCOPE_PLATFORM, "publish", "read", "查看模型发布")
PUBLISH_UPDATE = _r(SCOPE_PLATFORM, "publish", "update", "上下架/改名/定价/路由调整")
ENDPOINT_READ = _r(SCOPE_PLATFORM, "endpoint", "read", "查看来源端点")
ENDPOINT_UPDATE = _r(SCOPE_PLATFORM, "endpoint", "update", "登记/维护外部来源端点")
REQUEST_LOG_READ = _r(SCOPE_PLATFORM, "request_log", "read", "查看调用日志")
DASHBOARD_READ = _r(SCOPE_PLATFORM, "dashboard", "read", "查看平台数据看板（调用/延迟/错误率/引擎实时）")
AUDIT_LOG_READ = _r(SCOPE_PLATFORM, "audit_log", "read", "查看审计日志（全量）")
WALLET_READ = _r(SCOPE_PLATFORM, "wallet", "read", "查看钱包余额/流水")
WALLET_UPDATE = _r(SCOPE_PLATFORM, "wallet", "update", "钱包调额（充值/扣减）")

SYSTEM_READ = _r(SCOPE_PLATFORM, "system", "read", "查看系统配置")
SYSTEM_UPDATE = _r(SCOPE_PLATFORM, "system", "update", "更新系统配置")

SETTINGS_READ = _r(SCOPE_PLATFORM, "settings", "read", "查看全局设置")
SETTINGS_UPDATE = _r(SCOPE_PLATFORM, "settings", "update", "更新全局设置（品牌/主题/登录页/维护模式）")

INFERENCE_IMAGE_READ = _r(SCOPE_PLATFORM, "inference_image", "read", "查看推理镜像目录")

ACCESS_READ = _r(SCOPE_PLATFORM, "access", "read", "查看角色与权限")
ACCESS_UPDATE = _r(SCOPE_PLATFORM, "access", "update", "管理角色与授权")

# ===== 跨域拆分：user / tenant 资源的平台侧语义（租户侧见 tenant 模块）=====
ADMIN_USER_READ = _r(SCOPE_PLATFORM, "user", "read", "查看用户（全租户）")
ADMIN_USER_CREATE = _r(SCOPE_PLATFORM, "user", "create", "创建用户（任意租户）")
ADMIN_USER_UPDATE = _r(SCOPE_PLATFORM, "user", "update", "更新用户（任意租户）")
ADMIN_USER_DELETE = _r(SCOPE_PLATFORM, "user", "delete", "删除用户（任意租户）")

PLATFORM_TENANT_READ = _r(SCOPE_PLATFORM, "tenant", "read", "查看租户列表")
PLATFORM_TENANT_CREATE = _r(SCOPE_PLATFORM, "tenant", "create", "创建租户")
PLATFORM_TENANT_UPDATE = _r(SCOPE_PLATFORM, "tenant", "update", "更新租户")
PLATFORM_TENANT_DELETE = _r(SCOPE_PLATFORM, "tenant", "delete", "删除租户")

# ===== 管理端菜单（scope=platform）=====
# 声明只进菜单注册表（非权限）；required = 可见性判定（OR：持任一即可见；
# 空 = 平台域全员可见）。登录页 /admin/login 无需登录、非菜单，不声明。
_menu(SCOPE_PLATFORM, "/admin/dashboard", "总览大屏", (DASHBOARD_READ,))
_menu(SCOPE_PLATFORM, "/admin/statistics", "调用统计", (REQUEST_LOG_READ,))
_menu(SCOPE_PLATFORM, "/admin/engine-dashboard", "推理引擎", (DASHBOARD_READ,))
_menu(SCOPE_PLATFORM, "/admin/node-list", "节点列表", (NODE_READ,))
_menu(SCOPE_PLATFORM, "/admin/node-pool", "节点池", (CLUSTER_READ,))
_menu(SCOPE_PLATFORM, "/admin/deploy", "部署管理", (MODEL_DEPLOYMENT_READ,))
_menu(SCOPE_PLATFORM, "/admin/model-management", "模型管理", (MODEL_READ,))
_menu(SCOPE_PLATFORM, "/admin/model-release", "模型发布", (PUBLISH_READ,))
_menu(SCOPE_PLATFORM, "/admin/tenant-management", "租户管理", (PLATFORM_TENANT_READ,))
_menu(SCOPE_PLATFORM, "/admin/user", "用户管理", (ADMIN_USER_READ,))
_menu(SCOPE_PLATFORM, "/admin/role", "角色管理", (ACCESS_READ,))
_menu(SCOPE_PLATFORM, "/admin/playground", "在线调试", (INFERENCE_DEBUG,))
_menu(SCOPE_PLATFORM, "/admin/model-mark", "模型市场", (PUBLISH_READ,))
_menu(SCOPE_PLATFORM, "/admin/api-key", "API 密钥", (API_KEY_READ,))
_menu(SCOPE_PLATFORM, "/admin/logs", "审计日志", (AUDIT_LOG_READ,))
_menu(SCOPE_PLATFORM, "/admin/bills", "计量计费", (REQUEST_LOG_READ,))
_menu(SCOPE_PLATFORM, "/admin/config", "通用设置", (SETTINGS_READ,))
_menu(SCOPE_PLATFORM, "/admin/about", "关于")

# 平台只读权限集：operator/运维 等只读平台资源
PLATFORM_READONLY_OPS: list[PermSpec] = [
    CLUSTER_READ,
    NODE_READ,
    GPU_READ,
    MODEL_READ,
    MODEL_DEPLOYMENT_READ,
    SYSTEM_READ,
    SETTINGS_READ,
    ACCESS_READ,
    # ADMIN_USER_READ,
    PLATFORM_TENANT_READ,
]

# ===== 平台域内置角色 → 权限映射 =====
# super_admin 靠 is_superuser 会话短路，不在此列（也无需 seed 授权）。
# operator（平台运维）：平台资源读写，但不持 ACCESS_UPDATE（无权改角色授权）。
# 删除租户（PLATFORM_TENANT_DELETE）属高危操作，仅 super_admin（短路）可用，不授予 operator。
ROLE_PERMISSIONS: dict[str, list[PermSpec]] = {
    "operator": [
        CLUSTER_READ,
        CLUSTER_CREATE,
        CLUSTER_UPDATE,
        CLUSTER_DELETE,
        NODE_READ,
        NODE_UPDATE,
        NODE_EXEC,
        NODE_BOOTSTRAP,
        GPU_READ,
        UPLOAD_WRITE,
        INFERENCE_DEBUG,
        MODEL_CREATE,
        MODEL_UPDATE,
        MODEL_DELETE,
        MODEL_DEPLOYMENT_CREATE,
        MODEL_DEPLOYMENT_UPDATE,
        MODEL_DEPLOYMENT_DELETE,
        API_KEY_READ,
        API_KEY_CREATE,
        API_KEY_UPDATE,
        PUBLISH_READ,
        PUBLISH_UPDATE,
        ENDPOINT_READ,
        ENDPOINT_UPDATE,
        REQUEST_LOG_READ,
        DASHBOARD_READ,
        AUDIT_LOG_READ,
        WALLET_READ,
        WALLET_UPDATE,
        ADMIN_USER_READ,
        ADMIN_USER_CREATE,
        ADMIN_USER_UPDATE,
        ADMIN_USER_DELETE,
        PLATFORM_TENANT_READ,
        PLATFORM_TENANT_CREATE,
        PLATFORM_TENANT_UPDATE,
        SYSTEM_READ,
        SETTINGS_READ,
        ACCESS_READ,
    ],
}
