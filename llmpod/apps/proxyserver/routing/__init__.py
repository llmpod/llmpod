"""转发决策链：发给谁。三步按执行顺序排列，模块名即职责。

- ``session_hash``：从请求内容派生前缀边界指纹（纯函数，无状态）；
- ``session_affinity``：指纹 → 端点的绑定表（TTL + 条数上限 + 作用域隔离）；
- ``selector``：结合优先级分组 / 会话粘性 / 权重选出目标端点。

**实际把请求发出去不在这里**：协议适配在 ``protocol``（怎么跟上游说话），
换端循环与墙钟上限在 ``inference_handler``（发失败之后换谁）。本包只回答
「这次该发给谁」。
"""

from __future__ import annotations

from llmpod.apps.proxyserver.routing.selector import (
    ROUTE_REASON_AFFINITY,
    ROUTE_REASON_FAILOVER_AFFINITY,
    ROUTE_REASON_FAILOVER_WEIGHTED,
    ROUTE_REASON_WEIGHTED,
    EndpointSelector,
    Selection,
    route_reason,
)
from llmpod.apps.proxyserver.routing.session_affinity import EndpointAffinity
from llmpod.apps.proxyserver.routing.session_hash import (
    compute_boundary_hashes,
    compute_conv_prefix_hash,
)

__all__ = [
    "EndpointAffinity",
    "EndpointSelector",
    "ROUTE_REASON_AFFINITY",
    "ROUTE_REASON_FAILOVER_AFFINITY",
    "ROUTE_REASON_FAILOVER_WEIGHTED",
    "ROUTE_REASON_WEIGHTED",
    "Selection",
    "compute_boundary_hashes",
    "compute_conv_prefix_hash",
    "route_reason",
]
