"""bootstrap_token HMAC 工具单测（纯逻辑，无 HTTP）。"""

from __future__ import annotations

from llmpod.apps.webserver.utils.bootstrap_token import (
    mint_bootstrap_token,
    verify_bootstrap_token,
)


def test_roundtrip_binds_cluster_and_returns_exp():
    token, exp = mint_bootstrap_token("cluster-secret", 7, 3600)
    assert "." in token  # payload.sig
    assert isinstance(exp, int) and exp > 0
    assert verify_bootstrap_token("cluster-secret", token, 7) is True


def test_wrong_signing_key_fails():
    token, _ = mint_bootstrap_token("cluster-secret", 1, 3600)
    assert verify_bootstrap_token("other-key", token, 1) is False


def test_wrong_cluster_id_fails():
    """令牌绑定 cluster_id，跨集群挪用必须失败。"""
    token, _ = mint_bootstrap_token("k", 1, 3600)
    assert verify_bootstrap_token("k", token, 2) is False


def test_tampered_signature_fails():
    token, _ = mint_bootstrap_token("k", 1, 3600)
    assert verify_bootstrap_token("k", token[:-2] + "aa", 1) is False


def test_malformed_token_fails():
    assert verify_bootstrap_token("k", "not-a-token", 1) is False
    assert verify_bootstrap_token("k", "", 1) is False
    assert verify_bootstrap_token("k", "onlyonepart", 1) is False


def test_expired_token_fails():
    """mint 接受负 ttl → exp 已在过去 → 验签判过期。"""
    token, _ = mint_bootstrap_token("k", 1, -100)
    assert verify_bootstrap_token("k", token, 1) is False


def test_token_is_reusable_within_its_cluster():
    token, _ = mint_bootstrap_token("k", 1, 3600)
    assert verify_bootstrap_token("k", token, 1)
    assert verify_bootstrap_token("k", token, 1)
    assert not verify_bootstrap_token("k", token, 2)
