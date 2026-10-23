"""ORM 基类：DeclarativeBase + 通用混入类。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


class IntIdMixin:
    """自增整数主键混入类。

    所有表都继承此混入类，使用 `id` 作为主键列名。
    gpu 表还有一个 `uuid` 唯一列（GPU 硬件 UUID），但仍使用整数主键。
    """

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键ID（自增）")


class TimestampMixin:
    """created_at / updated_at 时间戳混入类。

    ``eager_defaults`` 让 INSERT/UPDATE 走 RETURNING，把 server_default / onupdate
    生成的 created_at / updated_at 一次读回 Python 对象——flush 后即可直接序列化，
    无需显式 refresh（否则 onupdate 列在 flush 后处于 expired，序列化触发懒加载 → MissingGreenlet）。
    """

    __mapper_args__ = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间（ISO8601，带时区）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间（ISO8601，带时区）",
    )
