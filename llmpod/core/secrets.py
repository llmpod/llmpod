"""source_auth 应用层加解密 + secrets 装配。

``Model.source_auth_encrypted`` 存密文（应用层对称加密，密钥来自 ``settings.secret_key``）；
API 永不回显。``PlacementService`` 下发 ENSURE_MODEL 时解密 → 装配成 ``secrets`` dict 随 RPC
内存传递，**不入 task.payload / task_event / node 本地表 / 日志**。

实现：stdlib only（无 cryptography 依赖）。HMAC-SHA256 keystream（CTR 风格）+ HMAC 认证 →
认证加密语义。保护对象是 API Key 明文（``ApiKey.key_plaintext_encrypted``）与模型源站凭证
（``Model.source_auth_encrypted``）——密钥文件（默认 ``data/secret_key``）随数据卷一起
备份与限权，泄露即等于这些凭据泄露；同一 ``secret_key`` 还用于派生下载令牌与节点引导
令牌的 HMAC 签名密钥（``download_token`` / ``bootstrap_token``），泄露后可伪造这两类
令牌。如需更高强度可换 Fernet（加 ``cryptography`` 依赖），本模块对外接口
（encrypt/decrypt/build_secrets）不变。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets as pysecrets
from pathlib import Path

from llmpod.config.enums import ModelSource
from llmpod.config.settings import settings

log = logging.getLogger("llmpod.core.secrets")

_NONCE_LEN = 16
_MAC_LEN = 32


def ensure_secret_key(config=settings) -> str:
    """加载或独占创建持久化密钥；空/不可读文件一律拒绝启动和加密。"""
    if config.secret_key:
        return config.secret_key
    path = Path(config.secret_key_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_name(path.name + "." + pysecrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(pysecrets.token_urlsafe(48))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # 仅第一位创建者成功，完整文件才可见
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                log.warning("已生成新的持久化加密密钥：%s；请随数据备份并限制读取权限", path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    value = path.read_text().strip()
    if not value:
        raise RuntimeError(f"加密密钥为空，无法启动：{path}")
    config.secret_key = value
    return value


def _key() -> bytes:
    return hashlib.sha256(ensure_secret_key().encode()).digest()


def encrypt(plain: str) -> str:
    """明文 → 密文（base64）。空串返回空串。"""
    if not plain:
        return ""
    data = plain.encode()  # 按「字节」长度生成密钥流（len(plain) 是字符数，多字节会截断）
    nonce = os.urandom(_NONCE_LEN)
    cipher = _xor(data, _keystream(_key(), nonce, len(data)))
    mac = hmac.new(_key(), nonce + cipher, hashlib.sha256).digest()
    return base64.b64encode(nonce + mac + cipher).decode()


def decrypt(token: str) -> str:
    """密文 → 明文。空/None 返回空串；校验失败抛 ValueError。"""
    if not token:
        return ""
    try:
        raw = base64.b64decode(token.encode())
    except Exception as e:
        raise ValueError("source_auth 密文非合法 base64") from e
    if len(raw) < _NONCE_LEN + _MAC_LEN:
        raise ValueError("source_auth 密文长度不足")
    nonce, mac, cipher = raw[:_NONCE_LEN], raw[_NONCE_LEN : _NONCE_LEN + _MAC_LEN], raw[_NONCE_LEN + _MAC_LEN :]
    expected = hmac.new(_key(), nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("source_auth HMAC 校验失败（密钥不符或密文损坏）")
    return _xor(cipher, _keystream(_key(), nonce, len(cipher))).decode()


def build_secrets(model) -> dict[str, str] | None:
    """按 ``model.source`` 把 ``source_auth_encrypted`` 解密装配成 secrets dict。

    返回 ``None`` 表示无凭证（HF public / local）。仅 HF-first V1：huggingface → ``{hf_token}``。
    解密失败记 warning 并返回 None（不下发坏凭证，由 node 按 public 处理或失败）。
    """
    enc = getattr(model, "source_auth_encrypted", None)
    if not enc:
        return None
    try:
        plain = decrypt(enc)
    except ValueError as e:
        log.warning("build_secrets: 解密失败（model_id=%s）：%s", getattr(model, "id", "?"), e)
        return None
    if not plain:
        return None
    source = (getattr(model, "source", None) or "").lower()
    if ModelSource.is_hf_source(source):
        return {"hf_token": plain}
    # 其他 source（oss/modelscope）凭证形态尚未冻结，原样放 token（细分待定）。
    return {"token": plain}


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 迭代生成 n 字节密钥流（CTR 风格）。"""
    out = bytearray()
    counter = 0
    while len(out) < n:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:n])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))
