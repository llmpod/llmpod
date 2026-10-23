"""安全工具单元测试。"""

from __future__ import annotations

from llmpod.core.security import (
    hash_password,
    verify_password,
    create_token,
    hash_token,
)


class TestPasswordHashing:
    """测试密码哈希和验证。"""

    def test_hash_password_produces_hash(self):
        """测试 hash_password 生成哈希值。"""
        password = "test123"
        hashed = hash_password(password)

        assert hashed != password
        assert len(hashed) > len(password)

    def test_verify_password_correct(self):
        """测试验证正确的密码。"""
        password = "test123"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        """测试验证错误的密码。"""
        password = "test123"
        hashed = hash_password(password)

        assert verify_password("wrong", hashed) is False

    def test_hash_different_each_time(self):
        """测试相同密码每次哈希结果不同（盐值）。"""
        password = "test123"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        assert hash1 != hash2  # 不同的盐值
        assert verify_password(password, hash1)
        assert verify_password(password, hash2)


class TestTokenCreation:
    """测试不透明令牌（opaque token）的生成。"""

    def test_create_token_returns_str(self):
        """测试 create_token 返回字符串。"""
        token = create_token()

        assert isinstance(token, str)
        assert len(token) > 0

    def test_create_token_is_url_safe(self):
        """测试令牌只包含 URL 安全字符（可直接放入 Header / URL）。"""
        token = create_token()

        # secrets.token_urlsafe 仅产生 [A-Za-z0-9_-] 字符
        assert all(c.isalnum() or c in "-_" for c in token)

    def test_create_token_unique(self):
        """测试每次生成的令牌互不相同（随机性）。"""
        tokens = {create_token() for _ in range(100)}

        assert len(tokens) == 100  # 100 次调用都应得到不同的令牌

    def test_create_token_sufficient_entropy(self):
        """测试令牌具备足够的熵（长度）。"""
        token = create_token()

        # secrets.token_urlsafe(32) 生成 ~43 个字符的 base64 串，
        # 对应 256 位熵，远高于安全下限。
        assert len(token) >= 32

    def test_create_token_is_opaque(self):
        """测试 create_token 生成的是不透明令牌（随机字符串，不携带信息）。

        会话信息由调用方写入 Redis，过期由 Redis TTL 管理。
        """
        token = create_token()

        # 令牌只是随机字符串，不应是可解码的结构化数据
        assert "." not in token  # 非分段分隔的结构化字符串


class TestTokenHashing:
    """测试用于 Redis 存储的令牌哈希。"""

    def test_hash_token_produces_hash(self):
        """测试 hash_token 生成哈希值。"""
        token = create_token()
        hashed = hash_token(token)

        assert hashed != token
        assert len(hashed) == 64  # SHA256 十六进制摘要

    def test_hash_token_deterministic(self):
        """测试相同令牌的哈希结果一致。"""
        token = "test-token"
        hash1 = hash_token(token)
        hash2 = hash_token(token)

        assert hash1 == hash2

    def test_hash_different_tokens_different(self):
        """测试不同令牌产生不同的哈希值。"""
        token1 = "token-1"
        token2 = "token-2"

        hash1 = hash_token(token1)
        hash2 = hash_token(token2)

        assert hash1 != hash2
