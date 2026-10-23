"""通用切片上传层集成测试（HTTP 契约 + 异常→HTTP 映射）。

本仓 HTTP 层测试只验路由注册、依赖链、统一 Result 契约、异常→HTTP 映射；
断开 PG/Redis。本层业务逻辑（真实分片落盘/merge/Redis 读写/后台清理）不进
HTTP 层测试——这里 stub 路由模块命名空间的 service 符号，只验端点契约。

测试范围：
- 路由注册（5 端点都在 /api/v1/admin/uploads/*，仅 GET/POST）；
- 鉴权门（无登录态 → 401）；
- declare 成功契约 + disk_full 映射（monkeypatch service）；
- part → part_sha_mismatch（409）/ session_finalizing（409）映射；
- finalize → session_not_finalizable（409）映射 + 成功契约；
- status 成功契约 + session_not_found（404）映射。

service 不再接收 redis 参数（service 内部 get_redis），路由不再触 redis——
故无需像旧版那样 stub 路由模块的 get_redis，直接桩 service 符号即可。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.services.upload import (
    DeclareResponse,
    FinalizeResponse,
    PartResponse,
    SessionStatusResponse,
)


def _admin_user() -> SessionData:
    return admin_session()


def _declare_body(name: str = "qwen") -> dict:
    """最小合法 declare body：1 文件 1 分片（declare 不带 sha，sha 在 part 上传时带）。"""
    return {
        "name": name,
        "files": [
            {
                "name": "model.safetensors",
                "size": 10,
                "total_parts": 1,
                "part_size": 10,
            }
        ],
    }


class TestUploadsRouteRegistration:
    async def test_declare_no_auth_401(self, client: AsyncClient):
        resp = await client.post("/api/v1/admin/uploads/declare", json=_declare_body())
        assert resp.status_code == 401

    async def test_status_no_auth_401(self, client: AsyncClient):
        resp = await client.get("/api/v1/admin/uploads/somesid")
        assert resp.status_code == 401

    async def test_only_get_post_methods(self, client: AsyncClient):
        """设计约束：全部端点仅 GET/POST，不暴露 DELETE/PUT。"""
        app.dependency_overrides[get_current_user] = _admin_user
        for method in ("put", "delete", "patch"):
            r1 = await getattr(client, method)(
                f"/api/v1/admin/uploads/somesid/{'abort' if method == 'delete' else 'finalize'}"
            )
            # 不存在该方法的端点 → 405（FastAPI 显式拒），而非 404 当作端点存在
            assert r1.status_code in (405, 404), f"{method} 不应是可用方法"


class TestDeclare:
    async def test_declare_success_contract(self, client: AsyncClient, monkeypatch):
        """declare 成功：返回 sid/staged_path/status=pending/files 清单。"""
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_declare(*, name, files_spec):
            assert name == "qwen"
            assert len(files_spec) == 1
            return DeclareResponse(
                sid="sid-123",
                staged_path="/data/models/upload/qwen",
                status="pending",
                files=[{"file_id": "f0", "name": "model.safetensors"}],
            )

        monkeypatch.setattr(uploads_route.upload_service, "declare_session", fake_declare)

        resp = await client.post("/api/v1/admin/uploads/declare", json=_declare_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["sid"] == "sid-123"
        assert data["status"] == "pending"
        assert data["files"][0]["file_id"] == "f0"

    async def test_declare_disk_full_maps_409(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import Conflict, ErrorCode

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_declare(*, name, files_spec):
            raise Conflict("磁盘不足", error_code=ErrorCode.DISK_FULL)

        monkeypatch.setattr(uploads_route.upload_service, "declare_session", fake_declare)

        resp = await client.post("/api/v1/admin/uploads/declare", json=_declare_body())
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "DISK_FULL"

    async def test_declare_oversize_maps_409(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import Conflict, ErrorCode

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_declare(*, name, files_spec):
            raise Conflict("超限", error_code=ErrorCode.OVERSIZE)

        monkeypatch.setattr(uploads_route.upload_service, "declare_session", fake_declare)

        resp = await client.post("/api/v1/admin/uploads/declare", json=_declare_body())
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "OVERSIZE"

    async def test_declare_invalid_request_422(self, client: AsyncClient):
        """请求体不合法（缺 files）→ 422。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post("/api/v1/admin/uploads/declare", json={"name": "x"})
        assert resp.status_code == 422


class TestPart:
    async def test_part_sha_mismatch_409(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import Conflict, ErrorCode

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_receive(*, sid, file_id, part_no, sha256, body):
            raise Conflict("sha 不符", error_code=ErrorCode.PART_SHA_MISMATCH)

        monkeypatch.setattr(uploads_route.upload_service, "receive_part", fake_receive)

        resp = await client.post(
            "/api/v1/admin/uploads/sid-1/part",
            data={"file_id": "f0", "part_no": "1", "sha256": "0" * 64},
            files={"file": ("p.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "PART_SHA_MISMATCH"

    async def test_part_session_finalizing_409(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import Conflict, ErrorCode

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_receive(*, sid, file_id, part_no, sha256, body):
            raise Conflict("合并中", error_code=ErrorCode.SESSION_FINALIZING)

        monkeypatch.setattr(uploads_route.upload_service, "receive_part", fake_receive)

        resp = await client.post(
            "/api/v1/admin/uploads/sid-1/part",
            data={"file_id": "f0", "part_no": "1", "sha256": "0" * 64},
            files={"file": ("p.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "SESSION_FINALIZING"

    async def test_part_session_not_found_404(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import ErrorCode, NotFound

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_receive(*, sid, file_id, part_no, sha256, body):
            raise NotFound("session", sid, error_code=ErrorCode.SESSION_NOT_FOUND)

        monkeypatch.setattr(uploads_route.upload_service, "receive_part", fake_receive)

        resp = await client.post(
            "/api/v1/admin/uploads/sid-x/part",
            data={"file_id": "f0", "part_no": "1", "sha256": "0" * 64},
            files={"file": ("p.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "SESSION_NOT_FOUND"

    async def test_part_success_contract(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_receive(*, sid, file_id, part_no, sha256, body):
            return PartResponse(file_id=file_id, part_no=part_no, received=True, session_status="pending")

        monkeypatch.setattr(uploads_route.upload_service, "receive_part", fake_receive)

        resp = await client.post(
            "/api/v1/admin/uploads/sid-1/part",
            data={"file_id": "f0", "part_no": "1", "sha256": "0" * 64},
            files={"file": ("p.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["received"] is True
        assert data["session_status"] == "pending"


class TestStatus:
    async def test_status_success_contract(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_status(*, sid):
            return SessionStatusResponse(
                sid=sid,
                status="pending",
                files=[
                    {
                        "file_id": "f0",
                        "name": "model.safetensors",
                        "total_parts": 3,
                        "received_parts": [1, 3],
                        "missing_parts": [2],
                        "complete": False,
                    }
                ],
            )

        monkeypatch.setattr(uploads_route.upload_service, "get_session_status", fake_status)

        resp = await client.get("/api/v1/admin/uploads/sid-1")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["files"][0]["missing_parts"] == [2]

    async def test_status_session_not_found_404(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import ErrorCode, NotFound

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_status(*, sid):
            raise NotFound("session", sid, error_code=ErrorCode.SESSION_NOT_FOUND)

        monkeypatch.setattr(uploads_route.upload_service, "get_session_status", fake_status)

        resp = await client.get("/api/v1/admin/uploads/sid-x")
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "SESSION_NOT_FOUND"


class TestFinalize:
    async def test_finalize_not_finalizable_409(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import Conflict, ErrorCode

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_finalize(*, sid):
            raise Conflict("未收齐", error_code=ErrorCode.SESSION_NOT_FINALIZABLE)

        monkeypatch.setattr(uploads_route.upload_service, "finalize_session", fake_finalize)

        resp = await client.post("/api/v1/admin/uploads/sid-1/finalize")
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "SESSION_NOT_FINALIZABLE"

    async def test_finalize_failed_500(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route
        from llmpod.core.exceptions import ErrorCode, LLMPodError

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_finalize(*, sid):
            raise LLMPodError("合并失败", error_code=ErrorCode.FINALIZE_FAILED)

        monkeypatch.setattr(uploads_route.upload_service, "finalize_session", fake_finalize)

        resp = await client.post("/api/v1/admin/uploads/sid-1/finalize")
        assert resp.status_code == 500
        assert resp.json()["error_code"] == "FINALIZE_FAILED"

    async def test_finalize_success_contract(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_finalize(*, sid):
            return FinalizeResponse(
                sid=sid,
                staged_path="/data/models/upload/qwen",
                status="completed",
                files=[{"file_id": "f0", "name": "model.safetensors", "size": 10}],
            )

        monkeypatch.setattr(uploads_route.upload_service, "finalize_session", fake_finalize)

        resp = await client.post("/api/v1/admin/uploads/sid-1/finalize")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "completed"
        assert data["files"][0]["name"] == "model.safetensors"

    async def test_finalize_kicks_off_async(self, client: AsyncClient, monkeypatch):
        """finalize 异步：校验通过立即返回 status=finalizing + 空 files，前端轮询拿结果。"""
        from llmpod.apps.webserver.routes.admin import uploads as uploads_route

        app.dependency_overrides[get_current_user] = _admin_user

        async def fake_finalize(*, sid):
            return FinalizeResponse(sid=sid, staged_path="/data/x", status="finalizing")

        monkeypatch.setattr(uploads_route.upload_service, "finalize_session", fake_finalize)

        resp = await client.post("/api/v1/admin/uploads/sid-1/finalize")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "finalizing"
        assert data["files"] == []
