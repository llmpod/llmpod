"""在线扩缩集成测试（无硬件，FakeSession + stub dispatch）。

覆盖：
- ``preview_scale``：份数/每份卡数/可扩上限/调度预览（只读）；
- ``request_scale``：受理守卫（同份数短路 / SCALING 中拒绝 / 异构 / CPU 部署）+ 意图落库；
- ``drive_scaling``（controller 侧驱动）：按落库意图判方向续跑；
- 扩容：新份四层行 + 卡排重（存量卡不可见）+ 份数回写 + 端点补齐；
- 扩容失败回滚：只删新份三层行 + 释放新卡，存量零感知；
- 缩容 LIFO：摘最新份端点/绑定 → 删三层行 → 份数回写 → 健康灯重算；
- 部署离开 RUNNING（stop/delete）→ 轮询守卫中止扩缩并回滚新份。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest

from _fakes import FakeSession, stub_model_verify
from llmpod.apps.manager.deployment import scale as scale_mod
from llmpod.apps.manager.deployment.constants import (
    DeployStep,
)
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.route_binding import RouteBinding


def _mk_stack(session, *, copies=2, gpu_total=4):
    """running 部署 + copies 份四层行（serving）+ 每份 1 卡 assignment + 端点。"""
    model = session.preload(
        Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=1.0,
            format="safetensors",
            source="local",
            source_url="/data/m",
            source_kind="transformers_folder",
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-sc111",
            cluster_id=1,
            name="sc",
            model_id=1,
            engine="vllm",
            status="running",
            phase="RUNNING",
            health_status="healthy",
            spec={"topology": {"tp": 1, "replicas": copies}, "model_path": "/data/m"},
        )
    )
    for seq in range(copies):
        rep_id, eng_id = f"rep-{seq}", f"eng-{seq}"
        session.preload(
            ModelDeploymentReplica(
                replica_id=rep_id,
                deployment_id=dep.deployment_id,
                seq=seq,
                status="serving",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id=eng_id,
                replica_id=rep_id,
                role="full",
                tp=1,
                pp=1,
                nnodes=1,
                status="ready",
                gpu_arch="nvidia",
            )
        )
        session.preload(
            Instance(
                instance_id=f"i-{seq}",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id=eng_id,
                role="full",
                rank=seq,
                assigned_node="n1",
                status="READY",
                spec={
                    "engine_instance_id": eng_id,
                    "node_rank": 0,
                    "replica_idx": seq,
                    "assigned_node": "n1",
                    "assigned_gpu_uuids": [f"g{seq}"],
                    "assigned_gpus": [seq],
                    "master_addr": "10.0.1.5",
                    "master_port": 29501,
                    "port": 8000 + seq,
                    "model_path": "/data/m",
                    "served_model_name": "m",
                    "gpu_arch": "nvidia",
                    "task": "generate",
                },
            )
        )
        session.preload(
            DeploymentGpuAssignment(
                id=seq + 1,
                deployment_id=dep.deployment_id,
                gpu_uuid=f"g{seq}",
                instance_id=f"i-{seq}",
                gpu_memory_utilization=0.9,
                allocated_vram_gb=9.9,
                resource_type="nvidia",
            )
        )
        session.preload(
            ModelEndpoint(
                id=100 + seq,
                deployment_id=dep.deployment_id,
                model_deployment_replica_id=rep_id,
                engine_type="vllm",
                protocol="openai",
                api_base=f"http://10.0.1.5:{8000 + seq}/v1",
                served_model="m",
                # 端点展示名的 base 是**部署名**（dep.name="sc"），不是模型名（model.name="m"）
                name="sc" if copies == 1 else f"sc·副本{seq + 1}",
                health_status="healthy",
            )
        )
    return dep, model


def _to_cpu_lane(session):
    """把存量分片改造成纯 CPU 部署事实（ram_claim_gb + arch=cpu + 无卡号）。

    扩缩的部署谓词看**实例事实**而非 ``config.allow_cpu``（后者只是「允许落 CPU」）。
    """
    for s in session._store[Instance].values():
        s.spec = {
            **s.spec,
            "gpu_arch": "cpu",
            "ram_claim_gb": 3.0,
            "assigned_gpu_uuids": [],
            "assigned_gpus": [],
        }


@pytest.fixture
def scale_stack(monkeypatch):
    session = FakeSession()
    dep, model = _mk_stack(session, copies=2)
    # staging 秒过：模型已 present（不打真实 placement ensure_present）
    from llmpod.models.node_model import NodeModel

    session.preload(
        NodeModel(
            model_id=1,
            node_id="n1",
            desired_state="present",
            status="present",
            local_dir="/data/m",
            model_path="/data/m",
        )
    )

    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(scale_mod, "get_db_context", _fake_db)
    # staging 门 dispatch 现态重读任务状态（placement 模块）也要走 FakeSession
    from llmpod.apps.manager.deployment import placement as placement_mod

    monkeypatch.setattr(placement_mod, "get_db_context", _fake_db)
    # _handle_scale_failure / 尾段经 reconciler/_provisioning 的真实 get_db_context 兜口
    # 也 stub 掉（FakeSession 全程）；缩容停止复用 StopService，同样要兜
    from llmpod.apps.manager.deployment import reconciler as rec_mod
    from llmpod.apps.manager.deployment import orchestrator as orch_mod
    from llmpod.apps.manager.deployment import stop as stop_mod

    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)
    monkeypatch.setattr(stop_mod, "get_db_context", _fake_db)
    orch_mod._orchestrator = None  # 用新 ledger 单例
    # deployment_projection 的 service 函数经 scale_mod 引用调用，共享同一 session
    # stub 调度相关 service 函数（load_online_gpus / load_node_ips）
    from llmpod.services import deployment_service

    class _G:
        def __init__(self, uuid, node_id, index=0, arch="nvidia", total_vram_gb=11.0, status="online"):
            self.uuid, self.node_id, self.index = uuid, node_id, index
            self.arch, self.total_vram_gb, self.status = arch, total_vram_gb, status
            self.name, self.driver_version = "RTX2080Ti", None

    async def _gpus(db):
        return [_G(f"g{i}", "n1", index=i) for i in range(4)]

    async def _ips(db, node_ids):
        return {"n1": "10.0.1.5"}

    monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
    monkeypatch.setattr(deployment_service, "load_node_ips", _ips)
    # 扩容 staging 放行以复核任务终态为准（node_model.status 只是缓存）→ 预置 SUCCESS 桩任务
    stub_model_verify(monkeypatch, session, 1, ["n1"])
    # 端口分配 stub：递增（签名对齐 allocate_node_port 的 local_taken 形参）
    counter = {"n": 9000}

    async def _alloc(db, node_id, kind, *, local_taken=None):
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(deployment_service, "allocate_node_port", _alloc)
    return session, dep, model


class TestPreviewScale:
    async def test_preview_reports_counts_and_placement(self, scale_stack):
        session, dep, model = scale_stack
        data = await scale_mod.preview_scale(session, dep.deployment_id)
        assert data["current_copies"] == 2
        assert data["per_copy_gpus"] == 1
        assert data["max_feasible_copies"] >= 3  # g2/g3 空闲，至少能再扩一份
        assert data["free_gpus"] == 2
        assert data["suggested_placement"] is not None
        assert set(data["suggested_placement"]["gpu_uuids"]) <= {"g2", "g3"}

    async def test_preview_counts_other_arch_free_cards(self, scale_stack, monkeypatch):
        """异构集群空闲卡口径：任一引擎支持 arch 的空闲卡都算（cann 卡不再被漏计）。

        复刻用户场景：nvidia 卡全占（本部署 2 份），ascend 节点的 1 张 cann 卡空闲——
        预览应报 free_gpus=1、可扩上限 3 份（新份落 cann 卡，镜像按 cann 解析）。
        """
        session, dep, model = scale_stack
        from llmpod.services import deployment_service

        class _G:
            def __init__(self, uuid, node_id, index=0, arch="nvidia", total_vram_gb=11.0, status="online"):
                self.uuid, self.node_id, self.index = uuid, node_id, index
                self.arch, self.total_vram_gb, self.status = arch, total_vram_gb, status
                self.name, self.driver_version = ("Ascend 310P3", "25.2.3") if arch == "cann" else ("RTX2080Ti", None)

        async def _gpus(db):
            # g0/g1 本部署占用；无空闲 nvidia 卡；c1 是唯一空闲卡（cann）
            return [_G("g0", "n1", 0), _G("g1", "n1", 1), _G("c1", "ncann", 0, arch="cann")]

        monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
        data = await scale_mod.preview_scale(session, dep.deployment_id)
        assert data["free_gpus"] == 1
        assert data["max_feasible_copies"] == 3
        assert data["suggested_placement"] is not None
        assert data["suggested_placement"]["gpu_uuids"] == ["c1"]
        assert data["suggested_placement"]["node_ids"] == ["ncann"]

    async def test_preview_excludes_cards_reserved_by_others(self, scale_stack, monkeypatch):
        """别的部署已预留的卡不算空闲/调度——容量口径与 assign 同源，预览不得虚高。"""
        session, dep, model = scale_stack
        from llmpod.apps.manager.deployment.orchestrator import get_orchestrator

        await get_orchestrator().ledger.reserve("other-dep", "g2", 9.9, 11.0)  # g2 被占满
        data = await scale_mod.preview_scale(session, dep.deployment_id)

        assert data["free_gpus"] == 1
        assert data["suggested_placement"] is not None
        assert data["suggested_placement"]["gpu_uuids"] == ["g3"]
        assert data["max_feasible_copies"] == 3

    async def test_preview_rejects_cpu_lane_deployment(self, scale_stack):
        """纯 CPU 部署（实例事实：有内存 claim / arch=cpu）：预览与路由/扩容同口径拒绝，
        不给执行时必被拒的份数。"""
        session, dep, model = scale_stack
        _to_cpu_lane(session)
        with pytest.raises(scale_mod.LLMPodValidationError, match="CPU"):
            await scale_mod.preview_scale(session, dep.deployment_id)

    async def test_preview_allows_gpu_lane_with_allow_cpu_flag(self, scale_stack):
        """allow_cpu=True 但模型装得下卡（部署落显存）→ 扩缩容放行。

        判据是实例事实：拿 config.allow_cpu 当部署判据会让这种部署永远无法调整副本，
        而扩容路径本会放行（两套谓词的自相矛盾）。
        """
        session, dep, model = scale_stack
        dep.spec = {**dep.spec, "allow_cpu": True}
        data = await scale_mod.preview_scale(session, dep.deployment_id)
        assert data["current_copies"] == 2

    async def test_preview_excludes_vram_insufficient_cards(self, scale_stack, monkeypatch):
        """显存不足的卡不得进调度结果/上限（每卡口径 = estimate 值直接比对，不二次除并行度）。"""
        session, dep, model = scale_stack
        from llmpod.services import deployment_service

        class _G:
            def __init__(self, uuid, node_id, index=0, arch="nvidia", total_vram_gb=11.0, status="online"):
                self.uuid, self.node_id, self.index = uuid, node_id, index
                self.arch, self.total_vram_gb, self.status = arch, total_vram_gb, status
                self.name, self.driver_version = "RTX2080Ti", None

        async def _gpus(db):
            # g2 足量、g3 只有 2GB（装不下 1GB 模型的 KV 预留口径）
            return [_G("g2", "n1", index=2), _G("g3", "n1", index=3, total_vram_gb=2.0)]

        monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
        data = await scale_mod.preview_scale(session, dep.deployment_id)
        assert data["suggested_placement"] is not None
        assert data["suggested_placement"]["gpu_uuids"] == ["g2"]  # 小卡被显存过滤掉


class TestRequestScale:
    """受理侧（路由调用）：只写 desired state，不做任何推进。"""

    async def test_writes_desired_state(self, scale_stack):
        """受理写意图 + phase=SCALING（驱动交给 controller，故本函数不起协程）。"""
        session, dep, model = scale_stack
        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target == 3
        assert dep2.phase == "SCALING"

    async def test_same_copies_is_noop(self, scale_stack):
        """目标份数 == 实际行数：短路返回——不写意图、不闪 SCALING、不记日志。"""
        session, dep, model = scale_stack
        from llmpod.models.deploy_log import DeployLog

        await scale_mod.request_scale(session, dep.deployment_id, 2, trigger="test")
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.phase == "RUNNING"
        assert dep2.scale_target is None
        assert not list(session._store.get(DeployLog, {}).values())

    async def test_rejected_while_scaling(self, scale_stack):
        """SCALING 中的重复提交被拒（409），且在途意图不被覆盖。"""
        session, dep, model = scale_stack
        dep.phase = "SCALING"
        dep.scale_target = 4
        with pytest.raises(scale_mod.Conflict, match="调整进行中"):
            await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target == 4

    async def test_rejects_cpu_lane(self, scale_stack):
        """纯 CPU 部署（实例事实）拒绝在线扩缩：扩缩走 GPU 调度口径，且不落意图。"""
        session, dep, model = scale_stack
        _to_cpu_lane(session)
        with pytest.raises(scale_mod.LLMPodValidationError, match="CPU"):
            await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target is None and dep2.phase == "RUNNING"

    async def test_rejects_heterogeneous(self, scale_stack):
        """异构副本（各份形状不同）无「单一形状」可复制 → 拒绝在线伸缩。"""
        session, dep, model = scale_stack
        from llmpod.apps.manager.deployment.schemas import Topology

        dep.spec = {**dep.spec, "topology": Topology(copies=[{"tp": 1}, {"tp": 2}]).model_dump()}
        with pytest.raises(scale_mod.Conflict, match="异构副本"):
            await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")

    async def test_rejected_when_not_running(self, scale_stack):
        """非 running（如 stopped）不受理。"""
        session, dep, model = scale_stack
        dep.status = "stopped"
        with pytest.raises(scale_mod.Conflict, match="仅 running"):
            await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")


class TestDriveScaling:
    async def test_resume_provisioning_residue_continues_scale_up(self, scale_stack, monkeypatch):
        """扩容中断（provisioning 残留 + spec 未回写）→ 按落库意图续跑到目标份数。"""
        session, dep, model = scale_stack
        calls: list[tuple] = []

        async def _fake_up(deployment_id, target, trigger):
            calls.append(("up", target))

        async def _fake_down(deployment_id, current, target, trigger):
            calls.append(("down", current, target))

        monkeypatch.setattr(scale_mod, "_scale_up", _fake_up)
        monkeypatch.setattr(scale_mod, "_scale_down", _fake_down)
        # 意图先落库（scale_target=3）+ 一份 provisioning 残留（seq=2，spec.replicas 仍 2）
        dep.scale_target = 3
        session.preload(
            ModelDeploymentReplica(
                replica_id="rep-2",
                deployment_id=dep.deployment_id,
                seq=2,
                status="provisioning",
            )
        )

        await scale_mod.drive_scaling(dep.deployment_id)

        assert calls == [("up", 3)]  # 扩容续跑：按落库意图扩到 3 份
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.phase == "RUNNING"  # 完成复位

    async def test_left_running_clears_intent_and_phase_residue(self, scale_stack):
        """SCALING 期部署离开 RUNNING（stop/fail 未覆盖到的窗口）：清意图 + phase 残影。

        残留 SCALING 会让 controller 每轮按 phase 捞起空转协程、UI 恒显扩缩中——
        意图作废、phase 归位终态部署唯一允许的 STOPPING。
        """
        session, dep, model = scale_stack
        dep.status = "STOPPED"
        dep.scale_target = 3
        dep.phase = "SCALING"
        session.preload(
            ModelDeploymentReplica(
                replica_id="rep-2",
                deployment_id=dep.deployment_id,
                seq=2,
                status="serving",
            )
        )

        await scale_mod.drive_scaling(dep.deployment_id)

        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target is None
        assert dep2.phase == "STOPPING"

    async def test_resume_finishing_window_still_scale_up(self, scale_stack, monkeypatch):
        """新份已全部 serving、spec 未回写的收尾窗口 → 仍是扩容（不得误判缩容摘份）。

        行状态上与「缩容删了一半」无法区分，方向只能由落库意图给出——这是 resume 的
        权威判据（旧口径按 provisioning 有无分流，会把这个窗口当缩容把刚扩出来的份删掉）。
        """
        session, dep, model = scale_stack
        calls: list[tuple] = []
        dep.scale_target = 3  # 用户要 3 份
        # 新份已 serving（finalize 之前的收尾窗口），spec 仍 2
        session.preload(
            ModelDeploymentReplica(
                replica_id="rep-2",
                deployment_id=dep.deployment_id,
                seq=2,
                status="serving",
            )
        )

        async def _fake_up(deployment_id, target, trigger):
            calls.append(("up", target))

        async def _fake_down(deployment_id, current, target, trigger):
            calls.append(("down", current, target))

        monkeypatch.setattr(scale_mod, "_scale_up", _fake_up)
        monkeypatch.setattr(scale_mod, "_scale_down", _fake_down)

        await scale_mod.drive_scaling(dep.deployment_id)

        assert calls == [("up", 3)]  # 继续扩容收尾，而不是缩容

    async def test_resume_serving_residue_continues_scale_down(self, scale_stack, monkeypatch):
        """缩容中断（serving 残留 + spec 已回写小值）→ 以落库意图为目标继续摘除。"""
        session, dep, model = scale_stack
        calls: list[tuple] = []
        dep.spec = {**dep.spec, "topology": {**dep.spec["topology"], "replicas": 1}}
        dep.scale_target = 1

        async def _fake_up(deployment_id, target, trigger):
            calls.append(("up", target))

        async def _fake_down(deployment_id, current, target, trigger):
            calls.append(("down", current, target))

        monkeypatch.setattr(scale_mod, "_scale_up", _fake_up)
        monkeypatch.setattr(scale_mod, "_scale_down", _fake_down)

        await scale_mod.drive_scaling(dep.deployment_id)

        assert calls == [("down", 2, 1)]  # 行数 2 → 意图 1

    async def test_resume_without_intent_converges_to_running(self, scale_stack, monkeypatch):
        """只有 phase 残影（无在途意图）→ 收敛回 RUNNING，不驱动任何扩缩。"""
        session, dep, model = scale_stack
        dep.phase = "SCALING"
        calls = {"n": 0}

        async def _boom(deployment_id, current, target, trigger):
            calls["n"] += 1

        monkeypatch.setattr(scale_mod, "_scale_up", _boom)
        monkeypatch.setattr(scale_mod, "_scale_down", _boom)

        await scale_mod.drive_scaling(dep.deployment_id)

        assert calls["n"] == 0
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.phase == "RUNNING"

    async def test_repeat_call_without_intent_is_noop(self, scale_stack, monkeypatch):
        """无意图的重复驱动（每轮扫描都会调）只复位相位，不重复推进。"""
        session, dep, model = scale_stack
        calls = {"n": 0}

        async def _boom(*_a, **_k):
            calls["n"] += 1

        monkeypatch.setattr(scale_mod, "_scale_up", _boom)
        monkeypatch.setattr(scale_mod, "_scale_down", _boom)
        await scale_mod.drive_scaling(dep.deployment_id)
        await scale_mod.drive_scaling(dep.deployment_id)  # 重入幂等
        assert calls["n"] == 0
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.phase == "RUNNING"


class TestScaleUpRollbackOnFailure:
    async def test_failed_scale_up_rolls_back_new_replicas_only(self, scale_stack, monkeypatch):
        """扩容驱动失败 → 只删新份三层行 + 释放新卡；存量份/端点/assignment 零感知。"""
        session, dep, model = scale_stack

        # 让容器阶段立即失败（staging 通过——模型已 present 缺省 False 时会等待，
        # 直接令 _drive_new_replicas 抛错最省）
        async def _drive_boom(deployment_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(scale_mod, "_drive_new_replicas", _drive_boom)

        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        with pytest.raises(RuntimeError):
            await scale_mod.drive_scaling(dep.deployment_id)

        # 新份三层行已回滚
        replicas = list(session._store[ModelDeploymentReplica].values())
        assert {r.seq for r in replicas} == {0, 1}  # 只剩存量两份
        engines = list(session._store[ModelDeploymentInstance].values())
        assert len(engines) == 2
        shards = list(session._store[Instance].values())
        assert len(shards) == 2
        # 存量端点不动；assignment 表删的是「新份卡」——存量 g0/g1 保留
        assert len(list(session._store[ModelEndpoint].values())) == 2
        remaining_uuids = {a.gpu_uuid for a in session._store.get(DeploymentGpuAssignment, {}).values()}
        assert remaining_uuids <= {"g0", "g1"}
        # 份数回原值（spec 未回写）
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.spec["topology"]["replicas"] == 2
        # 失败日志落了一条 scale 步骤
        from llmpod.models.deploy_log import DeployLog

        logs = list(session._store.get(DeployLog, {}).values())
        assert any(row.step == DeployStep.SCALE and row.status == "failed" for row in logs)


class TestScaleUpIdempotency:
    async def test_resume_does_not_duplicate_already_expanded_copies(self, scale_stack, monkeypatch):
        """崩溃重入：上一轮已落库的新份整份跳过（不重复展开/调度/占卡）。"""
        session, dep, model = scale_stack
        dep.scale_target = 3
        # 上一轮已展开的第 3 份（seq=2）：副本/引擎/容器/assignment 行齐备、已 serving
        session.preload(
            ModelDeploymentReplica(replica_id="rep-2", deployment_id=dep.deployment_id, seq=2, status="serving")
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-2",
                replica_id="rep-2",
                role="full",
                tp=1,
                pp=1,
                nnodes=1,
                status="ready",
                gpu_arch="nvidia",
            )
        )
        session.preload(
            Instance(
                instance_id="i-2",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id="eng-2",
                role="full",
                rank=2,
                assigned_node="n1",
                status="READY",
                spec={
                    "engine_instance_id": "eng-2",
                    "node_rank": 0,
                    "replica_idx": 2,
                    "assigned_node": "n1",
                    "assigned_gpu_uuids": ["g2"],
                    "assigned_gpus": [2],
                    "master_addr": "10.0.1.5",
                    "master_port": 29501,
                    "port": 8002,
                    "model_path": "/data/m",
                    "served_model_name": "m",
                    "gpu_arch": "nvidia",
                    "task": "generate",
                },
            )
        )
        session.preload(
            DeploymentGpuAssignment(
                id=9,
                deployment_id=dep.deployment_id,
                gpu_uuid="g2",
                instance_id="i-2",
                gpu_memory_utilization=0.9,
                allocated_vram_gb=9.9,
                resource_type="nvidia",
            )
        )
        drive_calls = {"n": 0}

        async def _drive(deployment_id):
            drive_calls["n"] += 1  # 只收尾，不再展开

        monkeypatch.setattr(scale_mod, "_drive_new_replicas", _drive)

        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        await scale_mod.drive_scaling(dep.deployment_id)

        replicas = list(session._store[ModelDeploymentReplica].values())
        assert sorted(r.seq for r in replicas) == [0, 1, 2]  # 不重复展开
        assert len(list(session._store[Instance].values())) == 3
        assert len(list(session._store[DeploymentGpuAssignment].values())) == 3  # 卡不双占
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.spec["topology"]["replicas"] == 3  # 收尾回写份数
        assert drive_calls["n"] == 1


class TestScaleUpStagingWriteback:
    """扩容 staging 的路径写回与初建同口径（节点回报目录权威）。"""

    @staticmethod
    def _drive_ok_factory(session):
        """容器阶段直接判新份 serving（本类只验 staging 的写回，不跑容器阶段）。"""

        async def _drive_ok(deployment_id):
            for r in session._store[ModelDeploymentReplica].values():
                if r.seq >= 2:
                    r.status = "serving"

        return _drive_ok

    async def test_writes_back_node_reported_paths(self, scale_stack, monkeypatch):
        """model_path 与 mmproj_path 都按节点回报的 local_dir 重算。

        `_stamp_assigned_instances` 写的是预估值（config.model_path 目录）；权威值在节点
        回报里——不回写 mmproj 会让新份的 ``--mmproj`` 指向容器里打不开的路径。
        """
        session, dep, model = scale_stack
        from llmpod.models.node_model import NodeModel

        model.mmproj_file = "mmproj-F16.gguf"
        nm = session._store[NodeModel][(1, "n1")]
        nm.local_dir = "/data/other"
        nm.model_path = "/data/other/tiny.gguf"
        monkeypatch.setattr(scale_mod, "_drive_new_replicas", self._drive_ok_factory(session))

        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        await scale_mod.drive_scaling(dep.deployment_id)

        new = [s for s in session._store[Instance].values() if s.spec.get("replica_idx") == 2]
        assert len(new) == 1
        assert new[0].spec["model_path"] == "/data/other/tiny.gguf"
        assert new[0].spec["mmproj_path"] == "/data/other/mmproj-F16.gguf"

    async def test_none_model_path_does_not_clobber(self, scale_stack, monkeypatch):
        """节点回报 model_path 为空 → 保留预估路径（不动 spec），不写出 ``--model None``。"""
        session, dep, model = scale_stack
        from llmpod.models.node_model import NodeModel

        model.mmproj_file = "mmproj-F16.gguf"
        nm = session._store[NodeModel][(1, "n1")]
        nm.model_path = None
        monkeypatch.setattr(scale_mod, "_drive_new_replicas", self._drive_ok_factory(session))

        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        await scale_mod.drive_scaling(dep.deployment_id)

        new = [s for s in session._store[Instance].values() if s.spec.get("replica_idx") == 2]
        assert new[0].spec["model_path"] == "/data/m"  # 预估路径未被 None 覆盖


class TestScaleFailureReleasesReservation:
    async def test_ghost_reservation_released_when_no_rows_persisted(self, scale_stack, monkeypatch):
        """assign 成功但落行前失败（库里无行）→ 账本预留仍须释放，不留幽灵占卡。"""
        session, dep, model = scale_stack
        from llmpod.apps.manager.deployment.orchestrator import get_orchestrator

        orch = get_orchestrator()
        # 模拟 assign 已记账（新份卡 g2）、但落行前抛错（库里没有新份行）
        await orch.ledger.reserve(dep.deployment_id, "g2", 9.9, 11.0)

        await scale_mod._handle_scale_failure(dep.deployment_id, 2, 3, RuntimeError("boom"), rollback_new=True)

        assert "g2" not in orch.ledger.get_reserved_for_deployment(dep.deployment_id)


class TestScaleDown:
    async def test_scale_down_removes_newest_replica_lifo(self, scale_stack, monkeypatch):
        """缩容 2→1：摘 r2 端点+绑定、删三层行、份数回写、健康灯重算；r1 不动。"""
        session, dep, model = scale_stack
        session.preload(RouteBinding(route_id=1, endpoint_id=101, weight=1, priority=0, enabled=True))
        # 停容器 stub：直接置 STOPPED（模拟 node 上报）

        async def _dispatch(task):
            if task.type == "STOP_INSTANCE":
                inst = (task.payload or {}).get("instance_id")
                row = session._store.get(Instance, {}).get(inst)
                if row is not None:
                    row.status = "STOPPED"

        from llmpod.apps.manager.deployment import task_ops

        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)

        await scale_mod.request_scale(session, dep.deployment_id, 1, trigger="test")
        await scale_mod.drive_scaling(dep.deployment_id)

        replicas = list(session._store[ModelDeploymentReplica].values())
        assert {r.seq for r in replicas} == {0}  # LIFO：r2（seq=1）被移除
        endpoints = list(session._store[ModelEndpoint].values())
        assert len(endpoints) == 1 and endpoints[0].model_deployment_replica_id == "rep-0"
        assert endpoints[0].name == "sc"  # 2→1 份：幸存端点退回裸名（base = 部署名）
        assert session._store.get(RouteBinding, {}) == {}  # r2 绑定随端点删
        assert len(list(session._store[Instance].values())) == 1
        assert len(list(session._store[DeploymentGpuAssignment].values())) == 1
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.spec["topology"]["replicas"] == 1
        assert dep2.health_status == "healthy"  # 剩余份 serving

    async def test_scale_down_cancels_inflight_start(self, scale_stack, monkeypatch):
        """被摘份有在途 START（自愈重启中）→ 先取消再停，防 STOP 后 START 把容器重新拉起。"""
        session, dep, model = scale_stack
        from llmpod.apps.manager.deployment import task_ops
        from llmpod.models.task import Task

        # r2（seq=1）分片的 START 在途（PENDING）
        shard = session._store[Instance]["i-1"]
        rk = task_ops.instance_start_key(shard)
        session.preload(
            Task(
                id="t-start",
                resource_type="INSTANCE",
                resource_key=rk,
                node_id="n1",
                type="START_INSTANCE",
                status="PENDING",
                attempt=1,
            )
        )
        cancelled: list[str] = []

        async def _cancel(node_id, task_id):
            cancelled.append(task_id)

        async def _dispatch(task):
            if task.type == "STOP_INSTANCE":
                inst = (task.payload or {}).get("instance_id")
                row = session._store.get(Instance, {}).get(inst)
                if row is not None:
                    row.status = "STOPPED"

        monkeypatch.setattr(task_ops, "dispatch_cancel_task", _cancel)
        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)

        await scale_mod.request_scale(session, dep.deployment_id, 1, trigger="test")
        await scale_mod.drive_scaling(dep.deployment_id)

        assert cancelled == ["t-start"]  # 在途 START 被取消
        assert session._store[Task]["t-start"].status == "CANCELLED"

    async def test_scale_down_aborts_when_stop_unconfirmed(self, scale_stack, monkeypatch):
        """容器未在预算内确认停止 → 中止缩容：三层行/assignment/份数全保留（防同卡双引擎）。"""
        session, dep, model = scale_stack

        async def _never_stopped(deployment_id, shards):
            return False  # 停止预算耗尽仍非 STOPPED

        monkeypatch.setattr(scale_mod, "_stop_replica_shards", _never_stopped)

        await scale_mod.request_scale(session, dep.deployment_id, 1, trigger="test")
        with pytest.raises(scale_mod.LLMPodError, match="确认停止"):
            await scale_mod.drive_scaling(dep.deployment_id)

        # 行全保留（端点摘了——流量已出池，但资源/行不动，重试续跑）
        replicas = list(session._store[ModelDeploymentReplica].values())
        assert {r.seq for r in replicas} == {0, 1}
        assert len(list(session._store[Instance].values())) == 2
        assert len(list(session._store[DeploymentGpuAssignment].values())) == 2
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.spec["topology"]["replicas"] == 2  # 份数未回写
        # 意图 + SCALING 保留：行数仍 > 意图，下一轮 drive_scaling 续摘剩余份——
        # 清了就静默卡在中间份数（controller 不再捞起，deploy_log 条目永不收口）
        assert dep2.scale_target == 1
        assert dep2.phase == "SCALING"
        # 失败簿记落 1（退避/放弃门的输入）
        assert scale_mod._scale_fail_streak[dep.deployment_id][0] == 1

    async def test_scale_down_gives_up_after_max_fails(self, scale_stack, monkeypatch):
        """缩容续摘连续失败超上限 → 放弃意图：清 scale_target + 相位回 RUNNING + ERROR
        日志——不再无限 5s 重试，用户可重新发起副本调整（SCALING 不再锁门）。"""
        session, dep, model = scale_stack

        async def _never_stopped(deployment_id, shards):
            return False

        monkeypatch.setattr(scale_mod, "_stop_replica_shards", _never_stopped)

        await scale_mod.request_scale(session, dep.deployment_id, 1, trigger="test")
        # 预置到上限边缘（退避已过）：本轮再失败一次即记满上限
        scale_mod._scale_fail_streak[dep.deployment_id] = (scale_mod.SCALE_DOWN_MAX_FAILS - 1, 0.0)
        with pytest.raises(scale_mod.LLMPodError, match="确认停止"):
            await scale_mod.drive_scaling(dep.deployment_id)
        assert scale_mod._scale_fail_streak[dep.deployment_id][0] == scale_mod.SCALE_DOWN_MAX_FAILS

        # 再驱动一轮（簿记超限）→ 放弃：意图清、相位回 RUNNING、不再抛错
        await scale_mod.drive_scaling(dep.deployment_id)

        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target is None  # 意图放弃
        assert dep2.phase == "RUNNING"  # 相位解锁（用户可重试）
        assert dep.deployment_id not in scale_mod._scale_fail_streak  # 簿记清

    async def test_scale_down_backoff_skips_drive(self, scale_stack, monkeypatch):
        """退避窗内本轮跳过驱动（不重试 stop/rm、不刷日志），出窗后继续。"""
        session, dep, model = scale_stack
        calls = []

        async def _never_stopped(deployment_id, shards):
            calls.append(1)
            return False

        monkeypatch.setattr(scale_mod, "_stop_replica_shards", _never_stopped)

        await scale_mod.request_scale(session, dep.deployment_id, 1, trigger="test")
        # 簿记：1 次失败 + 退避到 30s 后 → 本轮 drive 跳过
        scale_mod._scale_fail_streak[dep.deployment_id] = (1, time.monotonic() + 30.0)
        await scale_mod.drive_scaling(dep.deployment_id)
        assert calls == []  # 退避窗内未触达 stop


class TestScaleAbortsWhenDeploymentLeavesRunning:
    async def test_stop_mid_scale_up_aborts_and_rolls_back(self, scale_stack, monkeypatch):
        """扩容途中部署被 stop → 轮询守卫中止，新份回滚，不再往已停部署拉容器。"""
        session, dep, model = scale_stack

        async def _stop_after_staging(deployment_id):
            dep_row = await session.get(ModelDeployment, deployment_id)
            dep_row.status = "stopped"

        monkeypatch.setattr(scale_mod, "_stage_new_replicas", _stop_after_staging)

        await scale_mod.request_scale(session, dep.deployment_id, 3, trigger="test")
        with pytest.raises(scale_mod.LLMPodError, match="离开运行态"):
            await scale_mod.drive_scaling(dep.deployment_id)

        replicas = list(session._store[ModelDeploymentReplica].values())
        assert {r.seq for r in replicas} == {0, 1}  # 新份三层行已回滚
        assert len(list(session._store[Instance].values())) == 2
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target is None  # 意图恒清
        assert dep2.phase == "SCALING"  # 非 RUNNING 不复位相位（停止路径自己写）


class TestScaleRoute:
    async def test_route_writes_desired_state_then_wakes(self, scale_stack, monkeypatch):
        """路由三件事：受理（写 desired state）→ commit → 唤醒控制循环；不起协程。"""
        session, dep, model = scale_stack
        from llmpod.apps.manager.deployment import controller as ctrl_mod
        from llmpod.apps.webserver.auth.session import SessionData
        from llmpod.apps.webserver.routes.admin import deployment as route_mod

        async def _no_audit(*_a, **_k):
            return None

        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        woken = {"n": 0}

        class _Ctl:
            def wake(self):
                woken["n"] += 1

        monkeypatch.setattr(ctrl_mod, "get_controller", lambda: _Ctl())
        user = SessionData(
            user_id=1,
            username="tester",
            tenant_id=0,
            access_domain="platform",
            role_codes=["super_admin"],
            role_ids=[1],
            is_superuser=True,
        )

        payload = await route_mod.scale_deployment_route(
            dep.deployment_id, route_mod.ScaleRequest(copies=3), user=user, db=session
        )

        assert payload["data"]["copies"] == 3
        dep2 = await session.get(ModelDeployment, dep.deployment_id)
        assert dep2.scale_target == 3 and dep2.phase == "SCALING"
        assert woken["n"] == 1  # 落库后才唤醒
