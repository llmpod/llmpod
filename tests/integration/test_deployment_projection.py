"""四层状态投影链 + 端点投影测试（无硬件，FakeSession）。

覆盖：
- ``deployment_projection.project_upwards``：分片 → 引擎 → 副本 → 端点 → 部署健康灯
  逐级推导（全就绪/任一死/部分就绪/全待启动/无副本行 no-op）；
- ``orchestrator._finalize`` 副本级端点投影：多副本每副本×每协议一行、prefill 引擎
  永不出端点、孤儿端点（含绑定）清理、api_endpoint 代表地址 = 第一份副本。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
from llmpod.apps.manager.deployment.schemas import InstanceSpec
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.node import Node
from llmpod.models.route_binding import RouteBinding
from llmpod.services import deployment_projection
from llmpod.services.node_service import mark_node_offline, register_node
from llmpod.tunnel.schemas.system import NodeReport

from _fakes import FakeSession


# ---- 投影链 fixture ----


def _mk_stack(session, *, dep_status="running", shard_statuses=("READY",), engine_role="full"):
    """单副本单引擎 + 逐分片容器 + 一条挂副本的端点；返回各行引用。"""
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-p111",
            cluster_id=1,
            name="p1",
            model_id=1,
            engine="vllm",
            status=dep_status,
            phase="RUNNING",
            health_status="healthy",
        )
    )
    rep = session.preload(
        ModelDeploymentReplica(
            replica_id="rep-1",
            deployment_id=dep.deployment_id,
            seq=0,
            status="provisioning",
        )
    )
    eng = session.preload(
        ModelDeploymentInstance(
            instance_id="eng-1",
            replica_id="rep-1",
            role=engine_role,
            tp=1,
            pp=1,
            nnodes=len(shard_statuses),
            status="provisioning",
        )
    )
    shards = [
        session.preload(
            Instance(
                instance_id=f"i-{i}",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id="eng-1",
                role="head" if i == 0 else "worker",
                rank=i,
                status=st,
                spec={"node_rank": i},
            )
        )
        for i, st in enumerate(shard_statuses)
    ]
    ep = session.preload(
        ModelEndpoint(
            id=1,
            deployment_id=dep.deployment_id,
            model_deployment_replica_id="rep-1",
            engine_type="vllm",
            protocol="openai",
            api_base="http://10.0.1.5:8000/v1",
            served_model="m",
            name="m",
            health_status="healthy",
        )
    )
    return dep, rep, eng, shards, ep


class TestProjectionChain:
    async def test_all_shards_ready_projects_serving(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("READY", "READY"))

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "ready"
        assert rep.status == "serving"
        assert ep.health_status == "healthy"
        assert dep.health_status == "healthy"

    async def test_any_shard_dead_projects_down_to_endpoint(self):
        """多机引擎 worker 崩：引擎/副本判死 → 端点摘流量 → 部署降级。"""
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("READY", "CRASHED"))

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "down"
        assert rep.status == "down"
        assert ep.health_status == "down"
        assert dep.health_status == "degraded"

    async def test_partial_ready_is_starting_not_serving(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("READY", "RUNNING"))

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "starting"
        assert rep.status == "provisioning"
        assert ep.health_status == "down"

    async def test_all_pending_stays_provisioning(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("PENDING",))

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "provisioning"
        assert rep.status == "provisioning"

    async def test_stopped_shard_counts_as_down(self):
        """停止中的分片无法服务：引擎按 down 收敛（缩容排空窗口语义）。"""
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("STOPPING",))

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "down" and rep.status == "down"

    async def test_no_replica_rows_is_noop(self):
        """无副本行的旧数据：投影整体跳过，不误写端点/健康。"""
        session = FakeSession()
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-legacy",
                cluster_id=1,
                name="l",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
            )
        )
        ep = session.preload(
            ModelEndpoint(
                id=9,
                deployment_id=dep.deployment_id,
                engine_type="vllm",
                protocol="openai",
                api_base="http://x/v1",
                served_model="m",
                name="m",
                health_status="healthy",
            )
        )

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert ep.health_status == "healthy"  # 未被误翻成 down

    async def test_replica_down_when_sister_engine_dead(self):
        """PD 份内 P 死 ⇒ 整份判死（份内配对作废）——造 prefill+decode 两引擎。"""
        session = FakeSession()
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-pd",
                cluster_id=1,
                name="pd",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                health_status="healthy",
            )
        )
        rep = session.preload(
            ModelDeploymentReplica(
                replica_id="rep-pd",
                deployment_id=dep.deployment_id,
                seq=0,
                status="serving",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-p",
                replica_id="rep-pd",
                role="prefill",
                tp=1,
                pp=1,
                nnodes=1,
                status="provisioning",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-d",
                replica_id="rep-pd",
                role="decode",
                tp=1,
                pp=1,
                nnodes=1,
                status="provisioning",
            )
        )
        session.preload(
            Instance(
                instance_id="i-p",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id="eng-p",
                status="CRASHED",
                spec={},
            )
        )
        session.preload(
            Instance(
                instance_id="i-d",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id="eng-d",
                status="READY",
                spec={"node_rank": 0},
            )
        )

        await deployment_projection.project_upwards(session, dep.deployment_id)

        engines = {e.instance_id: e for e in session._store[ModelDeploymentInstance].values()}
        assert engines["eng-p"].status == "down"
        assert engines["eng-d"].status == "ready"
        assert rep.status == "down"  # P 死 ⇒ 整份死，D 虽 ready 也不 serving
        assert dep.health_status == "degraded"


# ---- 节点离线感知投影 ----


def _mk_node(session, node_id: str, *, status: str):
    return session.preload(Node(id=node_id, cluster_id=1, hostname=node_id, ip="0.0.0.0", status=status))


class TestNodeOfflineAwareProjection:
    """节点离线感知：宿主节点离线的分片按不可服务投影——引擎/副本判死、端点摘流、
    健康灯降级；节点回线（register_node）即恢复。容器行（权威依据）不动，
    自愈触发集（Instance.status）不因此扩大。

    触发点在状态翻转写点上：mark_node_offline / register_node 翻完 Node.status
    后就地重算（reproject_after_node_transition），bootstrap 回调零改动。
    """

    async def test_mark_node_offline_reprojects_to_down(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)
        for s in shards:
            s.assigned_node = "n1"
        _mk_node(session, "n1", status="online")

        await mark_node_offline("n1", session)

        assert eng.status == "down"
        assert rep.status == "down"
        assert ep.health_status == "down"
        assert dep.health_status == "degraded"
        # 权威依据不动：分片容器态维持 READY（节点回来 monitor 首扫再对真实死容器收敛）
        assert all(s.status == "READY" for s in shards)

    async def test_register_node_restores_projection(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)
        for s in shards:
            s.assigned_node = "n1"
        _mk_node(session, "n1", status="online")
        await mark_node_offline("n1", session)
        assert ep.health_status == "down"

        await register_node("n1", 1, NodeReport(hostname="n1"), session)

        assert eng.status == "ready"
        assert rep.status == "serving"
        assert ep.health_status == "healthy"
        assert dep.health_status == "healthy"
        assert all(s.status == "READY" for s in shards)

    async def test_offline_node_only_degrades_own_replica(self):
        """跨节点双副本：仅离线节点上的副本判死摘流，另一副本继续 serving。"""
        session = FakeSession()
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-2n",
                cluster_id=1,
                name="d2",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                health_status="healthy",
            )
        )
        rows = {}
        for tag, node_id in (("a", "n-a"), ("b", "n-b")):
            rep = session.preload(
                ModelDeploymentReplica(
                    replica_id=f"rep-{tag}",
                    deployment_id=dep.deployment_id,
                    seq=ord(tag) - ord("a"),
                    status="serving",
                )
            )
            eng = session.preload(
                ModelDeploymentInstance(
                    instance_id=f"eng-{tag}",
                    replica_id=f"rep-{tag}",
                    role="full",
                    tp=1,
                    pp=1,
                    nnodes=1,
                    status="ready",
                )
            )
            session.preload(
                Instance(
                    instance_id=f"i-{tag}",
                    deployment_id=dep.deployment_id,
                    model_deployment_instance_id=f"eng-{tag}",
                    role="head",
                    rank=0,
                    status="READY",
                    assigned_node=node_id,
                    spec={"node_rank": 0},
                )
            )
            ep = session.preload(
                ModelEndpoint(
                    id=ord(tag),
                    deployment_id=dep.deployment_id,
                    model_deployment_replica_id=f"rep-{tag}",
                    engine_type="vllm",
                    protocol="openai",
                    api_base=f"http://10.0.1.{ord(tag)}/v1",
                    served_model="m",
                    name=f"m-{tag}",
                    health_status="healthy",
                )
            )
            rows[tag] = (rep, eng, ep)
            _mk_node(session, node_id, status="online")

        await mark_node_offline("n-b", session)

        assert rows["a"][0].status == "serving"
        assert rows["a"][2].health_status == "healthy"  # 幸存副本端点仍在流
        assert rows["b"][0].status == "down"
        assert rows["b"][2].health_status == "down"
        assert dep.health_status == "degraded"

    async def test_regular_recalc_keeps_offline_down(self):
        """常规重算不洗白离线：摘流后任意普通 project_upwards（容器上报/扩缩等
        有机触发路径）仍按当前 OFFLINE 节点集收敛——离线口径不依赖翻转写点的
        一次性事件状态，每次重算自查。"""
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)
        for s in shards:
            s.assigned_node = "n1"
        _mk_node(session, "n1", status="online")
        await mark_node_offline("n1", session)
        assert ep.health_status == "down"

        await deployment_projection.project_upwards(session, dep.deployment_id)

        assert eng.status == "down"
        assert ep.health_status == "down"
        assert dep.health_status == "degraded"

    async def test_engine_spanning_nodes_down_when_one_offline(self):
        """多机引擎（head@n-a + worker@n-b）任一节点离线 ⇒ 整引擎判死。"""
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session, shard_statuses=("READY", "READY"))
        shards[0].assigned_node = "n-a"
        shards[1].assigned_node = "n-b"
        _mk_node(session, "n-a", status="online")
        _mk_node(session, "n-b", status="online")

        await mark_node_offline("n-b", session)

        assert eng.status == "down"
        assert rep.status == "down"
        assert ep.health_status == "down"
        assert dep.health_status == "degraded"

    async def test_legacy_no_replica_rows_degrades(self):
        """无副本行旧数据：健康灯按容器口径兜底，节点离线同样降级。"""
        session = FakeSession()
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-legacy-off",
                cluster_id=1,
                name="lo",
                model_id=1,
                engine="vllm",
                status="running",
                phase="RUNNING",
                health_status="healthy",
            )
        )
        session.preload(
            Instance(
                instance_id="i-lo",
                deployment_id=dep.deployment_id,
                status="READY",
                assigned_node="n-off",
                spec={},
            )
        )
        _mk_node(session, "n-off", status="online")

        await mark_node_offline("n-off", session)

        assert dep.health_status == "degraded"


# ---- _finalize 副本级端点投影 ----


def _spec(instance_id: str, engine_id: str, *, replica_idx=0, node_rank=0, port=8000) -> InstanceSpec:
    return InstanceSpec(
        instance_id=instance_id,
        engine_instance_id=engine_id,
        rank=replica_idx,
        role="head" if node_rank == 0 else "worker",
        replica_idx=replica_idx,
        nnodes=1,
        node_rank=node_rank,
        assigned_node="n1",
        assigned_gpus=[0],
        master_addr="10.0.1.5",
        master_port=29501,
        port=port,
        model_path="/data/models/m",
        served_model_name="m",
    )


class TestFinalizeEndpointProjection:
    def _stack(self, session, *, copies=3):
        """N 副本 × 单 full 引擎四层预置，容器全 READY，FINALIZING。"""
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-f111",
                cluster_id=1,
                name="f1",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="FINALIZING",
                spec={"topology": {"tp": 1, "replicas": copies}},
            )
        )
        model = session.preload(
            Model(
                id=1,
                name="Qwen2.5-0.5B",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
                source="local",
            )
        )
        specs = []
        for seq in range(copies):
            rep_id, eng_id = f"rep-{seq}", f"eng-{seq}"
            session.preload(
                ModelDeploymentReplica(
                    replica_id=rep_id,
                    deployment_id=dep.deployment_id,
                    seq=seq,
                    status="provisioning",
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
                )
            )
            specs.append(_spec(f"i-{seq}", eng_id, replica_idx=seq, port=8000 + seq))
            session.preload(
                Instance(
                    instance_id=f"i-{seq}",
                    deployment_id=dep.deployment_id,
                    model_deployment_instance_id=eng_id,
                    role="full",
                    rank=seq,
                    status="READY",
                    spec=specs[-1].model_dump(),
                )
            )
        return dep, model, specs

    async def test_multi_replica_creates_endpoint_per_replica(self):
        """3 副本 → 3×2 协议 = 6 行端点；名字 ·副本1..·副本3；api_endpoint = 第一份。"""
        session = FakeSession()
        dep, model, specs = self._stack(session, copies=3)

        await get_orchestrator()._finalize(session, dep, specs, get_adapter("vllm"), model)

        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        assert len(endpoints) == 6
        by_replica: dict[str, set[str]] = {}
        for ep in endpoints:
            by_replica.setdefault(ep.model_deployment_replica_id, set()).add(ep.protocol)
        assert set(by_replica) == {"rep-0", "rep-1", "rep-2"}
        assert all(v == {"openai", "anthropic"} for v in by_replica.values())
        names = {ep.name for ep in endpoints}
        # base 是部署名（dep.name="f1"），不是模型名（model.name="Qwen2.5-0.5B"）
        assert names == {"f1·副本1", "f1·副本2", "f1·副本3"}
        # api_base 逐副本各自 head 分片端口；代表地址 = 第一份
        bases = {ep.api_base for ep in endpoints}
        assert bases == {"http://10.0.1.5:8000/v1", "http://10.0.1.5:8001/v1", "http://10.0.1.5:8002/v1"}
        assert dep.api_endpoint == "http://10.0.1.5:8000/v1"
        assert dep.status == "running"
        # 四层达成态
        assert all(r.status == "serving" for r in session._store[ModelDeploymentReplica].values())

    async def test_single_replica_keeps_bare_name(self):
        session = FakeSession()
        dep, model, specs = self._stack(session, copies=1)

        await get_orchestrator()._finalize(session, dep, specs, get_adapter("vllm"), model)

        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        assert len(endpoints) == 2
        assert all(ep.name == "f1" for ep in endpoints)

    async def test_finalize_idempotent_reentry_fills_missing_only(self):
        """FINALIZING 崩溃恢复：已有 (rep-0, openai) 行 → 只补缺，不翻倍。"""
        session = FakeSession()
        dep, model, specs = self._stack(session, copies=2)
        session.preload(
            ModelEndpoint(
                id=1,
                deployment_id=dep.deployment_id,
                model_deployment_replica_id="rep-0",
                engine_type="vllm",
                protocol="openai",
                api_base="http://10.0.1.5:8000/v1",
                served_model="m",
                name="m",
                health_status="healthy",
            )
        )

        await get_orchestrator()._finalize(session, dep, specs, get_adapter("vllm"), model)

        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        assert len(endpoints) == 4  # 2 副本 × 2 协议，rep-0/openai 复用既有行

    async def test_orphan_endpoint_and_binding_cleaned(self):
        """改拓扑遗留的孤儿端点行（replica 已不存在）连绑定一并清，防残留端点进路由。"""
        session = FakeSession()
        dep, model, specs = self._stack(session, copies=1)
        orphan = session.preload(
            ModelEndpoint(
                id=7,
                deployment_id=dep.deployment_id,
                model_deployment_replica_id="rep-gone",
                engine_type="vllm",
                protocol="openai",
                api_base="http://x/v1",
                served_model="m",
                name="m",
                health_status="down",
            )
        )
        session.preload(RouteBinding(route_id=1, endpoint_id=7, weight=1, priority=0, enabled=True))

        await get_orchestrator()._finalize(session, dep, specs, get_adapter("vllm"), model)

        assert orphan not in session._store.get(ModelEndpoint, {}).values()
        # 孤儿的绑定随行清掉；新端点的自动发布绑定不受影响
        bindings = list(session._store.get(RouteBinding, {}).values())
        assert all(b.endpoint_id != 7 for b in bindings)
        assert bindings, "新端点应有自动发布绑定"

    async def test_prefill_engine_never_gets_endpoint(self):
        """PD 槽位：role=prefill 的引擎永不出端点；decode 引擎正常出（造行验证）。"""
        session = FakeSession()
        dep = session.preload(
            ModelDeployment(
                deployment_id="dep-pd2",
                cluster_id=1,
                name="pd2",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="FINALIZING",
                spec={"topology": {"tp": 1, "replicas": 1}},
            )
        )
        model = session.preload(
            Model(
                id=1,
                name="m",
                model_type="llm",
                size_gb=1.0,
                format="safetensors",
                source="local",
            )
        )
        session.preload(
            ModelDeploymentReplica(
                replica_id="rep-pd",
                deployment_id=dep.deployment_id,
                seq=0,
                status="provisioning",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-p",
                replica_id="rep-pd",
                role="prefill",
                tp=1,
                pp=1,
                nnodes=1,
                status="ready",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-d",
                replica_id="rep-pd",
                role="decode",
                tp=1,
                pp=1,
                nnodes=1,
                status="ready",
            )
        )
        specs = [
            _spec("i-p", "eng-p"),
            _spec("i-d", "eng-d", port=8001),
        ]

        await get_orchestrator()._finalize(session, dep, specs, get_adapter("vllm"), model)

        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        # 只 decode 引擎出端点（2 协议）；api_base 取 decode head 分片（8001）
        assert len(endpoints) == 2
        assert all(ep.api_base == "http://10.0.1.5:8001/v1" for ep in endpoints)
        assert all(ep.model_deployment_replica_id == "rep-pd" for ep in endpoints)


class TestSyncEndpointNames:
    """端点展示名随副本集重算：多副本 ``base·副本N``、单副本裸名。

    扩缩只增删副本行，已存在的端点行不会自动改名——缺这次重算，前端副本列表与路由
    成员名会长期不一致（1→2 份后首个端点仍裸名，2→1 份后幸存者仍带「·副本1」）。
    """

    async def test_single_to_multi_renames_existing(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)  # 单份：名 = 裸名
        session.preload(
            ModelDeploymentReplica(replica_id="rep-2", deployment_id=dep.deployment_id, seq=1, status="serving")
        )
        session.preload(
            ModelEndpoint(
                id=2,
                deployment_id=dep.deployment_id,
                model_deployment_replica_id="rep-2",
                engine_type="vllm",
                protocol="openai",
                api_base="http://10.0.1.5:8001/v1",
                served_model="m",
                name="p1·副本2",
                health_status="healthy",
            )
        )

        await deployment_projection.sync_endpoint_names(session, dep)

        # base 是**部署名**（_mk_stack 的 dep.name="p1"），不是模型名（served_model="m"）
        assert ep.name == "p1·副本1"
        assert session._store[ModelEndpoint][2].name == "p1·副本2"

    async def test_multi_to_single_drops_suffix(self):
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)
        ep.name = "p1·副本1"  # 上一轮多副本形态留下的名字
        session.preload(
            ModelDeploymentReplica(replica_id="rep-2", deployment_id=dep.deployment_id, seq=1, status="serving")
        )

        await deployment_projection.sync_endpoint_names(session, dep)
        assert ep.name == "p1·副本1"  # 仍是两份

        session._store[ModelDeploymentReplica].pop("rep-2")  # 缩容摘掉第二份
        await deployment_projection.sync_endpoint_names(session, dep)
        assert ep.name == "p1"

    async def test_overlong_base_clipped_to_column_width(self):
        """超长部署名收口到端点名列宽：多副本后缀会把长名顶过 varchar(255)，
        不收口的话 FINALIZING 写端点行时 PG 直接拒（容器已起却翻失败）。"""
        session = FakeSession()
        dep, rep, eng, shards, ep = _mk_stack(session)
        session.preload(
            ModelDeploymentReplica(replica_id="rep-2", deployment_id=dep.deployment_id, seq=1, status="serving")
        )
        dep.name = "长" * 300

        await deployment_projection.sync_endpoint_names(session, dep)

        limit = ModelEndpoint.ENDPOINT_NAME_MAX_CHARS
        assert ep.name == "长" * (limit - len("·副本1")) + "·副本1"
        assert len(ep.name) == limit
