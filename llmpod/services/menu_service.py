"""菜单可见性服务：权限过滤 + 用户自助开关。

一个菜单下发给出条件：

1. **功能权限**——菜单声明的 ``required`` 权限（见 ``permission/menu.py``）
   用户至少持有一条（OR 语义；required 为空的无门页面对该域全员可见；
   超管短路视为全持）。这是数据边界：不满足的菜单不进返回，key 的存在性
   即「有权看到」。想不给某人看某菜单 = 不授对应的 Read 权限即可；API
   鉴权仍由 ``require_permission`` 把门，菜单只决定入口显示；
2. **用户自助开关**——每个用户只改自己的菜单入口，做呈现层控制：关掉的
   菜单仍下发但标 ``enabled=False``（隐藏还是置灰由前端定）。存储
   ``SystemConfig`` key ``menus:u:{user_id}``，只作用于该用户所属域
   （platform 账号只动 platform 菜单、租户账号只动 tenant 菜单）。

不设「管理员/租户管理员替别人改」的路径：角色对菜单的控制统一走 required
权限层，个人入口偏好归个人自助——任何人（含管理员）都只能改自己的菜单，
无从越界代改他人。

读写口径分离同 settings_service：读侧 sanitize 容忍存量脏值（版本迭代删菜单后
旧 path 静默失效），写侧严格拒绝未声明路径（防手误关了个不存在的菜单还自以为
生效）。``/auth/menus`` 是登录/刷新必经路径，读取 DB 故障按全开回落（告警日志），
不让菜单读取打死登录。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models.system_config import SystemConfig
from llmpod.permission.menu import MenuSpec, declared_menu_paths, menus_by_scope
from llmpod.services.permission_service import get_user_permissions

log = logging.getLogger("llmpod.services.menus")

# SystemConfig 存储 key：用户自助菜单开关（menus:u:{user_id}）
USER_MENUS_KEY_PREFIX = "menus:u:"


def sanitize_disabled(value: Any, scope: str | None = None) -> frozenset[str]:
    """白名单清洗开关存储值：只保留当前代码已声明的菜单路径。

    ``scope`` 给定时白名单进一步收窄到该域（平台账号只认 platform 域路径，
    租户账号只认 tenant 域路径）。非法结构（非 dict / 非 list / 非 str
    元素）与未声明路径一律丢弃——存量值可能在旧版本写入，菜单删除后开关项
    自然失效。
    """
    if not isinstance(value, dict):
        return frozenset()
    raw = value.get("disabled")
    if not isinstance(raw, list):
        return frozenset()
    declared = declared_menu_paths(scope)
    return frozenset(p for p in raw if isinstance(p, str) and p in declared)


def filter_visible_menus(
    specs: list[MenuSpec],
    disabled: frozenset[str] | set[str],
    perm_codes: set[str],
    is_superuser: bool,
) -> dict[str, bool]:
    """两层过滤的纯函数核心，返回 ``{path: enabled}``（仅含有权菜单）。

    - 非超管：``required`` 判定不满足的**不出现**（required 空 = 该域全员
      可见；持任一 required 码即可见）——key 存在性即授权结论；
    - 超管：跳过 required 判定（与 ``require_permission`` 短路口径一致），
      仅受开关标注影响；
    - 开关关掉的菜单仍返回但值 ``False``（呈现决策留给前端）。
    """
    visible: dict[str, bool] = {}
    for spec in specs:
        if not is_superuser and spec.required and not any(p.code in perm_codes for p in spec.required):
            continue
        visible[spec.path] = spec.path not in disabled
    return visible


def user_menus_key(user_id: int) -> str:
    """用户自助开关的存储 key。"""
    return f"{USER_MENUS_KEY_PREFIX}{user_id}"


async def _write_disabled_row(
    db: AsyncSession,
    *,
    key: str,
    description: str,
    paths: list[str],
    scope: str | None,
    updated_by: int,
) -> frozenset[str]:
    """开关行的全量覆盖写公共路径：白名单校验 → upsert 行（只 flush）。

    ``scope`` 给定时白名单收窄到该域声明路径；未声明路径 raise 422。
    """
    unknown = sorted(set(paths) - declared_menu_paths(scope))
    if unknown:
        raise LLMPodValidationError(f"menus.disabled 含未声明的菜单路径：{', '.join(unknown)}")
    disabled = frozenset(paths)
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one_or_none()
    value = {"disabled": sorted(disabled)}
    if row is None:
        row = SystemConfig(
            key=key,
            value=value,
            description=description,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
    await db.flush()
    return disabled


async def get_user_disabled_menus(
    db: AsyncSession,
    user_id: int,
    scope: str,
) -> frozenset[str]:
    """读某用户被自助下线的菜单集（未配置 = 空集，直读不走缓存）。

    白名单收窄到 ``scope``（该用户所属域）——平台账号的存量值里若混入租户
    路径会被静默丢弃，反之亦然。只在登录下发 / 自助保存时读，频率低；DB
    故障按全开回落（告警日志），不让菜单读取打死登录。
    """
    try:
        row = (
            await db.execute(select(SystemConfig).where(SystemConfig.key == user_menus_key(user_id)))
        ).scalar_one_or_none()
    except Exception as exc:
        log.warning("读取按用户菜单开关失败，按全开处理：%s", exc)
        return frozenset()
    if row is None:
        return frozenset()
    return sanitize_disabled(row.value, scope)


async def set_user_disabled_menus(
    db: AsyncSession,
    *,
    user_id: int,
    scope: str,
    paths: list[str],
    updated_by: int,
) -> frozenset[str]:
    """全量覆盖写入某用户的自助下线集（事务由调用方 commit）。

    只作用于 ``user_id`` 本人（自助端点以 ``current_user`` 传入，无越界面）；
    白名单限定 ``scope`` 域声明路径（其他域路径 422）。行存在但开关为空时
    复用既有行写空列表，不删行——保留 updated_by/at 留痕。
    """
    return await _write_disabled_row(
        db,
        key=user_menus_key(user_id),
        description="用户隐藏的菜单",
        paths=paths,
        scope=scope,
        updated_by=updated_by,
    )


async def visible_menus_for(
    db: AsyncSession,
    *,
    scope: str,
    user_id: int,
    is_superuser: bool,
) -> dict[str, bool]:
    """组装当前用户在指定域下的可见菜单（``{path: enabled}``）。

    权限层两域各自判定：超管跳过 required（声明全集来自代码注册表）；普通
    用户一次查全量权限并集，required 空的菜单对全员可见。开关只吃该用户的
    自助下线集（按 ``scope`` 收窄白名单）——两域统一口径，无平台级开关。
    """
    specs = menus_by_scope(scope)
    disabled = await get_user_disabled_menus(db, user_id, scope)
    if is_superuser:
        return filter_visible_menus(specs, disabled, set(), True)
    perm_codes = await get_user_permissions(db, user_id)
    return filter_visible_menus(specs, disabled, perm_codes, False)
