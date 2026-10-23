"""LocalFileBodyStore 测试（内容寻址块存储）。

- put：SHA-256 寻址，gzip 落盘 blocks/{hash前2位}/{hash}.gz，幂等去重；
- get：解压还原原文；未知哈希 → None；
- GC 两步：expired_candidates 列超期候选（引用判定不在存储层）、remove_blocks 只删
  调用方核查后确认无引用的块——「超保留期 且 无引用」双条件由两步合成。
"""

from __future__ import annotations

import gzip
import os
import time

from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore


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
        assert block.stat().st_mtime_ns > old  # 幂等复用刷新 mtime（防 sweep 误删被新行引用的块）

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


class TestGcTwoStep:
    def _mk(self, tmp_path, content: str, age_seconds: float) -> str:
        store = LocalFileBodyStore(root=tmp_path)
        h = store.put(content)
        block = tmp_path / "blocks" / h[:2] / f"{h}.gz"
        old = time.time() - age_seconds
        os.utime(block, (old, old))
        return h

    def test_candidates_only_expired_then_remove_skips_referenced(self, tmp_path):
        expired_unref = self._mk(tmp_path, "old-unref", age_seconds=15 * 86400)
        expired_ref = self._mk(tmp_path, "old-ref", age_seconds=15 * 86400)
        fresh_unref = self._mk(tmp_path, "fresh-unref", age_seconds=60)

        store = LocalFileBodyStore(root=tmp_path)
        candidates = store.expired_candidates(retention_seconds=14 * 86400)

        assert set(candidates) == {expired_unref, expired_ref}
        # 引用核查由调用方做（app._sweep_body_store_once 按批回查 PG）；存储层只删指定块
        removed = store.remove_blocks([h for h in candidates if h != expired_ref], retention_seconds=14 * 86400)

        assert removed == 1
        assert store.get(expired_unref) is None  # 过期且未引用 → 删
        assert store.get(expired_ref) is not None  # 过期但被引用 → 留（双条件）
        assert store.get(fresh_unref) is not None  # 未过期 → 留

    def test_remove_rechecks_mtime_after_reference_window(self, tmp_path):
        """候选与删除之间的引用核查窗口内被 put 复用（mtime 刷新、新引用行在途）
        的块不得删——put 的 mtime 刷新防护依赖删除前的这次复核。"""
        stale_hash = self._mk(tmp_path, "stale-then-reused", age_seconds=15 * 86400)
        store = LocalFileBodyStore(root=tmp_path)
        candidates = store.expired_candidates(retention_seconds=14 * 86400)
        assert candidates == [stale_hash]

        # 模拟核查窗口内同内容复用：put 幂等命中并刷新 mtime
        store.put("stale-then-reused")

        removed = store.remove_blocks(candidates, retention_seconds=14 * 86400)
        assert removed == 0
        assert store.get(stale_hash) is not None  # 复用块幸存

    def test_remove_skips_non_hash_names(self, tmp_path):
        """目录里混入非哈希名 .gz（外部干扰/损坏）时跳过它，不中止整批。"""
        good = self._mk(tmp_path, "good-block", age_seconds=15 * 86400)
        shard = tmp_path / "blocks" / good[:2]
        stray = shard / "not-a-hash.gz"
        stray.write_bytes(b"junk")

        store = LocalFileBodyStore(root=tmp_path)
        removed = store.remove_blocks([good, "not-a-hash"], retention_seconds=14 * 86400)

        assert removed == 1
        assert store.get(good) is None
        assert stray.exists()

    def test_candidates_empty_dir_ok(self, tmp_path):
        store = LocalFileBodyStore(root=tmp_path)
        assert store.expired_candidates(retention_seconds=60) == []
        assert store.remove_blocks([], retention_seconds=60) == 0


def test_candidates_cleans_stale_tmp_residue(tmp_path):
    """put 崩溃残留的 .gz.tmp（os.replace 之前）超保留期后在扫候选时被清理；新鲜 tmp 保留；
    仅剩超期 tmp 的分片目录顺带移除（候选为空时调用方短路，须在扫描侧兜住）。"""
    import os
    import time

    store = LocalFileBodyStore(tmp_path)
    shard = tmp_path / "blocks" / "ab"
    shard.mkdir(parents=True)
    stale_tmp = shard / "ab...stale.gz.tmp"
    stale_tmp.write_bytes(b"half")
    fresh_tmp = shard / "ab...fresh.gz.tmp"
    fresh_tmp.write_bytes(b"half")
    ancient = time.time() - 3600
    os.utime(stale_tmp, (ancient, ancient))
    only_stale = tmp_path / "blocks" / "cd"
    only_stale.mkdir(parents=True)
    gone_tmp = only_stale / "cd...gone.gz.tmp"
    gone_tmp.write_bytes(b"half")
    os.utime(gone_tmp, (ancient, ancient))
    candidates = store.expired_candidates(retention_seconds=600)
    assert candidates == []
    assert not stale_tmp.exists()
    assert fresh_tmp.exists()
    assert shard.exists()
    assert not only_stale.exists()
