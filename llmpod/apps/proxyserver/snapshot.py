"""配置快照：PG 配置态 → API 网关内存路由表。

快照为不可变对象，由后台轮询任务重建后原子替换（热更新不中断，默认 3s 周期）。
轮询有周期，新增路由/新绑成员要等到下一轮才进快照，网关因此保留一条回源点查的
旁路（``load_route_entry``）——两条路径共用 ``build_route_entry`` 的装配规则。
装配规则：
- 路由全集进快照（unpublished 也在——路由层据此返回 403（model_not_published），
  与「路由不存在」的 404 区分开，不把「下架」与「不存在」混同）；
- 每个路由的候选来源 = RouteBinding（enabled）+ ModelEndpoint（enabled + healthy）展开；
- RouteTarget 带 protocol/api_key：转发按协议过滤、api_key 设 header（网关零部署感知）；
- 维护态随快照下发（全局设置的 maintenance 字段），管道据此拦租户 Key——
  复用既有轮询周期热更新，不新增轮询任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models import ModelEndpoint, Route, RouteBinding, Tenant, TenantModelBlock
from llmpod.models.route import FAILOVER_ENABLED_DEFAULT, FAILOVER_MAX_RETRIES_DEFAULT
from llmpod.services.publish_service import get_route_by_name, list_bindings
from llmpod.services.settings_service import MaintenanceState, get_maintenance


@dataclass(frozen=True)
class RouteTarget:
    """一个可选的来源端点（经路由成员展开后的可调用目标）。"""

    endpoint_id: int
    deployment_id: str | None
    api_base: str
    path: str | None
    served_model: str
    engine_type: str
    weight: int
    priority: int
    protocol: str = "openai"
    api_key: str | None = None
    # 上游展示名（快照自端点 ``name``：本地端点是部署名、外部端点是登记名，恒非空）。
    # 字段名沿用 provider_name 只为对齐 request_log 列名与前端 JSON 契约，语义已不是
    # 「供应商」——原来取自可空的 ModelEndpoint.provider_name，空值会让上游认不出来。
    # 快照装配恒显式传值；缺省 None 仅兜底 wire/测试构造（同 RouteEntry 的 failover 缺省口径）
    provider_name: str | None = None

    @property
    def upstream_type(self) -> str:
        """上游流向：deployment_id 有值=本地部署，NULL=外部登记端点。"""
        return "local" if self.deployment_id else "external"


@dataclass(frozen=True)
class RouteEntry:
    """路由条目：对外名 → 定价 + failover 配置 + 可选端点集。"""

    route_id: int
    name: str
    status: str
    input_price_per_mtok: Decimal | None
    output_price_per_mtok: Decimal | None
    cached_input_price_per_mtok: Decimal | None = None
    # 与 Route 列默认同源（列非空，快照装配恒显式传值；缺省仅兜底 wire 构造）
    failover_enabled: bool = FAILOVER_ENABLED_DEFAULT
    failover_max_retries: int = FAILOVER_MAX_RETRIES_DEFAULT
    targets: tuple[RouteTarget, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConfigSnapshot:
    """网关配置快照（鉴权/路由所需的全部配置态）。"""

    version: int
    routes: dict[str, RouteEntry]
    # 租户域授权态：tenant_id → 生效封禁路由 ID 集 / tenant_id → Tenant.status。
    # 热路径授权（authz）从这里取，免每请求两次 PG 点查；变更经周期轮询生效
    tenant_blocked_routes: dict[int, set[int]] = field(default_factory=dict)
    tenant_statuses: dict[int, str] = field(default_factory=dict)
    maintenance: MaintenanceState = field(default_factory=MaintenanceState)

    def get(self, name: str) -> RouteEntry | None:
        return self.routes.get(name)


async def _load_all(db: AsyncSession, entity) -> list:
    return list((await db.execute(select(entity))).scalars())


def build_route_entry(
    route: Route,
    bindings: list[RouteBinding],
    endpoints_by_id: dict[int, ModelEndpoint],
) -> RouteEntry:
    """单条路由装配：成员逐条展开为候选端点。

    全量构建与回源点查共用本函数——候选过滤口径（成员 enabled → 端点
    enabled 且 healthy）只此一处，两条路径装配出的条目才能一致。
    """
    targets: list[RouteTarget] = []
    for b in bindings:
        if not b.enabled:
            continue
        e = endpoints_by_id.get(b.endpoint_id)
        if e is None or not e.enabled or e.health_status != "healthy":
            continue
        targets.append(
            RouteTarget(
                endpoint_id=e.id,
                deployment_id=e.deployment_id,
                api_base=e.api_base,
                path=e.path,
                served_model=e.served_model,
                engine_type=e.engine_type,
                weight=b.weight,
                priority=b.priority,
                protocol=e.protocol,
                api_key=e.api_key,
                provider_name=e.name,
            )
        )
    return RouteEntry(
        route_id=route.id,
        name=route.name,
        status=route.status,
        input_price_per_mtok=route.input_price_per_mtok,
        output_price_per_mtok=route.output_price_per_mtok,
        cached_input_price_per_mtok=route.cached_input_price_per_mtok,
        failover_enabled=route.failover_enabled,
        failover_max_retries=route.failover_max_retries,
        targets=tuple(targets),
    )


async def build_config_snapshot(db: AsyncSession, *, version: int = 0) -> ConfigSnapshot:
    """全量读配置态并装配快照（单机规模配置态，全量拉取 + Python 装配）。"""
    routes = await _load_all(db, Route)
    bindings = await _load_all(db, RouteBinding)
    endpoints = await _load_all(db, ModelEndpoint)
    tenants = await _load_all(db, Tenant)
    blocks = await _load_all(db, TenantModelBlock)
    maintenance = await get_maintenance(db)

    bindings_by_route: dict[int, list[RouteBinding]] = {}
    for b in bindings:
        bindings_by_route.setdefault(b.route_id, []).append(b)

    ep_by_id = {e.id: e for e in endpoints}

    entries: dict[str, RouteEntry] = {}
    for route in routes:
        entries[route.name] = build_route_entry(route, bindings_by_route.get(route.id, []), ep_by_id)

    # 租户域授权态：active 封禁集 + 租户状态（authz 热路径消费，随轮询热更新）
    tenant_blocked_routes: dict[int, set[int]] = {}
    for block in blocks:
        if block.status == "active":
            tenant_blocked_routes.setdefault(block.tenant_id, set()).add(block.route_id)
    tenant_statuses = {t.id: t.status for t in tenants}

    return ConfigSnapshot(
        version=version,
        routes=entries,
        tenant_blocked_routes=tenant_blocked_routes,
        tenant_statuses=tenant_statuses,
        maintenance=maintenance,
    )


async def load_route_entry(db: AsyncSession, name: str) -> RouteEntry | None:
    """按对外名回源装配单条路由（点查，不整表扫）；库中无此名返回 None。

    网关用它补查快照还没轮到的名字：新增路由、路由刚绑上成员都不必等下一个
    轮询周期即可生效。只补「快照没有」这一侧——快照已有的结论不被覆盖，下架与
    删除的滞后由周期轮询承担（重建时回源缓存一并作废，不被 TTL 拖尾）。
    """
    route = await get_route_by_name(db, name)
    if route is None:
        return None

    bindings = await list_bindings(db, route_id=route.id)
    endpoints_by_id: dict[int, ModelEndpoint] = {}
    if bindings:
        endpoint_ids = [b.endpoint_id for b in bindings]
        endpoints = list((await db.execute(select(ModelEndpoint).where(ModelEndpoint.id.in_(endpoint_ids)))).scalars())
        endpoints_by_id = {e.id: e for e in endpoints}
    return build_route_entry(route, bindings, endpoints_by_id)
