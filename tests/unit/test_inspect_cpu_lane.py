"""inspect 路由的 CPU 部署分支单测（stub DB + monkeypatch 依赖符号）。

覆盖：allow_cpu 时产 ``memory`` 逐层表、纯 CPU 场景把无卡节点标为可选（``cpu_lane``）、
``resolved.args`` 回显 ``--n-gpu-layers 0``、手选卡与 allow_cpu 互斥。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.feasibility import NodeResource
from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology
from llmpod.apps.manager.deployment.schemas.config import KVCacheConfig
from llmpod.apps.webserver.routes.admin import deployment as route_mod
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node import Node

_MODEL = Model(
    id=7,
    name="tiny-gguf",
    model_type="llm",
    size_gb=1.0,
    format="gguf",
    num_layers=32,
    num_kv_heads=8,
    head_dim=128,
)


class _Result:
    def scalars(self):
        return []


class _StubDB:
    """只答 inspect 需要的两类查询：Model.get 与节点/GPU select（一律空）。"""

    def __init__(self, nodes=None):
        self._nodes = nodes or []

    async def get(self, cls, pk):
        return _MODEL if cls is Model else None

    async def execute(self, _stmt):
        return _Result()


def _node_fact(node_id: str, avail_ram_gb: float) -> NodeResource:
    return NodeResource(
        cluster_id=1,
        cluster_name="c1",
        node_id=node_id,
        hostname=node_id,
        nvidia_gpus=0,
        avail_vram_gb=0.0,
        max_card_vram_gb=0.0,
        avail_ram_gb=avail_ram_gb,
        memory_gb=avail_ram_gb,
        realtime_vram=False,
        realtime_ram=False,
    )


@pytest.fixture
def patched(monkeypatch):
    """把 inspect 的重依赖换成离线桩：feasibility 报告、节点资源、预览拓扑、占用。"""

    async def _feas(_db=None, **kw):
        return SimpleNamespace(deployable=True, message="ok", deployable_clusters=[1])

    async def _facts(_db, _cluster_id=None, **_kw):
        return [_node_fact("n1", 60.0), _node_fact("n2", 8.0)]

    async def _preview(*_a, **_kw):
        return None

    async def _views(_db, _uuids, **_kw):
        return {}, {}

    monkeypatch.setattr(inspect_svc, "assess_feasibility", _feas)
    monkeypatch.setattr(inspect_svc, "collect_node_resources", _facts)
    monkeypatch.setattr(inspect_svc, "build_preview_topology", _preview)
    # 占用/现状打在 inspect 消费入口（resolve_card_views）上——inspect 路径不查
    # create 路由用的 resolve_gpu_occupancy，打在 service 源上是从不触发的死桩
    monkeypatch.setattr("llmpod.services.deployment_service.resolve_card_views", _views)
    return monkeypatch


def _req(**kw) -> InspectDeploymentRequest:
    base = dict(
        model_id=7,
        cluster_id=1,
        engine="llamacpp",
        topology=Topology(tp=1, replicas=1, max_model_len=8192),
        allow_cpu=True,
        kv_cache=KVCacheConfig(n_parallel=2),
    )
    base.update(kw)
    return InspectDeploymentRequest(**base)


async def test_cpu_only_memory_and_node_support(patched):
    payload = await route_mod.inspect_deployment(_req(), db=_StubDB())
    body = payload["data"]

    memory = body["memory"]
    assert memory is not None
    assert memory["scenario"] == resource.SCENARIO_CPU_ONLY
    assert memory["recommended_tier"] == 0
    assert memory["tiers"][0]["offload_layers"] == 0

    # node_rows 空（stub 不返回节点行）→ 不渲染 node_support（前端按 node_support 缺失回退）
    assert body["node_support"] == []


class _StubDBWithNodes:
    """答 Model.get + 节点 select（无 GPU 行）：纯 CPU 集群形态，cluster_gpus 恒空。"""

    def __init__(self, nodes):
        self._nodes = nodes

    async def get(self, cls, pk):
        return _MODEL if cls is Model else None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Node:
            return _Rows(self._nodes)
        return _Rows([])


def _cpu_node(node_id: str, memory_gb: float) -> Node:
    return Node(
        id=node_id,
        cluster_id=1,
        hostname=node_id,
        ip="10.0.0.9",
        status="online",
        cpu_cores=8,
        memory_gb=memory_gb,
    )


def _facts_of(nodes):
    """按用例覆写 collect_node_resources 桩：CPU 调度求解的节点池来自它（指定/收窄也在此）。"""

    async def _facts(_db, _cluster_id=None, **_kw):
        return [_node_fact(nid, ram) for nid, ram in nodes]

    return _facts


async def test_cpu_only_placement_recommends_nodes(patched):
    """无 GPU 集群 + 纯 CPU：recommended_placement 出节点级调度（binpack 同创建内核）。"""
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(_req(), db=db)
    body = payload["data"]

    rp = body["recommended_placement"]
    assert rp is not None
    # 无卡：gpu_uuids 恒空（前端不得当手选卡回传，与 allow_cpu 互斥守卫对撞）
    assert rp["gpu_uuids"] == []
    assert rp["cards"] == []
    assert rp["arch"] == "cpu"
    assert rp["total_gpus"] == 0
    # binpack best-fit：单份 ~3.2GB（权重 1.0 + KV），n2 可用 8×0.9=7.2 装得下且余量最小 → 落 n2
    assert rp["node_ids"] == ["n2"]
    plans = rp["replica_plans"]
    assert len(plans) == 1
    assert plans[0]["arch"] == "cpu"
    assert plans[0]["node_ids"] == ["n2"]
    assert plans[0]["units"][0]["gpu_uuids"] == []
    assert plans[0]["fit"] is True

    # 前端节点列表数据源（转交文档口径）：无 GPU 集群上 node_support 仍逐节点可选
    # （cpu_lane=true，因内存可选）；DB 节点行驱动本断言（节点池求解由 _facts_of 驱动）
    assert [(n["hostname"], n["selectable"], n["cpu_lane"]) for n in body["node_support"]] == [
        ("n1", True, True),
        ("n2", True, True),
    ]


async def test_cpu_only_placement_multi_replica_spreads(patched):
    """多副本：逐份 binpack，同节点可承载多份（装得下时），replica_plans 逐份分组。"""
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    payload = await route_mod.inspect_deployment(_req(topology=Topology(tp=1, replicas=2, max_model_len=8192)), db=db)
    rp = payload["data"]["recommended_placement"]
    # 单节点集群：64×0.9=57.6 装得下两份，都落 n1；份序 0/1 各一条
    assert rp["node_ids"] == ["n1"]
    assert [p["node_ids"] for p in rp["replica_plans"]] == [["n1"], ["n1"]]
    assert [p["seq"] for p in rp["replica_plans"]] == [0, 1]


async def test_cpu_only_placement_pinned_node(patched):
    """指定节点（CPU 节点选取）：调度只在指定节点内（不在的节点不进推荐）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(_req(gpu_selector=GpuSelector(node_id="n1")), db=db)
    rp = payload["data"]["recommended_placement"]
    assert rp["node_ids"] == ["n1"]
    assert all(p["node_ids"] == ["n1"] for p in rp["replica_plans"])


async def test_cpu_only_no_fit_reports_issue_without_placement(patched):
    """显式 cpu_only 但节点内存装不下：报阻断 issue、memory 为 None——调度结果不出（无解可推），
    不是「绿灯 + 空调度结果」的静默放行。"""
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 2.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 2.0)])
    payload = await route_mod.inspect_deployment(_req(offload_preference="cpu_only"), db=db)
    body = payload["data"]
    assert body["memory"] is None
    assert body["deployable"] is False
    assert any("装不下" in issue for issue in body["issues"])


async def test_cpu_only_placement_no_fit_keeps_empty_with_issue(patched):
    """单节点装得下一份、装不下两份（replicas=2）：容量红 + 空调度结果（调度壳仍回对象）。

    覆盖 binpack 放不下（picks=None）分支：阻断 issue 来自 _solve_lane 的容量判定，
    调度侧不重复报——「红灯 + fit=true 调度结果」与「绿灯 + 空调度结果」都不许出现。
    """
    # 单节点 avail 卡在 1×need 与 2×need 之间（need 从首个响应的推荐档取，防口径漂移）
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    need = (await route_mod.inspect_deployment(_req(), db=db))["data"]["memory"]["tiers"][0]["ram_gb"]
    mid_total = 1.5 * need / 0.9  # 账本可用 = 总内存×0.9（账本空）= 1.5×need

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", mid_total)]))
    payload = await route_mod.inspect_deployment(
        _req(topology=Topology(tp=1, replicas=2, max_model_len=8192)), db=_StubDBWithNodes([_cpu_node("n1", mid_total)])
    )
    body = payload["data"]
    rp = body["recommended_placement"]
    assert rp is not None
    assert rp["node_ids"] == []
    assert rp["replica_plans"] == []
    assert body["deployable"] is False
    assert any("内存" in issue for issue in body["issues"])


async def test_cpu_only_placement_pins_per_instance_ram(patched):
    """固定调度的每实例内存口径 = 推荐档 tiers[0].ram_gb（与 create 计费同档）。

    节点可用精确卡在 2×need 边界：口径取错档（满 offload 档 ram=0）或翻倍，本用例
    的放得下/放不下都会翻转——量级带断言挡不住的变异在这里挡住。
    """
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    need = (await route_mod.inspect_deployment(_req(), db=db))["data"]["memory"]["tiers"][0]["ram_gb"]
    two_total = (2 * need + 0.01) / 0.9  # avail = 2×need + 0.01（留浮点余量）

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", two_total)]))
    payload = await route_mod.inspect_deployment(
        _req(topology=Topology(tp=1, replicas=2, max_model_len=8192)), db=_StubDBWithNodes([_cpu_node("n1", two_total)])
    )
    rp = payload["data"]["recommended_placement"]
    assert rp["node_ids"] == ["n1"]
    assert [p["node_ids"] for p in rp["replica_plans"]] == [["n1"], ["n1"]]


async def test_cpu_only_per_replica_pins_placement(patched):
    """逐副本指定（显式 cpu_only + node_ids）：第 i 份落 node_ids[i]，调度照指定回显。

    前端 CPU 副本逐份选节点的后端契约：副本1→n1、副本2→n2，互不覆盖（对标 GPU 的
    逐副本选卡）；replica_plans 逐份回显指定节点。
    """
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(
        _req(
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(node_ids=["n1", "n2"]),
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
        ),
        db=db,
    )
    body = payload["data"]
    assert body["deployable"] is True
    rp = body["recommended_placement"]
    assert rp["node_ids"] == ["n1", "n2"]
    assert [p["node_ids"] for p in rp["replica_plans"]] == [["n1"], ["n2"]]
    assert all(p["arch"] == "cpu" for p in rp["replica_plans"])


async def test_cpu_only_per_replica_pins_same_node_stacks(patched):
    """同节点指定多份：两份都指定 n1，调度照指定（容量按份数累加，n1 64GB 装得下两份）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(
        _req(
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(node_ids=["n1", "n1"]),
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
        ),
        db=db,
    )
    rp = payload["data"]["recommended_placement"]
    assert rp["node_ids"] == ["n1"]
    assert [p["node_ids"] for p in rp["replica_plans"]] == [["n1"], ["n1"]]


async def test_cpu_only_per_replica_pins_capacity_red(patched):
    """指定编排超出节点容量：红灯 + 空调度结果（按指定编排逐节点判，不是 binpack 口径——
    n1 装不下两份但 n2 装得下一份，binpack 会误放行「两份都挪去 n2」）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(
        _req(
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(node_ids=["n1", "n1"]),
            topology=Topology(tp=1, replicas=2, max_model_len=200_000),  # 每份 ~30GB，n1 只装得下一份
        ),
        db=db,
    )
    body = payload["data"]
    assert body["deployable"] is False
    assert any("n1" in issue and "指定" in issue for issue in body["issues"])
    # 容量红不回空调度结果：调度结果照给但份级 fit=false（容量类阻断可被 force 绕过，
    # 空调度结果会被前端第二步硬拦、确认页的「强制部署」入口永远够不着）
    rp = body["recommended_placement"]
    assert rp["node_ids"] == ["n1"]
    assert [p["fit"] for p in rp["replica_plans"]] == [False, False]
    assert all(p["issues"] for p in rp["replica_plans"])


async def test_cpu_only_per_replica_pins_length_mismatch(patched):
    """node_ids 长度 ≠ 副本数：结构 issue（长度=副本数是指定编排的硬约束）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    payload = await route_mod.inspect_deployment(
        _req(
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(node_ids=["n1"]),
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
        ),
        db=db,
    )
    body = payload["data"]
    assert body["deployable"] is False
    assert any("逐副本" in issue for issue in body["issues"])


async def test_cpu_only_per_replica_pins_offline_node(patched):
    """指定到不在线节点：红灯（不静默放宽为自动分配——用户指定的就是那台）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    payload = await route_mod.inspect_deployment(
        _req(
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(node_ids=["n1", "n-x"]),
            topology=Topology(tp=1, replicas=2, max_model_len=8192),
        ),
        db=db,
    )
    body = payload["data"]
    assert body["deployable"] is False
    assert any("不在线" in issue for issue in body["issues"])
    # 离线指定只报一条（「不在线」），不再叠「内存装不下」的重复阻断
    assert len(body["issues"]) == 1


async def test_pinned_node_keeps_other_nodes_display(patched):
    """指定收窄只影响求解域：node_support 是展示口径，未指定节点的可用内存/可选性不归零
    （否则前端把有 10GB 空闲的节点判成「CPU 部署 · 内存不足」）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0), ("n2", 8.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0), _cpu_node("n2", 8.0)])
    payload = await route_mod.inspect_deployment(_req(gpu_selector=GpuSelector(node_id="n1")), db=db)
    support = {
        (n["hostname"], n["available_memory_gb"], n["selectable"], n["cpu_lane"])
        for n in payload["data"]["node_support"]
    }
    # 未指定节点 n2：可用内存照实（账本口径 8×0.9=7.2 而非收窄归零/实时 8.0）、因自身内存可选
    assert ("n2", 7.2, True, True) in support
    assert ("n1", 57.6, True, True) in support


async def test_cpu_only_invalid_shape_keeps_issue_not_422(patched):
    """CPU 通道形态非法：结构问题转 issue（200 契约），不给绿灯调度结果、不 422。

    两种非法形态各验一遍：跨机（expand 内嵌的 validate_topology 也会拦）与 tp>1
    （只有 validate_cpu_lane 拦——后者的守卫行是本用例真正锁定的对象）。
    """
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    db = _StubDBWithNodes([_cpu_node("n1", 64.0)])
    shapes = (
        Topology(tp=1, pp=1, nnodes=2, replicas=1, max_model_len=8192),
        Topology(tp=2, pp=1, replicas=1, max_model_len=8192),
    )
    for topo in shapes:
        payload = await route_mod.inspect_deployment(_req(topology=topo), db=db)
        body = payload["data"]
        assert body["deployable"] is False
        assert any("纯 CPU 部署要求" in issue for issue in body["issues"])
        rp = body["recommended_placement"]
        assert rp is not None
        assert rp["arch"] == "cpu"
        assert rp["node_ids"] == []
        assert rp["replica_plans"] == []


async def test_explicit_cpu_only_skips_tp_derivation(patched):
    """显式 cpu_only 不按卡池推导 tp：30GB 模型 + 2×24GB 空卡会推得 tp=2，而 CPU 通道
    恒 tp=pp=1——按卡推导等于替用户改形 → validate_cpu_lane 假红（create 按原 payload
    tp=1 是绿的）。"""
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    payload = await route_mod.inspect_deployment(
        _req(
            model_id=8,
            offload_preference="cpu_only",
            topology=Topology(pp=1, replicas=1, max_model_len=8192),  # 不传 tp（非显式）
        ),
        db=_StubDBWithGpu(gpu_count=2),
    )
    body = payload["data"]
    assert body["deployable"] is True
    assert not any("纯 CPU 部署要求" in issue for issue in body["issues"])
    assert body["recommendation"]["topology"]["tp"] == 1
    assert body["recommended_placement"]["node_ids"] == ["n1"]


async def test_cpu_main_preference_also_skips_tp_derivation(patched):
    """cpu_main（CPU 优先）与 cpu_only 同属取向 CPU：推导 tp 同样会假红。"""
    patched.setattr(inspect_svc, "collect_node_resources", _facts_of([("n1", 64.0)]))
    payload = await route_mod.inspect_deployment(
        _req(
            model_id=8,
            offload_preference="cpu_main",
            topology=Topology(pp=1, replicas=1, max_model_len=8192),
        ),
        db=_StubDBWithGpu(gpu_count=2),
    )
    body = payload["data"]
    assert body["deployable"] is True
    assert not any("纯 CPU 部署要求" in issue for issue in body["issues"])
    assert body["recommendation"]["topology"]["tp"] == 1


async def test_resolved_args_echo_cpu_flags(patched):
    payload = await route_mod.inspect_deployment(_req(), db=_StubDB())
    resolved = payload["data"]["resolved"]
    assert resolved is not None
    args = resolved["args"]
    # 纯 CPU 部署不下发 GPU 语义参数（无卡），镜像按 cpu 变体解析
    assert "--n-gpu-layers" not in args
    assert "llama.cpp:server" in resolved["image"]  # cpu 变体镜像


async def test_allow_cpu_false_keeps_gpu_lane(patched):
    payload = await route_mod.inspect_deployment(_req(allow_cpu=False), db=_StubDB())
    body = payload["data"]
    assert body["memory"] is None
    resolved = body["resolved"]
    assert resolved is not None
    # 未开 CPU 部署：不产部署测算，走全量上卡口径（层数 999）
    assert "--n-gpu-layers" in resolved["args"]
    assert resolved["args"][resolved["args"].index("--n-gpu-layers") + 1] == "999"


async def test_manual_gpu_disables_cpu_lane(patched):
    """手选卡 = 固定显存部署：allow_cpu 被忽略，不产 memory。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    payload = await route_mod.inspect_deployment(_req(gpu_selector=GpuSelector(gpu_uuids=["g0"])), db=_StubDB())
    assert payload["data"]["memory"] is None


async def test_pinned_cpu_node_narrows_lane(patched):
    """指定节点（CPU 节点选取）：逐层表只在该节点上求解（RAM 不足的节点不会被选中）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    payload = await route_mod.inspect_deployment(_req(gpu_selector=GpuSelector(node_id="n2")), db=_StubDB())
    memory = payload["data"]["memory"]
    assert memory is not None
    # n2 只有 8GB 可用内存（stub 事实）→ 纯 CPU 装不下 → 无可行档
    assert memory is None or memory["scenario"] != "cpu_only" or memory["ram_avail_gb"] <= 8.0


async def test_pinned_offline_node_reports_undeployable(patched):
    """指定到不在线节点：不得静默放宽为全部节点报可部署（部署路径对同一请求硬拒）。"""
    from llmpod.apps.manager.deployment.schemas import GpuSelector

    payload = await route_mod.inspect_deployment(_req(gpu_selector=GpuSelector(node_id="n-gone")), db=_StubDB())
    body = payload["data"]
    assert body["deployable"] is False
    assert any("不在线" in issue for issue in body["issues"])


def test_manual_gpu_plus_allow_cpu_rejected(patched):
    """create 路由守卫：allow_cpu 与手选 GPU 互斥 → 422（LLMPodValidationError）。"""
    import asyncio

    from llmpod.apps.webserver.auth.session import SessionData
    from llmpod.apps.webserver.routes.admin.deployment import CreateDeploymentRequest, create_deployment
    from llmpod.apps.manager.deployment.schemas import DeploymentConfig, GpuSelector
    from llmpod.core.exceptions import LLMPodValidationError

    req = CreateDeploymentRequest(
        cluster_id=1,
        name="x",
        model_id=7,
        engine="llamacpp",
        spec=DeploymentConfig(
            topology=Topology(tp=1),
            allow_cpu=True,
            gpu_selector=GpuSelector(gpu_uuids=["g0"]),
        ),
    )
    user = SessionData(
        user_id=1,
        username="tester",
        tenant_id=0,
        access_domain="platform",
        role_codes=["super_admin"],
        role_ids=[1],
        is_superuser=True,
    )
    with pytest.raises(LLMPodValidationError, match="互斥"):
        asyncio.run(create_deployment(req, db=_StubDB(), user=user))


def test_manual_gpu_plus_explicit_cpu_only_rejected(patched):
    """回归：显式 cpu_only（allow_cpu 未给）+ 手选 GPU 也须被互斥门拦下——旧判据只认
    allow_cpu，该请求被静默接受后调度走纯 CPU 分支，用户手选的卡被丢弃且架构/显存
    校验全跳过，预览侧同一请求却报不可部署。"""
    import asyncio

    from llmpod.apps.webserver.auth.session import SessionData
    from llmpod.apps.webserver.routes.admin.deployment import CreateDeploymentRequest, create_deployment
    from llmpod.apps.manager.deployment.schemas import DeploymentConfig, GpuSelector
    from llmpod.core.exceptions import LLMPodValidationError

    req = CreateDeploymentRequest(
        cluster_id=1,
        name="x",
        model_id=7,
        engine="llamacpp",
        spec=DeploymentConfig(
            topology=Topology(tp=1),
            allow_cpu=False,
            offload_preference="cpu_only",
            gpu_selector=GpuSelector(gpu_uuids=["g0"]),
        ),
    )
    user = SessionData(
        user_id=1,
        username="tester",
        tenant_id=0,
        access_domain="platform",
        role_codes=["super_admin"],
        role_ids=[1],
        is_superuser=True,
    )
    with pytest.raises(LLMPodValidationError, match="互斥"):
        asyncio.run(create_deployment(req, db=_StubDB(), user=user))


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return list(self._rows)


class _StubDBWithGpu:
    """答 Model.get + Node/Gpu select：n1（64GB 内存）+ N 张 24GB 卡（gpu_count 定）。

    单卡供显存不足分流用例；两卡供 tp 推导用例（30GB 模型按卡推导可达 tp=2）。
    """

    def __init__(self, gpu_count: int = 1):
        self._gpu_count = gpu_count

    async def get(self, cls, pk):
        return _BIG_MODEL if cls is Model else None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Node:
            return _Rows(
                [
                    Node(
                        id="n1",
                        cluster_id=1,
                        hostname="n1",
                        ip="10.0.0.1",
                        status="online",
                        cpu_cores=8,
                        memory_gb=64.0,
                    )
                ]
            )
        if entity is Gpu:
            return _Rows(
                [
                    Gpu(
                        uuid=f"g{i}",
                        node_id="n1",
                        index=i,
                        name="RTX",
                        arch="nvidia",
                        total_vram_gb=24.0,
                        status="online",
                        driver_version="570",
                    )
                    for i in range(self._gpu_count)
                ]
            )
        return _Rows([])


_BIG_MODEL = Model(
    id=8,
    name="big-gguf",
    model_type="llm",
    size_gb=30.0,  # 24GB 卡装不下（全量档不可行）→ 只能纯 CPU
    format="gguf",
    num_layers=32,
    num_kv_heads=8,
    head_dim=128,
)


async def test_cpu_lane_skips_gpu_shortage_issue(patched):
    """纯 CPU 调度不报「显存不足请提高 tp」：无卡可推是预期（集群里有卡但装不下），
    调度改出节点级 node_ids（本次变更后此场景不再是空调度结果）。"""
    payload = await route_mod.inspect_deployment(_req(model_id=8), db=_StubDBWithGpu())
    body = payload["data"]
    assert body["memory"] is not None and body["memory"]["scenario"] == "cpu_only"
    # 前提：集群里有空闲卡但装不下（否则「显存不足」分流块本来就不会触发）
    gpu = next(g for g in body["cluster_gpus"] if g["uuid"] == "g0")
    assert gpu["required_vram_gb"] > gpu["available_vram_gb"] * 0.9
    rp = body["recommended_placement"]
    assert rp["gpu_uuids"] == []
    assert rp["node_ids"] == ["n1"]  # CPU 调度：落到 n1（64GB 内存）
    assert not any("提高 tp" in issue for issue in body["issues"])


async def test_gpu_lane_fits_without_double_discount(patched):
    """回归：全量上卡档的显存复核只折一次。

    24GB 空 → 账本可用 21.6（已折 0.9），每卡需求 ~20GB 落在 (0.9×21.6, 21.6]：
    旧实现对已折的可用量再乘 0.9（19.44 < 20）误报「装不下」，同一请求 create 走
    reserve_fits 单次折让却能成功——预检把可部署的配置劝退。
    """
    fit_model = Model(
        id=9,
        name="fit-gguf",
        model_type="llm",
        size_gb=19.0,  # 全量上卡档每卡 = 19+2(KV) = 21GB：> 21.6×0.9=19.44 且 ≤ 21.6（旧双重折让的分歧行）
        format="gguf",
        num_layers=32,
        num_kv_heads=8,
        head_dim=128,
    )

    class _DB(_StubDB):
        async def get(self, cls, pk):
            return fit_model if cls is Model else None

        async def execute(self, stmt):
            entity = stmt.column_descriptions[0].get("entity")
            if entity is Node:
                return _Rows(
                    [
                        Node(
                            id="n1",
                            cluster_id=1,
                            hostname="h1",
                            ip="10.0.1.5",
                            status="online",
                            cpu_cores=8,
                            memory_gb=64.0,
                        )
                    ]
                )
            if entity is Gpu:
                return _Rows(
                    [
                        Gpu(
                            uuid="g0",
                            node_id="n1",
                            index=0,
                            name="RTX",
                            arch="nvidia",
                            total_vram_gb=24.0,
                            status="online",
                            driver_version="570",
                        )
                    ]
                )
            return _Rows([])

    payload = await route_mod.inspect_deployment(_req(model_id=9), db=_DB())
    body = payload["data"]
    assert body["memory"] is not None and body["memory"]["scenario"] == resource.SCENARIO_GPU_ONLY
    assert body["deployable"] is True
    assert not any("装不下" in issue for issue in body["issues"])


async def test_allow_cpu_issue_for_non_cpu_lane_engine(patched):
    """引擎能力准入：vLLM 未声明 CPU 部署 → 请求 allow_cpu 直接报不可部署（不静默按显存解）。"""
    payload = await route_mod.inspect_deployment(
        _req(engine="vllm", allow_cpu=True, kv_cache=KVCacheConfig()), db=_StubDB()
    )
    body = payload["data"]
    assert body["deployable"] is False
    assert any("不支持 CPU / 内存部署" in issue for issue in body["issues"])
    assert body["memory"] is None


def test_allow_cpu_rejected_for_non_cpu_lane_engine(patched):
    """create 路由引擎能力准入：vLLM 未声明 CPU 部署 → allow_cpu 422（llama.cpp 放行见上）。"""
    import asyncio

    from llmpod.apps.webserver.auth.session import SessionData
    from llmpod.apps.webserver.routes.admin.deployment import CreateDeploymentRequest, create_deployment
    from llmpod.apps.manager.deployment.schemas import DeploymentConfig
    from llmpod.core.exceptions import LLMPodValidationError

    req = CreateDeploymentRequest(
        cluster_id=1,
        name="x",
        model_id=7,
        engine="vllm",
        spec=DeploymentConfig(topology=Topology(tp=1), allow_cpu=True),
    )
    user = SessionData(
        user_id=1,
        username="tester",
        tenant_id=0,
        access_domain="platform",
        role_codes=["super_admin"],
        role_ids=[1],
        is_superuser=True,
    )
    with pytest.raises(LLMPodValidationError, match="不支持 CPU / 内存部署"):
        asyncio.run(create_deployment(req, db=_StubDB(), user=user))


class _StubDBNoMeta:
    """模型缺架构元数据（num_layers 为空）+ n1 一张 24GB 卡 + 64GB 内存。"""

    async def get(self, cls, pk):
        return _NO_META_MODEL if cls is Model else None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Node:
            return _Rows(
                [
                    Node(
                        id="n1",
                        cluster_id=1,
                        hostname="n1",
                        ip="10.0.0.1",
                        status="online",
                        cpu_cores=8,
                        memory_gb=64.0,
                    )
                ]
            )
        if entity is Gpu:
            return _Rows(
                [
                    Gpu(
                        uuid="g0",
                        node_id="n1",
                        index=0,
                        name="RTX",
                        arch="nvidia",
                        total_vram_gb=24.0,
                        status="online",
                        driver_version="570",
                    )
                ]
            )
        return _Rows([])


_NO_META_MODEL = Model(
    id=9,
    name="no-meta-gguf",
    model_type="llm",
    size_gb=20.5,
    format="gguf",
    num_layers=None,  # 缺元数据 → 逐层表无法建，memory 为 None
)


async def test_cpu_only_without_model_meta_reports_meta_issue(patched):
    """模型缺架构元数据 + 显式 cpu_only：报「无法测算」而非 GPU 侧「显存不足请提高 tp」。"""
    payload = await route_mod.inspect_deployment(_req(model_id=9, offload_preference="cpu_only"), db=_StubDBNoMeta())
    body = payload["data"]
    assert body["memory"] is None
    assert not any("提高 tp" in issue for issue in body["issues"])
    assert any("元数据" in issue for issue in body["issues"])
    assert body["deployable"] is False


def test_instance_spec_cpu_fields_round_trip():
    """CPU 调度字段随 Instance.spec（JSONB）持久化，读回一致。"""
    inst = InstanceSpec(
        instance_id="i1",
        offload_layers=0,
        ram_claim_gb=3.0,
        assigned_gpus=[],
        gpu_arch="cpu",
    )
    restored = InstanceSpec.model_validate(inst.model_dump())
    assert restored.offload_layers == 0
    assert restored.ram_claim_gb == 3.0
    assert restored.assigned_gpus == []
