"""系统全局常量：跨 app / 跨进程共享。

分层约定（跨 app 依赖的分界，避免循环依赖）：
- **部署域**（manager 侧专属，Node 进程不加载）→ ``apps/manager/deployment/constants.py``：
  ``DeploymentStatus/Phase``、``DeployStep``、``LogLevel/Status``、``NodeModel*``、部署分组。
- **webserver 领域**（认证/用户/租户，仅 web 后端自用）→ ``apps/webserver/constants.py``：
  ``AccountStatus``。
- **本文件只放真正被多个 app 共享的**：跨进程 wire 契约（``InstanceStatus`` /
  ``TaskStatus``，Node 侧 agent/executor 也用）、资源域（node/gpu/cluster，manager
  app 与 webserver app 共用）、基础设施、RBAC/令牌/分页。

状态一律用 ``StrEnum``：同时是 ``str`` 子类，与裸字符串、DB 列值、JSON 序列化等价
（``TaskStatus.RUNNING == "RUNNING"`` 为 True），渐进替换零迁移。
DB CHECK 约束字面量以 models 层为存储层权威出处，与枚举值保持一致。
"""

from enum import StrEnum

# ==================== 基础设施：端口 / 心跳 / 重连 ====================

# 默认端口
DEFAULT_WEBSERVER_PORT = 8080  # 管理端 HTTP
DEFAULT_PROXY_PORT = 8090  # API 网关

# 心跳
DEFAULT_HEARTBEAT_INTERVAL = 10  # 秒
HEARTBEAT_JITTER = 1.0  # +/- 秒

# 重连
DEFAULT_RECONNECT_FAST = 5  # 秒
DEFAULT_RECONNECT_SLOW = 30  # 秒
DEFAULT_RECONNECT_FAST_WINDOW = 60  # 秒

# ==================== 跨进程契约：实例 / 任务状态 ====================


class InstanceStatus(StrEnum):
    """instance.status：单实例长期状态（大写存储，DB CHECK ck_instance_status）。

    跨进程契约：Node 经 ``report_instance_status`` 上报、manager 落库；executor 写
    extra 里的 ``instance_status`` 也用本枚举。PENDING 由编排器在规划/重置时写入。
    """

    PENDING = "PENDING"  # 规划完成、待启动
    STARTING = "STARTING"  # 容器启动中
    RUNNING = "RUNNING"  # 容器稳定运行
    READY = "READY"  # 就绪（health 通过）
    CRASHED = "CRASHED"  # 运行期容器异常退出/被删（非调度动作），自动重启中
    FAILED = "FAILED"  # 失败（保留崩溃容器供取证）
    STOPPING = "STOPPING"  # 停止中
    STOPPED = "STOPPED"  # 已停止（终态，不被 stale 上报覆盖）


class TaskStatus(StrEnum):
    """task.status：节点任务生命周期（大写存储，DB CHECK ck_task_status）。

    跨进程契约：Manager 建行、Node 执行器 ``report("SUCCESS", ...)`` 等上报终态。
    """

    PENDING = "PENDING"  # 已建待下发/触达
    RUNNING = "RUNNING"  # node 执行中
    SUCCESS = "SUCCESS"  # 成功
    FAILED = "FAILED"  # 失败
    CANCELLED = "CANCELLED"  # 被取消


class EnsureModelStage(StrEnum):
    """ENSURE_MODEL 的子阶段词表（``TaskEventPayload.stage`` wire 值，Node 写 / Manager 读）。

    任务状态三层模型的第二层：只在第一层 RUNNING 期间推进，进终态后冻结为最后快照。
    与 ``NodeModelState`` 对应（Manager 端投影把 stage 映射成 node_model.status）。
    """

    DOWNLOADING = "downloading"  # 下载中
    VERIFYING = "verifying"  # 校验中（verify 阶段）
    FINISHED = "finished"  # 完成（下载/校验结束）


class PullImageStage(StrEnum):
    """PULL_IMAGE 的子阶段词表（wire 契约同 EnsureModelStage）。

    值域与 EnsureModelStage 独立、允许跨族重复（如 FINISHED）；消费端按
    (resource_type, stage) 二元组匹配，不做全局字符串匹配。无 DB CHECK（开放词表）。
    """

    PULLING = "pulling"  # 拉镜像中
    FINISHED = "finished"  # 拉取结束


# 第三层辅助量的语义约定：``task.progress``（0-100）只有有量任务（ENSURE_MODEL /
# PULL_IMAGE）才报；瞬时任务（START/STOP_INSTANCE）不报、保持 0——前端按 task_type
# 决定是否渲染进度条，"无量任务的 0" 与 "有量任务刚启动的 0" 在消费端消歧。


# 尚未出结果的任务（占资源、可取消/可复用续接）
TASK_INCOMPLETE_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
)
# 已出结果、不再变化
TASK_TERMINAL_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.SUCCESS,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


class TaskType(StrEnum):
    """task.type：任务类型（跨进程契约——manager 建行下发、node executor registry 按此分发）。"""

    ENSURE_MODEL = "ENSURE_MODEL"  # 模型分发（下载 + verify 至 present）
    PULL_IMAGE = "PULL_IMAGE"  # 拉推理镜像
    START_INSTANCE = "START_INSTANCE"  # 起实例容器（含 readiness 探活）
    STOP_INSTANCE = "STOP_INSTANCE"  # 停实例容器（纯停不删）


class TaskResourceType(StrEnum):
    """task.resource_type：任务作用的资源类别（同 resource 去重/复用的分组键）。"""

    MODEL_PLACEMENT = "MODEL_PLACEMENT"  # (model, node) 模型分发（标识符沿用 PLACEMENT）
    IMAGE_PULL = "IMAGE_PULL"  # (node, image) 镜像拉取（跨部署共享）
    INSTANCE = "INSTANCE"  # (deployment, rank) 实例容器生命周期


# ==================== 资源域（node / gpu / cluster）====================


class NodeStatus(StrEnum):
    """node.status：节点状态（DB CHECK ck_node_status）。"""

    ONLINE = "online"  # 在线
    OFFLINE = "offline"  # 离线
    MAINTENANCE = "maintenance"  # 维护中
    BUSY = "busy"  # 繁忙
    DRAINED = "drained"  # 已排空


class GpuStatus(StrEnum):
    """gpu.status：online=在线 | stale=心跳过期 | offline=离线。"""

    ONLINE = "online"
    STALE = "stale"
    OFFLINE = "offline"


class ClusterStatus(StrEnum):
    """cluster.status：active=启用 | inactive=停用。"""

    ACTIVE = "active"
    INACTIVE = "inactive"


# ==================== RBAC：租户保留值 / 角色代码 ====================
# （访问域 AccessDomain 与菜单映射 ACCESS_DOMAIN_MENU_SCOPE 属 web 领域，
# 定义在 apps/webserver/constants.py）

# 平台租户 ID：平台域角色与用户归属的保留租户，禁止删除（删除会扰乱平台域 RBAC）。
# 它与「无租户维度」是两回事，不要互相兼职：0 是**真实归属**（管理员租户自身的数据，
# 如平台用户/角色、登录登出、平台租户的 Key），NULL 是**没有租户维度**（集群/节点/GPU/
# 模型/路由/端点/系统配置等本就没有 tenant_id 列的资源上的操作、system 主体写入的行、
# 平台直发 Key 的请求）。平台视角查询可把 0 当「平台域」哨兵取 NULL ∪ 0；租户视角
# （access_domain=tenant）一律精确等值，不得放宽——access_domain 由角色归属判定，
# 与 user.tenant_id 不保证一致，放大会越出本租户作用域。
PLATFORM_TENANT_ID = 0

# 绕过细粒度 RBAC 检查的超管角色代码（平台根，无需逐条授权）
SUPER_ADMIN_ROLE_CODES = ("super_admin",)

# 各域的管理员角色代码：回收用户此类角色时须确保至少保留一个用户，防止域"无管理员"。
# 平台域 super_admin + 租户域 tenant_admin。
ADMIN_ROLE_CODES = ("super_admin", "tenant_admin")

# 租户超级管理员角色代码：创建租户时引导的租户超管写死绑此角色（= tenant_admin，租户域管理员，已由 seed 授权 tenant:user:* 全套，开通即可登录管理本租户用户）。
# 与 SUPER_ADMIN_ROLE_CODES 区别：后者是平台根超管（is_superuser 短路），不可用于租户。
TENANT_SUPER_ADMIN_ROLE_CODE = "tenant_admin"

# ==================== 令牌 / 分页 ====================

# 令牌
TOKEN_HEADER = "Authorization"
TOKEN_PREFIX = "Bearer"

# 分页
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# ==================== API 网关（ProxyServer）平台契约 ====================

# ApiKey Redis 全量缓存 key 前缀：
# key = llmpod:apikey:{sha256}，value = Key 配置 JSON（只含哈希，不含明文）。
# 失效协议：管理端在禁用/删除/变更写 PG 后只做 DEL 不写内容；TTL 300s 兜底。
APIKEY_CACHE_PREFIX = "llmpod:apikey:"
