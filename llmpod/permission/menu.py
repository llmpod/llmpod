"""菜单注册表：菜单声明（路由路径 + 名称 + 可见性规则）的唯一定义处。

菜单不是权限——控制入口展示，不参与 RBAC。API 安全始终由路由上的
``require_permission`` 把门，菜单只决定「侧边栏给不给用户看」。

- ``required``：该菜单背后的功能权限（OR 语义，持有任一即可见）。
  想不给某人看某菜单 = 不授予对应的 Read 权限即可。空 = 对该域全员
  可见（无后端权限门的页面，如个人设置；在线调试走推理代理的 Key 鉴权而非
  管理端权限）；
- 注册表本身：按 scope 查询该侧全部菜单声明，供可见性过滤与用户自助
  开关的白名单校验。开关身份是裸 path（不带 scope），两侧路径前缀
  （/admin、/tenant）不得重复。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmpod.permission.registry import PermSpec

# 进程级菜单注册表：key = (scope, path)
_MENUS: dict[tuple[str, str], MenuSpec] = {}


@dataclass(frozen=True)
class MenuSpec:
    """一个菜单的声明：路由路径 + 名称 + 可见性规则。"""

    path: str
    title: str
    scope: str
    required: tuple[PermSpec, ...] = ()

    @property
    def required_codes(self) -> tuple[str, ...]:
        """背后功能权限码列表（管理端展示用）。"""
        return tuple(p.code for p in self.required)


def _menu(
    scope: str,
    path: str,
    title: str,
    required: tuple[PermSpec, ...] = (),
) -> MenuSpec:
    """声明一个菜单并登记进菜单注册表（同 (scope, path) 重复声明后者覆盖）。"""
    spec = MenuSpec(path=path, title=title, scope=scope, required=required)
    _MENUS[(scope, path)] = spec
    return spec


def menus_by_scope(scope: str) -> list[MenuSpec]:
    """返回指定域的全部菜单声明，按 path 排序（确定性顺序）。"""
    return sorted((m for m in _MENUS.values() if m.scope == scope), key=lambda m: m.path)


def declared_menu_paths(scope: str | None = None) -> set[str]:
    """已声明菜单的 path 集合（开关存量值 sanitize / 写侧白名单用）。

    ``scope`` 给定时只返回该域（用户自助开关只认本人所属域路径，防越域配置）。
    """
    return {m.path for m in _MENUS.values() if scope is None or m.scope == scope}
