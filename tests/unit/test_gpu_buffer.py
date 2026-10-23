"""Manager 端 GPU 状态内存缓冲区单元测试。"""

from __future__ import annotations

from llmpod.apps.manager.buffers import GpuStatusBuffer
from llmpod.tunnel.schemas import GpuInfo


def _gpu(uuid: str, **kw) -> GpuInfo:
    return GpuInfo(uuid=uuid, index=0, name="G1", **kw)


class TestGpuStatusBuffer:
    def test_update_and_get(self):
        buf = GpuStatusBuffer()
        gpus = [_gpu("GPU-1", utilization=50.0)]
        buf.update("node-a", gpus)
        assert buf.get("node-a") == gpus

    def test_get_missing_returns_empty(self):
        buf = GpuStatusBuffer()
        assert buf.get("absent") == []

    def test_get_all(self):
        buf = GpuStatusBuffer()
        buf.update("node-a", [_gpu("GPU-1")])
        buf.update("node-b", [_gpu("GPU-2")])
        all_buf = buf.get_all()
        assert set(all_buf.keys()) == {"node-a", "node-b"}

    def test_update_overwrites(self):
        """新帧应覆盖旧帧，保持最新实时状态。"""
        buf = GpuStatusBuffer()
        buf.update("node-a", [_gpu("GPU-1", utilization=10.0)])
        buf.update("node-a", [_gpu("GPU-1", utilization=90.0)])
        assert buf.get("node-a")[0].utilization == 90.0

    def test_remove_node(self):
        buf = GpuStatusBuffer()
        buf.update("node-a", [_gpu("GPU-1")])
        buf.remove_node("node-a")
        assert buf.get("node-a") == []
        assert "node-a" not in buf.get_all()

    def test_clear(self):
        buf = GpuStatusBuffer()
        buf.update("node-a", [_gpu("GPU-1")])
        buf.clear()
        assert buf.get_all() == {}
