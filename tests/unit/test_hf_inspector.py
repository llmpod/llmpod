"""huggingface.py HFHubInspector.inspect 单测（httpx.MockTransport 全桩，零网络）。

纯函数（parse_hf_ref / build_model_files）已在 test_model_source.py 覆盖；
本文件聚焦 inspect 的网络编排：meta 抓取 / config 优先级 / subpath 收窄 /
GGUF 头部 Range 探测 / 软失败降级 / endpoint 选择。
"""

from __future__ import annotations

import struct

import httpx
import pytest

from llmpod.apps.manager.model_source.gguf_meta import _MAX_HEADER_READ
from llmpod.apps.manager.model_source.huggingface import HFHubInspector
from llmpod.config.settings import settings

VT_UINT32 = 4
VT_STRING = 8


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _gstr(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u64(len(raw)) + raw


def _gguf_stream(*, expert_count: int = 8) -> httpx.Response:
    body = b"GGUF" + _u32(3) + _u64(0) + _u64(2)
    body += _gstr("general.architecture") + _u32(VT_STRING) + _gstr("llama")
    body += _gstr("llama.expert_count") + _u32(VT_UINT32) + _u32(expert_count)

    async def gen():
        yield body

    return httpx.Response(206, content=gen())


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _meta(siblings, pipeline_tag=None, sha="cafe1234") -> dict:
    meta = {"siblings": [{"rfilename": n} for n in siblings], "sha": sha}
    if pipeline_tag:
        meta["pipeline_tag"] = pipeline_tag
    return meta


CONFIG = {
    "architectures": ["Qwen3ForCausalLM"],
    "num_hidden_layers": 36,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    "hidden_size": 4096,
    "max_position_embeddings": 32768,
}


@pytest.fixture
def inspector() -> HFHubInspector:
    return HFHubInspector()


class TestInspectTransformers:
    async def test_success_assembles_identity(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path == "/api/models/org/qwen3":
                return _json_response(
                    _meta(["config.json", "model.safetensors", "tokenizer.json"], pipeline_tag="text-generation")
                )
            if path == "/org/qwen3/raw/main/config.json":
                return _json_response(CONFIG)
            raise AssertionError(f"unexpected path {path}")

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/qwen3")
        assert out.success is True
        assert out.architecture == "Qwen3ForCausalLM"
        assert out.has_config is True
        assert out.structure == "directory"
        assert out.source_kind == "transformers_folder"
        assert out.suggested_type == "llm"
        assert out.is_moe is False
        assert out.num_layers == 36 and out.num_attention_heads == 32 and out.num_kv_heads == 8
        assert out.head_dim == 128  # hidden_size ÷ heads
        assert out.max_context == 32768
        assert out.weight_files == ["config.json", "model.safetensors"]
        assert out.message is None
        # 未显式传 revision → 固定到仓库 sha（防内容漂移）
        assert out.revision == "cafe1234"

    async def test_explicit_revision_used_in_raw_url_and_result(self, inspector, monkeypatch):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/models/org/m":
                # meta 请求也必须带 revision——清单/内容固定同一 commit，防跨版本混装
                seen["meta_rev"] = dict(req.url.params).get("revision")
                return _json_response(_meta(["config.json", "model.safetensors"]))
            if "/raw/" in req.url.path:
                seen["raw"] = req.url.path
                return _json_response(CONFIG)
            raise AssertionError(f"unexpected path {req.url.path}")

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/m", revision="v2.0")
        assert seen["meta_rev"] == "v2.0"
        assert seen["raw"] == "/org/m/raw/v2.0/config.json"
        assert out.revision == "v2.0"  # 显式版本优先于 sha

    async def test_url_input_uses_url_host(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.host == "hf-mirror.com"  # 镜像 URL host 优先于 settings
            if req.url.path == "/api/models/org/m":
                return _json_response(_meta(["config.json", "model.safetensors"]))
            return _json_response(CONFIG)

        monkeypatch.setattr(settings, "model_source_hf_endpoint", "https://huggingface.co")
        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="https://hf-mirror.com/org/m/tree/main")
        assert out.success is True

    async def test_bare_repo_uses_settings_endpoint(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.host == "mirror.example"
            return _json_response(_meta(["model.safetensors"]))

        monkeypatch.setattr(settings, "model_source_hf_endpoint", "https://mirror.example")
        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/m")
        assert out.success is True


class TestInspectDiffusers:
    async def test_model_index_fallback(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("config.json"):
                return httpx.Response(404)  # 无 config.json（文件不在清单里也不会请求）
            if req.url.path == "/api/models/org/flux":
                return _json_response(
                    _meta(
                        ["model_index.json", "transformer/diffusion_pytorch_model.safetensors"],
                        pipeline_tag="text-to-image",
                    )
                )
            if req.url.path.endswith("model_index.json"):
                return _json_response({"_class_name": "FluxPipeline"})
            raise AssertionError(f"unexpected path {req.url.path}")

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/flux")
        assert out.has_config is True
        assert out.pipeline_class == "FluxPipeline"
        assert out.suggested_type == "image"
        assert out.source_kind == "diffusers_folder"


class TestInspectFailures:
    async def test_meta_404_soft_failure_with_hint(self, inspector, monkeypatch):
        _patch_client(monkeypatch, lambda req: httpx.Response(404))
        out = await inspector.inspect(source_url="org/gone")
        assert out.success is False
        assert "org/gone" in out.message
        assert "HTTPStatusError" in out.message
        assert "hf-mirror" in out.message  # 提示可配镜像

    async def test_connect_error_soft_failure(self, inspector, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("no route")

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/m")
        assert out.success is False
        assert "ConnectError" in out.message

    async def test_config_fetch_failure_records_config_error(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/models/org/m":
                return _json_response(_meta(["config.json", "model.safetensors"]))
            return httpx.Response(500)  # config.json 拉取失败

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/m")
        # 软失败：文件结构事实仍在，提示可人工补全
        assert out.success is True
        assert out.has_config is False
        assert "config.json" in out.message and "HTTPStatusError" in out.message
        assert sorted(out.weight_files) == ["config.json", "model.safetensors"]

    async def test_empty_siblings_tolerated(self, inspector, monkeypatch):
        _patch_client(monkeypatch, lambda req: _json_response({"sha": "x"}))  # 无 siblings/tag
        out = await inspector.inspect(source_url="org/m")
        assert out.success is False  # format/size 全缺 → 硬失败
        assert out.revision == "x"


class TestInspectGGUF:
    async def test_subpath_narrows_and_probes_header(self, inspector, monkeypatch):
        paths = []

        def handler(req: httpx.Request) -> httpx.Response:
            paths.append((req.method, req.url.path, dict(req.headers)))
            if req.url.path == "/api/models/org/gguf-mixtral":
                return _json_response(
                    _meta(["Q4_K_M/model-00001-of-00002.gguf", "Q4_K_M/model-00002-of-00002.gguf", "Q8_0/model.gguf"])
                )
            if "/resolve/" in req.url.path:
                return _gguf_stream(expert_count=8)
            raise AssertionError(f"unexpected path {req.url.path}")

        _patch_client(monkeypatch, handler)
        # 贴变体路径 → siblings 收窄到 Q4_K_M/，Range 只拉第 0 片头部
        out = await inspector.inspect(
            source_url="https://huggingface.co/org/gguf-mixtral/tree/main/Q4_K_M", revision=None
        )
        # 注：subpath 仅识别 URL 形式（bare id 视作 repo id，见 parse_hf_ref）
        resolve_calls = [p for p in paths if p[1].endswith(".gguf")]
        assert len(resolve_calls) == 1
        assert resolve_calls[0][1] == "/org/gguf-mixtral/resolve/main/Q4_K_M/model-00001-of-00002.gguf"
        assert resolve_calls[0][2]["range"] == f"bytes=0-{_MAX_HEADER_READ - 1}"
        assert out.success is True
        assert out.format == "gguf" and out.layout == "gguf"
        assert out.source_kind == "gguf_shards"
        assert out.source_subpath == "Q4_K_M"
        assert out.is_moe is True  # MoE 来自 GGUF 头（无 config）
        assert out.weight_files == ["Q4_K_M/model-00001-of-00002.gguf", "Q4_K_M/model-00002-of-00002.gguf"]
        assert out.is_vision is False

    async def test_mmproj_repo_suggests_multimodal(self, inspector, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/models/org/vl":
                return _json_response(_meta(["model.gguf", "mmproj-F16.gguf"]))  # 无 pipeline_tag
            return _gguf_stream()

        _patch_client(monkeypatch, handler)
        out = await inspector.inspect(source_url="org/vl")
        assert out.suggested_type == "multimodal"  # mmproj 文件级信号兜底
        assert out.is_vision is True
        assert out.mmproj_file == "mmproj-F16.gguf"
        assert out.is_moe is True  # 头部 expert_count=8
