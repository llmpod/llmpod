"""PullImageExecutor 单测：短路/拉取/失败/取消/重启探测（fake Deployer）。"""

from __future__ import annotations

import asyncio

from llmpod.apps.node.exceptions import TaskCancelled, TaskExecutionError
from llmpod.apps.node.executors import pull_image as pull_image_module
from llmpod.apps.node.executors.base import ByteProgressReporter
from llmpod.apps.node.executors.pull_image import PullImageExecutor
from llmpod.apps.node.models import LocalTask


def _fake_clock(step: float):
    """按调用次数步进的假时钟（每次 +step 秒）——注入 reporter 避免真等。"""
    calls = {"n": 0}

    def fake() -> float:
        calls["n"] += 1
        return calls["n"] * step

    return fake


def _patch_reporter_clock(monkeypatch, step: float):
    """executor 内构造 reporter 用的时间源换成假时钟（patch 类的默认 clock 参数）。"""
    real_cls = ByteProgressReporter

    class _ClockPatched(real_cls):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("clock", _fake_clock(step))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pull_image_module, "ByteProgressReporter", _ClockPatched)


class FakeDeployer:
    """可注入结果的 Deployer 桩：exists/pull_error/cancel/进度回调语义。"""

    def __init__(self, exists=False, pull_error=None, progress_points=(), probed_size=None):
        self.exists = exists
        self.pull_error = pull_error
        # pull 期间喂给 on_progress 的 (done, total) 序列
        self.progress_points = list(progress_points)
        self.probed_size = probed_size
        self.pull_calls: list[tuple[str, asyncio.Event | None]] = []
        self.probe_calls: list[str] = []

    async def image_exists(self, image):
        return self.exists

    async def image_download_size(self, image):
        self.probe_calls.append(image)
        return self.probed_size

    async def pull_image(self, image, cancel_event=None, on_progress=None):
        self.pull_calls.append((image, cancel_event))
        if on_progress is not None:
            for done, total in self.progress_points:
                await on_progress(done, total)
        if self.pull_error is not None:
            raise self.pull_error


def _task(image="vllm/vllm-openai:v0.23.0"):
    return LocalTask(
        task_id="t1",
        type="PULL_IMAGE",
        resource_type="IMAGE_PULL",
        resource_key="image:n1:abc",
        node_id="n1",
        status="RUNNING",
        payload={"image": image},
    )


def _collect():
    events: list[dict] = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        events.append(
            {
                "status": status,
                "progress": progress,
                "stage": stage,
                "message": message,
                "extra": extra,
            }
        )

    return events, report


class TestExecute:
    async def test_missing_image_ref_failed(self):
        ex = PullImageExecutor(FakeDeployer())
        events, report = _collect()
        await ex.execute(
            LocalTask(
                task_id="t1",
                type="PULL_IMAGE",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                status="RUNNING",
                payload={"image": "  "},
            ),
            None,
            report,
            asyncio.Event(),
        )
        assert events[-1]["status"] == "FAILED" and "缺少 image" in events[-1]["message"]

    async def test_invalid_payload_failed(self):
        """配方缺 image 字段 → 校验失败 FAILED（typed 契约在执行器入口把关）。"""
        ex = PullImageExecutor(FakeDeployer())
        events, report = _collect()
        await ex.execute(
            LocalTask(
                task_id="t1",
                type="PULL_IMAGE",
                resource_type="IMAGE_PULL",
                resource_key="image:n1:abc",
                node_id="n1",
                status="RUNNING",
                payload={},
            ),
            None,
            report,
            asyncio.Event(),
        )
        assert events[-1]["status"] == "FAILED" and "配方无效" in events[-1]["message"]

    async def test_image_exists_short_circuit_success(self):
        dep = FakeDeployer(exists=True)
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "SUCCESS" and events[-1]["progress"] == 100
        assert events[-1]["stage"] == "finished"
        assert events[-1]["extra"] == {"image": "vllm/vllm-openai:v0.23.0"}
        assert dep.pull_calls == []  # 短路不拉取

    async def test_pull_success_full_flow(self):
        dep = FakeDeployer(exists=False)
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        statuses = [e["status"] for e in events]
        assert statuses == ["RUNNING", "SUCCESS"]
        assert events[0]["stage"] == "pulling"
        assert events[0]["extra"] == {"image": "vllm/vllm-openai:v0.23.0"}
        assert dep.pull_calls and dep.pull_calls[0][0] == "vllm/vllm-openai:v0.23.0"
        # 快拉先完：在途的总量预探测被取消回收（结果已无消费方，不能独自挂在
        # registry 请求上无人认领）。探测落地路径由 test_probed_total_seeds_reporter 覆盖
        assert dep.probe_calls == []

    async def test_probed_total_seeds_reporter(self, monkeypatch):
        """预探测与拉取并行：进度报先走已出现层聚合（300），探测落地后按「分母取大」
        切到真实总量 10000——部分层和不缩分母、分母只增不减。假时钟 +5s/调用逐条放行。"""
        _patch_reporter_clock(monkeypatch, 5.0)
        dep = FakeDeployer(
            exists=False,
            probed_size=10_000,
            progress_points=[(0, 300), (3_000, 300), (10_000, 10_000)],
        )
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        running = [e for e in events if e["status"] == "RUNNING" and "bytes_done" in (e["extra"] or {})]
        totals = [e["extra"]["bytes_total"] for e in running]
        # 并行竞态下种子报与进度报的先后不确定，但不变量确定：
        # 分母单调不减、探测落地后（末两报）恒为真实总量、终值进度按真实总量到顶
        assert totals == sorted(totals)
        assert totals[-1] == 10_000
        assert 300 in totals and 10_000 in totals  # 两种口径都出现过（并行切换被观察到）
        # 终态前最后一报：done=10000 total=10000 → 99（clamp），SUCCESS 才是 100
        assert running[-1]["progress"] == 99

    async def test_probe_failure_falls_back(self, monkeypatch):
        """预探测失败（None）：分母由已出现层聚合逐步长出（旧行为）。"""
        _patch_reporter_clock(monkeypatch, 5.0)  # 报点间隔 5s → 逐条放行
        dep = FakeDeployer(
            exists=False,
            probed_size=None,
            progress_points=[(150, 300), (300, 300)],
        )
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        running = [e for e in events if e["status"] == "RUNNING" and "bytes_done" in (e["extra"] or {})]
        assert [e["extra"]["bytes_total"] for e in running] == [300, 300]

    async def test_probe_exception_falls_back(self):
        """预探测抛异常也不阻断拉取（吞掉按 None 走旧口径）。"""

        class _Boom(FakeDeployer):
            async def image_download_size(self, image):
                raise RuntimeError("network down")

        dep = _Boom(exists=False, progress_points=[(100, 100)])
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "SUCCESS"

    async def test_pull_progress_throttled_reports(self):
        # deployer 按字节增量回调；reporter 节流「最快 2s 一条」——burst 回调（dt≈0）
        # 中间报点全被 min_interval 压掉（wire 流量与 UI 刷新都按 2s 节奏走）；末点
        # (100,100) 是分子追平分母的终态检查点，恒放行（收尾到顶一报）
        dep = FakeDeployer(
            exists=False,
            progress_points=[(0, 100), (3, 100), (5, 100), (14, 100), (100, 100)],
        )
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        running = [e for e in events if e["status"] == "RUNNING" and "bytes_done" in (e["extra"] or {})]
        assert [e["progress"] for e in running] == [0, 99]
        assert all(e["stage"] == "pulling" for e in running)
        # context 键：reporter 把 image ref 透传进 extra
        assert all(e["extra"]["image"] == "vllm/vllm-openai:v0.23.0" for e in running)

    async def test_pull_progress_heartbeat_passes(self, monkeypatch):
        # 报点间隔 ≥2s（假时钟 +3s/调用）→ 逐条放行；百分比按 done/total 重算、封顶 99
        # （终值 100 由 SUCCESS 终态上报，与模型下载同契约）
        _patch_reporter_clock(monkeypatch, 3.0)
        dep = FakeDeployer(exists=False, progress_points=[(0, 100), (1, 100), (3, 100), (100, 100)])
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        running = [e for e in events if e["status"] == "RUNNING" and "bytes_done" in (e["extra"] or {})]
        assert [e["progress"] for e in running] == [0, 1, 3, 99]

    async def test_pull_progress_carries_rate(self, monkeypatch):
        # 第二个走 wire 的报点起 extra 带 bytes_per_sec（EMA 平滑速率，int）。
        # 假时钟 +2s/调用：报点 1 t=2，报点 2 t=4 → dt=2s，50B/2s=25B/s
        _patch_reporter_clock(monkeypatch, 2.0)
        dep = FakeDeployer(
            exists=False,
            progress_points=[(0, 100), (50, 100), (100, 100)],
        )
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        running = [e for e in events if e["status"] == "RUNNING" and "bytes_done" in (e["extra"] or {})]
        assert [e["progress"] for e in running] == [0, 50, 99]
        assert "bytes_per_sec" not in running[0]["extra"]
        assert running[1]["extra"]["bytes_per_sec"] == 25
        assert running[2]["extra"]["bytes_per_sec"] == 25  # EMA 平滑后仍 25

    async def test_pull_failure_reports_failed(self):
        dep = FakeDeployer(exists=False, pull_error=TaskExecutionError("docker pull failed: manifest unknown"))
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "FAILED"
        assert "manifest unknown" in events[-1]["message"]

    async def test_cancel_before_pull(self):
        dep = FakeDeployer(exists=False)
        ex = PullImageExecutor(dep)
        events, report = _collect()
        cancel = asyncio.Event()
        cancel.set()
        await ex.execute(_task(), None, report, cancel)
        assert events[-1]["status"] == "CANCELLED"
        assert dep.pull_calls == []

    async def test_cancel_during_pull(self):
        dep = FakeDeployer(exists=False, pull_error=TaskCancelled("pull cancelled"))
        ex = PullImageExecutor(dep)
        events, report = _collect()
        await ex.execute(_task(), None, report, asyncio.Event())
        assert events[-1]["status"] == "CANCELLED"


class TestReconcile:
    async def test_exists_returns_success(self):
        ex = PullImageExecutor(FakeDeployer(exists=True))
        r = await ex.reconcile(_task())
        assert r is not None and r.status == "SUCCESS"
        assert r.extra == {"image": "vllm/vllm-openai:v0.23.0"}

    async def test_absent_returns_none(self):
        ex = PullImageExecutor(FakeDeployer(exists=False))
        assert await ex.reconcile(_task()) is None

    async def test_missing_image_none(self):
        ex = PullImageExecutor(FakeDeployer(exists=True))
        assert (
            await ex.reconcile(
                LocalTask(
                    task_id="t1",
                    type="PULL_IMAGE",
                    resource_type="IMAGE_PULL",
                    resource_key="image:n1:abc",
                    node_id="n1",
                    status="RUNNING",
                    payload={},
                )
            )
            is None
        )
