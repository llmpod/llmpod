"""create 部署手选占用预检集成测试：被占且剩余不足 → 409 conflicts 明细；引擎显存 flag 共址不误拒。

遵循「stub get_db + 鉴权覆盖」：本测试用可种子化的 FakeSession 覆盖 get_db，
走真实路由代码（adapter/validate_topology/占用预检），不 stub service 函数。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user, get_db
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment


def _admin_user() -> SessionData:
    return admin_session()


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

    def all(self, cls):
        return list(self._store.get(cls, {}).values())

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


def _seed(session, gpu_total=22.0, occupied_gb=20.0):
    session.preload(
        Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=1.0,
        )
    )
    session.preload(
        Gpu(
            id=1,
            uuid="g1",
            node_id="n1",
            index=0,
            name="A100",
            arch="nvidia",
            total_vram_gb=gpu_total,
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
            status="running",
            phase="RUNNING",
            spec={},
        )
    )
    session.preload(
        DeploymentGpuAssignment(
            id=1,
            deployment_id="dep-other",
            gpu_uuid="g1",
            allocated_vram_gb=occupied_gb,
            gpu_memory_utilization=0.9,
        )
    )


class _Ctrl:
    def wake(self):
        pass


async def _fake_create(db, **kw):
    """返回带时间戳的部署行（FakeSession 不跑 server_default，response 序列化需要）。"""
    import datetime

    return ModelDeployment(
        deployment_id="dep-new",
        cluster_id=1,
        name="d",
        model_id=1,
        engine="vllm",
        status="creating",
        phase="PENDING",
        spec={},
        version=1,
        crash_count=0,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
    )


class TestCreateOccupancyPrecheck:
    async def test_cann_manual_selection_pp_rejected_422(self, client: AsyncClient, monkeypatch):
        """手选 cann 卡 + pp>1：create 同步 422（topology_conflict 判据，与编排器 _plan
        同源）——不是落库成 creating 再异步 failed 把原因埋进 last_error。"""
        app.dependency_overrides[get_current_user] = _admin_user
        session = FakeSession()
        session.preload(Model(id=1, name="m", model_type="llm", size_gb=1.0))
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

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield session

        app.dependency_overrides[get_db] = _fake_db
        try:
            r = await client.post(
                "/api/v1/admin/model-deployments",
                json={
                    "cluster_id": 1,
                    "name": "d",
                    "model_id": 1,
                    "engine": "vllm",
                    "force": True,  # force 也不放行：cann pp 是结构性硬拒
                    "spec": {"topology": {"tp": 1, "pp": 2}, "gpu_selector": {"gpu_uuids": ["c1", "c2"]}},
                },
            )
            assert r.status_code == 422
            assert "流水线并行" in r.json()["message"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    async def test_occupied_insufficient_409(self, client: AsyncClient, monkeypatch):
        """手选被占卡且剩余不足 → 409，data.conflicts 携带明细。"""
        app.dependency_overrides[get_current_user] = _admin_user
        session = FakeSession()
        _seed(session, gpu_total=22.0, occupied_gb=20.0)  # remaining 19.8-20 = -0.2 < 需求

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield session

        app.dependency_overrides[get_db] = _fake_db
        try:
            r = await client.post(
                "/api/v1/admin/model-deployments",
                json={
                    "cluster_id": 1,
                    "name": "d",
                    "model_id": 1,
                    "engine": "vllm",
                    "force": True,  # 跳过 feasibility，聚焦占用预检
                    "spec": {"topology": {"tp": 1}, "gpu_selector": {"gpu_uuids": ["g1"]}},
                },
            )
            assert r.status_code == 409
            body = r.json()
            assert body["code"] == 409
            c = body["data"]["conflicts"][0]
            assert c["gpu_uuid"] == "g1"
            assert c["deployment_name"] == "busy"
            assert c["occupied_pct"] == pytest.approx(20.0 / 22.0, abs=1e-3)
            assert c["remaining_gb"] < 0
            assert c["occupied_by"][0]["deployment_id"] == "dep-other"
        finally:
            app.dependency_overrides.pop(get_db, None)

    async def test_engine_flag_colocation_allowed(self, client: AsyncClient, monkeypatch):
        """引擎显存 flag 共址：需求 = 占比×total（非 estimate 值），剩余装得下 → 不误拒（200）。"""
        app.dependency_overrides[get_current_user] = _admin_user
        session = FakeSession()
        # total=100，他部署声明 50 → remaining 90-50=40 ≥ 占比×total=30 → 放行
        _seed(session, gpu_total=100.0, occupied_gb=50.0)

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield session

        app.dependency_overrides[get_db] = _fake_db
        monkeypatch.setattr("llmpod.apps.manager.deployment.controller.get_controller", lambda: _Ctrl())
        monkeypatch.setattr("llmpod.services.deployment_service.create_deployment", _fake_create)
        try:
            r = await client.post(
                "/api/v1/admin/model-deployments",
                json={
                    "cluster_id": 1,
                    "name": "d",
                    "model_id": 1,
                    "engine": "vllm",
                    "force": True,
                    "spec": {
                        "topology": {"tp": 1},
                        "engine_params": {"extra_args": ["--gpu-memory-utilization", "0.3"]},
                        "gpu_selector": {"gpu_uuids": ["g1"]},
                    },
                },
            )
            assert r.status_code == 200
            assert r.json()["data"]["deployment_id"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    async def test_stopped_occupier_not_blocking(self, client: AsyncClient, monkeypatch):
        """声明占用的部署已 stopped → 放行（status-aware，残留 assignment 不阻拦）。"""
        app.dependency_overrides[get_current_user] = _admin_user
        session = FakeSession()
        _seed(session, gpu_total=22.0, occupied_gb=20.0)
        # 把占用方改成 stopped（残留 assignment 行仍在——崩溃窗口语义）
        session.all(ModelDeployment)[0].status = "stopped"

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield session

        app.dependency_overrides[get_db] = _fake_db
        monkeypatch.setattr("llmpod.apps.manager.deployment.controller.get_controller", lambda: _Ctrl())
        monkeypatch.setattr("llmpod.services.deployment_service.create_deployment", _fake_create)
        try:
            r = await client.post(
                "/api/v1/admin/model-deployments",
                json={
                    "cluster_id": 1,
                    "name": "d",
                    "model_id": 1,
                    "engine": "vllm",
                    "force": True,
                    "spec": {"topology": {"tp": 1}, "gpu_selector": {"gpu_uuids": ["g1"]}},
                },
            )
            assert r.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)
