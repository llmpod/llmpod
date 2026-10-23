"""权限种子与一致性检查。

``seed_permissions(db)`` 幂等 upsert 全部已声明权限（``llmpod.permission.platform`` /
``llmpod.permission.tenant`` 常量在导入时自登记进注册表），并按全局
``BUILTIN_ROLE_PERMISSIONS`` 给 tenant_id=0 的平台内置角色授权。

``seed_tenant_builtin_roles(db, tenant_id)`` 在指定租户下 upsert 内置租户角色
（tenant_admin/tenant_operator）并按租户域映射授权，供租户开通时调用。

``check_consistency()`` 校验内置角色映射引用的权限码是否都已登记，在 webserver 启动时
调用，把"权限漂移"从运行时 403 提前到启动期告警。
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.permission._common import SCOPE_PLATFORM, SCOPE_TENANT
from llmpod.permission.registry import registered_permissions
from llmpod.services.permission_service import get_role_permission_ids_map

log = logging.getLogger("llmpod.permission.seed")

# 租户开通时自动创建的内置租户角色：(code, name)。
# code 与 llmpod.permission.tenant.ROLE_PERMISSIONS 的 key 对应。
TENANT_BUILTIN_ROLES: list[tuple[str, str]] = [
    ("tenant_admin", "租户管理员"),
    ("tenant_operator", "租户运营"),
]


async def _perm_id_map(db: AsyncSession) -> dict[str, int]:
    """构建 ``permission.code -> id`` 索引（供 seed 按权限串反查 id，免逐条查）。"""
    stmt = select(Permission.id, Permission.scope, Permission.resource, Permission.action)
    return {f"{scope}:{resource}:{action}": pid for pid, scope, resource, action in (await db.execute(stmt)).all()}


async def _grant_role_permissions(
    db: AsyncSession,
    roles: list[Role],
    role_perms: dict[str, list],
    perm_id_map: dict[str, int],
    *,
    scope_label: str,
) -> None:
    """给一组角色按 ``code -> [权限]`` 映射幂等授权（只增不撤）。

    平台内置角色（seed_permissions）与租户内置角色（存量补授权 / 开通授权）
    三处共用；已有授权一条 IN 查询批量取回。权限未注册时告警跳过
    （consistency 检查兜底，避免脏 FK）。
    """
    existing_map = await get_role_permission_ids_map(db, [r.id for r in roles])
    for role in roles:
        existing_ids = set(existing_map.get(role.id, ()))
        for perm in role_perms.get(role.code, []):
            pid = perm_id_map.get(perm.code)
            if pid is None:
                log.warning(
                    "seed: 权限 %s 未注册，无法授权给 %s（%s）",
                    perm.code,
                    role.code,
                    scope_label,
                )
                continue
            if pid in existing_ids:
                continue
            db.add(RolePermission(role_id=role.id, permission_id=pid))
            existing_ids.add(pid)


async def seed_permissions(db: AsyncSession) -> None:
    """幂等 upsert 已声明权限并应用内置角色授权。

    1. 导入 permissions 模块触发常量自登记（已由 webserver 路由间接导入，此处显式保证）；
    2. upsert 全部权限（缺失则建，已存在但缺 description 则补全，多余的不删）；
    3. 按 ``BUILTIN_ROLE_PERMISSIONS`` 给内置角色授权（幂等插 RolePermission）。
    """
    import llmpod.permission.permissions  # noqa: F401 — 确保权限常量已自登记进注册表

    # 1. upsert 已登记权限
    for spec in registered_permissions():
        stmt = select(Permission).where(
            Permission.scope == spec.scope,
            Permission.resource == spec.resource,
            Permission.action == spec.action,
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            db.add(
                Permission(
                    scope=spec.scope,
                    resource=spec.resource,
                    action=spec.action,
                    description=spec.description,
                )
            )
        elif not existing.description and spec.description:
            existing.description = spec.description
    await db.flush()

    # 清理系统已不存在的孤儿权限行升级在库时移除历史等已废弃权限
    registered_keys = {(s.scope, s.resource, s.action) for s in registered_permissions()}
    if registered_keys:
        referenced_ids = {pid for (pid,) in (await db.execute(select(RolePermission.permission_id).distinct())).all()}
        orphan_ids = [
            p.id
            for p in (await db.execute(select(Permission))).scalars().all()
            if (p.scope, p.resource, p.action) not in registered_keys and p.id not in referenced_ids
        ]
        if orphan_ids:
            await db.execute(delete(Permission).where(Permission.id.in_(orphan_ids)))
            log.info("seed_permissions: 清理 %d 个已废弃的孤儿权限行", len(orphan_ids))
    else:
        log.warning("seed_permissions: 无已登记权限，跳过孤儿权限清理以免误删")

    # 2. 内置角色授权（平台半边，tenant_id=0）
    from llmpod.permission.permissions import BUILTIN_ROLE_PERMISSIONS

    perm_id_map = await _perm_id_map(db)

    from llmpod.config.constants import PLATFORM_TENANT_ID

    role_by_code: dict[str, Role] = {
        r.code: r for r in (await db.execute(select(Role).where(Role.tenant_id == PLATFORM_TENANT_ID))).scalars()
    }
    missing_roles = set(BUILTIN_ROLE_PERMISSIONS) - set(role_by_code)
    for role_code in sorted(missing_roles):
        log.warning("seed_permissions: 内置角色 %s 未找到（tenant_id=0），跳过", role_code)
    await _grant_role_permissions(
        db,
        [role_by_code[c] for c in BUILTIN_ROLE_PERMISSIONS if c in role_by_code],
        BUILTIN_ROLE_PERMISSIONS,
        perm_id_map,
        scope_label="平台内置角色",
    )

    # 2b. 存量租户的内置角色补授权（租户域半边）。否则租户域新增权限只对
    # 「升级后新建的租户」生效（建租户走 seed_tenant_builtin_roles），存量租户的
    # tenant_admin/tenant_operator 全员 403。幂等只增不撤，与平台半边同语义。
    from llmpod.permission.tenant import ROLE_PERMISSIONS as TENANT_ROLE_PERMISSIONS

    tenant_builtin_roles = list(
        (
            await db.execute(
                select(Role).where(
                    Role.tenant_id != PLATFORM_TENANT_ID,
                    Role.is_builtin.is_(True),
                    Role.code.in_(set(TENANT_ROLE_PERMISSIONS)),
                )
            )
        ).scalars()
    )
    await _grant_role_permissions(
        db,
        tenant_builtin_roles,
        TENANT_ROLE_PERMISSIONS,
        perm_id_map,
        scope_label="存量租户内置角色",
    )
    await db.flush()


async def seed_tenant_builtin_roles(db: AsyncSession, tenant_id: int) -> None:
    """在指定租户下 upsert 内置租户角色并按租户域映射授权。

    租户开通时由 ``POST /admin/tenants`` 调用。对 ``TENANT_BUILTIN_ROLES`` 列出的每个
    role code：若该租户下无此 code 角色则创建（is_builtin=True），并按
    ``llmpod.permission.tenant.ROLE_PERMISSIONS`` 幂等授权。重复调用安全。

    与 ``seed_permissions`` 区别：后者只处理 tenant_id=0 的平台内置角色；本函数处理
    具体租户下的角色，故按 ``(tenant_id, code)`` 而非仅 code 定位角色。
    """
    from llmpod.permission.tenant import ROLE_PERMISSIONS as TENANT_ROLE_PERMISSIONS

    # 权限 code → id 索引（租户域权限在 seed_permissions 阶段已 upsert 进 DB）
    perm_id_map = await _perm_id_map(db)

    # 该租户下已有角色 code 索引；缺失的内置角色随建随入（下方授权要引用）
    existing_roles = {r.code: r for r in (await db.execute(select(Role).where(Role.tenant_id == tenant_id))).scalars()}

    for code, name in TENANT_BUILTIN_ROLES:
        role = existing_roles.get(code)
        if role is None:
            role = Role(
                tenant_id=tenant_id,
                name=name,
                code=code,
                is_builtin=True,
            )
            db.add(role)
            await db.flush()
            existing_roles[code] = role

    await _grant_role_permissions(
        db,
        [existing_roles[c] for c, _ in TENANT_BUILTIN_ROLES],
        TENANT_ROLE_PERMISSIONS,
        perm_id_map,
        scope_label=f"租户 {tenant_id} 内置角色",
    )
    await db.flush()


def check_consistency() -> list[str]:
    """校验声明处引用的权限码是否全部已登记。

    覆盖两类引用面：内置角色映射与菜单声明的 ``required``。返回告警文案列表
    （空表示一致）。权限常量在模块导入时即登记，故本函数可在 webserver 路由
    导入后任意时刻调用。
    """
    from llmpod.permission.menu import menus_by_scope
    from llmpod.permission.permissions import BUILTIN_ROLE_PERMISSIONS

    registered_codes = {p.code for p in registered_permissions()}
    warnings: list[str] = []
    for role_code, perms in BUILTIN_ROLE_PERMISSIONS.items():
        for perm in perms:
            if perm.code not in registered_codes:
                warnings.append(f"角色 {role_code} 引用了未注册的权限 {perm.code}")
    for spec in menus_by_scope(SCOPE_PLATFORM) + menus_by_scope(SCOPE_TENANT):
        for perm in spec.required:
            if perm.code not in registered_codes:
                warnings.append(f"菜单 {spec.path} 引用了未注册的权限 {perm.code}")
    return warnings
