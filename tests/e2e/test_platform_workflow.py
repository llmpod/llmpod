"""运行栈补充验收：页面数据、节点 RPC、分片上传与文件回收。"""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from _client import expect_error, ok, page


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/api/v1/settings", dict),
        ("/api/v1/proxy-endpoint", dict),
        ("/api/v1/admin/settings", dict),
        ("/api/v1/admin/clusters/topology", list),
        ("/api/v1/admin/orchestration/status", dict),
        ("/api/v1/admin/orchestration/nodes", list),
        ("/api/v1/admin/inference-images", list),
        ("/api/v1/admin/inference-images/engines", list),
        ("/api/v1/admin/models-market", list),
        ("/api/v1/admin/dashboard/summary", dict),
        ("/api/v1/admin/dashboard/screen", dict),
        ("/api/v1/admin/dashboard/trend", list),
        ("/api/v1/admin/dashboard/models", list),
        ("/api/v1/admin/dashboard/tenants", list),
        ("/api/v1/admin/dashboard/realtime", dict),
        ("/api/v1/admin/dashboard/tokens", dict),
        ("/api/v1/admin/dashboard/scrape-health", dict),
        ("/api/v1/admin/dashboard/overview", dict),
        ("/api/v1/admin/dashboard/engines/history", dict),
        ("/api/v1/admin/dashboard/anomaly", dict),
    ],
)
def test_platform_page_data(admin_client, client, tenant_client, path, kind):
    assert isinstance(ok(admin_client, "GET", path), kind)
    if "/admin/" in path:
        expect_error(client, "GET", path, status=401)
        expect_error(tenant_client, "GET", path, status=403)


@pytest.mark.parametrize("resource", ["models", "model-deployments", "audit-logs"])
def test_platform_resource_lists(admin_client, resource):
    data = page(admin_client, "GET", f"/api/v1/admin/{resource}")
    assert data["total"] >= len(data["items"])


@pytest.mark.parametrize("resource", ["dashboard/screen", "wallet", "models", "audit-logs"])
def test_tenant_page_data(tenant_client, resource):
    data = ok(tenant_client, "GET", f"/api/v1/tenant/{resource}")
    assert isinstance(data, list if resource == "models" else dict)


def test_node_rpc_and_audit(admin_client):
    node_id = os.environ.get("LLMPOD_E2E_CPU_NODE_ID")
    if not node_id:
        pytest.skip("需 LLMPOD_E2E_CPU_NODE_ID 指定可执行只读任务的实验节点")
    node = ok(admin_client, "GET", f"/api/v1/admin/nodes/{node_id}")
    root = f"/api/v1/admin/orchestration/nodes/{node_id}"
    info = ok(admin_client, "GET", f"{root}/info")
    assert info["cpu_arch"] == node["cpu_arch"]
    assert info["cpu_cores"] > 0
    assert info["memory_gb"] > 0
    assert isinstance(ok(admin_client, "GET", f"{root}/gpus"), list)
    result = ok(admin_client, "POST", f"{root}/tasks/shell", json={"command": "uname -m", "timeout": 30})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == info["cpu_arch"]
    audit = page(
        admin_client,
        "GET",
        "/api/v1/admin/audit-logs",
        params={"resource_type": "node", "resource_id": node_id, "action": "execute"},
    )
    assert any(row["result"] == "success" for row in audit["items"])


def _declare(admin_client, uniq):
    # 通用上传层只处理字节；此文件不作为可部署的模型权重。
    content = b'{"model_type":"llama","architectures":["LlamaForCausalLM"]}'
    size = (len(content) + 1) // 2
    session = ok(
        admin_client,
        "POST",
        "/api/v1/admin/uploads/declare",
        json={
            "name": uniq("upload"),
            "files": [{"name": "config.json", "size": len(content), "total_parts": 2, "part_size": size}],
        },
    )
    return session, [content[:size], content[size:]]


def _part(admin_client, session, number, content, *, checksum=None):
    return admin_client.post(
        f"/api/v1/admin/uploads/{session['sid']}/part",
        data={
            "file_id": session["files"][0]["file_id"],
            "part_no": str(number),
            "sha256": checksum or hashlib.sha256(content).hexdigest(),
        },
        files={"file": ("part.bin", content, "application/octet-stream")},
    )


def test_chunk_upload_resume_finalize_and_cleanup(admin_client, uniq):
    session, parts = _declare(admin_client, uniq)
    root = f"/api/v1/admin/uploads/{session['sid']}"
    model_id = None
    completed = False
    try:
        bad = _part(admin_client, session, 1, parts[0], checksum="0" * 64)
        assert bad.status_code == 409
        assert bad.json()["error_code"] == "PART_SHA_MISMATCH"
        for _ in range(2):  # 重复分片幂等，不改变已收片数。
            response = _part(admin_client, session, 1, parts[0])
            assert response.status_code == 200
            assert response.json()["data"]["received"] is True
        status = ok(admin_client, "GET", root)
        assert status["files"][0]["received_parts"] == [1]
        assert status["files"][0]["missing_parts"] == [2]
        expect_error(admin_client, "POST", f"{root}/finalize", status=409)
        assert _part(admin_client, session, 2, parts[1]).status_code == 200
        ok(admin_client, "POST", f"{root}/finalize")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = ok(admin_client, "GET", root)
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(1)
        assert status["status"] == "completed", status
        completed = True
        final = ok(admin_client, "POST", f"{root}/finalize")
        assert final["files"][0]["name"] == "config.json"
        assert final["files"][0]["size"] == sum(map(len, parts))
        # 登记上传目录，使用正式模型删除接口验证产物回收。
        model = ok(
            admin_client,
            "POST",
            "/api/v1/admin/models",
            json={
                "name": uniq("upload-metadata"),
                "source": "local",
                "source_url": final["staged_path"],
                "model_type": "llm",
            },
        )
        model_id = model["id"]
        deleted = ok(admin_client, "POST", f"/api/v1/admin/models/{model_id}/delete")
        model_id = None
        assert final["staged_path"] in deleted["purged_dirs"]
        expect_error(admin_client, "GET", f"/api/v1/admin/models/{model['id']}", status=404)
    finally:
        if model_id is not None:
            ok(admin_client, "POST", f"/api/v1/admin/models/{model_id}/delete")
        elif not completed:
            ok(admin_client, "POST", f"{root}/abort")


def test_chunk_upload_abort_rejects_late_parts(admin_client, uniq):
    session, parts = _declare(admin_client, uniq)
    root = f"/api/v1/admin/uploads/{session['sid']}"
    for _ in range(2):
        assert ok(admin_client, "POST", f"{root}/abort")["status"] == "aborted"
    late = _part(admin_client, session, 1, parts[0])
    # 正常取消已删除 Redis 会话；迟到分片按会话不存在拒绝。
    assert late.status_code == 404
    assert late.json()["error_code"] == "SESSION_NOT_FOUND"
