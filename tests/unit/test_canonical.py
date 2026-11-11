"""canonical 路径推导单测（``canonical_local_dir``/``slugify_name`` 居 placement.py）。

目录规则（两条分支）：
- local/manager 源：canonical = source_url（单文件取所在目录）——同机 node 直接用源
  目录、远端下载到同一路径，所有节点 model_path 恒相同；
- HF/远程源：``{model_store_dir}/{name-slug}``——与 ``hf download --local-dir`` /
  modelscope ``--local_dir`` 的落盘布局一致，用户可用官方 CLI 在 store 内直接预置管理。

回归防护：该函数曾用 ``from llmpod.config import settings``（拿到模块而非实例），
导致 ``canonical_local_dir()`` 实际调用时 AttributeError——被 orchestrator 的
``config.model_path or canonical_local_dir(...)`` 短路掩盖，HF 自动下载主路径会崩。
"""

from __future__ import annotations

import os

from llmpod.apps.manager.deployment.placement import canonical_local_dir, slugify_name
from llmpod.config.settings import settings
from llmpod.models.model import Model


def _model(
    name: str = "Qwen2.5-7B-Instruct",
    source: str | None = "huggingface",
    source_url: str | None = "Qwen/Qwen2.5-7B-Instruct",
) -> Model:
    """构造瞬态 Model（不落库），只喂 canonical 需要的配方/名称字段。"""
    return Model(name=name, model_type="llm", source=source, source_url=source_url)


class TestSlugifyName:
    def test_repo_style_takes_last_segment(self):
        # repo 写法 ``Qwen/Qwen3-8B`` → 取末段（用户贴仓库名当模型名的常见形态）
        assert slugify_name("Qwen/Qwen3-8B") == "qwen3-8b"

    def test_lowercase_and_separators(self):
        assert slugify_name("Qwen2.5-7B-Instruct") == "qwen2-5-7b-instruct"

    def test_spaces_and_punct_collapse(self):
        assert slugify_name("  My   Model!!  ") == "my-model"

    def test_path_traversal_stripped(self):
        # 取末段天然剥掉 ``../`` 前缀；名字里不会出现路径穿越成分
        assert ".." not in slugify_name("../etc/passwd")
        assert "/" not in slugify_name("../etc/passwd")

    def test_unicode_name_kept(self):
        assert slugify_name("生产客服7B") == "生产客服7b"

    def test_empty_falls_back(self):
        assert slugify_name("") == "model"
        assert slugify_name("---") == "model"

    def test_truncates_long_name(self):
        s = slugify_name("a" * 100)
        assert len(s) <= 48
        assert not s.endswith("-")


class TestHfSourceDir:
    def test_flat_slug_dir(self):
        # 黄金断言：HF 源 = {store}/{name-slug}（用户可用官方 CLI 同款目录直接预置）
        assert canonical_local_dir(_model()) == f"{settings.model_store_dir}/qwen2-5-7b-instruct"

    def test_repo_style_name_uses_last_segment(self):
        # 名字贴成仓库名 ``Qwen/Qwen3-8B`` → 目录取末段，不是把斜杠折成 ``-``
        assert canonical_local_dir(_model(name="Qwen/Qwen3-8B")) == f"{settings.model_store_dir}/qwen3-8b"

    def test_source_url_does_not_affect_hf_dir(self):
        # HF 源目录按注册名寻址（镜像 URL / repo id 只是获取方式，不进目录名）
        assert canonical_local_dir(_model(source_url="https://hf-mirror.com/x/y")) == canonical_local_dir(_model())

    def test_always_absolute(self):
        assert os.path.isabs(canonical_local_dir(_model()))

    def test_relative_store_dir_defensively_absolutized(self, monkeypatch):
        """canonical 恒为绝对路径——相对 model_store_dir（历史回归：曾拿到 settings 模块
        而非实例导致 AttributeError，且相对路径会让 docker 卷挂载报 invalid mount path）。"""
        monkeypatch.setattr(settings, "model_store_dir", "data/models")
        assert os.path.isabs(canonical_local_dir(_model()))

    def test_same_name_same_dir_regardless_of_id(self):
        m1 = _model()
        m2 = _model()
        m1.id = 1
        m2.id = 2
        assert canonical_local_dir(m1) == canonical_local_dir(m2)


class TestManagerSourceDir:
    def test_equals_source_url(self):
        # local 源：canonical = source_url 本身（同机共享挂载直接用，不另造目录）
        m = _model(source="local", source_url="/data/models/qwenxxx")
        assert canonical_local_dir(m) == "/data/models/qwenxxx"

    def test_single_file_source_uses_parent_dir(self, tmp_path):
        # 单文件源：取所在目录（权重与配套件同目录布局的既有约定）；文件/目录按盘上
        # 真实状态判，故用 tmp 文件驱动
        gguf = tmp_path / "model.gguf"
        gguf.write_bytes(b"x")
        m = _model(source="local", source_url=str(gguf))
        assert canonical_local_dir(m) == str(tmp_path)

    def test_name_irrelevant(self):
        # 目录跟 source_url 走，注册名不参与（改名不换目录）
        a = _model(name="a", source="local", source_url="/data/models/qwenxxx")
        b = _model(name="b", source="local", source_url="/data/models/qwenxxx")
        assert canonical_local_dir(a) == canonical_local_dir(b) == "/data/models/qwenxxx"

    def test_no_source_url_falls_back_to_store_root_placeholder(self):
        # 无 source_url：回退 store 根占位（合法路径形状，下发前会被拦截，不产生野路径）
        m = _model(source="local", source_url=None)
        assert canonical_local_dir(m) == os.path.abspath(settings.model_store_dir)
