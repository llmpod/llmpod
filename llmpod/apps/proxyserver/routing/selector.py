"""自研端点选择器：优先级分组（门槛）→ 组内会话亲和（缓存优先）→ 组内加权随机。

**选路优先级是严格顺序的，顺序本身就是语义，改动前先读这段：**

1. **优先级分组（门槛）**——只取当前可用的最高 priority 组，低组完全不看。优先级
   是运营对流量去向的显式意图（主池 / 降级 / 外部供应商），**亲和不得凌驾它**：
   故障期被挤到低优先级端点的会话，主池恢复后下一轮自然回主池（低组绑定不命中，
   走组内加权重新落回主池并改绑），否则每次成功续期的 TTL 会让它永久偏离主池。
2. **组内会话亲和（缓存优先）**——有前缀边界 hash 且命中的端点在最高组内，就直接
   取那个端点。同一会话复用上游的 KV / prompt 缓存，这是收益最大的一档，压过组内
   的负载均衡考量。
3. **组内加权随机**——亲和未命中（冷会话首轮、绑定端点已不可用或不在这组）时按
   weight 分流。

第 2、3 档是**负载均衡**，不是「故意绕开缓存」；冷会话没有缓存可保，只能从这里挑第一个
目标，之后该会话才由第 2 档接管。

**换端（failover）发生在上游失败之后**：失败端点被 exclude 掉后从第 1 档重来——所以一次
故障换端仍然先试会话亲和（同一会话可能还有绑到别的端点的较短边界，或绑在降级组里的
端点——可用组下移后它就在组内），亲和也答不出才按优先级 + 权重重选。

- 冷却（熔断）：连续失败 ≥ failure_threshold 进 cooldown_seconds 窗口；成功清零计数。
  窗口内该端点不再被选中——即便它是唯一候选（调用方对这种「端点级故障」也要求熔断，
  见 inference_handler 的 report_failure 调用点），此时请求快速 503 而不是逐个挂满超时。
  唯一例外是 ``select(strict=True)``：缓存优先策略宁可快速失败也不把会话甩到别的上游。
- v1 冷却与亲和状态均为进程内（单副本）；多副本部署时改为 Redis 共享。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from llmpod.apps.proxyserver.routing.session_affinity import EndpointAffinity
from llmpod.apps.proxyserver.snapshot import RouteTarget

log = logging.getLogger("llmpod.proxyserver.routing.selector")

DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_FAILURE_THRESHOLD = 2

# request_log.route_reason 取值：触发时机（首次选路 / 失败换端）× 选出机制（亲和 / 优先级+权重）
ROUTE_REASON_AFFINITY = "affinity"
ROUTE_REASON_WEIGHTED = "weighted"
ROUTE_REASON_FAILOVER_AFFINITY = "failover_affinity"
ROUTE_REASON_FAILOVER_WEIGHTED = "failover_weighted"


def route_reason(*, retried: bool, via_affinity: bool) -> str:
    """本次选路的出处：为什么是这个上游。

    排查时要能一眼分开「会话亲和（缓存优先）」与「优先级 + 加权随机（负载均衡）」，
    以及是不是上游故障后才换的端——两维正交，四个取值。
    """
    if retried:
        return ROUTE_REASON_FAILOVER_AFFINITY if via_affinity else ROUTE_REASON_FAILOVER_WEIGHTED
    return ROUTE_REASON_AFFINITY if via_affinity else ROUTE_REASON_WEIGHTED


@dataclass(frozen=True)
class Selection:
    """一次选路的结论：命中的端点 + 是否由会话亲和得来。

    选择必须带出处：``via_affinity`` 决定换端策略（``settings.affinity_failover_policy``
    的 rebind / strict，见 ``config.enums.AffinityFailoverPolicy``）——同一个
    RouteTarget 是随机抽中的还是亲和绑定的，后续处理完全不同。
    """

    target: RouteTarget
    via_affinity: bool


@dataclass
class _EndpointState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


class EndpointSelector:
    """端点选择器（有状态：跨请求维护失败计数、冷却窗口与会话亲和绑定）。"""

    def __init__(
        self,
        *,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        affinity: EndpointAffinity | None = None,
        clock=time.monotonic,
        rng: random.Random | None = None,
    ):
        self._cooldown_seconds = cooldown_seconds
        self._failure_threshold = failure_threshold
        self._affinity = affinity
        self._clock = clock
        self._rng = rng or random.Random()
        self._states: dict[int, _EndpointState] = {}

    def _state(self, endpoint_id: int) -> _EndpointState:
        return self._states.setdefault(endpoint_id, _EndpointState())

    def _available(self, endpoint_id: int) -> bool:
        return self._clock() >= self._state(endpoint_id).cooldown_until

    @property
    def affinity_enabled(self) -> bool:
        """会话亲和是否启用（未构造绑定表 = 配置关闭）。

        调用方据此跳过边界 hash 的计算——亲和关着时算了也没人消费，白付一遍
        O(消息数) 的哈希开销。
        """
        return self._affinity is not None

    def select(
        self,
        targets: tuple[RouteTarget, ...],
        *,
        exclude: frozenset[int] | None = None,
        hashes: list[str] | None = None,
        scope: str = "",
        strict: bool = False,
    ) -> Selection | None:
        """选一个端点：优先级分组（门槛）→ 组内会话亲和 → 组内加权随机。

        ``hashes`` 给定时在最高可用组内按「最长边界优先」取绑定端点；``scope`` 是
        亲和的作用域（路由名 + 协议），由调用方给——选择器拿不到路由身份，而不同
        路由的端点池不该在同一个前缀 hash 上互相串味。

        ``strict=True`` 是缓存优先策略的选路口径：亲和没有给出可用目标时直接放弃
        （返回 None，调用方快速失败），不落到加权随机把会话甩到别的端点。判定要等
        整条边界链走完——较早的边界绑定着冷却端点、较短的边界仍可能命中健康端点，
        提前返回会错杀。仅当「有绑定仍指向候选池内的端点，只是本轮选不出（熔断 /
        已试过 / 不在最高组）」才拦；无绑定（冷会话）或绑定已离开候选池（下架换绑，
        陈旧配置不该拦路）都照常回落负载均衡档。
        """
        if not targets:
            return None
        exclude = exclude or frozenset()
        available = {
            t.endpoint_id: t for t in targets if t.endpoint_id not in exclude and self._available(t.endpoint_id)
        }
        if not available:
            return None

        # 第 1 档：优先级分组——只取可用的最高 priority 组，低组完全不看（见模块
        # docstring：亲和与负载均衡都不得把流量压到低组去）
        top_priority = max(t.priority for t in available.values())
        top_group = {t.endpoint_id: t for t in available.values() if t.priority == top_priority}

        # 第 2 档：组内会话亲和（缓存优先）。绑定端点若已被熔断 / 本轮已试过 / 不在
        # 最高组（绑定是故障期落到低组的）/ 已移出候选池，会在这里被过滤掉并继续往
        # 短边界找，找不到就落到下面的负载均衡档
        strict_miss = False
        if hashes and self._affinity is not None:
            if strict:
                candidate_ids = {t.endpoint_id for t in targets}
            for endpoint_id in self._affinity.bound_endpoints(hashes, scope=scope):
                target = top_group.get(endpoint_id)
                if target is not None:
                    return Selection(target, via_affinity=True)
                if strict and endpoint_id in candidate_ids:
                    strict_miss = True

        if strict_miss:
            return None

        # 第 3 档：组内加权随机
        group = list(top_group.values())
        weights = [max(t.weight, 1) for t in group]
        return Selection(self._rng.choices(group, weights=weights, k=1)[0], via_affinity=False)

    def bind_affinity(self, hashes: list[str] | None, endpoint_id: int, *, scope: str = "") -> None:
        """把本次请求的前缀边界绑到该端点；亲和未启用时为 no-op。"""
        if self._affinity is not None:
            self._affinity.bind(hashes, endpoint_id, scope=scope)

    def report_success(self, endpoint_id: int) -> None:
        self._state(endpoint_id).consecutive_failures = 0

    def report_failure(self, endpoint_id: int, *, cooldown: bool = True) -> None:
        """记录失败；连续失败达阈值即熔断该端点 ``cooldown_seconds``。

        ``cooldown=False``（HTTP 4xx/5xx 这类**请求级**错误、且无别的候选）不计数也不冷却——
        冷却唯一端点会让用户换个正常请求也吃 503；而连接/超时这类**端点级**故障由调用方
        传 ``cooldown=True``，即使唯一候选也熔断（否则每个请求都要挂满整个超时）。
        """
        st = self._state(endpoint_id)
        if not cooldown:
            st.consecutive_failures = 0
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= self._failure_threshold:
            st.cooldown_until = self._clock() + self._cooldown_seconds
            st.consecutive_failures = 0  # 进冷却即重置，恢复后重新起算
            # 熔断对运维可见：端点被摘出候选，窗口内不再派流量
            log.warning(
                "端点 %s 连续失败达阈值，熔断 %ds（窗口内不再选它）",
                endpoint_id,
                self._cooldown_seconds,
            )
