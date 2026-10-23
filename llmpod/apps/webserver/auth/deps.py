"""FastAPI 依赖注入：当前用户、数据库会话、Redis。"""

from __future__ import annotations

import logging

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.app import get_settings
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.constants import AccessDomain
from llmpod.core.exceptions import AuthError, ErrorCode, PermissionDenied
from llmpod.core.security import hash_token
from llmpod.db import get_db, get_redis
from llmpod.permission.registry import PermSpec
from llmpod.services.audit_service import get_audit_ip, set_audit_ip, tenant_id_for_user, write_audit
from llmpod.services.permission_service import user_has_permission
from llmpod.models.user import User

log = logging.getLogger("llmpod.apps.webserver.auth.deps")

# Swagger UI 鉴权方案：声明后，任何依赖 get_current_user 的路由都会
# 在 OpenAPI 中带上 Bearer 安全要求，并在 /docs 页面显示 "Authorize" 按钮。
bearer_scheme = HTTPBearer(auto_error=False)


def get_client_ip(request: Request) -> str | None:
    """从请求取来源 IP（``request.client`` 可能为 None，如测试直调）；取不到为 None。"""
    client = request.client
    return client.host if client else None


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> SessionData:
    """从 Authorization 请求头中提取不透明令牌，并从 Redis 返回会话信息。

    令牌本身不携带任何用户信息。所有会话数据存储在 Redis 中，以
    hash_token(token) 为键；令牌是否有效/过期完全由 Redis 中是否
    存在该键以及 TTL 决定。
    """
    # 写入审计来源 IP（本依赖与 handler 同任务，contextvar 对装饰器/权限拒绝落账可见）
    set_audit_ip(get_client_ip(request))

    # 优先使用 HTTPBearer 解析结果，兼容旧的手动解析逻辑
    token = credentials.credentials if credentials else _extract_token(request)
    if not token:
        raise AuthError("缺少或无效的认证令牌")

    redis = get_redis()
    token_hash = hash_token(token)
    settings = get_settings()
    session_json = await redis.get(f"{settings.token_key_prefix}{token_hash}")
    if session_json is None:
        # 会话过期/令牌失效：前端据此弹"登录已过期"提示后跳登录（区别于"未带 token"）
        raise AuthError("令牌无效或已过期", error_code=ErrorCode.TOKEN_EXPIRED)

    try:
        session = SessionData.model_validate_json(session_json)
    except ValidationError:
        # 损坏/不合约的旧会话：按令牌过期处理，强制重登录
        raise AuthError("令牌无效或已过期", error_code=ErrorCode.TOKEN_EXPIRED)

    # 用户状态和版本均来自同一个数据库提交；旧 Redis 会话缺版本时强制重登。
    row = await db.get(User, session.user_id)
    if row is None or row.status != "active" or row.auth_version != session.session_ver:
        raise AuthError("令牌无效或已过期", error_code=ErrorCode.TOKEN_EXPIRED)
    return session


async def require_platform_role(
    user: SessionData = Depends(get_current_user),
) -> SessionData:
    """要求平台访问域（access_domain=platform）。"""
    if user.access_domain != AccessDomain.PLATFORM:
        raise PermissionDenied("需要平台域账号")
    return user


async def require_tenant_access(
    user: SessionData = Depends(get_current_user),
) -> SessionData:
    """要求租户访问域（access_domain=tenant 或 tenant_observer）。"""
    if user.access_domain not in (AccessDomain.TENANT, AccessDomain.TENANT_OBSERVER):
        raise PermissionDenied("需要租户域账号")
    return user


async def current_tenant_id(user: SessionData = Depends(get_current_user)) -> int:
    """返回当前会话强制绑定的租户 ID（租户路由数据作用域的唯一可信来源）。
    避免handler手写过滤、漏写导致跨租户数据泄漏。
    """
    return user.tenant_id


async def require_authenticated(
    user: SessionData = Depends(get_current_user),
) -> SessionData:
    """仅要求登录，不限定 access_domain。

    适用于平台管理员与租户都可调用的共享端点（如 /me、通知、个人设置）。
    若需要进一步区分行为，handler 内可通过 user.access_domain 判断。
    """
    return user


async def require_platform_or_tenant_access(
    user: SessionData = Depends(get_current_user),
) -> SessionData:
    """要求平台或租户访问域。

    用于"管理员可管理 / 租户可查看自己"这类双通端点。
    需要更细粒度时，handler 内用 user.access_domain / user.tenant_id 再分流。
    """
    if user.access_domain not in (AccessDomain.PLATFORM, AccessDomain.TENANT):
        raise PermissionDenied("需要平台域或租户域账号")
    return user


# ===== 细粒度权限（操作级 scope:resource:action） =====
#
# 用法：
#   1) router 外层：APIRouter(dependencies=[Depends(require_permission(ACCESS_READ))])
#   2) endpoint 细度：@router.get(..., dependencies=[Depends(require_permission(CLUSTER_READ))])
#
# 权限常量在 llmpod/permission/permissions.py 集中声明并自登记进注册表，
# 路由直接引用常量（typo = ImportError，编译期可查），无需手写字符串码。
#
# 分层：require_platform_role / require_tenant_access 外层门禁先挡住 access_domain，
# 到此处已过外层门禁，再校验用户是否持有具体 scope:resource:action。super_admin 直接短路放行。


def require_permission(perm: PermSpec, audit: bool = True):
    """构造一个校验 ``scope:resource:action`` 的依赖。

    参数为 ``PermSpec`` 常量（见 :mod:`llmpod.permission.permissions`），typo 会在
    导入期以 ``ImportError`` 暴露。返回可调用依赖：取当前用户会话，超管短路；否则
    查 DB 校验。失败抛 :class:`PermissionDenied`，由全局异常处理器映射为 403。

    权限声明集中在 ``permissions.py`` 的常量定义处（导入即登记），本工厂不再承担
    副作用登记职责。

    ``audit=True``（默认）时，权限拒绝在抛 ``PermissionDenied`` 之前落一条审计
    （``result='failed'`` + ``detail.error='permission_denied: <code>'``）——越权尝试
    本身是安全审计线索。落账失败只 ``log.error``，绝不影响 403 响应。
    """

    async def _checker(
        user: SessionData = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> SessionData:
        if user.is_superuser:
            return user
        if not await user_has_permission(db, user.user_id, perm.scope, perm.resource, perm.action):
            if audit:
                await write_audit(
                    action=perm.action,
                    actor_id=user.user_id,
                    actor_name=user.username,
                    tenant_id=tenant_id_for_user(user),
                    resource_type=perm.resource,
                    result="failed",
                    detail={"error": f"permission_denied: {perm.code}"},
                    ip_address=get_audit_ip(),
                )
            raise PermissionDenied(
                f"权限不足: {perm.code}",
                scope=perm.scope,
                resource=perm.resource,
                action=perm.action,
            )
        return user

    _checker.__name__ = f"require_permission_{perm.scope}_{perm.resource}_{perm.action}"
    return _checker


def _extract_token(request: Request) -> str | None:
    """从 Authorization 请求头中提取 Bearer 令牌。"""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()
