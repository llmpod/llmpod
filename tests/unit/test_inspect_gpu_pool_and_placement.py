"""inspect 卡池装配 / 节点可选性 / 手选校验 / 推荐调度结构的单测（FakeSession 直调路由）。

覆盖 cluster_gpus 的逐卡字段口径（灰化 / 分组 / 实时读数）、node_support 的原因分流、
手动选卡与选节点的校验文案、recommended_placement 的多副本结构（replica_plans /
arch_summary / unit_plans / cards 快照 / headroom）。与 test_inspect_occupancy.py 互补：
那边盯占用语义，这边盯装配结构与形状。
"""

from __future__ import annotations


from llmpod.apps.manager.deployment.schemas import GpuSelector, Topology
from llmpod.apps.webserver.routes.admin import deployment as route_mod
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.config.constants import GpuStatus
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node import Node
from tests.unit._fakes import FakeSession


def _node(nid, ip="10.0.0.1", cluster=1, status="online", mem=64.0):
    return Node(id=nid, hostname=nid, ip=ip, cluster_id=cluster, status=status, memory_gb=mem)


def _gpu(i, uuid, node="n1", *, total=22.0, status="online", arch="nvidia"):
    return Gpu(
        id=i,
        uuid=uuid,
        node_id=node,
        index=i,
        name="NVIDIA GeForce RTX 2080 Ti",
        arch=arch,
        driver_version="595.84",
        total_vram_gb=total,
        status=status,
        spec={},
    )


def _seed(model, *objs):
    s = FakeSession()
    s.preload(model)
    for o in objs:
        s.preload(o)
    return s


def _inspect(session, **kw):
    base = dict(model_id=1, cluster_id=1, engine="vllm", topology=Topology(tp=1, max_model_len=8192))
    base.update(kw)
    return route_mod.inspect_deployment(InspectDeploymentRequest(**base), session)


_M = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors")
_M_BIG = Model(id=1, name="m", model_type="llm", size_gb=30.0, format="safetensors")


class TestGpuPoolAssembly:
    """cluster_gpus 逐卡字段：离线/stale 卡也返回（前端灰化展示），节点限在线。"""

    async def test_offline_and_stale_cards_returned_greyed(self):
        """离线 / stale 卡不滤掉：进池、grey=offline、无分组键；online 卡正常三字段。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "off1", status="offline"), _gpu(3, "stale1", status="stale"))
        result = await _inspect(s)
        by_uuid = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert by_uuid["g1"]["grey_reason"] is None
        assert by_uuid["g1"]["group_key"] == "1:nvidia:22.0"
        assert by_uuid["g1"]["fit"] is True
        assert by_uuid["off1"]["grey_reason"] == "offline"
        assert by_uuid["off1"]["group_key"] is None
        assert by_uuid["stale1"]["grey_reason"] == "offline"
        assert by_uuid["stale1"]["status"] == GpuStatus.STALE

    async def test_offline_nodes_cards_excluded(self):
        """节点离线 → 其卡整体不进池（Node.status 过滤先于 Gpu 查询）。"""
        s = _seed(_M, _node("n1"), _node("n2", status="offline"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(s)
        assert [cg["uuid"] for cg in result["data"]["cluster_gpus"]] == ["g1"]
        assert [ns["node_id"] for ns in result["data"]["node_support"]] == ["n1"]

    async def test_card_fields_carry_all_dimensions(self):
        """单张 online 卡的完整字段口径：显存三值 / required 参考 / 心跳原始已用 None。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        cg = result["data"]["cluster_gpus"][0]
        assert cg["total_vram_gb"] == 22.0
        assert cg["available_vram_gb"] == 22.0  # 无实时读数 → total
        assert cg["used_vram_gb"] is None  # 心跳未上报
        assert cg["required_vram_gb"] == 1.1  # size + KV(8192)
        assert cg["index"] == 1
        assert cg["name"] == "NVIDIA GeForce RTX 2080 Ti"
        assert cg["driver_version"] == "595.84"

    async def test_realtime_usage_discounted_from_available(self, monkeypatch):
        """心跳实时已用 → available_vram_gb = total − used（钳非负）。"""
        monkeypatch.setattr(inspect_svc, "load_used_vram_by_uuid", lambda: {"g1": 5.0})
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        cg = result["data"]["cluster_gpus"][0]
        assert cg["used_vram_gb"] == 5.0
        assert cg["available_vram_gb"] == 17.0

    async def test_same_arch_different_vram_different_groups(self):
        """同 arch 不同显存 → 不同选择组（group_key 含 total_vram）；跨档不能同 TP。"""
        s = _seed(_M, _node("n1"), _gpu(1, "a100", total=80.0), _gpu(2, "ti"))
        result = await _inspect(s)
        groups = {cg["uuid"]: cg["group_key"] for cg in result["data"]["cluster_gpus"]}
        assert groups["a100"] == "1:nvidia:80.0"
        assert groups["ti"] == "1:nvidia:22.0"

    async def test_fit_false_when_single_card_insufficient(self):
        """30GB 模型 22GB 卡：卡不灰（可组 TP）但 fit=False（单卡装不下）。"""
        s = _seed(_M_BIG, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        cg = result["data"]["cluster_gpus"][0]
        assert cg["grey_reason"] is None
        assert cg["fit"] is False


class TestNodeSupport:
    """node_support：逐节点可选性 + 原因分流（前端节点选择禁用用）。"""

    async def test_node_without_gpu_reason(self):
        """在线节点但无 GPU 行 → selectable=False + reason=「该节点无 GPU」。"""
        s = _seed(_M, _node("n1"), _node("nogpu"))
        result = await _inspect(s)
        by_id = {ns["node_id"]: ns for ns in result["data"]["node_support"]}
        assert by_id["nogpu"]["selectable"] is False
        assert by_id["nogpu"]["reason"] == "该节点无 GPU"

    async def test_node_with_selectable_card(self):
        """有引擎可用卡的节点 → selectable=True、reason=None、回显内存两值。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s)
        ns = result["data"]["node_support"][0]
        assert ns["node_id"] == "n1"
        assert ns["selectable"] is True
        assert ns["reason"] is None
        assert ns["memory_gb"] == 64.0
        assert ns["hostname"] == "n1"

    async def test_selectable_nodes_sorted_first(self):
        """排序：可选节点在前，同档按 node_id 字典序。"""
        s = _seed(_M, _node("n1"), _node("n2"), _node("nogpu"), _gpu(1, "g2", "n2"))
        result = await _inspect(s)
        order = [ns["node_id"] for ns in result["data"]["node_support"]]
        assert order == ["n2", "n1", "nogpu"]


class TestManualSelectionValidation:
    """手选 gpu_uuids / node_ids 的结构校验与文案。"""

    async def test_too_few_cards_selected(self):
        """tp=2 只选 1 张 → 「手动选卡需 2 张（tp×pp），实际 1 张」。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(
            s, topology=Topology(tp=2, max_model_len=8192), gpu_selector=GpuSelector(gpu_uuids=["g1"])
        )
        d = result["data"]
        assert d["deployable"] is False
        assert any("手动选卡需 2 张" in i and "实际 1 张" in i for i in d["issues"])

    async def test_too_many_cards_selected(self):
        """tp=1 选 2 张 → 多选也拒（tp×pp 必须精确匹配）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3"))
        result = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1", "g3"]))
        assert result["data"]["deployable"] is False
        assert any("手动选卡需 1 张" in i and "实际 2 张" in i for i in result["data"]["issues"])

    async def test_offline_card_selected(self):
        """选了离线卡 → 「指定的 GPU 不可用（离线/不存在）」。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1", status="offline"), _gpu(2, "g2"))
        result = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        assert result["data"]["deployable"] is False
        assert any("指定的 GPU 不可用" in i and "g1" in i for i in result["data"]["issues"])

    async def test_heterogeneous_copies_manual_selection(self):
        """异构 copies=[{tp:2},{tp:1}] 手选 3 张 → 按份分流校验（Σ 各份 tp×pp=3）放行。

        回归：恒走同形口径会算成「首份 2 张 × 2 份 = 需 4 张」，把 create 接受的
        合法选集错拒，而同响应的 replica_plans 还把这 3 张描述成可行调度结果。
        """
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3", "n2"))
        result = await _inspect(
            s,
            topology=Topology(copies=[{"tp": 2}, {"tp": 1}], max_model_len=8192),
            gpu_selector=GpuSelector(gpu_uuids=["g1", "g2", "g3"]),
        )
        d = result["data"]
        assert d["deployable"] is True
        assert not any("手动选卡需" in i for i in d["issues"])
        rp = d["recommended_placement"]
        assert [p["units"][0]["gpu_uuids"] for p in rp["replica_plans"]] == [["g1", "g2"], ["g3"]]

    async def test_manual_selection_top_level_fields_take_selection(self):
        """手选卡时顶层 gpu_uuids/cards = 选集本身（去重保序），不再取自动内核结果。

        顶层字段的设计用途是给前端回填预选——取内核结果会把用户指定的卡静默换掉。
        例用 replicas=2（每份 1 卡、桶配对可行），单份 tp=2 跨节点的调度见
        test_manual_cross_node_layout_valid。
        """
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3", "n2"))
        result = await _inspect(
            s,
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
            gpu_selector=GpuSelector(gpu_uuids=["g2", "g3"]),
        )
        rp = result["data"]["recommended_placement"]
        assert rp["gpu_uuids"] == ["g2", "g3"]  # 选集原序，不是内核挑的 g1/g2
        assert rp["node_ids"] == ["n1", "n2"]
        assert [c["uuid"] for c in rp["cards"]] == ["g2", "g3"]
        assert [p["units"][0]["gpu_uuids"] for p in rp["replica_plans"]] == [["g2"], ["g3"]]

    async def test_node_selection_insufficient_cards_per_node(self):
        """选节点集 + tp=2 但该节点只有 1 张 → 「每节点需 2 张…不足」。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(
            s,
            gpu_selector=GpuSelector(node_ids=["n1"]),
            topology=Topology(tp=2, max_model_len=8192),
        )
        d = result["data"]
        assert d["deployable"] is False
        assert any("每节点需 2 张 nvidia 可用卡" in i for i in d["issues"])

    async def test_node_selection_with_unknown_node(self):
        """node_ids 含不存在/离线节点 → 逐个点名。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, gpu_selector=GpuSelector(node_ids=["nX"]))
        d = result["data"]
        assert d["deployable"] is False
        assert any("node_ids 含不可用节点" in i and "nX" in i for i in d["issues"])

    async def test_manual_cross_node_layout_valid(self):
        """手选两节点各 1 张 + tp=2×nnodes=2 → 通过 + 推荐调度即选集。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(
            s,
            topology=Topology(tp=2, nnodes=2, max_model_len=8192),
            gpu_selector=GpuSelector(gpu_uuids=["g1", "g2"]),
        )
        d = result["data"]
        assert d["deployable"] is True
        assert sorted(d["recommended_placement"]["gpu_uuids"]) == ["g1", "g2"]

    async def test_pinned_node_narrows_recommendation(self):
        """指定节点（未手选卡）：推荐池收进指定的节点，不推荐范围外的卡。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(s, gpu_selector=GpuSelector(node_id="n2"))
        assert result["data"]["recommended_placement"]["gpu_uuids"] == ["g2"]

    async def test_node_ids_narrows_recommendation(self):
        """node_ids 指定：推荐只在集合内节点选卡。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(s, gpu_selector=GpuSelector(node_ids=["n2"]))
        assert result["data"]["recommended_placement"]["gpu_uuids"] == ["g2"]


class TestPlacementStructure:
    """recommended_placement 的多副本结构口径（顶层 = 首份首单元展开）。"""

    async def test_two_replicas_across_nodes(self):
        """replicas=2 跨两节点：顶层 = 份 0；replica_plans 逐份（含 headroom）；
        cards 快照带 hostname（前端不必再关联 cluster_gpus）。"""
        s = _seed(_M, _node("n1"), _node("n2"), _gpu(1, "g1"), _gpu(2, "g2", "n2"))
        result = await _inspect(s, topology=Topology(tp=1, replicas=2, max_model_len=8192))
        rp = result["data"]["recommended_placement"]
        assert rp["gpu_uuids"] == ["g1", "g2"]  # 顶层 = 全部份调度到的卡合集
        assert rp["node_ids"] == ["n1", "n2"]
        assert rp["arch"] == "nvidia"
        assert rp["total_gpus"] == 2
        assert rp["arch_summary"] == {"nvidia": 2}
        plans = rp["replica_plans"]
        assert [p["seq"] for p in plans] == [0, 1]
        assert plans[0]["units"][0]["gpu_uuids"] == ["g1"]
        assert plans[1]["units"][0]["gpu_uuids"] == ["g2"]
        assert all(p["fit"] for p in plans)
        assert plans[0]["units"][0]["headroom_gb"] == 20.9  # 22.0 − 1.1
        cards = {c["uuid"]: c for c in rp["cards"]}
        assert set(cards) == {"g1", "g2"}
        assert cards["g1"]["hostname"] == "n1"
        assert cards["g1"]["available_vram_gb"] == 22.0
        assert cards["g1"]["required_vram_gb"] == 1.1

    async def test_tp2_unit_plan_and_headroom(self):
        """tp=2 单份：unit_plans 声明 2 卡引擎；headroom 按每卡需求（required÷tp）算。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(s, topology=Topology(tp=2, max_model_len=8192))
        rp = result["data"]["recommended_placement"]
        assert rp["unit_plans"] == [
            {
                "role": "full",
                "tp": 2,
                "pp": 1,
                "ep": 1,
                "nnodes": 1,
                "gpus_per_engine": 2,
                "image": "vllm/vllm-openai:v0.27.1",
            }
        ]
        assert rp["total_gpus"] == 2
        unit = rp["replica_plans"][0]["units"][0]
        assert unit["gpu_uuids"] == ["g1", "g2"]
        assert unit["fit"] is True
        assert unit["headroom_gb"] == 21.45  # 22.0 − 1.1/2

    async def test_manual_selection_pairing_for_copies(self):
        """手选 + replicas=2：pair_manual_replicas 逐份配对，cards 快照取选集。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(
            s,
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
            gpu_selector=GpuSelector(gpu_uuids=["g1", "g2"]),
        )
        rp = result["data"]["recommended_placement"]
        plans = rp["replica_plans"]
        assert [p["seq"] for p in plans] == [0, 1]
        assert plans[0]["units"][0]["gpu_uuids"] == ["g1"]
        assert plans[1]["units"][0]["gpu_uuids"] == ["g2"]
        assert [c["uuid"] for c in rp["cards"]] == ["g1", "g2"]

    async def test_heterogeneous_copies_reported_per_replica(self):
        """异构 copies（1 卡份 + 2 卡份）：顶层回显首份折算；unit_plans/逐份口径见
        replica_plans（本例 2×22G 集群凑不出 3 卡 → 结构性报不足）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(s, topology=Topology(copies=[{"tp": 1}, {"tp": 2}]))
        d = result["data"]
        assert d["deployable"] is False
        assert any("需 2 份引擎" in i and "逐份 1 张 + 2 张，共 3 张" in i for i in d["issues"])
        assert d["recommended_placement"]["replica_plans"] == []

    async def test_homogeneous_copies_list_normalizes(self):
        """同形 copies 列表归一为 int 形态（dump 与历史一致）。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(s, topology=Topology(copies=[{"tp": 1}, {"tp": 1}]))
        topo = result["data"]["recommendation"]["topology"]
        assert topo["copies"] == 1
        assert topo["tp"] == 1


class TestAutoPlacementSelection:
    """自动调度的推荐选卡行为：本地性 / 大卡优先 / 分档不跨组。"""

    async def test_prefers_big_single_card_over_tiered_pair(self):
        """50GB 模型：1×80G（tp=1 可行）优先于 2×22G 组（tp=3 非法）→ 推导 tp=1 落大卡。"""
        m = Model(id=1, name="m", model_type="llm", size_gb=50.0, format="safetensors")
        s = _seed(m, _node("n1"), _node("n2"), _gpu(1, "a100", total=80.0), _gpu(2, "t1", "n2"), _gpu(3, "t2", "n2"))
        result = await _inspect(s, engine=None, topology=None)
        d = result["data"]
        assert d["recommendation"]["topology"]["tp"] == 1
        assert d["recommended_placement"]["gpu_uuids"] == ["a100"]
        assert d["deployable"] is True

    async def test_derived_tp_respects_head_divisibility(self):
        """heads=8（合法 tp 1/2/4/8）+ 3 张 22G 卡 30GB 模型 → 推导 tp=2（不是 3）。"""
        m = Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=30.0,
            format="safetensors",
            num_attention_heads=8,
            num_kv_heads=8,
        )
        s = _seed(m, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3"))
        result = await _inspect(s, engine=None, topology=None)
        assert result["data"]["recommendation"]["topology"]["tp"] == 2

    async def test_unknown_shape_derives_from_card_count(self):
        """模型未探测 head 形状 → 合法 tp 退化为 1..卡数（2 卡 → tp=2）。"""
        m = Model(id=1, name="m", model_type="llm", size_gb=30.0, format="safetensors")
        s = _seed(m, _node("n1"), _gpu(1, "g1"), _gpu(2, "g2"), _gpu(3, "g3"))
        result = await _inspect(s, engine=None, topology=None)
        assert result["data"]["recommendation"]["topology"]["tp"] == 2

    async def test_util_override_does_not_change_inspect_gate(self):
        """用户显存占比 0.5 只回显，不放宽手选容量判定（inspect 按平台默认口径）。

        30GB 模型选 22GB 卡：即便用户把占比调到 0.5（11GB 预留也不够 33GB 需求），
        判定仍按整卡 0.9 口径报「提高 tp 或换更大显存」。"""
        s = _seed(_M_BIG, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(
            s,
            engine_params={"extra_args": ["--gpu-memory-utilization", "0.5"]},
            gpu_selector=GpuSelector(gpu_uuids=["g1"]),
        )
        d = result["data"]
        assert d["resolved"]["gpu_memory_utilization"] == 0.5  # 回显层跟随用户
        assert d["deployable"] is False  # 判定层不跟随
        assert any("按平台默认显存占比 90%" in i for i in d["issues"])
