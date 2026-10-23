"""download_token HMAC 工具单测（纯逻辑，无 HTTP）。"""

from __future__ import annotations

from llmpod.apps.webserver.utils.download_token import mint_download_token, verify_download_token


def test_roundtrip_binds_model_and_returns_exp():
    token, exp = mint_download_token("node-secret", 42, 1800)
    assert "." in token
    assert isinstance(exp, int) and exp > 0
    assert verify_download_token("node-secret", token, 42) is True


def test_wrong_signing_key_fails():
    token, _ = mint_download_token("node-secret", 42, 3600)
    assert verify_download_token("other-key", token, 42) is False


def test_wrong_model_fails():
    """令牌只能下指定 model_id 的文件。"""
    token, _ = mint_download_token("k", 42, 3600)
    assert verify_download_token("k", token, 99) is False


def test_tampered_and_malformed_fail():
    token, _ = mint_download_token("k", 42, 3600)
    assert verify_download_token("k", token[:-2] + "aa", 42) is False
    assert verify_download_token("k", "not-a-token", 42) is False
    assert verify_download_token("k", "", 42) is False


def test_expired_token_fails():
    """mint 接受负 ttl → exp 已在过去 → 验签判过期。"""
    token, _ = mint_download_token("k", 42, -100)
    assert verify_download_token("k", token, 42) is False
