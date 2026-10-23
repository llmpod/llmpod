"""推理部署全流程 e2e（真·栈：HTTP API → Manager 编排 → Node 拉起 vLLM → /health → ModelEndpoint）。

前置（运维准备）：一个在线 GPU 节点 + 已预置模型 + vllm 镜像。可用环境变量覆盖：
  LLMPOD_E2E_MODEL_PATH（默认 /data/models/Qwen2.5-0.5B）
  LLMPOD_E2E_IMAGE（默认 vllm/vllm-openai:v0.23.0）
  LLMPOD_E2E_MODEL_NAME（默认 Qwen2.5-0.5B）
无在线 GPU 节点时 skip（不 fail）。
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from _client import ok

_MODEL_PATH = os.environ.get("LLMPOD_E2E_MODEL_PATH", "/data/models/Qwen2.5-0.5B")
_IMAGE = os.environ.get("LLMPOD_E2E_IMAGE", "vllm/vllm-openai:v0.23.0")
_MODEL_NAME = os.environ.get("LLMPOD_E2E_MODEL_NAME", "Qwen2.5-0.5B")
_DEPLOY_TIMEOUT_S = int(os.environ.get("LLMPOD_E2E_DEPLOY_TIMEOUT", "600"))
_POLL_INTERVAL_S = 10


def _find_gpu_node(admin_client: httpx.Client) -> tuple[int, str, str] | None:
    """找一个在线且有 nvidia GPU 的节点。返回 (cluster_id, node_id, gpu_arch)。"""
    # 取首个 active 集群
    clusters = ok(admin_client, "GET", "/api/v1/admin/clusters?status=active")
    items = clusters["items"] if isinstance(clusters, dict) else clusters
    if not items:
        return None
    cluster_id = items[0]["id"]

    # 在线节点（经编排侧内存）
    nodes = ok(admin_client, "GET", "/api/v1/admin/orchestration/nodes")
    if not nodes:
        return None
    for n in nodes:
        node_id = n["node_id"]
        # 列表为精简摘要，用 gpu_archs 判别节点 GPU 软件栈（nvidia/cann/...）
        if "nvidia" in n.get("gpu_archs", []):
            return cluster_id, node_id, "nvidia"
    return None


def test_model_deployment_full_flow(admin_client: httpx.Client, uniq):
    """创建部署 → 轮询到 running → 校验 ModelEndpoint → 清理。"""
    target = _find_gpu_node(admin_client)
    if target is None:
        pytest.skip("无在线 nvidia GPU 节点（需预置节点+模型+镜像），跳过部署 e2e")
    cluster_id, node_id, arch = target

    # 1. Model（source=local + type 用户声明；cached_nodes 不在此填，部署靠下方 spec.model_path 兜底）
    model = ok(
        admin_client,
        "POST",
        "/api/v1/admin/models",
        json={
            "name": _MODEL_NAME,
            "source": "local",
            "source_url": _MODEL_PATH,
            "model_type": "llm",
            "files": {"format": "safetensors", "size_gb": 1.0},
        },
    )

    # 2. 创建部署（触发异步编排）
    dep = ok(
        admin_client,
        "POST",
        "/api/v1/admin/model-deployments",
        json={
            "cluster_id": cluster_id,
            "name": uniq("dep"),
            "model_id": model["id"],
            "engine": "vllm",
            "spec": {
                "topology": {"mode": "tensor_parallel", "tp": 1, "replicas": 1},
                "model_path": _MODEL_PATH,
                "image": _IMAGE,
                "health_check": {"timeout_seconds": 600, "interval_seconds": 10},
            },
        },
    )
    deployment_id = dep["deployment_id"]

    try:
        # 3. 轮询直到 running/failed
        deadline = time.time() + _DEPLOY_TIMEOUT_S
        final = None
        while time.time() < deadline:
            detail = ok(admin_client, "GET", f"/api/v1/admin/model-deployments/{deployment_id}")
            final = detail
            status = detail["deployment"]["status"]
            if status in ("running", "failed"):
                break
            time.sleep(_POLL_INTERVAL_S)

        assert final is not None, "部署详情查询无结果"
        assert final["deployment"]["status"] == "running", (
            f"部署未达 running（最终 {final['deployment']['status']}）；详情={final}"
        )
        # 4. 校验 ModelEndpoint 产出
        assert final["endpoint"] is not None, "running 但未产出 ModelEndpoint"
        assert final["endpoint"]["served_model"] == _MODEL_NAME
        assert final["endpoint"]["api_base"].startswith("http://")
        assert final["endpoint"]["health_status"] == "healthy"
        # instance READY（readiness 通过后的长期状态）
        assert final["instances"], "无 instance"
        assert all(i["status"] == "READY" for i in final["instances"])
    finally:
        # 5. 清理（删除守卫）
        try:
            ok(admin_client, "POST", f"/api/v1/admin/model-deployments/{deployment_id}/delete")
        except Exception:
            pass
