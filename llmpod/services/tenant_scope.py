"""平台域作用域判定（admin 列表共用）。

「平台域」= 无租户维度的行（tenant_id IS NULL，如平台级资源操作、平台直发 Key 的
计量行）∪ 平台租户自身的行（tenant_id = 0）。三个 admin 列表（审计日志 / 调用日志 /
API Key）共用同一条判定——各写一份时曾漂移出两种语义（「传 0」一个当平台域哨兵、
一个当精确等值），本模块统一收口、只此一份。

租户端路由不传开关、恒带具体 tenant_id，本判定对租户视角不生效。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, or_
from sqlalchemy.sql.elements import ColumnClause

from llmpod.config.constants import PLATFORM_TENANT_ID


def platform_scope_requested(tenant_id: int | None, *, exclude_tenant_scope: bool) -> bool:
    """admin 请求是否要的是平台域视图。

    路由侧的派生开关同源于此：``tenant_id`` 不传或传 0 = 平台域（哨兵口径与
    database 规则一致）；传具体租户（N≥1）= 精确看该租户，不是平台域。
    """
    return exclude_tenant_scope and (tenant_id is None or tenant_id == PLATFORM_TENANT_ID)


def platform_domain_clause(tenant_col: ColumnClause) -> ColumnElement[bool]:
    """平台域的 SQL 谓词：``tenant_id IS NULL OR tenant_id = 0``。

    只在 :func:`platform_scope_requested` 为真时由调用方追加；列由调用方传入
    （各服务自己的 ORM 列），本函数不耦合任何具体表。
    """
    return or_(tenant_col.is_(None), tenant_col == PLATFORM_TENANT_ID)
