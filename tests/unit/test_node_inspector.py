"""节点目录探测器单测：wire 探测结果 → 与 local 同口径的 ``ModelFiles``。

被测缝是 ``NodeInspector.inspect``；节点 RPC 用 monkeypatch 替换，验证 manager 侧的
权重过滤、识别装配与软失败语义，不连接真实节点。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.model_source.node import NodeInspector
from llmpod.core.exceptions import LLMPodError
from llmpod.tunnel.schemas import ModelDirEntry, ModelDirProbe


def _probe(**overrides) -> ModelDirProbe:
    base = dict(
        exists=True,
        is_dir=True,
        config_name="config.json",
        config={"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"]},
        files=[
            ModelDirEntry(path="config.json", size=60),
            ModelDirEntry(path="model.safetensors", size=15 * 1024**3),
        ],
        total_bytes=100,
    )
    base.update(overrides)
    return ModelDirProbe(**base)


@pytest.fixture
def patch_probe(monkeypatch):
    from llmpod.apps.manager import dispatch

    async def _install(value):
        async def _call(node_id, path):
            return value

        monkeypatch.setattr(dispatch, "probe_model_dir", _call)
        return value

    return _install


class TestNodeInspector:
    async def test_transformer_dir_matches_local_semantics(self, patch_probe):
        await patch_probe(_probe())
        files = await NodeInspector("n1").inspect(source_url="/data/models/qwen")

        assert files.success is True
        assert files.format == "safetensors"
        assert files.structure == "directory"
        assert files.architecture == "Qwen2ForCausalLM"
        assert files.source_kind == "transformers_folder"
        assert files.size_gb == 15.0

    async def test_non_weight_files_excluded_from_size(self, patch_probe):
        await patch_probe(_probe())
        files = await NodeInspector("n1").inspect(source_url="/data/models/qwen")
        assert files.size_gb == 15.0  # config.json 不计入权重体积

    async def test_dir_without_weights_is_soft_failure(self, patch_probe):
        await patch_probe(_probe(files=[ModelDirEntry(path="README.md", size=10)]))
        files = await NodeInspector("n1").inspect(source_url="/data/models/empty")
        assert files.success is False
        assert "权重" in (files.message or "")

    async def test_missing_dir_is_soft_failure(self, patch_probe):
        await patch_probe(ModelDirProbe(exists=False, message="目录不存在或不可达"))
        files = await NodeInspector("n1").inspect(source_url="/data/models/nope")
        assert files.success is False
        assert "不存在" in (files.message or "")

    async def test_node_offline_is_soft_failure(self, monkeypatch):
        from llmpod.apps.manager import dispatch

        async def _offline(node_id, path):
            raise LLMPodError("节点不在线")

        monkeypatch.setattr(dispatch, "probe_model_dir", _offline)
        files = await NodeInspector("n1").inspect(source_url="/data/models/qwen")
        assert files.success is False
        assert "节点" in (files.message or "")
