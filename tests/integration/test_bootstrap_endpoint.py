"""节点引导令牌链路集成测试（stub 驱动，不依赖真实 PG/Redis）。

覆盖：
- POST /api/v1/admin/nodes/bootstrap-token：登录态签发 → 返回 token + curl_command。
- GET  /api/v1/nodes/bootstrap：公共端点凭有效 token 渲染脚本（含 docker run、
  正确 MANAGER_URL、不再 0.0.0.0）；token 无效 / 跨集群 / 集群不存在 → 401/404。

遵循 testing 规则：client fixture 已 stub get_db；本文件在路由模块命名空间 patch
validate_cluster / get_db_context / get_settings，并提供临时 node_token 文件。
模板 scripts/addnode_template.sh 取仓库真实文件（CWD=repo root）。
"""

from __future__ import annotations

import contextlib
import re

import pytest

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.utils.bootstrap_token import mint_bootstrap_token, bootstrap_signing_key

# 公共端点、admin 端点各自 import 后的命名空间符号（patch 目标）
import llmpod.apps.webserver.routes.public.node as pub_node
import llmpod.apps.webserver.routes.admin.node as admin_node


def _admin_user() -> SessionData:
    """超管会话：require_permission 见 is_superuser=True 直接短路。"""
    return admin_session()


@contextlib.asynccontextmanager
async def _fake_db_ctx():
    """不连真实 PG 的 get_db_context 桩。"""
    yield None


async def _validate_true(cluster_id, db):
    return True


async def _validate_false(cluster_id, db):
    return False


def _setup_node_token(monkeypatch, tmp_path):
    """设置独立签名密钥文件与可达的 manager 地址。"""
    from llmpod.config.settings import settings

    monkeypatch.setattr(settings, "secret_key", "")
    monkeypatch.setattr(settings, "secret_key_file", str(tmp_path / "secret_key"))
    monkeypatch.setattr(settings, "node_token", "shared-node-secret")
    # 对外地址只配 IP/域名，端口取自 webserver_port——一并锁定，免得本机 .env 改了端口就飘
    monkeypatch.setattr(settings, "node_manager_ip", "manager.example")
    monkeypatch.setattr(settings, "webserver_port", 8080)


def _mint_test_token(cluster_id=1):
    from llmpod.config.settings import settings

    return mint_bootstrap_token(bootstrap_signing_key(settings), cluster_id, 3600)


# ===== admin 签发端点 =====
@pytest.mark.asyncio
async def test_mint_bootstrap_token(client, monkeypatch, tmp_path):
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_node, "validate_cluster", _validate_true)

    app.dependency_overrides[get_current_user] = _admin_user
    try:
        resp = await client.post(
            "/api/v1/admin/nodes/bootstrap-token",
            json={"cluster_id": 1, "ttl": 3600},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["cluster_id"] == 1
    assert "node_id" not in data
    assert data["token"]
    assert data["expires_at"]
    # curl 命令里含 token + http origin（ws→http 推导），不再是 0.0.0.0；
    # 外层 bash -c pipefail 包装：curl 失败（令牌过期/manager 不可达）时非零退出，
    # 不再静默空跑成「秒完成、什么都没发生」；URL 单引号包裹（'"'"' 拼接），
    # 管理端地址混入 $ ` " 等字符不会在装机端展开
    assert "token=" + data["token"] in data["command"]
    assert "http://manager.example:8080" in data["command"]
    assert "0.0.0.0" not in data["command"]
    assert data["command"].startswith("bash -c 'set -eo pipefail; curl -fsSL '\"'\"'http")
    assert data["command"].endswith("'\"'\"' | bash'")


@pytest.mark.asyncio
async def test_mint_bootstrap_token_unknown_cluster(client, monkeypatch, tmp_path):
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_node, "validate_cluster", _validate_false)

    app.dependency_overrides[get_current_user] = _admin_user
    try:
        resp = await client.post(
            "/api/v1/admin/nodes/bootstrap-token",
            json={"cluster_id": 99},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


# ===== 公共 bootstrap 端点 =====
@pytest.mark.asyncio
async def test_public_bootstrap_with_valid_token(client, monkeypatch, tmp_path):
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    token, _ = _mint_test_token()
    # 不设置 get_current_user override —— 证明该端点无需登录
    resp = await client.get("/api/v1/nodes/bootstrap", params={"cluster_id": 1, "token": token})

    assert resp.status_code == 200, resp.text
    assert "shellscript" in resp.headers["content-type"]
    body = resp.text
    assert "docker run" in body
    assert 'NODE_TOKEN="shared-node-secret"' in body
    assert f'BOOTSTRAP_TOKEN="{token}"' in body
    assert "LLMPOD_NODE_ID=" not in body
    assert 'MANAGER_URL="ws://manager.example:8080/ws/node"' in body  # 可达地址
    assert "0.0.0.0" not in body


@pytest.mark.asyncio
async def test_public_bootstrap_invalid_token(client, monkeypatch, tmp_path):
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    resp = await client.get(
        "/api/v1/nodes/bootstrap",
        params={"cluster_id": 1, "token": "totally-bogous"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_public_bootstrap_cluster_mismatch(client, monkeypatch, tmp_path):
    """令牌绑定 cluster_id=2，请求 cluster_id=1 → cid 校验失败 → 401。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    token, _ = _mint_test_token(2)
    resp = await client.get("/api/v1/nodes/bootstrap", params={"cluster_id": 1, "token": token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_bootstrap_cluster_not_found(client, monkeypatch, tmp_path):
    """token 有效但集群不存在 → 404。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_false)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    token, _ = _mint_test_token()
    resp = await client.get("/api/v1/nodes/bootstrap", params={"cluster_id": 1, "token": token})
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "NOT_FOUND"


# ===== 引导参数（query 传入，渲染进脚本）=====

_PARAMS = {
    "node_ip": "10.0.0.5",
    "container_name": "llmpod-node-gpu2",
    "data_volume": "/mnt/bigdisk/llmpod-node-data",
    "model_host_dir": "/mnt/bigdisk/models",
    "extra_mounts": ["/mnt/hf:/hf:ro", "/v:/v:ro,z"],
    "image": "llmpod:v2",
}


@pytest.mark.asyncio
async def test_mint_bootstrap_token_with_params(client, monkeypatch, tmp_path):
    """签发带引导参数 → command URL 只携带非默认参数（model_dir=manager 配置不进 URL）。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_node, "validate_cluster", _validate_true)

    app.dependency_overrides[get_current_user] = _admin_user
    try:
        resp = await client.post("/api/v1/admin/nodes/bootstrap-token", json={"cluster_id": 1, **_PARAMS})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, resp.text
    command = resp.json()["data"]["command"]
    assert "cluster_id=1&token=" in command
    assert "container_name=llmpod-node-gpu2" in command
    assert "node_ip=10.0.0.5" in command
    assert "extra_mounts=/mnt/hf:/hf:ro" in command
    assert "extra_mounts=/v:/v:ro,z" in command
    assert "model_host_dir=/mnt/bigdisk/models" in command
    # model_dir 非用户参数（canonical 恒取 manager 配置）→ 永不进 URL
    assert "model_dir=" not in command


@pytest.mark.asyncio
async def test_mint_bootstrap_token_invalid_params(client, monkeypatch, tmp_path):
    """签发带非法参数（注入字符）→ 422，不产出 command。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(admin_node, "validate_cluster", _validate_true)

    app.dependency_overrides[get_current_user] = _admin_user
    try:
        resp = await client.post(
            "/api/v1/admin/nodes/bootstrap-token",
            json={"cluster_id": 1, "container_name": "a;rm -rf /"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_public_bootstrap_with_params(client, monkeypatch, tmp_path):
    """公共端点带 query 参数 → 渲染体含各变量值与挂载/通告 IP 注入。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    token, _ = _mint_test_token()
    resp = await client.get("/api/v1/nodes/bootstrap", params={"cluster_id": 1, "token": token, **_PARAMS})

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'CONTAINER_NAME="${LLMPOD_NODE_CONTAINER_NAME:-llmpod-node-gpu2}"' in body
    assert 'DATA_VOLUME="${LLMPOD_NODE_DATA_VOLUME:-/mnt/bigdisk/llmpod-node-data}"' in body
    # canonical 容器内路径恒取 manager 配置（非用户参数，无可覆盖 env）
    assert 'MODEL_DIR="/data/models"' in body
    assert 'MODEL_HOST_DIR="${LLMPOD_MODEL_HOST_DIR:-/mnt/bigdisk/models}"' in body
    assert 'IMAGE="${LLMPOD_NODE_IMAGE:-llmpod:v2}"' in body
    assert '-v "$MODEL_HOST_DIR:$MODEL_DIR"' in body
    assert '-e "LLMPOD_MODEL_STORE_DIR=$MODEL_DIR"' in body
    # 宿主前缀注入容器 env：node 创建推理容器时按它改写卷 host 侧
    assert '-e "LLMPOD_MODEL_HOST_DIR=$MODEL_HOST_DIR"' in body
    # 节点互联 IP：引导参数直渲（优先于目标机 env），并恒注入容器 env
    # （显式指定 IP 是必选项，脚本对空值路径要么交互选定要么直接退出）
    assert 'NODE_IP="10.0.0.5"' in body
    assert 'NODE_IP="${LLMPOD_NODE_ADVERTISE_IP:-}"' in body
    assert '-e "LLMPOD_NODE_ADVERTISE_IP=$NODE_IP"' in body
    assert 'EXTRA_MOUNTS_SPEC="/mnt/hf:/hf:ro /v:/v:ro,z"' in body
    # 渲染后不残留任何未处理的 {{placeholder}}
    assert not re.search(r"\{\{[a-z_]+\}\}", body)


@pytest.mark.asyncio
async def test_bootstrap_token_allows_editing_defaults(client, monkeypatch, tmp_path):
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)
    token, _ = _mint_test_token()
    for changed in ({"extra_mounts": ["/host:/container:rw"]}, {"image": "attacker/image:latest"}):
        resp = await client.get("/api/v1/nodes/bootstrap", params={"cluster_id": 1, "token": token, **changed})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_public_bootstrap_invalid_param_422(client, monkeypatch, tmp_path):
    """token 有效但参数非法 → 422（非法值不落脚本）。"""
    _setup_node_token(monkeypatch, tmp_path)
    monkeypatch.setattr(pub_node, "validate_cluster", _validate_true)
    monkeypatch.setattr(pub_node, "get_db_context", _fake_db_ctx)

    token, _ = _mint_test_token()
    resp = await client.get(
        "/api/v1/nodes/bootstrap",
        params={"cluster_id": 1, "token": token, "extra_mounts": ["/a:/b;touch /tmp/x"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"
