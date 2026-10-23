"""build_run_spec 适配器默认值接线单测（vLLM 默认值不再硬编码在编排器）。

验证编排器从适配器读 container 默认值（network/ipc/shm/restart/port/protocol/capabilities），
而非硬编码 vLLM 的 host/16g/...——加第 2 引擎只需覆盖这些 class attr，不被 vLLM 影子覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

from llmpod.apps.manager.deployment.engine.base import EngineAdapter
from llmpod.apps.manager.deployment import orchestrator as orch_mod
from llmpod.apps.manager.deployment.schemas import InstanceSpec


class _FakeAdapter(EngineAdapter):
    """非 vLLM 的假引擎：声明一套不同于 vLLM 的容器默认值。"""

    supported_categories = ["llm"]
    supported_gpu_archs = ["nvidia"]
    supported_features = ["pd"]
    supported_protocols = ["openai"]
    entrypoint = ["fake-serve"]
    # 故意与 vLLM 不同，验证编排器用适配器的值
    default_port = 9000
    default_network_mode = "bridge"
    default_ipc = None  # None → spec 不含该字段
    default_shm_size = "8g"
    default_restart_policy = "unless-stopped"
    default_protocol = "openai"

    def generate_start_args(self, instance, topology, engine_params, *, engine_version=None):
        return ["--serve"]

    @classmethod
    def build_device_config(cls, instance, gpu_arch):
        from llmpod.apps.manager.deployment.schemas import DeviceConfig

        return DeviceConfig(runtime=None, gpus=[0], driver_mounts=[], privileged=False, cap_add=[])

    @classmethod
    def build_env(cls, instance, topology, engine_config, health_timeout_s=600):
        return {}


class TestBuildRunSpecAdapterDefaults:
    def test_spec_uses_adapter_defaults_not_vllm(self, monkeypatch):
        monkeypatch.setattr(orch_mod, "get_adapter", lambda engine: _FakeAdapter())
        dep = SimpleNamespace(
            deployment_id="dep-x", engine="fake", spec={"topology": {"tp": 1}, "image": "fake/engine:latest"}
        )
        model = SimpleNamespace()
        inst = InstanceSpec(
            instance_id="i1", assigned_node="n1", assigned_gpus=[0], model_path="/data/m", role="full", port=9000
        )

        spec = orch_mod.build_run_spec(dep, model, inst)

        # 适配器声明的默认值生效（非 vLLM 的 host/16g/no）
        assert spec.network_mode == "bridge"
        assert spec.shm_size == "8g"
        assert spec.restart_policy == "unless-stopped"
        assert spec.entrypoint == ["fake-serve"]
        # None 的字段不进 spec（default_ipc=None → 不设置，不泄漏 vLLM 的 host）
        assert spec.ipc is None
        # 设备配置来自适配器 build_device_config
        assert spec.gpus == [0]

    def test_vllm_defaults_unchanged(self):
        # 回归：VLLMAdapter 仍声明 host/ipc=host/16g/no/8000
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter

        a = VLLMAdapter()
        assert a.default_port == 8000
        assert a.default_network_mode == "host"
        assert a.default_ipc == "host"
        assert a.default_shm_size == "16g"
        assert a.default_restart_policy == "no"
        assert a.default_protocol == "openai"

    def test_base_defaults_neutral(self):
        # 基类默认值中性，不暗带 vLLM 值（防泄漏的根本）
        assert EngineAdapter.default_port == 0
        assert EngineAdapter.default_network_mode is None
        assert EngineAdapter.default_ipc is None
        assert EngineAdapter.default_protocol is None


class TestModelVolumes:
    """模型卷口径：单文件权重挂所在目录（配套件 mmproj/分片随目录进容器）。"""

    def test_dir_mounts_itself(self):
        assert orch_mod.model_volumes("/data/models/m-abc") == {"/data/models/m-abc": "/data/models/m-abc"}

    def test_file_mounts_parent_dir(self):
        assert orch_mod.model_volumes("/data/models/m-abc/model.gguf") == {"/data/models/m-abc": "/data/models/m-abc"}

    def test_none(self):
        assert orch_mod.model_volumes(None) is None

    def test_mmproj_outside_model_dir_is_also_mounted(self):
        """``source_subpath`` 收窄主模型到子目录、投影器在仓库根：两个目录都要挂，
        否则容器里打不开投影器（--mmproj 指向未挂载路径）。

        主模型目录已覆盖投影器时只保留父挂载点（不产生嵌套的冗余卷）。
        """
        assert orch_mod.model_volumes("/data/models/repo/Q4_K_M/model.gguf", "/data/models/repo/mmproj-F16.gguf") == {
            "/data/models/repo": "/data/models/repo"
        }
        assert orch_mod.model_volumes("/data/models/repo/model.gguf", "/data/models/other/mmproj-F16.gguf") == {
            "/data/models/repo": "/data/models/repo",
            "/data/models/other": "/data/models/other",
        }

    def test_mmproj_alone_mounts_its_dir(self):
        assert orch_mod.model_volumes(None, "/data/models/repo/mmproj-F16.gguf") == {
            "/data/models/repo": "/data/models/repo"
        }
