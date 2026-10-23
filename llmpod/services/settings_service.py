"""平台全局设置服务：品牌 / 全局主题 / 登录页定制 / 维护模式。

平台级一份（三端 SPA 共用），存 ``SystemConfig`` 单 key ``"settings"``，
value 为一层扁平 JSON 对象（camelCase 字段名即对外 API 契约）。

读写两侧校验口径分离：
- 读（``sanitize_settings``）容忍任意来源（通用 config 接口可写入任意结构），非法字段静默丢弃；
- 写（``update_settings``）对**本次传入**的每个值严格校验，非法即 ``raise``
  ``LLMPodValidationError``（422）——绝不静默清空已存的合法值（如 5 位 hex 手误
  直接把整站主色清掉属数据事故）。``None`` / 空串仍是合法的「清空」语义。

读取走进程内 TTL 缓存（index.html 种子注入每请求都要读，不能逐请求打库）；
DB 读失败时保留缓存旧值兜底（无缓存则按未配置处理），不让全局设置故障阻塞页面。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import DEFAULT_MAINTENANCE_MESSAGE, LLMPodValidationError
from llmpod.models.system_config import SystemConfig

log = logging.getLogger("llmpod.services.settings")

# SystemConfig 存储 key
SETTINGS_KEY = "settings"

# 3/6 位 hex 主色（themeColor / loginPrimary）
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# mode 合法值（前端不消费——明暗为纯本地偏好；字段与校验预留）
_VALID_MODES = frozenset({"light", "dark", "auto"})
# 显式 scheme 前缀（logo / loginBackground 只放行 http/https/data:image）
_URLISH_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# 带 scheme 的字段：其余文本字段无注入面，不套此闸
_SCHEME_FIELDS = frozenset({"logo", "loginBackground"})

# 各文本字段限长（字符）
_MAX_LEN = {
    "siteName": 100,
    "siteDescription": 500,
    "platformName": 100,
    "logo": 64 * 1024,
    "footerText": 200,
    "themeColor": 7,
    "mode": 6,
    "loginBackground": 256 * 1024,
    "loginPrimary": 7,
    "maintenanceMessage": 200,
}

# 进程内 TTL 缓存：写后失效，多 worker 各自缓存（更新最多延迟一个 TTL 可见）
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0


def clear_settings_cache() -> None:
    """清空全局设置缓存（写路径与测试隔离用）。"""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


class SettingsPayload(BaseModel):
    """全局设置载荷（camelCase 字段名即对外 API 契约）。

    按消费域分四组。品牌 / 主题两组随 index.html 种子注入 localStorage
    （品牌 → ``app_brand``，主题 → ``app_remote_theme``），让配置全员首屏生效。
    除 ``maintenanceMode`` 外均可空：``None`` / 空串 = 未配置，前端回落各自默认值；
    限长与格式约束见上方 ``_MAX_LEN`` 等常量表（勿在注释里重复，防漂移）。
    """

    # 品牌基本信息（顶栏 / 页脚 / 浏览器标题等站点级展示）
    siteName: str | None = None  # 站点名（标题等站点级标识）
    siteDescription: str | None = None  # 站点描述（预留 SEO meta 用）
    platformName: str | None = None  # 平台显示名（顶栏品牌位文案）
    logo: str | None = None  # logo：http(s)/data:image URL、/ 开头相对路径或 emoji 文本均可
    footerText: str | None = None  # 页脚文案；空则前端回落默认「©2026 LLMPod项目」

    # 全站主题；themeColor 注入 localStorage 时映射为槽内 primary 键
    themeColor: str | None = None  # 全站主色，#rgb / #rrggbb hex
    mode: str | None = None  # 明暗三态 light/dark/auto；前端不消费（明暗为纯本地偏好），预留

    # 登录页定制（只影响登录页，不动站内主题）
    loginBackground: str | None = None  # 登录页背景：图片 URL / data:image / 纯色皆可
    loginPrimary: str | None = None  # 登录页主色 hex；空则前端回落登录页专属默认色

    # 维护模式：开启后登录返回 503，网关租户 Key 全拒（平台直发 Key 放行）
    maintenanceMode: bool = False
    maintenanceMessage: str | None = None  # 维护提示文案；空则回落默认文案


def _safe_url_or_text(value: str) -> bool:
    """URL 字段的 scheme 闸：带显式 scheme 只放行 http(s)/data:image，其余拒绝。

    - 无 scheme 前缀（相对路径 ``/x``、协议相对 ``//host``、emoji 文本）→ 放行；
    - ``javascript:`` / ``vbscript:`` / ``data:text/html`` 等 → 拒绝（存储型 XSS 面）。
    """
    if not _URLISH_SCHEME_RE.match(value):
        return True
    return value.startswith(("http://", "https://", "//")) or value.startswith("data:image/")


def _field_invalid_reason(name: str, raw: str) -> str | None:
    """非空字符串 ``raw`` 在字段 ``name`` 下的非法原因；合法返回 None。

    写路径据此 raise（拒非法值），读路径据此丢弃（sanitize 容忍存量脏数据）。
    """
    if len(raw) > _MAX_LEN.get(name, 0):
        return f"长度超过 {_MAX_LEN[name]} 字符"
    if name in ("themeColor", "loginPrimary") and not _HEX_COLOR_RE.match(raw):
        return "需 3/6 位 hex 主色（如 #1a2b3c）"
    if name == "mode" and raw not in _VALID_MODES:
        return "mode 仅支持 light/dark/auto"
    if name in _SCHEME_FIELDS and not _safe_url_or_text(raw):
        return "仅支持 http(s)/data:image URL 或普通文本"
    return None


def sanitize_settings(value: Any) -> SettingsPayload:
    """白名单清洗任意来源的全局设置（DB 值 / 通用 config 接口写入的结构）。

    - 未知键丢弃；类型不匹配丢弃；``None`` / 空串统一留空；
    - ``themeColor`` / ``loginPrimary`` 非 3/6 位 hex、``mode`` 非法、超限长、
      URL 字段带危险 scheme → 丢弃（读侧容忍脏数据，不炸前端）。
    """
    if not isinstance(value, dict):
        return SettingsPayload()
    cleaned: dict[str, Any] = {}
    for name in SettingsPayload.model_fields:
        if name not in value or value[name] is None:
            continue
        raw = value[name]
        if name == "maintenanceMode":
            if isinstance(raw, bool):
                cleaned[name] = raw
            continue
        if not isinstance(raw, str) or raw == "":
            continue
        if _field_invalid_reason(name, raw) is not None:
            continue
        cleaned[name] = raw
    return SettingsPayload(**cleaned)


async def _get_settings_row(db: AsyncSession) -> SystemConfig | None:
    """读全局设置行（缓存回填 / 更新合并基 / 管理端元数据共用的一次点查）。"""
    return (await db.execute(select(SystemConfig).where(SystemConfig.key == SETTINGS_KEY))).scalar_one_or_none()


def _is_cache_valid(now: float) -> bool:
    return _CACHE_TTL_SECONDS > 0 and _cache_ts > 0 and (now - _cache_ts) < _CACHE_TTL_SECONDS


async def _load_settings(db: AsyncSession) -> dict | None:
    """读当前 value（未配置返回 None）。异常向上抛由调用方兜底。"""
    row = await _get_settings_row(db)
    return row.value if row is not None else None


async def _fetch_settings(db: AsyncSession) -> tuple[dict[str, Any], bool]:
    """带 TTL 缓存的全局设置读取，返回 ``(清洗后的值, 是否可信)``。

    DB 异常时：有缓存用缓存旧值（``ok=True``，最多延迟一个 TTL 可见）；
    无缓存返回 ``({}, False)``——调用方据此决定按未配置处理还是跳过动作。
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if _is_cache_valid(now):
        assert _cache is not None
        return (_cache, True)
    try:
        value = await _load_settings(db)
        _cache = sanitize_settings(value).model_dump()
        _cache_ts = now
        return (_cache, True)
    except Exception as exc:
        if _cache is not None:
            log.warning("读取全局设置失败，回落缓存旧值：%s", exc)
            _cache_ts = now
            return (_cache, True)
        log.warning("读取全局设置失败且无缓存，按未配置处理：%s", exc)
        return ({}, False)


async def get_settings(db: AsyncSession) -> SettingsPayload:
    """读当前全局设置（TTL 缓存 + DB 失败回落缓存旧值）。

    DB 异常且无缓存时按未配置（全空 SettingsPayload）返回，不让全局设置读取故障打死请求。
    """
    data, ok = await _fetch_settings(db)
    return SettingsPayload(**data) if ok else SettingsPayload()


async def get_settings_or_none(db: AsyncSession) -> SettingsPayload | None:
    """同 :func:`get_settings`，但 DB 失败且无缓存时返回 ``None``。

    供 index.html 种子注入区分「未配置（注入清键脚本）」与「读失败
    （跳过注入，保留浏览器上一次的缓存主题）」。
    """
    data, ok = await _fetch_settings(db)
    return SettingsPayload(**data) if ok else None


async def get_settings_meta(db: AsyncSession) -> tuple[SettingsPayload, int | None, datetime | None]:
    """管理端读：payload + 更新元数据（同源一次点查，不走 TTL——回显即最新）。"""
    row = await _get_settings_row(db)
    payload = sanitize_settings(row.value if row is not None else None)
    if row is None:
        return payload, None, None
    return payload, row.updated_by, row.updated_at


class MaintenanceState(BaseModel):
    """维护模式判定结果（login 拦截与网关共用口径的字段子集）。"""

    enabled: bool = False
    message: str = ""


async def get_maintenance(db: AsyncSession) -> MaintenanceState:
    """维护模式当前态（走全局设置缓存，message 缺省回落默认文案）。"""
    settings = await get_settings(db)
    return MaintenanceState(
        enabled=settings.maintenanceMode,
        message=settings.maintenanceMessage or DEFAULT_MAINTENANCE_MESSAGE,
    )


def _raise_invalid(name: str, raw: Any, reason: str) -> None:
    raise LLMPodValidationError(f"settings.{name} 取值非法：{reason}（收到 {raw!r}）")


async def update_settings(
    db: AsyncSession,
    payload: dict[str, Any],
    updated_by: int,
) -> SettingsPayload:
    """三态部分更新全局设置并落库（事务由调用方 commit）。

    ``payload`` 应为调用方 ``model_dump(exclude_unset=True)`` 的产物：

    - 键缺省 → 保持库中值不动；
    - 键为 ``None`` / 空串 → 清空该字段（回落前端默认）；
    - 键有值 → 覆写；**非法值 raise 422**（非 hex 主色 / 非法 mode / 危险 scheme /
      超长），绝不静默清空已存的合法值。

    合并基 = 库中当前行（非 TTL 缓存），避免并发/近时更新以过期值互相覆盖。
    本函数只 ``flush``，``commit`` 归路由层（services 不持有请求域事务边界）。
    """
    row = await _get_settings_row(db)
    base = sanitize_settings(row.value if row is not None else None)

    merged = base.model_dump()
    for name in SettingsPayload.model_fields:
        if name not in payload:
            continue
        raw = payload[name]
        if name == "maintenanceMode":
            if not isinstance(raw, bool):
                _raise_invalid(name, raw, "需布尔值")
            merged[name] = raw
            continue
        # 文本字段三态：null / 空串 = 清空；非空 = 严格校验后覆写
        if raw is None or raw == "":
            merged[name] = None
            continue
        if not isinstance(raw, str):
            _raise_invalid(name, raw, "需字符串")
        reason = _field_invalid_reason(name, raw)
        if reason is not None:
            _raise_invalid(name, raw, reason)
        merged[name] = raw

    if row is None:
        row = SystemConfig(
            key=SETTINGS_KEY,
            value=merged,
            description="平台设置",
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.value = merged
        row.updated_by = updated_by
    await db.flush()
    clear_settings_cache()
    return SettingsPayload(**merged)
