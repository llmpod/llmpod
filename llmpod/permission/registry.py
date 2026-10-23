"""权限注册表。

权限声明集中在 ``llmpod/permission/permissions.py``：所有权限作为 ``PermSpec``
常量在该模块导入时自登记进本注册表。路由 gate 引用常量做 enforcement，
种子脚本 ``seed_permissions`` 统一从本注册表 upsert 到 DB——"声明处即种子处"，
无需手维护权限列表，杜绝漂移。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermSpec:
    """一个权限的不可变描述：``scope:resource:action`` + 描述。"""

    scope: str
    resource: str
    action: str
    description: str = ""

    @property
    def code(self) -> str:
        """权限串 ``scope:resource:action``。"""
        return f"{self.scope}:{self.resource}:{self.action}"


# 进程级注册表：key = .code（scope:resource:action）
_REGISTERED: dict[str, PermSpec] = {}


def register_permission(perm: PermSpec) -> PermSpec:
    """登记一条权限到进程级注册表。

    幂等：同一 ``.code`` 重复登记不会复制条目。description 首次非空者获胜；
    若旧记录缺 description 而本次带，则补全；若旧记录已有 description 则本次的忽略，
    避免无意覆盖（描述以最早带值者为准）。
    """
    prev = _REGISTERED.get(perm.code)
    if prev is None:
        _REGISTERED[perm.code] = perm
    elif not prev.description and perm.description:
        # 补全空 description
        _REGISTERED[perm.code] = perm
    return _REGISTERED[perm.code]


def registered_permissions() -> list[PermSpec]:
    """返回全部已登记权限，按 ``(scope, resource, action)`` 排序。"""
    return sorted(_REGISTERED.values(), key=lambda p: (p.scope, p.resource, p.action))


def is_registered(scope: str, resource: str, action: str) -> bool:
    """检查某权限是否已登记。"""
    return f"{scope}:{resource}:{action}" in _REGISTERED


def _reset_for_test() -> None:
    """清空注册表（仅测试用，避免跨用例污染）。"""
    _REGISTERED.clear()
