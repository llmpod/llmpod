"""节点 Shell 执行审计记录完整命令。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmpod.apps.webserver.routes.admin import orchestration
from llmpod.apps.webserver.schemas.orchestration import ShellRequest


async def test_execute_shell_audits_plaintext_command(monkeypatch):
    records = []

    async def execute(*_args):
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def audit(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(orchestration.dispatch, "execute_shell", execute)
    monkeypatch.setattr(orchestration, "write_audit", audit)
    command = "curl -H 'Authorization: Bearer private' https://example.invalid"
    await orchestration.execute_shell(
        "node-1",
        ShellRequest(command=command, timeout=7, cwd="/tmp"),
        user=SimpleNamespace(user_id=1, username="admin", tenant_id=None),
    )
    detail = records[0]["detail"]
    assert detail["command"] == command
    assert detail["timeout"] == 7 and detail["cwd"] == "/tmp"
    assert records[0]["result"] == "success"


async def test_execute_shell_failure_audits_plaintext_command(monkeypatch):
    records = []

    async def execute(*_args):
        raise RuntimeError("node unavailable")

    async def audit(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(orchestration.dispatch, "execute_shell", execute)
    monkeypatch.setattr(orchestration, "write_audit", audit)
    command = "echo hello"
    with pytest.raises(RuntimeError, match="node unavailable"):
        await orchestration.execute_shell(
            "node-1",
            ShellRequest(command=command),
            user=SimpleNamespace(user_id=1, username="admin", tenant_id=None),
        )

    assert records[0]["result"] == "failed"
    assert records[0]["detail"]["command"] == command
    assert records[0]["detail"]["error_type"] == "RuntimeError"


async def test_docker_run_audits_image_and_container(monkeypatch):
    """docker-run 与 shell 同口径落审计（复审发现此前仅 shell 有）。"""
    from llmpod.apps.webserver.schemas.orchestration import ContainerRunRequest

    records = []

    async def run_container(_node_id, spec):
        return {"container_id": "abc"}

    async def audit(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(orchestration.dispatch, "deploy_container", run_container)
    monkeypatch.setattr(orchestration, "write_audit", audit)
    await orchestration.deploy(
        "node-1",
        ContainerRunRequest(image="nginx:latest", name="workload-1", args=["--port", "80"]),
        user=SimpleNamespace(user_id=1, username="admin", tenant_id=None),
    )

    detail = records[0]["detail"]
    assert records[0]["result"] == "success"
    assert detail["operation"] == "docker-run"
    assert detail["image"] == "nginx:latest"
    assert detail["container_name"] == "workload-1"
    assert detail["args"] == ["--port", "80"]


async def test_docker_pull_audits_image(monkeypatch):
    from llmpod.apps.webserver.schemas.orchestration import DockerPullRequest

    records = []

    async def pull_stream(_node_id, _image):
        async def _gen():
            yield "Pulling..."

        return _gen()

    async def audit(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(orchestration.dispatch, "docker_pull_stream", pull_stream)
    monkeypatch.setattr(orchestration, "write_audit", audit)
    response = await orchestration.docker_pull(
        "node-1",
        DockerPullRequest(image="nginx:latest"),
        user=SimpleNamespace(user_id=1, username="admin", tenant_id=None),
    )

    assert records[0]["result"] == "success"
    assert records[0]["detail"] == {"operation": "docker-pull", "image": "nginx:latest"}
    assert response.headers["X-Image"] == "nginx:latest"


async def test_docker_pull_failure_audits_error_type(monkeypatch):
    from llmpod.apps.webserver.schemas.orchestration import DockerPullRequest

    records = []

    async def pull_stream(_node_id, _image):
        raise RuntimeError("node offline")

    async def audit(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(orchestration.dispatch, "docker_pull_stream", pull_stream)
    monkeypatch.setattr(orchestration, "write_audit", audit)
    with pytest.raises(RuntimeError, match="node offline"):
        await orchestration.docker_pull(
            "node-1",
            DockerPullRequest(image="nginx:latest"),
            user=SimpleNamespace(user_id=1, username="admin", tenant_id=None),
        )

    assert records[0]["result"] == "failed"
    assert records[0]["detail"]["error_type"] == "RuntimeError"
