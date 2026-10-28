"""审计日志服务：低侵入的操作审计记录。

两条落账路径：

1. ``@audit_action`` 装饰器——路由层自动记录（写端点接入 ≈ 一行），是主力入口；
2. ``write_audit``——独立 session 直接落一行（登录/登出/权限拒绝等特例）。

核心约束：审计写入用**独立 AsyncSession**（``AsyncSessionLocal`` 连接池），与业务事务
完全隔离——业务失败回滚后审计行照常落地，审计写入失败也只 ``log.error`` 绝不影响
业务响应（成功路径绝不因审计把已 commit 的业务改成 500）。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from contextvars import ContextVar
from functools import wraps as functools_wraps
from typing import Any, Callable

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models.audit_log import AuditLog
from llmpod.services.tenant_scope import platform_domain_clause, platform_scope_requested

log = logging.getLogger("llmpod.services.audit")

# 请求来源 IP：由 get_current_user 在鉴权时写入（与 handler 同任务，contextvar 可见），
# 装饰器/权限拒绝落账读取。取不到为 None，不炸。
audit_ip_ctx: ContextVar[str | None] = ContextVar("audit_ip", default=None)

# 独立 session 工厂（测试注入点）：默认为 AsyncSessionLocal；测试替换为内存工厂
# （FakeSession 不支持真正的独立连接提交，故独立 session 路径须注入真实/替换工厂）。
_session_factory: Callable[[], AsyncSession] | None = None

# 请求参数脱敏：键名命中这些片段（大小写不敏感）→ 值打码 ``***``。
# ``auth`` 单列覆盖 source_auth（模型来源凭据，键名不含 token/secret 等片段）；
# 宁可多打码（如 *_author 这类键名罕见）也不能漏过任何凭据形态的键。
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|credential|auth|authorization|api[_-]?key)", re.IGNORECASE
)
# detail JSONB 序列化上限（字节），超限截断并加标记，防超大 body 撑爆审计行。
_MAX_DETAIL_BYTES = 4096
# 失败审计里异常摘要的上限（字符）。
_MAX_ERROR_CHARS = 512

# 参数快照时跳过的基础设施依赖名（非请求语义，或已在 actor/ip 字段单独承载）。
_SKIP_PARAM_NAMES = {"db", "user", "redis", "response", "settings", "token", "credentials", "_perm"}


def set_audit_ip(ip: str | None) -> None:
    """写入当前请求来源 IP（由鉴权依赖调用）。"""
    audit_ip_ctx.set(ip)


def get_audit_ip() -> str | None:
    """读取当前请求来源 IP，取不到为 None。"""
    return audit_ip_ctx.get()


def _sanitize_value(value: Any) -> Any:
    """递归脱敏：dict 键命中敏感片段→``***``，list/tuple 递归，其余原样。"""
    if isinstance(value, dict):
        return {
            (str(k)): ("***" if _SENSITIVE_KEY_PATTERN.search(str(k)) else _sanitize_value(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(v) for v in value]
    return value


def sanitize_params(params: Any) -> Any:
    """请求参数脱敏快照：敏感键打码；JSON 序列化超 4KB 截断加标记。

    正常返回脱敏后的 dict；超限时返回 ``{"_truncated": True, "data": "<前 4KB JSON>"}``。
    截断按字节计（非字符），多字节 UTF-8 内容不会超限。
    """
    cleaned = _sanitize_value(params)
    serialized = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(serialized.encode("utf-8")) <= _MAX_DETAIL_BYTES:
        return cleaned
    truncated = serialized.encode("utf-8")[:_MAX_DETAIL_BYTES].decode("utf-8", errors="ignore")
    return {"_truncated": True, "data": truncated}


def _collect_params(kwargs: dict[str, Any], exclude_unset: bool = False) -> dict[str, Any]:
    """从 handler kwargs 提取请求参数快照。

    只收 pydantic 请求体（``model_dump(mode="json")`` 保证 JSONB 可序列化）与标量
    路径/查询参数；跳过 db/user/redis 等基础设施依赖与文件流。默认 ``exclude_none=True``
    让 update 类请求只记「实际改动」字段，reduce 审计噪音。三态更新端点（null=清空，
    如关维护开关）需显式 null 留痕时传 ``exclude_unset=True``——``exclude_unset`` 保留
    请求体里显式传的 null，只剔除未提供的键。
    """
    params: dict[str, Any] = {}
    for name, value in kwargs.items():
        if name in _SKIP_PARAM_NAMES:
            continue
        if isinstance(value, BaseModel):
            # 两口径正交：exclude_unset 保留显式 null、剔除未提供键；否则剔除 null 保留全部键
            params[name] = value.model_dump(
                exclude_unset=exclude_unset,
                exclude_none=not exclude_unset,
                mode="json",
            )
        elif isinstance(value, (str, int, float, bool)):
            params[name] = value
        # list/dict/文件流/Request 等非标量、非 pydantic 值：跳过（不具审计价值且可能不可序列化）
    return params


def _extract_id(data: Any) -> str | None:
    """从响应 data（dict 或 ORM/pydantic 对象）里取 ``id``，返回字符串快照或 None。"""
    if data is None:
        return None
    if isinstance(data, dict):
        return str(data["id"]) if data.get("id") is not None else None
    id_val = getattr(data, "id", None)
    return str(id_val) if id_val is not None else None


def _result_data_id(result: Any) -> str | None:
    """从统一响应里兜底取资源 ID：``Result.data`` 的 ``id``（dict 或对象均支持）。"""
    if not isinstance(result, dict):
        return _extract_id(result)
    return _extract_id(result.get("data"))


def _resolve_resource_id(
    resource_id: str | Callable[[Any], Any] | None,
    kwargs: dict[str, Any],
    result: Any,
) -> str | None:
    """resource_id 三种来源（优先级递减）：

    - callable：以返回值调用（create 场景从新资源取 id）；
    - str：视为路径/请求参数名，从 kwargs 取值；
    - None：从 ``Result.data.id`` 兜底。
    """
    if callable(resource_id):
        if result is None:
            # 失败路径无返回值（资源未创建），无法从返回值取 id
            return None
        try:
            value = resource_id(result)
        except Exception:
            log.exception("审计 resource_id callable 失败")
            value = None
        return str(value) if value is not None else None
    if isinstance(resource_id, str):
        value = kwargs.get(resource_id)
        return str(value) if value is not None else None
    return _result_data_id(result)


def tenant_id_for_user(user: Any) -> int | None:
    """数据归属租户：租户域操作归属 = ``user.tenant_id``；平台域操作拿不到则 None。

    唯一定义处：权限拒绝落账（deps.py）与装饰器归属解析共用，避免枚举/字符串两套判定漂移。
    """
    if user is not None and user.access_domain in ("tenant", "tenant_observer"):
        return user.tenant_id
    return None


def _resolve_tenant_id(
    tenant_id_arg: str | None,
    tenant_id_resolver: Callable[[Any], Any] | None,
    kwargs: dict[str, Any],
    user: Any,
    result: Any,
    fixed_tenant_id: int | None = None,
    owner_is_actor: bool = False,
) -> int | None:
    """tenant_id 归属（= 数据归属租户，非操作者租户）：

    - fixed_tenant_id：该端点的资源恒定归属某租户（平台用户/角色管理恒归平台租户 0）
      ——成功与失败都取该值，不依赖返回值，失败行不会退化成「无归属」；
    - owner_is_actor：资源就是**操作者本人的数据**（自助改菜单等）——归属取操作者所属
      租户，平台租户账号因此记 0，与自助改密（显式传 ``user.tenant_id``）同口径；
    - resolver（callable）：以返回值取（创建租户等）；失败路径拿不到返回值，退回默认；
    - tenant_id_arg：从 kwargs 取（admin 代操作某租户资源）；
    - 默认：租户域操作归属 = ``user.tenant_id``（见 :func:`tenant_id_for_user`）。
    """
    if fixed_tenant_id is not None:
        return fixed_tenant_id
    if owner_is_actor:
        return user.tenant_id
    if tenant_id_resolver is not None and result is not None:
        try:
            value = tenant_id_resolver(result)
        except Exception:
            log.exception("审计 tenant_id resolver 失败")
            value = None
        return value if isinstance(value, int) else None
    if tenant_id_arg is not None:
        value = kwargs.get(tenant_id_arg)
        return value if isinstance(value, int) else None
    return tenant_id_for_user(user)


def _default_session_factory() -> AsyncSession:
    """默认独立 session 工厂（运行时取全局 AsyncSessionLocal，避免 import 期捕获 None）。"""
    from llmpod.db.pg import AsyncSessionLocal

    if AsyncSessionLocal is None:
        raise RuntimeError("PostgreSQL 未初始化（AsyncSessionLocal 为 None）")
    return AsyncSessionLocal()


async def _write_audit(entry: AuditLog) -> None:
    """独立 session 落一行审计；任何失败只 ``log.error``，绝不抛出。"""
    factory = _session_factory or _default_session_factory
    try:
        session = factory()
        try:
            session.add(entry)
            await session.commit()
        finally:
            await session.close()
    except Exception:
        log.error(
            "审计写入失败 action=%s resource=%s actor=%s:%s",
            entry.action,
            entry.resource_type,
            entry.actor_type,
            entry.actor_id,
            exc_info=True,
        )


def _build_audit_entry(
    *,
    actor_type: str = "user",
    actor_id: int,
    action: str,
    actor_name: str = "",
    tenant_id: int | None = None,
    resource_type: str = "",
    resource_id: str = "",
    result: str = "success",
    detail: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """构建一条审计行，统一字段归一化（空串→None、``detail`` 兜底空 dict）。

    唯一定义处：``write_audit`` / 装饰器 ``_record`` 两条路径共用，
    避免字段约定（空串 vs None、``result`` 默认值）在多处漂移。
    """
    return AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_name=actor_name or None,
        tenant_id=tenant_id,
        action=action,
        result=result,
        resource_type=resource_type or None,
        resource_id=resource_id or None,
        detail=detail or {},
        ip_address=ip_address or None,
    )


async def write_audit(
    *,
    actor_type: str = "user",
    actor_id: int = 0,
    action: str,
    actor_name: str = "",
    tenant_id: int | None = None,
    resource_type: str = "",
    resource_id: str = "",
    result: str = "success",
    detail: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """独立 session 落一条审计（登录/登出/权限拒绝等不走装饰器的特例场景）。"""
    await _write_audit(
        _build_audit_entry(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            actor_name=actor_name,
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            detail=detail,
            ip_address=ip_address,
        )
    )


def _resolved_signature(func: Callable[..., Any]) -> inspect.Signature:
    """把 ``func`` 的字符串参数注解解析为真实对象，挂到 wrapper 的 ``__signature__``。

    路由模块普遍 ``from __future__ import annotations``（注解全是字符串）。装饰器 wrapper
    定义在本模块，FastAPI 用 ``wrapper.__globals__`` 评估字符串注解会 NameError。这里用
    ``func`` 自身的模块命名空间逐个解析参数注解（不碰 return 注解，避免引用仅定义在后面的
    名称），解析失败回退原字符串（与 FastAPI 未解析前一致，不影响既有行为）。
    """
    sig = inspect.signature(func)
    globalns = getattr(func, "__globals__", {})
    new_params = []
    for name, param in sig.parameters.items():
        ann = param.annotation
        if isinstance(ann, str):
            try:
                # 解析参数注解为真实对象：作用域限定为被装饰函数所在模块的 globals，
                # 与 FastAPI 自身 evaluate_forwardref 用的命名空间一致，无注入面。
                ann = eval(ann, globalns, globalns)
            except Exception:
                ann = param.annotation
        new_params.append(param.replace(annotation=ann))
    return sig.replace(parameters=new_params)


def audit_action(
    action: str,
    resource: str,
    resource_id: str | Callable[[Any], Any] | None = None,
    tenant_id_arg: str | None = None,
    tenant_id_resolver: Callable[[Any], Any] | None = None,
    fixed_tenant_id: int | None = None,
    owner_is_actor: bool = False,
    *,
    params_exclude_unset: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """路由层审计装饰器：自动记录写操作的成功/失败，与业务事务隔离。

    :param action: 操作（``create | update | delete | ...``，对齐 RBAC 词表）。
    :param resource: 资源类型（对齐权限 resource 串，如 ``user`` / ``tenant`` / ``api_key``）。
    :param resource_id: 资源 ID 三来源——callable(返回值) / 参数名(str) / None(兜底 ``Result.data.id``)。
    :param tenant_id_arg: 数据归属租户来自哪个参数名（admin 代操作某租户资源）。
    :param tenant_id_resolver: callable(返回值) → tenant_id（创建租户等）。
    :param fixed_tenant_id: 数据归属租户恒为该值——端点操作的是**自身带租户归属**的固定
       一类资源时用（如平台用户/角色管理恒归平台租户 ``PLATFORM_TENANT_ID``）。与
       ``tenant_id_arg`` / ``tenant_id_resolver`` 互斥（装饰期 fail-fast）；相比 resolver
       的好处是**失败路径也成立**（resolver 拿不到返回值，失败行会退化成无归属）。
        平台级资源（集群/节点/模型/路由等本就无租户列）不要给值，留空即记 NULL。
    :param owner_is_actor: 归属取**操作者本人所属租户**（含平台租户 0）——端点操作的就是
       操作者自己的数据时用（自助改菜单开关）。与自助改密同口径；同样与其余来源互斥。
    :param params_exclude_unset: 请求体参数快照用 ``exclude_unset`` 而非 ``exclude_none``——
       三态更新端点（显式 ``null``=清空/关闭，如关维护开关）开启，让安全相关的「关闭」
       动作留痕；默认 ``False`` 维持既有行为。

    用法（装饰器须在 ``@router.post`` 之下、紧贴 handler）：::

        @router.post("/{user_id}/delete", ..., dependencies=[Depends(require_permission(...))])
        @audit_action(action="delete", resource="user", resource_id="user_id")
        async def delete_user(user_id: int, user: SessionData = Depends(get_current_user), db=Depends(get_db)):
            ...

    要点：

    - handler 必须声明 ``user: SessionData`` 参数（actor 来源），缺失在装饰期即抛 ``TypeError``
      绝不静默漏记；``get_current_user`` 每请求已缓存，补该参数零额外 IO。
    - 成功路径：handler 已自行 commit 业务 → 独立 session 写 ``result='success'`` 行。
    - 失败路径：先 ``db.rollback()`` 业务 session → 独立 session 写 ``result='failed'`` + ``detail.error`` → 原样 re-raise。
    - ``functools.wraps`` + ``__signature__`` 解析保留原签名，FastAPI 依赖注入不受影响。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _AUDIT_WRITE_VOCAB.add((action, resource))
        sig = inspect.signature(func)
        if "user" not in sig.parameters:
            raise TypeError(
                f"@audit_action(action={action!r}, resource={resource!r}) 要求 handler 声明 "
                f"``user: SessionData`` 参数（审计 actor 来源）：{func.__qualname__}"
            )
        # 归属来源互斥：多给一个就静默按优先级取错来源，故装饰期即抛
        sources = [
            name
            for name, given in (
                ("fixed_tenant_id", fixed_tenant_id is not None),
                ("owner_is_actor", owner_is_actor),
                ("tenant_id_arg", tenant_id_arg is not None),
                ("tenant_id_resolver", tenant_id_resolver is not None),
            )
            if given
        ]
        if len(sources) > 1:
            raise TypeError(
                f"@audit_action(action={action!r}, resource={resource!r}) 归属来源只能选一种，"
                f"同时给了 {sources}：{func.__qualname__}"
            )

        async def _record(
            user: Any, kwargs: dict[str, Any], result: Any, *, failed: bool, exc: Exception | None
        ) -> None:
            if user is None:
                # 装饰期已校验签名含 user；此处仅防御直接以非 FastAPI 方式调用时缺失
                log.error("审计跳过：handler 缺 user 参数 %s", func.__qualname__)
                return
            detail: dict[str, Any] = {
                "params": sanitize_params(_collect_params(kwargs, exclude_unset=params_exclude_unset))
            }
            if failed and exc is not None:
                detail["error"] = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            await _write_audit(
                _build_audit_entry(
                    actor_id=user.user_id,
                    actor_name=user.username,
                    tenant_id=_resolve_tenant_id(
                        tenant_id_arg, tenant_id_resolver, kwargs, user, result, fixed_tenant_id, owner_is_actor
                    ),
                    action=action,
                    resource_type=resource,
                    resource_id=_resolve_resource_id(resource_id, kwargs, result),
                    result="failed" if failed else "success",
                    detail=detail,
                    ip_address=get_audit_ip(),
                )
            )

        @functools_wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            user = kwargs.get("user")
            db = kwargs.get("db")
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                if isinstance(db, AsyncSession):
                    try:
                        await db.rollback()
                    except Exception:
                        log.exception("审计路径回滚业务 session 失败 action=%s", action)
                await _record(user, kwargs, None, failed=True, exc=exc)
                raise
            await _record(user, kwargs, result, failed=False, exc=None)
            return result

        # functools.wraps 后显式挂载解析过的签名，供 FastAPI 正确识别参数注解与依赖
        wrapper.__signature__ = _resolved_signature(func)
        return wrapper

    return decorator


# ---- 展示文案（中文）：资源/操作代码 → 审计列表页文案 ----
# 词表是开放的：@audit_action 各路由串之外，「权限拒绝」落账（auth/deps）会写入
# RBAC 注册表里的全量 resource/action 串，未命中的 key 一律回退英文原串。
# 出参翻译在 webserver 的 AuditLogOut/AuditLogTenantOut（field_validator）做；
# 筛选入参与 DB 存储仍是英文代码，本表只管展示。
# 写路径词表登记：装饰器应用时（路由 import 期）记录 (action, resource)，漂移守卫测试
# 据此全量断言 ⊆ 标签表——新增审计端点忘补词表时 CI 直接红（运行时未命中回退英文，不炸）。
_AUDIT_WRITE_VOCAB: set[tuple[str, str]] = set()


_RESOURCE_TYPE_LABELS: dict[str, str] = {
    "node": "节点",
    "cluster": "集群",
    "model": "模型",
    "model_deployment": "部署",
    "endpoint": "端点",
    "publish": "发布",
    "user": "用户",
    "tenant": "租户",
    "access": "角色权限",
    "api_key": "API Key",
    "wallet": "钱包",
    "settings": "全局设置",
    "system": "系统配置",
    "menus": "菜单偏好",
    "audit_log": "审计日志",
    "dashboard": "数据看板",
    "gpu": "GPU",
    "inference_debug": "在线调试",
    "inference_image": "推理镜像",
    "request_log": "调用日志",
    "upload": "切片上传",
}

_ACTION_LABELS: dict[str, str] = {
    "create": "创建",
    "update": "更新",
    "delete": "删除",
    "login": "登录",
    "logout": "登出",
    "upgrade": "升级",
    "upgrade_batch": "批量升级",
    "bootstrap": "节点引导",
    "probe": "探测",
    "execute": "执行",
    # 以下只会出现在「权限拒绝」行（RBAC action 串）
    "read": "查看",
    "write": "写入",
    "exec": "执行",
}


def resource_type_label(value: str | None) -> str | None:
    """资源类型代码 → 中文标签；None 透传 None；未命中回退英文原串。"""
    if value is None:
        return None
    return _RESOURCE_TYPE_LABELS.get(value, value)


def action_label(value: str) -> str:
    """操作代码 → 中文标签；未命中回退英文原串。"""
    return _ACTION_LABELS.get(value, value)


async def list_audit_logs(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    tenant_id: int | None = None,
    actor_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: str | None = None,
    since: Any = None,
    until: Any = None,
    exclude_tenant_scope: bool = False,
) -> tuple[list[AuditLog], int]:
    """审计日志分页列表（筛选：租户/操作者/动作/资源/结果/时间区间）。

    返回 ``(当页行, 总数)``。``tenant_id`` 由调用方决定作用域——admin 传查询参数，
    tenant 强制传 ``current_tenant_id``。``since``/``until`` 为 UTC aware datetime 或 None。

    ``exclude_tenant_scope=True``（平台视角的收敛开关，由 admin 路由在「不传或传 0」时
    置位）：返回整个平台域——``tenant_id IS NULL OR tenant_id = 0``，即「无租户维度的
    资源操作」（节点/集群/模型管理）与「平台租户自身的操作」（平台用户/角色/登录）
    一并可见，不再把后者拆到某个租户视图里。开关关着时 ``tenant_id`` 精确过滤——
    传 ``0`` 精确匹配平台租户行、传 ``None`` 只匹配平台级行（两者是不同的集合）；
    开关开着但给了具体租户（``N≥1``）同样精确过滤。
    **租户端不传该开关，故本租户作用域不会被放宽**。
    """
    conds = []
    # 平台域收敛只在「未指定租户」或「指定平台租户 0」时生效；给了具体租户则精确过滤
    if platform_scope_requested(tenant_id, exclude_tenant_scope=exclude_tenant_scope):
        conds.append(platform_domain_clause(AuditLog.tenant_id))
    elif tenant_id is not None:
        conds.append(AuditLog.tenant_id == tenant_id)
    if actor_id is not None:
        conds.append(AuditLog.actor_id == actor_id)
    if action is not None:
        conds.append(AuditLog.action == action)
    if resource_type is not None:
        conds.append(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        conds.append(AuditLog.resource_id == resource_id)
    if result is not None:
        conds.append(AuditLog.result == result)
    if since is not None:
        conds.append(AuditLog.created_at >= since)
    if until is not None:
        conds.append(AuditLog.created_at <= until)

    base = select(AuditLog)
    if conds:
        base = base.where(*conds)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = list(
        (await db.execute(base.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size))).scalars()
    )
    return rows, total
