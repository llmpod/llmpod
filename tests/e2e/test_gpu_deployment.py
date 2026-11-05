"""真实 NVIDIA 推理：上传、分发、CUDA 上卡、发布、计量及重启。

显式设置 LLMPOD_E2E_GPU_MODEL_FILE / NODE_ID / UUID；可选 GPU_IMAGE（默认
llama.cpp server-cuda12）、GPU_CPU_ARCH。只清理本次创建的资源。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import time

import httpx
import pytest

from _client import ok
from test_cpu_deployment import (
    _check_request_logs,
    _delete,
    _infer,
    _wait,
    _wait_auto_publish,
    _wait_deployment,
)

MODEL_FILE = os.environ.get("LLMPOD_E2E_GPU_MODEL_FILE", "")
NODE_ID = os.environ.get("LLMPOD_E2E_GPU_NODE_ID", "")
GPU_UUID = os.environ.get("LLMPOD_E2E_GPU_UUID", "")
IMAGE = os.environ.get("LLMPOD_E2E_GPU_IMAGE", "ghcr.io/ggml-org/llama.cpp:server-cuda12")

pytestmark = pytest.mark.skipif(
    not (MODEL_FILE and NODE_ID and GPU_UUID),
    reason="需显式配置 LLMPOD_E2E_GPU_MODEL_FILE / NODE_ID / UUID",
)


@pytest.fixture
def gpu_resources(admin_client):
    resources = {}
    yield resources
    cleanup = [
        ("/api/v1/admin/api-keys", resources.get("key_id"), False),
        ("/api/v1/admin/model-deployments", resources.get("deployment_id"), True),
        *(("/api/v1/admin/publishes", rid, False) for rid in resources.get("route_ids", [])),
        ("/api/v1/admin/models", resources.get("model_id"), True),
    ]
    errors = []
    for prefix, resource_id, can_get in cleanup:
        if resource_id is not None:
            try:
                error = _delete(admin_client, f"{prefix}/{resource_id}", can_get=can_get)
                if error:
                    errors.append(error)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                errors.append(f"{prefix}/{resource_id}: {type(exc).__name__}")
    if resources.get("upload_path") and not resources.get("model_id"):
        errors.append(f"上传目录未登记，需清理：{resources['upload_path']}")
    assert not errors, "；".join(errors)


def _gpu_endpoint(client, deployment_id, detail, record_property):
    assert len(detail["instances"]) == 1
    instance = detail["instances"][0]
    assert instance["assigned_node"] == NODE_ID and instance["status"] == "READY"
    assert instance["spec"]["gpu_arch"] == "nvidia"
    assert instance["spec"]["assigned_gpu_uuids"] == [GPU_UUID]
    base = f"/api/v1/admin/model-deployments/{deployment_id}"
    command = ok(client, "GET", base + f"/instances/{instance['instance_id']}/command", timeout=60)
    assert command["found"] and command["image"] == IMAGE
    assert "--gpus" in command["command"] and "--n-gpu-layers" in command["command"]
    logs = ok(client, "GET", base + "/logs/containers", params={"tail": 1000}, timeout=60)
    assert logs and all(not row["error"] for row in logs)
    text = "\n".join(row["logs"] for row in logs)
    offloaded = re.search(r"offloaded (\d+)/(\d+) layers to GPU", text)
    assert offloaded and int(offloaded[1]) > 0, "未证明权重实际上卡（不允许 CPU 回退）"
    assert offloaded[1] == offloaded[2], "没有完成全量 GPU offload"
    assert "CUDA0" in text, "容器日志缺 CUDA 设备"
    record_property("gpu_offloaded_layers", offloaded[0])
    record_property("gpu_container_id", instance["container_id"])
    print(f"[gpu-e2e] CUDA 已就绪: {offloaded[0]}, container={instance['container_id']}", flush=True)
    endpoint = next(ep for ep in detail["endpoints"] if ep["protocol"] == "openai")
    assert endpoint["health_status"] == "healthy"
    return endpoint


def test_gpu_deployment_full_flow(admin_client, uniq, gpu_resources, record_property):
    model_file = Path(MODEL_FILE)
    assert model_file.is_file() and model_file.suffix.lower() == ".gguf"
    proxy_url = os.environ.get("LLMPOD_E2E_PROXY_BASE_URL", "")
    assert proxy_url, "必须显式设置测试网关地址"

    def ready_node():
        node = ok(admin_client, "GET", f"/api/v1/admin/nodes/{NODE_ID}", timeout=60)
        connected = ok(admin_client, "GET", "/api/v1/admin/orchestration/nodes", timeout=60)
        return node if node["status"] == "online" and any(n["node_id"] == NODE_ID for n in connected) else None

    node = _wait("GPU 节点上线并连接 Manager", ready_node, 300)
    if expected_arch := os.environ.get("LLMPOD_E2E_GPU_CPU_ARCH"):
        assert node["cpu_arch"] == expected_arch
    record_property("gpu_node_cpu_arch", node["cpu_arch"])
    record_property("gpu_uuid", GPU_UUID)
    model_name, alias = uniq("physical-gpu-model"), uniq("physical-gpu-chat")
    print(f"[gpu-e2e] 节点 CPU 架构={node['cpu_arch']}, GPU={GPU_UUID}", flush=True)
    with model_file.open("rb") as source:
        uploaded = ok(
            admin_client,
            "POST",
            "/api/v1/admin/models/upload",
            params={"name": model_name},
            files={"files": (model_file.name, source, "application/octet-stream")},
            timeout=300,
        )
    gpu_resources["upload_path"] = uploaded["path"]
    files = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models/inspect",
        json={"source": "local", "source_url": uploaded["path"], "model_type": "llm"},
        timeout=120,
    )["files"]
    assert files["success"] and files["format"] == "gguf" and files["num_layers"] > 0
    model = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models",
        json={
            "name": model_name,
            "source": "local",
            "source_url": uploaded["path"],
            "model_type": "llm",
            "files": files,
        },
        timeout=60,
    )
    gpu_resources["model_id"] = model["id"]
    spec = {
        "offload_preference": "gpu_only",
        "image": IMAGE,
        "topology": {"tp": 1, "replicas": 1, "max_model_len": 512},
        "gpu_selector": {"gpu_uuids": [GPU_UUID]},
        "engine_params": {"extra_args": ["--threads", "2", "--parallel", "1", "--chat-template", "chatml", "-lv", "5"]},
    }
    preview = ok(
        admin_client,
        "POST",
        "/api/v1/admin/model-deployments/inspect",
        json={"cluster_id": node["cluster_id"], "model_id": model["id"], "engine": "llamacpp", **spec},
        timeout=120,
    )
    assert preview["deployable"], f"GPU 预检阻断: {preview['issues']}"
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/model-deployments",
        json={
            "cluster_id": node["cluster_id"],
            "model_id": model["id"],
            "name": uniq("physical-gpu-deploy"),
            "engine": "llamacpp",
            "spec": {**spec, "publish": {"auto_publish": True, "name": alias}},
        },
        timeout=120,
    )
    deployment_id = gpu_resources["deployment_id"] = created["deployment_id"]
    route_ids = gpu_resources["route_ids"] = []
    detail = _wait_deployment(admin_client, deployment_id, "running")
    endpoint = _gpu_endpoint(admin_client, deployment_id, detail, record_property)
    assert endpoint["served_model"] == model_name
    staging = ok(
        admin_client,
        "GET",
        f"/api/v1/admin/model-deployments/{deployment_id}/logs",
        params={"step": "staging_model"},
        timeout=60,
    )
    assert staging["total"] > 0, "没有真实模型分发记录"
    _wait_auto_publish(admin_client, alias, endpoint["id"], route_ids)
    response = admin_client.post(
        "/api/v1/admin/api-keys",
        json={"name": uniq("physical-gpu-key"), "allowed_models": [alias]},
        timeout=60,
    )
    assert response.status_code == 200 and response.json().get("code") == 200
    issued = response.json()["data"]
    key_id = gpu_resources["key_id"] = issued["api_key"]["id"]
    with httpx.Client(base_url=proxy_url, timeout=300) as proxy:
        proxy.headers["Authorization"] = "Bearer " + issued.pop("plaintext")
        del response, issued

        def visible():
            response = proxy.get("/v1/models")
            assert response.status_code == 200
            ids = {row["id"] for row in response.json()["data"]}
            assert ids <= {alias}, "Key 看到了未授权模型"
            return alias in ids

        _wait("GPU 模型发布可见", visible)
        expected = {}
        for stream in (False, True):
            request_id, usage = _infer(proxy, alias, stream=stream)
            expected[request_id] = (usage, endpoint["id"])
        _check_request_logs(admin_client, key_id, alias, expected)
        print("[gpu-e2e] GPU 流式、非流式推理与计量通过", flush=True)
        base = f"/api/v1/admin/model-deployments/{deployment_id}"
        ok(admin_client, "POST", base + "/stop", timeout=120)
        _wait_deployment(admin_client, deployment_id, "stopped")
        ok(admin_client, "POST", base + "/start", timeout=120)
        restarted = _wait_deployment(admin_client, deployment_id, "running")
        endpoint = _gpu_endpoint(admin_client, deployment_id, restarted, record_property)
        _wait_auto_publish(admin_client, alias, endpoint["id"], route_ids)
        time.sleep(6)
        request_id, usage = _infer(proxy, alias)
        expected[request_id] = (usage, endpoint["id"])
        _check_request_logs(admin_client, key_id, alias, expected)
        print("[gpu-e2e] GPU 停止、重启、自动发布恢复与再次推理通过", flush=True)
