"""BodyStore：调用日志 body 快照的内容寻址块存储抽象。

接口四方法：put（幂等，同内容同哈希）/ get / list_blocks（策略中立的清点，
一次遍历拿齐 hash+mtime+size）/ remove_blocks（按清点结果删除，删前复核
mtime 未变）。存储层不做任何「该不该删」的判断——按时间 / 按容量等轮转
策略见 retention.py，由 sweep 循环（proxyserver/background）驱动：清点 →
策略选块 → 限速删除。

PG 计量行只存块哈希（内容寻址 key），不感知后端——v1 本地文件实现，
v1.5+ 可平滑替换 S3/MinIO（接口与表结构不变）。

v1 块粒度 = 整条请求体 / 响应体各一块。
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

# 块哈希格式闸（64 位小写十六进制）：put/get 入口断言，防止非哈希字符串
# （带 ../ 等）经路径拼接越出 root——当前调用链全内生（sha256 自算 + DB 列），
# 此为纵深防御。
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# put 崩溃残留的 .gz.tmp 半块（os.replace 之前中断）按此龄清理：正常并发
# put 的 tmp 只存活毫秒级，超过一天必是孤儿残留，与轮转策略无关
_TMP_RESIDUE_SECONDS = 86400


@dataclass(slots=True)
class BlockInfo:
    """块目录清点出的一个条目（一次 stat 拿齐，轮转策略各取所需）。"""

    hash: str
    mtime: float
    size: int


class BodyStore(ABC):
    """内容寻址块存储抽象。"""

    @abstractmethod
    def put(self, content: bytes | str) -> str:
        """写入块，返回 SHA-256 hex；同内容幂等（不重写）。"""

    @abstractmethod
    def get(self, block_hash: str) -> bytes | None:
        """读取块原文；不存在返回 None。"""

    @abstractmethod
    def list_blocks(self) -> list[BlockInfo]:
        """清点全部块（哈希 / mtime / 大小）。顺带两类维护：清超龄 .gz.tmp 残留、
        移除只剩残留的空分片目录（只读清点与这两类维护在同一趟目录遍历里完成，
        省一次单独扫描）。"""

    @abstractmethod
    def remove_blocks(self, blocks: list[BlockInfo]) -> int:
        """删除指定块（含所属分片目录顺带清空）；返回删除数。

        删前按「当前 mtime == 清点时 mtime」复核：清点到删除的窗口里被
        ``put`` 幂等复用的块 mtime 已刷新（新引用行在途），不能删。
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
            # 轮转策略可能在选块与删除之间按旧时间把它选走（复用即保留期重新起算）
            os.utime(path, None)
            return block_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时名带随机后缀：同内容被两个请求并发 put 时各写各的 tmp，互不覆盖
        # mkdir 与 replace 之间分片目录可能被 list_blocks 的空分片 rmdir 抽走（见
        # list_blocks），replace 抛 FileNotFoundError——重试一轮重建重写
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

    def list_blocks(self) -> list[BlockInfo]:
        """清点全部块；顺带清超龄 .gz.tmp 残留与只剩残留的空分片目录。"""
        blocks_dir = self._root / "blocks"
        if not blocks_dir.is_dir():
            return []
        now = time.time()
        blocks: list[BlockInfo] = []
        for shard in blocks_dir.iterdir():
            if not shard.is_dir():
                continue
            # 本趟目录枚举同时判定分片是否已空（块/新鲜 tmp/杂散条目在 → 不空），
            # 不再用 any(iterdir()) 对每个分片多读一遍目录
            has_entries = False
            for block in shard.iterdir():
                if block.name.endswith(".gz"):
                    st = block.stat()
                    blocks.append(BlockInfo(hash=block.name[:-3], mtime=st.st_mtime, size=st.st_size))
                    has_entries = True
                elif block.name.endswith(".gz.tmp") and now - block.stat().st_mtime > _TMP_RESIDUE_SECONDS:
                    # put 崩溃残留：无引用方，超龄直接清，防缓慢累积（清走的
                    # 不置 has_entries——分片因此变空时可顺带 rmdir）
                    block.unlink(missing_ok=True)
                else:
                    has_entries = True
            # 分片只剩已被清掉的 tmp 残留时，块的删除方（remove_blocks）触达不到
            # ——扫到这里顺带 rmdir，防空目录永久滞留
            if not has_entries:
                shard.rmdir()
        return blocks

    def remove_blocks(self, blocks: list[BlockInfo]) -> int:
        """删除给定块（幂等）；本批删过的分片若已空顺带清掉。

        删前按「当前 mtime == 清点时 mtime」复核一遍：清点与真正 unlink 之间
        隔着策略选块与限速分批，期间被 ``put`` 幂等复用的块会刷新 mtime——它即将有
        新引用行，此时删掉会让新行指向已删块（详见 ``put`` 的 mtime 注释），复核把
        这个窗口挡掉。非哈希名的杂散条目跳过不中止整批（哈希闸会 raise）。
        空分片只在本批触达过的分片里找（rmdir 对非空目录自然失败即跳过）——全树
        重扫等于把删除成本再付一遍整库遍历；其他分片的常规维护由 list_blocks 承担。
        """
        removed = 0
        touched_shards = set()
        for info in blocks:
            if not _HASH_RE.fullmatch(info.hash or ""):
                continue
            path = self._block_path(info.hash)
            try:
                st = path.stat()
            except OSError:
                continue  # 已不在（并发删除/手工清理），幂等跳过
            if st.st_mtime != info.mtime:
                continue  # 清点后被 put 复用（mtime 刷新、新引用行在途）→ 幸存
            path.unlink(missing_ok=True)
            removed += 1
            touched_shards.add(path.parent)
        for shard in touched_shards:
            try:
                shard.rmdir()
            except OSError:
                pass  # 分片还有块 / 杂散文件 / 并发 put 刚写入 → 留着
        return removed
