"""BodyStore：调用日志 body 快照的内容寻址块存储抽象。

接口四方法：put（幂等，同内容同哈希）/ get / expired_candidates + remove_blocks
（GC 拆两步——先扫盘出候选、调用方分批核查引用、再删核查通过的块；引用集不整表
物化，见 proxyserver/app._sweep_body_store_once）。
PG 计量行只存块哈希（内容寻址 key），不感知后端——v1 本地文件实现，
v1.5+ 可平滑替换 S3/MinIO（接口与表结构不变）。

v1 块粒度 = 整条请求体 / 响应体各一块；v1.5 增量存储仅细化切块粒度 +
行内引用变块清单，本接口零改动（内容寻址第一性）。
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import time
from abc import ABC, abstractmethod

# 块哈希格式闸（64 位小写十六进制）：put/get 入口断言，防止非哈希字符串
# （带 ../ 等）经路径拼接越出 root——当前调用链全内生（sha256 自算 + DB 列），
# 此为纵深防御。
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class BodyStore(ABC):
    """内容寻址块存储抽象。"""

    @abstractmethod
    def put(self, content: bytes | str) -> str:
        """写入块，返回 SHA-256 hex；同内容幂等（不重写）。"""

    @abstractmethod
    def get(self, block_hash: str) -> bytes | None:
        """读取块原文；不存在返回 None。"""

    @abstractmethod
    def expired_candidates(self, retention_seconds: int) -> list[str]:
        """列出超保留期的块哈希（GC 删除候选；引用判定由调用方做）。"""

    @abstractmethod
    def remove_blocks(self, hashes: list[str], *, retention_seconds: int) -> int:
        """删除指定块（含所属分片目录顺带清空）；返回删除数。

        删除前须按 mtime 复核「仍超保留期」：候选集与真正删除之间隔着调用方的
        引用核查窗口，期间被 ``put`` 幂等复用的块 mtime 已刷新，不能删。
        """


class LocalFileBodyStore(BodyStore):
    """本地文件实现：{root}/blocks/{hash前2位}/{hash}.gz（gzip 压缩块）。"""

    def __init__(self, root):
        self._root = root

    def _block_path(self, block_hash: str):
        if not _HASH_RE.fullmatch(block_hash or ""):
            raise ValueError(f"非法块哈希（须为 64 位十六进制）: {block_hash!r}")
        return self._root / "blocks" / block_hash[:2] / f"{block_hash}.gz"

    def put(self, content: bytes | str) -> str:
        raw = content.encode() if isinstance(content, str) else content
        block_hash = hashlib.sha256(raw).hexdigest()
        path = self._block_path(block_hash)
        if path.is_file():
            # 幂等复用也刷新 mtime：内容寻址下同一块可能被新计量行复用，若沿用旧 mtime，
            # sweep 可能在其引用行 commit 前按「超保留期」误删（保留期计时重新起算）
            os.utime(path, None)
            return block_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时名带随机后缀：同内容被两个请求并发 put 时各写各的 tmp，互不覆盖
        # mkdir 与 replace 之间分片目录可能被 GC 的空分片 rmdir 抽走（见
        # expired_candidates），replace 抛 FileNotFoundError——重试一轮重建重写
        for attempt in range(2):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f"{block_hash}.{os.urandom(6).hex()}.gz.tmp"
            with gzip.open(tmp, "wb") as f:
                f.write(raw)
            try:
                os.replace(tmp, path)  # 原子落盘（并发 put 不留半块）
                return block_hash
            except FileNotFoundError:
                if attempt:
                    raise  # 两轮都被抽走：异常上抛，计量行照落、body 快照按缺失记

    def get(self, block_hash: str) -> bytes | None:
        path = self._block_path(block_hash)
        if not path.is_file():
            return None
        with gzip.open(path, "rb") as f:
            return f.read()

    def expired_candidates(self, retention_seconds: int) -> list[str]:
        """mtime 超保留期的块哈希清单；顺带清掉超期的 .gz.tmp 半块残留。"""
        blocks_dir = self._root / "blocks"
        if not blocks_dir.is_dir():
            return []
        now = time.time()
        candidates = []
        for shard in blocks_dir.iterdir():
            if not shard.is_dir():
                continue
            for block in shard.iterdir():
                if block.name.endswith(".gz"):
                    if now - block.stat().st_mtime > retention_seconds:
                        candidates.append(block.name[:-3])
                elif block.name.endswith(".gz.tmp"):
                    # put 崩溃残留（os.replace 之前的半块）：无引用方，按同保留期清理，防缓慢累积
                    if now - block.stat().st_mtime > retention_seconds:
                        block.unlink(missing_ok=True)
            # 仅剩 tmp 残留被清空的分片，块候选为空时调用方短路、触达不到
            # remove_blocks 的目录清理——扫到这里顺带 rmdir，防空目录永久滞留
            if not any(shard.iterdir()):
                shard.rmdir()
        return candidates

    def remove_blocks(self, hashes: list[str], *, retention_seconds: int) -> int:
        """删除给定块（幂等）；分片目录空了顺带清掉（残留文件在也不会误删目录）。

        删除前按 mtime 复核一遍「仍超保留期」：候选集与真正 unlink 之间隔着调用方的
        引用核查（若干 DB 往返），期间被 ``put`` 幂等复用的块会刷新 mtime——它即将有
        新引用行，此时删掉会让新行指向已删块（详见 ``put`` 的 mtime 注释），复核把这
        个窗口挡掉。非哈希名的杂散文件跳过不中止整批（哈希闸会 raise）。
        """
        removed = 0
        now = time.time()
        for block_hash in hashes:
            if not _HASH_RE.fullmatch(block_hash or ""):
                continue
            path = self._block_path(block_hash)
            if path.is_file() and now - path.stat().st_mtime > retention_seconds:
                path.unlink(missing_ok=True)
                removed += 1
        blocks_dir = self._root / "blocks"
        if blocks_dir.is_dir():
            for shard in blocks_dir.iterdir():
                if shard.is_dir() and not any(shard.iterdir()):
                    shard.rmdir()
        return removed
