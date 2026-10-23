"""LLMPod 自定义异常体系（跨 app 共享的通用领域异常）。

每类异常携带一个稳定的业务错误码 ``error_code``（``ErrorCode`` 枚举成员），
由全局异常处理器（``apps/webserver/errors.py``）统一塞进响应 body 的 ``error_code``
字段。前端据此 ``switch error_code`` 做分支，永远不必匹配 ``message`` 字符串。
HTTP status 与 body.code 不变，``error_code`` 为新增可空字段，不破坏既有契约。

各 app 专属异常（如 node 的 TaskExecutionError）请放在对应 apps/<app>/exceptions.py，
继承 LLMPodError 以保持统一异常基类（自动继承 ``ErrorCode.BUSINESS_ERROR`` 兜底码）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """稳定的业务错误码。值即字符串本身，供前端 ``switch`` 分支。

    与 HTTP status 一对多：同一 status 可对应多个子类型码（如 401 下
    UNAUTHENTICATED / TOKEN_EXPIRED / INVALID_CREDENTIALS），前端按码分支。
    """

    # 401 认证失败三类：前端需分别处理（静默跳登录 / 过期提示 / 表单内提示）
    UNAUTHENTICATED = "UNAUTHENTICATED"  # 未带 token / 头格式错：静默跳登录
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # 会话过期或被踢下线：跳登录 + "已过期"提示
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"  # 登录凭据错：登录表单内提示
    # 403 权限不足
    PERMISSION_DENIED = "PERMISSION_DENIED"
    # 404 资源不存在
    NOT_FOUND = "NOT_FOUND"
    # 409 资源冲突
    CONFLICT = "CONFLICT"
    # 422 输入校验失败
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # 400 通用业务错误（LLMPodError 兜底，含 ConfigError 及各 app 自定义子类）
    BUSINESS_ERROR = "BUSINESS_ERROR"
    # 500 服务器内部错误
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # 维护中：503 拦登录/租户 Key，401 把存量非平台侧会话踢回登录页（message 为维护文案）
    MAINTENANCE = "MAINTENANCE"

    # ===== 切片上传（/admin/uploads/*）业务子码 =====
    # 404：会话/文件不存在
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    # 409：会话已失效（已清理/过期/Redis 丢失）
    SESSION_EXPIRED = "SESSION_EXPIRED"
    # 409：会话已取消（命中 abort 取消标记）
    SESSION_ABORTED = "SESSION_ABORTED"
    # 409：合并进行中拒新分片
    SESSION_FINALIZING = "SESSION_FINALIZING"
    # 409：分片 sha 不符 declare 声明
    PART_SHA_MISMATCH = "PART_SHA_MISMATCH"
    # 400：分片序号越界
    PART_OUT_OF_RANGE = "PART_OUT_OF_RANGE"
    # 409：该 part 已收但本次 sha 不同
    DUPLICATE_PART_INCONSISTENT = "DUPLICATE_PART_INCONSISTENT"
    # 409：存在未收齐的文件，不可合并
    SESSION_NOT_FINALIZABLE = "SESSION_NOT_FINALIZABLE"
    # 500：合并任意异常（失败即弃）
    FINALIZE_FAILED = "FINALIZE_FAILED"
    # 409：磁盘剩余不足（declare 预检）
    DISK_FULL = "DISK_FULL"
    # 400：总大小超上传上限
    OVERSIZE = "OVERSIZE"
    # 400：文件名非法/穿越
    INVALID_FILENAME = "INVALID_FILENAME"
    # 400：manifest 请求体不合法
    INVALID_MANIFEST = "INVALID_MANIFEST"


class LLMPodError(Exception):
    """所有 LLMPod 异常的基类。

    ``error_code`` 为类属性，子类可覆盖；默认 ``BUSINESS_ERROR``，
    由全局异常处理器映射为 400。实例可经 ``error_code=`` 覆盖类默认——例如
    上传层多个子码复用 ``Conflict``：``raise Conflict(msg, error_code=ErrorCode.PART_SHA_MISMATCH)``。
    """

    error_code: ErrorCode = ErrorCode.BUSINESS_ERROR

    def __init__(self, message: str = "", error_code: ErrorCode | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class ConfigError(LLMPodError):
    """配置错误（缺少必需的环境变量、值无效、依赖未初始化等）。"""


# 维护模式对外提示默认文案（maintenanceMessage 未配置时）。唯一定义处：
# settings_service.get_maintenance 与 webserver 的 503 handler 共用，避免两处字面量漂移。
DEFAULT_MAINTENANCE_MESSAGE = "系统维护中"


class Maintenance(LLMPodError):
    """平台维护中（维护模式生效期间的对外拒绝）。

    message 为面向用户的维护文案（全局设置的 maintenanceMessage，
    缺省回落 DEFAULT_MAINTENANCE_MESSAGE），处理器映射 HTTP 503。
    """

    error_code = ErrorCode.MAINTENANCE


class AuthError(LLMPodError):
    """认证失败（令牌无效、会话过期、凭据错误等）。

    默认码 ``UNAUTHENTICATED``；会话过期场景显式传 ``TOKEN_EXPIRED``、
    登录凭据错显式传 ``INVALID_CREDENTIALS``，使前端能区分 401 子类型。

    ``subject`` 为可选的内部上下文（如登录失败时已命中的注册用户 ORM 对象）：
    仅供服务端审计等内部逻辑判定主体是否真实存在，不进入对外响应，
    客户端看到的错误码/message 与未命中用户时保持一致（防用户名枚举）。
    """

    error_code = ErrorCode.UNAUTHENTICATED

    def __init__(
        self,
        message: str = "认证失败",
        error_code: ErrorCode = ErrorCode.UNAUTHENTICATED,
        subject: Any = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.subject = subject


class PermissionDenied(LLMPodError):
    """RBAC 权限检查失败。"""

    error_code = ErrorCode.PERMISSION_DENIED

    def __init__(
        self,
        message: str = "权限不足",
        scope: str = "",
        resource: str = "",
        action: str = "",
    ):
        super().__init__(message)
        self.scope = scope
        self.resource = resource
        self.action = action


# NotFound 对外文案里的资源中文名：raise 侧传英文资源标识时经此表转成中文展示；
# 未登记的（含直接传完整文案的调用）原样透出。结构化字段 ``resource`` 始终是
# raise 侧传入的原串，不受映射影响。
_RESOURCE_LABELS: dict[str, str] = {
    "Model": "模型",
    "Model files": "模型文件",
    "Model source_url": "模型下载地址",
    "ModelDeployment": "模型部署",
    "Role": "角色",
    "User": "用户",
    "Cluster": "集群",
    "Node": "节点",
    "Node (online)": "在线节点",
    "Tenant": "租户",
    "Instance": "实例",
    "Gpu": "GPU",
    "File": "文件",
    "Config": "配置项",
    # 切片上传（upload service 小写资源名）
    "file": "文件",
    "session": "上传会话",
}


class NotFound(LLMPodError):
    """请求的资源未找到。"""

    error_code = ErrorCode.NOT_FOUND

    def __init__(
        self,
        resource: str,
        resource_id: str | int = "",
        error_code: ErrorCode | None = None,
    ):
        # 两类调用约定：传英文资源标识（"Role"+id，经 _RESOURCE_LABELS 转中文文案）；
        # 或直接传完整文案（f"租户不存在: id={...}"，原样透出，不追加任何后缀）。
        label = _RESOURCE_LABELS.get(resource)
        if label is None:
            message = f"{resource}不存在: {resource_id}" if resource_id else resource
        else:
            message = f"{label}不存在: {resource_id}" if resource_id else f"{label}不存在"
        super().__init__(message)
        self.resource = resource
        self.resource_id = resource_id
        if error_code is not None:
            self.error_code = error_code


class Conflict(LLMPodError):
    """资源冲突（重复、约束违反等）。"""

    error_code = ErrorCode.CONFLICT


class LLMPodValidationError(LLMPodError):
    """输入验证失败。"""

    error_code = ErrorCode.VALIDATION_ERROR
