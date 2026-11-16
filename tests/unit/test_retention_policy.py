"""BodyStore 轮转策略测试（纯决策，无 I/O）。

- 按时间：超保留期全选，未超期不选；
- 按容量：未超上限不动；超限从最旧删到 80% 水位，不设年龄豁免；
- 工厂：mode 配置 → 对应策略类。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llmpod.apps.proxyserver.bodystore import BlockInfo
from llmpod.apps.proxyserver.retention import (
    SizeRetentionPolicy,
    TimeRetentionPolicy,
    build_retention_policy,
)
from llmpod.config.settings import Settings

_NOW = 1_000_000_000.0


def _block(name: str, age_days: float, size: int = 100) -> BlockInfo:
    return BlockInfo(hash=name, mtime=_NOW - age_days * 86400, size=size)


class TestTimePolicy:
    def test_selects_only_expired(self):
        policy = TimeRetentionPolicy(days=7)
        blocks = [_block("a", 10), _block("b", 3), _block("c", 7.5)]

        doomed = policy.select(blocks, now=_NOW)

        assert [b.hash for b in doomed] == ["a", "c"]  # 超期全选（最旧在前），b 留

    def test_empty_when_all_fresh(self):
        policy = TimeRetentionPolicy(days=7)
        assert policy.select([_block("a", 1), _block("b", 2)], now=_NOW) == []


class TestSizePolicy:
    def test_noop_under_limit(self):
        policy = SizeRetentionPolicy(max_bytes=1000)
        blocks = [_block("a", 30, size=400), _block("b", 30, size=400)]

        assert policy.select(blocks, now=_NOW) == []

    def test_drops_oldest_to_watermark(self):
        policy = SizeRetentionPolicy(max_bytes=1000)
        blocks = [
            _block("oldest", 30, size=400),
            _block("middle", 20, size=400),
            _block("newest", 10, size=400),
        ]  # 总量 1200 > 1000，水位 800：删最旧一块（1200-400=800 ≤ 800）即止

        doomed = policy.select(blocks, now=_NOW)

        assert [b.hash for b in doomed] == ["oldest"]

    def test_deletes_recent_blocks_too_when_over_limit(self):
        """纯容量闸：超限时刚写入的块也参与驱逐，不设年龄豁免。"""
        policy = SizeRetentionPolicy(max_bytes=1000)
        blocks = [
            _block("old", 10, size=400),
            _block("new", 0.1, size=900),
        ]  # 总量 1300，水位 800：删 old（1300-400=900 仍超水位）→ 连 new 一起删

        doomed = policy.select(blocks, now=_NOW)

        assert [b.hash for b in doomed] == ["old", "new"]


class TestFactory:
    def test_size_mode_default(self):
        policy = build_retention_policy(Settings(body_retention_mode="size"))
        assert isinstance(policy, SizeRetentionPolicy)

    def test_time_mode(self):
        cfg = Settings(body_retention_mode="time", body_retention_days=7)
        policy = build_retention_policy(cfg)
        assert isinstance(policy, TimeRetentionPolicy)

    def test_invalid_mode_rejected_at_settings(self):
        """拼错的轮转方式在 Settings 装配期即报错（不静默回落 size）。"""
        with pytest.raises(ValidationError):
            Settings(body_retention_mode="bogus")
