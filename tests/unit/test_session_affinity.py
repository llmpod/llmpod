"""会话亲和单元测试：链式边界 hash + 绑定表 + strict/rebind 换端语义。

确定性：注入 clock 与 seeded random，锁定「跨轮命中 / 最长边界优先 / 过期 / 作用域隔离」。
"""

from __future__ import annotations

import random

import pytest

from tests.unit._fakes import FixedClock, route_target
from llmpod.apps.proxyserver.routing import (
    EndpointAffinity,
    EndpointSelector,
    compute_boundary_hashes,
    compute_conv_prefix_hash,
)
from llmpod.config.enums import AffinityFailoverPolicy


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def _turn(n: int) -> dict:
    """第 n 轮请求：n 轮问答（最后一条是本轮新问题）。"""
    messages = []
    for i in range(1, n):
        messages.append(_msg("user", f"q{i}"))
        messages.append(_msg("assistant", f"a{i}"))
    messages.append(_msg("user", f"q{n}"))
    return {"model": "m1", "messages": messages}


class TestBoundaryHashes:
    def test_one_hash_per_message(self):
        hashes = compute_boundary_hashes("chat.completions", _turn(3))
        assert hashes is not None
        assert len(hashes) == 5  # 3 轮 = user/assistant ×2 + 新问题

    def test_later_turn_extends_earlier_chain(self):
        """核心性质：消息只往后追加，故第 N+1 轮的 hash 序列以第 N 轮的序列开头。

        这正是「跨轮最长边界匹配」成立的前提——上一轮存的边界仍是下一轮序列里的前缀。
        """
        h1 = compute_boundary_hashes("chat.completions", _turn(1))
        h2 = compute_boundary_hashes("chat.completions", _turn(2))
        h3 = compute_boundary_hashes("chat.completions", _turn(3))

        assert h2[: len(h1)] == h1
        assert h3[: len(h2)] == h2

    def test_append_only_chain_is_what_makes_lookup_work(self):
        """逐轮验证：第 N 轮绑的最长边界，第 N+1 轮一定在候选边界里。"""
        h_next = compute_boundary_hashes("chat.completions", _turn(2))
        h_now = compute_boundary_hashes("chat.completions", _turn(3))

        longest_last_turn = h_next[len(h_next) - 1]
        assert longest_last_turn in h_now

    def test_anthropic_system_participates_in_chain(self):
        """Anthropic 的 system 在顶层、不随消息累积，漏进种子就会把两个不同会话算成一条链。"""
        base = {"messages": [_msg("user", "hi")]}
        a = compute_boundary_hashes("anthropic.messages", {**base, "system": "你是 A"})
        b = compute_boundary_hashes("anthropic.messages", {**base, "system": "你是 B"})
        assert a != b

    def test_openai_tools_participate_in_chain(self):
        base = {"messages": [_msg("user", "hi")]}
        a = compute_boundary_hashes("chat.completions", {**base, "tools": [{"name": "t1"}]})
        b = compute_boundary_hashes("chat.completions", {**base, "tools": [{"name": "t2"}]})
        assert a != b

    def test_semantically_equal_payloads_share_hashes(self):
        """规范化（sort_keys）后语义相同即同 hash——键序不同不该算成两个会话。"""
        a = compute_boundary_hashes("chat.completions", {"messages": [{"role": "user", "content": "hi"}]})
        b = compute_boundary_hashes("chat.completions", {"messages": [{"content": "hi", "role": "user"}]})
        assert a == b

    def test_long_base64_denoised(self):
        """图片体积不进指纹：同一张图换编码细节仍算同一会话。"""
        blob_a = "data:image/png;base64," + "A" * 400
        blob_b = "data:image/png;base64," + "B" * 400
        a = compute_boundary_hashes("chat.completions", {"messages": [_msg("user", blob_a)]})
        b = compute_boundary_hashes("chat.completions", {"messages": [_msg("user", blob_b)]})
        assert a == b

    @pytest.mark.parametrize("operation", ["embeddings", "rerank"])
    def test_no_session_operations_return_none(self, operation):
        assert compute_boundary_hashes(operation, {"input": "x"}) is None

    def test_every_operation_declares_session_semantics(self):
        """操作注册与会话语义声明对账：新操作加入 ADAPTERS 时必须显式二选一。

        忘了把消息形但无会话语义的操作（如 count_tokens）登记进
        _NO_SESSION_OPERATIONS，它会白算边界、还可能建出误导性的绑定。
        """
        from llmpod.apps.proxyserver.protocol import ADAPTERS
        from llmpod.apps.proxyserver.routing.session_hash import _NO_SESSION_OPERATIONS

        payload = {"messages": [{"role": "user", "content": "hi"}], "input": "hi"}
        for operation in ADAPTERS:
            hashes = compute_boundary_hashes(operation, payload)
            if operation in _NO_SESSION_OPERATIONS:
                assert hashes is None, operation
            else:
                assert hashes is not None, operation

    def test_empty_messages_yields_empty_list(self):
        assert compute_boundary_hashes("chat.completions", {"messages": []}) == []

    def test_preamble_only_conversation_yields_no_boundary(self):
        """只有垫场消息的请求没有可命中边界——共享同一段系统提示词的无关会话
        不该被吸到同一个端点（否则最短边界等于「有一段系统提示词」，负载均衡被绕过）。"""
        payload = {
            "messages": [
                {"role": "system", "content": "你是一个专业助手"},
                {"role": "user", "content": "你好"},
            ]
        }
        assert compute_boundary_hashes("chat.completions", payload) is not None
        assert len(compute_boundary_hashes("chat.completions", payload)) == 1

        system_only = {"messages": [{"role": "system", "content": "你是一个专业助手"}]}
        assert compute_boundary_hashes("chat.completions", system_only) == []

    def test_preamble_participates_in_chain_identity(self):
        """垫场消息不产出边界，但身份必须进链种子——否则共享同一段系统提示词的
        不同会话从第一个非垫场边界起算成同一条链。"""
        base = {"messages": [{"role": "system", "content": "共享提示词"}, {"role": "user", "content": "hi"}]}
        other = {"messages": [{"role": "system", "content": "另一段提示词"}, {"role": "user", "content": "hi"}]}
        assert compute_boundary_hashes("chat.completions", base) != compute_boundary_hashes("chat.completions", other)

    def test_mid_conversation_system_message_still_yields_boundary(self):
        """中段插入的 system（客户端注入 reminder 等）不算垫场——只有开场连续的
        垫场才豁免，中段的照常产出边界。"""
        payload = {
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "system", "content": "[reminder]"},
                {"role": "user", "content": "q2"},
            ]
        }
        assert len(compute_boundary_hashes("chat.completions", payload)) == 3

    def test_lone_surrogate_does_not_crash(self):
        """JSON 转义出的孤立代理项（\\ud800）不该把请求路径打成裸 500。"""
        payload = {"messages": [{"role": "user", "content": "坏字符:\ud800续"}]}
        hashes = compute_boundary_hashes("chat.completions", payload)
        assert hashes is not None and len(hashes) == 1
        assert compute_conv_prefix_hash("chat.completions", payload) is not None

    def test_distinct_from_log_fingerprint(self):
        """两个入口口径不同：落账列取整段一次算、路由侧要逐边界序列，不能互代。"""
        payload = _turn(3)
        assert compute_conv_prefix_hash("chat.completions", payload) not in compute_boundary_hashes(
            "chat.completions", payload
        )


class TestClientHintsAreNotRoutingKeys:
    """亲和键由对话内容派生，客户端自报的会话标识不参与选路。

    这类标识（请求头、``metadata.user_id``、``prompt_cache_key``）要么由客户端
    随意生成、要么按安装固定，既不可信也没有会话粒度；把它当键会让换一个提示词
    就换一条链，或者同一台设备的无关对话互相吸附。
    """

    def test_openai_client_hints_do_not_change_boundary_hashes(self):
        base = _turn(3)
        hinted = {
            **base,
            "metadata": {"user_id": "device-abc"},
            "prompt_cache_key": "conv-xyz",
            "conversation_id": "conv-xyz",
            "user": "device-abc",
        }
        assert compute_boundary_hashes("chat.completions", hinted) == compute_boundary_hashes("chat.completions", base)

    def test_anthropic_metadata_user_id_does_not_change_boundary_hashes(self):
        base = {"messages": [_msg("user", "q1")]}
        a = compute_boundary_hashes("anthropic.messages", {**base, "metadata": {"user_id": "device-a"}})
        b = compute_boundary_hashes("anthropic.messages", {**base, "metadata": {"user_id": "device-b"}})
        assert a == b

    def test_same_client_hint_does_not_merge_different_content(self):
        """同一客户端标识下的两段不同对话仍算出不同的链——提示词不能替代内容。"""
        a = compute_boundary_hashes(
            "anthropic.messages", {"metadata": {"user_id": "same-device"}, "messages": [_msg("user", "q1")]}
        )
        b = compute_boundary_hashes(
            "anthropic.messages", {"metadata": {"user_id": "same-device"}, "messages": [_msg("user", "q2")]}
        )
        assert a != b


class TestEndpointAffinity:
    def _affinity(self, **kw) -> EndpointAffinity:
        kw.setdefault("ttl_seconds", 300)
        kw.setdefault("max_entries", 1000)
        kw.setdefault("clock", FixedClock())
        return EndpointAffinity(**kw)

    def test_bound_endpoint_is_returned(self):
        aff = self._affinity()
        hashes = ["h1", "h2", "h3"]
        aff.bind(hashes, endpoint_id=7, scope="m1:openai")
        assert aff.bound_endpoints(hashes, scope="m1:openai") == [7]

    def test_longest_boundary_first(self):
        """不同长度前缀绑到不同端点：长边界排在前面，取缓存命中半径最大的那个。"""
        aff = self._affinity()
        aff.bind(["h1", "h2"], endpoint_id=10, scope="s")
        aff.bind(["h3", "h4"], endpoint_id=20, scope="s")
        assert aff.bound_endpoints(["h1", "h2", "h3", "h4"], scope="s") == [20, 10]

    def test_same_endpoint_reported_once(self):
        """同一会话的各边界通常绑在同一端点——去重后调用方不必反复试探同一个 id。"""
        aff = self._affinity()
        aff.bind(["h1", "h2", "h3"], endpoint_id=7, scope="s")
        assert aff.bound_endpoints(["h1", "h2", "h3"], scope="s") == [7]

    def test_scope_isolation(self):
        aff = self._affinity()
        hashes = ["h1"]
        aff.bind(hashes, endpoint_id=7, scope="m1:openai")
        assert aff.bound_endpoints(hashes, scope="m2:openai") == []
        assert aff.bound_endpoints(hashes, scope="m1:anthropic") == []

    def test_expires_after_ttl(self):
        clock = FixedClock()
        aff = self._affinity(ttl_seconds=300, clock=clock)
        aff.bind(["h1"], endpoint_id=7, scope="s")

        clock.advance(299)
        assert aff.bound_endpoints(["h1"], scope="s") == [7]

        clock.advance(2)
        assert aff.bound_endpoints(["h1"], scope="s") == []

    def test_rebind_refreshes_ttl(self):
        clock = FixedClock()
        aff = self._affinity(ttl_seconds=300, clock=clock)
        aff.bind(["h1"], endpoint_id=7, scope="s")

        clock.advance(200)
        aff.bind(["h1"], endpoint_id=7, scope="s")  # 会话仍在活动，续期
        clock.advance(200)

        assert aff.bound_endpoints(["h1"], scope="s") == [7]

    def test_stride_samples_multiples_only(self):
        aff = self._affinity(stride=2)
        hashes = ["h1", "h2", "h3", "h4"]
        aff.bind(hashes, endpoint_id=7, scope="s")
        # 只绑了边界 2 与 4：边界 3 没绑上
        assert aff.bound_endpoints(hashes, scope="s") == [7]
        assert aff.bound_endpoints(["h1", "h2", "h3"], scope="s") == [7]  # 边界 2 命中
        assert aff.bound_endpoints(["h1"], scope="s") == []  # 边界 2 都没到

    def test_max_entries_full_and_live_skips_write(self):
        """表满且无过期可清：跳过本次写入（绑定表是缓存，丢一次绑定只损失命中率）。"""
        aff = self._affinity(max_entries=2)
        aff.bind(["h1"], endpoint_id=1, scope="s")
        aff.bind(["h2"], endpoint_id=2, scope="s")
        aff.bind(["h3"], endpoint_id=3, scope="s")  # 触顶且都存活 → 不写入
        assert aff.bound_endpoints(["h1"], scope="s") == [1]  # 既有绑定原样保留
        assert aff.bound_endpoints(["h3"], scope="s") == []

    def test_max_entries_evicts_expired_amortized(self):
        """表满经摊销清扫清过期腾位：其它 scope 的存活绑定不陪葬。"""
        clock = FixedClock()
        aff = self._affinity(max_entries=2, clock=clock)
        aff.bind(["h1"], endpoint_id=1, scope="other")
        aff.bind(["h2"], endpoint_id=2, scope="s")
        clock.advance(301)  # h1/h2 过期（TTL=300）

        # h3 需要腾位且清扫间隔（TTL/4=75s）早已到点 → 清过期后写入成功
        aff.bind(["h3"], endpoint_id=3, scope="s")

        assert aff.bound_endpoints(["h1"], scope="other") == []
        assert aff.bound_endpoints(["h3"], scope="s") == [3]

    def test_max_entries_expired_sweep_is_amortized(self):
        """表满但清扫间隔未到点：先跳过写入，间隔到点后才扫过期。"""
        clock = FixedClock()
        aff = self._affinity(max_entries=2, clock=clock)
        aff.bind(["h1"], endpoint_id=1, scope="s")
        aff.bind(["h2"], endpoint_id=2, scope="s")
        clock.advance(10)  # 远小于 TTL/4：即便有条目将过期，也还不到清扫点
        aff.bind(["h3"], endpoint_id=3, scope="s")

        assert aff.bound_endpoints(["h3"], scope="s") == []  # 本轮跳过写入

        clock.advance(300)  # 到清扫点且 h1/h2 已过期 → h4 可写入
        aff.bind(["h4"], endpoint_id=4, scope="s")
        assert aff.bound_endpoints(["h4"], scope="s") == [4]

    def test_bind_caps_entries_per_request(self):
        """单次绑定只写最近 _BOUNDARY_WINDOW 条边界：超长会话不把表无界撑大。"""
        from llmpod.apps.proxyserver.routing.session_affinity import _BOUNDARY_WINDOW

        aff = self._affinity(max_entries=10_000)
        hashes = [f"h{i}" for i in range(500)]
        aff.bind(hashes, endpoint_id=7, scope="s")
        assert len(aff._map) == _BOUNDARY_WINDOW
        # 最近窗口外的老边界不命中，窗口内的最长边界照常命中
        assert aff.bound_endpoints(hashes[: -(_BOUNDARY_WINDOW + 5)], scope="s") == []
        assert aff.bound_endpoints(hashes, scope="s") == [7]

    def test_none_hashes_is_noop(self):
        aff = self._affinity()
        aff.bind(None, endpoint_id=7, scope="s")
        assert aff.bound_endpoints(None, scope="s") == []

    def test_stride_must_be_positive(self):
        with pytest.raises(ValueError):
            self._affinity(stride=0)


class TestPolicySwitch:
    """换端策略只剩取值集合与请求路径分支（strict / rebind），行为锁定在集成层。"""

    def test_policy_vocabulary(self):
        assert AffinityFailoverPolicy.values() == ["rebind", "strict"]


class TestSelectorAffinity:
    def _selector(self, **kw) -> EndpointSelector:
        affinity = EndpointAffinity(ttl_seconds=300, max_entries=1000, clock=kw.pop("clock", FixedClock()))
        return EndpointSelector(affinity=affinity, rng=random.Random(0), clock=FixedClock(), **kw)

    def test_affinity_hit_reports_provenance(self):
        sel = self._selector()
        sel.bind_affinity(["h1", "h2"], 20, scope="s")

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1", "h2"], scope="s")

        assert picked is not None
        assert picked.target.endpoint_id == 20
        assert picked.via_affinity is True

    def test_random_pick_is_not_marked_as_affinity(self):
        sel = self._selector()
        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s")
        assert picked is not None
        assert picked.via_affinity is False

    def test_affinity_still_hits_after_the_client_hint_changes(self):
        """换一个客户端会话标识（内容不变）仍命中同一条链：标识不参与键的计算。"""
        sel = self._selector()
        base = {"messages": [_msg("user", "q1")]}
        sel.bind_affinity(compute_boundary_hashes("anthropic.messages", base), 20, scope="s")

        hinted = {**base, "metadata": {"user_id": "another-device"}}
        picked = sel.select(
            (route_target(10), route_target(20)),
            hashes=compute_boundary_hashes("anthropic.messages", hinted),
            scope="s",
        )

        assert picked is not None
        assert picked.target.endpoint_id == 20
        assert picked.via_affinity is True

    def test_affinity_falls_through_when_bound_endpoint_cooling(self):
        """绑定的端点被熔断 → 不能硬塞，回落随机且如实标为未命中亲和。"""
        sel = self._selector(failure_threshold=1)
        sel.bind_affinity(["h1"], 20, scope="s")
        sel.report_failure(20)  # 立即冷却

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s")

        assert picked is not None
        assert picked.target.endpoint_id == 10
        assert picked.via_affinity is False

    def test_affinity_falls_through_when_bound_endpoint_excluded(self):
        sel = self._selector()
        sel.bind_affinity(["h1"], 20, scope="s")

        picked = sel.select((route_target(10), route_target(20)), exclude=frozenset({20}), hashes=["h1"], scope="s")

        assert picked is not None
        assert picked.target.endpoint_id == 10
        assert picked.via_affinity is False

    def test_affinity_ignores_endpoint_outside_candidates(self):
        """绑定端点已从候选池移出（下架/换绑）→ 不命中。"""
        sel = self._selector()
        sel.bind_affinity(["h1"], 99, scope="s")

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s")

        assert picked is not None
        assert picked.via_affinity is False

    def test_affinity_disabled_when_no_map_constructed(self):
        sel = EndpointSelector(rng=random.Random(0), clock=FixedClock())
        assert sel.affinity_enabled is False
        sel.bind_affinity(["h1"], 20, scope="s")  # no-op，不炸
        picked = sel.select((route_target(10),), hashes=["h1"], scope="s")
        assert picked is not None
        assert picked.via_affinity is False

    def test_affinity_does_not_override_priority_groups(self):
        """亲和只作用于最高可用优先级组：绑定在低组的会话在主池恢复后要能回去。

        故障期被挤到降级端点的会话，若亲和凌驾优先级，每次成功续期的 TTL 会让它
        永久偏离主池（外部供应商持续花钱）。
        """
        sel = self._selector()
        # 故障期：唯一可用的是低优先级端点 10，会话被绑到它
        sel.bind_affinity(["h1"], 10, scope="s")
        assert sel.select((route_target(10, priority=0),), hashes=["h1"], scope="s").target.endpoint_id == 10

        # 主池恢复：高优先级端点 20 回到候选，亲和（绑在 10）不得把流量压回低组
        picked = sel.select((route_target(10, priority=0), route_target(20, priority=9)), hashes=["h1"], scope="s")

        assert picked is not None
        assert picked.target.endpoint_id == 20
        assert picked.via_affinity is False

    def test_affinity_hits_within_top_group(self):
        """绑定端点就在最高组内 → 照常亲和命中（这是亲和的正常工作区）。"""
        sel = self._selector()
        sel.bind_affinity(["h1"], 20, scope="s")

        picked = sel.select((route_target(10, priority=9), route_target(20, priority=9)), hashes=["h1"], scope="s")

        assert picked is not None
        assert picked.target.endpoint_id == 20
        assert picked.via_affinity is True

    def test_strict_select_returns_bound_endpoint_when_healthy(self):
        sel = self._selector()
        sel.bind_affinity(["h1"], 20, scope="s")

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s", strict=True)

        assert picked is not None
        assert picked.target.endpoint_id == 20
        assert picked.via_affinity is True

    def test_strict_select_refuses_to_switch_when_bound_endpoint_cooling(self):
        """strict 的「不换端」要在冷却面前也成立：绑定端点被熔断时快速失败，
        而不是把会话甩到别的端点（普通 select 会这么做）。"""
        sel = self._selector(failure_threshold=1)
        sel.bind_affinity(["h1"], 20, scope="s")
        sel.report_failure(20)  # 立即冷却

        assert sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s", strict=True) is None

    def test_strict_select_cold_session_falls_back_to_normal(self):
        """无绑定（冷会话）不受 strict 约束——没有缓存可保，照常负载均衡。"""
        sel = self._selector()

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s", strict=True)

        assert picked is not None
        assert picked.via_affinity is False

    def test_strict_select_stale_binding_falls_back_to_normal(self):
        """绑定端点已移出候选池（下架/换绑）：陈旧配置不该拦路，按无亲和处理。"""
        sel = self._selector()
        sel.bind_affinity(["h1"], 99, scope="s")

        picked = sel.select((route_target(10), route_target(20)), hashes=["h1"], scope="s", strict=True)

        assert picked is not None
        assert picked.via_affinity is False


class TestConversationStickiness:
    """端到端语义（不经过 HTTP）：同一会话连续两轮应落到同一上游。"""

    def test_second_turn_sticks_to_first_endpoint(self):
        sel = EndpointSelector(
            affinity=EndpointAffinity(ttl_seconds=300, max_entries=1000),
            rng=random.Random(7),
        )
        targets = (route_target(10), route_target(20))
        scope = "m1:openai"

        turn1 = sel.select(targets, hashes=compute_boundary_hashes("chat.completions", _turn(1)), scope=scope)
        assert turn1 is not None
        sel.bind_affinity(compute_boundary_hashes("chat.completions", _turn(1)), turn1.target.endpoint_id, scope=scope)

        turn2 = sel.select(targets, hashes=compute_boundary_hashes("chat.completions", _turn(2)), scope=scope)

        assert turn2 is not None
        assert turn2.via_affinity is True
        assert turn2.target.endpoint_id == turn1.target.endpoint_id

    def test_long_conversation_keeps_sticking(self):
        sel = EndpointSelector(
            affinity=EndpointAffinity(ttl_seconds=300, max_entries=1000),
            rng=random.Random(3),
        )
        targets = (route_target(10), route_target(20), route_target(30))
        scope = "m1:openai"
        bound: set[int] = set()

        for n in range(1, 12):
            hashes = compute_boundary_hashes("chat.completions", _turn(n))
            picked = sel.select(targets, hashes=hashes, scope=scope)
            assert picked is not None
            sel.bind_affinity(hashes, picked.target.endpoint_id, scope=scope)
            bound.add(picked.target.endpoint_id)

        assert len(bound) == 1  # 全会话绑定在同一个上游

    def test_compaction_resets_affinity_to_a_new_endpoint(self):
        """auto-compaction 重写历史 → 前缀全变 → 亲和失配回落随机（已知代价，非缺陷）。"""
        sel = EndpointSelector(
            affinity=EndpointAffinity(ttl_seconds=300, max_entries=1000),
            rng=random.Random(11),
        )
        targets = (route_target(10), route_target(20))
        scope = "m1:openai"

        hashes = compute_boundary_hashes("chat.completions", _turn(4))
        picked = sel.select(targets, hashes=hashes, scope=scope)
        assert picked is not None
        sel.bind_affinity(hashes, picked.target.endpoint_id, scope=scope)

        # 压缩后的会话：历史被重写成一段摘要，前缀不再与任何已存边界相交
        compacted = {
            "messages": [
                _msg("user", "（前文摘要）…"),
                _msg("assistant", "好的"),
                _msg("user", "继续"),
            ]
        }
        after = sel.select(targets, hashes=compute_boundary_hashes("chat.completions", compacted), scope=scope)

        assert after is not None
        assert after.via_affinity is False
