"""遥测服务单元测试：kind 推导 / 采集映射 / 实例标识 / 每日门控 / 发送 / 任务生命周期。

外呼一律 monkeypatch 模块命名空间的 httpx；落库用 FakeSession（查询形状按其
能力设计：平铺列 + Python 分组，无 GROUP BY）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import httpx
import pytest

import llmpod
from llmpod.services import telemetry_service as svc
from llmpod.services.telemetry_service import (
    TELEMETRY_CONFIG_KEY,
    TelemetryModelEntry,
    TelemetryPayload,
    collect_engines,
    collect_features,
    collect_models,
    collect_scale,
    derive_kind,
    ensure_instance_state,
    mark_report_sent,
    payload_body,
    report_if_due,
    send_report,
    start_telemetry_task,
)
from llmpod.core.taskwatch import BackgroundTaskManager


@pytest.fixture
def fake_db():
    """空库 FakeSession（各用例按需 preload 行）。"""
    from tests.integration._fakes import FakeSession

    return FakeSession()


# ---------------------------------------------------------------------------
# 桩构造（ORM 行须填满 orm_schema 可能提取的列）
# ---------------------------------------------------------------------------


def _stub_model(model_id, **overrides):
    from llmpod.models.model import Model

    base = dict(
        id=model_id,
        name="secret-display-name",
        model_type="llm",
        architecture="Qwen2ForCausalLM",
        is_moe=False,
        is_vision=False,
        size_gb=14.5,
        format="safetensors",
        quantization="AWQ",
        num_layers=None,
        num_attention_heads=None,
        num_kv_heads=None,
        head_dim=None,
        max_context=None,
        hash=None,
        source=None,
        source_url=None,
        source_kind="huggingface",
        source_subpath=None,
        revision=None,
        hash_mode=None,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Model(**base)


def _stub_deployment(deployment_id, model_id, engine="vllm", status="running"):
    from llmpod.models.model_deployment import ModelDeployment

    return ModelDeployment(
        deployment_id=deployment_id,
        model_id=model_id,
        engine=engine,
        status=status,
    )


class TestDeriveKind:
    """词表透传 / llm+视觉升格 / 未知回落。"""

    @pytest.mark.parametrize("kind", ["llm", "multimodal", "embedding", "rerank", "image", "stt", "tts"])
    def test_vocab_passes_through(self, kind):
        assert derive_kind(kind, False) == kind

    def test_llm_with_vision_upgrades(self):
        assert derive_kind("llm", True) == "multimodal"

    def test_vision_does_not_upgrade_other_types(self):
        assert derive_kind("embedding", True) == "embedding"

    @pytest.mark.parametrize("model_type", ["video", ""])
    def test_unknown_passes_through(self, model_type):
        assert derive_kind(model_type, False) == model_type

    def test_none_falls_back_empty(self):
        assert derive_kind(None, False) == ""


class TestCollectModels:
    async def test_field_mapping_and_deployed(self, fake_db):
        fake_db.preload(_stub_model(1, name="secret-display-name"))
        fake_db.preload(_stub_model(2, architecture=None, format=None, quantization=None, size_gb=None))
        fake_db.preload(_stub_deployment("dep-1", model_id=1, status="running"))

        entries = await collect_models(fake_db)
        assert len(entries) == 2

        first = entries[0]
        assert first.architecture == "Qwen2ForCausalLM"
        assert first.kind == "llm"
        assert first.format == "safetensors"
        assert first.quant == "awq"  # 统一小写
        assert first.size_gb == pytest.approx(14.5)
        assert first.deployed is True  # 有部署行

        second = entries[1]
        assert second.architecture == ""  # 空值兜底
        assert second.format == ""
        assert second.quant == ""
        assert second.size_gb == 0.0
        assert second.deployed is False  # 无任何部署行

    async def test_display_name_never_in_payload(self, fake_db):
        """Model.name（用户起的显示名）永不进遥测数据。"""
        fake_db.preload(_stub_model(1, name="secret-display-name"))
        entries = await collect_models(fake_db)
        payload = TelemetryPayload(
            instance_id="i" * 32,
            version="26.9.20",
            installed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            reported_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
            models=entries,
        )
        assert b"secret-display-name" not in payload_body(payload)

    async def test_empty_store(self, fake_db):
        assert await collect_models(fake_db) == []


class TestCollectScale:
    async def test_grouping_and_vram_sum(self, fake_db):
        from llmpod.models.gpu import Gpu

        for i, (name, vram) in enumerate(
            [("NVIDIA A100-SXM4-80GB", 80.0), ("NVIDIA A100-SXM4-80GB", 80.0), ("310P3", 24.0)]
        ):
            fake_db.preload(
                Gpu(
                    id=i + 1,
                    uuid=f"gpu-{i}",
                    node_id="node-1",
                    index=i,
                    name=name,
                    arch="nvidia",
                    spec={},
                    total_vram_gb=vram,
                    driver_version=None,
                    status="online",
                )
            )

        scale = await collect_scale(fake_db)
        assert scale.gpu_by_model == {"NVIDIA A100-SXM4-80GB": 2, "310P3": 1}
        assert scale.vram_total_gb == pytest.approx(184.0)

    async def test_empty_store_zeros(self, fake_db):
        scale = await collect_scale(fake_db)
        assert scale.cluster_count == 0
        assert scale.node_count == 0
        assert scale.gpu_by_model == {}
        assert scale.vram_total_gb == 0.0


class TestCollectEnginesFeatures:
    async def test_grouping_and_counts(self, fake_db):
        from llmpod.models.api_key import ApiKey
        from llmpod.models.model_deployment_replica import ModelDeploymentReplica
        from llmpod.models.model_endpoint import ModelEndpoint
        from llmpod.models.route import Route

        fake_db.preload(_stub_deployment("dep-1", model_id=1, engine="vllm"))
        fake_db.preload(_stub_deployment("dep-2", model_id=1, engine="vllm"))
        fake_db.preload(_stub_deployment("dep-3", model_id=2, engine="llamacpp"))
        # dep-1 双副本 → multi_replica；dep-2/3 单副本
        fake_db.preload(ModelDeploymentReplica(replica_id="r-1", deployment_id="dep-1", seq=1, status="serving"))
        fake_db.preload(ModelDeploymentReplica(replica_id="r-2", deployment_id="dep-1", seq=2, status="serving"))
        fake_db.preload(ModelDeploymentReplica(replica_id="r-3", deployment_id="dep-3", seq=1, status="serving"))

        engines = await collect_engines(fake_db)
        assert engines == {"vllm": 2, "llamacpp": 1}

        fake_db.preload(Route(id=1, name="route-1", model_type="llm"))
        fake_db.preload(Route(id=2, name="route-2", model_type="embedding"))
        fake_db.preload(ApiKey(id=1, key_hash="h1", tenant_id=0, status="active"))
        fake_db.preload(ModelEndpoint(id=1, engine_type="vllm", protocol="openai"))
        fake_db.preload(ModelEndpoint(id=2, engine_type="external", protocol="openai"))

        features = await collect_features(fake_db, deployment_count=sum(engines.values()))
        assert features.deployment_count == 3
        assert features.multi_replica_count == 1
        assert features.route_count == 2
        assert features.api_key_count == 1
        assert features.endpoint_count == 2  # 本地投影 + 外部注册都计

    async def test_cpu_offload_distinct_deployments(self, fake_db):
        """纯 CPU 部署不落 GPU 分配行，形态标记在 Instance.spec 的 gpu_arch=="cpu"。"""
        from llmpod.models.instance import Instance

        # dep-1 纯 CPU（两实例只计一次）；dep-2 GPU 部署；dep-3 CPU 但无部署实例列
        for i, deployment_id in enumerate(("dep-1", "dep-1", "dep-2")):
            gpu_arch = "cpu" if deployment_id == "dep-1" else "nvidia"
            fake_db.preload(
                Instance(
                    instance_id=f"inst-{i}",
                    deployment_id=deployment_id,
                    status="READY",
                    spec={"gpu_arch": gpu_arch},
                )
            )
        fake_db.preload(Instance(instance_id="inst-9", deployment_id="dep-3", status="READY", spec={"gpu_arch": "cpu"}))
        fake_db.preload(_stub_deployment("dep-1", model_id=1))
        fake_db.preload(_stub_deployment("dep-2", model_id=2))
        fake_db.preload(_stub_deployment("dep-3", model_id=3))

        features = await collect_features(fake_db, deployment_count=3)
        assert features.cpu_offload_count == 2  # dep-1（两实例去重）+ dep-3


class TestInstanceState:
    @staticmethod
    def _make_row(value: dict):
        from llmpod.models.system_config import SystemConfig

        return SystemConfig(key=TELEMETRY_CONFIG_KEY, value=value)

    async def test_initial_boot_generates_and_persists(self, fake_db):
        state = await ensure_instance_state(fake_db)
        assert len(state.instance_id) == 32  # token_hex(16) → 32 hex 字符（站点限 8–64）
        assert state.installed_at is not None
        assert state.last_report_date == ""
        from llmpod.models.system_config import SystemConfig

        rows = fake_db.all(SystemConfig)
        assert len(rows) == 1  # 新建 SystemConfig 行
        assert rows[0].value["instance_id"] == state.instance_id
        assert rows[0].value["last_report_date"] == ""

    async def test_existing_row_returns_same_identity(self, fake_db):
        row = self._make_row(
            {
                "instance_id": "a" * 32,
                "installed_at": "2026-08-01T03:00:00+08:00",
                "last_report_date": "2026-09-21",
            }
        )
        fake_db.preload(row)
        state = await ensure_instance_state(fake_db)
        assert state.instance_id == "a" * 32
        assert state.installed_at == datetime.fromisoformat("2026-08-01T03:00:00+08:00")
        assert state.last_report_date == "2026-09-21"
        # 未新建行
        from llmpod.models.system_config import SystemConfig

        assert len(fake_db.all(SystemConfig)) == 1

    async def test_corrupted_installed_at_repaired_and_persisted(self, fake_db):
        """installed_at 不可解析：兜底当前时间并回写落库（身份保留、不反复外发当刻时间）。"""
        row = self._make_row({"instance_id": "b" * 32, "installed_at": "garbage"})
        fake_db.preload(row)
        state = await ensure_instance_state(fake_db)
        assert state.instance_id == "b" * 32
        assert state.installed_at is not None
        assert row.value["installed_at"] == state.installed_at.isoformat()  # 修复已持久化

    async def test_non_dict_value_regenerates(self, fake_db):
        """value 被通用配置接口写成非 dict：不抛、走重建分支（防每小时 AttributeError）。"""
        row = self._make_row(["not", "a", "dict"])
        fake_db.preload(row)
        state = await ensure_instance_state(fake_db)
        assert len(state.instance_id) == 32
        assert isinstance(row.value, dict)  # 整行已重写为合法形态

    async def test_short_instance_id_regenerates(self, fake_db):
        """instance_id 短于站点下限 8：重新生成并清空日期记忆（防永久 422 静默）。"""
        row = self._make_row(
            {"instance_id": "x", "installed_at": "2026-08-01T00:00:00+00:00", "last_report_date": "2026-09-21"}
        )
        fake_db.preload(row)
        state = await ensure_instance_state(fake_db)
        assert len(state.instance_id) == 32
        assert state.last_report_date == ""  # 换身份即清日期，避免旧身份的日期挡住新身份首报
        assert row.value["instance_id"] == state.instance_id

    async def test_mark_report_sent_updates_in_place(self, fake_db):
        await ensure_instance_state(fake_db)  # 先建行
        from llmpod.models.system_config import SystemConfig

        row = fake_db.all(SystemConfig)[0]
        original_id = row.value["instance_id"]
        original_installed = row.value["installed_at"]

        await mark_report_sent(fake_db, original_id, date(2026, 9, 22))
        assert row.value["last_report_date"] == "2026-09-22"
        assert row.value["instance_id"] == original_id
        assert row.value["installed_at"] == original_installed
        assert len(fake_db.all(SystemConfig)) == 1  # 原地更新不加新行


class TestDailyGate:
    """report_if_due：当日已报不发；未报才发且成功落日期。"""

    @staticmethod
    def _patch_db(monkeypatch, fake_db):
        @asynccontextmanager
        async def _ctx():
            yield fake_db

        # report_if_due 函数体内局部 import，须 patch 源模块符号
        monkeypatch.setattr("llmpod.db.pg.get_db_context", _ctx)

    async def test_already_reported_today_skips(self, monkeypatch, fake_db):
        self._patch_db(monkeypatch, fake_db)
        from llmpod.utils.time import now_local

        fake_db.preload(
            SystemConfig_row(
                {
                    "instance_id": "a" * 32,
                    "installed_at": "2026-08-01T00:00:00+00:00",
                    "last_report_date": now_local().date().isoformat(),
                }
            )
        )
        calls = []

        async def fake_send(payload):
            calls.append(payload)
            return True

        monkeypatch.setattr(svc, "send_report", fake_send)
        await report_if_due()
        assert calls == []  # 当日已报 → 不构建不发送

    async def test_due_sends_and_marks(self, monkeypatch, fake_db):
        self._patch_db(monkeypatch, fake_db)
        fake_db.preload(SystemConfig_row({}))
        from llmpod.models.system_config import SystemConfig

        sent = []

        async def fake_send(payload):
            sent.append(payload)
            return True

        monkeypatch.setattr(svc, "send_report", fake_send)
        await report_if_due()
        assert len(sent) == 1
        assert len(sent[0].instance_id) == 32  # 首次走 ensure 生成
        # 成功后落了当日日期
        assert fake_db.all(SystemConfig)[0].value["last_report_date"] == sent[0].reported_at.date().isoformat()

    async def test_send_failure_not_marked(self, monkeypatch, fake_db):
        self._patch_db(monkeypatch, fake_db)
        fake_db.preload(SystemConfig_row({}))
        from llmpod.models.system_config import SystemConfig

        async def fake_send(payload):
            return False

        monkeypatch.setattr(svc, "send_report", fake_send)
        await report_if_due()
        assert fake_db.all(SystemConfig)[0].value["last_report_date"] == ""  # 失败不落日期

    async def test_force_bypasses_daily_gate_without_consuming_it(self, monkeypatch, fake_db):
        """force=True（启动首轮）直发且不落日期——当天后续 tick 仍正常报一次（共两次）。"""
        self._patch_db(monkeypatch, fake_db)
        fake_db.preload(
            SystemConfig_row(
                {
                    "instance_id": "a" * 32,
                    "installed_at": "2026-08-01T00:00:00+00:00",
                    "last_report_date": "2000-01-01",  # 当天还没报过
                }
            )
        )
        from llmpod.models.system_config import SystemConfig

        sent = []

        async def fake_send(payload):
            sent.append(payload)
            return True

        monkeypatch.setattr(svc, "send_report", fake_send)
        await report_if_due(force=True)
        assert len(sent) == 1
        assert fake_db.all(SystemConfig)[0].value["last_report_date"] == "2000-01-01"  # 直发不落日期

        await report_if_due()  # 同日下一 tick：门控未消费，仍正常遥测
        assert len(sent) == 2
        assert fake_db.all(SystemConfig)[0].value["last_report_date"] == sent[1].reported_at.date().isoformat()


def SystemConfig_row(value: dict):
    """构造 telemetry_instance 的 SystemConfig 行（TestDailyGate 用）。"""
    from llmpod.models.system_config import SystemConfig

    return SystemConfig(key=TELEMETRY_CONFIG_KEY, value=value)


class TestSendReport:
    """伪 httpx.AsyncClient：断言 URL/UA/请求体；成败与超限分支。"""

    @staticmethod
    def _make_payload(model_count=1):
        models = [TelemetryModelEntry(architecture="qwen2", kind="llm", size_gb=1.0) for _ in range(model_count)]
        return TelemetryPayload(
            instance_id="c" * 32,
            version=llmpod.__version__,
            installed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            reported_at=datetime(2026, 9, 22, 0, 2, 17, tzinfo=timezone.utc),
            models=models,
        )

    @staticmethod
    def _install_client(monkeypatch, response=None, exc=None):
        captured = {}

        class _Resp:
            is_success = response is not None and 200 <= response < 300
            status_code = response

        class _Client:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = headers
                if exc is not None:
                    raise exc
                return _Resp()

        monkeypatch.setattr(svc.httpx, "AsyncClient", _Client)
        return captured

    async def test_success_sends_schema2(self, monkeypatch):
        captured = self._install_client(monkeypatch, response=204)
        payload = self._make_payload()
        assert await send_report(payload) is True
        assert captured["url"] == "https://llmpod.ai/api/telemetry/report"
        assert captured["headers"]["User-Agent"] == f"llmpod/{llmpod.__version__}"
        # httpx 对 content=bytes 不自动补 Content-Type，缺头站点恒 422——必须显式带
        assert captured["headers"]["Content-Type"] == "application/json"
        body = json.loads(captured["content"])
        assert body["schema"] == 2
        assert body["instance_id"] == "c" * 32
        assert body["reported_at"] == "2026-09-22T00:02:17Z"

    async def test_http_error_status_is_failure(self, monkeypatch):
        self._install_client(monkeypatch, response=422)
        assert await send_report(self._make_payload()) is False

    async def test_network_error_is_failure(self, monkeypatch):
        self._install_client(monkeypatch, exc=httpx.ConnectError("boom", request=httpx.Request("POST", "https://x")))
        assert await send_report(self._make_payload()) is False

    async def test_oversized_body_drops_models(self, monkeypatch):
        captured = self._install_client(monkeypatch, response=204)
        monkeypatch.setattr(svc, "TELEMETRY_MAX_BODY_BYTES", 1)  # 强制超限（丢 models 后仍 >1B）
        assert await send_report(self._make_payload()) is False

        # 上限落在「丢 models 后」与「带 models」之间：丢 models 后应可发
        full = self._make_payload()
        without_models = full.model_copy(deep=True)
        without_models.models = []
        limit = (len(payload_body(without_models)) + len(payload_body(full))) // 2
        monkeypatch.setattr(svc, "TELEMETRY_MAX_BODY_BYTES", limit)
        assert await send_report(full) is True
        assert json.loads(captured["content"])["models"] == []


class TestTaskLifecycle:
    async def test_disabled_registers_nothing(self, monkeypatch):
        background_tasks = BackgroundTaskManager()
        monkeypatch.setattr(svc.settings, "telemetry_enabled", False)
        await start_telemetry_task(background_tasks)
        assert not background_tasks._tasks
        await background_tasks.stop()

    async def test_enabled_creates_named_task(self, monkeypatch):
        monkeypatch.setattr(svc.settings, "telemetry_enabled", True)
        monkeypatch.setattr(svc, "TELEMETRY_TICK_SECONDS", 3600.0)
        started = asyncio.Event()

        async def fake_report(**kwargs):
            started.set()
            # 挂住模拟长 sleep，让 cancel 有明确目标
            await asyncio.sleep(3600)

        monkeypatch.setattr(svc, "report_if_due", fake_report)
        background_tasks = BackgroundTaskManager()
        await start_telemetry_task(background_tasks)
        task = background_tasks._tasks["telemetry-report"]
        assert task.get_name() == "telemetry-report"
        await started.wait()
        await background_tasks.stop()
        assert task.cancelled()

    async def test_loop_survives_tick_exception(self, monkeypatch):
        """tick 抛异常被吞，循环继续下一轮。"""
        monkeypatch.setattr(svc.settings, "telemetry_enabled", True)
        ticks = []

        async def flaky_report(**kwargs):
            ticks.append(1)
            if len(ticks) == 1:
                raise ValueError("db down")
            raise asyncio.CancelledError()  # 第二轮借 cancel 结束循环

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(svc, "report_if_due", flaky_report)
        monkeypatch.setattr(svc.asyncio, "sleep", fake_sleep)
        task = asyncio.create_task(svc.run_telemetry_loop())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert len(ticks) == 2  # 第一轮异常没杀死循环
