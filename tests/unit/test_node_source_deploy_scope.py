"""节点目录模型的部署限制测试。

覆盖服务层硬限制（绑定节点、单机、显式选择校验、自动调度锁定到绑定节点）与路由层的
创建/重启拦截。使用 FakeSession，不连接真实 PG/Redis。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from llmpod.app import app
from _fakes import FakeSession

from llmpod.apps.manager.deployment.schemas import DeploymentConfig
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import Conflict, LLMPodValidationError
from llmpod.db import get_db
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node_model import NodeModel
from llmpod.services import deployment_service


def _node_model(*, present: bool = True) -> Model:
    return Model(id=1, name="Qwen", model_type="llm", source="node", node_id="n1", format="safetensors")


def _copy() -> NodeModel:
    return NodeModel(model_id=1, node_id="n1", desired_state="present", status="present")


def _gpu(uuid: str, node_id: str) -> Gpu:
    return Gpu(uuid=uuid, node_id=node_id, index=0, name="A100", arch="nvidia", total_vram_gb=80.0)


class TestEnforceNodeSourceSpec:
    async def test_auto_pins_gpu_deploy_to_bound_node(self):
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(topology={"tp": 1})

        await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

        assert spec.gpu_selector is not None
        assert spec.gpu_selector.node_id == "n1"

    async def test_auto_pins_cpu_replicas_to_bound_node(self):
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(offload_preference="cpu_only", topology={"tp": 1, "replicas": 2})

        await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

        assert spec.gpu_selector is not None
        assert spec.gpu_selector.node_ids == ["n1", "n1"]

    @pytest.mark.parametrize(
        "selector",
        [
            {"gpu_uuids": []},
            {"node_ids": []},
            {},
            {"node_id": ""},
        ],
    )
    async def test_empty_selectors_pinned_to_bound_node(self, selector):
        """空选择器不是「已指定」——必须钉到绑定节点（真机漏洞的复现点）。"""
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector=selector)

        injected = await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

        assert injected is True
        assert spec.gpu_selector is not None
        assert spec.gpu_selector.node_id == "n1"

    async def test_explicit_bound_node_kept(self):
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"node_id": "n1"})

        injected = await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

        assert injected is False
        assert spec.gpu_selector.node_id == "n1"

    async def test_explicit_other_node_rejected(self):
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"node_id": "n2"})
        with pytest.raises(LLMPodValidationError, match="绑定节点"):
            await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

    async def test_explicit_other_gpu_rejected(self):
        session = FakeSession()
        session.preload(_copy())
        session.preload(_gpu("g2", "n2"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"gpu_uuids": ["g2"]})
        with pytest.raises(LLMPodValidationError, match="绑定节点"):
            await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

    async def test_multi_node_rejected(self):
        session = FakeSession()
        session.preload(_copy())
        spec = DeploymentConfig(topology={"tp": 1, "nnodes": 2})
        with pytest.raises(LLMPodValidationError, match="单台节点"):
            await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

    async def test_missing_present_copy_rejected(self):
        session = FakeSession()
        spec = DeploymentConfig(topology={"tp": 1})
        with pytest.raises(Conflict):
            await deployment_service.enforce_node_source_spec(session, _node_model(), spec)

    async def test_non_node_source_unaffected(self):
        session = FakeSession()
        spec = DeploymentConfig(topology={"tp": 1})
        model = Model(id=1, name="Qwen", model_type="llm", source="huggingface")
        await deployment_service.enforce_node_source_spec(session, model, spec)
        assert spec.gpu_selector is None


def _override_db(session: FakeSession) -> None:
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db


def _override_user() -> None:
    def _admin() -> SessionData:
        return SessionData(
            user_id=1,
            username="tester",
            tenant_id=0,
            access_domain="platform",
            role_codes=["super_admin"],
            role_ids=[1],
            is_superuser=True,
        )

    app.dependency_overrides[get_current_user] = _admin


class TestDeploymentRouteGuard:
    async def test_create_with_other_node_rejected(self, client: AsyncClient):
        session = FakeSession()
        session.preload(_node_model())
        session.preload(_copy())
        _override_db(session)
        _override_user()

        resp = await client.post(
            "/api/v1/admin/model-deployments",
            json={
                "cluster_id": 1,
                "name": "dep",
                "model_id": 1,
                "engine": "vllm",
                "spec": {"topology": {"tp": 1}, "gpu_selector": {"node_id": "n2"}},
            },
        )
        assert resp.status_code == 422, resp.text


class TestInspectScope:
    """预检阶段：绑定节点集群锁定、显式越界判不可部署、可见池收缩。"""

    def _state(self, **request_kwargs):
        from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
        from llmpod.apps.webserver.services.deployment_inspect import InspectState

        request = InspectDeploymentRequest(model_id=1, **request_kwargs)
        return InspectState(request=request, model=_node_model())

    async def test_locks_cluster_to_bound_node(self):
        from llmpod.models.node import Node
        from llmpod.apps.webserver.services.deployment_inspect import _scope_node_source

        session = FakeSession()
        session.preload(Node(id="n1", cluster_id=2, hostname="h", ip="1.1.1.1", status="online"))
        session.preload(_copy())
        state = self._state()

        await _scope_node_source(session, state)

        assert state.request.cluster_id == 2
        assert state.deployable is True

    async def test_explicit_other_node_marks_undeployable(self):
        from llmpod.models.node import Node
        from llmpod.apps.webserver.services.deployment_inspect import _scope_node_source

        session = FakeSession()
        session.preload(Node(id="n1", cluster_id=2, hostname="h", ip="1.1.1.1", status="online"))
        state = self._state(gpu_selector={"node_id": "n2"})

        await _scope_node_source(session, state)

        assert state.deployable is False
        assert any("绑定节点" in issue for issue in state.issues)

    async def test_pool_restricted_to_bound_node(self):
        from types import SimpleNamespace

        from llmpod.apps.webserver.services.deployment_inspect import _restrict_pool_to_bound_node

        state = self._state()
        state.node_rows = [SimpleNamespace(id="n1"), SimpleNamespace(id="n2")]
        state.cluster_gpus = [
            SimpleNamespace(uuid="g1", node_id="n1"),
            SimpleNamespace(uuid="g2", node_id="n2"),
        ]
        state.node_resources = [
            SimpleNamespace(node_id="n1"),
            SimpleNamespace(node_id="n2"),
        ]

        _restrict_pool_to_bound_node(state)

        assert [n.id for n in state.node_rows] == ["n1"]
        assert [g.uuid for g in state.cluster_gpus] == ["g1"]
        assert [r.node_id for r in state.node_resources] == ["n1"]
