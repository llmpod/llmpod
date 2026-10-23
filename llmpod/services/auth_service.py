"""认证服务：登录、令牌签发、Redis 会话存储、自助改密。"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.session import LoginResult, SessionData
from llmpod.config.settings import Settings
from llmpod.core.exceptions import AuthError, ErrorCode
from llmpod.core.security import (
    create_token,
    hash_password,
    hash_token,
    verify_password,
)
from llmpod.db import get_redis
from llmpod.models.user import User
from llmpod.models.role import Role
from llmpod.models.user_role import UserRole

log = logging.getLogger("llmpod.services.auth")

# 时序抹平用的哑哈希：懒加载算一次（bcrypt 百毫秒级，不值得在导入期付）。
# 「用户不存在」路径跑一次等价 bcrypt 再抛同款错误，消除与「密码错」的
# 响应时差，堵用户名枚举侧信道。
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("llmpod-login-timing-pad")
    return _DUMMY_HASH


async def authenticate(
    db: AsyncSession,
    username: str,
    password: str,
    settings: Settings,
) -> LoginResult:
    """通过用户名+密码认证用户，创建不透明令牌和 Redis 会话。

    返回 ``(token, session: SessionData)``——会话形状由 ``SessionData`` 唯一定义，
    写入侧 ``model_dump_json`` 与读回侧 ``model_validate_json`` 同源。
    """
    # 查找用户（username 全局唯一，纯 username+password 即可鉴别，无需租户标识）
    from llmpod.apps.webserver.constants import AccountStatus

    stmt = select(User).where(User.username == username, User.status == AccountStatus.ACTIVE).with_for_update()
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        # 用户不存在：subject 保持 None（登录失败审计据此跳过，防匿名刷 audit_log）；
        # 先跑一次哑 bcrypt 抹平时序（见 _dummy_hash 注释）再抛同款错误
        await asyncio.to_thread(verify_password, password, _dummy_hash())
        raise AuthError("用户名或密码错误", error_code=ErrorCode.INVALID_CREDENTIALS)

    # 验证密码（对外错误与「用户不存在」完全一致，防用户名枚举；
    # subject 带上已命中的注册用户，供失败审计记录真实主体）
    if not await asyncio.to_thread(verify_password, password, user.password_hash):
        raise AuthError(
            "用户名或密码错误",
            error_code=ErrorCode.INVALID_CREDENTIALS,
            subject=user,
        )

    # 获取用户的所有角色（多对多，经 user_role 关联表）
    role_stmt = select(Role).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
    role_result = await db.execute(role_stmt)
    roles = list(role_result.scalars())
    if not roles:
        raise AuthError("用户未分配角色", error_code=ErrorCode.INVALID_CREDENTIALS, subject=user)

    role_ids = [r.id for r in roles]
    # 自定义角色 code 可空（仅内置角色有 code）
    role_codes = [r.code for r in roles if r.code]

    # 根据角色集合确定访问域（platform / tenant）
    access_domain = _determine_access_domain(roles)
    # 超管判定：角色须同时满足 tenant_id==PLATFORM_TENANT_ID（平台域）且 code 命中
    from llmpod.config.constants import PLATFORM_TENANT_ID, SUPER_ADMIN_ROLE_CODES

    is_superuser = any(r.tenant_id == PLATFORM_TENANT_ID and r.code in SUPER_ADMIN_ROLE_CODES for r in roles)

    # 生成不透明令牌（令牌本身不携带任何信息，会话存储在 Redis 中，
    # 以 hash_token(token) 为键，过期由 Redis TTL 管理）
    token = create_token()

    # 快照当前会话版本号：此后禁用/改密/角色变更会 bump 版本，旧会话即刻失效
    session_ver = user.auth_version

    # 在 Redis 中存储会话（SessionData 是形状契约，读回侧 get_current_user 同源校验）
    session = SessionData(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role_ids=role_ids,
        role_codes=role_codes,
        access_domain=access_domain,
        is_superuser=is_superuser,
        username=user.username,
        session_ver=session_ver,
    )
    redis = get_redis()
    token_hash = hash_token(token)
    await redis.setex(
        f"{settings.token_key_prefix}{token_hash}",
        settings.token_expire_seconds,
        session.model_dump_json(),
    )

    # 更新最后登录时间
    from llmpod.utils.time import now_utc

    user.last_login_at = now_utc()
    await db.flush()

    log.info("User %s (id=%d) logged in, access_domain=%s", username, user.id, access_domain)
    return LoginResult(token=token, session=session)


async def logout(token: str, settings: Settings):
    """从 Redis 中移除令牌会话。"""
    redis = get_redis()
    token_hash = hash_token(token)
    await redis.delete(f"{settings.token_key_prefix}{token_hash}")


async def change_password(
    db: AsyncSession,
    *,
    user_id: int,
    old_password: str,
    new_password: str,
) -> None:
    """自助改密核心：定位当前用户 → 验旧密 → 落新哈希（bcrypt 移出事件循环）。

    只按 ``user_id`` 定位（不收外部用户标识，杜绝越权改他人）；旧密不匹配抛
    ``AuthError(INVALID_CREDENTIALS)``。会话撤销与审计留给路由层——撤销需要原始
    token，审计需要请求上下文（与 login 的分层一致）。
    """
    row = await db.get(User, user_id)
    if row is None:
        raise AuthError("用户不存在")

    # bcrypt 是百毫秒级 CPU 操作，放线程池避免卡住整个 webserver 事件循环
    ok = await asyncio.to_thread(verify_password, old_password, row.password_hash)
    if not ok:
        raise AuthError("旧密码不正确", error_code=ErrorCode.INVALID_CREDENTIALS)

    row.password_hash = await asyncio.to_thread(hash_password, new_password)
    from llmpod.services import session_version_service

    await session_version_service.bump_version(user_id, db)
    await db.commit()


def _determine_access_domain(roles: list[Role]) -> str:
    """根据角色归属判定访问域。

    tenant_id=0 的角色为平台域（super_admin/operator），其余为租户域。
    """
    from llmpod.apps.webserver.constants import AccessDomain
    from llmpod.config.constants import PLATFORM_TENANT_ID

    if any(r.tenant_id == PLATFORM_TENANT_ID for r in roles):
        return AccessDomain.PLATFORM
    return AccessDomain.TENANT
