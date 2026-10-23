"""node_service 单元测试：sync_gpus 对脏数据的健壮性。

arch 缺失的 GPU 不应中断整个节点注册——跳过+告警，其余 GPU 照常 upsert。
（断开真实 DB：最小桩 session，仅承接 execute/add/flush。）
"""

from __future__ import annotations

from llmpod.tunnel.schemas.system import GpuInfo, NodeReport


class _FakeResult:
    def scalar_one_or_none(self):
        return None  # 全部走 insert 分支

    def scalars(self):
        return []  # 批量预取无已有行（同样全走 insert）


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    async def execute(self, stmt):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


async def test_sync_gpus_skips_gpu_missing_arch():
    """arch 缺失 → 跳过该卡，不抛异常、不阻断其余 GPU。"""
    from llmpod.services.node_service import sync_gpus

    db = _FakeSession()
    await sync_gpus(
        "node-1",
        [
            GpuInfo(uuid="bad-no-arch", index=0, name="Weird"),  # 应被跳过
            GpuInfo(uuid="good", index=0, arch="nvidia", name="A100", total_vram_gb=40),
        ],
        db,
    )

    assert len(db.added) == 1
    g = db.added[0]
    assert g.uuid == "good"
    assert g.arch == "nvidia"
    assert g.node_id == "node-1"


async def test_sync_gpus_all_bad_does_not_raise():
    """所有 GPU 都缺 arch → 一个都不入库，但不抛异常（不阻断 on_hello 发 ack）。"""
    from llmpod.services.node_service import sync_gpus

    db = _FakeSession()
    await sync_gpus(
        "node-2",
        [
            GpuInfo(uuid="a", index=0, name="x"),
            GpuInfo(uuid="b", index=0, name="x"),
            GpuInfo(uuid="c", index=0, name="x"),
        ],
        db,
    )
    assert db.added == []


def test_enrich_nodes_runtime_merges_buffers(monkeypatch):
    """在线节点：从 system_buffer + gpu_buffer 聚合出 runtime 摘要；离线节点为 None。"""
    from llmpod.apps.manager.buffers import GpuStatusBuffer, SystemStatusBuffer
    from llmpod.models.node import Node
    from llmpod.services.node_service import enrich_nodes_runtime

    sys_buf = SystemStatusBuffer()
    sys_buf.update(
        "n1",
        NodeReport(
            hostname="h1",
            ip="1.2.3.4",
            cpu_percent=33.3,
            memory_used_gb=16.0,
            memory_percent=50.0,
            disk_total_gb=500.0,
            disk_used_gb=100.0,
            disk_percent=20.0,
        ),
    )
    gpu_buf = GpuStatusBuffer()
    gpu_buf.update(
        "n1",
        [
            GpuInfo(
                uuid="g1", index=0, name="A100", arch="nvidia", utilization=80.0, used_vram_gb=30.0, total_vram_gb=40.0
            ),
            GpuInfo(
                uuid="g2", index=1, name="A100", arch="nvidia", utilization=60.0, used_vram_gb=20.0, total_vram_gb=40.0
            ),
        ],
    )
    monkeypatch.setattr("llmpod.apps.manager.app.gpu_buffer", gpu_buf)
    monkeypatch.setattr("llmpod.apps.manager.app.system_buffer", sys_buf)

    nodes = [
        Node(id="n1", cluster_id=1, hostname="h1", ip="1.2.3.4"),
        Node(id="offline", cluster_id=1, hostname="h2", ip="1.2.3.5"),
    ]
    enrich_nodes_runtime(nodes)

    rt = nodes[0].runtime
    assert rt["cpu_percent"] == 33.3
    assert rt["memory_used_gb"] == 16.0
    assert rt["memory_percent"] == 50.0
    assert rt["disk_total_gb"] == 500.0
    assert rt["disk_percent"] == 20.0
    assert rt["gpu_count"] == 2
    assert rt["gpu_utilization"] == 70.0  # (80 + 60) / 2
    assert rt["used_vram_gb"] == 50.0  # 30 + 20
    assert rt["total_vram_gb"] == 80.0  # 40 + 40
    assert nodes[1].runtime is None


def test_enrich_nodes_runtime_buffers_none(monkeypatch):
    """Manager 缓冲未初始化（None）→ 不抛异常，runtime 全为 None。"""
    from llmpod.models.node import Node
    from llmpod.services.node_service import enrich_nodes_runtime

    monkeypatch.setattr("llmpod.apps.manager.app.gpu_buffer", None)
    monkeypatch.setattr("llmpod.apps.manager.app.system_buffer", None)

    nodes = [Node(id="n1", cluster_id=1, hostname="h1", ip="1.2.3.4")]
    enrich_nodes_runtime(nodes)
    assert nodes[0].runtime is None


def test_enrich_gpus_runtime_merges_buffer(monkeypatch):
    """缓冲有该卡 → 回填实时指标摘要（含 round 精度）；无该卡数据 → runtime=None。"""
    from llmpod.apps.manager.buffers import GpuStatusBuffer
    from llmpod.models.gpu import Gpu
    from llmpod.services.node_service import enrich_gpus_runtime

    gpu_buf = GpuStatusBuffer()
    gpu_buf.update(
        "n1",
        [
            GpuInfo(
                uuid="g1",
                index=0,
                name="A100",
                arch="nvidia",
                utilization=80.44,
                used_vram_gb=30.516,
                total_vram_gb=40.0,
                memory_utilization=76.34,
                temperature=65,
                fan_speed=70,
                power_draw_w=250.44,
                power_limit_w=300.0,
                sm_clock_mhz=1410,
                mem_clock_mhz=1215,
                process_count=2,
            ),
        ],
    )
    monkeypatch.setattr("llmpod.apps.manager.app.gpu_buffer", gpu_buf)

    gpus = [
        Gpu(id=1, uuid="g1", node_id="n1", index=0, name="A100", arch="nvidia", total_vram_gb=40.0, status="online"),
        Gpu(
            id=2,
            uuid="g-missing",
            node_id="n1",
            index=1,
            name="A100",
            arch="nvidia",
            total_vram_gb=40.0,
            status="offline",
        ),
    ]
    enrich_gpus_runtime(gpus)

    rt = gpus[0].runtime
    assert rt.used_vram_gb == 30.52  # round(x, 2)
    assert rt.memory_utilization == 76.3  # round(x, 1)
    assert rt.utilization == 80.4
    assert rt.temperature == 65
    assert rt.fan_speed == 70
    assert rt.power_draw_w == 250.4
    assert rt.power_limit_w == 300.0
    assert rt.sm_clock_mhz == 1410
    assert rt.mem_clock_mhz == 1215
    assert rt.process_count == 2
    assert gpus[1].runtime is None  # 缓冲无该卡


def test_enrich_gpus_runtime_buffers_none(monkeypatch):
    """Manager 缓冲未初始化（None）→ 不抛异常，runtime 全为 None。"""
    from llmpod.models.gpu import Gpu
    from llmpod.services.node_service import enrich_gpus_runtime

    monkeypatch.setattr("llmpod.apps.manager.app.gpu_buffer", None)

    gpus = [
        Gpu(id=1, uuid="g1", node_id="n1", index=0, name="A100", arch="nvidia", total_vram_gb=40.0, status="online")
    ]
    enrich_gpus_runtime(gpus)
    assert gpus[0].runtime is None
