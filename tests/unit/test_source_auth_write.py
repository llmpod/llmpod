"""source_auth 加解密 + ModelOut 不回显（安全属性）。"""

from __future__ import annotations

from datetime import datetime, timezone

from llmpod.apps.webserver.schemas.out_models import ModelOut
from llmpod.core.secrets import decrypt, encrypt
from llmpod.models.model import Model


def test_encrypt_decrypt_roundtrip():
    ct = encrypt("hf_live_abc123")
    assert ct and ct != "hf_live_abc123"
    assert decrypt(ct) == "hf_live_abc123"


def test_modelout_never_leaks_source_auth():
    """配置了凭证：ModelOut 不含 source_auth_encrypted 字段。"""
    m = Model(name="x", model_type="llm", source="huggingface")
    m.id = 1
    m.is_moe = False
    m.is_vision = False
    m.needs_trust_remote_code = False
    m.source_kind = "unknown"
    m.hash_mode = "none"
    m.source_auth_encrypted = "ENCRYPTED_CIPHERTEXT"
    now = datetime.now(timezone.utc)
    m.created_at = now
    m.updated_at = now

    dump = ModelOut.model_validate(m).model_dump()
    assert "source_auth_encrypted" not in dump  # 密文/明文均不外泄
