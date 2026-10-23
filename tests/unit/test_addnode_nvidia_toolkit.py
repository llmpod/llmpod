"""NVIDIA Docker 运行时配置测试；不访问宿主 Docker 配置。"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "addnode_template.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _run(tmp_path: Path, *, ready: bool = False, restart_fails: bool = False, skip: bool = False):
    source = TEMPLATE.read_text()
    functions = []
    for name in ("install_nvidia_toolkit", "nvidia_runtime_available", "prepare_nvidia_runtime"):
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", source, re.M | re.S)
        assert match
        functions.append(match.group(0))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    config = tmp_path / "daemon.json"
    config.write_text('{"log-level":"warn"}\n')
    runtime = tmp_path / "runtime-ready"
    if ready:
        runtime.touch()
    calls = tmp_path / "calls.log"
    for name in ("cp", "rm", "mktemp", "grep"):
        (bin_dir / name).symlink_to(shutil.which(name))
    _stub(bin_dir / "id", 'printf "0\\n"\n')
    _stub(
        bin_dir / "docker",
        'if [ "$1" = "info" ] && [ "$2" = "--format" ]; then\n'
        '    if [ -f "$MOCK_RUNTIME" ]; then printf \'{"nvidia":{}}\\n\'; else printf \'{"runc":{}}\\n\'; fi\n'
        "    exit 0\n"
        "fi\n"
        '[ "$1" = "info" ]\n',
    )
    _stub(
        bin_dir / "nvidia-ctk",
        'printf "nvidia-ctk %s\\n" "$*" >> "$MOCK_LOG"\n'
        'printf \'{"runtimes":{"nvidia":{}}}\\n\' > "$MOCK_CONFIG"\n'
        ': > "$MOCK_RUNTIME"\n',
    )
    _stub(
        bin_dir / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$MOCK_LOG"\nif [ "$MOCK_RESTART_FAIL" = "1" ]; then exit 1; fi\n',
    )
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "MOCK_CONFIG": str(config),
        "MOCK_RUNTIME": str(runtime),
        "MOCK_LOG": str(calls),
        "MOCK_RESTART_FAIL": "1" if restart_fails else "0",
        "LLMPOD_BOOTSTRAP_ASSUME_YES": "1",
    }
    env.pop("LLMPOD_SKIP_NVIDIA_TOOLKIT_INSTALL", None)
    if skip:
        env["LLMPOD_SKIP_NVIDIA_TOOLKIT_INSTALL"] = "yes"
    script = "\n".join(functions) + f"\nprepare_nvidia_runtime {shlex.quote(str(config))}\n"
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
    return result, config.read_text(), calls.read_text() if calls.exists() else ""


def test_existing_nvidia_runtime_is_left_alone(tmp_path):
    result, config, calls = _run(tmp_path, ready=True)
    assert result.returncode == 0
    assert config == '{"log-level":"warn"}\n'
    assert calls == ""


def test_toolkit_configures_docker_runtime(tmp_path):
    result, config, calls = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"nvidia"' in config
    assert "nvidia-ctk runtime configure --runtime=docker --config=" in calls
    assert "systemctl restart docker" in calls


def test_failed_restart_restores_docker_config(tmp_path):
    result, config, calls = _run(tmp_path, restart_fails=True)
    assert result.returncode != 0
    assert config == '{"log-level":"warn"}\n'
    assert "已恢复 Docker 配置" in result.stdout
    assert calls.count("systemctl restart docker") == 2


def test_toolkit_install_can_be_disabled(tmp_path):
    result, config, calls = _run(tmp_path, skip=True)
    assert result.returncode != 0
    assert config == '{"log-level":"warn"}\n'
    assert calls == ""


def _run_bootstrap(tmp_path: Path, *, gpu_works: bool, node_name: str | None = None, prompt_input: str | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker.log"
    _stub(
        bin_dir / "docker",
        'printf "%s\\n" "$*" >> "$MOCK_LOG"\n'
        'case "$1" in\n'
        '    info) if [ "$2" = "--format" ]; then printf \'{"nvidia":{}}\\n\'; fi ;;\n'
        '    run) if [ "$2" = "--rm" ] && [ "$MOCK_GPU_FAIL" = "1" ]; then exit 1; fi; printf "mock-id\\n" ;;\n'
        "esac\n",
    )
    _stub(bin_dir / "nvidia-smi", 'printf "Mock GPU\\n"\n')
    _stub(bin_dir / "hostname", 'printf "cloned-host\\n"\n')
    # curl 桩记录实际探测的 URL：候选带 /ws/node 固定路径，健康探测必须剥路径后
    # 打根 /health（此前桩成无路径 URL + exit 0，掩盖了拼接缺陷）
    _stub(
        bin_dir / "curl",
        'printf "%s\\n" "$*" >> "$MOCK_LOG"\nexit 0\n',
    )
    values = {
        "node_token": "test-token",
        "manager_ws_url": "ws://manager.example:8080",
        "manager_ws_candidates": "'ws://manager.example.example:8080/ws/node' 'ws://10.0.0.5:8080/ws/node'",
        "cluster_id": "1",
        "node_ip": "10.0.0.5",
        "container_name": "llmpod-test-node",
        "data_volume": "llmpod-test-data",
        "model_dir": str(tmp_path / "models"),
        "model_host_dir": str(tmp_path / "models"),
        "extra_mounts": "",
        "image": "llmpod:test",
    }
    script = TEMPLATE.read_text()
    for key, value in values.items():
        script = script.replace("{{" + key + "}}", value)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "MOCK_LOG": str(calls),
        "MOCK_GPU_FAIL": "0" if gpu_works else "1",
        "LLMPOD_BOOTSTRAP_ASSUME_YES": "1",
    }
    for name in ("LLMPOD_NODE_IMAGE", "LLMPOD_MODEL_HOST_DIR", "LLMPOD_NODE_CONTAINER_NAME", "LLMPOD_NODE_NAME"):
        env.pop(name, None)
    if node_name is not None:
        env["LLMPOD_NODE_NAME"] = node_name
    if prompt_input is None:
        result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
    else:
        env.pop("LLMPOD_BOOTSTRAP_ASSUME_YES")
        script_path = tmp_path / "bootstrap.sh"
        script_path.write_text(script)
        result = subprocess.run(
            ["script", "-q", "-e", "-c", f"/bin/bash {shlex.quote(str(script_path))}", "/dev/null"],
            input=prompt_input,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_gpu_probe_fails_before_replacing_node(tmp_path):
    result, calls = _run_bootstrap(tmp_path, gpu_works=False)
    assert result.returncode != 0
    assert "NVIDIA 容器自检失败" in result.stdout
    assert any(call.startswith("run --rm --runtime=nvidia --gpus all") for call in calls)
    assert not any(call.startswith("run -d") or call.startswith("rm -f") for call in calls)


def test_gpu_probe_allows_node_start(tmp_path):
    result, calls = _run_bootstrap(tmp_path, gpu_works=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call.startswith("run --rm --runtime=nvidia --gpus all") for call in calls)
    assert any(call.startswith("run -d") and "--runtime=nvidia --gpus all" in call for call in calls)
    # 健康探测 URL 剥掉了候选的 /ws/node 路径，打在根 /health 上
    assert any("http://manager.example.example:8080/health" in call for call in calls), calls
    assert not any("/ws/node/health" in call for call in calls), calls


def test_node_name_defaults_to_host_and_reaches_container(tmp_path):
    result, calls = _run_bootstrap(tmp_path, gpu_works=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call.startswith("run -d") and "-e LLMPOD_NODE_NAME=cloned-host" in call for call in calls)


def test_custom_node_name_reaches_container(tmp_path):
    result, calls = _run_bootstrap(tmp_path, gpu_works=True, node_name="gpu-west-2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call.startswith("run -d") and "-e LLMPOD_NODE_NAME=gpu-west-2" in call for call in calls)


def test_invalid_node_name_stops_before_docker(tmp_path):
    result, calls = _run_bootstrap(tmp_path, gpu_works=True, node_name="bad name")
    assert result.returncode != 0
    assert "节点名称格式无效" in result.stdout
    assert calls == []


@pytest.mark.parametrize("answer, expected", [("", "cloned-host"), ("gpu-west-2", "gpu-west-2")])
def test_interactive_node_name_accepts_default_or_edit(tmp_path, answer, expected):
    if not shutil.which("script"):
        pytest.skip("需要 script 命令模拟终端")
    result, calls = _run_bootstrap(tmp_path, gpu_works=True, prompt_input=answer + "\n" * 7)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "节点名称（默认值: cloned-host，回车确认）" in result.stdout
    assert any(call.startswith("run -d") and f"-e LLMPOD_NODE_NAME={expected}" in call for call in calls)
