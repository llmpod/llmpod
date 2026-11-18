"""NPS 服务单元测试：serverid 派生一致性 / 表单字段封装 / 收集端结果映射。

外呼一律 monkeypatch 模块命名空间的 httpx；serverid 派生 monkeypatch
``derive_machine_hash`` 与 ``nps_server_id``（本模块从遥测模块 import 的符号，
桩打在本模块命名空间）。落库用 FakeSession 预载 telemetry_instance 行。
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

import llmpod
from llmpod.core.exceptions import LLMPodError
from llmpod.models.system_config import SystemConfig
from llmpod.services import nps_service as svc
from llmpod.services.nps_service import NpsFeedbackRequest
from llmpod.services.telemetry_service import TELEMETRY_CONFIG_KEY
from tests.integration._fakes import FakeSession


@pytest.fixture
def fake_db():
    return FakeSession()


def _preload_instance(fake_db, instance_id: str, installed_at="2026-08-01T00:00:00+00:00"):
    fake_db.preload(
        SystemConfig(
            key=TELEMETRY_CONFIG_KEY,
            value={"instance_id": instance_id, "installed_at": installed_at, "last_report_date": ""},
        )
    )


# ---------------------------------------------------------------------------
# serverid 派生：与遥测 instance_id 的一致性
# ---------------------------------------------------------------------------


class TestServerId:
    async def test_machine_derived_full_hash_prefix_is_instance_id(self, monkeypatch, fake_db):
        """宿主可派生且与遥测行同源：serverid 用全量哈希，前 32 位 == instance_id。"""
        full = hashlib.sha256(b"llmpod:host-raw").hexdigest()
        monkeypatch.setattr(svc, "derive_machine_hash", lambda: full)
        _preload_instance(fake_db, full[:32])
        assert await svc.nps_server_id(fake_db) == full

    async def test_machine_identity_drift_follows_stored_instance(self, monkeypatch, fake_db):
        """宿主标识已变（前缀对不上遥测行）：跟随遥测行身份再哈希，不产生第二台机器。"""
        monkeypatch.setattr(svc, "derive_machine_hash", lambda: hashlib.sha256(b"llmpod:new-host").hexdigest())
        stored = "a" * 32
        _preload_instance(fake_db, stored)
        assert await svc.nps_server_id(fake_db) == hashlib.sha256(("llmpod:" + stored).encode()).hexdigest()

    async def test_no_machine_source_hashes_stored_instance(self, monkeypatch, fake_db):
        """宿主来源全不可读（遥测随机兜底的机器）：对已存 instance_id 做同盐 sha256。"""
        monkeypatch.setattr(svc, "derive_machine_hash", lambda: None)
        stored = "b" * 32
        _preload_instance(fake_db, stored)
        server_id = await svc.nps_server_id(fake_db)
        assert server_id == hashlib.sha256(("llmpod:" + stored).encode()).hexdigest()
        assert len(server_id) == 64

    async def test_shape_is_always_64_lower_hex(self, monkeypatch, fake_db):
        monkeypatch.setattr(svc, "derive_machine_hash", lambda: None)
        _preload_instance(fake_db, "c" * 32)
        server_id = await svc.nps_server_id(fake_db)
        assert len(server_id) == 64
        assert all(ch in "0123456789abcdef" for ch in server_id)


# ---------------------------------------------------------------------------
# 提交：表单封装 + 结果映射
# ---------------------------------------------------------------------------


class TestSubmitFeedback:
    @staticmethod
    def _install_client(monkeypatch, body=None, status_code=200, exc=None):
        captured = {}

        class _Resp:
            def __init__(self):
                self.status_code = status_code

            def json(self):
                if isinstance(body, Exception):
                    raise body
                return body

            text = ""

        class _Client:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, data=None):
                captured["url"] = url
                captured["data"] = data
                if exc is not None:
                    raise exc
                return _Resp()

        monkeypatch.setattr(svc.httpx, "AsyncClient", _Client)
        return captured

    @staticmethod
    def _stub_server_id(monkeypatch, value="d" * 64):
        async def _fake(db):
            return value

        monkeypatch.setattr(svc, "nps_server_id", _fake)
        return value

    async def test_form_wraps_fixed_fields(self, monkeypatch, fake_db):
        """固定字段全部服务端补齐；评语注入 questions JSON，键为题目编号。"""
        captured = self._install_client(monkeypatch, body={"code": 200, "msg": "提交成功"})
        server_id = self._stub_server_id(monkeypatch)
        await svc.submit_feedback(fake_db, NpsFeedbackRequest(rate=5, comment="还行还行"))
        assert captured["url"] == svc.NPS_SUBMIT_URL
        assert captured["data"] == {
            "action": "submit",
            "serverid": server_id,
            "product_type": svc.NPS_PRODUCT_TYPE,
            "panel_version": llmpod.__version__,
            "rate": "5",
            "questions": json.dumps({svc.NPS_QUESTION_ID: "还行还行"}, ensure_ascii=False),
        }

    async def test_empty_comment_omits_questions(self, monkeypatch, fake_db):
        captured = self._install_client(monkeypatch, body={"code": 200, "msg": "提交成功"})
        self._stub_server_id(monkeypatch)
        await svc.submit_feedback(fake_db, NpsFeedbackRequest(rate=3, comment=None))
        assert "questions" not in captured["data"]

    async def test_upstream_rejection_raises_with_msg(self, monkeypatch, fake_db):
        """收集端 code!=200：原样带回中文 msg（如 400 错误的参数【SERVER_ID】）。"""
        self._install_client(monkeypatch, body={"code": 400, "msg": "错误的参数【SERVER_ID】"})
        self._stub_server_id(monkeypatch)
        with pytest.raises(LLMPodError, match="错误的参数【SERVER_ID】"):
            await svc.submit_feedback(fake_db, NpsFeedbackRequest(rate=5, comment="x"))

    async def test_non_json_body_raises(self, monkeypatch, fake_db):
        self._install_client(monkeypatch, body=ValueError("not json"))
        self._stub_server_id(monkeypatch)
        with pytest.raises(LLMPodError, match="反馈提交失败"):
            await svc.submit_feedback(fake_db, NpsFeedbackRequest(rate=5, comment="x"))

    async def test_network_error_raises(self, monkeypatch, fake_db):
        self._install_client(
            monkeypatch,
            exc=httpx.ConnectError("boom", request=httpx.Request("POST", "https://x")),
        )
        self._stub_server_id(monkeypatch)
        with pytest.raises(LLMPodError, match="反馈提交失败"):
            await svc.submit_feedback(fake_db, NpsFeedbackRequest(rate=5, comment="x"))
