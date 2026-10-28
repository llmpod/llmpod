"""docker_run spec → docker SDK kwargs 映射单测（纯函数，不依赖 docker daemon）。

覆盖非直传字段：gpus→device_requests、ipc→ipc_mode、extra_devices→devices、
driver_mounts 并入 volumes、restart_policy str→dict、runtime/privileged/cap_add 直传。
"""

from __future__ import annotations

from llmpod.apps.node.deployers.docker import DockerDeployer
from llmpod.tunnel.schemas import DeploySpec

# 纯翻译函数已搬进 DockerDeployer（staticmethod），保留旧名便于本测试体不改
_spec_to_run_kwargs = DockerDeployer._spec_to_run_kwargs


def _spec(**kw) -> DeploySpec:
    """最小 spec（image+name 必填）+ 覆盖字段。"""
    base = {"image": "vllm/vllm-openai:latest", "name": "c1"}
    base.update(kw)
    return DeploySpec(**base)


class TestSpecToRunKwargs:
    def test_minimal(self):
        kw = _spec_to_run_kwargs(_spec())
        assert kw["image"] == "vllm/vllm-openai:latest"
        assert kw["name"] == "c1"
        assert kw["detach"] is True
        # 无 GPU 时不带 device_requests
        assert "device_requests" not in kw

    def test_gpus_device_list(self):
        kw = _spec_to_run_kwargs(_spec(gpus=[2, 3]))
        dr = kw["device_requests"]
        assert len(dr) == 1
        # docker SDK DeviceRequest 内部 PascalCase key（DeviceIDs）
        assert list(dr[0]["DeviceIDs"]) == ["2", "3"]
        # utility 与 compute 同属一个能力集：NVML 只在 utility 下注入，缺了引擎
        # 探测不到设备（老版 nvidia-container-toolkit 不认环境变量兜底）
        assert dr[0]["Capabilities"] == [["compute", "utility"]]

    def test_gpus_empty_means_all(self):
        # [] 空表 = 全部 GPU（docker count=-1）
        kw = _spec_to_run_kwargs(_spec(gpus=[]))
        dr = kw["device_requests"]
        assert len(dr) == 1
        assert dr[0]["Count"] == -1
        assert dr[0]["Capabilities"] == [["compute", "utility"]]

    def test_gpus_none(self):
        kw = _spec_to_run_kwargs(_spec(gpus=None))
        assert "device_requests" not in kw

    def test_ipc_renamed(self):
        kw = _spec_to_run_kwargs(_spec(ipc="host"))
        assert kw["ipc_mode"] == "host"
        assert "ipc" not in kw

    def test_extra_devices_renamed(self):
        kw = _spec_to_run_kwargs(_spec(extra_devices=["/dev/x:/dev/x:rwm"]))
        assert kw["devices"] == ["/dev/x:/dev/x:rwm"]
        assert "extra_devices" not in kw

    def test_args_to_command(self):
        kw = _spec_to_run_kwargs(_spec(args=["serve", "--port", "8000"]))
        assert kw["command"] == ["serve", "--port", "8000"]

    def test_entrypoint(self):
        kw = _spec_to_run_kwargs(_spec(entrypoint=["vllm", "serve"]))
        assert kw["entrypoint"] == ["vllm", "serve"]

    def test_env_to_environment(self):
        kw = _spec_to_run_kwargs(_spec(env={"HF_ENDPOINT": "https://hf-mirror.com"}))
        assert kw["environment"] == {"HF_ENDPOINT": "https://hf-mirror.com"}

    def test_ports_inverted_to_sdk_shape(self):
        """DeploySpec.ports 是 {host: cont}，SDK 的 ports kwarg 是 {container: host}。

        方向反了会让服务发布到错误的端口（且与 docker_run_command 的 -p host:cont 矛盾）。
        """
        kw = _spec_to_run_kwargs(_spec(ports={"18000": 8000}))
        assert kw["ports"] == {"8000": 18000}

    def test_volumes_and_driver_mounts_merged(self):
        kw = _spec_to_run_kwargs(
            _spec(
                volumes={"/data/models": "/models"},
                driver_mounts=["/usr/local/Ascend/driver:/usr/local/Ascend/driver:ro"],
            )
        )
        vol = kw["volumes"]
        assert vol["/data/models"] == {"bind": "/models", "mode": "rw"}
        assert vol["/usr/local/Ascend/driver"] == {"bind": "/usr/local/Ascend/driver", "mode": "ro"}

    def test_volumes_model_host_prefix_untouched_by_default(self, monkeypatch):
        # 默认 model_host_store_dir 为空 → 改写恒等（存量部署零影响）
        monkeypatch.setattr("llmpod.apps.node.deployers.docker.settings.model_store_dir", "/data/models", raising=False)
        monkeypatch.setattr("llmpod.apps.node.deployers.docker.settings.model_host_store_dir", "", raising=False)
        kw = _spec_to_run_kwargs(_spec(volumes={"/data/models/m-abc": "/data/models/m-abc"}))
        assert kw["volumes"]["/data/models/m-abc"] == {"bind": "/data/models/m-abc", "mode": "rw"}

    def test_volumes_model_host_prefix_rewritten(self, monkeypatch):
        # 大盘在别处：canonical 前缀改写为宿主真实路径，容器内 bind 不变
        monkeypatch.setattr("llmpod.apps.node.deployers.docker.settings.model_store_dir", "/data/models")
        monkeypatch.setattr("llmpod.apps.node.deployers.docker.settings.model_host_store_dir", "/mnt/bigdisk/models")
        kw = _spec_to_run_kwargs(
            _spec(
                volumes={"/data/models/m-abc": "/data/models/m-abc"},
                driver_mounts=["/usr/local/Ascend/driver:/usr/local/Ascend/driver:ro"],
            )
        )
        vol = kw["volumes"]
        assert vol["/mnt/bigdisk/models/m-abc"] == {"bind": "/data/models/m-abc", "mode": "rw"}
        assert "/data/models/m-abc" not in vol
        # driver_mounts 的 src 是宿主路径，不参与改写
        assert vol["/usr/local/Ascend/driver"] == {"bind": "/usr/local/Ascend/driver", "mode": "ro"}

    def test_rewrite_model_host_path_boundaries(self):
        # 纯函数边界：store_dir 本身 / 子路径 / 非前缀路径 / 尾斜杠
        f = DockerDeployer._rewrite_model_host_path
        assert f("/data/models", "/data/models", "/mnt/models") == "/mnt/models"
        assert f("/data/models/x/y", "/data/models", "/mnt/models/") == "/mnt/models/x/y"
        assert f("/data/models-other/x", "/data/models", "/mnt/models") == "/data/models-other/x"
        assert f("/etc/passwd", "/data/models", "/mnt/models") == "/etc/passwd"
        # host_store_dir 与 store_dir 相同 → 恒等（含 None/空入参）
        assert f("/data/models/x", "/data/models", "/data/models") == "/data/models/x"
        assert f("/data/models/x", "", "/mnt/models") == "/data/models/x"

    def test_restart_policy_str_to_dict(self):
        kw = _spec_to_run_kwargs(_spec(restart_policy="no"))
        assert kw["restart_policy"] == {"Name": "no"}

    def test_restart_policy_none(self):
        kw = _spec_to_run_kwargs(_spec(restart_policy=None))
        assert "restart_policy" not in kw

    def test_arch_keyed_device_config(self):
        """arch-keyed DeviceConfig 字段直传。"""
        kw = _spec_to_run_kwargs(
            _spec(
                runtime="nvidia",
                privileged=False,
                cap_add=["SYS_ADMIN"],
                shm_size="16g",
                network_mode="host",
            )
        )
        assert kw["runtime"] == "nvidia"
        assert kw["shm_size"] == "16g"
        assert kw["network_mode"] == "host"
        assert kw["cap_add"] == ["SYS_ADMIN"]
        # privileged=False 不进 kwargs（默认值省略）
        assert "privileged" not in kw

    def test_privileged_true(self):
        kw = _spec_to_run_kwargs(_spec(privileged=True))
        assert kw["privileged"] is True

    def test_labels_values_stringified(self):
        # DeploySpec.labels 为 dict[str, str]（value 必须 string），非 str 值在 model 边界即被拒；
        # 这里验证数值形 label 经 deployer 的 str() 兜底后仍为 string。
        kw = _spec_to_run_kwargs(_spec(labels={"llmpod.managed": "true", "llmpod.replica": "2"}))
        assert kw["labels"]["llmpod.managed"] == "true"
        assert kw["labels"]["llmpod.replica"] == "2"  # str 兜底后仍为 string
