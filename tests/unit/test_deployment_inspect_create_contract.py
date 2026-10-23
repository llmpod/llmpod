"""inspect 预检判定与 create 侧调度（GpuScheduler.assign）的跨路径契约测试。

锁一条不变量：同一集群状态下，inspect 说的（deployable / 推荐调度 / 手选校验文案与
出路）与 create 侧 ``_plan → assign`` 会做的（选卡 / 拒绝 / 逃生门可用）必须一致。
「与 create 同口径」的注释散见 deployment_inspect 两文件十来处，此前没有测试同时跑
两侧——口径漂移时两边测试各自全绿。本文件用同一套种子数据分别驱动两侧：

- inspect 侧：FakeSession 种 Node / Gpu /（占用场景）ModelDeployment + assignment 行，
  直调 ``run_inspect``；
- create 侧：同一批卡转 GpuCandidate 池 + 真 ResourceLedger（已存在的部署按 orchestrator
  同式先 assign 落账），直调 ``GpuScheduler.assign``（_plan 的有状态提交段，跳过任务编排）。

数字口径（48GB 单卡 Demo）：权重 14.5GB + 8K 上下文 KV 1.45GB → 每卡需求 15.95GB；
平台信封 = 0.9×48 = 43.2GB（全卡共享）。两份部署共址的可行窗口 = 各自份额装得下
每卡需求，且份额之和 ≤ 信封：第一份占比 0.5（占 24GB）时，第二份占比 ∈ [33.5%, 40%]。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import GpuCandidate, GpuSelector, InstanceSpec, Topology
from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler
from llmpod.apps.manager.deployment.resource_ledger import ResourceLedger
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.services.deployment_inspect import run_inspect
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node import Node
from tests.unit._fakes import FakeSession

# 每卡需求 = 权重 14.5 + KV 1.45（8K 上下文启发式）；生产里两侧行都出自 adapter.estimate_vram_gb
NEED = 15.95
# 平台信封（0.9×48）与第一份部署的份额（0.5×48），共址算术用
ENVELOPE = 43.2
FIRST_SLICE = 24.0

_MODEL = Model(id=1, name="demo", model_type="llm", size_gb=14.5, format="safetensors")


def _seed_cluster(session, uuids=("g1",)):
    """n1 上的 48GB 在线 nvidia 卡（每卡可用 = 48，心跳未上报）。"""
    session.preload(_MODEL)
    session.preload(Node(id="n1", hostname="node1", ip="10.0.0.1", cluster_id=1, status="online", memory_gb=64.0))
    for i, uuid in enumerate(uuids):
        session.preload(
            Gpu(
                id=i + 1,
                uuid=uuid,
                node_id="n1",
                index=i,
                name="A100",
                arch="nvidia",
                driver_version="595.84",
                total_vram_gb=48.0,
                status="online",
                spec={},
            )
        )


def _seed_occupier(session, gpu_uuid="g1", declared=FIRST_SLICE, util=0.5):
    """已存在的一个活跃部署（inspect 的占用视图读 assignment 行）。"""
    session.preload(
        ModelDeployment(
            deployment_id="dep-1",
            cluster_id=1,
            name="busy",
            model_id=1,
            engine="vllm",
            status="running",
            phase="RUNNING",
            spec={},
        )
    )
    session.preload(
        DeploymentGpuAssignment(
            id=1,
            deployment_id="dep-1",
            gpu_uuid=gpu_uuid,
            allocated_vram_gb=declared,
            gpu_memory_utilization=util,
        )
    )


async def _inspect(session, **kw):
    base = dict(model_id=1, cluster_id=1, engine="vllm", topology=Topology(tp=1, max_model_len=8192))
    base.update(kw)
    return await run_inspect(session, InspectDeploymentRequest(**base))


def _pool(uuids=("g1",), used=None):
    """同一批卡转调度候选池（心跳已用可选，物理闸用）。"""
    return [
        GpuCandidate(
            uuid=u, node_id="n1", index=i, arch="nvidia", total_vram_gb=48.0, status="online", used_vram_gb=used
        )
        for i, u in enumerate(uuids)
    ]


def _scheduler():
    return GpuScheduler(ResourceLedger())


class TestGreenPathAgreement:
    """空卡绿灯：inspect 推荐的卡就是 create 会落的卡（默认占比与用户占比都成立）。"""

    async def test_default_util_same_card(self):
        s = FakeSession()
        _seed_cluster(s)
        resp = await _inspect(s)
        assert resp.deployable is True
        assert resp.recommended_placement.gpu_uuids == ["g1"]
        assert resp.cluster_gpus[0].required_vram_gb == pytest.approx(NEED)
        assignments = await _scheduler().assign(
            "dep-x", [InstanceSpec(instance_id="i1", tp=1)], _pool(), NEED, arch="nvidia"
        )
        assert sorted(a.gpu_uuids[0] for a in assignments) == sorted(resp.recommended_placement.gpu_uuids)

    async def test_user_util_same_card(self):
        """第一份带用户占比 0.5：inspect 判定不跟随（平台默认口径），create 落账跟随。"""
        s = FakeSession()
        _seed_cluster(s)
        resp = await _inspect(s, engine_params={"extra_args": ["--gpu-memory-utilization", "0.5"]})
        assert resp.deployable is True
        assignments = await _scheduler().assign(
            "dep-x",
            [InstanceSpec(instance_id="i1", tp=1)],
            _pool(),
            NEED,
            arch="nvidia",
            vram_percent=0.5,
        )
        assert assignments[0].gpu_uuids == ["g1"]
        assert assignments[0].allocated == {"g1": pytest.approx(FIRST_SLICE)}

    async def test_two_free_cards_replicas_agree(self):
        """replicas=2 双卡：inspect 逐份推荐与 create 逐实例调度同集。"""
        s = FakeSession()
        _seed_cluster(s, uuids=("g1", "g2"))
        resp = await _inspect(s, topology=Topology(tp=1, replicas=2, max_model_len=8192))
        assert sorted(resp.recommended_placement.gpu_uuids) == ["g1", "g2"]
        insts = [
            InstanceSpec(instance_id="i1", tp=1, replica_idx=0),
            InstanceSpec(instance_id="i2", tp=1, replica_idx=1),
        ]
        assignments = await _scheduler().assign("dep-x", insts, _pool(("g1", "g2")), NEED, arch="nvidia")
        assert sorted(u for a in assignments for u in a.gpu_uuids) == ["g1", "g2"]


class TestOccupiedCardAgreement:
    """被占卡共址：inspect 红灯 + 出路文案 ⇔ create 默认拒 / 降占比成。"""

    async def test_both_refuse_under_default_policy(self):
        s = FakeSession()
        _seed_cluster(s)
        _seed_occupier(s)
        resp = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        assert resp.deployable is False
        # 文案给出路：调低显存占比可共址
        assert any("busy" in i and "调低显存占比" in i for i in resp.issues)
        # create 侧：第一份占 0.5×48=24 落账后，第二份默认占比（0.9×48=43.2）超余量 → 拒
        sched = _scheduler()
        await sched.assign(
            "dep-1",
            [InstanceSpec(instance_id="i1", tp=1)],
            _pool(),
            NEED,
            arch="nvidia",
            gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            vram_percent=0.5,
        )
        with pytest.raises(LLMPodValidationError):
            await sched.assign(
                "dep-2",
                [InstanceSpec(instance_id="i2", tp=1)],
                _pool(),
                NEED,
                arch="nvidia",
                gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            )

    async def test_escape_hatch_lowered_util_succeeds(self):
        """inspect 文案承诺的逃生门真实可用：第二份降到 0.35（16.8GB）落在同一张卡。"""
        sched = _scheduler()
        await sched.assign(
            "dep-1",
            [InstanceSpec(instance_id="i1", tp=1)],
            _pool(),
            NEED,
            arch="nvidia",
            gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            vram_percent=0.5,
        )
        assignments = await sched.assign(
            "dep-2",
            [InstanceSpec(instance_id="i2", tp=1)],
            _pool(),
            NEED,
            arch="nvidia",
            gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            vram_percent=0.35,
        )
        assert assignments[0].gpu_uuids == ["g1"]
        assert assignments[0].allocated == {"g1": pytest.approx(16.8)}
        # 份额之和 24 + 16.8 = 40.8 ≤ 信封 43.2

    async def test_util_sum_over_envelope_refused(self):
        """两份各 0.5：份额和 48 > 信封 43.2 → 第二份拒绝（想 50%+50% 不行）。"""
        sched = _scheduler()
        await sched.assign(
            "dep-1",
            [InstanceSpec(instance_id="i1", tp=1)],
            _pool(),
            NEED,
            arch="nvidia",
            gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            vram_percent=0.5,
        )
        with pytest.raises(LLMPodValidationError):
            await sched.assign(
                "dep-2",
                [InstanceSpec(instance_id="i2", tp=1)],
                _pool(),
                NEED,
                arch="nvidia",
                gpu_selector=GpuSelector(gpu_uuids=["g1"]),
                vram_percent=0.5,
            )

    async def test_util_below_model_need_refused(self):
        """份额装不下需求（0.3×48=14.4 < 15.95）→ 拒绝（可行窗口下界）。"""
        with pytest.raises(LLMPodValidationError):
            await _scheduler().assign(
                "dep-x",
                [InstanceSpec(instance_id="i1", tp=1)],
                _pool(),
                NEED,
                arch="nvidia",
                gpu_selector=GpuSelector(gpu_uuids=["g1"]),
                vram_percent=0.3,
            )

    async def test_external_process_heartbeat_both_refuse(self, monkeypatch):
        """平台外进程占卡（心跳已用 30）：inspect 手选红「平台外进程」⇔ create 物理闸拒。"""
        s = FakeSession()
        _seed_cluster(s)
        monkeypatch.setattr(inspect_svc, "load_used_vram_by_uuid", lambda: {"g1": 30.0})
        resp = await _inspect(s, gpu_selector=GpuSelector(gpu_uuids=["g1"]))
        assert resp.deployable is False
        assert any("平台外进程" in i for i in resp.issues)
        with pytest.raises(LLMPodValidationError):
            await _scheduler().assign(
                "dep-x",
                [InstanceSpec(instance_id="i1", tp=1)],
                _pool(used=30.0),
                NEED,
                arch="nvidia",
                gpu_selector=GpuSelector(gpu_uuids=["g1"]),
            )

    async def test_single_card_fully_occupied_auto_recommend_empty_silent(self):
        """锁定现状：独卡被占 + 自动调度 → 推荐为空且 issues 无归因文案。

        已知解释性缺口（归因分支要求「存在空闲卡」才说话，全占卡池被短路）；占用事实
        仍在卡片视图（occupied_by）里可解释。修复归因时反转本用例的断言。
        """
        s = FakeSession()
        _seed_cluster(s)
        _seed_occupier(s)
        resp = await _inspect(s)
        assert resp.deployable is True
        assert resp.recommended_placement.gpu_uuids == []
        assert not any("占用" in i or "释放" in i for i in resp.issues)
        assert resp.cluster_gpus[0].occupied_by  # 事实在卡片视图
