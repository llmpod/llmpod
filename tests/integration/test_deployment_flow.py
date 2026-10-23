"""部署编排集成测试：orchestrator 交接 + reconciler phase 推进（无硬件）。

用 FakeSession（真 DB 语义：get/add/flush/按 PK 索引）+ FakeHub/FakeProxy（stub node RPC），
覆盖两段链路：
- ``orchestrator.deploy()``：加载→拓扑展开→GPU调度+VRAM预留→rank→staging（node_model 轮询）
  → 写回 model_path + phase=PULL_IMAGE 交接（不再内联 docker_run/health）；
- ``reconciler.advance_deployment``：PULL_IMAGE→START_HEAD→WAIT_HEAD_READY→FINALIZING→RUNNING
  （Task 行驱动 + 双门判定 + attempt 超限 fail）。
真实 docker/GPU 由 e2e（test_model_deployments）覆盖；本测试验证编排接线正确性。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import Delete

from llmpod.apps.manager.deployment import orchestrator as orch_mod
from llmpod.apps.manager.deployment import reconciler as rec_mod
from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
from llmpod.apps.manager.deployment.reconciler import advance_deployment
from llmpod.config.constants import TaskType
from llmpod.apps.manager.deployment.resource_ledger import ResourceLedger
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.node_model import NodeModel
from llmpod.models.node import Node
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.route import Route
from llmpod.models.route_binding import RouteBinding
from llmpod.models.task import Task
from llmpod.tunnel.schemas import (
    ContainerState,
    DeployResult,
    RemoveResult,
    StopResult,
)
from _fakes import stub_model_verify


# ---- FakeSession：按 PK 索引的内存 async session ----


class FakeSession:
    def __init__(self):
        self._store: dict = {}  # type -> {pk: obj}
        self._staged: list = []
        self._counters: dict = {}

    def preload(self, obj):
        self._store.setdefault(type(obj), {})[self._pk(obj)] = obj
        return obj

    def _pk(self, obj):
        # 取 ORM 主键列（避免 Instance 同时有 deployment_id+instance_id 的歧义）；
        # 复合主键（node_model）返回 tuple 键，防同 model_id 不同 node 的行互相覆盖。
        pk_cols = list(obj.__table__.primary_key.columns)
        if len(pk_cols) > 1:
            return tuple(getattr(obj, c.key, None) for c in pk_cols)
        if pk_cols:
            return getattr(obj, pk_cols[0].key, None)
        return None

    async def get(self, cls, pk):
        return self._store.get(cls, {}).get(pk)

    def add(self, obj):
        self._staged.append(obj)

    def begin_nested(self):
        """SAVEPOINT 桩：直通上下文（回滚到保存点的语义由 service 侧承担）。"""

        @asynccontextmanager
        async def _sp():
            yield

        return _sp()

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

    async def refresh(self, obj):
        pass

    async def execute(self, stmt):
        """支持 select(Entity).where(...) 与字面量 delete(...).where(...)：
        等值(==) 与 IN(.in_()) 子句（orchestrator/controller/deployment_service 用）。"""
        if isinstance(stmt, Delete):
            return self._execute_delete(stmt)
        entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
        objs = list(self._store.get(entity, {}).values())
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                objs = [o for o in objs if getattr(o, wc.left.key, None) in val]  # IN
            else:
                objs = [o for o in objs if getattr(o, wc.left.key, None) == val]  # 等值
        # 单列投影 select(Entity.col)（expr 是列而非实体）→ 标量流（判空 in_ 集合等）
        descs = stmt.column_descriptions or []
        if len(descs) == 1 and descs[0].get("expr") is not entity:
            return _FakeResult([getattr(o, descs[0]["name"], None) for o in objs])
        return _FakeResult(objs)

    def _execute_delete(self, stmt):
        """字面量 delete()：等值/IN where 匹配的行从 store 移除（批量清规划产物用）。"""
        cls = next(
            (c for c in self._store if getattr(getattr(c, "__table__", None), "name", "") == stmt.table.name),
            None,
        )
        if cls is None:
            return _FakeResult([])
        store = self._store.get(cls, {})
        wc = stmt.whereclause
        if wc is not None and hasattr(wc, "left") and hasattr(wc.left, "key"):
            right = wc.right
            val = getattr(right, "effective_value", None)
            if val is None:
                val = getattr(right, "value", right)
            if isinstance(val, (list, tuple, set)):
                doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) in val]
            else:
                doomed = [pk for pk, o in store.items() if getattr(o, wc.left.key, None) == val]
        else:
            doomed = list(store.keys())
        for pk in doomed:
            store.pop(pk, None)
        return _FakeResult([])

    async def delete(self, obj):
        for d in self._store.values():
            for k in list(d):
                if d[k] is obj:
                    del d[k]


class _FakeResult:
    def __init__(self, objs):
        self._objs = list(objs)

    def scalars(self):
        return self._objs

    def scalar(self):
        return self._objs[0] if self._objs else None

    def scalar_one_or_none(self):
        return self._objs[0] if self._objs else None


# ---- FakeHub / FakeProxy：stub node RPC ----


class FakeProxy:
    def __init__(self):
        self.calls = []

    async def deploy(self, spec):
        self.calls.append(("deploy", spec.name))
        return DeployResult(success=True, container_name=spec.name)

    async def inspect(self, name):
        self.calls.append(("inspect", name))
        return ContainerState(Status="running", Running=True, Restarting=False, RestartCount=0)

    async def logs(self, name, tail=200):
        self.calls.append(("logs", name, tail))
        return "fake-container-log"

    async def list(self, label_filter=None, all=True):
        self.calls.append(("list", label_filter, all))
        return []  # 无残留容器（首次部署/清理 no-op）

    async def stop(self, name, timeout=10):
        self.calls.append(("stop", name))
        return StopResult(success=True, container_name=name)

    async def remove(self, name, force=False):
        self.calls.append(("rm", name, force))
        return RemoveResult(success=True, removed=True, container_name=name)


class FakeHub:
    def __init__(self):
        self.proxy_for = {"n1": FakeProxy()}
        # 容器名 → 代理，便于断言
        self.proxies = self.proxy_for

    def get(self, node_id):
        return self.proxy_for.get(node_id) and object()  # 在线判定非 None 即可

    def proxy(self, node_id, cls):
        return self.proxy_for[node_id]


@pytest.fixture
def deployed_stack(monkeypatch):
    """预置 Model/Version/Node/Gpu + 部署行，stub 掉 DB/hub（reconciler 共用同一 session）。"""
    session = FakeSession()
    session.preload(
        Node(
            id="n1",
            cluster_id=1,
            hostname="h1",
            ip="10.0.1.5",
            status="online",
            cpu_cores=8,
            memory_gb=64.0,
        )
    )
    session.preload(
        Gpu(uuid="g0", node_id="n1", index=0, name="RTX2080Ti", arch="nvidia", total_vram_gb=11.0, status="online")
    )
    session.preload(
        Gpu(uuid="g1", node_id="n1", index=1, name="RTX2080Ti", arch="nvidia", total_vram_gb=11.0, status="online")
    )
    model = session.preload(
        Model(
            id=1,
            name="Qwen2.5-0.5B",
            model_type="llm",
            size_gb=1.0,
            format="safetensors",
            source="local",
            source_url="/data/models/Qwen2.5-0.5B",
            source_kind="transformers_folder",
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-aaaa1111-2222",
            cluster_id=1,
            name="d1",
            model_id=model.id,
            engine="vllm",
            status="creating",
            phase="PENDING",
            spec={
                "topology": {"mode": "tensor_parallel", "tp": 1, "replicas": 1},
                "model_path": "/data/models/Qwen2.5-0.5B",
            },
        )
    )
    # staging：node_model 已 present → _stage_models 首轮即放行（happy path 瞬时）
    session.preload(
        NodeModel(
            model_id=model.id,
            node_id="n1",
            desired_state="present",
            status="present",
            local_dir="/data/models/Qwen2.5-0.5B",
            model_path="/data/models/Qwen2.5-0.5B",
        )
    )

    # stub get_db_context → 共享 session（orchestrator / placement / reconciler 同一 FakeSession）
    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(orch_mod, "get_db_context", _fake_db)
    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.placement.get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.stop.get_db_context", _fake_db)

    # stub hub（RPC 经 dispatch 门面：patch dispatch 的 hub 获取）
    fake_hub = FakeHub()
    from llmpod.apps.manager import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_get_hub", lambda: fake_hub)

    # stub select-based loaders（已下沉 deployment_service，patch 服务函数）
    from llmpod.services import deployment_service
    from llmpod.apps.manager.deployment.scheduler.gpu import GpuCandidate

    async def _gpus(db):
        return [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=11.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=11.0, status="online"),
        ]

    async def _ips(db, node_ids):
        return {"n1": "10.0.1.5"}

    monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
    monkeypatch.setattr(deployment_service, "load_node_ips", _ips)

    # staging 放行以复核任务终态为准（node_model.status 只是缓存）→ 预置 SUCCESS 桩任务
    stub_model_verify(monkeypatch, session, model.id, ["n1"])

    # 重置单例（保证用新 ledger）
    orch_mod._orchestrator = None
    return session, dep, fake_hub


@pytest.fixture
def deployed_stack_multinode(monkeypatch):
    """多机预置：2 节点各 2 卡，tp=2/nnodes=2 单 replica（head+worker），两 node 模型已 present。"""
    session = FakeSession()
    session.preload(
        Node(
            id="n1",
            cluster_id=1,
            hostname="h1",
            ip="10.0.1.5",
            status="online",
            cpu_cores=8,
            memory_gb=64.0,
        )
    )
    session.preload(
        Node(
            id="n2",
            cluster_id=1,
            hostname="h2",
            ip="10.0.1.6",
            status="online",
            cpu_cores=8,
            memory_gb=64.0,
        )
    )
    for uid, nid, idx in (("g0", "n1", 0), ("g1", "n1", 1), ("g2", "n2", 0), ("g3", "n2", 1)):
        session.preload(
            Gpu(uuid=uid, node_id=nid, index=idx, name="RTX2080Ti", arch="nvidia", total_vram_gb=24.0, status="online")
        )
    model = session.preload(
        Model(
            id=1,
            name="Qwen2.5-0.5B",
            model_type="llm",
            size_gb=1.0,
            format="safetensors",
            source="local",
            source_url="/data/models/Qwen2.5-0.5B",
            source_kind="transformers_folder",
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-bbbb1111-2222",
            cluster_id=1,
            name="d-mn",
            model_id=model.id,
            engine="vllm",
            status="creating",
            phase="PENDING",
            spec={"topology": {"tp": 2, "nnodes": 2, "replicas": 1}, "model_path": "/data/models/Qwen2.5-0.5B"},
        )
    )
    for nid in ("n1", "n2"):
        session.preload(
            NodeModel(
                model_id=model.id,
                node_id=nid,
                desired_state="present",
                status="present",
                local_dir="/data/models/Qwen2.5-0.5B",
                model_path="/data/models/Qwen2.5-0.5B",
            )
        )

    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(orch_mod, "get_db_context", _fake_db)
    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.placement.get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.stop.get_db_context", _fake_db)

    fake_hub = FakeHub()
    fake_hub.proxy_for["n2"] = FakeProxy()
    from llmpod.apps.manager import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_get_hub", lambda: fake_hub)

    from llmpod.services import deployment_service
    from llmpod.apps.manager.deployment.scheduler.gpu import GpuCandidate

    async def _gpus(db):
        return [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g3", node_id="n2", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]

    async def _ips(db, node_ids):
        return {"n1": "10.0.1.5", "n2": "10.0.1.6"}

    monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
    monkeypatch.setattr(deployment_service, "load_node_ips", _ips)

    # staging 放行以复核任务终态为准（node_model.status 只是缓存）→ 预置 SUCCESS 桩任务
    stub_model_verify(monkeypatch, session, model.id, ["n1", "n2"])

    orch_mod._orchestrator = None
    return session, dep, fake_hub


@pytest.fixture
def deployed_stack_cpu(monkeypatch):
    """纯 CPU 部署预置：无 GPU 节点 + GGUF 模型（架构形状齐备）+ allow_cpu/kv_cache.mode=cpu。"""
    from llmpod.apps.manager.deployment.schemas import NodeCandidate

    session = FakeSession()
    session.preload(
        Node(id="n1", cluster_id=1, hostname="h1", ip="10.0.1.5", status="online", cpu_cores=8, memory_gb=64.0)
    )
    session.preload(
        Node(id="n2", cluster_id=1, hostname="h2", ip="10.0.1.6", status="online", cpu_cores=8, memory_gb=128.0)
    )
    model = session.preload(
        Model(
            id=2,
            name="tiny-gguf",
            model_type="llm",
            size_gb=1.0,
            format="gguf",
            source="local",
            source_url="/data/models/tiny.gguf",
            source_kind="single_gguf",
            num_layers=32,
            num_kv_heads=8,
            head_dim=128,
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-cpu1111-2222",
            cluster_id=1,
            name="cpu-dep",
            model_id=model.id,
            engine="llamacpp",
            status="creating",
            phase="PENDING",
            spec={
                "topology": {"tp": 1, "replicas": 1},
                "allow_cpu": True,
                "model_path": "/data/models/tiny.gguf",
            },
        )
    )
    session.preload(
        NodeModel(
            model_id=model.id,
            node_id="n1",
            desired_state="present",
            status="present",
            local_dir="/data/models/tiny.gguf",
            model_path="/data/models/tiny.gguf",
        )
    )
    session.preload(
        NodeModel(
            model_id=model.id,
            node_id="n2",
            desired_state="present",
            status="present",
            local_dir="/data/models/tiny.gguf",
            model_path="/data/models/tiny.gguf",
        )
    )

    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(orch_mod, "get_db_context", _fake_db)
    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.placement.get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.stop.get_db_context", _fake_db)

    fake_hub = FakeHub()
    from llmpod.apps.manager import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_get_hub", lambda: fake_hub)

    from llmpod.services import deployment_service

    async def _no_gpus(db):
        return []

    async def _nodes(db, cluster_id=None):
        return [
            NodeCandidate(node_id="n1", hostname="h1", ip="10.0.1.5", memory_gb=64.0),
            NodeCandidate(node_id="n2", hostname="h2", ip="10.0.1.6", memory_gb=128.0),
        ]

    async def _ips(db, node_ids):
        return {"n1": "10.0.1.5", "n2": "10.0.1.6"}

    monkeypatch.setattr(deployment_service, "load_online_gpus", _no_gpus)
    monkeypatch.setattr(deployment_service, "load_online_nodes", _nodes)
    monkeypatch.setattr(deployment_service, "load_node_ips", _ips)

    # staging 放行以复核任务终态为准（node_model.status 只是缓存）→ 预置 SUCCESS 桩任务
    stub_model_verify(monkeypatch, session, model.id, ["n1", "n2"])

    orch_mod._orchestrator = None
    return session, dep, fake_hub


@pytest.fixture
def deployed_stack_hybrid(monkeypatch):
    """混合 offload 预置：单卡 11GB 装不下 20GB 模型，节点内存 64GB → 逐层拆分。

    混合档系统不推荐（auto 只取全量/纯 CPU 两端），必须显式 ``offload_preference=gpu_main``。
    """
    from llmpod.apps.manager.deployment.schemas import NodeCandidate

    session = FakeSession()
    session.preload(
        Node(id="n1", cluster_id=1, hostname="h1", ip="10.0.1.5", status="online", cpu_cores=8, memory_gb=64.0)
    )
    session.preload(
        Gpu(uuid="g0", node_id="n1", index=0, name="RTX2080Ti", arch="nvidia", total_vram_gb=11.0, status="online")
    )
    model = session.preload(
        Model(
            id=3,
            name="big-gguf",
            model_type="llm",
            size_gb=20.0,
            format="gguf",
            source="local",
            source_url="/data/models/big.gguf",
            source_kind="single_gguf",
            num_layers=40,
            num_kv_heads=8,
            head_dim=128,
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-hyb1111-2222",
            cluster_id=1,
            name="hybrid-dep",
            model_id=model.id,
            engine="llamacpp",
            status="creating",
            phase="PENDING",
            spec={
                "topology": {"tp": 1, "replicas": 1, "max_model_len": 8192},
                "allow_cpu": True,
                "offload_preference": "gpu_main",
                "model_path": "/data/models/big.gguf",
            },
        )
    )
    session.preload(
        NodeModel(
            model_id=model.id,
            node_id="n1",
            desired_state="present",
            status="present",
            local_dir="/data/models/big.gguf",
            model_path="/data/models/big.gguf",
        )
    )

    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(orch_mod, "get_db_context", _fake_db)
    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.placement.get_db_context", _fake_db)
    monkeypatch.setattr("llmpod.apps.manager.deployment.stop.get_db_context", _fake_db)

    fake_hub = FakeHub()
    from llmpod.apps.manager import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_get_hub", lambda: fake_hub)

    from llmpod.services import deployment_service
    from llmpod.apps.manager.deployment.scheduler.gpu import GpuCandidate

    async def _gpus(db):
        return [GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=11.0, status="online")]

    async def _nodes(db, cluster_id=None):
        return [NodeCandidate(node_id="n1", hostname="h1", ip="10.0.1.5", memory_gb=64.0)]

    async def _ips(db, node_ids):
        return {"n1": "10.0.1.5"}

    monkeypatch.setattr(deployment_service, "load_online_gpus", _gpus)
    monkeypatch.setattr(deployment_service, "load_online_nodes", _nodes)
    monkeypatch.setattr(deployment_service, "load_node_ips", _ips)

    # staging 放行以复核任务终态为准（node_model.status 只是缓存）→ 预置 SUCCESS 桩任务
    stub_model_verify(monkeypatch, session, model.id, ["n1"])

    orch_mod._orchestrator = None
    return session, dep, fake_hub


class TestDeployCpuLane:
    """纯 CPU 部署：无卡时选节点 + RAM 预留 + 启动参数（不下发任何 GPU 语义参数）。"""

    async def test_cpu_only_deploy_places_by_ram(self, deployed_stack_cpu):
        from llmpod.apps.manager.deployment.orchestrator import build_run_spec
        from llmpod.apps.manager.deployment.schemas import InstanceSpec

        session, dep, _hub = deployed_stack_cpu
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"

        instances = [v for v in session._store.get(Instance, {}).values()]
        assert len(instances) == 1
        inst = instances[0]
        # best-fit:两份节点内存都够时优先填小节点(n1 64GB < n2 128GB)
        assert inst.assigned_node == "n1"
        assert inst.spec["assigned_gpus"] == []
        assert inst.spec["gpu_arch"] == "cpu"
        assert inst.spec["offload_layers"] == 0
        assert inst.spec["ram_claim_gb"] > 0

        # RAM 预留已记账（无 VRAM 预留）
        ram = orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id)
        assert set(ram) == {"n1"} and ram["n1"] == inst.spec["ram_claim_gb"]
        assert orch.ledger.get_reserved_for_deployment(dep.deployment_id) == {}

        # 启动参数：不申请 GPU 设备 + 不下发任何 GPU 语义参数（纯 CPU 无卡）
        model = await session.get(Model, dep.model_id)
        spec = build_run_spec(dep_obj, model, InstanceSpec.model_validate(inst.spec))
        assert spec.gpus is None
        assert "--n-gpu-layers" not in spec.args

    async def test_cpu_only_preference_without_allow_cpu_still_places_on_cpu(self, deployed_stack_cpu):
        """显式 offload_preference=cpu_only 隐含允许纯 CPU：allow_cpu=False 也要按 CPU 调度。

        部署门控只看 allow_cpu 的话，这个偏好会被静默忽略、按显存档部署，且编排器里
        「显式要纯 CPU 却无解」的报错分支永远不可达——而 create 路由/预览/扩缩都把
        cpu_only 当 CPU 部署信号，口径会自相矛盾。
        """
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {**dep_obj.spec, "allow_cpu": False, "offload_preference": "cpu_only"}
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"
        inst = [v for v in session._store.get(Instance, {}).values()][0]
        assert inst.spec["gpu_arch"] == "cpu"
        assert inst.spec["offload_layers"] == 0
        assert orch.ledger.get_reserved_for_deployment(dep.deployment_id) == {}

    async def test_cpu_only_deploy_honors_pinned_node(self, deployed_stack_cpu):
        """指定节点（gpu_selector.node_id，前端 CPU 节点选取走这条）：落在指定节点，不走 best-fit。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        spec = dict(dep_obj.spec)
        spec["gpu_selector"] = {"node_id": "n2"}
        dep_obj.spec = spec

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        inst = [v for v in session._store.get(Instance, {}).values()][0]
        assert inst.assigned_node == "n2"
        assert orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id) == {"n2": inst.spec["ram_claim_gb"]}

    async def test_cpu_only_deploy_injects_mmproj(self, deployed_stack_cpu):
        """视觉模型（Model.mmproj_file 有值）→ mmproj_path 落 spec 且启动参数带 --mmproj。"""
        from llmpod.apps.manager.deployment.orchestrator import build_run_spec
        from llmpod.apps.manager.deployment.schemas import InstanceSpec

        session, dep, _hub = deployed_stack_cpu
        model = await session.get(Model, dep.model_id)
        model.mmproj_file = "mmproj-F16.gguf"

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        inst = [v for v in session._store.get(Instance, {}).values()][0]
        mmproj_path = inst.spec["mmproj_path"]
        # 与主模型同落 canonical 目录（fixture 的 node_model.local_dir 即该目录）
        assert mmproj_path == "/data/models/tiny.gguf/mmproj-F16.gguf"

        spec = build_run_spec(dep_obj, model, InstanceSpec.model_validate(inst.spec))
        assert spec.args[spec.args.index("--mmproj") + 1] == mmproj_path
        # 卷挂主模型所在目录（只挂单文件时同目录的 mmproj 进不了容器 → 引擎报缺件）
        assert spec.volumes == {"/data/models": "/data/models"}

    async def test_cpu_only_without_ram_rejected(self, deployed_stack_cpu):
        """显式 cpu_only 且内存装不下 → 规划期失败（不静默回退到显存档）。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        spec = dict(dep_obj.spec)
        # 上下文拉到 10M → KV 需求远超 64GB 内存 → 纯 CPU 档无解
        spec["topology"] = {**spec.get("topology", {}), "max_model_len": 10_000_000}
        spec["offload_preference"] = "cpu_only"
        dep_obj.spec = spec

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)  # 自身异常被吞并标 failed（await_deploy 契约）

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "failed"
        assert "内存装不下纯 CPU" in (dep_obj.last_error or "")

    async def test_cpu_only_multi_replica_places_each_instance(self, deployed_stack_cpu):
        """多副本纯 CPU（自动调度）：逐实例 binpack 到集群节点 + 账本按节点聚合各份 claim。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {**dep_obj.spec, "topology": {"tp": 1, "replicas": 3}}

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "PULL_IMAGE"
        instances = [v for v in session._store.get(Instance, {}).values()]
        assert len(instances) == 3
        assert all(i.spec["gpu_arch"] == "cpu" and i.spec["ram_claim_gb"] > 0 for i in instances)
        # 调度全在部署集群节点内（fixture 只答 cluster 1 的 n1/n2）
        assert {i.assigned_node for i in instances} <= {"n1", "n2"}
        # 账本按节点聚合 = 落在该节点各实例 claim 之和（同节点多份累加）
        ram = orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id)
        claims_by_node: dict[str, float] = {}
        for i in instances:
            claims_by_node[i.assigned_node] = claims_by_node.get(i.assigned_node, 0.0) + i.spec["ram_claim_gb"]
        assert set(ram) == set(claims_by_node)
        for node, total in claims_by_node.items():
            assert ram[node] == pytest.approx(total)

    async def test_cpu_only_multi_replica_per_replica_pins(self, deployed_stack_cpu):
        """逐副本指定（显式 cpu_only + node_ids）：第 i 份放 node_ids[i]，各节点独立记账。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {
            **dep_obj.spec,
            "topology": {"tp": 1, "replicas": 2},
            "gpu_selector": {"node_ids": ["n2", "n1"]},
            "offload_preference": "cpu_only",
        }

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        instances = [v for v in session._store.get(Instance, {}).values()]
        assert len(instances) == 2
        by_rank = {i.spec.get("rank", 0): i for i in instances}
        # 位置对应份序：第 1 份 → n2、第 2 份 → n1（不受 best-fit 影响，指定即分配）
        assert by_rank[0].assigned_node == "n2"
        assert by_rank[1].assigned_node == "n1"
        ram = orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id)
        assert set(ram) == {"n1", "n2"}
        assert ram["n1"] == by_rank[1].spec["ram_claim_gb"]
        assert ram["n2"] == by_rank[0].spec["ram_claim_gb"]

    async def test_cpu_only_per_replica_pins_offline_node_fails(self, deployed_stack_cpu):
        """指定到不在线节点：规划失败（不静默改走自动分配——用户指定的就是那台）。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {
            **dep_obj.spec,
            "topology": {"tp": 1, "replicas": 2},
            "gpu_selector": {"node_ids": ["n1", "n-x"]},
            "offload_preference": "cpu_only",
        }

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "failed"
        assert "不在线" in (dep_obj.last_error or "")
        assert orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id) == {}

    async def test_cpu_only_multi_replica_pinned_single_node(self, deployed_stack_cpu):
        """指定单节点 + 多副本：全部份落在指定节点（内存按份数累加），不扩散到其它节点。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {
            **dep_obj.spec,
            "topology": {"tp": 1, "replicas": 2},
            "gpu_selector": {"node_id": "n2"},
        }

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        instances = [v for v in session._store.get(Instance, {}).values()]
        assert len(instances) == 2
        assert {i.assigned_node for i in instances} == {"n2"}
        ram = orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id)
        assert set(ram) == {"n2"}
        assert ram["n2"] == pytest.approx(sum(i.spec["ram_claim_gb"] for i in instances))

    async def test_cpu_only_multi_replica_no_fit_fails(self, deployed_stack_cpu):
        """副本数超出集群内存容量：规划失败标 failed，不留账本残留（预检同口径的前移校验）。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        # 单份 ~4GB × 200 份 >> n1 57.6 + n2 115.2（账本可用）
        dep_obj.spec = {**dep_obj.spec, "topology": {"tp": 1, "replicas": 200}}

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "failed"
        assert orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id) == {}

    async def test_cpu_only_node_pool_scoped_to_deploy_cluster(self, deployed_stack_cpu, monkeypatch):
        """CPU 调度候选池按部署集群收窄：编排器须带 cluster_id 取池，集群内装不下就失败，
        不得静默落到其它集群的节点（回归：平台级节点池会把副本 binpack 到目标集群外）。"""
        from llmpod.apps.manager.deployment.schemas import NodeCandidate
        from llmpod.services import deployment_service

        seen: dict = {}

        async def _nodes(db, cluster_id=None):
            seen["cluster_id"] = cluster_id
            # 只答部署集群（cluster 1）的节点，且都装不下单份；其它集群的胖节点不可见
            return [
                NodeCandidate(node_id="n1", hostname="h1", ip="10.0.1.5", memory_gb=1.0),
                NodeCandidate(node_id="n2", hostname="h2", ip="10.0.1.6", memory_gb=1.0),
            ]

        monkeypatch.setattr(deployment_service, "load_online_nodes", _nodes)
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.spec = {**dep_obj.spec, "offload_preference": "cpu_only"}

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        # 编排器把部署集群传进了节点池装配（dep.cluster_id=1）
        assert seen["cluster_id"] == 1
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "failed"
        assert "内存装不下纯 CPU" in (dep_obj.last_error or "")

    async def test_load_online_nodes_filters_by_cluster(self):
        """load_online_nodes 的集群过滤（SQL 侧）：cluster_id 给定只返回该集群在线节点。"""
        from tests.integration._fakes import FakeSession

        session = FakeSession()
        session.preload(
            Node(id="a1", cluster_id=1, hostname="a1", ip="10.0.0.1", status="online", cpu_cores=8, memory_gb=64.0)
        )
        session.preload(
            Node(id="a2", cluster_id=1, hostname="a2", ip="10.0.0.2", status="offline", cpu_cores=8, memory_gb=32.0)
        )
        session.preload(
            Node(id="b1", cluster_id=2, hostname="b1", ip="10.0.0.3", status="online", cpu_cores=8, memory_gb=256.0)
        )

        from llmpod.services.deployment_service import load_online_nodes

        scoped = await load_online_nodes(session, cluster_id=1)
        assert {n.node_id for n in scoped} == {"a1"}  # 集群内 + 在线
        every = await load_online_nodes(session)
        assert {n.node_id for n in every} == {"a1", "b1"}  # 不传 = 平台级（重启查表口径）

    async def test_cpu_only_force_deploys_when_ram_insufficient(self, deployed_stack_cpu):
        """force 部署：内存装不下也执行（超额调度 + 超额记账，能否跑起来交引擎/OOM 兜底）。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        spec = dict(dep_obj.spec)
        spec["topology"] = {**spec.get("topology", {}), "max_model_len": 10_000_000}
        spec["offload_preference"] = "cpu_only"
        spec["force"] = True
        dep_obj.spec = spec

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"
        inst = [v for v in session._store.get(Instance, {}).values()][0]
        # 调度位置取可用内存最大的节点（n2 128GB > n1 64GB），需求照算不缩水
        assert inst.assigned_node == "n2"
        assert inst.spec["offload_layers"] == 0
        assert inst.spec["ram_claim_gb"] > 128.0
        # 超额也记账（走 force_reserve 路径）：后续部署能看见这份占用
        ram = orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id)
        assert ram["n2"] == inst.spec["ram_claim_gb"]

    async def test_restart_replans_when_planning_never_persisted(self, deployed_stack_cpu):
        """规划期失败的部署没有实例行 → 重启是「清空重跑 _plan」，不是 409 不可启动。"""
        session, dep, _hub = deployed_stack_cpu
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        dep_obj.status = "failed"
        dep_obj.last_error = "LLMPodValidationError: 目标节点内存装不下纯 CPU 部署"

        orch = get_orchestrator()
        await orch.restart(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "creating"
        assert dep_obj.last_error is None
        # 规划重跑：正常路径照常调度（本 fixture 内存充裕）
        await orch.deploy(dep.deployment_id)
        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"

    async def test_gpu_main_falls_back_to_cpu_when_card_too_small(self, deployed_stack_hybrid):
        """显式 gpu_main 但单卡装不下整模型 → 落纯 CPU（不再自动逐层拆分）。

        混合档已从取解中移除：层数语义跨 llama.cpp 版本不稳（新版把 output 层算进 N），
        且「账本按部分层预留 vs 引擎实际占用」会脱钩。要混合层数请自己在部署参数里传
        ``--n-gpu-layers``（该 flag 不在 protected_flags 里，可覆盖系统值）。
        """
        from llmpod.apps.manager.deployment.orchestrator import build_run_spec
        from llmpod.apps.manager.deployment.schemas import InstanceSpec

        session, dep, _hub = deployed_stack_hybrid
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"

        inst = [v for v in session._store.get(Instance, {}).values()][0]
        assert inst.assigned_node == "n1"
        assert inst.spec["gpu_arch"] == "cpu"  # 纯 CPU：不占卡
        assert inst.spec["assigned_gpu_uuids"] == []
        assert inst.spec["offload_layers"] == 0
        assert inst.spec["ram_claim_gb"] > 0  # 权重 + KV 记内存账

        # 只有内存账，没有显存账
        assert not orch.ledger.get_reserved_for_deployment(dep.deployment_id)
        assert orch.ledger.get_ram_reserved_for_deployment(dep.deployment_id) == {"n1": inst.spec["ram_claim_gb"]}

        model = await session.get(Model, dep.model_id)
        spec = build_run_spec(dep_obj, model, InstanceSpec.model_validate(inst.spec))
        assert spec.gpus is None  # 纯 CPU 不申请设备
        assert "--n-gpu-layers" not in spec.args


class TestDeployHandoff:
    """orchestrator.deploy()：规划 + staging，完成后 phase=PULL_IMAGE 交接 reconciler。"""

    async def test_deploy_hands_off_at_pull_image(self, deployed_stack):
        """staging 完成 → model_path 写回 instance.spec + phase=PULL_IMAGE；容器阶段未跑。"""
        session, dep, hub = deployed_stack
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "creating"  # 容器阶段由 reconciler 推进，deploy 不置 running
        assert dep_obj.phase == "PULL_IMAGE"  # 交接点
        assert dep_obj.api_endpoint is None
        assert not session._store.get(ModelEndpoint, {})  # endpoint 在 FINALIZING 产出

        # Instance 已持久化：rank/model_path（staging 写回的权威值）
        instances = [v for v in session._store.get(Instance, {}).values()]
        assert len(instances) == 1
        assert instances[0].status == "PENDING"
        assert instances[0].rank == 0
        assert instances[0].assigned_node == "n1"
        assert instances[0].spec["model_path"] == "/data/models/Qwen2.5-0.5B"

        # VRAM 已预留（交接不释放）
        assert orch.ledger.get_reserved_for_deployment(dep.deployment_id)

    async def test_deploy_multinode_stages_all_nodes(self, deployed_stack_multinode):
        """多机：head+worker 跨 2 节点调度；两节点模型 present → model_path 写回全部实例。"""
        session, dep, hub = deployed_stack_multinode
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.phase == "PULL_IMAGE"

        instances = sorted(session._store.get(Instance, {}).values(), key=lambda i: i.rank)
        assert len(instances) == 2
        # head + worker 落在不同节点（tp=2/nnodes=2 → 每节点 1 卡）
        assert {i.assigned_node for i in instances} == {"n1", "n2"}
        head, worker = instances
        assert head.spec["node_rank"] == 0 and head.spec["role"] == "head"
        assert worker.spec["node_rank"] == 1 and worker.spec["role"] == "worker"
        # 两实例均写回 model_path + master_addr 指向 head 节点 IP（rendezvous）
        ip_of = {"n1": "10.0.1.5", "n2": "10.0.1.6"}
        for i in instances:
            assert i.spec["model_path"] == "/data/models/Qwen2.5-0.5B"
        assert head.spec["master_addr"] == ip_of[head.assigned_node]
        assert worker.spec["master_addr"] == head.spec["master_addr"]

    async def test_deploy_fail_when_model_placement_over_cap(self, deployed_stack, monkeypatch):
        """node_model=failed 且 ensure_present 返回 None（超 attempt 上限）→ staging 抛
        LLMPodError → status=failed（模型不在不再立即失败，重试耗尽才终态）。"""
        from llmpod.apps.manager.deployment.placement import PlacementService

        session, dep, hub = deployed_stack
        nm = next(iter(session._store.get(NodeModel, {}).values()))
        nm.status = "failed"
        nm.last_error = "超过自动重试上限 (3)，等待人工处理"

        async def _over_cap(self, model_id, node_id):
            return None

        monkeypatch.setattr(PlacementService, "ensure_present", _over_cap)

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        dep_obj = await session.get(ModelDeployment, dep.deployment_id)
        assert dep_obj.status == "failed"
        assert dep_obj.last_error and "模型下载失败" in dep_obj.last_error
        assert not session._store.get(ModelEndpoint, {})
        assert not orch.ledger.get_reserved_for_deployment(dep.deployment_id)  # VRAM 释放

    async def test_plan_idempotent_reuses_existing_instances(self, deployed_stack):
        """_plan 幂等守卫：已有 Instance 行 → 复用，不重复建行/预留。"""
        session, dep, hub = deployed_stack
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)
        n1 = len(session._store.get(Instance, {}))
        reserved = dict(orch.ledger.get_reserved_for_deployment(dep.deployment_id))

        # 再次 deploy（如 controller 重驱动）：Instance 行数不变，VRAM 不重复预留
        d = await session.get(ModelDeployment, dep.deployment_id)
        d.phase = "PENDING"  # 模拟 recovery 重置后的重驱动
        await orch.deploy(dep.deployment_id)
        assert len(session._store.get(Instance, {})) == n1
        assert orch.ledger.get_reserved_for_deployment(dep.deployment_id) == reserved

    async def test_plan_backfills_topology_rows_for_legacy_deployment(self, deployed_stack):
        """旧结构数据（四层化前的部署：有容器行、无副本/引擎行）重驱动 _plan 时补建
        两层行并回填容器挂载列——否则投影链查不到份、finalize 端点计划为空，部署
        重启后永远停在 creating。"""
        from llmpod.models.model_deployment_instance import ModelDeploymentInstance
        from llmpod.models.model_deployment_replica import ModelDeploymentReplica

        session, dep, hub = deployed_stack
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        # 模拟 0008 前的旧数据：两层行不存在、容器行未挂引擎
        session._store[ModelDeploymentReplica] = {}
        session._store[ModelDeploymentInstance] = {}
        for row in session._store.get(Instance, {}).values():
            if row.deployment_id != dep.deployment_id:
                continue
            row.model_deployment_instance_id = None
            spec = dict(row.spec or {})
            spec["engine_instance_id"] = ""
            row.spec = spec

        d = await session.get(ModelDeployment, dep.deployment_id)
        d.phase = "PENDING"
        await orch.deploy(dep.deployment_id)

        replicas = [
            r for r in session._store.get(ModelDeploymentReplica, {}).values() if r.deployment_id == dep.deployment_id
        ]
        engines = list(session._store.get(ModelDeploymentInstance, {}).values())
        assert len(replicas) == 1 and len(engines) == 1  # 按份序号重建两层行
        irows = [r for r in session._store.get(Instance, {}).values() if r.deployment_id == dep.deployment_id]
        assert irows and all(r.model_deployment_instance_id == engines[0].instance_id for r in irows)
        assert all((r.spec or {}).get("engine_instance_id") == engines[0].instance_id for r in irows)

        # 幂等：再驱动一次不重复建行
        d.phase = "PENDING"
        await orch.deploy(dep.deployment_id)
        assert len(session._store.get(ModelDeploymentReplica, {})) == 1
        assert len(session._store.get(ModelDeploymentInstance, {})) == 1

    async def test_restart_backfills_topology_rows_for_legacy_deployment(self, deployed_stack):
        """旧结构部署走 restart（不经过 _plan，phase 直落 PULL_IMAGE）也要补建两层行——
        否则 finalize 查不到份、端点计划为空，部署停在 finalizing 且零端点。"""
        from llmpod.models.model_deployment_instance import ModelDeploymentInstance
        from llmpod.models.model_deployment_replica import ModelDeploymentReplica

        session, dep, hub = deployed_stack
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        # 模拟 0008 前的旧数据 + 已停止部署（restart 的前置态）
        session._store[ModelDeploymentReplica] = {}
        session._store[ModelDeploymentInstance] = {}
        for row in session._store.get(Instance, {}).values():
            if row.deployment_id != dep.deployment_id:
                continue
            row.model_deployment_instance_id = None
            spec = dict(row.spec or {})
            spec["engine_instance_id"] = ""
            row.spec = spec
            row.status = "STOPPED"

        d = await session.get(ModelDeployment, dep.deployment_id)
        d.status = "stopped"
        await orch.restart(dep.deployment_id)

        replicas = [
            r for r in session._store.get(ModelDeploymentReplica, {}).values() if r.deployment_id == dep.deployment_id
        ]
        engines = list(session._store.get(ModelDeploymentInstance, {}).values())
        assert len(replicas) == 1 and len(engines) == 1  # restart 路径同样补建两层行
        irows = [r for r in session._store.get(Instance, {}).values() if r.deployment_id == dep.deployment_id]
        assert irows and all(r.model_deployment_instance_id == engines[0].instance_id for r in irows)

    async def test_deploy_with_manual_gpu_selection(self, deployed_stack):
        """手动选卡：spec.gpu_selector.gpu_uuids 指定 g0 → 精确预留 g0（非自动 spread）。"""
        session, dep, hub = deployed_stack
        dep.spec = {
            "topology": {"tp": 1, "replicas": 1},
            "gpu_selector": {"gpu_uuids": ["g0"]},
            "model_path": "/data/models/Qwen2.5-0.5B",
        }
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        instances = list(session._store.get(Instance, {}).values())
        assert len(instances) == 1
        assert instances[0].assigned_node == "n1"
        # 手选精确落到 g0（自动 spread 会按 index 取，这里用 uuid 固定）
        assigns = [
            a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.deployment_id == dep.deployment_id
        ]
        assert len(assigns) == 1
        assert assigns[0].gpu_uuid == "g0"

    async def test_deploy_manual_selection_cross_node(self, deployed_stack_multinode):
        """跨机手动选卡：tp=2/nnodes=2，gpu_uuids 跨 n1/n2 各 1 → 两 instance 落不同节点。"""
        session, dep, hub = deployed_stack_multinode
        dep.spec = {
            "topology": {"tp": 2, "nnodes": 2, "replicas": 1},
            "gpu_selector": {"gpu_uuids": ["g0", "g2"]},  # g0@n1, g2@n2
            "model_path": "/data/models/Qwen2.5-0.5B",
        }
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        instances = list(session._store.get(Instance, {}).values())
        assert len(instances) == 2
        assert {i.assigned_node for i in instances} == {"n1", "n2"}
        assigns = {
            a.gpu_uuid
            for a in session._store.get(DeploymentGpuAssignment, {}).values()
            if a.deployment_id == dep.deployment_id
        }
        assert assigns == {"g0", "g2"}

    async def test_two_deployments_share_gpu_via_engine_flag(self, deployed_stack):
        """引擎显存 flag 共享：两部署同卡各 0.4 → 都落 g0（非互斥），allocated=0.4×total。"""
        session, dep, hub = deployed_stack
        spec = {
            "topology": {"tp": 1, "replicas": 1},
            "engine_params": {"extra_args": ["--gpu-memory-utilization", "0.4"]},
            "gpu_selector": {"gpu_uuids": ["g0"]},
            "model_path": "/data/models/Qwen2.5-0.5B",
        }
        dep.spec = spec
        dep_b = session.preload(
            ModelDeployment(
                deployment_id="dep-share-bbbb-2222",
                cluster_id=1,
                name="db",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="PENDING",
                spec=spec,
            )
        )
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)
        await orch.deploy(dep_b.deployment_id)

        g0_assigns = [a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.gpu_uuid == "g0"]
        assert len(g0_assigns) == 2  # 两部署共享 g0（引擎 flag 非互斥）
        for a in g0_assigns:
            assert a.gpu_memory_utilization == 0.4
            assert a.allocated_vram_gb == pytest.approx(0.4 * 11.0, abs=0.01)

    async def test_deploy_with_node_ids_selection(self, deployed_stack_multinode):
        """node_ids 跨机选节点：n1+n2 各自动分 1 卡（tp=2/nnodes=2）。"""
        session, dep, hub = deployed_stack_multinode
        dep.spec = {
            "topology": {"tp": 2, "nnodes": 2, "replicas": 1},
            "gpu_selector": {"node_ids": ["n1", "n2"]},
            "model_path": "/data/models/Qwen2.5-0.5B",
        }
        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        instances = list(session._store.get(Instance, {}).values())
        assert len(instances) == 2
        assert {i.assigned_node for i in instances} == {"n1", "n2"}


class TestReconcilerAdvance:
    """reconciler.advance_deployment：容器阶段 phase 逐步推进（Task 驱动 + 双门判定）。"""

    def _instance(self, session, dep, status="PENDING", spec_extra=None):
        """单副本单引擎四层预置：replica + engine(full) 行 + 容器行挂引擎。"""
        from llmpod.apps.manager.deployment.schemas import InstanceSpec
        from llmpod.models.model_deployment_instance import ModelDeploymentInstance
        from llmpod.models.model_deployment_replica import ModelDeploymentReplica

        engine_id = "eng-0001"
        session.preload(
            ModelDeploymentReplica(
                replica_id="rep-0001",
                deployment_id=dep.deployment_id,
                seq=0,
                status="provisioning",
            )
        )
        session.preload(
            ModelDeploymentInstance(
                instance_id=engine_id,
                replica_id="rep-0001",
                role="full",
                tp=1,
                pp=1,
                nnodes=1,
                status="provisioning",
            )
        )
        spec = InstanceSpec(
            instance_id="i1",
            engine_instance_id=engine_id,
            rank=0,
            role="full",
            assigned_node="n1",
            assigned_gpus=[0],
            master_addr="10.0.1.5",
            master_port=29501,
            port=8000,
            model_path="/data/models/Qwen2.5-0.5B",
            served_model_name="Qwen2.5-0.5B",
            **(spec_extra or {}),
        )
        return session.preload(
            Instance(
                instance_id="i1",
                deployment_id=dep.deployment_id,
                model_deployment_instance_id=engine_id,
                role="full",
                rank=0,
                assigned_node="n1",
                status=status,
                spec=spec.model_dump(),
            )
        )

    def _pull_task(self, session, dep, status, attempt=1):
        import hashlib

        image = "vllm/vllm-openai:v0.23.0"
        h = hashlib.sha256(image.encode()).hexdigest()[:16]
        return session.preload(
            Task(
                id=f"pull-{attempt}",
                resource_type="IMAGE_PULL",
                resource_key=f"image:n1:{h}",
                node_id="n1",
                type=TaskType.PULL_IMAGE,
                status=status,
                attempt=attempt,
                payload={"image": image},
            )
        )

    def _start_task(self, session, dep, status, attempt=1, last_error=None):
        return session.preload(
            Task(
                id=f"start-{attempt}",
                resource_type="INSTANCE",
                resource_key="instance:eng-0001:0",
                node_id="n1",
                type=TaskType.START_INSTANCE,
                status=status,
                attempt=attempt,
                payload={},
                last_error=last_error,
            )
        )

    def _set_phase(self, session, dep, phase):
        d = session._store[ModelDeployment][dep.deployment_id]
        d.phase = phase
        return d

    def _patch_dispatch(self, monkeypatch, ensures, cancels):
        from llmpod.apps.manager.deployment import task_ops

        async def _dispatch(task):
            ensures.append(task)

        async def _cancel(node_id, task_id):
            cancels.append((node_id, task_id))

        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)
        monkeypatch.setattr(task_ops, "dispatch_cancel_task", _cancel)

    async def test_pull_image_creates_task_and_waits(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep)
        self._set_phase(session, dep, "PULL_IMAGE")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures and ensures[0].type == TaskType.PULL_IMAGE
        assert ensures[0].payload == {"image": "vllm/vllm-openai:v0.23.0"}
        assert ensures[0].resource_key.startswith("image:n1:")
        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "PULL_IMAGE"  # task 未完成，不推进

    async def test_pull_image_resolves_driver_specific(self, deployed_stack, monkeypatch):
        """PULL 阶段按节点真实驱动解析镜像（R575→cu129），不拉一刀切默认。"""
        session, dep, hub = deployed_stack
        self._instance(
            session,
            dep,
            spec_extra={
                "gpu_arch": "nvidia",
                "target_gpu_model": "NVIDIA GeForce RTX 2080 Ti",
                "target_driver_version": "575.51",
            },
        )
        self._set_phase(session, dep, "PULL_IMAGE")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures and ensures[0].type == TaskType.PULL_IMAGE
        # R575 命中 cu129 阶梯条目（实测地板 >=575；580+ 才走 cu130 默认），非默认 v0.23.0
        assert ensures[0].payload == {"image": "vllm/vllm-openai:v0.27.1-cu129"}

    async def test_pull_success_advances_to_start_head(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep)
        self._set_phase(session, dep, "PULL_IMAGE")
        self._pull_task(session, dep, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures == []  # 已 SUCCESS 不重建 task
        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "START_HEAD"

    async def test_pull_over_cap_fails_deployment(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep)
        self._set_phase(session, dep, "PULL_IMAGE")
        for i in (1, 2, 3):
            self._pull_task(session, dep, "FAILED", attempt=i)
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "failed" and d.phase == "STOPPING"
        # 失败收敛只派发 STOP（清理残留容器），不派发 START
        assert not any(t.type == "START_INSTANCE" for t in ensures)

    async def test_start_head_creates_start_task(self, deployed_stack, monkeypatch):
        """START_HEAD 下发 head START task，并等容器 RUNNING 才切健康检查（时间线顺序）。"""
        session, dep, hub = deployed_stack
        irow = self._instance(session, dep)
        self._set_phase(session, dep, "START_HEAD")
        self._pull_task(session, dep, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures and ensures[0].type == TaskType.START_INSTANCE
        p = ensures[0].payload
        assert p["deployment_id"] == dep.deployment_id and p["instance_id"] == "i1"
        assert p["container_name"].startswith("llmpod-dep-aaaa")
        assert p["spec"]["image"] == "vllm/vllm-openai:v0.23.0"
        assert p["readiness"]["url"] == "http://10.0.1.5:8000/health"
        assert irow.status == "STARTING"
        d = await session.get(ModelDeployment, dep.deployment_id)
        # task 已下发但容器未起（RUNNING）→ 仍停留 START_HEAD（等容器启动，不切健康检查）
        assert d.phase == "START_HEAD"

        # 容器进程起来（RUNNING）→ 下一轮切 WAIT_HEAD_READY（健康检查）
        irow.status = "RUNNING"
        await advance_deployment(dep.deployment_id)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_HEAD_READY"

    async def test_wait_ready_by_task_success_dual_gate(self, deployed_stack, monkeypatch):
        """双门：START task SUCCESS（实例上报丢失的兜底）→ FINALIZING。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="STARTING")
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        self._start_task(session, dep, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "FINALIZING"
        assert (await session.get(Instance, "i1")).status == "READY"

    async def test_wait_ready_by_instance_status(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        self._start_task(session, dep, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "FINALIZING"

    async def test_wait_failure_retries_within_cap(self, deployed_stack, monkeypatch):
        """START FAILED attempt<3 → 实例回 PENDING + phase 回 START_HEAD（下轮重发）。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="STARTING")
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        self._start_task(session, dep, "FAILED", attempt=1, last_error="readiness 超时")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "START_HEAD" and d.status == "creating"
        assert (await session.get(Instance, "i1")).status == "PENDING"

    async def test_wait_failure_over_cap_fails(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="STARTING")
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        # 判耗尽只认末尾连续 FAILED 数（attempt 编号被手动 stop/start 抬高不算）：
        # 三条连续失败才到上限
        for n in (1, 2, 3):
            self._start_task(session, dep, "FAILED", attempt=n, last_error="readiness 超时" if n == 3 else None)
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "failed" and d.phase == "STOPPING"
        assert "readiness 超时" in (d.last_error or "")
        # VRAM 释放延迟到 controller 对 failed 部署的收敛（实例全 STOPPED 后），
        # 不在 fail_deployment 即时释放——故此处不再断言即时释放。

    async def test_finalize_produces_endpoint_and_running(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "running" and d.phase == "RUNNING"
        assert d.api_endpoint.startswith("http://10.0.1.5:8000")
        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        # 一协议一行：vLLM 生成类模型同时暴露 openai 面与 anthropic 面（同 api_base）
        assert len(endpoints) == 2
        by_protocol = {ep.protocol: ep for ep in endpoints}
        assert set(by_protocol) == {"openai", "anthropic"}
        assert all(ep.engine_type == "vllm" for ep in endpoints)
        assert all(ep.served_model == "Qwen2.5-0.5B" for ep in endpoints)
        assert all(ep.health_status == "healthy" for ep in endpoints)
        assert by_protocol["openai"].path == "chat/completions"  # 按 category=llm 派生（子路径，无版本根）
        assert by_protocol["anthropic"].path == "messages"
        assert by_protocol["anthropic"].api_base == by_protocol["openai"].api_base

    async def test_finalize_idempotent_no_duplicate_endpoint(self, deployed_stack, monkeypatch):
        """FINALIZING 重入（崩溃恢复）不重复建 endpoint——协议集对齐，仍两行。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)
        # 模拟崩溃后 phase 仍是 FINALIZING 重入
        d = await session.get(ModelDeployment, dep.deployment_id)
        d.phase = "FINALIZING"
        d.status = "creating"
        await advance_deployment(dep.deployment_id)

        endpoints = list(session._store.get(ModelEndpoint, {}).values())
        assert len(endpoints) == 2  # openai + anthropic 各一行，不因重入翻倍
        assert {ep.protocol for ep in endpoints} == {"openai", "anthropic"}

    async def test_finalize_auto_publishes_route_and_binding(self, deployed_stack, monkeypatch):
        """部署 READY 联动自动发布：默认路由(served_model)+每协议各一成员(weight=1)。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        routes = list(session._store.get(Route, {}).values())
        assert len(routes) == 1
        route = routes[0]
        assert route.name == "Qwen2.5-0.5B" and route.status == "published"
        endpoints = {ep.id: ep.protocol for ep in session._store.get(ModelEndpoint, {}).values()}
        bindings = list(session._store.get(RouteBinding, {}).values())
        assert len(bindings) == 2  # openai/anthropic 两端点都挂默认路由
        assert {endpoints[b.endpoint_id] for b in bindings} == {"openai", "anthropic"}
        assert all(b.route_id == route.id and b.weight == 1 for b in bindings)

    async def test_finalize_skips_route_when_auto_publish_false(self, deployed_stack, monkeypatch):
        """auto_publish=false：只部署不发布——端点仍建，路由/绑定不建。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        dep.spec["publish"] = {"auto_publish": False}
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "running"
        assert len(session._store.get(ModelEndpoint, {}).values()) == 2  # 端点仍建
        assert list(session._store.get(Route, {}).values()) == []  # 路由/绑定不建
        assert list(session._store.get(RouteBinding, {}).values()) == []

    async def test_finalize_publishes_custom_name_and_pricing(self, deployed_stack, monkeypatch):
        """publish.name 自定义对外名 + 定价/标签随路由带入。"""
        from decimal import Decimal

        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        dep.spec["publish"] = {"name": "my-alias", "input_price_per_mtok": 1.5, "tags": ["思考"]}
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        routes = list(session._store.get(Route, {}).values())
        assert len(routes) == 1
        route = routes[0]
        assert route.name == "my-alias"
        assert route.input_price_per_mtok == Decimal("1.5")
        assert route.tags == ["思考"]

    async def test_finalize_republishes_stopped_auto_route(self, deployed_stack, monkeypatch):
        """stop 下架过的 auto 路由在 restart/finalize 时恢复 published。"""
        session, dep, hub = deployed_stack
        self._instance(session, dep, status="READY")
        self._set_phase(session, dep, "FINALIZING")
        session.preload(Route(id=1, name="Qwen2.5-0.5B", status="unpublished", origin="auto"))
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        route = await session.get(Route, 1)
        assert route.status == "published"

    async def test_advance_noop_when_stopped(self, deployed_stack, monkeypatch):
        session, dep, hub = deployed_stack
        self._instance(session, dep)
        d = self._set_phase(session, dep, "WAIT_HEAD_READY")
        d.status = "stopped"
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_HEAD_READY"
        assert ensures == []

    async def test_fail_deployment_cancels_active_start_tasks(self, deployed_stack, monkeypatch):
        """fail_deployment 直调：活跃 START task 置 CANCELLED + 下发 cancel。"""
        from llmpod.apps.manager.deployment.reconciler import fail_deployment

        session, dep, hub = deployed_stack
        self._instance(session, dep, status="STARTING")
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        self._start_task(session, dep, "RUNNING", attempt=3)
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await fail_deployment(dep.deployment_id, error="直接失败（测试）")

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "failed" and d.phase == "STOPPING"
        assert cancels == [("n1", "start-3")]
        assert (await session.get(Task, "start-3")).status == "CANCELLED"


@pytest.fixture
def deployed_stack_multinode_rec(monkeypatch):
    """多机 reconciler 预置：nnodes=2 部署 + head(rank0/n1) + worker(rank1/n2) 实例行。"""
    session = FakeSession()
    model = session.preload(
        Model(
            id=1,
            name="Qwen2.5-0.5B",
            model_type="llm",
            size_gb=1.0,
            format="safetensors",
            source="local",
            source_url="/data/models/Qwen2.5-0.5B",
            source_kind="transformers_folder",
        )
    )
    dep = session.preload(
        ModelDeployment(
            deployment_id="dep-mn1111-2222",
            cluster_id=1,
            name="d-mn",
            model_id=model.id,
            engine="vllm",
            status="creating",
            phase="PENDING",
            spec={"topology": {"tp": 2, "nnodes": 2, "replicas": 1}},
        )
    )

    from llmpod.apps.manager.deployment.schemas import InstanceSpec

    def _mk_inst(iid, rank, node_rank, node, role):
        spec = InstanceSpec(
            instance_id=iid,
            engine_instance_id="eng-mn",
            rank=rank,
            role=role,
            replica_idx=0,
            nnodes=2,
            node_rank=node_rank,
            assigned_node=node,
            assigned_gpus=[0],
            master_addr="10.0.1.5",
            master_port=29501,
            port=8000,
            model_path="/data/models/Qwen2.5-0.5B",
            served_model_name="Qwen2.5-0.5B",
        )
        return session.preload(
            Instance(
                instance_id=iid,
                deployment_id=dep.deployment_id,
                model_deployment_instance_id="eng-mn",
                role=role,
                rank=rank,
                assigned_node=node,
                status="PENDING",
                spec=spec.model_dump(),
            )
        )

    head = _mk_inst("i-head", 0, 0, "n1", "head")
    worker = _mk_inst("i-worker", 1, 1, "n2", "worker")

    @asynccontextmanager
    async def _fake_db():
        yield session

    monkeypatch.setattr(rec_mod, "get_db_context", _fake_db)

    orch_mod._orchestrator = None
    return session, dep, head, worker


class TestReconcilerMultinode:
    """reconciler 多机：head 先起 → workers 随起（stable 判定）→ head /health 整集群门。"""

    def _set_phase(self, session, dep, phase):
        d = session._store[ModelDeployment][dep.deployment_id]
        d.phase = phase
        return d

    def _patch_dispatch(self, monkeypatch, ensures, cancels):
        from llmpod.apps.manager.deployment import task_ops

        async def _dispatch(task):
            ensures.append(task)

        async def _cancel(node_id, task_id):
            cancels.append((node_id, task_id))

        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)
        monkeypatch.setattr(task_ops, "dispatch_cancel_task", _cancel)

    def _start_task(self, session, dep, rank, status, attempt=1, last_error=None):
        return session.preload(
            Task(
                id=f"start-r{rank}-{attempt}",
                resource_type="INSTANCE",
                resource_key=f"instance:eng-mn:{rank}",
                node_id="n1",
                type=TaskType.START_INSTANCE,
                status=status,
                attempt=attempt,
                payload={},
                last_error=last_error,
            )
        )

    async def test_start_head_dispatches_head_only_goes_start_workers(self, deployed_stack_multinode_rec, monkeypatch):
        """START_HEAD 只发 head task；等 head 容器 RUNNING 后才 → START_WORKERS（worker 不动）。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        self._set_phase(session, dep, "START_HEAD")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures and ensures[0].payload["instance_id"] == "i-head"
        assert ensures[0].payload["rank"] == 0
        # head readiness = HTTP /health
        assert ensures[0].payload["readiness"]["url"] == "http://10.0.1.5:8000/health"
        assert head.status == "STARTING"
        assert worker.status == "PENDING"  # worker 未动
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_HEAD"  # 等 head RUNNING

        head.status = "RUNNING"
        await advance_deployment(dep.deployment_id)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_WORKERS"

    async def test_start_head_single_node_skips_workers_phase(self, deployed_stack, monkeypatch):
        """单节点（无 worker 行）：START_HEAD 等容器 RUNNING 后 → WAIT_HEAD_READY。"""
        session, dep, hub = deployed_stack
        from llmpod.apps.manager.deployment.schemas import InstanceSpec

        spec = InstanceSpec(
            instance_id="i1",
            rank=0,
            role="full",
            assigned_node="n1",
            assigned_gpus=[0],
            master_addr="10.0.1.5",
            master_port=29501,
            port=8000,
            model_path="/data/models/Qwen2.5-0.5B",
            served_model_name="Qwen2.5-0.5B",
        )
        irow = session.preload(
            Instance(
                instance_id="i1",
                deployment_id=dep.deployment_id,
                role="full",
                rank=0,
                assigned_node="n1",
                status="PENDING",
                spec=spec.model_dump(),
            )
        )
        self._set_phase(session, dep, "START_HEAD")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_HEAD"  # 等 RUNNING

        irow.status = "RUNNING"
        await advance_deployment(dep.deployment_id)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_HEAD_READY"

    async def test_start_workers_stable_readiness_payload(self, deployed_stack_multinode_rec, monkeypatch):
        """START_WORKERS 发 worker task（readiness=stable_running），等 worker RUNNING 后 → WAIT_WORKERS_READY。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "STARTING"
        self._set_phase(session, dep, "START_WORKERS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures and ensures[0].payload["instance_id"] == "i-worker"
        p = ensures[0].payload
        assert p["instance_id"] == "i-worker" and p["rank"] == 1
        assert p["readiness"]["mode"] == "stable_running"
        assert p["readiness"]["url"] is None  # typed dump：字段恒在，headless 无 url 为 None
        assert p["readiness"]["interval_sec"] == 4
        assert p["endpoint"] is None
        assert worker.status == "STARTING"
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_WORKERS"  # 等 worker RUNNING

        worker.status = "RUNNING"
        await advance_deployment(dep.deployment_id)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_WORKERS_READY"

    async def test_start_workers_no_workers_skips(self, deployed_stack_multinode_rec, monkeypatch):
        """START_WORKERS 无 worker 行 → 直接 WAIT_HEAD_READY（防御路径）。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        await session.delete(worker)
        self._set_phase(session, dep, "START_WORKERS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert ensures == []
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_HEAD_READY"

    async def test_wait_workers_ready_advances(self, deployed_stack_multinode_rec, monkeypatch):
        """worker READY（stable 判定达成）→ WAIT_HEAD_READY。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "STARTING"
        worker.status = "READY"
        self._set_phase(session, dep, "WAIT_WORKERS_READY")
        self._start_task(session, dep, 1, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "WAIT_HEAD_READY"

    async def test_wait_workers_retry_within_cap_back_to_start_workers(self, deployed_stack_multinode_rec, monkeypatch):
        """worker FAILED attempt<3 → 回 START_WORKERS（不回 START_HEAD）。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "STARTING"
        worker.status = "STARTING"
        self._set_phase(session, dep, "WAIT_WORKERS_READY")
        self._start_task(session, dep, 1, "FAILED", attempt=1, last_error="worker 崩了")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "START_WORKERS" and d.status == "creating"
        assert (await session.get(Instance, "i-worker")).status == "PENDING"

    async def test_wait_workers_over_cap_fails_deployment(self, deployed_stack_multinode_rec, monkeypatch):
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "STARTING"
        worker.status = "STARTING"
        self._set_phase(session, dep, "WAIT_WORKERS_READY")
        # 三条连续失败才到自动重试上限（末尾连续 FAILED 数，非 attempt 编号）
        for n in (1, 2, 3):
            self._start_task(session, dep, 1, "FAILED", attempt=n, last_error="worker 崩了" if n == 3 else None)
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.status == "failed" and d.phase == "STOPPING"

    async def test_wait_head_ready_gates_head_only(self, deployed_stack_multinode_rec, monkeypatch):
        """WAIT_HEAD_READY 只等 head（workers 已在 WAIT_WORKERS_READY 门过）。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "READY"
        worker.status = "READY"
        self._set_phase(session, dep, "WAIT_HEAD_READY")
        self._start_task(session, dep, 0, "SUCCESS")
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "FINALIZING"

    async def test_head_retry_does_not_redispatch_ready_workers(self, deployed_stack_multinode_rec, monkeypatch):
        """head FAILED 回 START_HEAD 重走：READY workers 被跳过（幂等短路），不重发。"""
        session, dep, head, worker = deployed_stack_multinode_rec
        head.status = "PENDING"
        worker.status = "READY"
        self._set_phase(session, dep, "START_HEAD")
        self._start_task(session, dep, 1, "SUCCESS")  # worker 已启动成功
        ensures, cancels = [], []
        self._patch_dispatch(monkeypatch, ensures, cancels)

        await advance_deployment(dep.deployment_id)

        # 只发 head task（worker READY 短路，不重发）；head 未 RUNNING → 仍停留 START_HEAD
        assert ensures and all(t.payload["instance_id"] == "i-head" for t in ensures)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_HEAD"

        head.status = "RUNNING"
        await advance_deployment(dep.deployment_id)
        assert (await session.get(ModelDeployment, dep.deployment_id)).phase == "START_WORKERS"


class TestDeployConcurrency:
    """deploy 进行中 stop/cancel 的并发竞态——cancel + status-aware 不覆盖终态。"""

    async def test_stop_during_deploy_keeps_stopped(self, deployed_stack, monkeypatch):
        """deploy 卡在 staging 轮询（node_model 未就绪）时 stop → 标 stopped+cancel；
        staging 的 _check_cancel 抛 cancel → 清理 VRAM，deploy 不覆盖成 failed/running。"""
        import asyncio as _aio
        from llmpod.apps.manager.deployment.placement import PlacementService

        session, dep, hub = deployed_stack
        nm = next(iter(session._store.get(NodeModel, {}).values()))
        nm.status = "downloading"

        async def _downloading(self, model_id, node_id):
            return "fake-task-id"

        monkeypatch.setattr(PlacementService, "ensure_present", _downloading)
        monkeypatch.setattr(orch_mod, "STAGING_POLL_INTERVAL", 0.0)

        orch = get_orchestrator()
        orch.start_deploy(dep.deployment_id)
        await _aio.sleep(0.05)

        d = await session.get(ModelDeployment, dep.deployment_id)
        d.status = "stopped"
        await orch.request_cancel(dep.deployment_id)
        await orch.await_deploy(dep.deployment_id, timeout=5.0)

        d2 = await session.get(ModelDeployment, dep.deployment_id)
        assert d2.status == "stopped"  # status-aware：未被覆盖成 failed/running
        assert d2.phase == "STAGING_MODEL"

    async def test_stale_present_not_confirmed_by_verify_task(self, deployed_stack, monkeypatch):
        """node_model 仍是上次的 present、本次复核任务未 SUCCESS → 不放行。

        复核任务发现配套文件缺失（如 mmproj 被删）时 node_model 会短暂仍是 present，
        据此放行会让缺权重的容器上线——staging 只认复核任务终态。
        """
        import asyncio as _aio

        session, dep, hub = deployed_stack
        task = next(iter(session._store.get(Task, {}).values()))
        task.status = "FAILED"  # 本次复核失败；node_model 保持陈旧的 present

        monkeypatch.setattr(orch_mod, "STAGING_POLL_INTERVAL", 0.0)

        orch = get_orchestrator()
        orch.start_deploy(dep.deployment_id)
        await _aio.sleep(0.05)

        d = await session.get(ModelDeployment, dep.deployment_id)
        assert d.phase == "STAGING_MODEL"  # 未交接容器阶段

        await orch.request_cancel(dep.deployment_id)
        await orch.await_deploy(dep.deployment_id, timeout=5.0)

    async def test_cancel_mid_deploy_releases_vram(self, deployed_stack, monkeypatch):
        """staging 期 cancel → 清理 VRAM + assignments + 标 failed（容器阶段不在协程内）。"""
        import asyncio as _aio
        from llmpod.apps.manager.deployment.placement import PlacementService
        from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment

        session, dep, hub = deployed_stack
        nm = next(iter(session._store.get(NodeModel, {}).values()))
        nm.status = "downloading"

        async def _downloading(self, model_id, node_id):
            return "fake-task-id"

        monkeypatch.setattr(PlacementService, "ensure_present", _downloading)
        monkeypatch.setattr(orch_mod, "STAGING_POLL_INTERVAL", 0.0)

        orch = get_orchestrator()
        orch.start_deploy(dep.deployment_id)
        await _aio.sleep(0.05)
        # _plan 已写 assignments（VRAM 已预留）
        assert session._store.get(DeploymentGpuAssignment, {})

        await orch.request_cancel(dep.deployment_id)
        await orch.await_deploy(dep.deployment_id, timeout=5.0)

        d2 = await session.get(ModelDeployment, dep.deployment_id)
        assert d2.status == "failed"  # cancel（非 stop）→ mark_failed_if_active
        assert not orch.ledger.get_reserved_for_deployment(dep.deployment_id)  # VRAM 释放
        assert not session._store.get(DeploymentGpuAssignment, {})  # assignments 清

    async def test_failed_deploy_deletes_assignments(self, deployed_stack, monkeypatch):
        """部署失败后 DeploymentGpuAssignment 行应被删（VRAM 账实不泄露）。"""
        from llmpod.apps.manager.deployment.placement import PlacementService

        session, dep, hub = deployed_stack
        session.preload(
            DeploymentGpuAssignment(
                deployment_id=dep.deployment_id,
                gpu_uuid="g0",
                instance_id=None,
                allocated_vram_gb=8.0,
                resource_type="nvidia",
            )
        )
        nm = next(iter(session._store.get(NodeModel, {}).values()))
        nm.status = "failed"
        nm.last_error = "超过自动重试上限 (3)"

        async def _over_cap(self, model_id, node_id):
            return None

        monkeypatch.setattr(PlacementService, "ensure_present", _over_cap)

        orch = get_orchestrator()
        await orch.deploy(dep.deployment_id)

        remaining = [
            a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.deployment_id == dep.deployment_id
        ]
        assert remaining == []


class TestRecoveryStartup:
    """manager 重启恢复——staging 阶段清空重跑、容器阶段保留续跑、running 保留。"""

    async def test_recover_clears_staging_keeps_running(self, monkeypatch):
        from llmpod.apps.manager.deployment.recovery import recover_on_startup
        from llmpod.models.model_deployment_instance import ModelDeploymentInstance
        from llmpod.models.model_deployment_replica import ModelDeploymentReplica

        session = FakeSession()
        session.preload(Node(id="n1", cluster_id=1, hostname="h", ip="1.1.1.1", status="online"))
        session.preload(
            Gpu(id=1, uuid="g1", node_id="n1", index=0, name="X", arch="nvidia", total_vram_gb=24.0, status="online")
        )
        # creating + STAGING_MODEL：清空重跑（重规划）
        session.preload(
            ModelDeployment(
                deployment_id="dep-c",
                cluster_id=1,
                name="c",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="STAGING_MODEL",
                spec={},
            )
        )
        session.preload(Instance(instance_id="i-c", deployment_id="dep-c", role="full", status="STARTING"))
        # 副本/引擎两层行也必须一起清：只清分片会让重驱动重建撞 (deployment_id, seq) 唯一约束
        session.preload(ModelDeploymentReplica(replica_id="rep-c", deployment_id="dep-c", seq=0, status="provisioning"))
        session.preload(
            ModelDeploymentInstance(
                instance_id="eng-c",
                replica_id="rep-c",
                role="full",
                tp=1,
                pp=1,
                ep=1,
                nnodes=1,
                status="provisioning",
            )
        )
        session.preload(
            DeploymentGpuAssignment(
                id=1, deployment_id="dep-c", gpu_uuid="g1", allocated_vram_gb=8.0, resource_type="nvidia"
            )
        )
        # running 部署（有 assignment，应保留）
        session.preload(
            ModelDeployment(
                deployment_id="dep-r", cluster_id=1, name="r", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        session.preload(
            DeploymentGpuAssignment(
                id=2, deployment_id="dep-r", gpu_uuid="g1", allocated_vram_gb=4.0, resource_type="nvidia"
            )
        )

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr("llmpod.apps.manager.deployment.recovery.get_db_context", _fake_db)
        monkeypatch.setattr("llmpod.db.get_db_context", _fake_db)

        inflight = await recover_on_startup()
        assert inflight == ["dep-c"]
        # staging 部署的 instance + assignment 被清，phase 重置 PENDING
        assert await session.get(Instance, "i-c") is None
        assert await session.get(ModelDeploymentReplica, "rep-c") is None
        assert await session.get(ModelDeploymentInstance, "eng-c") is None
        assert not [a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.deployment_id == "dep-c"]
        assert (await session.get(ModelDeployment, "dep-c")).phase == "PENDING"
        # running 部署的 assignment 保留（不会被误清）
        assert [a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.deployment_id == "dep-r"]
        # node→offline、gpu→stale
        assert list(session._store.get(Node, {}).values())[0].status == "offline"
        assert list(session._store.get(Gpu, {}).values())[0].status == "stale"

    async def test_recover_keeps_container_phase_for_resume(self, monkeypatch):
        """容器阶段（PULL_IMAGE..FINALIZING）的 creating 部署保留 Instance/assignment——
        reconciler 从 phase 续跑，不重新规划/预留（不重起健康容器）。"""
        from llmpod.apps.manager.deployment.recovery import recover_on_startup

        session = FakeSession()
        session.preload(
            ModelDeployment(
                deployment_id="dep-w",
                cluster_id=1,
                name="w",
                model_id=1,
                engine="vllm",
                status="creating",
                phase="WAIT_HEAD_READY",
                spec={},
            )
        )
        session.preload(Instance(instance_id="i-w", deployment_id="dep-w", role="full", status="STARTING"))
        session.preload(
            DeploymentGpuAssignment(
                id=1, deployment_id="dep-w", gpu_uuid="g1", allocated_vram_gb=8.0, resource_type="nvidia"
            )
        )

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr("llmpod.apps.manager.deployment.recovery.get_db_context", _fake_db)

        inflight = await recover_on_startup()
        assert inflight == []  # 不清空（不走重规划）
        assert await session.get(Instance, "i-w") is not None  # 保留供续跑
        assert (await session.get(ModelDeployment, "dep-w")).phase == "WAIT_HEAD_READY"
        assert [a for a in session._store.get(DeploymentGpuAssignment, {}).values() if a.deployment_id == "dep-w"]

    async def test_ledger_rebuild_from_db(self, monkeypatch):
        """ResourceLedger 从 DeploymentGpuAssignment（显存）+ Instance.spec（内存）重建。"""
        session = FakeSession()
        session.preload(
            DeploymentGpuAssignment(
                id=3, deployment_id="dep-r", gpu_uuid="g1", allocated_vram_gb=4.0, resource_type="nvidia"
            )
        )
        session.preload(
            DeploymentGpuAssignment(
                id=4, deployment_id="dep-r", gpu_uuid="g2", allocated_vram_gb=6.0, resource_type="nvidia"
            )
        )
        # 活跃部署的 CPU 实例：内存 claim 按节点求和
        session.preload(ModelDeployment(deployment_id="dep-r", status="running"))
        session.preload(
            Instance(instance_id="i-1", deployment_id="dep-r", assigned_node="node-A", spec={"ram_claim_gb": 12.0})
        )
        session.preload(
            Instance(instance_id="i-2", deployment_id="dep-r", assigned_node="node-A", spec={"ram_claim_gb": 8.0})
        )
        # 已停止部署的实例不参与重建（stop 只释放预留、不删 Instance 行）
        session.preload(ModelDeployment(deployment_id="dep-s", status="stopped"))
        session.preload(
            Instance(instance_id="i-3", deployment_id="dep-s", assigned_node="node-B", spec={"ram_claim_gb": 99.0})
        )

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr("llmpod.db.get_db_context", _fake_db)

        ledger = ResourceLedger()
        await ledger.rebuild_from_db()
        assert ledger.get_reserved_for_deployment("dep-r") == {"g1": 4.0, "g2": 6.0}
        assert ledger.get_ram_reserved_for_deployment("dep-r") == {"node-A": 20.0}
        assert ledger.get_ram_reserved_for_deployment("dep-s") == {}


class TestController:
    """DeploymentController 控制循环——按 status/phase 分流驱动 + inflight 防重入。"""

    async def test_cancel_joins_spawned_drive(self, monkeypatch):
        import asyncio
        from llmpod.apps.manager.deployment.controller import DeploymentController

        controller = DeploymentController(scan_interval=0.01, startup_grace=0)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def drive():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def scan_once():
            if not controller._active:
                controller._launch("dep-1", "drive", drive())

        monkeypatch.setattr(controller, "_scan_once", scan_once)
        runner = asyncio.create_task(controller.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert stopped.is_set()
        assert not controller._active

    async def test_scan_drives_staging_and_dedups_inflight(self, deployed_stack, monkeypatch):
        import asyncio as _aio
        from llmpod.apps.manager.deployment import controller as ctrl_mod
        from llmpod.apps.manager.deployment.controller import DeploymentController

        session, dep, hub = deployed_stack

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr(ctrl_mod, "get_db_context", _fake_db)

        driven: list[str] = []

        def _fake_start_deploy(did):
            driven.append(did)
            return _aio.ensure_future(_aio.sleep(100))

        orch = get_orchestrator()
        monkeypatch.setattr(orch, "start_deploy", _fake_start_deploy)

        ctrl = DeploymentController(startup_grace=0)
        await ctrl._scan_once()
        await ctrl._scan_once()  # inflight 应阻止重复驱动

        assert driven == [dep.deployment_id]  # 只驱动一次（inflight 防重入）

    async def test_scan_routes_container_phase_to_reconciler(self, deployed_stack, monkeypatch):
        """creating + 容器 phase → reconciler.advance_deployment（不走 start_deploy）。"""
        import asyncio as _aio
        from llmpod.apps.manager.deployment import controller as ctrl_mod
        from llmpod.apps.manager.deployment.controller import DeploymentController

        session, dep, hub = deployed_stack
        d = await session.get(ModelDeployment, dep.deployment_id)
        d.phase = "PULL_IMAGE"

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr(ctrl_mod, "get_db_context", _fake_db)

        advanced: list[str] = []

        async def _fake_advance(did):
            advanced.append(did)

        monkeypatch.setattr(ctrl_mod.reconciler, "advance_deployment", _fake_advance)
        started: list[str] = []
        orch = get_orchestrator()
        monkeypatch.setattr(orch, "start_deploy", lambda did: started.append(did))

        ctrl = DeploymentController(startup_grace=0)
        await ctrl._scan_once()
        await _aio.sleep(0)  # 让 create_task 调度执行

        assert advanced == [dep.deployment_id]
        assert started == []  # 容器阶段不走 start_deploy

    async def test_scan_routes_stopped_to_converge(self, deployed_stack, monkeypatch):
        """stopped 残留（路由崩溃窗口：容器未停）→ 兜底收敛（StopService stop-only，不 label 扫除——容器保留）。"""
        import asyncio as _aio
        from llmpod.apps.manager.deployment import controller as ctrl_mod
        from llmpod.apps.manager.deployment import stop as stop_mod
        from llmpod.apps.manager.deployment.controller import DeploymentController

        session, dep, hub = deployed_stack
        d = await session.get(ModelDeployment, dep.deployment_id)
        d.status = "stopped"
        # 容器未停（路由崩溃）→ 非稳态，兜底收敛须真跑 StopService
        session.preload(
            Instance(
                instance_id="i1",
                deployment_id=dep.deployment_id,
                role="full",
                rank=0,
                assigned_node="n1",
                status="READY",
            )
        )

        @asynccontextmanager
        async def _fake_db():
            yield session

        monkeypatch.setattr(ctrl_mod, "get_db_context", _fake_db)
        monkeypatch.setattr(stop_mod, "get_db_context", _fake_db)

        # 模拟 Node 立即执行 STOP 并上报 STOPPED（dispatch 桩）
        from llmpod.apps.manager.deployment import task_ops

        async def _dispatch(task):
            inst_id = (task.payload or {}).get("instance_id")
            inst = session._store.get(Instance, {}).get(inst_id)
            if inst is not None:
                inst.status = "STOPPED"

        async def _cancel(node_id, task_id):
            pass

        monkeypatch.setattr(task_ops, "dispatch_task", _dispatch)
        monkeypatch.setattr(task_ops, "dispatch_cancel_task", _cancel)

        ctrl = DeploymentController(startup_grace=0)
        await ctrl._scan_once()
        await _aio.sleep(0.2)

        proxy = hub.proxies["n1"]
        assert not any(c[0] == "list" for c in proxy.calls)  # stop 路径不 label 扫除——容器保留
        assert (await session.get(Instance, "i1")).status == "STOPPED"  # 收敛完成（stop-only）


class TestDeleteDeploymentCascade:
    """删除部署级联：除 instance/assignment/endpoint/log/deployment 外，连带清自动发布的 RouteBinding 与空 Route。"""

    async def test_cascade_cleans_binding_and_auto_published_route(self):
        from llmpod.services import deployment_service
        from llmpod.models.tenant_model_block import TenantModelBlock

        session = FakeSession()
        session.preload(Model(id=1, name="m1", model_type="llm"))
        session.preload(
            ModelDeployment(
                deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        session.preload(
            ModelEndpoint(
                id=10,
                deployment_id="dep-1",
                protocol="openai",
                served_model="m1",
                name="m1",
                api_base="http://x",
                engine_type="vllm",
            )
        )
        session.preload(Route(id=1, name="m1", status="published", origin="auto"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))
        session.preload(TenantModelBlock(id=1, tenant_id=2, route_id=1))

        await deployment_service.delete_deployment_cascade(session, "dep-1")

        assert await session.get(ModelDeployment, "dep-1") is None
        assert list(session._store.get(ModelEndpoint, {}).values()) == []
        assert list(session._store.get(RouteBinding, {}).values()) == []
        assert list(session._store.get(Route, {}).values()) == []  # 自动发布 route 变空 → 连带删除
        assert list(session._store.get(TenantModelBlock, {}).values()) == []  # 封禁引用一并清除

    async def test_cascade_keeps_route_with_other_bindings(self):
        from llmpod.services import deployment_service

        session = FakeSession()
        session.preload(Model(id=1, name="m1", model_type="llm"))
        session.preload(
            ModelDeployment(
                deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        # 两个端点：10 属于本部署、20 是外部端点（route 仍绑着它，删部署后不可删 route）
        session.preload(
            ModelEndpoint(
                id=10,
                deployment_id="dep-1",
                protocol="openai",
                served_model="m1",
                name="m1",
                api_base="http://x",
                engine_type="vllm",
            )
        )
        session.preload(
            ModelEndpoint(
                id=20,
                deployment_id=None,
                protocol="openai",
                served_model="ext",
                name="ext",
                api_base="http://ext",
                engine_type="vllm",
            )
        )
        session.preload(Route(id=1, name="m1", status="published", origin="auto"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))
        session.preload(RouteBinding(id=2, route_id=1, endpoint_id=20, weight=1, priority=0, enabled=True))

        await deployment_service.delete_deployment_cascade(session, "dep-1")

        assert await session.get(ModelDeployment, "dep-1") is None
        assert list(session._store.get(ModelEndpoint, {}).values()) == [
            await session.get(ModelEndpoint, 20)
        ]  # 只删本部署端点
        bindings = list(session._store.get(RouteBinding, {}).values())
        assert [b.endpoint_id for b in bindings] == [20]  # 只删悬空 binding
        assert [r.id for r in session._store.get(Route, {}).values()] == [1]  # route 仍被外部端点绑定，保留

    async def test_cascade_keeps_manual_route_when_empty(self):
        from llmpod.services import deployment_service

        session = FakeSession()
        session.preload(Model(id=1, name="m1", model_type="llm"))
        session.preload(
            ModelDeployment(
                deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm", status="running", spec={}
            )
        )
        session.preload(
            ModelEndpoint(
                id=10,
                deployment_id="dep-1",
                protocol="openai",
                served_model="m1",
                name="m1",
                api_base="http://x",
                engine_type="vllm",
            )
        )
        # 手工建 route（origin=manual）仅绑本部署端点：删部署后 binding 悬空，但 route 是手工的 → 保留
        session.preload(Route(id=1, name="m1", status="published", origin="manual"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))

        await deployment_service.delete_deployment_cascade(session, "dep-1")

        assert await session.get(ModelDeployment, "dep-1") is None
        assert list(session._store.get(RouteBinding, {}).values()) == []  # binding 仍清
        assert [r.id for r in session._store.get(Route, {}).values()] == [1]  # 手工 route 保留
