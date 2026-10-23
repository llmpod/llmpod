"""时间工具函数。"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """获取当前 UTC 时间，返回带时区信息的 datetime。"""
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    """获取系统本地时区当前时间（aware；部署期 TZ 决定本地语义）。"""
    return datetime.now().astimezone()


def as_utc(dt: datetime | None) -> datetime | None:
    """naive 时间按 UTC 补时区，None 透传。

    DB 时间列是 timestamptz（读出即 aware），naive 入参（FastAPI 对无偏移 ISO 串
    的解析产物）与之直接比较会抛 TypeError，入口处先归一。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def format_datetime(dt: datetime | None) -> str | None:
    """将 datetime 格式化为 ISO 8601 字符串。"""
    if dt is None:
        return None
    return dt.isoformat()


def ms_to_datetime(ms: int | None) -> datetime | None:
    """毫秒时间戳 → UTC aware datetime（epoch 语义无时区歧义，前端统一传 ms）。"""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
