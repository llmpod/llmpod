"""节点目录探测 RPC 单测：只读目录元信息 + 安全边界（绝对路径/允许根/符号链接）。

被测缝是节点服务实现（``NodeServiceImpl.probe_model_dir``）——manager 经隧道调用的
最高可达边界；文件系统行为用 tmp_path 真实驱动，不依赖 docker/manager。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from llmpod.apps.node.services.node import NodeServiceImpl


def _svc(store_dir) -> NodeServiceImpl:
    return NodeServiceImpl(SimpleNamespace(model_store_dir=str(store_dir), model_import_roots=""))


def _write(p, content="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _transformer_dir(root, name="m"):
    d = root / name
    _write(d / "config.json", json.dumps({"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"]}))
    _write(d / "model.safetensors", "x" * 100)
    _write(d / "README.md", "readme")
    return d


class TestProbeModelDir:
    async def test_transformer_dir_returns_raw_facts(self, tmp_path):
        d = _transformer_dir(tmp_path)
        probe = await _svc(tmp_path).probe_model_dir(str(d))

        assert probe.exists is True
        assert probe.is_dir is True
        assert probe.config_name == "config.json"
        assert probe.config["model_type"] == "qwen2"
        assert {e.path for e in probe.files} == {"config.json", "model.safetensors", "README.md"}
        config_text = '{"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"]}'
        assert probe.total_bytes == len(config_text) + 100 + len("readme")

    async def test_missing_dir_is_soft_failure(self, tmp_path):
        probe = await _svc(tmp_path).probe_model_dir(str(tmp_path / "nope"))
        assert probe.exists is False
        assert probe.message

    async def test_file_path_rejected(self, tmp_path):
        f = tmp_path / "model.gguf"
        _write(f)
        probe = await _svc(tmp_path).probe_model_dir(str(f))
        assert probe.exists is False or probe.is_dir is False
        assert probe.message

    async def test_relative_path_rejected(self, tmp_path):
        probe = await _svc(tmp_path).probe_model_dir("models/foo")
        assert probe.exists is False
        assert "绝对路径" in (probe.message or "")

    async def test_outside_allowed_root_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside-model"
        _transformer_dir(outside.parent, outside.name)
        store = tmp_path / "store"
        store.mkdir()
        probe = await _svc(store).probe_model_dir(str(outside))
        assert probe.exists is False
        assert "允许" in (probe.message or "") or "根目录" in (probe.message or "")

    async def test_symlink_rejected(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        outside = _transformer_dir(tmp_path, "real")
        link = store / "link"
        link.symlink_to(outside, target_is_directory=True)
        probe = await _svc(store).probe_model_dir(str(link))
        assert probe.exists is False
        assert "符号链接" in (probe.message or "")

    async def test_root_itself_rejected(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        probe = await _svc(store).probe_model_dir(str(store))
        assert probe.exists is False

    async def test_nested_dirs_listed_relative(self, tmp_path):
        d = tmp_path / "m"
        _write(d / "config.json", "{}")
        _write(d / "sub" / "model-00001-of-00002.safetensors", "a" * 10)
        _write(d / "sub" / "model-00002-of-00002.safetensors", "b" * 20)
        probe = await _svc(tmp_path).probe_model_dir(str(d))
        assert {e.path for e in probe.files} == {
            "config.json",
            "sub/model-00001-of-00002.safetensors",
            "sub/model-00002-of-00002.safetensors",
        }
        assert probe.total_bytes == len("{}") + 10 + 20

    async def test_bad_config_json_degrades(self, tmp_path):
        d = tmp_path / "m"
        _write(d / "config.json", "{not json")
        _write(d / "model.safetensors", "x")
        probe = await _svc(tmp_path).probe_model_dir(str(d))
        assert probe.exists is True
        assert probe.config is None
        assert probe.message


class TestRemoveModelDir:
    async def test_removes_model_dir(self, tmp_path):
        d = _transformer_dir(tmp_path)
        result = await _svc(tmp_path).remove_model_dir(str(d))
        assert result.removed is True
        assert not d.exists()

    async def test_refuses_non_model_dir(self, tmp_path):
        d = tmp_path / "plain"
        _write(d / "notes.txt", "x")
        result = await _svc(tmp_path).remove_model_dir(str(d))
        assert result.removed is False
        assert d.exists()
        assert "拒绝" in (result.message or "")

    async def test_refuses_outside_root(self, tmp_path):
        outside = _transformer_dir(tmp_path, "real")
        store = tmp_path / "store"
        store.mkdir()
        result = await _svc(store).remove_model_dir(str(outside))
        assert result.removed is False
        assert outside.exists()

    async def test_refuses_symlink(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        real = _transformer_dir(tmp_path, "real")
        link = store / "link"
        link.symlink_to(real, target_is_directory=True)
        result = await _svc(store).remove_model_dir(str(link))
        assert result.removed is False
        assert "符号链接" in (result.message or "")
        assert real.exists()

    async def test_missing_dir(self, tmp_path):
        result = await _svc(tmp_path).remove_model_dir(str(tmp_path / "nope"))
        assert result.removed is False
        assert result.message


class TestTildePath:
    async def test_tilde_is_not_absolute(self, tmp_path):
        probe = await _svc(tmp_path).probe_model_dir("~/models")
        assert probe.exists is False
        assert "绝对路径" in (probe.message or "")
