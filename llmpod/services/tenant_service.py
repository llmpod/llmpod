"""租户域服务：统计装配。

当前只收口"租户表展示用户 / 角色计数"与"租户概览聚合"两类跨路由复用逻辑
（管理端 ``/admin/tenants`` 列表与详情在返回前批量回填统计字段；``/overview``
一次聚合概览页整屏数据）。保持 services 层"纯 async 函数、首参 ``AsyncSession``"
约定；数据隔离不在本层做。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.core.exceptions import NotFound
from llmpod.models.balance_transaction import BalanceTransaction
from llmpod.models.role import Role
from llmpod.models.tenant import Tenant
from llmpod.models.user import User
from llmpod.services.model_block_service import get_blocked_models_map
from llmpod.services.wallet_service import get_tenant_balance_map
from llmpod.utils.time import now_local

log = logging.getLogger("llmpod.services.tenant")


async def get_tenant(db: AsyncSession, tenant_id: int) -> Tenant:
    """取租户；不存在 → NotFound（租户资源存在性的单一判定点）。"""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Tenant", tenant_id)
    return tenant


async def get_tenant_stats_map(db: AsyncSession, tenant_ids: list[int]) -> dict[int, dict]:
    """批量取 ``tenant_id → {user_count, role_count}``。

    user_count / role_count 各一条 GROUP BY tenant_id。未出现的 tenant_id 由调用方补 0。
    平台租户 id=0 同样纳入统计（User / Role 的 tenant_id 普通列，无特殊处理）。
    """
    if not tenant_ids:
        return {}
    user_stmt = (
        select(User.tenant_id, func.count(User.id)).where(User.tenant_id.in_(tenant_ids)).group_by(User.tenant_id)
    )
    user_map = {tid: cnt for tid, cnt in (await db.execute(user_stmt)).all()}
    role_stmt = (
        select(Role.tenant_id, func.count(Role.id)).where(Role.tenant_id.in_(tenant_ids)).group_by(Role.tenant_id)
    )
    role_map = {tid: cnt for tid, cnt in (await db.execute(role_stmt)).all()}
    # 仅返回有用户或角色的租户（稀疏映射）；无用户 / 角色的租户不在结果中，
    # 由 enrich_tenants 的默认值兜底为 0。
    return {
        tid: {
            "user_count": user_map.get(tid, 0),
            "role_count": role_map.get(tid, 0),
        }
        for tid in user_map.keys() | role_map.keys()
    }


async def enrich_tenants(db: AsyncSession, tenants: list[Tenant]) -> list[Tenant]:
    """把每个租户的 user_count / role_count / 余额 / 封禁模型批量回填到 ORM 实例上（in-place）。

    余额从 wallet 表批量取（无钱包行按 0），available 为派生字段（balance - frozen_balance），
    与钱包路由口径一致；封禁模型从 tenant_model_block 批量取 active 行并解析出名称/类别。
    供路由在返回前调用：``TenantDetailOut`` 经 ``from_attributes`` 读取。
    """
    if not tenants:
        return tenants
    ids = [t.id for t in tenants]
    stats = await get_tenant_stats_map(db, ids)
    balances = await get_tenant_balance_map(db, ids)
    blocked = await get_blocked_models_map(db, ids)
    for t in tenants:
        s = stats.get(t.id, {})
        t.user_count = s.get("user_count", 0)
        t.role_count = s.get("role_count", 0)
        balance, frozen = balances.get(t.id, (Decimal("0"), Decimal("0")))
        t.balance = balance
        t.frozen_balance = frozen
        t.available = balance - frozen
        t.blocked_models = blocked.get(t.id, [])
    return tenants


async def tenant_overview(db: AsyncSession, *, days: int, top_n: int) -> dict:
    """租户概览页整屏聚合：状态计数 / 总用户数 / 近 N 天消费趋势 / 余额 top。

    消费趋势按 ``balance_transaction``（type=consume，amount 为负数记账）逐租户逐日求和，
    窗口为本地时区近 N 天（含今日，空日补 0）；趋势与余额 top 都取区间消费总额 /
    可用余额降序的前 top_n 租户。平台租户（id=0）与平台用户不参与租户口径。
    """
    now = now_local()
    start_day = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_list = [start_day.date() + timedelta(days=i) for i in range(days)]

    # 状态计数 + 总用户数
    tenants = list(
        (await db.execute(select(Tenant.id, Tenant.name, Tenant.status).where(Tenant.id != PLATFORM_TENANT_ID))).all()
    )
    total_tenants = len(tenants)
    active_tenants = sum(1 for t in tenants if t.status == "active")
    disabled_tenants = total_tenants - active_tenants
    total_users = (
        await db.execute(select(func.count(User.id)).where(User.tenant_id != PLATFORM_TENANT_ID))
    ).scalar() or 0
    name_by_id = {t.id: t.name for t in tenants}

    # 消费流水窗口内按租户+本地日 SQL 聚合（consume 的 amount 记负数，日消费取绝对值
    # 求和）。日界用 epoch+时区偏移再按日取整的表达式，不依赖 PG 时区名/会话 GUC；
    # 账务表行量大，聚合必须下推 SQL，不拉明细进内存。day_idx 为「本地日序号」，
    # 与窗口首日序号相减即得在 day_list 中的下标（整数解码，不做二次时区换算）。
    tz_off = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    day_expr = func.floor((func.extract("epoch", BalanceTransaction.created_at) + tz_off) / 86400)
    tx_rows = (
        await db.execute(
            select(
                BalanceTransaction.tenant_id,
                day_expr,
                func.sum(BalanceTransaction.amount),
            )
            .where(
                BalanceTransaction.type == "consume",
                BalanceTransaction.created_at >= start_day,
                BalanceTransaction.tenant_id != PLATFORM_TENANT_ID,
            )
            .group_by(BalanceTransaction.tenant_id, day_expr)
        )
    ).all()

    base_idx = int((start_day.timestamp() + tz_off) // 86400)
    by_day: dict[int, dict[int, Decimal]] = {}
    for tid, day_idx, amount in tx_rows:
        # tenant_id/amount 均 NOT NULL；窗口外（时钟漂移/未来流水）不进图表
        idx = int(day_idx) - base_idx
        if not 0 <= idx < days:
            continue
        day_by_tenant = by_day.setdefault(tid, {})
        day_by_tenant[idx] = day_by_tenant.get(idx, Decimal("0")) + abs(amount)

    window_total = {tid: sum(m.values()) for tid, m in by_day.items()}
    trend_rank = sorted(window_total, key=lambda t: window_total[t], reverse=True)[:top_n]
    dates = [d.isoformat() for d in day_list]
    consume_trend = [
        {
            "tenant_id": tid,
            "tenant_name": name_by_id.get(tid, f"租户#{tid}"),
            "data": [float(by_day.get(tid, {}).get(i, Decimal("0"))) for i in range(days)],
        }
        for tid in trend_rank
    ]

    # 余额 top（available = balance − frozen，wallet 列 NOT NULL 无兜底需求）
    balances = await get_tenant_balance_map(db, [t.id for t in tenants])
    wallets = [(balance - frozen, balance, tid) for tid, (balance, frozen) in balances.items()]
    wallets.sort(key=lambda w: w[0], reverse=True)
    balance_top = [
        {
            "tenant_id": tid,
            "tenant_name": name_by_id.get(tid, f"租户#{tid}"),
            "balance": balance,
            "available": available,
        }
        for available, balance, tid in wallets[:top_n]
    ]

    return {
        "total_tenants": total_tenants,
        "active_tenants": active_tenants,
        "disabled_tenants": disabled_tenants,
        "total_users": int(total_users),
        "dates": dates,
        "consume_trend": consume_trend,
        "balance_top": balance_top,
    }
