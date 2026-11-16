"""BodyStore 块的轮转策略：纯决策，无 I/O。

两种轮转方式：
- 按时间（TimeRetentionPolicy）：mtime 早于保留期的块全删——保留期固定，
  磁盘占用量随流量浮动；
- 按容量（SizeRetentionPolicy）：块目录总量超上限时按 mtime 从最旧删到
  水位（80%）——磁盘占用量封顶，回看窗口 ≈ 上限 ÷ 日增。

策略只回答「选哪些块删」；扫描目录、限速分批、实际删除由 sweep 循环调
BodyStore 完成（见 proxyserver/background.sweep_body_store_once）。新增轮转
方式 = 新增 RetentionPolicy 实现 + build_retention_policy 工厂加一支。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from llmpod.apps.proxyserver.bodystore import BlockInfo
from llmpod.config.enums import BodyRetentionMode
from llmpod.config.settings import Settings

# 按容量轮转的删除水位：超上限后删到上限的 80%。固定值不做成配置——防抖
# 实现细节（避免流量停在阈值附近时每轮都触发删除），不是运维语义
_SIZE_WATERMARK = 0.8


class RetentionPolicy(ABC):
    """轮转策略：从清点结果中选出待删块。纯函数语义，不碰文件不碰库。"""

    @abstractmethod
    def select(self, blocks: list[BlockInfo], *, now: float) -> list[BlockInfo]:
        """返回应删除的块（最旧在前，便于调用方截断限速）；空列表 = 本轮不删。"""


class TimeRetentionPolicy(RetentionPolicy):
    """按时间轮转：mtime 早于 ``now - days`` 的块全删。"""

    def __init__(self, days: int):
        self._seconds = days * 86400

    def select(self, blocks: list[BlockInfo], *, now: float) -> list[BlockInfo]:
        doomed = [b for b in blocks if now - b.mtime > self._seconds]
        doomed.sort(key=lambda b: b.mtime)
        return doomed


class SizeRetentionPolicy(RetentionPolicy):
    """按容量轮转：总量超 ``max_bytes`` 时从最旧删到水位，不设年龄豁免。

    纯容量闸：缺口（总量 − 水位）凑够即停；回看窗口的大小由 ``max_bytes``
    本身决定。删除顺序按 mtime 从最旧起——``put`` 复用会刷新 mtime，还在被
    引用的块排在最后被驱逐。
    """

    def __init__(self, max_bytes: int):
        self._max_bytes = max_bytes

    def select(self, blocks: list[BlockInfo], *, now: float) -> list[BlockInfo]:
        total = sum(b.size for b in blocks)
        if total <= self._max_bytes:
            return []
        target = self._max_bytes * _SIZE_WATERMARK
        doomed: list[BlockInfo] = []
        freed = 0
        for block in sorted(blocks, key=lambda b: b.mtime):
            doomed.append(block)
            freed += block.size
            if total - freed <= target:
                break
        return doomed


def build_retention_policy(cfg: Settings) -> RetentionPolicy:
    """按配置装配轮转策略（proxyserver lifespan 调用）。

    mode 的合法性由 Settings 的 ``BodyRetentionMode`` 枚举在配置装配期把守
    （拼错即启动报错），此处不重复设防。
    """
    if cfg.body_retention_mode == BodyRetentionMode.TIME:
        return TimeRetentionPolicy(days=cfg.body_retention_days)
    return SizeRetentionPolicy(max_bytes=cfg.body_max_bytes)
