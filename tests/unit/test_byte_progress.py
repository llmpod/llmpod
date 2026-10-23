"""``ByteProgressReporter`` 单测：下载进度上报的节流规则（纯逻辑，无 I/O）。

下载器逐 chunk 都有字节数，直报会刷爆上报通道与 deploy_log——本类按「距上次放行
≥min_interval 秒」放行（纯时间节拍，停滞期也发心跳）；``force`` 绕过节拍（文件边界
检查点）但不污染速率 EMA。镜像拉取与模型下载（HF / local 源）共用；速率只认真实
传输（force/过短窗口不进 EMA）。时间用注入的假时钟，断言不依赖真实等待。
"""

from __future__ import annotations

from llmpod.apps.node.executors.base import ByteProgressReporter


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float) -> None:
        self.t += dt


def _recorder():
    calls = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        calls.append({"status": status, "progress": progress, "stage": stage, "extra": extra or {}})

    return calls, report


def _reporter(report, clock, *, bytes_total=1000, **kw):
    return ByteProgressReporter(report, bytes_total=bytes_total, stage="downloading", clock=clock, **kw)


class TestByteProgressReporter:
    async def test_first_report_always_passes(self):
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock)
        await r.report(0)
        assert [c["progress"] for c in calls] == [0]

    async def test_drops_within_min_interval(self):
        """纯时间节拍：节拍窗口内的一切报点（无论 Δ 多大）都被压掉。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=5.0)
        await r.report(0)  # 首报
        await r.report(500)  # Δ=50% 但 dt=0 < 5s → 丢
        clock.tick(4.9)
        await r.report(900)  # 仍差 0.1s → 丢
        assert [c["progress"] for c in calls] == [0]
        clock.tick(0.2)  # 距上次放行 5.1s
        await r.report(900)
        assert [c["progress"] for c in calls] == [0, 90]

    async def test_reached_top_passes_gate(self):
        """分子追平分母（终态检查点）绕过节拍：镜像拉取收尾「补报到顶」的总分母
        与预探测种入值相等（grew=False），若无此放行会被 2s 节流吞掉——时间线
        停在低百分比直接跳终态，收尾补报等于没报。速率 EMA 不受跳满污染。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=5.0)
        await r.report(100)  # 首报 10%
        await r.report(1000, 1000)  # dt=0 且非 grew（分母未变）→ 到顶必须放行
        assert [c["progress"] for c in calls] == [10, 99]
        assert "bytes_per_sec" not in calls[-1]["extra"]  # 终态跳满不进 EMA

    async def test_heartbeat_passes_without_delta(self):
        """停滞（Δ=0）过了节拍也放行——前端持续看到存活心跳。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=2.0)
        await r.report(100)
        clock.tick(2.0)
        await r.report(100)  # 字节没动，节拍到 → 放
        assert len(calls) == 2

    async def test_force_bypasses_throttle(self):
        """force（文件边界检查点）绕过节拍立即放行。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=60.0)
        await r.report(0)
        await r.report(500, force=True)  # 节拍远未到
        assert [c["progress"] for c in calls] == [0, 50]

    async def test_rate_ema_on_real_windows(self):
        """速率 EMA：真实传输窗口（dt ≥ 0.5s 且非 force）进 EMA（0.3/0.7 平滑）。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=0.0)
        clock.tick(1.0)
        await r.report(100)  # 首报：无前点，不带速率
        clock.tick(2.0)
        await r.report(300)  # 200B/2s = 100B/s
        clock.tick(2.0)
        await r.report(900)  # 600B/2s = 300B/s → EMA = 0.3*300 + 0.7*100 = 160
        assert "bytes_per_sec" not in calls[0]["extra"]
        assert calls[1]["extra"]["bytes_per_sec"] == 100
        assert calls[2]["extra"]["bytes_per_sec"] == 160

    async def test_force_and_short_window_do_not_spike_rate(self):
        """跳档不虚增速率：force 检查点与过短窗口只平移基线、沿用 EMA——文件整段跳过
        /续传瞬间 +18GB 不会把「6.2GB/s」留在时间线上。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=0.0, bytes_total=None)
        clock.tick(1.0)
        await r.report(1000)
        clock.tick(1.0)
        await r.report(2000)  # 1000B/1s = 1000B/s，EMA=1000
        clock.tick(1.0)
        # 跳过一堆文件但未到分母顶（20GB done / 远大于的总分母场景由 bytes_total=None
        # 构造保证）——reached_top 与 force 同为检查点，只平移基线不进 EMA
        await r.report(20_000_000_000, force=True)
        assert calls[-1]["extra"]["bytes_per_sec"] == 1000  # EMA 不被 2×10^10/s 污染
        clock.tick(0.1)
        await r.report(20_000_000_100)  # 真实放行但 dt=0.1 < 0.5s：沿用 EMA
        assert calls[-1]["extra"]["bytes_per_sec"] == 1000
        clock.tick(0.5)
        await r.report(20_000_000_100)  # 停滞干净窗口：Δbytes=0 → 0B/s，EMA = 0.3*0+0.7*1000 = 700
        assert calls[-1]["extra"]["bytes_per_sec"] == 700

    async def test_progress_clamped_below_100(self):
        """中间进度封顶 99：100 是终值，由调用方在全部文件落定后上报。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, min_interval=0.0)
        await r.report(1000)
        assert calls[-1]["progress"] == 99

    async def test_extra_frozen_keys_and_stage(self):
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, bytes_total=2000)
        await r.report(500)
        c = calls[-1]
        assert c["status"] == "RUNNING" and c["stage"] == "downloading"
        assert c["extra"] == {"bytes_done": 500, "bytes_total": 2000}

    async def test_context_keys_into_extra(self):
        """context（如镜像 ref）原样并入 extra。"""
        clock = _Clock()
        calls, report = _recorder()
        r = ByteProgressReporter(report, bytes_total=200, stage="pulling", context={"image": "x:1"}, clock=clock)
        await r.report(100)
        assert calls[0]["extra"] == {"bytes_done": 100, "bytes_total": 200, "image": "x:1"}

    async def test_unknown_total_never_divides_by_zero(self):
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, bytes_total=None, min_interval=0.0)
        await r.report(123)
        assert calls[-1]["progress"] == 0
        assert calls[-1]["extra"] == {"bytes_done": 123, "bytes_total": None}

    async def test_seeded_total_not_shrunk_by_partial_sums(self):
        """预探测总量种入后，中途的部分层和（更小）不缩分母——百分比按真实总量算。"""
        clock = _Clock()
        calls, report = _recorder()
        probed = 2 * 2**30  # 2GB（manifest 全部 blob 之和）
        r = _reporter(report, clock, bytes_total=probed, min_interval=0.0)
        await r.report(100 * 2**20, 300 * 2**20)  # 已出现层聚合 300MB < 2GB → 分母保持 2GB
        assert calls[-1]["extra"]["bytes_total"] == probed
        assert calls[-1]["progress"] == 4  # 100MB / 2GB ≈ 4%（按真实总量，不是 33%）

    async def test_total_growth_passes_and_monotonic(self):
        """bytes_total 增长（层和超过探测值）：即刻放行一报更新分母；百分比不回退。"""
        clock = _Clock()
        calls, report = _recorder()
        r = _reporter(report, clock, bytes_total=100, min_interval=60.0)
        clock.tick(1.0)
        await r.report(50)  # 首报 50/100 = 50%
        clock.tick(1.0)
        await r.report(50, 500)  # 分母 100→500（grew）：节拍未到也放行；50/500=10% → 单调守卫仍报 50
        assert len(calls) == 2
        assert calls[1]["extra"]["bytes_total"] == 500
        assert calls[1]["progress"] == 50
