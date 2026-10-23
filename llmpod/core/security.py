"""安全工具：不透明令牌生成 + bcrypt 密码哈希 + 令牌哈希。

认证采用「不透明令牌 + Redis 缓存」方案：令牌本身是一个不可预测的
随机字符串，不携带任何用户信息；用户的会话信息存储在 Redis 中，
以 hash_token(token) 为键，过期由 Redis 的 TTL（token_expire_seconds）
统一管理。
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt


def hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码是否与其 bcrypt 哈希值匹配。"""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_token() -> str:
    """生成一个密码学安全的不透明令牌（opaque token）。

    令牌不包含任何 payload，也不需要签名/解码。用户会话数据由调用方
    写入 Redis（以 hash_token(token) 为键），令牌的过期完全由 Redis
    的 TTL 控制。
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """对令牌进行哈希，用作 Redis 存储键（避免存储/暴露原始令牌）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
