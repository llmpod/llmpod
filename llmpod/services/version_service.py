"""版本与更新检查服务。

「检查更新」的上游是官网 manifest 端点，地址写死在 ``check_update`` 内
（产品级固定值，随代码发版变更；不做部署配置，也没有可覆盖它的变量）。
请求带 ``X-LLMPod-Version`` 头，服务端据此定通道——stable/beta/test 实例
各自只看到本通道最新版。manifest 契约::

    {
      "latest_version": "26.9.4",
      "release_type": "feature",               # 版本类型：feature | patch（修复版，可渲染更强提醒）
      "notes": "1. 新增 xxx\\n2. 修复 xxx",   # 可选：新版本更新内容（多行文本）
      "release_url": "https://...",            # 可选：更新详情/下载页链接
      "published_at": "2026-09-04T10:00:00Z"   # 可选：新版本发布时间（ISO 8601）
    }

对外语义约定：``latest_version`` 只在确有新版本时才高于当前版本——
无更新 / 上游失败时一律等于当前版本，前端据此零歧义地渲染
红点与「已是最新版本」提示。版本号为日历版本号（``yy.m.d``，当天追加发布
``yy.m.d.n``），比较按段数值语义（缺段补 0，容忍 ``v`` 前缀）；解析不了的候选版本一律视为无更新，绝不拿垃圾版本号吓用户。

上游结果走进程内 TTL 缓存（避免每次登录/每个页面挂载都打上游），
``asyncio.Lock`` 保证并发下只有一个请求真正外呼。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import llmpod
from llmpod.config.settings import settings
from llmpod.models.system_config import SystemConfig
from llmpod.utils import now_utc

log = logging.getLogger("llmpod.services.version")

# 官方发布镜像：默认 CNB 制品库（国内直连），发版时同一镜像同步推 Docker Hub
# 作海外备份。节点升级提议的 image_ref 由此推导（版本 tag 随发版双推），仓库
# 可用 LLMPOD_OFFICIAL_IMAGE 覆盖（离线/自建分发指向私有 registry），单个节点
# 仍可用引导参数里的 image= 指定本地 ref。
OFFICIAL_IMAGE = settings.official_image


def official_image_ref(version: str | None = None) -> str:
    """官方镜像的版本 ref（默认当前运行版本）。

    发版纪律保证每个版本号都有对应 tag；manager 下发 ``$node_upgrade`` 时用它
    告诉节点「拉哪个镜像能到我的版本」。
    """
    return f"{OFFICIAL_IMAGE}:{version or llmpod.__version__}"


# 上游请求超时（秒）；成功结果缓存时长（秒，避免频繁打上游）；
# 失败结果负缓存时长（秒）——上游故障期不让每个请求都吃满超时还排队，
# 又不把瞬时故障固化成五分钟
UPDATE_FETCH_TIMEOUT_SECONDS = 5.0
UPDATE_CACHE_TTL_SECONDS = 300.0
UPDATE_FAIL_TTL_SECONDS = 15.0

# 版本号形态：1-4 个数值段（日历版本号 yy.m.d，当天追加发布 yy.m.d.n），
# 可带 -beta / -test 之类预发布后缀（不参与比较，仅选通道）。段之间必须是单个点。
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:[-][0-9A-Za-z.-]+)?$")


class UpdateCheckData(BaseModel):
    """检查更新结果。"""

    current_version: str
    latest_version: str
    has_update: bool
    update_check_enabled: bool
    checked_at: datetime | None = None
    notes: str | None = None
    release_url: str | None = None
    published_at: datetime | None = None  # 新版本发布时间（manifest 提供；未提供/非法为 None）
    release_type: str | None = None  # 新版本类型 feature | patch；patch=修复版可提示尽快更新；回显态为 None
    error_message: str | None = None


def parse_version(raw: str) -> tuple[int, int, int, int] | None:
    """把版本串解析成可比较的数值元组；不成形返回 None。

    日历版本号体系：``yy.m.d``（如 26.9.3），当天追加发布追加第 4 段
    （26.9.3.1）。比较按段数值进行，缺段补 0——``26.9.3`` 等价于
    ``26.9.3.0``，因此 ``26.9.3.1 > 26.9.3``、``26.9.4 > 26.9.3.9``。
    容忍 ``v`` 前缀与首尾空白；预发布后缀（``-beta`` / ``-test``）不参与比较。
    """
    match = _VERSION_RE.match(raw.strip().lstrip("vV"))
    if match is None:
        return None
    parts = [int(g or 0) for g in match.groups()[:4]]
    return parts[0], parts[1], parts[2], parts[3]


def _no_update(current: str, *, checked_at: datetime | None, error: str | None) -> UpdateCheckData:
    """构造「无更新」形态的结果（latest 恒等于 current；检查恒启用）。"""
    return UpdateCheckData(
        current_version=current,
        latest_version=current,
        has_update=False,
        update_check_enabled=True,
        checked_at=checked_at,
        error_message=error,
    )


# 进程内缓存：单槽 ``(写入时刻 monotonic, 结果)``。成功走长 TTL，失败走短 TTL
# 负缓存；多 worker 各自缓存
_cache: tuple[float, UpdateCheckData] | None = None
_fetch_lock = asyncio.Lock()


def _fresh_cache(now: float) -> UpdateCheckData | None:
    """未过期的缓存结果；失败结果按短 TTL 负缓存判定。"""
    if _cache is None:
        return None
    ts, data = _cache
    ttl = UPDATE_CACHE_TTL_SECONDS if data.error_message is None else UPDATE_FAIL_TTL_SECONDS
    return data if now - ts < ttl else None


def clear_update_cache() -> None:
    """清空更新检查缓存（测试隔离用）。"""
    global _cache
    _cache = None


def _fetch_manifest(url: str, timeout: float) -> dict:
    """同步语义的外呼由调用方放在 to_thread 里执行；这里只做请求与 JSON 校验。"""
    response = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={
            # 官网按该头精确匹配版本记录判定通道（stable/beta/test），只回本通道最新
            "X-LLMPod-Version": llmpod.__version__,
            # 站点访问日志按 UA 统计，标识产品便于区分真实客户端
            "User-Agent": f"llmpod/{llmpod.__version__}",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("manifest 顶层必须是 JSON 对象")
    return payload


def _parse_iso_datetime(value: object) -> datetime | None:
    """解析 manifest 的发布时间（ISO 8601）；缺失/非法返回 None，不影响检查结果。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("manifest published_at 不是合法 ISO 8601 时间: %r", value)
        return None


async def check_update() -> UpdateCheckData:
    """检查更新：比较当前版本与 manifest 声明的最新版本。"""
    global _cache

    # 官网更新检查端点（产品级固定值）：按 X-LLMPod-Version 头返回请求方所在
    # 通道的最新版。写死在代码里，不做部署配置、不提供可覆盖它的变量。
    url = "https://llmpod.ai/api/version/latest"

    fresh = _fresh_cache(time.monotonic())
    if fresh is not None:
        return fresh

    current = llmpod.__version__
    last_checked = _cache[1].checked_at if _cache is not None else None
    async with _fetch_lock:
        # 拿到锁后再看一眼缓存：等锁期间别的协程可能已刷新
        fresh = _fresh_cache(time.monotonic())
        if fresh is not None:
            return fresh

        try:
            payload = await asyncio.to_thread(_fetch_manifest, url, UPDATE_FETCH_TIMEOUT_SECONDS)
            candidate = str(payload.get("latest_version", "")).strip()
            parsed = parse_version(candidate)
            if not candidate or parsed is None:
                raise ValueError(f"manifest 缺少合法的 latest_version 字段: {candidate!r}")
            # 归一展示形态（去 v 前缀），与内部版本号风格保持一致
            candidate = candidate.lstrip("vV")
        except (httpx.HTTPError, ValueError) as exc:
            # 失败走负缓存：上游故障期避免逐请求吃满超时；无更新语义不变
            log.warning("更新源检查失败(%s): %s", url, exc)
            result = _no_update(
                current,
                checked_at=last_checked,
                error="更新源不可达或响应格式不正确，请稍后重试",
            )
        else:
            # 当前版本解析不成形时按无更新处理（内置常量正常不会走到这）
            parsed_current = parse_version(current)
            has_update = parsed_current is not None and parsed > parsed_current
            result = UpdateCheckData(
                current_version=current,
                latest_version=candidate if has_update else current,
                has_update=has_update,
                update_check_enabled=True,
                checked_at=now_utc(),
                notes=(str(payload["notes"]) if payload.get("notes") else None),
                release_url=(str(payload["release_url"]) if payload.get("release_url") else None),
                published_at=_parse_iso_datetime(payload.get("published_at")),
                # 类型只描述「新版本」：无更新/回显态（描述的是当前版本）不带值
                release_type=(str(payload["release_type"]) if has_update and payload.get("release_type") else None),
            )

    _cache = (time.monotonic(), result)
    return result


# ---------------------------------------------------------------------------
# 启动版本对账：检测「本次启动是否发生了升级」
#
# 部署机由用户自己 docker pull + up -d 升级，容器内无法干预宿主机——能做的是
# 每次启动（entrypoint init_db）把库里记录的上次运行版本与当前镜像版本对账：
# 升级/降级写审计日志（admin 可见），SystemConfig 持久化当前版本供下次对账。
# ---------------------------------------------------------------------------

# SystemConfig 存储 key；value = {"version": ..., "recorded_at": ..., "previous_version": ...}
BOOT_VERSION_KEY = "system_version"

# 启动对账方向
BootDirection = Literal["initial", "same", "upgrade", "downgrade"]


class BootVersion(BaseModel):
    """本次启动的版本对账结果。"""

    current_version: str
    previous_version: str | None = None
    direction: BootDirection
    recorded_at: datetime


def _compare_direction(previous: str | None, current: str) -> BootDirection:
    """按日历版本号比较启动方向；旧记录缺失/不可解析时按首次或升级处理。"""
    if previous is None:
        return "initial"
    if previous == current:
        return "same"
    prev_parsed = parse_version(previous)
    cur_parsed = parse_version(current)
    if prev_parsed is not None and cur_parsed is not None and prev_parsed > cur_parsed:
        return "downgrade"
    # 无法比较语义时（历史脏数据）也按升级对待：版本串已变化
    return "upgrade"


async def record_boot_version(db: AsyncSession) -> BootVersion:
    """对账并落库：上次运行版本 vs 当前镜像版本。

    幂等：每次启动调用，写入的记录总是当前版本（previous_version 留档）。
    """
    now = now_utc()
    current = llmpod.__version__

    stmt = select(SystemConfig).where(SystemConfig.key == BOOT_VERSION_KEY)
    config = (await db.execute(stmt)).scalar_one_or_none()
    previous = str(config.value.get("version")) if config is not None else None
    direction = _compare_direction(previous, current)

    new_value = {
        "version": current,
        "recorded_at": now.isoformat(),
        "previous_version": previous,
    }
    if config is not None:
        config.value = new_value
    else:
        db.add(SystemConfig(key=BOOT_VERSION_KEY, value=new_value, description="最近运行版本"))
    await db.flush()

    return BootVersion(
        current_version=current,
        previous_version=previous,
        direction=direction,
        recorded_at=now,
    )
