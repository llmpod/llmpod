"""dispatch.container_logs 编排逻辑单测（不起 HTTP、不建 RpcChannel）。

验证：查 instance 行 → 拼容器名（deployment_container_name）→ 经 hub.proxy 逐节点调
ContainerService.logs → 汇总 [{instance_id, node_id, container_name, status, logs, error}]。
失败隔离：单节点 RPC 异常只落入对应 instance 的 error，其余照常返回；
查不到部署行 → NotFound；无 instance → 空列表。
"""

from __future__ import annotations

import pytest

from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment
from _fakes import FakeSession


def _inst(id, dep, node, status="READY", rank=0):
    return Instance(
        instance_id=id,
        deployment_id=dep,
        role="full",
        rank=rank,
        assigned_node=node,
        status=status,
        spec={},
    )


class _StubProxy:
    """ContainerService 桩：按容器名预置返回字符串或异常。"""

    def __init__(self, results: dict):
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def logs(self, name: str, tail: int = 200) -> str:
        self.calls.append((name, tail))
        r = self._results[name]
        if isinstance(r, Exception):
            raise r
        return r


class _StubHub:
    """dispatch._get_hub() 的替身：proxy(node, ContainerService) → 桩（get 走代理表）。"""

    def __init__(self, proxies: dict[str, _StubProxy]):
        self._proxies = proxies

    def get(self, node_id):
        return object() if node_id in self._proxies else None

    def proxy(self, node_id, service):
        return self._proxies[node_id]


@pytest.fixture
def patch_hub(monkeypatch):
    def _patch(hub):
        import llmpod.apps.manager.dispatch as dispatch_mod

        monkeypatch.setattr(dispatch_mod, "_get_hub", lambda: hub)
        return dispatch_mod

    return _patch


class TestContainerLogsDispatch:
    async def test_fans_out_and_collects(self, patch_hub):
        dep = ModelDeployment(deployment_id="dep-1", cluster_id=1, name="d", model_id=1, engine="vllm")
        session = FakeSession()
        session.preload(dep)
        session.preload(_inst("inst-1", "dep-1", "node-a", status="CRASHED"))
        session.preload(_inst("inst-2", "dep-1", "node-b", status="READY"))

        proxy_a = _StubProxy({"llmpod-dep-1-inst-1": "boom traceback"})
        proxy_b = _StubProxy({"llmpod-dep-1-inst-2": "INFO ready"})
        mod = patch_hub(_StubHub({"node-a": proxy_a, "node-b": proxy_b}))

        items = await mod.container_logs(session, "dep-1", tail=50)

        assert [i["instance_id"] for i in items] == ["inst-1", "inst-2"]
        assert items[0]["node_id"] == "node-a"
        assert items[0]["container_name"] == "llmpod-dep-1-inst-1"
        assert items[0]["status"] == "CRASHED"
        assert items[0]["logs"] == "boom traceback"
        assert items[0]["error"] is None
        assert items[1]["logs"] == "INFO ready"
        # tail 透传到 RPC
        assert proxy_a.calls == [("llmpod-dep-1-inst-1", 50)]
        assert proxy_b.calls == [("llmpod-dep-1-inst-2", 50)]

    async def test_rpc_failure_isolated_per_instance(self, patch_hub):
        dep = ModelDeployment(deployment_id="dep-2", cluster_id=1, name="d", model_id=1, engine="vllm")
        session = FakeSession()
        session.preload(dep)
        session.preload(_inst("inst-1", "dep-2", "node-a"))
        session.preload(_inst("inst-2", "dep-2", "node-b"))

        proxy_a = _StubProxy({"llmpod-dep-2-inst-1": RuntimeError("node offline")})
        proxy_b = _StubProxy({"llmpod-dep-2-inst-2": "ok"})
        mod = patch_hub(_StubHub({"node-a": proxy_a, "node-b": proxy_b}))

        items = await mod.container_logs(session, "dep-2")

        assert items[0]["error"] is not None
        assert "node offline" in items[0]["error"]
        assert items[0]["logs"] is None
        assert items[1]["error"] is None
        assert items[1]["logs"] == "ok"

    async def test_missing_deployment_raises_404(self, patch_hub):
        mod = patch_hub(_StubHub({}))
        with pytest.raises(Exception) as ei:
            await mod.container_logs(FakeSession(), "nope")
        assert "404" in str(ei.value) or "NotFound" in type(ei.value).__name__

    async def test_no_instances_returns_empty(self, patch_hub):
        dep = ModelDeployment(deployment_id="dep-3", cluster_id=1, name="d", model_id=1, engine="vllm")
        session = FakeSession()
        session.preload(dep)
        mod = patch_hub(_StubHub({}))
        assert await mod.container_logs(session, "dep-3") == []
