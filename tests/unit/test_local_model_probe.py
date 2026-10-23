"""local 模型探测单测：ModelProbe → ModelFiles 装配 + manager 本地直读。"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from llmpod.apps.manager.model_source.local import (
    LocalInspector,
    ModelProbe,
    _read_local_probe_sync,
    build_local_model_files,
)


# ============ ModelProbe → ModelFiles（manager 侧） ============


class TestBuildLocalModelFiles:
    def test_transformers_dir(self):
        probe = ModelProbe(
            exists=True,
            config={"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"},
            weight_files=["model.safetensors"],
            total_bytes=1024**3,
        )
        mf = build_local_model_files(probe, "/data/models/Qwen2.5-0.5B")
        assert mf.structure == "directory"
        assert mf.source_kind == "transformers_folder"
        assert mf.layout == "transformers"
        assert mf.architecture == "Qwen2ForCausalLM"
        assert mf.format == "safetensors"
        assert mf.size_gb == 1.0
        assert mf.suggested_type is None  # local 无 pipeline_tag，用户选
        assert mf.has_config is True

    def test_single_gguf_file(self):
        probe = ModelProbe(exists=True, weight_files=["qwen2.5-7b-q4_k_m.gguf"], total_bytes=5 * 1024**3)
        mf = build_local_model_files(probe, "/data/models/qwen.gguf")
        assert mf.structure == "single_file"
        assert mf.source_kind == "single_gguf"
        assert mf.layout == "gguf"
        assert mf.format == "gguf"
        assert mf.quantization == "q4_k_m"
        assert mf.suggested_type is None

    def test_moe_config(self):
        probe = ModelProbe(
            exists=True,
            config={"architectures": ["MixtralForCausalLM"], "num_local_experts": 8},
            weight_files=["model.safetensors"],
            total_bytes=0,
        )
        mf = build_local_model_files(probe, "/data/models/mixtral")
        assert mf.is_moe is True
        assert mf.architecture == "MixtralForCausalLM"


class TestLocalInspector:
    """LocalInspector 现为 manager 本地直读（不再经 node RPC）。

    inspect 有 model_store_dir 根约束（防借探测接口枚举 manager 任意目录），
    用例统一把 settings.model_store_dir 指到 tmp_path。
    """

    async def test_symlinked_config_outside_store_is_not_read(self, tmp_path, monkeypatch):
        from llmpod.config.settings import settings

        store = tmp_path / "models"
        store.mkdir()
        model = store / "model"
        model.mkdir()
        outside = tmp_path / "private.json"
        outside.write_text('{"architectures":["SECRET_CLASS"]}')
        (model / "config.json").symlink_to(outside)
        monkeypatch.setattr(settings, "model_store_dir", str(store))
        result = await LocalInspector().inspect(source_url=str(model))
        assert result.has_config is False
        assert "SECRET_CLASS" not in str(result)

    async def test_transformers_dir(self, tmp_path: Path, monkeypatch):
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))
        (tmp_path / "config.json").write_text(
            json.dumps({"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}), encoding="utf-8"
        )
        (tmp_path / "model.safetensors").write_bytes(b"\x00" * 100)
        mf = await LocalInspector().inspect(source_url=str(tmp_path))
        assert mf.success is True
        assert mf.structure == "directory"
        assert mf.layout == "transformers"
        assert mf.format == "safetensors"
        assert mf.architecture == "Qwen2ForCausalLM"
        assert mf.suggested_type is None  # local 无 pipeline_tag，用户选

    async def test_single_gguf_file(self, tmp_path: Path, monkeypatch):
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))
        f = tmp_path / "qwen2.5-7b-q4_k_m.gguf"
        f.write_bytes(b"\x00" * 100)
        mf = await LocalInspector().inspect(source_url=str(f))
        assert mf.success is True
        assert mf.structure == "single_file"
        assert mf.layout == "gguf"
        assert mf.format == "gguf"
        assert mf.quantization == "q4_k_m"

    async def test_path_not_exists_soft_fail(self, tmp_path: Path, monkeypatch):
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))
        mf = await LocalInspector().inspect(source_url=str(tmp_path / "nope"))
        assert mf.success is False
        assert mf.message  # 软失败，前端可人工填

    async def test_outside_store_root_rejected(self, tmp_path: Path, monkeypatch):
        """越界路径（不在 model_store_dir 下）软失败拒绝，不读盘。"""

        from llmpod.config.settings import settings
        from llmpod.apps.manager.model_source import local as local_mod

        store = tmp_path / "store"
        store.mkdir()
        monkeypatch.setattr(settings, "model_store_dir", str(store))

        called = False
        orig = local_mod._read_local_probe_sync

        def _spy(p):
            nonlocal called
            called = True
            return orig(p)

        monkeypatch.setattr(local_mod, "_read_local_probe_sync", _spy)

        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "config.json").write_text(json.dumps({"architectures": ["X"], "model_type": "x"}), encoding="utf-8")
        mf = await LocalInspector().inspect(source_url=str(outside))
        assert mf.success is False
        assert "不存在或不可达" in (mf.message or "")  # 与「不存在」同文案，不泄露 store 边界判定
        assert called is False  # 未进入读盘

    async def test_dotdot_traversal_rejected(self, tmp_path: Path, monkeypatch):
        """store 内路径带 .. 逃逸到根外同样拒绝。"""
        from llmpod.config.settings import settings

        store = tmp_path / "store"
        (store / "sub").mkdir(parents=True)
        monkeypatch.setattr(settings, "model_store_dir", str(store))
        mf = await LocalInspector().inspect(source_url=str(store / "sub" / ".." / ".." / "etc"))
        assert mf.success is False
        assert "不存在或不可达" in (mf.message or "")  # 与「不存在」同文案，不泄露 store 边界判定


class TestReadLocalProbe:
    """manager 本地路径 → ModelProbe（纯函数，离线）。"""

    def test_not_exists(self, tmp_path: Path):
        probe = _read_local_probe_sync(tmp_path / "nope")
        assert probe.exists is False
        assert probe.message

    def test_dir_with_config_and_weights(self, tmp_path: Path):
        (tmp_path / "config.json").write_text(
            json.dumps({"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}), encoding="utf-8"
        )
        (tmp_path / "model-00001-of-00002.safetensors").write_bytes(b"\x00" * 100)
        (tmp_path / "model-00002-of-00002.safetensors").write_bytes(b"\x00" * 50)
        probe = _read_local_probe_sync(tmp_path)
        assert probe.exists is True
        assert probe.config == {"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}
        assert set(probe.weight_files) == {
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
        }
        assert probe.total_bytes == 150

    def test_single_file(self, tmp_path: Path):
        f = tmp_path / "model.gguf"
        f.write_bytes(b"\x00" * 200)
        probe = _read_local_probe_sync(f)
        assert probe.exists is True
        assert probe.config is None
        assert probe.weight_files == ["model.gguf"]
        assert probe.total_bytes == 200

    def test_dir_no_config(self, tmp_path: Path):
        (tmp_path / "model.safetensors").write_bytes(b"\x00" * 100)
        probe = _read_local_probe_sync(tmp_path)
        assert probe.exists is True
        assert probe.config is None
        assert probe.weight_files == ["model.safetensors"]

    def test_utf16_config_soft_degrades(self, tmp_path: Path):
        """非 UTF-8 编码且解码必失败的 config.json（UTF-16 带 BOM 手工预置常见）不炸
        探测——软失败按「无可用 config」继续，否则 /inspect 直接 500。
        """
        (tmp_path / "config.json").write_bytes('{"model_type": "qwen2"}'.encode("utf-16"))
        (tmp_path / "model.safetensors").write_bytes(b"\x00" * 100)
        probe = _read_local_probe_sync(tmp_path)
        assert probe.exists is True
        assert probe.config is None  # 读不出但探测不死
        assert probe.weight_files == ["model.safetensors"]

    def test_non_dict_config_soft_degrades(self, tmp_path: Path):
        """顶层非 JSON object 的 config.json（合法 JSON：数组/字符串/数字）同样降级——
        喂给 ModelProbe.config（dict | None）会 pydantic ValidationError 逃成 500。"""
        (tmp_path / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
        (tmp_path / "model.safetensors").write_bytes(b"\x00" * 100)
        probe = _read_local_probe_sync(tmp_path)
        assert probe.exists is True
        assert probe.config is None
        assert probe.config_name is None
        assert probe.weight_files == ["model.safetensors"]


# ---- GGUF 字节流构造（value_type 与 gguf_meta 内部一致）----

_VT_UINT32 = 4
_VT_STRING = 8


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _gguf_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u64(len(raw)) + raw


def _kv(key: str, vtype: int, value: bytes) -> bytes:
    return _gguf_str(key) + _u32(vtype) + value


def _gguf_with_header(*, file_type: int = 15, layers: int = 48, experts: int = 0) -> bytes:
    """最小 GGUF：架构 + 层数 +（可选）专家数 + 文件头量化类型。"""
    kvs = [
        _kv("general.architecture", _VT_STRING, _gguf_str("qwen2")),
        _kv("qwen2.block_count", _VT_UINT32, _u32(layers)),
        _kv("general.file_type", _VT_UINT32, _u32(file_type)),
    ]
    if experts:
        kvs.append(_kv("qwen2.expert_count", _VT_UINT32, _u32(experts)))
    out = b"GGUF" + _u32(3) + _u64(0) + _u64(len(kvs))
    for kv in kvs:
        out += kv
    return out


class TestLocalGGUFHeaderRead:
    """local 源读 GGUF 文件头的两种落盘形态。

    单文件源曾经把**文件路径当目录**用，拼出 ``<file>/<file>`` 永远读不到——MoE / 架构形状 /
    量化类型在 local 单文件源上静默全空，量化只能靠文件名兜底。既有用例 monkeypatch 了解析器，
    照不到这条路径，故这里用真实临时文件打穿。
    """

    def test_single_file_reads_the_file_itself(self, tmp_path: Path):
        # 文件名故意不带量化标记：值只能来自文件头
        gguf = tmp_path / "deepseek-r1-14b.gguf"
        gguf.write_bytes(_gguf_with_header(file_type=15, layers=48))

        probe = _read_local_probe_sync(gguf)
        mf = build_local_model_files(probe, str(gguf))

        assert mf.quantization == "q4_k_m"
        assert mf.num_layers == 48

    def test_directory_form_joins_relative_path(self, tmp_path: Path):
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "model.gguf").write_bytes(_gguf_with_header(file_type=30, layers=32, experts=8))

        probe = _read_local_probe_sync(staged)
        mf = build_local_model_files(probe, str(staged))

        assert mf.quantization == "iq4_xs"
        assert mf.num_layers == 32
        assert mf.is_moe is True

    def test_single_file_mmproj_is_not_treated_as_the_model(self, tmp_path: Path):
        """source 指到投影器文件时不能拿它的头当模型。

        目录形态经 ``first_gguf_shard`` 天然排掉 mmproj，单文件形态没有那一层——
        漏了就会把投影器的层数/视觉标记/量化安到模型上。
        """
        proj = tmp_path / "mmproj-model-f16.gguf"
        proj.write_bytes(_gguf_with_header(file_type=1, layers=999))

        probe = _read_local_probe_sync(proj)
        mf = build_local_model_files(probe, str(proj))

        assert mf.num_layers is None  # 没读投影器的头
        assert mf.quantization is None  # 也没从投影器文件名里抠出 f16

    def test_header_quant_used_even_with_config_present(self, tmp_path: Path):
        """有 config.json 也读 GGUF 头——HF 路径无条件这么做，两条源口径必须一致。

        GGUF 的 config.json 从不带量化信息；不读头就会退回文件名，同一份产物在
        local 与 HF 两条源上得到不同的量化值 → recipe_digest 分叉成两个下载目录。
        """
        (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen2"}), encoding="utf-8")
        (tmp_path / "model-Q5_K_L.gguf").write_bytes(_gguf_with_header(file_type=15, layers=48))

        mf = build_local_model_files(_read_local_probe_sync(tmp_path), str(tmp_path))

        assert mf.quantization == "q4_k_m"  # 来自文件头，不是文件名里的伪标记 Q5_K_L
