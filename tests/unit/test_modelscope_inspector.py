"""modelscope.ModelScopeInspector.inspect 单测（httpx.MockTransport 全桩，零网络）。

覆盖 ticket 02 的核心路径：裸 id / 根 URL、文件清单 → 元信息 → config、
revision 钉到 LatestCommitter.ShortId、任务标签映射、软失败；
以及 diffusers 与 GGUF 两条产物形态。
"""

from __future__ import annotations

import struct

import httpx

from llmpod.apps.manager.model_source.modelscope import ModelScopeInspector

VT_UINT32 = 4
VT_STRING = 8

REPO = "Qwen/Qwen2.5-0.5B-Instruct"


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _gstr(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u64(len(raw)) + raw


def _gguf_stream() -> httpx.Response:
    body = b"GGUF" + _u32(3) + _u64(0) + _u64(2)
    body += _gstr("general.architecture") + _u32(VT_STRING) + _gstr("llama")
    body += _gstr("llama.expert_count") + _u32(VT_UINT32) + _u32(8)

    async def gen():
        yield body

    return httpx.Response(206, content=gen())


def _gguf_shape_stream() -> httpx.Response:
    entries = [
        ("general.architecture", VT_STRING, "llama"),
        ("general.file_type", VT_UINT32, 15),  # q4_k_m
        ("llama.block_count", VT_UINT32, 32),
        ("llama.attention.head_count", VT_UINT32, 32),
        ("llama.attention.head_count_kv", VT_UINT32, 8),
        ("llama.attention.key_length", VT_UINT32, 128),
        ("llama.context_length", VT_UINT32, 32768),
        ("llama.expert_count", VT_UINT32, 8),
    ]
    body = b"GGUF" + _u32(3) + _u64(0) + _u64(len(entries))
    for key, vtype, value in entries:
        body += _gstr(key) + _u32(vtype)
        body += _gstr(value) if vtype == VT_STRING else _u32(value)

    async def gen():
        yield body

    return httpx.Response(206, content=gen())


def _files_response(paths, *, short_id: str = "abc1234") -> httpx.Response:
    files = [{"Path": path, "Size": size, "Type": "blob"} for path, size in paths]
    return httpx.Response(
        200,
        json={
            "Code": 200,
            "Success": True,
            "Data": {"Files": files, "LatestCommitter": {"ShortId": short_id}},
        },
    )


def _meta_response(*, task: str | None = "text-generation", arch: str | None = "Qwen2ForCausalLM"):
    tasks = [{"Name": task}] if task else []
    archs = [arch] if arch else []
    return httpx.Response(200, json={"Code": 200, "Success": True, "Data": {"Tasks": tasks, "Architectures": archs}})


def _patch_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


CONFIG = {
    "architectures": ["Qwen2ForCausalLM"],
    "num_hidden_layers": 24,
    "num_attention_heads": 14,
    "num_key_value_heads": 2,
    "hidden_size": 896,
    "max_position_embeddings": 32768,
}


class TestInspect:
    async def test_bare_id_pins_short_commit_and_maps_task(self, monkeypatch):
        seen: list[str] = []

        async def handler(request: httpx.Request):
            seen.append(str(request.url))
            path = request.url.path
            if path.endswith("/repo/files"):
                assert request.url.params["Revision"] == "master"
                return _files_response([("config.json", 700), ("model.safetensors", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response()
            if path.endswith("/resolve/abc1234/config.json"):
                return httpx.Response(200, json=CONFIG)
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.source_kind == "transformers_folder"
        assert files.format == "safetensors"
        assert files.architecture == "Qwen2ForCausalLM"
        assert files.suggested_type == "llm"
        assert files.revision == "abc1234"  # 未指定 revision → 钉到 ShortId
        assert files.size_gb and files.size_gb > 0
        assert any("/resolve/abc1234/config.json" in url for url in seen)

    async def test_root_url_uses_url_host(self, monkeypatch):
        hosts: list[str] = []

        async def handler(request: httpx.Request):
            hosts.append(request.url.host)
            path = request.url.path
            if path.endswith("/repo/files"):
                return _files_response([("config.json", 700), ("model.safetensors", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response()
            if path.endswith("/config.json"):
                return httpx.Response(200, json=CONFIG)
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=f"https://modelscope.cn/models/{REPO}")

        assert files.success is True
        assert set(hosts) == {"modelscope.cn"}

    async def test_explicit_revision_is_not_replaced(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                assert request.url.params["Revision"] == "v1"
                return _files_response([("config.json", 700), ("model.safetensors", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response()
            if path.endswith("/resolve/v1/config.json"):
                return httpx.Response(200, json=CONFIG)
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO, revision="v1")

        assert files.success is True
        assert files.revision == "v1"

    async def test_404_returns_soft_failure_with_public_hint(self, monkeypatch):
        async def handler(request: httpx.Request):
            return httpx.Response(
                404,
                json={"Code": 10010205001, "Success": False, "Message": "record not found"},
            )

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url="NoSuchOrg/NoSuchModel")

        assert files.success is False
        assert "公开模型" in (files.message or "")

    async def test_business_code_failure_returns_soft_failure(self, monkeypatch):
        async def handler(request: httpx.Request):
            return httpx.Response(200, json={"Code": 1001, "Success": False, "Message": "bad"})

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is False
        assert "公开模型" in (files.message or "")

    async def test_config_failure_does_not_block_probe(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                return _files_response([("config.json", 700), ("model.safetensors", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response()
            if path.endswith("/config.json"):
                return httpx.Response(500)
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.format == "safetensors"
        assert "config" in (files.message or "")

    async def test_diffusers_model_index(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                return _files_response(
                    [
                        ("model_index.json", 500),
                        ("transformer/diffusion_pytorch_model.safetensors", 2_000_000_000),
                    ]
                )
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task="text-to-image", arch=None)
            if path.endswith("/model_index.json"):
                return httpx.Response(200, json={"_class_name": "FluxPipeline"})
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.source_kind == "diffusers_folder"
        assert files.pipeline_class == "FluxPipeline"
        assert files.suggested_type == "image"

    async def test_gguf_header_supplies_moe(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                return _files_response([("model.gguf", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task=None, arch=None)
            if path.endswith("/model.gguf"):
                return _gguf_stream()
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.format == "gguf"
        assert files.is_moe is True

    async def test_resolve_url_subpath_scopes_files_and_url_revision_wins(self, monkeypatch):
        seen_revisions: list[str] = []

        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                seen_revisions.append(request.url.params["Revision"])
                return httpx.Response(
                    200,
                    json={
                        "Code": 200,
                        "Success": True,
                        "Data": {
                            "Files": [
                                {"Path": "Q4_K_M/model.gguf", "Size": 1_000_000_000, "Type": "blob"},
                                {"Path": "Q8_0/model.gguf", "Size": 2_000_000_000, "Type": "blob"},
                            ],
                            "LatestCommitter": {"ShortId": "abc1234"},
                        },
                    },
                )
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task=None, arch=None)
            if path.endswith("/Q4_K_M/model.gguf"):
                return _gguf_stream()
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(
            source_url=f"https://www.modelscope.cn/models/{REPO}/resolve/v1/Q4_K_M",
            revision="form-rev",
        )

        assert files.success is True
        assert files.source_subpath == "Q4_K_M"
        assert files.format == "gguf"
        assert files.is_moe is True
        assert seen_revisions == ["v1"]  # URL 内 revision 优先于表单

    async def test_file_view_url_subpath(self, monkeypatch):
        seen_revisions: list[str] = []

        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                seen_revisions.append(request.url.params["Revision"])
                return _files_response([("config.json", 700), ("Q4_K_M/model.gguf", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task=None, arch=None)
            if path.endswith("/config.json"):
                return httpx.Response(200, json={})
            if path.endswith("/Q4_K_M/model.gguf"):
                return _gguf_stream()
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(
            source_url=f"https://www.modelscope.cn/models/{REPO}/file/view/master/Q4_K_M"
        )

        assert files.source_subpath == "Q4_K_M"
        assert files.revision == "master"  # URL 内显式 revision 优先，不替换为 ShortId
        assert seen_revisions == ["master"]

    async def test_nonexistent_subpath_returns_soft_failure(self, monkeypatch):
        async def handler(request: httpx.Request):
            if request.url.path.endswith("/repo/files"):
                return _files_response([("config.json", 700), ("model.safetensors", 1_000_000_000)])
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(
            source_url=f"https://www.modelscope.cn/models/{REPO}/resolve/master/nope"
        )

        assert files.success is False
        assert "子路径" in (files.message or "")

    async def test_multiple_gguf_variants_error_uses_pinned_revision(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                return httpx.Response(
                    200,
                    json={
                        "Code": 200,
                        "Success": True,
                        "Data": {
                            "Files": [
                                {"Path": "Q4_K_M/model.gguf", "Size": 1_000_000_000, "Type": "blob"},
                                {"Path": "Q8_0/model.gguf", "Size": 2_000_000_000, "Type": "blob"},
                            ],
                            "LatestCommitter": {"ShortId": "abc1234"},
                        },
                    },
                )
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task=None, arch=None)
            if path.endswith(".gguf"):
                return _gguf_stream()
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is False
        assert "多个 GGUF" in (files.message or "")
        assert "/resolve/abc1234/" in (files.message or "")

    async def test_architecture_fallback_from_meta(self, monkeypatch):
        async def handler(request: httpx.Request):
            if request.url.path.endswith("/repo/files"):
                return _files_response([("model.safetensors", 1_000_000_000)])
            if request.url.path == f"/api/v1/models/{REPO}":
                return _meta_response(task="text-generation", arch="Qwen2ForCausalLM")
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.architecture == "Qwen2ForCausalLM"
        assert files.has_config is False

    async def test_gguf_header_shape_and_quantization(self, monkeypatch):
        async def handler(request: httpx.Request):
            path = request.url.path
            if path.endswith("/repo/files"):
                return _files_response([("model.gguf", 1_000_000_000)])
            if path == f"/api/v1/models/{REPO}":
                return _meta_response(task=None, arch=None)
            if path.endswith(".gguf"):
                return _gguf_shape_stream()
            return httpx.Response(404)

        _patch_client(monkeypatch, handler)
        files = await ModelScopeInspector().inspect(source_url=REPO)

        assert files.success is True
        assert files.is_moe is True
        assert files.quantization == "q4_k_m"
        assert files.num_layers == 32
        assert files.num_attention_heads == 32
        assert files.num_kv_heads == 8
        assert files.head_dim == 128
        assert files.max_context == 32768


async def test_unparseable_source_returns_soft_failure():
    files = await ModelScopeInspector().inspect(source_url="not-a-repo")
    assert files.success is False
    assert "组织/模型名" in (files.message or "")
