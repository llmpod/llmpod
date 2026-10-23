"""加密工具函数。"""

from __future__ import annotations

import uuid

from llmpod.core.security import create_token as generate_token

__all__ = ["generate_token", "generate_uuid"]


def generate_uuid() -> str:
    """生成 UUID4 字符串。"""
    return str(uuid.uuid4())
