"""local 源下载令牌：HMAC 签名（无状态、可重复用）。

用途：Manager 在下发 local 源 ENSURE_MODEL 任务时签发一个绑定 ``model_id``、带过期时间的
令牌，Node 用它访问 Manager 文件服务的 manifest / file 端点。Node 无用户会话，凭此
令牌鉴权。TTL 默认 24 小时——令牌每次 attempt 下发时重铸，任务执行受 12h 看门狗上限
约束，单窗口内不存在过期问题。

签名密钥由部署密钥按下载用途独立派生；泄漏的集群 Node Token 不能伪造下载令牌。
令牌有效期内可重复使用（无状态、不依赖 Redis）。

形态：``base64url(payload_json).base64url(hmac_sha256)``，
payload = ``{"mid": model_id, "exp": unix_ts}``。
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

from llmpod.config.settings import settings


def download_signing_key(config=settings) -> str:
    """模型下载签名域独立于集群共享令牌及引导签名域。"""
    from llmpod.core.secrets import ensure_secret_key

    return hmac.new(ensure_secret_key(config).encode(), b"llmpod/download/v2", sha256).hexdigest()


def _b64u(raw: bytes) -> str:
    """base64url 编码（去掉填充 ``=``，URL 安全）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_download_token(node_token: str, model_id: int, ttl: int) -> tuple[str, int]:
    """签发下载令牌。

    Args:
        node_token: HMAC 签名密钥（调用方传入用途隔离后的密钥）。
        model_id: 绑定的模型 id（只能下该模型的文件）。
        ttl: 有效期（秒），clamp 到 ``download_token_max_ttl``。

    Returns:
        ``(token, expires_at_unix)``。
    """
    ttl = min(int(ttl), int(settings.download_token_max_ttl))  # 只 clamp 上限；允许负值以便测试过期
    exp = int(time.time()) + ttl
    payload_raw = json.dumps({"mid": int(model_id), "exp": exp}, separators=(",", ":")).encode()
    payload_b64 = _b64u(payload_raw)
    sig = _b64u(hmac.new(node_token.encode(), payload_b64.encode(), sha256).digest())
    return f"{payload_b64}.{sig}", exp


def verify_download_token(node_token: str, token: str, model_id: int) -> bool:
    """验签 + 查过期 + 校验 model_id 绑定。

    任一不符（格式错、签名不符、已过期、绑定不匹配）均返回 False；成功返回 True。
    签名比对用 ``hmac.compare_digest`` 防时序攻击。
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return False

    expected = _b64u(hmac.new(node_token.encode(), payload_b64.encode(), sha256).digest())
    try:
        # compare_digest 对非 ASCII/异长输入抛 TypeError——畸形令牌按验签失败处理，
        # 不让公开端点 500
        if not hmac.compare_digest(expected, sig_b64):
            return False
    except TypeError:
        return False

    try:
        data = json.loads(_b64u_decode(payload_b64))
        mid = int(data["mid"])
        exp = int(data["exp"])
    except (ValueError, KeyError, TypeError):
        return False

    if mid != int(model_id):
        return False
    if exp < time.time():
        return False
    return True


__all__ = ["mint_download_token", "verify_download_token", "download_signing_key"]
