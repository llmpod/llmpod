"""core.secrets 单测：加解密往返 + build_secrets 按 source 装配（HF-first V1）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmpod.core import secrets as secrets_mod


class TestEncryptDecrypt:
    def test_roundtrip(self):
        for plain in ["hf_xxx", "a", "中文凭证", "x" * 1000]:
            assert secrets_mod.decrypt(secrets_mod.encrypt(plain)) == plain

    def test_empty_passthrough(self):
        assert secrets_mod.encrypt("") == ""
        assert secrets_mod.decrypt("") == ""
        assert secrets_mod.decrypt(None) == ""

    def test_ciphertext_differs_from_plaintext(self):
        enc = secrets_mod.encrypt("hf_secret")
        assert enc != "hf_secret"
        assert "hf_secret" not in enc  # 明文不外泄

    def test_tamper_detected(self):
        enc = secrets_mod.encrypt("hf_secret")
        # 翻转密文末位一字节
        import base64

        raw = bytearray(base64.b64decode(enc))
        raw[-1] ^= 0xFF
        tampered = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(ValueError):
            secrets_mod.decrypt(tampered)

    def test_wrong_key_fails(self, monkeypatch):
        enc = secrets_mod.encrypt("hf_secret")
        monkeypatch.setattr(secrets_mod.settings, "secret_key", "different-key")
        with pytest.raises(ValueError):
            secrets_mod.decrypt(enc)


class TestBuildSecrets:
    def test_none_when_no_auth(self):
        m = SimpleNamespace(id=1, source="huggingface", source_auth_encrypted=None)
        assert secrets_mod.build_secrets(m) is None

    def test_empty_auth_returns_none(self):
        m = SimpleNamespace(id=1, source="huggingface", source_auth_encrypted="")
        assert secrets_mod.build_secrets(m) is None

    def test_huggingface_maps_to_hf_token(self):
        token = "hf_abc123"
        m = SimpleNamespace(
            id=1,
            source="huggingface",
            source_auth_encrypted=secrets_mod.encrypt(token),
        )
        assert secrets_mod.build_secrets(m) == {"hf_token": token}

    def test_other_source_falls_back_to_token(self):
        token = "oss_key:secret"
        m = SimpleNamespace(
            id=7,
            source="oss",
            source_auth_encrypted=secrets_mod.encrypt(token),
        )
        assert secrets_mod.build_secrets(m) == {"token": token}

    def test_corrupt_auth_returns_none(self):
        m = SimpleNamespace(id=1, source="huggingface", source_auth_encrypted="!!!notb64!!!")
        assert secrets_mod.build_secrets(m) is None
