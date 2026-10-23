"""deploy_log_service 单测：纯函数 helper + FakeSession 读写路径。

FakeSession 只支持 select.where(==)——list_logs/latest_log_by_node/delete_for_deployment
均按「deployment_id 单条件 .where + Python 侧过滤/分页」实现，与之兼容。
"""

from __future__ import annotations

import datetime


from llmpod.models.deploy_log import DeployLog
from llmpod.services import deploy_log_service as svc

from tests.integration._fakes import FakeSession

_BASE = datetime.datetime(2026, 8, 14, 12, tzinfo=datetime.timezone.utc)


def _ts(seconds: int) -> datetime.datetime:
    return _BASE + datetime.timedelta(seconds=seconds)


class TestHelpers:
    def test_phase_to_step_known(self):
        assert svc.phase_to_step("PENDING") == "queued"
        assert svc.phase_to_step("STAGING_MODEL") == "staging_model"
        assert svc.phase_to_step("PULL_IMAGE") == "pull_image"
        assert svc.phase_to_step("START_HEAD") == "start_head"
        assert svc.phase_to_step("START_WORKERS") == "start_workers"
        assert svc.phase_to_step("WAIT_HEAD_READY") == "head_health_check"
        assert svc.phase_to_step("WAIT_WORKERS_READY") == "worker_health_check"
        assert svc.phase_to_step("FINALIZING") == "finalize"
        assert svc.phase_to_step("RUNNING") == "running"
        assert svc.phase_to_step("STOPPING") == "stopping"

    def test_phase_to_step_unknown_defaults_orchestration(self):
        assert svc.phase_to_step("WAT") == "orchestration"
        assert svc.phase_to_step(None) == "orchestration"

    def test_phase_to_message(self):
        assert svc.phase_to_message("PENDING") == "排队中，等待调度"
        assert svc.phase_to_message("STAGING_MODEL") == "开始下载模型"
        assert svc.phase_to_message("WAIT_HEAD_READY") == "等待主节点健康检查通过"
        assert svc.phase_to_message("WAIT_WORKERS_READY") == "等待工作节点健康检查通过"
        assert svc.phase_to_message("RUNNING") == "服务已就绪"
        assert svc.phase_to_message(None) == "编排推进"

    def test_phase_to_log_status(self):
        assert svc.phase_to_log_status("RUNNING") == "success"
        assert svc.phase_to_log_status("STOPPING") == "running"
        assert svc.phase_to_log_status("PULL_IMAGE") == "running"
        assert svc.phase_to_log_status("START_HEAD") == "running"

    def test_norm_level(self):
        assert svc._norm_level("ERROR") == "error"
        assert svc._norm_level(None) == "info"
        assert svc._norm_level("bogus") == "info"

    def test_norm_status(self):
        assert svc._norm_status("FAILED") == "failed"
        assert svc._norm_status(None) is None
        assert svc._norm_status("bogus") is None


class TestAppendLog:
    async def test_append_log_writes_normalized_row(self):
        db = FakeSession()
        await svc.append_log(
            db,
            deployment_id="d1",
            step="submit",
            level="INFO",
            status="PENDING",
            message="hello",
            node_id="n1",
            extra={"k": 1},
        )
        rows = db.all(DeployLog)
        assert len(rows) == 1
        r = rows[0]
        assert r.deployment_id == "d1" and r.step == "submit"
        assert r.level == "info" and r.status == "pending"  # 归一化小写
        assert r.message == "hello" and r.extra == {"k": 1}

    async def test_append_log_unknown_step_still_persisted(self):
        """未知 step 仍入库（不阻断日志），仅 log.warning。"""
        db = FakeSession()
        await svc.append_log(db, deployment_id="d1", step="typo_step")
        rows = db.all(DeployLog)
        assert len(rows) == 1 and rows[0].step == "typo_step"


class TestRecord:
    async def test_record_best_effort_no_raise(self, monkeypatch):
        """record 写失败绝不外传（get_db_context 抛错 → 仅 debug）。"""

        class _BoomCtx:
            async def __aenter__(self):
                raise RuntimeError("db down")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(svc, "get_db_context", lambda: _BoomCtx())
        # 不抛即通过
        await svc.record(deployment_id="d1", step="submit", message="x")


async def _seed(db: FakeSession, rows_spec):
    """批量 append_log 并手动设 created_at（FakeSession 无 server_default）。"""
    for i, (node, step, level, status, msg) in enumerate(rows_spec):
        await svc.append_log(
            db,
            deployment_id="d1",
            node_id=node,
            step=step,
            level=level,
            status=status,
            message=msg,
        )
    for i, obj in enumerate(db.all(DeployLog)):
        obj.created_at = _ts(i)


class TestListLogs:
    async def test_list_all_sorted_asc_with_total(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "提交"),
                ("n1", "start_instance", "info", "running", "启动"),
                ("n1", "head_health_check", "info", "success", "就绪"),
                ("n2", "start_instance", "error", "failed", "OOM crash"),
                (None, "running", "info", "success", "phase RUNNING"),
            ],
        )
        items, total = await svc.list_logs(db, deployment_id="d1")
        assert total == 5
        assert [r.step for r in items] == ["submit", "start_instance", "head_health_check", "start_instance", "running"]

    async def test_list_filter_node_and_pagination(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "s0"),
                ("n1", "start_instance", "info", "running", "s1"),
                ("n2", "start_instance", "info", "running", "s2"),
                ("n2", "head_health_check", "info", "success", "s3"),
            ],
        )
        # 仅 n2：2 条
        items, total = await svc.list_logs(db, deployment_id="d1", node_id="n2")
        assert total == 2 and [r.message for r in items] == ["s2", "s3"]
        # 分页：page_size=1 page=2 → 第 2 条（n2 的第二条）
        items, total = await svc.list_logs(db, deployment_id="d1", node_id="n2", page=2, page_size=1)
        assert total == 2 and len(items) == 1 and items[0].message == "s3"

    async def test_list_filter_step_level_status(self):
        db = FakeSession()
        await _seed(
            db,
            [
                ("n1", "start_instance", "info", "running", "a"),
                ("n1", "start_instance", "error", "failed", "b"),
                ("n1", "head_health_check", "info", "success", "c"),
            ],
        )
        items, total = await svc.list_logs(db, deployment_id="d1", step="start_instance")
        assert total == 2
        items, total = await svc.list_logs(db, deployment_id="d1", level="error")
        assert total == 1 and items[0].message == "b"
        items, total = await svc.list_logs(db, deployment_id="d1", status="success")
        assert total == 1 and items[0].message == "c"

    async def test_list_search_case_insensitive(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "deployment submitted"),
                ("n1", "start_instance", "error", "failed", "OOM Crash Detected"),
            ],
        )
        items, total = await svc.list_logs(db, deployment_id="d1", search="crash")
        assert total == 1 and items[0].message.startswith("OOM")

    async def test_list_since_until(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "s0"),
                ("n1", "start_instance", "info", "running", "s1"),
                ("n1", "head_health_check", "info", "success", "s2"),
            ],
        )
        items, total = await svc.list_logs(db, deployment_id="d1", since=_ts(1))
        assert total == 2 and [r.message for r in items] == ["s1", "s2"]
        items, total = await svc.list_logs(db, deployment_id="d1", until=_ts(0))
        assert total == 1 and items[0].message == "s0"


class TestSummarizeByNode:
    async def test_last_per_node_excludes_orchestration(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "编排事件（无 node_id）"),
                ("n1", "start_instance", "info", "running", "n1 启动"),
                ("n1", "head_health_check", "info", "success", "n1 就绪"),
                ("n2", "start_instance", "error", "failed", "n2 崩溃"),
            ],
        )
        summ = await svc.latest_log_by_node(db, "d1")
        by_node = {s.node_id: s for s in summ}
        assert set(by_node) == {"n1", "n2"}  # 编排事件（node_id=None）排除
        # 每节点取最后一条（升序后者覆盖）
        assert by_node["n1"].step == "head_health_check" and by_node["n1"].status == "success"
        assert by_node["n2"].step == "start_instance" and by_node["n2"].status == "failed"
        assert by_node["n1"].created_at is not None


class TestDeleteForDeployment:
    async def test_delete_all_for_deployment(self):
        db = FakeSession()
        await _seed(
            db,
            [
                (None, "submit", "info", "pending", "a"),
                ("n1", "start_instance", "info", "running", "b"),
            ],
        )
        n = await svc.delete_for_deployment(db, "d1")
        assert n == 2 and db.all(DeployLog) == []
