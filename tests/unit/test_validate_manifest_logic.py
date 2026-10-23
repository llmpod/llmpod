"""validate-manifest 纯函数单测（零 IO）。

覆盖 8 条规则的触发条件：
no_weight_file / no_config_or_index / safetensors_shard_incomplete /
gguf_split_incomplete / single_gguf_no_config / mixed_layouts /
unsupported_ext / oversize；以及目录 vs 单文件形态判定、缺片定位。
"""

from __future__ import annotations

from llmpod.apps.manager.model_source.manifest import (
    ManifestFile,
    SEVERITY_FATAL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    validate_manifest,
)

_MAX = 214748364800  # 200 GB


def _f(name: str, size: int = 1) -> ManifestFile:
    return ManifestFile(name=name, size=size)


def _codes(warnings: list, severity: str | None = None) -> list[str]:
    return [w.code for w in warnings if severity is None or w.severity == severity]


# ============ 1. no_weight_file（fatal） ============


class TestNoWeightFile:
    def test_no_weight_fatal(self):
        ws = validate_manifest([_f("config.json"), _f("tokenizer.json"), _f("README.md")], _MAX)
        assert "no_weight_file" in _codes(ws)
        assert ws[_codes(ws).index("no_weight_file")].severity == SEVERITY_FATAL

    def test_single_text_file_no_weight(self):
        ws = validate_manifest([_f("README.md", 100)], _MAX)
        assert "no_weight_file" in _codes(ws)


# ============ 2. no_config_or_index（warn，仅目录形态） ============


class TestNoConfigOrIndex:
    def test_directory_no_config_warn(self):
        # 多文件（目录形态）+ 有权重但无 config/model_index
        ws = validate_manifest([_f("model.safetensors", 1 << 30), _f("tokenizer.json")], _MAX)
        assert "no_config_or_index" in _codes(ws)
        assert ws[_codes(ws).index("no_config_or_index")].severity == SEVERITY_WARN

    def test_single_file_no_config_not_warned(self):
        # 单文件形态不触发 no_config_or_index（由 single_gguf_no_config 等 info 覆盖）
        ws = validate_manifest([_f("model.safetensors", 1 << 30)], _MAX)
        assert "no_config_or_index" not in _codes(ws)

    def test_has_config_no_warning(self):
        ws = validate_manifest([_f("config.json"), _f("model.safetensors", 1 << 30)], _MAX)
        assert "no_config_or_index" not in _codes(ws)

    def test_nested_config_detected_by_basename(self):
        """子路径下的 config.json（name 文档允许子路径）取末段识别，不误报缺 config。"""
        ws = validate_manifest([_f("subdir/config.json"), _f("model.safetensors", 1 << 30)], _MAX)
        assert "no_config_or_index" not in _codes(ws)


# ============ 3. safetensors_shard_incomplete（fatal） ============


class TestSafetensorsShardIncomplete:
    def test_incomplete_safetensors_fatal(self):
        # 声明 1/3，缺 00002、00003
        ws = validate_manifest([_f("model-00001-of-00003.safetensors", 1 << 30)], _MAX)
        assert "safetensors_shard_incomplete" in _codes(ws)
        w = ws[_codes(ws).index("safetensors_shard_incomplete")]
        assert w.severity == SEVERITY_FATAL
        assert "00002" in w.message and "00003" in w.message
        assert "1/3" in w.message

    def test_complete_safetensors_no_warning(self):
        ws = validate_manifest(
            [
                _f("model-00001-of-00002.safetensors", 1 << 30),
                _f("model-00002-of-00002.safetensors", 1 << 30),
            ],
            _MAX,
        )
        assert "safetensors_shard_incomplete" not in _codes(ws)

    def test_shard_missing_middle(self):
        # 缺中间片 00002（声明 1,3/3）
        ws = validate_manifest(
            [
                _f("model-00001-of-00003.safetensors", 1 << 30),
                _f("model-00003-of-00003.safetensors", 1 << 30),
            ],
            _MAX,
        )
        w = ws[_codes(ws).index("safetensors_shard_incomplete")]
        # 缺失列表只有 model-00002-of-00003，不含 model-00001（已有）
        assert "model-00002-of-00003.safetensors" in w.message
        assert "model-00001-of-00003.safetensors" not in w.message

    def test_single_safetensors_no_shard_check(self):
        # model.safetensors（非分片命名）不触发分片检查
        ws = validate_manifest([_f("model.safetensors", 1 << 30)], _MAX)
        assert "safetensors_shard_incomplete" not in _codes(ws)

    def test_two_models_same_total_grouped_by_prefix(self):
        """不同模型共用同一 total 时按 (prefix, total) 分组——不合并 parts 集合。

        回归：旧实现只按 total 聚合，modelA-0000{1,2} + modelB-00001 合并成
        parts={1,2} 判「齐全」，modelB 缺第 2 片被 fatal 级检查漏报。
        """
        ws = validate_manifest(
            [
                _f("modelA-00001-of-00002.safetensors", 1 << 30),
                _f("modelA-00002-of-00002.safetensors", 1 << 30),
                _f("modelB-00001-of-00002.safetensors", 1 << 30),
            ],
            _MAX,
        )
        codes = _codes(ws)
        assert codes.count("safetensors_shard_incomplete") == 1
        w = ws[codes.index("safetensors_shard_incomplete")]
        assert "modelB-00002-of-00002.safetensors" in w.message
        assert "modelA" not in w.message  # 缺失名按组内前缀重建，不串模型

    def test_out_of_range_part_does_not_mask_missing(self):
        """part=0 / part>total 的异常编号不虚增 parts——真实缺片照样报。"""
        ws = validate_manifest(
            [
                _f("model-00000-of-00002.safetensors", 1 << 30),
                _f("model-00005-of-00002.safetensors", 1 << 30),
            ],
            _MAX,
        )
        assert "safetensors_shard_incomplete" in _codes(ws)
        w = ws[_codes(ws).index("safetensors_shard_incomplete")]
        assert "model-00001-of-00002" in w.message and "model-00002-of-00002" in w.message


# ============ 4. gguf_split_incomplete（warn） ============


class TestGgufSplitIncomplete:
    def test_incomplete_gguf_split_warn(self):
        ws = validate_manifest([_f("model-00001-of-00002.gguf", 1 << 30)], _MAX)
        assert "gguf_split_incomplete" in _codes(ws)
        assert ws[_codes(ws).index("gguf_split_incomplete")].severity == SEVERITY_WARN

    def test_complete_gguf_split_no_warning(self):
        ws = validate_manifest(
            [
                _f("model-00001-of-00002.gguf", 1 << 30),
                _f("model-00002-of-00002.gguf", 1 << 30),
            ],
            _MAX,
        )
        assert "gguf_split_incomplete" not in _codes(ws)


# ============ 5. single_gguf_no_config（info） ============


class TestSingleGgufNoConfig:
    def test_single_gguf_info(self):
        ws = validate_manifest([_f("qwen2.5-7b-q4_k_m.gguf", 5 << 30)], _MAX)
        assert "single_gguf_no_config" in _codes(ws)
        assert ws[_codes(ws).index("single_gguf_no_config")].severity == SEVERITY_INFO

    def test_single_safetensors_not_gguf_info(self):
        ws = validate_manifest([_f("model.safetensors", 1 << 30)], _MAX)
        assert "single_gguf_no_config" not in _codes(ws)


# ============ 6. mixed_layouts（warn） ============


class TestMixedLayouts:
    def test_both_config_and_model_index_warn(self):
        ws = validate_manifest(
            [
                _f("config.json"),
                _f("model_index.json"),
                _f("model.safetensors", 1 << 30),
            ],
            _MAX,
        )
        assert "mixed_layouts" in _codes(ws)
        assert ws[_codes(ws).index("mixed_layouts")].severity == SEVERITY_WARN

    def test_only_config_no_mixed(self):
        ws = validate_manifest([_f("config.json"), _f("model.safetensors", 1 << 30)], _MAX)
        assert "mixed_layouts" not in _codes(ws)


# ============ 7. unsupported_ext（warn，仅大文件） ============


class TestUnsupportedExt:
    def test_large_unknown_ext_warn(self):
        ws = validate_manifest([_f("weights.foo", 10 << 20)], _MAX)
        assert "unsupported_ext" in _codes(ws)
        w = ws[_codes(ws).index("unsupported_ext")]
        assert w.severity == SEVERITY_WARN
        assert ".foo" in w.message

    def test_large_unknown_ext_warn_uppercase_aux_not_flagged(self):
        """大写辅助文件（CONFIG.JSON / README.MD）经 _ext 归一小写后进白名单，不误报。

        回归：旧 _ext 在 lower 后的串找 dot、从原始串切片，返回 ".JSON" 原始大小写，
        白名单（全小写）判定失效。
        """
        ws = validate_manifest(
            [_f("model.safetensors", 1 << 30), _f("CONFIG.JSON", 1 << 21), _f("README.MD", 1 << 21)], _MAX
        )
        assert "unsupported_ext" not in _codes(ws)

    def test_small_unknown_ext_not_warned(self):
        # 小文件（< 1MB）不触发，避免误报 README 等
        ws = validate_manifest([_f("notes.xyz", 100)], _MAX)
        assert "unsupported_ext" not in _codes(ws)

    def test_known_weight_ext_not_warned(self):
        ws = validate_manifest([_f("model.safetensors", 1 << 30)], _MAX)
        assert "unsupported_ext" not in _codes(ws)

    def test_json_not_warned(self):
        ws = validate_manifest([_f("config.json", 1 << 20)], _MAX)
        assert "unsupported_ext" not in _codes(ws)

    def test_dedup_by_ext(self):
        # 同扩展名多个文件只提示一次
        ws = validate_manifest([_f("a.foo", 10 << 20), _f("b.foo", 10 << 20)], _MAX)
        assert _codes(ws).count("unsupported_ext") == 1


# ============ 8. oversize（fatal） ============


class TestOversize:
    def test_oversize_fatal(self):
        ws = validate_manifest([_f("model.safetensors", _MAX + 1)], _MAX)
        assert "oversize" in _codes(ws)
        assert ws[_codes(ws).index("oversize")].severity == SEVERITY_FATAL

    def test_exact_limit_not_oversize(self):
        ws = validate_manifest([_f("model.safetensors", _MAX)], _MAX)
        assert "oversize" not in _codes(ws)


# ============ 综合场景 ============


class TestCombined:
    def test_complete_transformers_model_clean(self):
        """完整 transformers 模型：config + 3 片 safetensors 齐全 → 无 fatal/warn。"""
        ws = validate_manifest(
            [
                _f("config.json"),
                _f("model-00001-of-00003.safetensors", 1 << 30),
                _f("model-00002-of-00003.safetensors", 1 << 30),
                _f("model-00003-of-00003.safetensors", 1 << 30),
                _f("tokenizer.json"),
            ],
            _MAX,
        )
        bad = [w for w in ws if w.severity in (SEVERITY_FATAL, SEVERITY_WARN)]
        assert bad == []

    def test_fatal_blocks_user_from_uploading_tens_of_gb(self):
        """设计核心场景：用户只选了 1/3 分片 → fatal 提示，阻止上传几十 GB。"""
        ws = validate_manifest([_f("model-00001-of-00003.safetensors", 10 << 30)], _MAX)
        fatals = [w for w in ws if w.severity == SEVERITY_FATAL]
        assert any(w.code == "safetensors_shard_incomplete" for w in fatals)
