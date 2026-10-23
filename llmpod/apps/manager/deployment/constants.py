"""部署域常量：状态/阶段/步骤枚举族 + 分组元组。

只供 **manager 侧**（apps/manager/deployment、services/deployment_service、
services/deploy_log_service、apps/webserver/routes/admin/deployment、
apps/manager/services/task_projections）使用；Node 进程不加载本模块。

与 config/constants.py（全局）的分界：
- 跨进程 wire 契约（Node 也会用）→ config/constants.py：``InstanceStatus``（node 经
  report_instance_status 上报）、``TaskStatus``（node 执行器上报终态）。
- 部署域专属（Node 不感知）→ 本模块：``DeploymentStatus/Phase``、``DeployStep``、
  ``LogLevel/Status``、``NodeModel*``、全部部署分组、端口池。

全部 ``StrEnum``：同时是 ``str`` 子类，与裸字符串 / DB 列值 / JSON 序列化等价
（``DeploymentStatus.RUNNING == "running"`` 为 True），渐进替换零迁移。
DB CHECK 约束字面量以 models 层定义为准（存储层），与枚举值保持一致。
"""

from enum import StrEnum

from llmpod.config.constants import InstanceStatus

# ==================== 部署生命周期（model_deployment）====================


class DeploymentStatus(StrEnum):
    """model_deployment.status：部署生命周期（小写存储，DB CHECK ck_model_deployment_status）。

    creating/running/stopped/failed 可互相流转（stop 可停 running/failed/creating，
    start 可重启 stopped/failed）；deleting 由 delete 路由置位、级联清理后行即消失。
    launching 为历史遗留值（不在 DB CHECK 内），仅旧数据兼容读取。
    """

    CREATING = "creating"  # 已提交、编排进行中（desired 态，需控制器收敛 actual）
    RUNNING = "running"  # 部署成功，endpoint 可用
    STOPPED = "stopped"  # 用户停止；实例/容器保留（只停不删）
    FAILED = "failed"  # 失败；保留崩溃容器供 RPC 取证
    DELETING = "deleting"  # delete 路由置位，级联清理后行即消失
    LAUNCHING = "launching"  # 历史遗留值（不在 DB CHECK 内），仅旧数据兼容读取


class HealthStatus(StrEnum):
    """model_deployment.health_status：运行时健康（推论，独立于生命周期 status）。

    healthy/degraded 仅 status=running 期维护（DB CHECK ck_model_deployment_health），
    creating/stopped 等期为 NULL——生命周期照常推进，健康只反映实例层运行时状态。
    """

    HEALTHY = "healthy"  # 全部副本 serving
    DEGRADED = "degraded"  # 存在 down / provisioning 中的副本（自愈恢复窗口）


class EngineStatus(StrEnum):
    """model_deployment_instance.status：引擎实例投影状态（DB CHECK 同名）。

    纯投影无独立权威来源：全部分片 READY ⇔ ready；任一分片死 ⇒ down；全 PENDING ⇒
    provisioning；其余（启动中/部分就绪）⇒ starting。写点在投影链
    （services/deployment_projection），容器状态是唯一权威来源。
    """

    PROVISIONING = "provisioning"  # 分片全部待启动
    STARTING = "starting"  # 分片启动中（含部分就绪）
    READY = "ready"  # 全部分片 READY（head 健康）
    DOWN = "down"  # 任一分片死（崩溃/失败/停止）


class ReplicaStatus(StrEnum):
    """model_deployment_replica.status：副本投影状态（DB CHECK 同名）。

    纯投影：份内全部引擎 ready ⇔ serving；任一引擎 down ⇒ down（份内配对作废，
    整份判死）；其余 provisioning。写点在投影链。
    """

    PROVISIONING = "provisioning"  # 份内引擎未全部就绪
    SERVING = "serving"  # 份内全部引擎 ready，对外承担流量
    DOWN = "down"  # 任一引擎 down，端点摘流量


class DeploymentPhase(StrEnum):
    """model_deployment.phase：编排推进的可观测子态（DB CHECK ck_model_deployment_phase）。

    PENDING→STAGING_MODEL 在部署协程内推进；PULL_IMAGE 起交 reconciler 按 phase 驱动
    容器阶段直至 RUNNING。SCALING 是 running 期在线扩缩的暂驻子态（新副本独立推进，
    完成回 RUNNING）。STOPPING 是终态部署（stopped/deleting）下唯一允许写入的子态。
    """

    PENDING = "PENDING"  # 初始态，等待部署协程接管
    STAGING_MODEL = "STAGING_MODEL"  # 声明式模型分发至 node present
    PULL_IMAGE = "PULL_IMAGE"  # 拉镜像（交 reconciler 后第一步）
    START_HEAD = "START_HEAD"  # 起 head（node_rank=0）
    WAIT_HEAD_READY = "WAIT_HEAD_READY"  # 等 head /health 就绪（整集群就绪门）
    START_WORKERS = "START_WORKERS"  # 并发起 workers
    WAIT_WORKERS_READY = "WAIT_WORKERS_READY"  # 等 workers 稳定存活
    FINALIZING = "FINALIZING"  # 产出 endpoint + status=running
    RUNNING = "RUNNING"  # 部署达成
    SCALING = "SCALING"  # 在线扩缩中（status 仍 running；新副本 provisioning 独立推进）
    STOPPING = "STOPPING"  # 停止收敛子态（终态部署唯一允许写）


# ==================== 部署日志（deploy_log）====================


class DeployStep(StrEnum):
    """deploy_log.step：部署日志的步骤分类（应用层白名单，无 DB CHECK）。

    分两组：
    - 编排阶段（部署推进主轴，前端时间线据此渲染标题，与 DeploymentPhase 一一对应）：
      queued/staging_model/pull_image/start_head/head_health_check/start_workers/
      worker_health_check/finalize/running；
    - 事件类步骤（用户/系统操作 + 实例生命周期 + 兜底）：submit（提交）/ start（启动/
      重启）/ stop（手动停止）/ stopping（停止过程）/ start_instance（实例生命周期）/
      orchestration（异常终止/兜底）。
    """

    # 编排阶段
    QUEUED = "queued"  # 排队中（PENDING，尚未被调度接管）
    STAGING_MODEL = "staging_model"  # 下载模型
    PULL_IMAGE = "pull_image"  # 拉取镜像
    START_HEAD = "start_head"  # 启动主节点
    HEAD_HEALTH_CHECK = "head_health_check"  # 主节点健康检查
    START_WORKERS = "start_workers"  # 启动工作节点
    WORKER_HEALTH_CHECK = "worker_health_check"  # 工作节点健康检查
    FINALIZE = "finalize"  # 收尾中（生成端点）
    RUNNING = "running"  # 就绪（部署达成）
    # 事件类步骤
    SUBMIT = "submit"  # 部署提交
    START = "start"  # 启动/重启（stopped/failed 原卡重启）
    STOP = "stop"  # 手动停止（用户操作事件）
    STOPPING = "stopping"  # 停止过程（阶段/实例停止）
    SCALE = "scale"  # 在线扩缩（份数调整 + 逐份扩容/缩容事件）
    START_INSTANCE = "start_instance"  # 实例生命周期投影（启动/失败）
    ORCHESTRATION = "orchestration"  # 异常终止/兜底（无法归类时）


class LogLevel(StrEnum):
    """deploy_log.level：日志级别（DB CHECK ck_deploy_log_level）。"""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class LogStatus(StrEnum):
    """deploy_log.status：步骤进度态（DB CHECK ck_deploy_log_status）。

    语义是「这一步进行到哪」而非部署状态：step=stopping + status=running 表示
    「停止这一步进行中」，与部署的 running 同名但不同义。
    """

    PENDING = "pending"  # 步骤待开始
    RUNNING = "running"  # 步骤进行中
    SUCCESS = "success"  # 步骤完成
    FAILED = "failed"  # 步骤失败
    CANCELLED = "cancelled"  # 步骤被取消


# ==================== 模型分发（node_model）====================


class NodeModelDesiredState(StrEnum):
    """node_model.desired_state：manager 聚合的期望（present=需要这份 / absent=不需要）。"""

    PRESENT = "present"  # 需要这份
    ABSENT = "absent"  # 不需要


class NodeModelState(StrEnum):
    """node_model.status：node 回报的观测（DB CHECK ck_node_model_status）。"""

    ABSENT = "absent"  # 尚未下载
    DOWNLOADING = "downloading"  # 下载中
    VERIFYING = "verifying"  # 校验中
    PRESENT = "present"  # 已就位（model_path 权威）
    FAILED = "failed"  # 下载/校验失败


class CardState(StrEnum):
    """卡的部署痕迹（调度软偏好的事实值，词表定义在此处供生产/消费两侧共用）。

    - ``RESIDUAL``：只剩已停止/失败部署的声明行（卡可用，但调度评分排后面）
    - ``SHARED``：仍被运行中的其它部署占用（只在显存占比共享场景出现，更要避开）
    无痕迹的卡不进这个词表（``dict.get`` 取不到即最优）。
    """

    RESIDUAL = "residual"
    SHARED = "shared"


# ==================== 分组元组 ====================

# 部署驱动状态（desired≠终态，需控制器/恢复收敛 actual）——唯一定义处
DEPLOYMENT_INFLIGHT_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.CREATING,
    DeploymentStatus.LAUNCHING,
)
# 控制器扫描驱动：creating 推进 phase；stopped/failed 释放 VRAM+删 assignment 保留行/容器；
# deleting 级联删行+删容器。failed 在列以修复其 VRAM/assignment 泄漏。
DEPLOYMENT_RECONCILE_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.CREATING,
    DeploymentStatus.STOPPED,
    DeploymentStatus.DELETING,
    DeploymentStatus.FAILED,
)
# 部署终态（进入后不再推进 phase / 不再写回实例瞬态；STOPPING 子态例外）
DEPLOYMENT_TERMINAL_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.STOPPED,
    DeploymentStatus.DELETING,
    DeploymentStatus.FAILED,
)
# 可标 failed 的部署态：creating/launching（启动失败）+ running（运行期崩溃重启耗尽
# attempt）——不含 stopped/deleting（用户显式终态，失败收敛不得覆盖）
DEPLOYMENT_FAILABLE_STATUSES: tuple[DeploymentStatus, ...] = (
    *DEPLOYMENT_INFLIGHT_STATUSES,
    DeploymentStatus.RUNNING,
)
# 可 start（重启）的部署态（原卡重启的入口守卫）
DEPLOYMENT_RESTARTABLE_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.STOPPED,
    DeploymentStatus.FAILED,
)
# 可 stop 的部署态（stop 路由守卫）
DEPLOYMENT_STOPPABLE_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.RUNNING,
    DeploymentStatus.FAILED,
    DeploymentStatus.CREATING,
)
# 持有 GPU 声明占用/模型分发需求的部署态：stopped/failed/deleting 的残留声明行不计
# 占用（容器已停/即将释放）
DEPLOYMENT_ACTIVE_STATUSES: tuple[DeploymentStatus, ...] = (
    DeploymentStatus.CREATING,
    DeploymentStatus.RUNNING,
)
# reconciler 驱动的容器阶段，按真实推进序排列（勿按声明序重排——未来按序判进度
# 回退/恢复续跑会依赖此序）。RUNNING 后不再驱动；PENDING/STAGING_MODEL 归部署协程。
CONTAINER_PHASES: tuple[DeploymentPhase, ...] = (
    DeploymentPhase.PULL_IMAGE,
    DeploymentPhase.START_HEAD,
    DeploymentPhase.START_WORKERS,
    DeploymentPhase.WAIT_WORKERS_READY,
    DeploymentPhase.WAIT_HEAD_READY,
    DeploymentPhase.FINALIZING,
)
# 部署协程内的前置阶段（控制器见此二相时需唤醒 deploy 协程而非 reconciler）
STAGING_PHASES: tuple[DeploymentPhase, ...] = (
    DeploymentPhase.PENDING,
    DeploymentPhase.STAGING_MODEL,
)
# 实例瞬态（终态部署下禁止复活写入 set_instance_status 的守卫范围）
INSTANCE_TRANSIENT_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.PENDING,
    InstanceStatus.STARTING,
    InstanceStatus.RUNNING,
    InstanceStatus.READY,
)
# 容器已死的实例态（启动失败/运行期崩溃）：不得被 backfill 补写或幂等短路复活——
# 历史 START task SUCCESS 不再代表容器活着，须重走 attempt 建新 START
INSTANCE_DEAD_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.FAILED,
    InstanceStatus.CRASHED,
)
# RUNNING 部署运行期自愈的触发集（controller 挑候选 / reconcile 驱动重启共用）：
# CRASHED（崩溃）/ FAILED（启动失败遗留）/ PENDING（node 离线遗留）/ STARTING·RUNNING
# （上报丢失，由 backfill 或 monitor 收敛后退出触发集）
RUNTIME_ABNORMAL_INSTANCE_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.CRASHED,
    InstanceStatus.FAILED,
    InstanceStatus.PENDING,
    InstanceStatus.STARTING,
    InstanceStatus.RUNNING,
)
# 这些状态的实例不再持有端口（容器已停，端口可复用）
INSTANCE_PORT_RELEASED_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.STOPPED,
    InstanceStatus.FAILED,
)
# 引擎判死的容器分片态：崩溃/启动失败/停止中/已停止——停止中的容器无法对外服务，
# 引擎投影按 down 处理（scale 缩容的排空/停止窗口也覆盖）
ENGINE_SHARD_DEAD_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.CRASHED,
    InstanceStatus.FAILED,
    InstanceStatus.STOPPING,
    InstanceStatus.STOPPED,
)
# 出端点的引擎角色（端点投影只挂 serving 引擎）：full 单体 / decode（PD 分离时 D 侧
# 对外接客）；prefill 永不出端点
SERVING_ENGINE_ROLES: tuple[str, ...] = ("full", "decode")
# 每节点端口池：服务口（vLLM 等 HTTP 服务）与 rendezvous（多机 torch dist）各自独立查重
SERVICE_PORT_RANGE = range(8000, 8100)
RENDEZVOUS_PORT_RANGE = range(29501, 29600)
# rendezvous 主通信口之上的隔离带：NCCL/gloo 会在主口附近开额外通道（store 辅助连接），
# 相邻口若被其他引擎的主口占用会互相干扰。分配器每分配一个主口即跳过其后的
# ISOLATION_BAND 个口（池按「主口 + 带宽」步进消耗）。
RENDEZVOUS_ISOLATION_BAND = 10

# 同 resource_key 最大自动重试次数，超限等人工（四类任务共用，按 resource_key 独立计数）
MAX_AUTO_ATTEMPTS = 3
# 重试耗尽类失败的编排行文案（reconciler fail 调用点统一；实例 FAILED 行已带崩溃详情，不重复）
RETRY_EXHAUSTED_MESSAGE = "部署异常终止（已达最大重试次数）"


def runtime_restart_backoff_sec(trailing_failures: int) -> float:
    """运行期自愈重试的指数退避间隔：10s 起、每次翻倍、5min 封顶。

    无退避的固定短间隔会在故障期放大负载（毒容器被连续拉起）；无限退避则恢复太慢。
    ``trailing_failures`` 为该 resource_key 的末尾连续失败数（首次失败等 10s 再试）。
    """
    initial = 10.0
    cap = 300.0
    return min(initial * 2 ** max(0, trailing_failures - 1), cap)
