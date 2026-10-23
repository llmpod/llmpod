"""ProxyServer API 网关应用。

进程形态：独立进程（python -m llmpod proxyserver，:8090）。
本文件只做装配：lifespan（DB/Redis/记账/计费接线 + 后台轮询）+ 挂载路由
（routes/，runtime 挂 app.state.proxy）；请求生命周期编排见 inference_handler.py。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.proxyserver.billing import BillingService, reset_frozen_state
from llmpod.apps.proxyserver.routes import router as inference_router
from llmpod.apps.proxyserver.routing import EndpointAffinity, EndpointSelector
from llmpod.apps.proxyserver.snapshot import ConfigSnapshot, RouteEntry, build_config_snapshot, load_route_entry
from llmpod.config.enums import AffinityFailoverPolicy
from llmpod.config.settings import Settings, settings
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.services.settings_service import clear_settings_cache

# 转发客户端工厂在此 re-export：集成测试经 app 模块命名空间 monkeypatch，
# 且路由模块从 protocol 包直接 import（绑定各自引用，互不影响）。
from llmpod.apps.proxyserver.protocol import (  # noqa: F401  re-export 供测试 patch
    get_anthropic_client,
    get_openai_client,
    get_rerank_http_client,
)

log = logging.getLogger("llmpod.proxyserver")

# 回源结论缓存：快照未覆盖的名字在 TTL 内不再回源。快照答得出的永远走快路径，答不出
# 的那一侧（刚发布、刚绑成员，以及查无此名）才是每次请求都要打库的地方。TTL 只挡回源
# 流量：快照重建（周期轮询）时会整体作废本缓存，删除类变更不被 TTL 拖尾。
_RESOLVE_CACHE_TTL_SECONDS = 5.0
_RESOLVE_CACHE_MAX = 1024


# quota 重建 marker：存在即认为 Redis 计数器是上次写入的存活态，跳过全表重建。
# 带 TTL 兜底：计数器若因清库/驱逐丢失而 marker 仍存活，TTL 到期后下一次重启会重新
# 重建（重建本身就高不回填，见 limits.rebuild_quota_counters），避免配额被「used=0」
# 无限放行。
_QUOTA_REBUILD_MARKER = "llmpod:quota:rebuild"
_QUOTA_REBUILD_MARKER_TTL = 7 * 24 * 3600  # 7 天：计数器本应常驻，TTL 仅兜底，不频繁重建


@dataclass
class _ResolvedRoute:
    """一次回源的结论及其有效期；entry=None 表示库中查无此名。"""

    expires_at: float
    entry: RouteEntry | None


class ProxyRuntime:
    """网关运行态：配置快照（原子替换）+ 快照外的回源 + 端点选择器 + 后台任务登记。"""

    def __init__(self) -> None:
        self._snapshot: ConfigSnapshot | None = None
        if settings.affinity_enabled and settings.affinity_failover_policy not in AffinityFailoverPolicy.values():
            # 策略取值写错只告警不拦启动：请求路径按非 strict 处理（rebind 语义），
            # 行为退到可用性优先那一侧
            log.warning(
                "未知的亲和换端策略 %r（合法取值 %s），按 rebind（可用性优先）处理",
                settings.affinity_failover_policy,
                AffinityFailoverPolicy.values(),
            )
        self.selector = EndpointSelector(
            cooldown_seconds=settings.upstream_cooldown_seconds,
            failure_threshold=settings.upstream_failure_threshold,
            affinity=(
                EndpointAffinity(
                    ttl_seconds=settings.affinity_ttl_seconds,
                    max_entries=settings.affinity_max_entries,
                    stride=settings.affinity_stride,
                )
                if settings.affinity_enabled
                else None
            ),
        )
        # fire-and-forget 记账任务的强引用集合（防 GC 中断落账）
        self.log_tasks: set[asyncio.Task] = set()
        # 在途请求 id 集（hold reaper 的跳过集）：本进程仍在处理的请求，其 hold 即便
        # 超 TTL 也不回收（长流场景）；请求结束/流收尾时解除登记
        self.active_request_ids: set[str] = set()
        # 回源结论缓存：name → _ResolvedRoute
        self._resolved: dict[str, _ResolvedRoute] = {}

    @property
    def snapshot(self) -> ConfigSnapshot:
        if self._snapshot is None:
            raise RuntimeError("config snapshot not loaded yet")
        return self._snapshot

    async def rebuild(self, db: AsyncSession) -> ConfigSnapshot:
        # 重建 = 现在生效：清全局设置的进程 TTL 缓存（否则维护开关要再等一个 TTL 才
        # 拦得住），并作废回源旁路的旧结论（否则已删路由还能被缓存顶回来一个 TTL）
        clear_settings_cache()
        snap = await build_config_snapshot(db)
        self._snapshot = snap  # 引用替换即原子切换（frozen dataclass）
        self._resolved.clear()
        return snap

    async def get_or_build(self, db: AsyncSession) -> ConfigSnapshot:
        if self._snapshot is None:
            await self.rebuild(db)
        return self._snapshot

    async def resolve(self, db: AsyncSession, name: str) -> RouteEntry | None:
        """按对外名取路由条目：快照命中直接用，未覆盖时才回源点查（结论短期缓存）。

        回源只补「快照还没轮到」——新增路由、路由刚绑上成员，发布后立即可用，
        不必等下一个轮询周期；快照已有的结论不被覆盖，下架与删除的滞后由周期
        重建承担（重建时作废回源缓存，不被 TTL 拖尾）。
        """
        snapshot = await self.get_or_build(db)
        entry = snapshot.get(name)
        if entry is not None and entry.targets:
            return entry

        cached = self._resolved.get(name)
        if cached is not None and cached.expires_at > time.monotonic():
            if cached.entry is not None:
                return cached.entry
            return entry  # 缓存结论是「查无此名」→ 回落快照口径

        loaded = await load_route_entry(db, name)
        self._remember_resolved(name, loaded)
        if loaded is not None:
            return loaded
        return entry  # 回源也答不出：保留快照那条（存在但无候选），删除滞后交给轮询

    def _remember_resolved(self, name: str, entry: RouteEntry | None) -> None:
        # 表满整体清空：有界优先于命中率，本缓存只挡回源流量，重建成本为零
        if len(self._resolved) >= _RESOLVE_CACHE_MAX:
            self._resolved.clear()
        expires_at = time.monotonic() + _RESOLVE_CACHE_TTL_SECONDS
        self._resolved[name] = _ResolvedRoute(expires_at=expires_at, entry=entry)

    # 异步记账：logger 为 None = 不记账（测试默认/显式关闭）；log_db 为测试注入
    logger = None
    log_db = None
    # 钱包扣费：billing 为 None = 不扣费（测试默认；生产在 lifespan 接线）
    billing = None


def create_proxy_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings
    runtime = ProxyRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from llmpod.db import init_required_dbs, close_all_dbs

        await init_required_dbs(cfg)
        background_tasks = BackgroundTaskManager()
        try:
            from pathlib import Path

            from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
            from llmpod.apps.proxyserver.hold_reaper import reap_expired_holds
            from llmpod.apps.proxyserver.limits import rebuild_quota_counters
            from llmpod.apps.proxyserver.request_logger import RequestLogger
            from llmpod.db import get_db_context
            from llmpod.services.api_key_service import warm_apikey_cache

            async with get_db_context() as db:
                redis = await app_dependency_redis()
                await warm_apikey_cache(db, redis)
                await runtime.rebuild(db)
                # 有未结账时保留冻结并告警；只有干净状态才做零值归一。
                await reset_frozen_state(db)
                # Redis 配额计数器丢失（清库/持久化关闭重启）才从 PG 调用日志聚合重建；
                # marker 在则跳过全表扫——常规重启开销 O(1)
                try:
                    marker = await redis.get(_QUOTA_REBUILD_MARKER)
                    if marker is None:
                        counts = await rebuild_quota_counters(db, redis)
                        await redis.set(_QUOTA_REBUILD_MARKER, "1", ex=_QUOTA_REBUILD_MARKER_TTL)
                        if counts["keys"]:
                            log.info("quota counters rebuilt from request_log: %s", counts)
                except Exception:
                    log.exception("quota counter rebuild failed (startup)")
            body_store = LocalFileBodyStore(Path(cfg.body_dir))
            runtime.logger = RequestLogger(db_factory=get_db_context, store=body_store)
            # 钱包扣费接线（PG 独占记账，短事务）
            runtime.billing = BillingService(db_factory=get_db_context)
            # 三个后台循环：config 热更新 / BodyStore GC / 悬空冻结回收（见 hold_reaper.py）
            background_tasks.every(
                lambda: _poll_config_once(runtime),
                name="proxy-config-poll",
                interval=cfg.poll_interval_seconds,
            )
            background_tasks.every(
                lambda: _sweep_body_store_once(body_store, cfg.body_retention_seconds),
                name="body-store-sweep",
                interval=cfg.body_sweep_interval_seconds,
            )
            background_tasks.every(
                lambda: reap_expired_holds(get_db_context, runtime.active_request_ids),
                name="hold-reaper",
                interval=cfg.hold_reap_interval_seconds,
            )
            yield
        finally:
            await background_tasks.stop()
            # 请求记账任务有自己的完成策略：限时等待，再取消仍未完成的任务。
            if runtime.log_tasks:
                _, pending = await asyncio.wait(runtime.log_tasks, timeout=10)
                for pending_task in pending:
                    pending_task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            from llmpod.apps.proxyserver.protocol import close_shared_clients

            await close_shared_clients()
            await close_all_dbs()

    async def app_dependency_redis():
        from llmpod.db import get_redis as _gr

        return _gr()

    app = FastAPI(
        title="LLMPod ProxyServer",
        lifespan=lifespan,
        # 文档端点与管理端同闸（docs_enabled，默认关）：API 网关 schema 对外暴露
        # 只服务调试，生产无理由放开
        docs_url="/docs" if cfg.docs_enabled else None,
        redoc_url="/redoc" if cfg.docs_enabled else None,
        openapi_url="/openapi.json" if cfg.docs_enabled else None,
    )
    # CORS 与 webserver 同源配置（LLMPOD_CORS_ORIGINS）：SDK 服务端调用不依赖 CORS，
    # 浏览器直连网关时按配置放行/拦截（默认空 = 同源，跨域直连需显式配来源白名单）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=cfg.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.proxy = runtime
    app.include_router(inference_router)

    return app


async def _poll_config_once(runtime: ProxyRuntime) -> None:
    """重建配置快照；周期由 BackgroundTaskManager 管理。"""
    from llmpod.db import get_db_context

    async with get_db_context() as db:
        await runtime.rebuild(db)


# 引用核查的 IN 批大小（64 位哈希 × 2 列，1000/批对 asyncpg 参数上限余量充足）
_SWEEP_REFERENCED_BATCH = 1000


async def _sweep_body_store_once(store, retention_seconds: int) -> None:
    """BodyStore 保守 GC 单轮：仅清「超保留期且无计量行引用」的块。

    引用集不整表物化（request_log 只增不删，全量 refs 集合随运行时长无界膨胀）：
    先扫盘拿超期候选（无候选即短路，常态零 PG 查询），再按批回查两列 ref 的 IN
    命中，只删核查后确认无引用的块；remove_blocks 删除前还会按 mtime 复核一遍
    「仍超期」——核查窗口内被 put 复用的块（mtime 已刷新、新引用行在途）借此
    挡掉。request_ref/response_ref 无索引，回查是顺扫但按批推进、内存有界；
    文件遍历/删除走 to_thread 不阻塞事件循环。store/参数由 lifespan 传入
    （与写侧同一实例、同一配置源）。
    """
    from sqlalchemy import or_, select

    from llmpod.db import get_db_context
    from llmpod.models import RequestLog

    candidates = await asyncio.to_thread(store.expired_candidates, retention_seconds)
    if not candidates:
        return
    referenced: set[str] = set()
    async with get_db_context() as db:
        for i in range(0, len(candidates), _SWEEP_REFERENCED_BATCH):
            chunk = candidates[i : i + _SWEEP_REFERENCED_BATCH]
            rows = await db.execute(
                select(RequestLog.request_ref, RequestLog.response_ref).where(
                    or_(RequestLog.request_ref.in_(chunk), RequestLog.response_ref.in_(chunk))
                )
            )
            for request_ref, response_ref in rows:
                if request_ref:
                    referenced.add(request_ref)
                if response_ref:
                    referenced.add(response_ref)
    doomed = [h for h in candidates if h not in referenced]
    if not doomed:
        return
    removed = await asyncio.to_thread(store.remove_blocks, doomed, retention_seconds=retention_seconds)
    if removed:
        log.info("body store sweep removed %s blocks", removed)


# 全局应用实例（uvicorn: llmpod.apps.proxyserver.app:app）
app = create_proxy_app()
