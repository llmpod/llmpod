"""canonical 路径推导单测（``canonical_local_dir``/``slugify_name``/``recipe_digest`` 居 placement.py）。

回归防护：该函数曾用 ``from llmpod.config import settings``（拿到模块而非实例），
导致 ``canonical_local_dir()`` 实际调用时 AttributeError——被 orchestrator 的
``config.model_path or canonical_local_dir(...)`` 短路掩盖，HF 自动下载主路径会崩。
"""

from __future__ import annotations

import os

from llmpod.apps.manager.deployment.placement import canonical_local_dir, recipe_digest, slugify_name
from llmpod.config.settings import settings
from llmpod.models.model import Model


def _model(
    name: str = "Qwen2.5-7B-Instruct",
    source: str | None = "huggingface",
    source_url: str | None = "Qwen/Qwen2.5-7B-Instruct",
    source_subpath: str | None = None,
    revision: str | None = None,
    source_kind: str | None = None,
) -> Model:
    """构造瞬态 Model（不落库），只喂 canonical 需要的配方/名称字段。"""
    kwargs = dict(
        name=name,
        model_type="llm",
        source=source,
        source_url=source_url,
        source_subpath=source_subpath,
        revision=revision,
    )
    if source_kind is not None:
        kwargs["source_kind"] = source_kind
    return Model(**kwargs)


class TestSlugifyName:
    def test_lowercase_and_separators(self):
        assert slugify_name("Qwen2.5-7B-Instruct") == "qwen2-5-7b-instruct"

    def test_spaces_and_punct_collapse(self):
        assert slugify_name("  My   Model!!  ") == "my-model"

    def test_unicode_name_kept(self):
        assert slugify_name("生产客服7B") == "生产客服7b"

    def test_path_traversal_stripped(self):
        assert ".." not in slugify_name("../etc/passwd")

    def test_empty_falls_back(self):
        assert slugify_name("") == "model"
        assert slugify_name("---") == "model"

    def test_truncates_long_name(self):
        s = slugify_name("a" * 100)
        assert len(s) <= 48
        assert not s.endswith("-")


class TestRecipeDigest:
    def test_deterministic(self):
        assert recipe_digest("huggingface", "Qwen/Qwen2.5-7B", None, None) == recipe_digest(
            "huggingface", "Qwen/Qwen2.5-7B", None, None
        )

    def test_none_treated_as_empty(self):
        assert recipe_digest(None, None, None, None) == recipe_digest("", "", "", "")

    def test_distinct_source_url_distinct_digest(self):
        a = recipe_digest("huggingface", "Qwen/Qwen2.5-7B", None, None)
        b = recipe_digest("huggingface", "meta-llama/Llama-3-8B", None, None)
        assert a != b

    def test_subpath_changes_digest(self):
        a = recipe_digest("huggingface", "org/repo", None, None)
        b = recipe_digest("huggingface", "org/repo", "ggml-model-Q4_K_M", None)
        assert a != b

    def test_revision_changes_digest(self):
        a = recipe_digest("huggingface", "org/repo", None, None)
        b = recipe_digest("huggingface", "org/repo", None, "abc123")
        assert a != b

    def test_format_quantization_kind_change_digest(self):
        """同仓库不同格式/量化/源形态 → 不同 digest（目录不碰撞，防用错权重）。"""
        base = ("huggingface", "org/repo", None, None)
        assert recipe_digest(*base) != recipe_digest(*base, format="safetensors")
        assert recipe_digest(*base, format="safetensors") != recipe_digest(*base, format="gguf")
        assert recipe_digest(*base, quantization="awq") != recipe_digest(*base, quantization="gptq")
        assert recipe_digest(*base, source_kind="single_gguf") != recipe_digest(
            *base, source_kind="transformers_folder"
        )


class TestCanonicalLocalDir:
    def test_returns_store_root_plus_slug_digest(self):
        # 黄金断言：默认 fixture 的路径是配方确定的字面量（slug=name 的 slug，digest=sha256[:12]），
        # 不用被测函数自证——那会掩盖 slug/digest 算法回归。
        assert canonical_local_dir(_model()) == f"{settings.model_store_dir}/qwen2-5-7b-instruct-994d353985fa"

    def test_always_absolute(self):
        assert os.path.isabs(canonical_local_dir(_model()))

    def test_relative_store_dir_defensively_absolutized(self, monkeypatch):
        """相对 model_store_dir（单机 manager+node 同 CWD）也能救回，不直接 invalid mount path 崩。"""
        monkeypatch.setattr(settings, "model_store_dir", "data/models")
        assert os.path.isabs(canonical_local_dir(_model()))

    def test_uses_name_not_id(self):
        # 路径含 name slug，不含 DB 自增 id（清库 id 重建不串模型）
        path = canonical_local_dir(_model(name="MyModel"))
        assert "mymodel" in path
        assert "/model-" not in path

    def test_same_recipe_same_dir_regardless_of_id(self):
        # 同配方（同名+同 source）→ 同目录；id 不同也不变（清库重建 id 复用旧目录的根因被消除）
        m1 = _model()
        m2 = _model()
        m1.id = 1
        m2.id = 2
        assert canonical_local_dir(m1) == canonical_local_dir(m2)

    def test_different_name_distinct_dir(self):
        assert canonical_local_dir(_model(name="A")) != canonical_local_dir(_model(name="B"))

    def test_different_recipe_distinct_dir(self):
        assert canonical_local_dir(_model(source_url="org/a")) != canonical_local_dir(_model(source_url="org/b"))

    def test_no_redundant_models_segment(self):
        path = canonical_local_dir(_model())
        assert "/models/models/" not in path

    def test_manager_source_content_addressed_not_by_url(self):
        # local/manager 源：身份 = name-slug + 「source+形状+内容哈希」digest，不含 source_url 末段
        m = _model(name="qwen3-5-4b-fp8", source="local", source_url="/store/upload/aaa")
        path = canonical_local_dir(m)
        assert path.startswith(f"{settings.model_store_dir}/qwen3-5-4b-fp8-")
        # 同内容（同 name + 同形状/哈希）→ 同目录；source_url 变化（每次上传独立目录）不影响
        m2 = _model(name="qwen3-5-4b-fp8", source="local", source_url="/store/upload/bbb")
        assert canonical_local_dir(m2) == path
        # 与 hf/oss 源不撞目录（source 进 digest）
        hf = _model(name="qwen3-5-4b-fp8", source="huggingface", source_url="Qwen/Qwen3")
        assert canonical_local_dir(hf) != path

    def test_manager_source_rename_changes_dir(self):
        # 改名换 slug → 换目录（rename 罕见，可接受；内容哈希才是真正的身份）
        a = _model(name="a", source="local", source_url="/store/upload/x")
        b = _model(name="b", source="local", source_url="/store/upload/x")
        assert canonical_local_dir(a) != canonical_local_dir(b)

    def test_manager_source_shape_changes_dir(self):
        # 同目录名、不同形状（source_kind）→ 不撞目录，防用错权重
        a = _model(source="local", source_url="/store/upload/qwen3", source_kind="single_gguf")
        b = _model(source="local", source_url="/store/upload/qwen3", source_kind="transformers_folder")
        assert canonical_local_dir(a) != canonical_local_dir(b)

    def test_remote_source_still_uses_digest(self):
        # hf/oss 源 source_url 是稳定身份（repo id/直链），仍带配方哈希（默认 fixture 的黄金断言已锁字面量）
        assert canonical_local_dir(_model(source="huggingface")).endswith("qwen2-5-7b-instruct-994d353985fa")
