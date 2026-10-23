"""inspect_deployment 占用回显 / 推荐过滤 / 手选占用 issue 单测（节点共享冲突避免）。

直接调用路由函数（FakeSession 种子 Cluster GPU + 其他部署的 assignment）：
- cluster_gpus 盖 declared/occupied_pct/occupied_by（status-aware）；
- 推荐只推零占用卡（被占卡绝不自动推荐）；
- 手选被占卡剩余不足 → issues 明细 + deployable=False。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.webserver.routes.admin.deployment import inspect_deployment
from llmpod.apps.webserver.schemas.deployment_inspect import (
    GpuSelector,
    InspectDeploymentRequest,
)
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node import Node
from llmpod.models.node_model import NodeModel


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


def _seed(session, *, occ_status="running"):
    """n1 两张卡：g1 被其他部署声明 20GB（共 22GB），g2 空闲（24GB）。"""
    session.preload(
        Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=1.0,
            format="safetensors",
        )
    )
    session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
    session.preload(
        Gpu(
            id=1,
            uuid="g1",
            node_id="n1",
            index=0,
            name="A100",
            arch="nvidia",
            total_vram_gb=22.0,
            status="online",
            spec={},
        )
    )
    session.preload(
        Gpu(
            id=2,
            uuid="g2",
            node_id="n1",
            index=1,
            name="A100",
            arch="nvidia",
            total_vram_gb=24.0,
            status="online",
            spec={},
        )
    )
    session.preload(
        ModelDeployment(
            deployment_id="dep-other",
            cluster_id=1,
            name="busy",
            model_id=1,
            engine="vllm",
            status=occ_status,
            phase="RUNNING",
            spec={},
        )
    )
    session.preload(
        DeploymentGpuAssignment(
            id=1,
            deployment_id="dep-other",
            gpu_uuid="g1",
            allocated_vram_gb=20.0,
            gpu_memory_utilization=0.9,
        )
    )


@pytest.fixture
def session():
    return FakeSession()


class TestInspectOccupancy:
    async def test_stamp_and_recommend_free_only(self, session):
        """cluster_gpus 盖占用字段；推荐只推零占用的 g2。"""
        _seed(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["declared_vram_gb"] == 20.0
        assert gpus["g1"]["occupied_pct"] == pytest.approx(20.0 / 22.0, abs=1e-3)
        assert gpus["g1"]["occupied_by"][0]["deployment_name"] == "busy"
        assert gpus["g2"]["occupied_by"] == [] and gpus["g2"]["declared_vram_gb"] == 0
        # 推荐只推零占用卡：g1 被占 → 只推 g2
        rec = result["data"]["recommended_placement"]
        assert rec is not None
        assert "g1" not in rec["gpu_uuids"]
        assert rec["gpu_uuids"]

    async def test_stopped_occupier_stamped_free(self, session):
        """占用方 stopped → 不计占用（残留 assignment 不再灰化推荐）。"""
        _seed(session, occ_status="stopped")
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["occupied_by"] == []
        # g1 恢复可推荐（两张卡都在池里，可能任选，但 g1 不再被排除）
        rec = result["data"]["recommended_placement"]["gpu_uuids"]
        assert "g1" in rec or "g2" in rec

    async def test_manual_selection_occupied_issue(self, session):
        """手选被占卡剩余不足 → issues 明细 + deployable=False。"""
        _seed(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            ),
            session,
        )
        assert not result["data"]["deployable"]
        assert any("busy" in i and "占用" in i for i in result["data"]["issues"])

    async def test_manual_selection_explicit_tp_scales_per_card_need(self, session):
        """手选卡 + 显式 tp=2：容量门槛按每卡需求（整模型 ÷ 2），与 create 同口径。

        复刻误报：30GB 模型选两张 22GB 空卡跑 tp=2（每卡 16.5GB），旧口径拿整模型
        33GB 去比单卡 → 误报「装不下」；实际整卡预留 19.8 ≥ 16.5、可用 22 ≥ 19.8 都过。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=30.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        for idx, uuid in enumerate(("g1", "g2")):
            session.preload(
                Gpu(
                    id=idx + 1,
                    uuid=uuid,
                    node_id="n1",
                    index=idx,
                    name="A100",
                    arch="nvidia",
                    total_vram_gb=22.0,
                    status="online",
                    spec={},
                )
            )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=2, max_model_len=8192),
                gpu_selector=GpuSelector(gpu_uuids=["g1", "g2"]),
            ),
            session,
        )
        assert result["data"]["deployable"]
        assert not any("装不下" in i for i in result["data"]["issues"])

    async def test_cann_pp_reject_becomes_issue_not_422(self, session):
        """手选 cann 卡 + pp=2：组装期硬拒转 issue + deployable=False，不 422 整个预检。

        dry-run 的 assemble_args 对 cann+pp 会抛结构级异常；不包住的话用户看到的是
        整个请求报错，改完参数也没法继续预览（上方的 arch 预检 issue 同样被吞掉）。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        for idx, uuid in enumerate(("c1", "c2")):
            session.preload(
                Gpu(
                    id=idx + 1,
                    uuid=uuid,
                    node_id="n1",
                    index=idx,
                    name="Ascend 310P3",
                    arch="cann",
                    driver_version="25.2.3",
                    total_vram_gb=24.0,
                    status="online",
                    spec={},
                )
            )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1, pp=2),
                gpu_selector=GpuSelector(gpu_uuids=["c1", "c2"]),
                image_tag="0.22.1",
            ),
            session,
        )
        data = result["data"]
        assert not data["deployable"]
        assert any("流水线并行" in i for i in data["issues"])

    async def test_manual_selection_still_gets_recommended_placement(self, session):
        """手选也要给推荐调度：架构序为空在调度内核里是「上游把架构全闸掉了」的否决信号，
        会让推荐调度整片回空，而同一响应里 replica_plans 还在描述选集。"""
        _seed(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                gpu_selector=GpuSelector(gpu_uuids=["g2"]),
            ),
            session,
        )
        rec = result["data"]["recommended_placement"]
        assert rec is not None and rec["gpu_uuids"], "手选路径的推荐调度不该为空"

    async def test_cann_selection_uses_selected_card_arch(self, session):
        """选 cann 卡 → 校验 arch 从所选卡推断（不再误拒「需 nvidia」，与部署期 orchestrator 同口径）。"""
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="c1",
                node_id="n1",
                index=0,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=24.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                gpu_selector=GpuSelector(gpu_uuids=["c1"]),
                image_tag="0.22.1",
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["c1"]["grey_reason"] is None  # 合法 cann tag → 可选
        assert not any("需 nvidia" in i for i in result["data"]["issues"])
        ns = {n["node_id"]: n for n in result["data"]["node_support"]}
        assert ns["n1"]["selectable"] is True

    async def test_cann_no_image_greys_card_and_node(self, session):
        """cann 卡 + 无对应条目的 image_tag → 卡 grey=no_image + 节点 selectable=False + issue 提示。

        310P3 在 0.22.1/0.23.0/0.26.0/0.27.1 均有（高版本为补位条目），用确无条目的 0.20.0 验证。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="c1",
                node_id="n1",
                index=0,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=24.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                gpu_selector=GpuSelector(gpu_uuids=["c1"]),
                image_tag="0.20.0",
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["c1"]["grey_reason"] == "no_image"
        assert "0.20.0" in gpus["c1"]["grey_detail"]
        ns = {n["node_id"]: n for n in result["data"]["node_support"]}
        assert ns["n1"]["selectable"] is False
        assert any("未找到" in i and "0.20.0" in i for i in result["data"]["issues"])

    async def test_recommendation_falls_back_to_cann_arch(self, session):
        """全 Ascend 集群（无 nvidia 卡）→ 推荐回退到 cann arch（非空，前端可据此自动选中）。"""
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="c1",
                node_id="n1",
                index=0,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=24.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                image_tag="0.22.1",
            ),
            session,
        )
        rec = result["data"]["recommended_placement"]
        assert rec is not None
        assert rec["gpu_uuids"] == ["c1"]
        assert rec["node_ids"] == ["n1"]
        assert rec["arch"] == "cann"
        # auto 路径 dry-run/容量校验按集群真实 arch（cann），不再误报「0 张 nvidia 卡」
        assert not any("nvidia" in i for i in result["data"]["issues"])

    async def test_bad_image_tag_reports_cann_image_missing(self, session):
        """cann 集群 + 无该 arch 版本的 image_tag（0.20.0）→ 明确报 vllm/cann 镜像缺失 + 推荐为空。

        310P3 在 0.22.1-0.27.1 均有（高版本为补位条目），用确无条目的版本验证缺失分支。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="c1",
                node_id="n1",
                index=0,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=24.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                image_tag="0.20.0",
            ),
            session,
        )
        assert any("未找到" in i and "0.20.0" in i for i in result["data"]["issues"])
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["c1"]["grey_reason"] == "no_image"
        assert not result["data"]["deployable"]
        rec = result["data"]["recommended_placement"]
        assert rec["gpu_uuids"] == []  # 该 tag 无可用卡 → 无推荐

    async def test_nvidia_old_driver_with_0271_reports_driver_requirement(self, session):
        """nvidia 卡驱动 <575 + image_tag=0.27.1（cu130 需 >=580、cu129 需 >=575）→ 卡灰 no_image + issues 明确驱动要求 + 推荐为空。

        通用 dry-run（驱动未知）能解析出镜像，但真实卡驱动不满足 → 集群级原因顶到 issues。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="g1",
                node_id="n1",
                index=0,
                name="A100",
                arch="nvidia",
                driver_version="550.54",
                total_vram_gb=80.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                image_tag="0.27.1",
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["grey_reason"] == "no_image"
        assert ">=580" in gpus["g1"]["grey_detail"]
        assert any(
            ">=575" in i for i in result["data"]["issues"]
        )  # 集群级原因顶到 issues（cu129/cu130 两档驱动要求都列出）
        assert not result["data"]["deployable"]
        assert result["data"]["recommended_placement"]["gpu_uuids"] == []

    async def test_mixed_cann_and_nvidia_0271_recommends_nvidia(self, session):
        """混合集群（cann 310P + nvidia 2080Ti@595.84）+ tag 0.27.1 → 两 arch 均可选，推荐 nvidia（默认 arch 优先）。"""
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="ascend", ip="192.0.2.26", cluster_id=1, status="online"))
        session.preload(Node(id="n2", hostname="nvidia", ip="192.0.2.29", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="c1",
                node_id="n1",
                index=0,
                name="Ascend 310P3",
                arch="cann",
                driver_version="25.2.3",
                total_vram_gb=21.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=2,
                uuid="g1",
                node_id="n2",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=3,
                uuid="g2",
                node_id="n2",
                index=1,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
                image_tag="0.27.1",
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["grey_reason"] is None  # nvidia 595.84 满足 >=590
        assert gpus["c1"]["grey_reason"] is None  # 310P3 在 0.27.1 有补位条目 → 可选
        rec = result["data"]["recommended_placement"]
        assert rec["gpu_uuids"], "混合集群应推荐 nvidia 卡"
        assert rec["gpu_uuids"][0] in ("g1", "g2")
        assert rec["arch"] == "nvidia"  # adapter 默认 arch 优先

    async def test_large_model_tp1_hints_raise_tp(self, session):
        """模型单卡放不下（tp=1）→ 推荐为空 + issue 提示提高 tp + deployable=False。

        复刻生产：model 需 31.6GB、2080Ti 单卡可用 ~19.3GB。tp=2 两张卡可承载。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=30.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="g1",
                node_id="n1",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1, max_model_len=8192),
                image_tag="0.27.1",
            ),
            session,
        )
        assert result["data"]["recommended_placement"]["gpu_uuids"] == []
        assert not result["data"]["deployable"]
        assert any("提高 tp" in i for i in result["data"]["issues"])
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["grey_reason"] is None  # 卡可选但 fit=false（单卡装不下）

    async def test_auto_derive_tp_for_large_model(self, session):
        """用户未显式指定 tp + 模型单卡装不下 → 自动推导 tp=2，推荐非空。

        复刻生产：request 只带 max_model_len，tp 走默认——系统按集群 2×2080Ti 推导 tp=2。
        推导后 vram_per_gpu_gb 必须同步按 tp=2 口径（33GB→16.5GB），否则前端按
        topology.tp × vram_per_gpu_gb 对账自相矛盾。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=30.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="g1",
                node_id="n1",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=2,
                uuid="g2",
                node_id="n1",
                index=1,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(max_model_len=8192),
                image_tag="0.27.1",
            ),
            session,
        )
        rec = result["data"]["recommended_placement"]
        assert rec["gpu_uuids"], "应自动推导 tp=2 并推荐两张卡"
        assert set(rec["gpu_uuids"]) == {"g1", "g2"}
        assert result["data"]["recommendation"]["topology"]["tp"] == 2
        assert result["data"]["recommendation"]["vram_per_gpu_gb"] == pytest.approx(16.5, abs=0.1)
        assert result["data"]["deployable"]

    async def test_derive_tp_skipped_for_manual_selection(self, session):
        """手选单张 80GB 大卡（未显式 tp）→ 不按集群小卡组推导，手选合法通过。

        复刻 review 场景：集群含 n1 1×80GB + n2 2×22GB，模型 30GB。用户手选 80GB 卡
        （单卡 72GB 可用，tp=1 足够）；若按全集群推导会得 tp=2（22GB 组 need=2），
        把合法单卡选择误拒成「需 2 张实际 1 张」。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=30.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="big", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(Node(id="n2", hostname="small", ip="10.0.0.2", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="big1",
                node_id="n1",
                index=0,
                name="A100",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=80.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=2,
                uuid="s1",
                node_id="n2",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=3,
                uuid="s2",
                node_id="n2",
                index=1,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(max_model_len=8192),
                image_tag="0.27.1",
                gpu_selector=GpuSelector(gpu_uuids=["big1"]),
            ),
            session,
        )
        assert result["data"]["deployable"]
        assert not any("手动选卡需" in i for i in result["data"]["issues"])
        assert result["data"]["recommendation"]["topology"]["tp"] == 1

    async def test_replicas_exceed_free_cards_reports_shortage(self, session):
        """副本数超出集群在线卡可凑出的份数 → 推荐为空 + 结构性原因 + deployable=False。

        复刻向导 Step1 副本数设 3、集群仅 2 张在线卡的基础预检（不带 engine 的请求
        形态）：此前该形态静默返回空推荐（前端什么都不选也无报错），须给人读原因。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="g1",
                node_id="n1",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=2,
                uuid="g2",
                node_id="n1",
                index=1,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                topology=Topology(tp=1, replicas=3),
            ),
            session,
        )
        data = result["data"]
        rec = data["recommended_placement"]
        assert rec is not None
        assert rec["gpu_uuids"] == []
        assert rec["replica_plans"] == []
        assert not data["deployable"]
        assert any("3 份引擎" in i and "nvidia 2 张" in i for i in data["issues"])

    async def test_infeasible_cluster_reports_real_cause(self, session):
        """集群卡全装不下（tp 提到顶也不够）→ 提示「提高 tp」；凑得出显存但卡数不够 → 提示释放卡。

        两条分流避免误导：60GB 模型 + 2×22GB（need=4 > 2 张，提高 tp 也无用）→ 显存不足分支；
        tp=2 需 2 张卡但只有 1 张空闲（另 1 张被占）→ 卡数不足分支（提高 tp 只会更糟）。
        """
        session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=60.0,
                format="safetensors",
            )
        )
        session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
        session.preload(
            Gpu(
                id=1,
                uuid="g1",
                node_id="n1",
                index=0,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        session.preload(
            Gpu(
                id=2,
                uuid="g2",
                node_id="n1",
                index=1,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(max_model_len=8192),
                image_tag="0.27.1",
            ),
            session,
        )
        # 60GB 模型 + 2×22GB：tp 推导不出可行值（need=4 > 2），默认 tp=1 单卡放不下
        assert not result["data"]["deployable"]
        assert any("提高 tp" in i for i in result["data"]["issues"])


def _seed_mixed_arch(session):
    """混插集群：n1 两张 nvidia(22GB) + n2 一张 Ascend 310P3(21GB)。"""
    session.preload(Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"))
    session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
    session.preload(Node(id="n2", hostname="node2", ip="10.0.0.2", cluster_id=1, status="online"))
    for idx, uuid in enumerate(("g0", "g1")):
        session.preload(
            Gpu(
                id=idx + 1,
                uuid=uuid,
                node_id="n1",
                index=idx,
                name="NVIDIA GeForce RTX 2080 Ti",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=22.0,
                status="online",
                spec={},
            )
        )
    session.preload(
        Gpu(
            id=3,
            uuid="a0",
            node_id="n2",
            index=0,
            name="310P3",
            arch="cann",
            driver_version="25.2.3",
            total_vram_gb=21.02,
            status="online",
            spec={},
        )
    )


class TestInspectMixedArchPlacement:
    """混插集群的自动调度：副本间可异构（不同架构各成一份），按 arch_order 逐份回落。"""

    async def test_replicas_span_nvidia_and_cann(self, session):
        """2×nvidia + 1×Ascend 上 replicas=3 → 落成 2+1，不再报「仅 2 张 nvidia 不足 3」。"""
        _seed_mixed_arch(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                image_tag="0.23.0",
                topology=Topology(tp=1, replicas=3, max_model_len=8192),
            ),
            session,
        )
        data = result["data"]
        assert data["deployable"], data["issues"]
        rec = data["recommended_placement"]
        assert rec["arch_summary"] == {"nvidia": 2, "cann": 1}
        assert [rp["arch"] for rp in rec["replica_plans"]] == ["nvidia", "nvidia", "cann"]
        assert rec["arch"] == "nvidia"  # 部署级回显取首个调度到的卡架构

    async def test_single_arch_still_preferred(self, session):
        """同架构够用时不留混布：2×nvidia 上 replicas=2 全落 nvidia。"""
        _seed_mixed_arch(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                image_tag="0.23.0",
                topology=Topology(tp=1, replicas=2, max_model_len=8192),
            ),
            session,
        )
        data = result["data"]
        assert data["deployable"], data["issues"]
        assert data["recommended_placement"]["arch_summary"] == {"nvidia": 2}

    async def test_structural_shortage_names_arch_and_engines(self, session):
        """架构闸门过了但份数凑不出（在线卡不够）→ 结构性原因，说清需几份、在线各几张。"""
        _seed_mixed_arch(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                image_tag="0.23.0",
                topology=Topology(tp=1, replicas=4, max_model_len=8192),
            ),
            session,
        )
        data = result["data"]
        assert not data["deployable"]
        assert any("4 份引擎" in i and "nvidia 2 张" in i and "cann 1 张" in i for i in data["issues"])

    async def test_capacity_shortage_names_arch(self, session):
        """结构摆得下（3 张在线卡可容 2 份）但空闲卡不足 → 容量口径原因，按目标架构说。"""
        _seed_mixed_arch(session)
        # 两张 nvidia 卡被别的运行中部署占用（声明占用 → 不进推荐池）
        session.preload(
            ModelDeployment(
                deployment_id="dep-busy",
                cluster_id=1,
                name="busy",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                spec={},
            )
        )
        for i, uuid in enumerate(("g0", "g1")):
            session.preload(
                DeploymentGpuAssignment(
                    id=i + 1,
                    deployment_id="dep-busy",
                    gpu_uuid=uuid,
                    allocated_vram_gb=19.8,
                    gpu_memory_utilization=0.9,
                )
            )
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                image_tag="0.23.0",
                topology=Topology(tp=1, replicas=2, max_model_len=8192),
            ),
            session,
        )
        data = result["data"]
        assert not data["deployable"]
        # 只有 Ascend 卡空闲：容量原因按候选架构命名，且不再拿它论证 nvidia 部署「够」
        assert any("replicas=2" in i and "nvidia/cann" in i for i in data["issues"])


def _seed_stopped_residue(session):
    """n1/n2 各一张同型号同容量卡：n1 那张留着「已停止部署」的声明行（残留）。"""
    session.preload(Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors"))
    session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online"))
    session.preload(Node(id="n2", hostname="node2", ip="10.0.0.2", cluster_id=1, status="online"))
    session.preload(
        Gpu(
            id=1,
            uuid="dirty",
            node_id="n1",
            index=0,
            name="NVIDIA GeForce RTX 2080 Ti",
            arch="nvidia",
            driver_version="595.84",
            total_vram_gb=22.0,
            status="online",
            spec={},
        )
    )
    session.preload(
        Gpu(
            id=2,
            uuid="clean",
            node_id="n2",
            index=0,
            name="NVIDIA GeForce RTX 2080 Ti",
            arch="nvidia",
            driver_version="595.84",
            total_vram_gb=22.0,
            status="online",
            spec={},
        )
    )
    session.preload(
        ModelDeployment(
            deployment_id="dep-old",
            cluster_id=1,
            name="old",
            model_id=1,
            engine="vllm",
            status="stopped",
            phase="STOPPING",
            spec={},
        )
    )
    session.preload(
        DeploymentGpuAssignment(
            id=1,
            deployment_id="dep-old",
            gpu_uuid="dirty",
            allocated_vram_gb=19.8,
            gpu_memory_utilization=0.9,
        )
    )


class TestCardStatePreference:
    """卡痕迹偏好：已停止部署的残留卡排最后（软偏好，不拦人）。"""

    async def test_stopped_residue_card_deprioritized(self, session):
        _seed_stopped_residue(session)
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        data = result["data"]
        gpus = {cg["uuid"]: cg for cg in data["cluster_gpus"]}
        # 停止部署的声明不计占用（两张卡都可选），但痕迹如实回显
        assert gpus["dirty"]["occupied_by"] == []
        assert gpus["dirty"]["card_state"] == "residual"
        assert gpus["clean"]["card_state"] is None
        # 推荐调度选没有痕迹的那张
        assert data["recommended_placement"]["gpu_uuids"] == ["clean"]

    async def test_running_occupier_state_is_shared(self, session):
        _seed(session)  # g1 被运行中部署占用
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        gpus = {cg["uuid"]: cg for cg in result["data"]["cluster_gpus"]}
        assert gpus["g1"]["card_state"] == "shared"
        assert gpus["g2"]["card_state"] is None


class TestCardSituationEcho:
    """推荐结构自带「这张卡什么情况」：卡上现有部署（含已停止）、是否本地已有模型。"""

    async def test_recommendation_carries_card_situation(self, session):
        _seed_stopped_residue(session)  # n1=dirty（有已停止部署残留）, n2=clean
        # n2 所在机器已有该模型文件 → 本地性命中
        session.preload(NodeModel(model_id=1, node_id="n2", desired_state="present", status="present"))
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        data = result["data"]
        cards = {c["uuid"]: c for c in data["recommended_placement"]["cards"]}
        assert set(cards) == {"clean"}  # 推荐的就是干净那张
        assert cards["clean"]["model_local"] is True  # 该节点已有模型 → 免一次下发
        # 另一张卡（有残留）的情况在 cluster_gpus 面板里可查，不必进推荐
        gpus = {g["uuid"]: g for g in data["cluster_gpus"]}
        assert gpus["clean"]["model_local"] is True
        assert gpus["dirty"]["model_local"] is False
        assert [o["status"] for o in gpus["dirty"]["occupants"]] == ["stopped"]
        assert gpus["dirty"]["occupants"][0]["active"] is False

    async def test_occupant_marks_same_model(self, session):
        """卡上跑的就是同一个模型 → same_model=True（用户挑卡时最关心的一条）。"""
        _seed(session)  # g1 被 「busy」运行中占用（同 model_id=1）
        result = await inspect_deployment(
            InspectDeploymentRequest(
                model_id=1,
                cluster_id=1,
                engine="vllm",
                topology=Topology(tp=1),
            ),
            session,
        )
        gpus = {g["uuid"]: g for g in result["data"]["cluster_gpus"]}
        occ = gpus["g1"]["occupants"][0]
        assert occ["deployment_name"] == "busy" and occ["active"] is True and occ["same_model"] is True
        assert gpus["g2"]["occupants"] == []
