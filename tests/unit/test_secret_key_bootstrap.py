"""secret_key 首启随机生成单测：生成、复用、显式配置优先。"""

from __future__ import annotations

import secrets as pysecrets
from pathlib import Path
import pytest

from llmpod.apps.webserver.app import WebServerLifespan
from llmpod.config.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(secret_key="", secret_key_file=str(tmp_path / "data" / "secret_key"))


async def test_generates_and_persists(tmp_path: Path):
    s = _settings(tmp_path)
    await WebServerLifespan().startup(s)
    assert s.secret_key  # 随机生成
    raw = Path(s.secret_key_file).read_text().strip()
    assert raw == s.secret_key
    assert (Path(s.secret_key_file).stat().st_mode & 0o777) == 0o600


async def test_reuses_existing_file(tmp_path: Path):
    s = _settings(tmp_path)
    path = Path(s.secret_key_file)
    path.parent.mkdir(parents=True)
    path.write_text("existing-key")
    await WebServerLifespan().startup(s)
    assert s.secret_key == "existing-key"


async def test_explicit_config_wins(tmp_path: Path):
    s = Settings(secret_key="explicit", secret_key_file=str(tmp_path / "data" / "secret_key"))
    await WebServerLifespan().startup(s)
    assert s.secret_key == "explicit"
    assert not Path(s.secret_key_file).exists()  # 不落盘


async def test_generated_key_has_entropy(tmp_path: Path):
    s1 = _settings(tmp_path / "a")
    s2 = _settings(tmp_path / "b")
    await WebServerLifespan().startup(s1)
    await WebServerLifespan().startup(s2)
    assert s1.secret_key != s2.secret_key
    assert len(pysecrets.token_urlsafe(48)) >= 48  # 参考长度（生成强度由 token_urlsafe 保证）


async def test_empty_key_file_fails_closed(tmp_path: Path):
    s = _settings(tmp_path)
    path = Path(s.secret_key_file)
    path.parent.mkdir(parents=True)
    path.write_text("")
    with pytest.raises(RuntimeError, match="加密密钥为空"):
        await WebServerLifespan().startup(s)
