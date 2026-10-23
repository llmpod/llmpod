"""引擎参数声明单测：protected_flags / param_suggestions / overlap 检测。"""

from __future__ import annotations

from types import SimpleNamespace

from llmpod.apps.manager.deployment.assess import check_protected_overlap
from llmpod.apps.manager.deployment.engine.base import ParamSuggestion, ensure_trust_remote_code
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter


class TestVllmParamDecls:
    def test_protected_flags_cover_hardcoded(self):
        """protected_flags 必须覆盖 generate_start_args 里硬编码的系统必控 flag（防漂移）。"""
        hardcoded = {
            "--model",
            "--served-model-name",
            "--host",
            "--port",
            "--tensor-parallel-size",
            "--pipeline-parallel-size",
            "--max-model-len",
            "--runner",
            "--convert",
            "--nnodes",
            "--node-rank",
            "--master-addr",
            "--master-port",
            "--headless",
        }
        assert hardcoded <= set(VLLMAdapter.protected_flags)

    def test_suggestions_are_not_protected(self):
        """推荐参数 chip 不应与 protected_flags 重叠（重叠=用户加了却被忽略，误导）。"""
        suggestion_names = {p.name for p in VLLMAdapter.param_suggestions}
        assert suggestion_names.isdisjoint(set(VLLMAdapter.protected_flags))

    def test_suggestions_have_names(self):
        names = [p.name for p in VLLMAdapter.param_suggestions]
        assert "--trust-remote-code" in names
        assert "--gpu-memory-utilization" not in names  # 由 memory_utilization_flag 注入，非自由 chip
        assert all(n.startswith("--") for n in names)

    def test_gpu_memory_utilization_not_protected(self):
        """--gpu-memory-utilization 是用户熟悉的显存占比权威出处，可编辑（不在 protected_flags）。"""
        assert "--gpu-memory-utilization" not in VLLMAdapter.protected_flags

    def test_param_suggestion_model(self):
        p = ParamSuggestion(name="--x", hint="h", default="1", choices=["1", "2"])
        assert p.model_dump() == {"name": "--x", "hint": "h", "default": "1", "choices": ["1", "2"]}


class TestCheckProtectedOverlap:
    def test_overlap_detected(self):
        overlap = check_protected_overlap("vllm", ["--port", "8000", "--trust-remote-code"])
        assert overlap == ["--port"]

    def test_no_overlap(self):
        assert check_protected_overlap("vllm", ["--trust-remote-code", "--dtype", "half"]) == []

    def test_empty_extra_args(self):
        assert check_protected_overlap("vllm", None) == []
        assert check_protected_overlap("vllm", []) == []

    def test_unknown_engine(self):
        assert check_protected_overlap("nope_engine", ["--port"]) == []


class TestEnsureTrustRemoteCode:
    """自定义架构模型（jina/mxbai 系 reranker）创建部署时自动补信任 flag。"""

    def _model(self, needs: bool):
        return SimpleNamespace(needs_trust_remote_code=needs)

    def test_adds_flag_when_model_needs_trust(self):
        out = ensure_trust_remote_code(VLLMAdapter, self._model(True), {})
        assert out["extra_args"][-1] == "--trust-remote-code"

    def test_noop_when_model_does_not_need(self):
        assert ensure_trust_remote_code(VLLMAdapter, self._model(False), {}) == {}

    def test_noop_when_user_already_gave_flag(self):
        params = {"extra_args": ["--dtype", "half"]}
        out = ensure_trust_remote_code(VLLMAdapter, self._model(True), params)
        assert out["extra_args"] == ["--dtype", "half", "--trust-remote-code"]
        # 用户已给（含 =value 连写形态）不重复加
        params2 = {"extra_args": ["--trust-remote-code"]}
        assert ensure_trust_remote_code(VLLMAdapter, self._model(True), params2) == params2
        params3 = {"extra_args": ["--trust-remote-code=True"]}
        assert ensure_trust_remote_code(VLLMAdapter, self._model(True), params3) == params3

    def test_noop_for_engine_without_flag(self):
        from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter

        assert LlamaCppAdapter.trust_remote_code_flag is None
        assert ensure_trust_remote_code(LlamaCppAdapter, self._model(True), {}) == {}

    def test_preserves_other_engine_params(self):
        params = {"extra_args": ["--dtype", "half"], "other": 1}
        out = ensure_trust_remote_code(VLLMAdapter, self._model(True), params)
        assert out["other"] == 1
        assert out["extra_args"] == ["--dtype", "half", "--trust-remote-code"]
