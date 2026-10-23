"""docker_run_command 渲染单测（DeploySpec → docker run CLI 投影）。

三层防线：golden（典型 vLLM/SGLang 形态逐 token 比对）；逐字段覆盖（DeploySpec
每个字段设值后断言输出含对应 flag——将来加字段没同步渲染器，此测试必红）；与
DockerDeployer._spec_to_run_kwargs 交叉核对（两套翻译不许漂：kwargs 每个键都能
在渲染结果里找到对应表达）。
"""

from __future__ import annotations

import pytest

from llmpod.tunnel.schemas import DeploySpec, docker_run_command


def _flat(cmd: str) -> str:
    """去续行符压成单行（断言用，视觉换行不参与比对）。"""
    return " ".join(line.strip() for line in cmd.split("\\\n"))


class TestGolden:
    def test_vllm_shape(self):
        spec = DeploySpec(
            image="vllm/vllm-openai:v0.23.0",
            name="llmpod-a1b2c3d4-inst-0",
            args=["--model", "/models/qwen", "--port", "8000", "--tp", "2"],
            env={"HF_ENDPOINT": "https://hf-mirror.com"},
            gpus=[0, 1],
            volumes={"/models/qwen": "/models/qwen"},
            labels={"llmpod.deployment-id": "a1b2c3d4"},
            network_mode="host",
            ipc="host",
            shm_size="16g",
            restart_policy="no",
        )
        cmd = _flat(docker_run_command(spec))
        assert cmd == (
            "docker run -d"
            " --name llmpod-a1b2c3d4-inst-0"
            " --network host"
            " --ipc host"
            " --shm-size 16g"
            " --restart no"
            ' --gpus "device=0,1"'
            " -v /models/qwen:/models/qwen"
            " --label llmpod.deployment-id=a1b2c3d4"
            " -e HF_ENDPOINT=https://hf-mirror.com"
            " vllm/vllm-openai:v0.23.0"
            " --model /models/qwen --port 8000 --tp 2"
        )

    def test_sglang_entrypoint_prefix(self):
        """entrypoint 多元素（SGLang）：不用 --entrypoint flag，直接作 argv 前缀。"""
        spec = DeploySpec(
            image="lmsysorg/sglang:v0.5.17",
            name="llmpod-d1-i0",
            entrypoint=["python3", "-m", "sglang.launch_server"],
            args=["--model", "/models/qwen"],
        )
        cmd = _flat(docker_run_command(spec))
        assert cmd == (
            "docker run -d"
            " --name llmpod-d1-i0"
            " lmsysorg/sglang:v0.5.17"
            " python3 -m sglang.launch_server --model /models/qwen"
        )
        assert "--entrypoint" not in cmd

    def test_minimal_spec(self):
        cmd = _flat(docker_run_command(DeploySpec(image="x", name="y")))
        assert cmd == "docker run -d --name y x"

    def test_line_continuations(self):
        """多段以 ` \\`+换行 拼接：弹窗可读，整段复制到 shell 仍合法。"""
        spec = DeploySpec(image="x", name="y", gpus=[0], env={"A": "1"})
        cmd = docker_run_command(spec)
        assert "\\\n" in cmd
        assert _flat(cmd) == 'docker run -d --name y --gpus "device=0" -e A=1 x'

    def test_shell_metacharacters_quoted(self):
        """env/argv 含空格等元字符的值 shlex.quote（复制执行不裂开）。

        引号包整段 `k=v`（而非只包 value）：与 docker CLI 自身 `docker run -e K=V`
        的整参引号习惯一致，shlex 还原出的 argv 与渲染前逐 token 等价。
        """
        spec = DeploySpec(
            image="x",
            name="y",
            env={"GREETING": "hello world"},
            args=["--msg", "a b'c"],
        )
        cmd = _flat(docker_run_command(spec))
        assert "-e 'GREETING=hello world'" in cmd
        assert "--msg 'a b'\"'\"'c'" in cmd
        # 整段命令 shlex 还原回 token 序列，与 spec 字段逐项对得上
        import shlex

        tokens = shlex.split(cmd)
        assert tokens[tokens.index("-e") + 1] == "GREETING=hello world"
        assert tokens[tokens.index("--msg") + 1] == "a b'c"


class TestPerFieldCoverage:
    """DeploySpec 逐字段设值断言输出含对应 flag——防将来加字段漏渲染。"""

    def _assert(self, spec: DeploySpec, *needles: str) -> None:
        cmd = _flat(docker_run_command(spec))
        for needle in needles:
            assert needle in cmd, f"渲染结果缺 {needle!r}: {cmd}"

    def test_runtime(self):
        self._assert(DeploySpec(image="x", name="y", runtime="nvidia"), "--runtime nvidia")

    def test_network_ipc_shm_restart(self):
        self._assert(
            DeploySpec(image="x", name="y", network_mode="bridge", ipc="host", shm_size="1g", restart_policy="always"),
            "--network bridge",
            "--ipc host",
            "--shm-size 1g",
            "--restart always",
        )

    def test_privileged_cap_add_extra_devices(self):
        self._assert(
            DeploySpec(
                image="x",
                name="y",
                privileged=True,
                cap_add=["SYS_ADMIN", "IPC_LOCK"],
                extra_devices=["/dev/davinci0:/dev/davinci0:rwm"],
            ),
            "--privileged",
            "--cap-add SYS_ADMIN",
            "--cap-add IPC_LOCK",
            "--device /dev/davinci0:/dev/davinci0:rwm",
        )

    @pytest.mark.parametrize(
        "gpus,expected",
        [
            (None, None),  # 不分配：无 --gpus
            ([], "--gpus all"),  # 空表=全部 GPU
            ([0], '--gpus "device=0"'),
            ([0, 2], '--gpus "device=0,2"'),
        ],
    )
    def test_gpus(self, gpus, expected):
        cmd = _flat(docker_run_command(DeploySpec(image="x", name="y", gpus=gpus)))
        if expected is None:
            assert "--gpus" not in cmd
        else:
            assert expected in cmd

    def test_ports_volumes_driver_mounts(self):
        self._assert(
            DeploySpec(
                image="x",
                name="y",
                ports={"18000": 8000},
                volumes={"/models/q": "/models/q"},
                driver_mounts=["/usr/local/Ascend/driver:/usr/local/Ascend/driver:ro"],
            ),
            "-p 18000:8000",
            "-v /models/q:/models/q",
            "-v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro",
        )

    def test_labels_env_entrypoint_args(self):
        self._assert(
            DeploySpec(
                image="x",
                name="y",
                labels={"llmpod.managed": "true"},
                env={"NCCL_DEBUG": "INFO"},
                entrypoint=["python3", "-m", "sglang.launch_server"],
                args=["--tp", "2"],
            ),
            "--label llmpod.managed=true",
            "-e NCCL_DEBUG=INFO",
            "python3 -m sglang.launch_server --tp 2",
        )


class TestDeployerCrossCheck:
    """与 DockerDeployer._spec_to_run_kwargs 交叉核对：kwargs 的每个键（除 detach）
    在渲染结果里都有对应表达——两套翻译（SDK kwargs vs CLI flags）不许漂。"""

    REPRESENTATIVE = DeploySpec(
        image="vllm/vllm-openai:v0.23.0",
        name="llmpod-x",
        entrypoint=["python3", "-m", "sglang.launch_server"],
        args=["--model", "/models/qwen", "--tp", "2"],
        env={"HF_ENDPOINT": "https://hf-mirror.com"},
        gpus=[0, 1],
        ports={"18000": 8000},
        volumes={"/models/qwen": "/models/qwen"},
        driver_mounts=["/usr/local/driver:/usr/local/driver:ro"],
        labels={"llmpod.managed": "true"},
        network_mode="host",
        ipc="host",
        shm_size="16g",
        runtime="nvidia",
        privileged=False,
        cap_add=["IPC_LOCK"],
        extra_devices=["/dev/davinci0:/dev/davinci0:rwm"],
        restart_policy="no",
    )

    def test_every_kwarg_key_has_cli_expression(self):
        from llmpod.apps.node.deployers.docker import DockerDeployer

        kwargs = DockerDeployer._spec_to_run_kwargs(self.REPRESENTATIVE)
        cmd = _flat(docker_run_command(self.REPRESENTATIVE))

        # kwargs 键 → 渲染结果中的对应表达（image/name 在 --name 与尾部都出现）
        expectations = {
            "image": ["vllm/vllm-openai:v0.23.0"],
            "name": ["--name llmpod-x"],
            "entrypoint": ["python3", "-m", "sglang.launch_server"],
            "command": ["--model", "/models/qwen", "--tp", "2"],
            "environment": ["-e HF_ENDPOINT=https://hf-mirror.com"],
            "labels": ["--label llmpod.managed=true"],
            "network_mode": ["--network host"],
            "shm_size": ["--shm-size 16g"],
            "runtime": ["--runtime nvidia"],
            "cap_add": ["--cap-add IPC_LOCK"],
            "ipc_mode": ["--ipc host"],
            "devices": ["--device /dev/davinci0:/dev/davinci0:rwm"],
            "ports": ["-p 18000:8000"],
            "volumes": ["-v /models/qwen:/models/qwen", "-v /usr/local/driver:/usr/local/driver:ro"],
            "device_requests": ['--gpus "device=0,1"'],
            "restart_policy": ["--restart no"],
        }
        # privileged=False 时 kwargs 无该键；渲染也不含 --privileged——两侧一致
        assert ("privileged" in kwargs) == ("--privileged" in cmd)

        for key, needles in expectations.items():
            assert key in kwargs, f"deployer kwargs 缺 {key}（代表性 spec 应触发）"
            for needle in needles:
                assert needle in cmd, f"kwargs[{key}] 有值但渲染缺 {needle!r}"
        # kwargs 不应冒出映射表之外的键（出现即本测试需更新）
        unknown = set(kwargs) - set(expectations) - {"privileged", "detach"}
        assert not unknown, f"deployer kwargs 出现未核对键: {unknown}"
