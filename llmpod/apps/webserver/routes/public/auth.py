"""认证路由：登录、登出、获取当前用户、菜单、自助改密。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.app import get_settings
from llmpod.apps.webserver.auth.deps import _extract_token, get_client_ip, get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.constants import ACCESS_DOMAIN_MENU_SCOPE
from llmpod.apps.webserver.schemas import Result, success
from llmpod.core.exceptions import AuthError, ErrorCode, Maintenance
from llmpod.db import get_db
from llmpod.permission import SCOPE_PLATFORM
from llmpod.services.settings_service import get_maintenance
from llmpod.services.first_guide_service import open_first_guide
from llmpod.services.audit_service import audit_action, get_audit_ip, write_audit
from llmpod.services import auth_service
from llmpod.services.auth_service import authenticate, logout
from llmpod.services.menu_service import set_user_disabled_menus, user_menus_key, visible_menus_for

log = logging.getLogger("llmpod.apps.webserver.routes.public.auth")

router = APIRouter()


class LogoutData(BaseModel):
    """登出结果。"""

    logged_out: bool


class PasswordChangedData(BaseModel):
    """改密结果（成功即要求前端跳登录）。"""

    changed: bool


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="登录密码")


class ChangePasswordRequest(BaseModel):
    """自助改密请求（用户中心）：只作用于当前会话用户，不收 user_id。"""

    old_password: str = Field(..., min_length=1, description="当前密码（校验通过才允许修改）")
    new_password: str = Field(..., min_length=1, max_length=128, description="新密码（明文传入，服务端哈希存储）")


class LoginData(BaseModel):
    token: str = Field(..., description="访问令牌（后续请求放入 Authorization: Bearer <token>）")
    user_id: int = Field(..., description="用户ID")
    username: str = Field(..., description="用户名")
    role_codes: list[str] = Field(default_factory=list, description="用户拥有的角色编码")
    access_domain: str = Field(..., description="访问域：platform=平台/管理端 | tenant=租户端")
    is_superuser: bool = Field(..., description="是否为超级管理员")


class UserInfoData(SessionData):
    """/me 回执——继承 SessionData（会话载体即用户可见信息，字段唯一定义处）。"""


@router.post("/login", response_model=Result[LoginData], summary="用户登录")
async def login(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db),
    http_request: Request = None,
):
    """用户登录：验证凭据，签发不透明令牌并将会话写入 Redis。"""
    settings = get_settings()
    ip = get_client_ip(http_request)
    # 暴力破解限速：失败计数超限直接拒（不进 bcrypt）；Redis 不可用时放行
    from llmpod.services import login_throttle_service

    try:
        permit = await login_throttle_service.admit(request.username, ip, settings)
    except login_throttle_service.LoginThrottled:
        raise AuthError("登录失败次数过多，请稍后再试", error_code=ErrorCode.INVALID_CREDENTIALS)
    try:
        token, session = await authenticate(db, request.username, request.password, settings)
    except AuthError as exc:
        # 登录失败审计只记**注册用户**的失败尝试（尝试性操作是安全线索）：
        # authenticate 对用户不存在的场景 subject=None，匿名攻击者拿任意用户名
        # 狂试不会刷 audit_log；密码错的场景 subject=命中的注册用户，记真实
        # actor_id/actor_name/tenant_id，便于定位「谁在反复输错密码」。
        # subject 不进对外响应，两种场景的错误码/message 一致（防用户名枚举）。
        # 不存任何密码相关内容。
        await login_throttle_service.record_failure(request.username, ip, settings)
        subject = getattr(exc, "subject", None)
        if subject is not None:
            await write_audit(
                action="login",
                actor_id=subject.id,
                actor_name=subject.username,
                tenant_id=subject.tenant_id,
                result="failed",
                detail={"error": exc.error_code.value},
                ip_address=ip,
            )
        raise
    finally:
        await permit.release()
    # 维护模式拦截：authenticate 已在 Redis 写入会话，此刻才判定——平台域/超管放行
    # （管理员必须能进来关开关），其余域拒绝：先撤销刚签发的会话再 raise，否则每次
    # 被拒登录都会在 Redis 留下一个客户端永远拿不到但完全有效的孤儿会话。
    maintenance = await get_maintenance(db)
    if maintenance.enabled and not session.exempt_from_maintenance:
        await logout(token, settings)
        await write_audit(
            action="login",
            actor_id=session.user_id,
            actor_name=session.username,
            tenant_id=session.tenant_id,
            result="failed",
            detail={"error": "MAINTENANCE"},
            ip_address=ip,
        )
        raise Maintenance(maintenance.message)
    # 首次引导弹窗触发：仅平台超管的成功登录参与（防抢先触发），与
    # last_login_at 同事务落库（SAVEPOINT 隔离失败）；关闭不在登录侧
    # （见 frontend 的管理端消耗）。
    if session.is_superuser:
        await open_first_guide(db, user_id=session.user_id)
    await db.commit()  # 持久化 authenticate 内更新的 last_login_at 与引导记账
    await login_throttle_service.clear(request.username, ip)
    await write_audit(
        action="login",
        actor_id=session.user_id,
        actor_name=session.username,
        tenant_id=session.tenant_id,
        result="success",
        ip_address=ip,
    )
    return success(
        LoginData(
            token=token,
            user_id=session.user_id,
            username=session.username,
            role_codes=session.role_codes,
            access_domain=session.access_domain,
            is_superuser=session.is_superuser,
        )
    )


@router.post("/logout", response_model=Result[LogoutData], summary="用户登出")
async def logout_endpoint(
    request: Request,
    user: SessionData = Depends(get_current_user),
):
    """用户登出：从 Redis 中删除令牌会话，使令牌立即失效。"""
    await _revoke_current_token(request)
    await write_audit(
        action="logout",
        actor_id=user.user_id,
        actor_name=user.username,
        tenant_id=user.tenant_id,
        result="success",
        ip_address=get_audit_ip(),  # get_current_user 已写入 contextvar，此处直接读取
    )
    return success(LogoutData(logged_out=True))


@router.post("/change-password", response_model=Result[PasswordChangedData], summary="修改当前用户密码")
async def change_password(
    request: ChangePasswordRequest,
    http_request: Request,
    user: SessionData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户中心自助改密：验旧密码 → 落新哈希 → 撤销当前令牌（前端收到成功后跳登录）。

    越权面收死：核心逻辑按 ``session.user_id`` 定位（见 auth_service.change_password），
    不收外部用户标识；管理员重置他人密码仍走 ``/admin/users/{id}/update``（不验旧密）。
    改密同时 bump 会话版本：同用户其他端（其他浏览器/被盗窃用 token）的存量
    会话一并失效，不必等自然过期。
    """
    try:
        await auth_service.change_password(
            db,
            user_id=user.user_id,
            old_password=request.old_password,
            new_password=request.new_password,
        )
    except AuthError as e:
        # 记真实失败原因（旧密码错 / 用户已不存在等），勿硬编码误导排查
        await write_audit(**_change_password_audit(user, result="failed", detail={"error": str(e)}))
        raise
    await _revoke_current_token(http_request)
    await write_audit(**_change_password_audit(user, result="success", detail={"action": "change_password"}))
    return success(PasswordChangedData(changed=True))


def _change_password_audit(user: SessionData, *, result: str, detail: dict) -> dict:
    """自助改密审计公共字段（成功/失败两分支共用）。"""
    return dict(
        action="update",
        actor_id=user.user_id,
        actor_name=user.username,
        tenant_id=user.tenant_id,
        resource_type="user",
        resource_id=str(user.user_id),
        result=result,
        detail=detail,
        ip_address=get_audit_ip(),
    )


async def _revoke_current_token(http_request: Request) -> None:
    """撤销当前请求携带的令牌（logout 与自助改密共用）。"""
    settings = get_settings()
    token = _extract_token(http_request)
    if token:
        await logout(token, settings)


@router.get("/me", response_model=Result[UserInfoData], summary="获取当前用户")
async def get_current_user_info(
    user: SessionData = Depends(get_current_user),
):
    """从 Redis 会话中获取当前用户信息（SessionData 即回显形状）。"""
    return success(user)


class MenuItemOut(BaseModel):
    """菜单条目的附加属性——目前只有开关标注。

    展示属性（title / 排序）由前端路由表自持，接口只下发「能不能看」与
    「开没开」，避免两侧菜单文案漂移。
    """

    enabled: bool = Field(
        ...,
        description="该菜单是否启用",
    )


@router.get(
    "/menus",
    response_model=Result[dict[str, MenuItemOut]],
    summary="获取当前用户可见菜单",
)
async def get_my_menus(
    user: SessionData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回当前用户可见菜单字典：key=前端路由路径，value=开关标注。

    按用户 ``access_domain`` 选域——平台账号(platform)返回 ``platform`` 菜单、
    租户账号返回 ``tenant`` 菜单，使两套 SPA 互不泄露对方菜单结构。
    过滤口径两层（见 menu_service）：非超管按菜单声明的 ``required``
    功能权限判定（OR：持任一即可见；空 = 该域全员可见），不满足的
    **不进返回**——key 的存在性即「有权看到」；开关关掉的菜单仍下发但标
    ``enabled=False``（呈现层控制）。超管短路视为全可见。
    """
    scope = ACCESS_DOMAIN_MENU_SCOPE.get(user.access_domain, SCOPE_PLATFORM)
    visible = await visible_menus_for(db, scope=scope, user_id=user.user_id, is_superuser=user.is_superuser)
    return success({path: MenuItemOut(enabled=enabled) for path, enabled in visible.items()})


class UserMenusUpdateRequest(BaseModel):
    """自助修改本人菜单开关请求体：disabled 即最终态，空列表 = 全部启用。"""

    disabled: list[str] = Field(
        default_factory=list,
        description="要隐藏的菜单路径；提交后替换当前隐藏列表",
    )


class UserMenusState(BaseModel):
    """自助修改后的本人菜单开关态回显。"""

    user_id: int = Field(..., description="用户ID（即当前会话用户）")
    disabled: list[str] = Field(..., description="当前隐藏的菜单路径")


@router.post(
    "/menus/update",
    response_model=Result[UserMenusState],
    summary="自助修改本人菜单可见性",
)
@audit_action(
    action="update",
    resource="menus",
    resource_id=lambda result: user_menus_key(result["data"]["user_id"]),
    owner_is_actor=True,  # 数据归属 = 操作者本人（与自助改密同口径）
)
async def update_my_menus(
    request: UserMenusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """全量覆盖写入本人菜单下线集，返回覆盖后回显。

    只作用于当前会话用户（不收 user_id，杜绝替他人改）；按 ``access_domain``
    选域——平台账号改 platform 菜单、租户账号改 tenant 菜单。其他域/未声明
    路径 422。开关只影响入口显示，不动本人权限（API 访问不受影响）。
    """
    scope = ACCESS_DOMAIN_MENU_SCOPE.get(user.access_domain, SCOPE_PLATFORM)
    disabled = await set_user_disabled_menus(
        db,
        user_id=user.user_id,
        scope=scope,
        paths=list(request.disabled),
        updated_by=user.user_id,
    )
    await db.commit()  # 事务边界：service 只 flush，路由层 commit（对齐全仓约定）
    return success(UserMenusState(user_id=user.user_id, disabled=sorted(disabled)))
