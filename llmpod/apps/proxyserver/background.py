"""proxyserver 的定时生命周期任务：登记与节拍集中一处。

app.py 只做装配（lifespan 内一次调用 register_background_tasks）；本模块收拢
每个任务的循环体/间隔/重试/注册，域逻辑仍在各自模块——配置重建的实现在
snapshot.py、块轮转的选块策略在 retention.py、存储在 bodystore.py、悬空冻结
回收在 hold_reaper.py。新增定时任务 = 在此加一个登记项。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from llmpod.apps.proxyserver.bodystore import BodyStore
from llmpod.apps.proxyserver.hold_reaper import reap_expired_holds
from llmpod.apps.proxyserver.retention import RetentionPolicy, build_retention_policy
from llmpod.config.settings import Settings
from llmpod.core.taskwatch import BackgroundTaskManager

log = logging.getLogger("llmpod.proxyserver.background")

# 单轮删除上限与积压快速重试：首轮升级的部署可能压着几十万超期块，一轮删完
# 会造成 IO 尖峰；删满上限说明还有积压，隔 _SWEEP_BACKLOG_RETRY_SECONDS 再跑
# 一轮消化，删不满（回到正常水位）则回到常规周期
_SWEEP_MAX_REMOVALS_PER_PASS = 50000
_SWEEP_BACKLOG_RETRY_SECONDS = 60


async def poll_config_once(runtime, *, db_context: Callable[[], "object"]) -> None:
    """重建配置快照；周期由 BackgroundTaskManager 管理。

    ``db_context`` 由调用方注入（生产传 llmpod.db.get_db_context）——本模块
    顶部 import 它会在测试里连真实 DB 模块，集成测试按 app 模块命名空间
    monkeypatch 的既有范式也随之失效。
    """
    async with db_context() as db:
        await runtime.rebuild(db)


async def sweep_body_store_once(store: BodyStore, policy: RetentionPolicy) -> bool:
    """块轮转单轮：清点 → 策略选块 → 限速删除。返回是否删满上限（有积压）。

    早期实现按「超期且无计量行引用」删除，引用回查要对 request_log 的
    request_ref/response_ref 做 IN 批查——两列无索引，每批一次全表顺扫，行只增
    不删的部署上候选集随历史无界增长，小规格机器上单轮可把 CPU 打满数分钟。
    现行口径：body 快照本就是短命排障产物，按轮转策略直接删，不再回查引用
    （块先删、计量行后留：详情页对已删块按 body 缺失展示）。文件遍历/策略
    排序/删除全走 to_thread（积压期数十万块的排序不能压事件循环）。每轮打
    一条日志（含空转轮：证明任务活着——旧实现 removed=0 时静默，GC 烧 CPU
    零痕迹）。
    """
    blocks = await asyncio.to_thread(store.list_blocks)
    doomed = await asyncio.to_thread(policy.select, blocks, now=time.time())
    batch = doomed[:_SWEEP_MAX_REMOVALS_PER_PASS]
    removed = await asyncio.to_thread(store.remove_blocks, batch) if batch else 0
    backlog = len(doomed) > len(batch)
    log.info(
        "body store sweep: listed=%s selected=%s removed=%s backlog=%s",
        len(blocks),
        len(doomed),
        removed,
        backlog,
    )
    return backlog


def _start_body_sweep(
    manager: BackgroundTaskManager,
    store: BodyStore,
    *,
    interval_seconds: int,
    policy_factory: Callable[[], RetentionPolicy],
) -> None:
    """登记轮转循环。策略每轮经 ``policy_factory`` 现取——管理端热改轮转方式/
    数值进配置快照（≤3s），下一轮即按新策略执行。间隔自选：首轮与积压未清完
    时隔 _SWEEP_BACKLOG_RETRY_SECONDS 快速跑（升级首日的存量超期块在小时级内
    消化，不等常规周期），否则回 interval_seconds 常规节拍。"""

    async def self_scheduling() -> None:
        next_delay = _SWEEP_BACKLOG_RETRY_SECONDS
        while True:
            await asyncio.sleep(next_delay)
            try:
                backlog = await sweep_body_store_once(store, policy_factory())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单轮失败不崩循环，下一轮重试
                log.exception("body store sweep 单轮失败")
                backlog = False
            next_delay = _SWEEP_BACKLOG_RETRY_SECONDS if backlog else interval_seconds

    manager.start(self_scheduling, name="body-store-sweep")


def register_background_tasks(
    manager: BackgroundTaskManager,
    *,
    runtime,
    cfg: Settings,
    body_store: BodyStore,
    db_context: Callable[[], "object"],
) -> None:
    """登记本进程全部定时任务（任务清单即本函数体，节拍取自 cfg）。"""
    manager.every(
        lambda: poll_config_once(runtime, db_context=db_context),
        name="proxy-config-poll",
        interval=cfg.poll_interval_seconds,
    )

    def _current_retention_policy() -> RetentionPolicy:
        snap = runtime.snapshot
        return build_retention_policy(
            cfg,
            mode=snap.body_retention_mode,
            days=snap.body_retention_days,
            max_bytes=snap.body_max_bytes,
        )

    _start_body_sweep(
        manager,
        body_store,
        interval_seconds=cfg.body_sweep_interval_seconds,
        policy_factory=_current_retention_policy,
    )
    manager.every(
        lambda: reap_expired_holds(db_context, runtime.active_request_ids),
        name="hold-reaper",
        interval=cfg.hold_reap_interval_seconds,
    )
