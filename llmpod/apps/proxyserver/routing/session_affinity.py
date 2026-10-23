"""会话亲和：前缀边界 → 端点绑定，让同一会话的连续请求落回同一上游。

**为什么值得做**：引擎侧的前缀缓存（vLLM / SGLang 的自动前缀缓存、Anthropic 的
prompt cache）只存在于服务过这段前缀的那个实例上。多供应商 / 多副本路由下逐请求
加权随机会把同一会话打散，每轮都换一个上游 → 缓存全 miss，每轮重新 prefill；
Anthropic 侧更亏，缓存写按 1.25x 计费而读才 0.1x，等于每轮都在付溢价重建。

**key 为什么用内容派生而不是客户端会话 ID**：不依赖客户端配合（任何 SDK / curl
都生效），且与引擎侧「按前缀匹配 KV」的判定口径一致——引擎认的是内容，路由就按
内容绑定。多段边界的必要性见 ``session_hash.compute_boundary_hashes``：单段前缀 hash
逐轮都变，只有「每轮存多个边界、查时取最长命中」才能跨轮接上链。

**进程内状态**（与端点冷却同口径）：单副本够用，多副本要共享时整体挪 Redis。

换端取舍（``settings.affinity_failover_policy``）的取值见
``config.enums.AffinityFailoverPolicy``。按仓库反过度抽象约定，两个取值用一个普通
分支表达即可，不为它们铺 ABC + 注册表。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("llmpod.proxyserver.routing.session_affinity")

# 单次绑定/查表参与的边界条数（按最长的取）：把写量与查量随会话长度封顶。会话跨轮
# 匹配靠的是「上一轮的最长边界出现在下一轮序列里」，正常追加式对话的增长远小于该
# 窗口，压缩重写这类本就失配的场景不受影响。
_BOUNDARY_WINDOW = 64

# 过期清扫的摊销间隔（占 TTL 的比例）：表停在容量上限时，逐次绑定都全表扫过期是
# O(容量)/请求的热路径开销，摊销后只在间隔到点扫一次（见 _sweep_expired）。
_EVICT_INTERVAL_FRACTION = 4


@dataclass
class _Binding:
    endpoint_id: int
    expires_at: float


class EndpointAffinity:
    """前缀边界 hash → 端点绑定（进程内；TTL 过期 + 条数上限）。

    条目按「作用域 + 边界 hash」索引：作用域由调用方给（路由名 + 协议），避免两条
    路由的端点池在同一个 hash 上互相串味。
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_entries: int,
        stride: int = 1,
        clock=time.monotonic,
    ):
        if stride < 1:
            raise ValueError("stride must be >= 1")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._stride = stride
        self._clock = clock
        self._map: dict[str, _Binding] = {}
        self._last_evict_at = clock()

    def _boundaries(self, hashes: list[str]) -> list[str]:
        """按步长取样边界 hash（长的在后），只保留最长的 ``_BOUNDARY_WINDOW`` 条。

        步长越大，存与查的条目越少，代价是匹配粒度变粗（最短的那个边界退化成
        「只有开头几条消息」，撞车变多）。窗口让绑定表与查表的规模不随会话长度
        无界增长：结果从尾部取，更长的边界不进入返回集。
        """
        return hashes[self._stride - 1 :: self._stride][-_BOUNDARY_WINDOW:]

    def bound_endpoints(self, hashes: list[str] | None, *, scope: str) -> list[int]:
        """按边界从长到短返回命中的端点 id（去重，过期条目就地剔除）。

        只做「查表 + 过期清理」，不判断端点当前是否可用——可用性（冷却、是否被本轮
        failover 排除、是否还在候选集里）是选择器的状态，由调用方过滤。返回最长优先
        的顺序，是要让调用方取到的第一个可用端点就是缓存命中半径最大的那个；同一会话
        的多个边界通常绑在同一端点，去重后调用方不必拿同一个 id 反复试探。
        """
        if not hashes:
            return []
        now = self._clock()
        out: list[int] = []
        seen: set[int] = set()
        for boundary in reversed(self._boundaries(hashes)):
            key = self._key(scope, boundary)
            binding = self._map.get(key)
            if binding is None:
                continue
            if binding.expires_at <= now:
                del self._map[key]
                continue
            if binding.endpoint_id in seen:
                continue
            seen.add(binding.endpoint_id)
            out.append(binding.endpoint_id)
        return out

    def _sweep_expired(self, now: float) -> None:
        """按 TTL 摊销扫一次过期条目（由表满时的写入路径调用）。

        常态是「表停在容量上限、过期速率≈写入速率」，逐次绑定都全表扫过期是
        O(容量)/请求的热路径开销，摊销后只在间隔到点扫一次。过期条目对查表本就
        不可见，晚一小段回收只影响内存、不影响行为。
        """
        if now - self._last_evict_at < self._ttl_seconds / _EVICT_INTERVAL_FRACTION:
            return
        expired = [key for key, binding in self._map.items() if binding.expires_at <= now]
        for key in expired:
            del self._map[key]
        self._last_evict_at = now

    def bind(self, hashes: list[str] | None, endpoint_id: int, *, scope: str) -> None:
        """把本次请求的全部采样边界都绑到该端点。

        全部写上而非只写最长一条：客户端下一轮未必恰好从同一个边界切进来（注入
        system reminder、压缩重写等都会挪边界），多写几条让后续轮次从任一较长边界
        切进来都能命中。

        表满先按 TTL 摊销扫一次过期腾位；仍腾不出空间就跳过本次写入——绑定表是
        缓存，丢最近几条只损失一点命中率、不损正确性（查表对过期/缺键本就不可见）。
        """
        if not hashes:
            return
        boundaries = self._boundaries(hashes)
        now = self._clock()
        if len(self._map) + len(boundaries) > self._max_entries:
            self._sweep_expired(now)
            if len(self._map) + len(boundaries) > self._max_entries:
                return
        expires_at = now + self._ttl_seconds
        for boundary in boundaries:
            self._map[self._key(scope, boundary)] = _Binding(endpoint_id=endpoint_id, expires_at=expires_at)

    @staticmethod
    def _key(scope: str, boundary: str) -> str:
        return f"{scope}:{boundary}"
