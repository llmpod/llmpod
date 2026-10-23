"""SystemConfig ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base


class SystemConfig(Base):
    """键值对系统配置存储。

    主键为 VARCHAR(100) 的键字符串，非自增。
    """

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(
        String(100), primary_key=True, autoincrement=False, comment="配置键名（字符串主键）"
    )
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="配置值（JSON 对象）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="配置说明（可空）")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间（ISO8601，带时区）",
    )
    # 逻辑外键 → user.id
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="最后更新人用户ID（可空）")

    def __repr__(self) -> str:
        return f"<SystemConfig key={self.key!r}>"
