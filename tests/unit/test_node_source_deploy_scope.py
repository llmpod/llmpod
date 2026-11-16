"""节点目录模型的部署限制测试。

覆盖服务层硬限制（副本节点集合非空、单机、显式选择校验、不注入选择器）与路由层的
创建拦截。使用 FakeSession，不连接真实 PG/Redis。
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


def _copy(node_id: str = "n1", *, present: bool = True) -> NodeModel:
    return NodeModel(
        model_id=1, node_id=node_id, desired_state="present", status="present" if present else "failed"
    )


def _gpu(uuid: str, node_id: str) -> Gpu:
    return Gpu(uuid=uuid, node_id=node_id, index=0, name="A100", arch="nvidia", total_vram_gb=80.0)


class TestValidateNodeSourceSpec:
    """副本集合语义：显式选择校验集合成员，空选择器不再注入节点。"""

    async def test_returns_copy_set_without_injecting_selector(self):
        session = FakeSession()
        session.preload(_copy("n1"))
        session.preload(_copy("n2"))
        spec = DeploymentConfig(topology={"tp": 1})

        copy_nodes = await deployment_service.validate_node_source_spec(session, _node_model(), spec)

        assert copy_nodes == {"n1", "n2"}
        assert spec.gpu_selector is None  # 不注入：调度池由调用方按集合收缩

    async def test_empty_selectors_left_untouched(self, ):
        """空选择器不是「已指定」——保持自动调度，调度池收缩保证不越界。"""
        session = FakeSession()
        session.preload(_copy("n1"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={})

        copy_nodes = await deployment_service.validate_node_source_spec(session, _node_model(), spec)

        assert copy_nodes == {"n1"}
        assert not any(
            (spec.gpu_selector.node_id, spec.gpu_selector.gpu_uuids, spec.gpu_selector.node_ids)
        )

    async def test_explicit_copy_node_kept(self):
        session = FakeSession()
        session.preload(_copy("n1"))
        session.preload(_copy("n2"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"node_id": "n2"})

        copy_nodes = await deployment_service.validate_node_source_spec(session, _node_model(), spec)

        assert copy_nodes == {"n1", "n2"}
        assert spec.gpu_selector.node_id == "n2"

    async def test_explicit_other_node_rejected(self):
        session = FakeSession()
        session.preload(_copy("n1"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"node_id": "n2"})
        with pytest.raises(LLMPodValidationError, match="副本在节点：n1"):
            await deployment_service.validate_node_source_spec(session, _node_model(), spec)

    async def test_explicit_gpu_on_other_node_rejected(self):
        session = FakeSession()
        session.preload(_copy("n1"))
        session.preload(_gpu("g2", "n2"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"gpu_uuids": ["g2"]})
        with pytest.raises(LLMPodValidationError, match="不在持有副本的节点"):
            await deployment_service.validate_node_source_spec(session, _node_model(), spec)

    async def test_gpu_on_second_copy_node_accepted(self):
        """多副本：第二台副本节点上的卡可选——部署可在任一副本节点。"""
        session = FakeSession()
        session.preload(_copy("n1"))
        session.preload(_copy("n2"))
        session.preload(_gpu("g2", "n2"))
        spec = DeploymentConfig(topology={"tp": 1}, gpu_selector={"gpu_uuids": ["g2"]})

        copy_nodes = await deployment_service.validate_node_source_spec(session, _node_model(), spec)

        assert copy_nodes == {"n1", "n2"}

    async def test_multi_node_rejected(self):
        session = FakeSession()
        session.preload(_copy("n1"))
        session.preload(_copy("n2"))
        spec = DeploymentConfig(topology={"tp": 1, "nnodes": 2})
        with pytest.raises(LLMPodValidationError, match="单台节点"):
            await deployment_service.validate_node_source_spec(session, _node_model(), spec)

    async def test_no_present_copy_rejected(self):
        """副本行全都不是 present（failed/absent）= 没有可用副本，拒绝。"""
        session = FakeSession()
        session.preload(_copy("n1", present=False))
        spec = DeploymentConfig(topology={"tp": 1})
        with pytest.raises(Conflict):
            await deployment_service.validate_node_source_spec(session, _node_model(), spec)

    async def test_non_node_source_returns_none(self):
        session = FakeSession()
        spec = DeploymentConfig(topology={"tp": 1})
        model = Model(id=1, name="Qwen", model_type="llm", source="huggingface")
        assert await deployment_service.validate_node_source_spec(session, model, spec) is None
        assert spec.gpu_selector is None


class TestRequireModelCopyPresent:
    async def test_all_copies_gone_blocks_restart(self):
        session = FakeSession()
        session.preload(_copy("n1", present=False))
        with pytest.raises(Conflict):
            await deployment_service.require_model_copy_present(session, _node_model())

    async def test_non_node_source_untouched(self):
        session = FakeSession()
        model = Model(id=1, name="Qwen", model_type="llm", source="huggingface")
        await deployment_service.require_model_copy_present(session, model)  # 不抛


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
        session.preload(_copy("n1"))
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
    """预检阶段：副本节点集合装配、集群锁定、显式越界判不可部署、可见池收缩。"""

    def _state(self, **request_kwargs):
        from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
        from llmpod.apps.webserver.services.deployment_inspect import InspectState

        request = InspectDeploymentRequest(model_id=1, **request_kwargs)
        return InspectState(request=request, model=_node_model())

    async def test_assembles_copy_nodes_and_locks_unique_cluster(self):
        from llmpod.models.node import Node
        from llmpod.apps.webserver.services.deployment_inspect import _scope_node_source

        session = FakeSession()
        session.preload(Node(id="n1", cluster_id=2, hostname="h", ip="1.1.1.1", status="online"))
        session.preload(Node(id="n2", cluster_id=2, hostname="h2", ip="1.1.1.2", status="online"))
        session.preload(_copy("n1"))
        session.preload(_copy("n2"))
        state = self._state()

        await _scope_node_source(session, state)

        assert state.copy_nodes == {"n1", "n2"}
        assert state.copy_cluster_ids == {2}
        assert state.request.cluster_id == 2  # 唯一副本集群 → 锁定
        assert state.deployable is True

    async def test_no_present_copy_marks_undeployable(self):
        from llmpod.apps.webserver.services.deployment_inspect import _scope_node_source

        session = FakeSession()
        session.preload(_copy("n1", present=False))
        state = self._state()

        await _scope_node_source(session, state)

        assert state.deployable is False
        assert any("没有节点持有此模型的可用副本" in issue for issue in state.issues)

    async def test_explicit_other_node_marks_undeployable(self):
        from llmpod.apps.webserver.services.deployment_inspect import _scope_node_source

        session = FakeSession()
        session.preload(_copy("n1"))
        state = self._state(gpu_selector={"node_id": "n2"})

        await _scope_node_source(session, state)

        assert state.deployable is False
        assert any("副本" in issue for issue in state.issues)

    async def test_pool_restricted_to_copy_nodes(self):
        from types import SimpleNamespace

        from llmpod.apps.webserver.services.deployment_inspect import _restrict_pool_to_copy_nodes

        state = self._state()
        state.copy_nodes = {"n1"}
        state.node_rows = [SimpleNamespace(id="n1"), SimpleNamespace(id="n2")]
        state.cluster_gpus = [
            SimpleNamespace(uuid="g1", node_id="n1"),
            SimpleNamespace(uuid="g2", node_id="n2"),
        ]
        state.node_resources = [
            SimpleNamespace(node_id="n1"),
            SimpleNamespace(node_id="n2"),
        ]

        _restrict_pool_to_copy_nodes(state)

        assert [n.id for n in state.node_rows] == ["n1"]
        assert [g.uuid for g in state.cluster_gpus] == ["g1"]
        assert [r.node_id for r in state.node_resources] == ["n1"]
