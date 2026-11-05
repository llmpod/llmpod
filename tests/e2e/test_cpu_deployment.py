"""真实 ARM CPU 推理：上传 GGUF → 分发/部署 → 自动及手工发布/Key → 推理 → stop/start。

运行前设置 LLMPOD_E2E=1、LLMPOD_E2E_BASE_URL、LLMPOD_E2E_PROXY_BASE_URL、
LLMPOD_E2E_ADMIN_USER/PASSWORD，并显式提供：
  LLMPOD_E2E_CPU_MODEL_FILE：测试机可读的 stories15M-q4_0.gguf 文件（经 HTTP 上传）
  LLMPOD_E2E_CPU_NODE_ID：目标栈中在线 ARM64 CPU 节点的 ID
可选 LLMPOD_E2E_CPU_IMAGE、LLMPOD_E2E_CPU_DEPLOY_TIMEOUT（默认 900 秒）、
LLMPOD_E2E_CPU_INFERENCE_TIMEOUT（默认 300 秒）。不预置 canonical 目录或伪造元数据。

手工发布成员在当前 restart 实现中可能被删除：先记录现场，再显式重绑新端点；
此限制单独写入测试属性，重绑后的成功不代表手工发布能够自动恢复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import httpx
import pytest

from _client import ok

MODEL_FILE = os.environ.get("LLMPOD_E2E_CPU_MODEL_FILE", "")
NODE_ID = os.environ.get("LLMPOD_E2E_CPU_NODE_ID", "")
IMAGE = os.environ.get("LLMPOD_E2E_CPU_IMAGE", "ghcr.io/ggml-org/llama.cpp:server")
DEPLOY_TIMEOUT = float(os.environ.get("LLMPOD_E2E_CPU_DEPLOY_TIMEOUT", "900"))
INFERENCE_TIMEOUT = float(os.environ.get("LLMPOD_E2E_CPU_INFERENCE_TIMEOUT", "300"))

pytestmark = pytest.mark.skipif(
    not (MODEL_FILE and NODE_ID),
    reason="需显式配置 LLMPOD_E2E_CPU_MODEL_FILE 和 LLMPOD_E2E_CPU_NODE_ID",
)


def _stage(message: str) -> None:
    print(f"[cpu-e2e] {message}", flush=True)


def _wait(label, probe, timeout=120):
    deadline = time.monotonic() + timeout
    next_update = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        if time.monotonic() >= next_update:
            _stage(f"等待 {label}")
            next_update = time.monotonic() + 30
        time.sleep(10)
    pytest.fail(f"等待 {label} 超时（{timeout:g} 秒）")


def _wait_deployment(client, deployment_id, status):
    previous = None

    def probe():
        nonlocal previous
        detail = ok(client, "GET", f"/api/v1/admin/model-deployments/{deployment_id}", timeout=60)
        dep = detail["deployment"]
        states = [i["status"] for i in detail["instances"]]
        summary = (dep["status"], dep.get("phase"), states)
        if summary != previous:
            _stage(f"部署 {deployment_id}: {summary}")
            previous = summary
        assert dep["status"] != "failed", f"部署失败: {dep.get('last_error')}"
        if dep["status"] != status or not states:
            return None
        if status == "stopped":
            return detail if all(s == "STOPPED" for s in states) else None
        healthy = any(ep["protocol"] == "openai" and ep["health_status"] == "healthy" for ep in detail["endpoints"])
        return detail if all(s == "READY" for s in states) and healthy else None

    return _wait(f"部署 {status}", probe, DEPLOY_TIMEOUT)


def _assert_cpu_serving(detail, model_name):
    assert detail["deployment"]["engine"] == "llamacpp"
    assert len(detail["instances"]) == 1
    instance = detail["instances"][0]
    assert instance["assigned_node"] == NODE_ID
    assert instance["status"] == "READY" and instance["container_id"]
    spec = instance["spec"]
    assert spec["gpu_arch"] == "cpu"
    assert spec["assigned_gpus"] == []
    assert not spec.get("assigned_gpu_uuids")
    assert spec["model_path"].lower().endswith(".gguf")
    endpoint = next(ep for ep in detail["endpoints"] if ep["protocol"] == "openai")
    assert endpoint["health_status"] == "healthy"
    assert endpoint["served_model"] == model_name
    return endpoint


def _check_diagnostics(client, deployment_id, detail):
    base = f"/api/v1/admin/model-deployments/{deployment_id}"
    logs = ok(client, "GET", base + "/logs", params={"page_size": 500}, timeout=60)
    assert logs["items"], "部署日志为空"
    staging = ok(client, "GET", base + "/logs", params={"step": "staging_model"}, timeout=60)
    assert staging["total"] > 0, "未记录模型分发阶段"
    assert ok(client, "GET", base + "/logs/nodes", timeout=60)
    containers = ok(client, "GET", base + "/logs/containers", params={"tail": 100}, timeout=60)
    assert containers, "容器日志列表为空"
    for item in containers:
        assert not item["error"], f"容器日志不可读: {item['error']}"
        assert isinstance(item["logs"], str) and item["logs"].strip()
    for instance in detail["instances"]:
        command = ok(client, "GET", base + f"/instances/{instance['instance_id']}/command", timeout=60)
        assert command["found"] is True, "没有实际下发的启动任务快照"
        assert command["image"] == IMAGE
        argv = command["command"] or ""
        assert "docker run" in argv and ".gguf" in argv
        assert "--gpus" not in argv and "--runtime=nvidia" not in argv
    _stage("部署日志、容器日志与实际 CPU 启动命令可读")


def _check_usage(usage):
    assert isinstance(usage, dict), "推理响应缺 usage"
    assert usage["prompt_tokens"] > 0
    assert 0 < usage["completion_tokens"] <= 8
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def _infer(proxy, alias, *, stream=False, path="/v1/chat/completions"):
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "Once upon a time"}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": stream,
    }
    if not stream:
        response = proxy.post(path, json=body, timeout=INFERENCE_TIMEOUT)
        assert response.status_code == 200, f"非流式推理 HTTP {response.status_code}: {response.text[:500]}"
        data = response.json()
        assert data["model"] == alias
        assert data["choices"][0]["message"]["content"].strip(), "没有实际生成内容"
        assert data["choices"][0]["finish_reason"] is not None
        usage = data.get("usage")
        request_id = response.headers.get("x-request-id")
    else:
        body["stream_options"] = {"include_usage": True}
        chunks = []
        usage = None
        done = False
        finished = False
        with proxy.stream("POST", path, json=body, timeout=INFERENCE_TIMEOUT) as response:
            assert response.status_code == 200, f"流式推理 HTTP {response.status_code}"
            assert "text/event-stream" in response.headers.get("content-type", "")
            request_id = response.headers.get("x-request-id")
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    # 与 OpenAI SDK 一样在 [DONE] 后关闭连接，结算不能依赖继续读到 EOF。
                    break
                data = json.loads(payload)
                assert "error" not in data, "SSE 返回错误事件"
                if "model" in data:
                    assert data["model"] == alias
                if data.get("usage"):
                    usage = data["usage"]
                for choice in data.get("choices", []):
                    chunks.append(choice.get("delta", {}).get("content") or "")
                    finished = finished or choice.get("finish_reason") is not None
        assert done and finished, "SSE 未正常结束"
        assert "".join(chunks).strip(), "SSE 没有实际生成内容"
    assert request_id, "网关未返回 x-request-id"
    _check_usage(usage)
    _stage(f"{'流式' if stream else '非流式'}推理成功: request={request_id}, tokens={usage['total_tokens']}")
    return request_id, usage


def _check_request_logs(client, key_id, alias, expected):
    def probe():
        params = {"model": alias, "page_size": 100}
        if key_id is not None:
            params["api_key_id"] = key_id
        page = ok(
            client,
            "GET",
            "/api/v1/admin/request-logs",
            params=params,
            timeout=60,
        )
        rows = {row["request_id"]: row for row in page["items"]}
        return rows if expected.keys() <= rows.keys() else None

    rows = _wait("推理计量日志落库", probe)
    for request_id, (usage, endpoint_id) in expected.items():
        row = rows[request_id]
        assert row["status"] == "success" and row["http_status"] == 200
        assert row["tenant_id"] is None
        if key_id is not None:
            assert row["api_key_id"] == key_id
        assert row["upstream_type"] == "local" and row["engine"] == "llamacpp"
        assert row["endpoint_id"] == endpoint_id and row["model_alias"] == alias
        assert row["prompt_tokens"] == usage["prompt_tokens"]
        assert row["completion_tokens"] == usage["completion_tokens"]
        assert row["total_tokens"] == usage["total_tokens"]
        detail = ok(client, "GET", f"/api/v1/admin/request-logs/{request_id}", timeout=60)
        assert detail["request_id"] == request_id
    _stage(f"{len(expected)} 条真实推理日志与端点、usage 对账成功")


def _publish(client, route_id):
    rows = ok(client, "GET", "/api/v1/admin/publishes", timeout=60)
    return next(row for row in rows if row["route"]["id"] == route_id)


def _wait_auto_publish(client, alias, endpoint_id, route_ids):
    def probe():
        rows = ok(client, "GET", "/api/v1/admin/publishes", timeout=60)
        published = next((row for row in rows if row["route"]["name"] == alias), None)
        if published is None:
            return None
        route = published["route"]
        if route["id"] not in route_ids:
            route_ids.append(route["id"])
        assert route["origin"] == "auto"
        if route["status"] != "published":
            return None
        if any(b["endpoint_id"] == endpoint_id and b["enabled"] for b in published["bindings"]):
            return published
        return None

    published = _wait(f"自动发布 {alias} 绑定端点 {endpoint_id}", probe)
    assert {b["endpoint_id"] for b in published["bindings"] if b["enabled"]} == {endpoint_id}
    _stage(f"自动发布已就绪: route={published['route']['id']}, endpoint={endpoint_id}")
    return published


def _delete(client, path, *, can_get=False):
    deadline = time.monotonic() + min(DEPLOY_TIMEOUT, 300)
    last = "未发起"
    while time.monotonic() < deadline:
        try:
            if can_get:
                current = client.get(path, timeout=60)
                if current.status_code == 404:
                    return None
                if current.status_code == 200:
                    data = current.json()["data"]
                    if data.get("deployment", {}).get("status") == "deleting":
                        last = "deleting"
                        time.sleep(3)
                        continue
            response = client.post(path + "/delete", timeout=60)
            last = f"HTTP {response.status_code}"
            if response.status_code == 404:
                return None
            if response.status_code == 200 and response.json().get("code") == 200:
                if not can_get:
                    return None
            elif response.status_code not in (409, 500, 502, 503, 504):
                break
        except httpx.TransportError as exc:
            last = type(exc).__name__
        time.sleep(3)
    return f"清理失败 {path}: {last}"


@pytest.fixture
def cpu_resources(admin_client):
    resources = {}
    yield resources
    errors = []
    cleanup = [
        ("key_id", "/api/v1/admin/api-keys", resources.get("key_id"), False),
        ("auto_key_id", "/api/v1/admin/api-keys", resources.get("auto_key_id"), False),
        ("route_id", "/api/v1/admin/publishes", resources.get("route_id"), False),
        ("deployment_id", "/api/v1/admin/model-deployments", resources.get("deployment_id"), True),
    ]
    # 自动路由可能随部署级联删除，或在重启后换 ID；逐个回收，已不存在视为成功。
    for route_id in resources.get("auto_route_ids", []):
        cleanup.append(("auto_route_id", "/api/v1/admin/publishes", route_id, False))
    cleanup.append(("model_id", "/api/v1/admin/models", resources.get("model_id"), True))
    for field, prefix, resource_id, can_get in cleanup:
        if resource_id is not None:
            try:
                error = _delete(admin_client, f"{prefix}/{resource_id}", can_get=can_get)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                error = f"清理失败 {field}={resource_id}: {type(exc).__name__}"
            if error:
                errors.append(error)
            else:
                _stage(f"已清理 {field}={resource_id}")
    if resources.get("upload_path") and not resources.get("model_id"):
        errors.append(f"登记前失败，上传目录尚未归属模型：{resources['upload_path']}")
    assert not errors, "；".join(errors)


def test_cpu_deployment_full_flow(admin_client, uniq, cpu_resources, record_property):
    model_file = Path(MODEL_FILE)
    assert model_file.is_file() and model_file.stat().st_size > 0, "CPU_MODEL_FILE 必须是测试机可读文件"
    assert model_file.suffix.lower() == ".gguf"
    proxy_url = os.environ.get("LLMPOD_E2E_PROXY_BASE_URL", "")
    assert proxy_url, "必须显式设置 LLMPOD_E2E_PROXY_BASE_URL，避免连接其他运行栈"

    def online_node():
        candidate = ok(admin_client, "GET", f"/api/v1/admin/nodes/{NODE_ID}", timeout=60)
        if candidate["status"] != "online":
            return None
        connected = ok(admin_client, "GET", "/api/v1/admin/orchestration/nodes", timeout=60)
        return candidate if any(item["node_id"] == NODE_ID for item in connected) else None

    node = _wait("目标 CPU 节点上线并连接 Manager", online_node, DEPLOY_TIMEOUT)
    assert node["cpu_arch"] in ("aarch64", "arm64"), "目标节点不是 ARM64"
    model_name = uniq("arm64-cpu-model")
    alias = uniq("arm64-cpu-chat")
    auto_alias = uniq("arm64-cpu-auto")
    _stage(f"目标 ARM64 节点 {NODE_ID}，镜像 {IMAGE}")

    manifest = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models/validate-manifest",
        json={"files": [{"name": model_file.name, "size": model_file.stat().st_size}]},
        timeout=60,
    )
    assert not any(w["severity"] == "fatal" for w in manifest["warnings"]), "模型文件清单存在阻断项"
    with model_file.open("rb") as source:
        uploaded = ok(
            admin_client,
            "POST",
            "/api/v1/admin/models/upload",
            params={"name": model_name},
            files={"files": (model_file.name, source, "application/octet-stream")},
            timeout=INFERENCE_TIMEOUT,
        )
    cpu_resources["upload_path"] = uploaded["path"]
    inspected = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models/inspect",
        json={"source": "local", "source_url": uploaded["path"], "model_type": "llm"},
        timeout=INFERENCE_TIMEOUT,
    )["files"]
    assert inspected["success"] and inspected["format"] == "gguf"
    assert inspected["source_kind"] == "single_gguf"
    assert (inspected["size_gb"] or 0) > 0 and (inspected["num_layers"] or 0) > 0
    model = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models",
        json={
            "name": model_name,
            "source": "local",
            "source_url": uploaded["path"],
            "model_type": "llm",
            "files": inspected,
        },
        timeout=60,
    )
    cpu_resources["model_id"] = model["id"]
    _stage(f"模型上传、GGUF 元数据探测与登记完成: model={model['id']}")
    spec = {
        "offload_preference": "cpu_only",
        "topology": {"tp": 1, "replicas": 1, "max_model_len": 512},
        "gpu_selector": {"node_id": NODE_ID},
        "image": IMAGE,
        "engine_params": {"extra_args": ["--threads", "2", "--parallel", "1", "--chat-template", "chatml"]},
    }
    preview = ok(
        admin_client,
        "POST",
        "/api/v1/admin/model-deployments/inspect",
        json={"cluster_id": node["cluster_id"], "model_id": model["id"], "engine": "llamacpp", **spec},
        timeout=INFERENCE_TIMEOUT,
    )
    assert preview["deployable"], f"CPU 部署预检阻断: {preview['issues']}"
    assert preview["memory"] and preview["memory"]["scenario"] == "cpu_only"
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/model-deployments",
        json={
            "cluster_id": node["cluster_id"],
            "model_id": model["id"],
            "name": uniq("arm64-cpu-deploy"),
            "engine": "llamacpp",
            "spec": {**spec, "publish": {"auto_publish": True, "name": auto_alias}},
        },
        timeout=INFERENCE_TIMEOUT,
    )
    deployment_id = cpu_resources["deployment_id"] = created["deployment_id"]
    auto_route_ids = cpu_resources["auto_route_ids"] = []
    detail = _wait_deployment(admin_client, deployment_id, "running")
    endpoint = _assert_cpu_serving(detail, model_name)
    assert not detail["instances"][0]["spec"]["model_path"].startswith(uploaded["path"] + "/")
    _check_diagnostics(admin_client, deployment_id, detail)
    auto_published = _wait_auto_publish(admin_client, auto_alias, endpoint["id"], auto_route_ids)
    auto_route_before = auto_published["route"]["id"]
    published = ok(
        admin_client,
        "POST",
        "/api/v1/admin/publishes",
        json={"name": alias, "model_type": "llm", "context_length": 512, "bindings": [{"endpoint_id": endpoint["id"]}]},
        timeout=60,
    )
    route_id = cpu_resources["route_id"] = published["route"]["id"]
    assert published["route"]["status"] == "published"

    with (
        httpx.Client(base_url=proxy_url, timeout=INFERENCE_TIMEOUT) as proxy,
        httpx.Client(base_url=proxy_url, timeout=INFERENCE_TIMEOUT) as auto_proxy,
    ):
        response = admin_client.post(
            "/api/v1/admin/api-keys", json={"name": uniq("arm64-cpu-key"), "allowed_models": [alias]}, timeout=60
        )
        if response.status_code != 200 or response.json().get("code") != 200:
            pytest.fail(f"平台 Key 签发失败: HTTP {response.status_code}")
        issued = response.json()["data"]
        key_id = cpu_resources["key_id"] = issued["api_key"]["id"]
        if not issued.get("plaintext"):
            pytest.fail("Key 签发响应缺少 plaintext")
        proxy.headers["Authorization"] = "Bearer " + issued.pop("plaintext")
        del issued, response

        response = admin_client.post(
            "/api/v1/admin/api-keys",
            json={"name": uniq("arm64-cpu-auto-key"), "allowed_models": [auto_alias]},
            timeout=60,
        )
        if response.status_code != 200 or response.json().get("code") != 200:
            pytest.fail(f"自动发布专用平台 Key 签发失败: HTTP {response.status_code}")
        issued = response.json()["data"]
        auto_key_id = cpu_resources["auto_key_id"] = issued["api_key"]["id"]
        if not issued.get("plaintext"):
            pytest.fail("自动发布专用 Key 签发响应缺少 plaintext")
        auto_proxy.headers["Authorization"] = "Bearer " + issued.pop("plaintext")
        del issued, response

        def visible():
            models = proxy.get("/v1/models")
            assert models.status_code == 200, f"模型列表 HTTP {models.status_code}"
            return any(item["id"] == alias for item in models.json()["data"])

        _wait("发布别名进入网关模型列表", visible)
        _stage("手工发布、平台 Key 与网关模型列表通过")

        def auto_visible():
            models = auto_proxy.get("/v1/models")
            assert models.status_code == 200, f"自动发布模型列表 HTTP {models.status_code}"
            visible_ids = {item["id"] for item in models.json()["data"]}
            assert visible_ids <= {auto_alias}, "自动发布专用 Key 可见未授权模型"
            return auto_alias in visible_ids

        _wait("自动发布别名进入专用 Key 模型列表", auto_visible)
        auto_request_id, auto_usage = _infer(auto_proxy, auto_alias)
        auto_expected = {auto_request_id: (auto_usage, endpoint["id"])}
        _check_request_logs(admin_client, auto_key_id, auto_alias, auto_expected)
        _stage("默认自动发布、独立 Key 与首次真实 CPU 推理通过")
        expected = {}
        for stream in (False, True):
            request_id, usage = _infer(proxy, alias, stream=stream)
            expected[request_id] = (usage, endpoint["id"])
        _check_request_logs(admin_client, key_id, alias, expected)

        # 管理端在线调试返回原生 OpenAI 响应；内部保留 Key 由服务管理，不属于本测试清理范围。
        debug_models = admin_client.get("/api/v1/admin/inference/models", timeout=INFERENCE_TIMEOUT)
        assert debug_models.status_code == 200, f"管理端模型列表透传 HTTP {debug_models.status_code}"
        assert any(item["id"] == alias for item in debug_models.json()["data"])
        request_id, usage = _infer(admin_client, alias, path="/api/v1/admin/inference/chat/completions")
        _check_request_logs(admin_client, None, alias, {request_id: (usage, endpoint["id"])})
        _stage("管理端在线调试经内部网关完成真实 CPU 推理")

        old_endpoint_id = endpoint["id"]
        before = _publish(admin_client, route_id)
        assert any(b["endpoint_id"] == old_endpoint_id and b["enabled"] for b in before["bindings"])
        _stage(f"停止前: endpoint={old_endpoint_id}, bindings={[b['endpoint_id'] for b in before['bindings']]}")
        base = f"/api/v1/admin/model-deployments/{deployment_id}"
        _stage("停止 CPU 部署")
        ok(admin_client, "POST", base + "/stop", timeout=INFERENCE_TIMEOUT)
        _wait_deployment(admin_client, deployment_id, "stopped")
        _stage("重新启动 CPU 部署")
        ok(admin_client, "POST", base + "/start", timeout=INFERENCE_TIMEOUT)
        restarted = _wait_deployment(admin_client, deployment_id, "running")
        endpoint = _assert_cpu_serving(restarted, model_name)
        assert endpoint["id"] == old_endpoint_id, "重启丢失了原端点身份"
        auto_restored = _wait_auto_publish(admin_client, auto_alias, endpoint["id"], auto_route_ids)
        record_property("auto_route_before_restart", auto_route_before)
        record_property("auto_route_after_restart", auto_restored["route"]["id"])
        _wait("重启后自动发布恢复网关可见", auto_visible)
        # 模型列表只表明 alias 可见；给同一快照内的端点更新留一个完整轮询周期。
        time.sleep(6)
        auto_request_id, auto_usage = _infer(auto_proxy, auto_alias)
        auto_expected[auto_request_id] = (auto_usage, endpoint["id"])
        _check_request_logs(admin_client, auto_key_id, auto_alias, auto_expected)
        record_property("auto_route_restored", True)
        _stage("重启后自动发布恢复 published、保留端点绑定及专用 Key 推理/计量通过")

        after = _publish(admin_client, route_id)
        bound_ids = [b["endpoint_id"] for b in after["bindings"] if b["enabled"]]
        missing = endpoint["id"] not in bound_ids
        record_property("manual_route_rebind_required", missing)
        record_property("endpoint_before_restart", old_endpoint_id)
        record_property("endpoint_after_restart", endpoint["id"])
        record_property("old_endpoint_survived", any(ep["id"] == old_endpoint_id for ep in restarted["endpoints"]))
        record_property("old_binding_survived", old_endpoint_id in bound_ids)
        record_property("bindings_after_restart", json.dumps(bound_ids))
        _stage(f"重启现场: old_endpoint={old_endpoint_id}, new_endpoint={endpoint['id']}, bindings={bound_ids}")
        assert not missing, "重启后手工发布成员丢失"
        assert after["bindings"] == before["bindings"], "重启修改了手工发布成员配置"
        request_id, usage = _infer(proxy, alias)
        expected[request_id] = (usage, endpoint["id"])
        _check_request_logs(admin_client, key_id, alias, expected)
        _check_diagnostics(admin_client, deployment_id, restarted)
        _stage("CPU 部署全流程完成，开始按本次资源 ID 清理")
