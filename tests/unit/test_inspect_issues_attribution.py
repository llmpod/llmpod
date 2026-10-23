"""inspect 的 issue 归因分支单测（FakeSession 直调路由，零外部依赖）。

锁定「为什么放不下/哪张卡为什么灰/手选为什么被拒」的文案分流：每个请求至多命中
一条最具体的原因，分支错了前端就会给出误导性提示。迁移/重构 inspect 编排时，
本文件与 test_inspect_occupancy.py 一起构成行为锁——**issues 顺序与文案逐条不变**。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import GpuSelector, Topology
from llmpod.apps.webserver.routes.admin import deployment as route_mod
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node import Node


class FakeSession:
    def __init__(self):
        self._store: dict = {}
        self._staged: list = []
        self._counters: dict = {}

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

    def _pk(self, obj):
        pk = list(obj.__table__.primary_key.columns)
        return getattr(obj, pk[0].key, None) if pk else None

    async def get(self, cls, pk):
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

    async def flush(self):
        for obj in self._staged:
            cls = type(obj)
            pk = self._pk(obj)
            if pk is None and hasattr(obj, "id"):
                n = self._counters.get(cls, 0) + 1
                self._counters[cls] = n
                obj.id = n
                pk = n
            self._store.setdefault(cls, {})[pk] = obj
        self._staged = []

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, *_a, **_k):
        pass

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        objs = list(self._store.get(entity, {}).values())
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None) or getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                objs = [o for o in objs if getattr(o, wc.left.key, None) in val]
            else:
                objs = [o for o in objs if getattr(o, wc.left.key, None) == val]
        return _Result(objs)

    async def delete(self, obj):
        for d in self._store.values():
            for k in [k for k, v in d.items() if v is obj]:
                del d[k]


class _Result:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self._objs

    def scalar_one_or_none(self):
        return self._objs[0] if self._objs else None


def _gpu(i, uuid, *, arch="nvidia", name="NVIDIA GeForce RTX 2080 Ti", drv="595.84", total=22.0, status="online"):
    return Gpu(
        id=i,
        uuid=uuid,
        node_id="n1",
        index=i,
        name=name,
        arch=arch,
        driver_version=drv,
        total_vram_gb=total,
        status=status,
        spec={},
    )


def _seed(model, *gpus):
    s = FakeSession()
    s.preload(model)
    s.preload(Node(id="n1", hostname="n1", ip="10.0.0.1", cluster_id=1, status="online"))
    for g in gpus:
        s.preload(g)
    return s


def _inspect(session, **kw):
    base = dict(model_id=1, cluster_id=1, engine="vllm", topology=Topology(tp=1, max_model_len=8192))
    base.update(kw)
    return route_mod.inspect_deployment(InspectDeploymentRequest(**base), session)


_BIG_META_MODEL = Model(
    id=1,
    name="m",
    model_type="llm",
    size_gb=30.0,
    format="safetensors",
    max_context=4096,
    needs_trust_remote_code=True,
)


class TestManualCapacityAttribution:
    """手选卡容量预检的分支①（模型装不下）与分支③（平台外进程实时占用）。

    分支②（平台内占用，带占用方名）见 test_inspect_occupancy.py
    的 manual_selection_occupied_issue。判定双闸取严：心跳实况 + 账本余量，
    一张卡至多报一条，按最具体的原因分流。
    """

    async def test_need_exceeds_platform_default_reserve(self):
        """30GB 模型手选 22GB 空卡跑 tp=1：预留（90%×22=19.8）< 每卡需求 → 分支①。

        文案要说清两件事：预留按平台默认占比（不看用户显存覆盖）、出路是提高 tp 或换大卡。
        """
        s = _seed(Model(id=1, name="m", model_type="llm", size_gb=30.0, format="safetensors"), _gpu(1, "g1"))
        result = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        assert result["data"]["deployable"] is False
        assert result["data"]["issues"] == [
            "GPU g1 按平台默认显存占比 90% 只能预留 19.8GB，装不下本部署每卡需求 33.0GB；请提高 tp 或换更大显存的卡"
        ]

    async def test_realtime_usage_blocks_reserve(self, monkeypatch):
        """空卡（无平台内占用）但心跳实时已用 15GB：可用 7 < 需预留 19.8 → 分支③。

        卡上没有任何声明行、模型也装得下占比上限——只有平台外进程这一种解释，
        文案须指「确认占用已释放」而不是劝用户提高 tp。
        """
        monkeypatch.setattr(inspect_svc, "load_used_vram_by_uuid", lambda: {"g1": 15.0})
        s = _seed(Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"), _gpu(1, "g1"))
        result = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        assert result["data"]["deployable"] is False
        assert result["data"]["issues"] == [
            "GPU g1 实时可用显存不足：可用 7.0GB < 需预留 19.8GB（卡上已有占用，可能是平台外进程）；"
            "请换卡或确认占用已释放"
        ]


class TestAllGreyAttribution:
    """集群卡全灰时的归因：按灰化原因全集说人话（no_image 分支见 test_inspect_occupancy.py）。"""

    async def test_arch_mismatch_named_per_engine(self):
        """llamacpp 只支持 nvidia/cpu → cann 卡全灰：说「架构均不被该引擎支持」。"""
        s = _seed(
            Model(id=1, name="m", model_type="llm", size_gb=1.0, format="gguf"),
            _gpu(1, "c1", arch="cann", name="Ascend 310P3", drv="25.2.3", total=24.0),
        )
        result = await _inspect(s, engine="llamacpp")
        assert result["data"]["deployable"] is False
        assert result["data"]["issues"] == ["集群 GPU 架构均不被引擎 llamacpp 支持"]
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["c1"]["grey_reason"] == "arch_mismatch"


class TestEngineCapabilityGates:
    """引擎能力闸（判据与 create 同源；级别按角色）：类别不符是确定性硬伤 → 预检
    直接阻断；并行未声明是策略性风险（引擎实际可能接受）→ 预检只黄灯提醒并把
    「force 可绕」指给用户，硬拒在 create 第一遍。"""

    async def test_category_mismatch_blocks(self):
        """vLLM 不服务 image 类别 → 预检直接不可部署（create 同 payload 422）。"""
        s = _seed(
            Model(id=1, name="m", model_type="image", size_gb=1.0, format="safetensors"),
            _gpu(1, "g1"),
        )
        result = await _inspect(s)
        assert result["data"]["deployable"] is False
        assert any("不支持模型类别" in i and "image" in i for i in result["data"]["issues"])

    async def test_parallel_undeclared_warns_without_blocking(self, monkeypatch):
        """pp=2 + 引擎未声明 pp（压掉声明模拟未测引擎）→ 黄灯提醒（deployable 不受
        影响），文案带「force=true」出路。vllm 现已声明 pp，主路径见
        test_parallel_declared_no_warning。"""
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter

        monkeypatch.setattr(VLLMAdapter, "supported_parallel", ["tp"])
        s = _seed(_M_LLM(), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(s, topology=Topology(tp=1, pp=2, max_model_len=8192))
        d = result["data"]
        assert d["deployable"] is True
        assert d["issues"] == []
        assert any("未声明支持流水线并行" in w and "force=true" in w for w in d["warnings"])

    async def test_parallel_declared_no_warning(self):
        """pp=2 + vLLM 已声明 pp（多机 pp 形态放行后）→ 无并行类黄灯，直接绿灯。"""
        s = _seed(_M_LLM(), _gpu(1, "g1"), _gpu(2, "g2"))
        result = await _inspect(s, topology=Topology(tp=1, pp=2, max_model_len=8192))
        d = result["data"]
        assert d["deployable"] is True
        assert d["issues"] == []
        assert not any("流水线并行" in w for w in d["warnings"])

    async def test_multinode_flag_echoed_by_engine(self):
        """recommendation.supports_multinode 回显：vllm True；llamacpp（单机引擎）False。"""
        s = _seed(Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"), _gpu(1, "g1"))
        assert (await _inspect(s))["data"]["recommendation"]["supports_multinode"] is True

        s_gguf = _seed(
            Model(id=1, name="m", model_type="llm", size_gb=1.0, format="gguf"),
            _gpu(1, "g1"),
        )
        result = await _inspect(s_gguf, engine="llamacpp")
        assert result["data"]["recommendation"]["supports_multinode"] is False

    async def test_multinode_flag_version_aware(self):
        """锁定旧 vllm 镜像（0.8.5 无 elastic 多机参数，词表 multinode=false）→ 回显
        False：前端据此不放 nnodes 控件，而不是提交后才异步失败。未锁定版本 → 按最新。"""
        s = _seed(Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"), _gpu(1, "g1"))
        assert (await _inspect(s, image_tag="0.8.5"))["data"]["recommendation"]["supports_multinode"] is False
        assert (await _inspect(s, image_tag="0.23.0"))["data"]["recommendation"]["supports_multinode"] is True


def _M_LLM():
    return Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors")

    async def test_all_offline_cards(self):
        """节点在线但卡全部离线 → 报「全部离线」，不落进笼统的「无可用 GPU」。"""
        s = _seed(
            Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"),
            _gpu(1, "g1", status="offline"),
        )
        result = await _inspect(s)
        assert result["data"]["deployable"] is False
        assert result["data"]["issues"] == ["集群 GPU 全部离线，无可选卡"]


class TestEmptyPlacementAttribution:
    """推荐调度为空的原因分流（「提高 tp」/「释放卡」两分支见 test_inspect_occupancy.py）。"""

    async def test_no_schedulable_arch_for_topology(self, monkeypatch):
        """全 cann 集群 + pp=2：卡不灰（镜像在、架构受支持）但拓扑只有 nvidia 服务得了
        → arch_candidates 空，分流到「没有可用于本引擎/本拓扑的架构」。vllm 现已声明
        pp，压掉声明让「黄灯提醒」分支回归（cann 上 pp 被 topology_conflict 剔除的另一
        归因路径不受此影响）。"""
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter

        monkeypatch.setattr(VLLMAdapter, "supported_parallel", ["tp"])
        s = _seed(
            Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"),
            _gpu(1, "c1", arch="cann", name="Ascend 310P3", drv="25.2.3", total=24.0),
            _gpu(2, "c2", arch="cann", name="Ascend 310P3", drv="25.2.3", total=24.0),
        )
        result = await _inspect(s, topology=Topology(tp=1, pp=2, max_model_len=8192), image_tag="0.22.1")
        assert result["data"]["deployable"] is False
        issues = result["data"]["issues"]
        # 引擎未声明 pp → 黄灯提醒（不进 issues）；通用 dry-run 按默认 arch 解析不出
        # 可用 nvidia 镜像报一条（0.22.1 有候选条目但全被驱动/型号门控拦下，
        # 报「未找到兼容镜像+候选要求」；部署侧归因不重复吞掉）
        assert issues[0].startswith("未找到兼容镜像（vllm/nvidia@0.22.1）")
        assert issues[1] == (
            "集群没有可用于本引擎/本拓扑的架构（`vllm` 支持 ['nvidia', 'cann']，请检查镜像 tag 与卡的在线上报）"
        )
        assert any("未声明支持流水线并行" in w for w in result["data"]["warnings"])
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert all(g["grey_reason"] is None for g in gpus.values())  # 卡没灰，是拓扑不匹配

    async def test_unknown_size_falls_back_to_card_count(self):
        """模型体积未知（required=None）→ 无法比显存，按卡数口径提示（不带显存数字）。

        tp=2 需 2 张卡、空闲仅 1 张（另 1 张被运行中部署占用）：结构性探测能过
        （全池 2 张摆得下）、推荐调度为空 → 容量口径的「缩小并行度」分支。
        """
        s = _seed(
            Model(id=1, name="m", model_type="llm", size_gb=None, format="safetensors"), _gpu(1, "g1"), _gpu(2, "g2")
        )
        s.preload(
            ModelDeployment(
                deployment_id="dep-x",
                cluster_id=1,
                name="busy",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={},
            )
        )
        s.preload(
            DeploymentGpuAssignment(
                id=1,
                deployment_id="dep-x",
                gpu_uuid="g2",
                allocated_vram_gb=19.8,
                gpu_memory_utilization=0.9,
            )
        )
        result = await _inspect(s, topology=Topology(tp=2, max_model_len=8192))
        assert result["data"]["deployable"] is False
        assert result["data"]["issues"] == [
            "当前 tp=2×replicas=1 需要更多可用卡：集群空闲的 nvidia 卡不足；请释放占用卡或缩小并行度"
        ]


class TestIssuesOrderPin:
    """issues / warnings 追加顺序锁定：与编排内判定段落的行序一一对应。

    用户同时踩多个问题时，各列表内顺序不得变（前端按列表顺序渲染）；
    超上下文/信任远程代码属提醒（warnings），手选卡容量属阻断（issues）。
    """

    async def test_full_order_for_multi_problem_request(self):
        s = _seed(_BIG_META_MODEL, _gpu(1, "g1"))
        result = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        d = result["data"]
        assert d["warnings"] == [
            "max_model_len 8192 超过模型自带上下文上限 4096，引擎可能拒绝启动（可保持默认值或调小）",
            "该模型带自定义代码（config auto_map），创建部署时将自动在引擎参数里加 --trust-remote-code"
            "（模型仓库里的 Python 将在引擎容器内执行，请确认来源可信）",
        ]
        assert d["issues"] == [
            "GPU g1 按平台默认显存占比 90% 只能预留 19.8GB，装不下本部署每卡需求 31.5GB；请提高 tp 或换更大显存的卡",
        ]


class TestLaneAttributionDistinctVocabulary:
    """部署形态归因的专用文案：与 GPU 侧「提高 tp / 换大卡」分流词表刻意不同。

    内存口径装不下时劝用户提高 tp 只会更糟（权重根本没上卡）——这些分支在
    test_inspect_cpu_lane.py 有行为断言，这里补充锁定文案分流，防迁移时被并进 GPU 侧。
    """

    async def test_cpu_only_insufficient_ram_wording(self, patched):
        """显式 cpu_only + 指定节点内存装不下 → 「目标节点内存装不下纯 CPU 部署」。"""
        from llmpod.apps.manager.deployment.feasibility import NodeResource

        def _fact(node_id: str, avail_ram_gb: float) -> NodeResource:
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

        async def _facts(_db, _cluster_id=None, **_kw):
            return [_fact("n1", 2.0)]  # 30GB 权重 + KV，2GB 内存必装不下

        patched.setattr(inspect_svc, "collect_node_resources", _facts)
        s = FakeSession()
        s.preload(Model(id=1, name="m", model_type="llm", size_gb=30.0, format="gguf", num_layers=32))
        s.preload(Node(id="n1", hostname="n1", ip="10.0.0.1", cluster_id=1, status="online", memory_gb=2.0))
        result = await route_mod.inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="llamacpp",
                topology=Topology(tp=1, max_model_len=8192),
                gpu_selector=GpuSelector(node_id="n1"),
                allow_cpu=True,
                offload_preference="cpu_only",
            ),
            s,
        )
        assert result["data"]["deployable"] is False
        assert any("目标节点内存装不下纯 CPU 部署" in i for i in result["data"]["issues"])
        assert not any("提高 tp" in i or "换更大显存" in i for i in result["data"]["issues"])


@pytest.fixture
def patched(monkeypatch):
    """与 test_inspect_cpu_lane 同款离线桩：feasibility 报告 + 预览拓扑。"""

    async def _feas(_db=None, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(deployable=True, message="ok", deployable_clusters=[1])

    async def _preview(*_a, **_kw):
        return None

    monkeypatch.setattr(inspect_svc, "assess_feasibility", _feas)
    monkeypatch.setattr(inspect_svc, "build_preview_topology", _preview)
    return monkeypatch
