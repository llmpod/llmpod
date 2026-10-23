"""短期节点引导令牌：绑定集群，允许目标机修改安装参数。"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256


def bootstrap_signing_key(config) -> str:
    """与节点共享凭据隔离的引导签名密钥。"""
    from llmpod.core.secrets import ensure_secret_key

    return hmac.new(ensure_secret_key(config).encode(), b"llmpod/bootstrap/v2", sha256).hexdigest()


def _b64u(raw: bytes) -> str:
    """base64url 编码（去掉填充 ``=``，URL 安全）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_bootstrap_token(node_token: str, cluster_id: int, ttl: int) -> tuple[str, int]:
    """签发引导令牌。

    Args:
        node_token: HMAC 签名密钥（调用方传入用途隔离后的密钥）。
        cluster_id: 绑定的集群 ID（验签时会再比对，防跨集群挪用）。
        ttl: 有效期（秒）。

    Returns:
        ``(token, expires_at_unix)``。
    """
    exp = int(time.time()) + int(ttl)
    payload = {"cid": int(cluster_id), "exp": exp}
    payload_raw = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = _b64u(payload_raw)
    sig = _b64u(hmac.new(node_token.encode(), payload_b64.encode(), sha256).digest())
    return f"{payload_b64}.{sig}", exp


def verify_bootstrap_token(node_token: str, token: str, cluster_id: int) -> bool:
    """验签、查过期并校验集群。

    任一不符（格式错、签名不符、已过期、cluster_id 不匹配）均返回 False；
    成功返回 True。签名比对用 ``hmac.compare_digest`` 防时序攻击。
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return False

    expected = _b64u(hmac.new(node_token.encode(), payload_b64.encode(), sha256).digest())
    if not hmac.compare_digest(expected, sig_b64):
        return False

    try:
        data = json.loads(_b64u_decode(payload_b64))
        cid = int(data["cid"])
        exp = int(data["exp"])
    except (ValueError, KeyError, TypeError):
        return False

    if cid != int(cluster_id):
        return False
    if exp < time.time():
        return False
    return True


__all__ = ["mint_bootstrap_token", "verify_bootstrap_token", "bootstrap_signing_key"]
