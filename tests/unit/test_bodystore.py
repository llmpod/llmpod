"""LocalFileBodyStore 测试（内容寻址块存储）。

- put：SHA-256 寻址，gzip 落盘 blocks/{hash前2位}/{hash}.gz，幂等去重；
- get：解压还原原文；未知哈希 → None；
- list_blocks / remove_blocks：策略中立的清点与带复核的删除，「该不该删」
  的判断不在存储层（见 retention.py 与 test_retention_policy.py）。
"""

from __future__ import annotations

import gzip
import os
import time

from llmpod.apps.proxyserver.bodystore import BlockInfo, LocalFileBodyStore


class TestPutAndGet:
    def test_put_returns_hash_and_writes_gzip_block(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        content = '{"messages": ["hello 世界"]}'

        h = store.put(content)

        assert len(h) == 64 and int(h, 16) >= 0  # sha256 hex
        block = tmp_path / "blocks" / h[:2] / f"{h}.gz"
        assert block.is_file()
        with gzip.open(block, "rb") as f:
            assert f.read() == content.encode()

    def test_put_idempotent_same_content_same_hash(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        h1 = store.put("same content")
        block = tmp_path / "blocks" / h1[:2] / f"{h1}.gz"
        old = block.stat().st_mtime_ns - 3600
        os.utime(block, ns=(old, old))  # 拉回旧时间，验证复用会刷新保留期计时

        h2 = store.put("same content")
        assert h1 == h2
        assert block.stat().st_mtime_ns > old  # 幂等复用刷新 mtime（防轮转误删被新行引用的块）

    def test_different_content_different_hash(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        assert store.put("a") != store.put("b")

    def test_get_roundtrip(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        content = '{"role": "user", "content": "q"}'
        h = store.put(content)
        assert store.get(h) == content.encode()

    def test_get_unknown_returns_none(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        assert store.get("ff" * 32) is None


class TestListAndRemove:
    def _mk(self, tmp_path, content: str, age_seconds: float) -> BlockInfo:
        """写入一个块并回拨 mtime，返回清点口径的 BlockInfo。"""
        store = LocalFileBodyStore(root=tmp_path)
        h = store.put(content)
        block = tmp_path / "blocks" / h[:2] / f"{h}.gz"
        old = time.time() - age_seconds
        os.utime(block, (old, old))
        st = block.stat()
        return BlockInfo(hash=h, mtime=st.st_mtime, size=st.st_size)

    def test_list_blocks_reports_hash_mtime_size(self, tmp_path):
        info = self._mk(tmp_path, "some-body", age_seconds=60)

        listed = LocalFileBodyStore(root=tmp_path).list_blocks()

        assert len(listed) == 1
        assert listed[0].hash == info.hash
        assert listed[0].mtime == info.mtime
        assert listed[0].size > 0

    def test_remove_deletes_listed_blocks_only(self, tmp_path):
        doomed = self._mk(tmp_path, "old", age_seconds=15 * 86400)
        kept = self._mk(tmp_path, "fresh", age_seconds=60)

        store = LocalFileBodyStore(root=tmp_path)
        removed = store.remove_blocks([doomed])

        assert removed == 1
        assert store.get(doomed.hash) is None
        assert store.get(kept.hash) is not None

    def test_remove_skips_block_reused_after_listing(self, tmp_path):
        """清点与删除之间被 put 复用（mtime 刷新、新引用行在途）的块不得删。"""
        stale = self._mk(tmp_path, "stale-then-reused", age_seconds=15 * 86400)
        store = LocalFileBodyStore(root=tmp_path)

        # 模拟清点后同内容复用：put 幂等命中并刷新 mtime
        store.put("stale-then-reused")

        removed = store.remove_blocks([stale])
        assert removed == 0
        assert store.get(stale.hash) is not None  # 复用块幸存

    def test_remove_skips_non_hash_names(self, tmp_path):
        """目录里混入非哈希名 .gz（外部干扰/损坏）时跳过它，不中止整批。"""
        good = self._mk(tmp_path, "good-block", age_seconds=15 * 86400)
        shard = tmp_path / "blocks" / good.hash[:2]
        stray = shard / "not-a-hash.gz"
        stray.write_bytes(b"junk")

        store = LocalFileBodyStore(root=tmp_path)
        removed = store.remove_blocks(
            [good, BlockInfo(hash="not-a-hash", mtime=time.time(), size=4)]
        )

        assert removed == 1
        assert store.get(good.hash) is None
        assert stray.exists()

    def test_remove_missing_block_is_noop(self, tmp_path):
        """清点后块已消失（并发删除/手工清理）→ 幂等跳过，不抛。"""
        info = self._mk(tmp_path, "gone", age_seconds=15 * 86400)
        store = LocalFileBodyStore(root=tmp_path)
        (tmp_path / "blocks" / info.hash[:2] / f"{info.hash}.gz").unlink()

        assert store.remove_blocks([info]) == 0

    def test_empty_dir_ok(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        assert store.list_blocks() == []
        assert store.remove_blocks([]) == 0


def test_list_cleans_stale_tmp_residue(tmp_path):
    """put 崩溃残留的 .gz.tmp（os.replace 之前）超龄后在清点时被清理；新鲜 tmp 保留；
    仅剩超龄 tmp 的分片目录顺带移除（残留不进清点结果，删除方触达不到）。"""
    store = LocalFileBodyStore(tmp_path)
    shard = tmp_path / "blocks" / "ab"
    shard.mkdir(parents=True)
    stale_tmp = shard / "ab...stale.gz.tmp"
    stale_tmp.write_bytes(b"half")
    fresh_tmp = shard / "ab...fresh.gz.tmp"
    fresh_tmp.write_bytes(b"half")
    ancient = time.time() - 2 * 86400  # 超过一天的 tmp 残留龄
    os.utime(stale_tmp, (ancient, ancient))
    only_stale = tmp_path / "blocks" / "cd"
    only_stale.mkdir(parents=True)
    gone_tmp = only_stale / "cd...gone.gz.tmp"
    gone_tmp.write_bytes(b"half")
    os.utime(gone_tmp, (ancient, ancient))
    listed = store.list_blocks()
    assert listed == []
    assert not stale_tmp.exists()
    assert fresh_tmp.exists()
    assert shard.exists()
    assert not only_stale.exists()
