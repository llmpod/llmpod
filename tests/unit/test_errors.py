"""错误映射单元测试：DB IntegrityError → 领域级 Result（修复 IntegrityError→500 泄漏）。

用户输入触发 DB 约束违例时，前端应拿到稳定的 409/422 + error_code，而非笼统 500。
asyncpg orig：UniqueViolationError → 409 CONFLICT；NotNullViolationError → 422 VALIDATION_ERROR。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from llmpod.apps.webserver.errors import register_exception_handlers


class _UniqueViolationError(Exception):
    pass


class _NotNullViolationError(Exception):
    pass


def _make_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/uniq")
    def uniq():  # noqa: ANN201 — 路由处理器不注解返回，被测的是异常处理器
        raise IntegrityError('INSERT INTO "x" ...', {}, _UniqueViolationError('duplicate key violates "uq_x"'))

    @app.get("/notnull")
    def notnull():  # noqa: ANN201 — 路由处理器不注解返回，被测的是异常处理器
        raise IntegrityError('INSERT INTO "x" ...', {}, _NotNullViolationError('null value in column "y"'))

    return app


def test_integrity_unique_maps_to_409_conflict():
    client = TestClient(_make_app(), raise_server_exceptions=False)
    r = client.get("/uniq")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == 409
    assert body["error_code"] == "CONFLICT"
    # 约束名/上游原文只进日志不回传（防表结构泄露）；data 只给分类
    assert body["data"] == {"kind": "conflict"}


def test_integrity_notnull_maps_to_422_validation():
    client = TestClient(_make_app(), raise_server_exceptions=False)
    r = client.get("/notnull")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == 422
    assert body["error_code"] == "VALIDATION_ERROR"
