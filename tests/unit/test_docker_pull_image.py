"""DockerDeployer.pull_image SDK 流式拉取单测：layer 聚合 + 单调守卫 + cancel + 异常包装。"""

from __future__ import annotations

import asyncio

import pytest
import requests.exceptions as requests_exceptions
from docker.errors import APIError, ImageNotFound, StreamParseError

import llmpod.apps.node.deployers.docker as dmod
from llmpod.apps.node.deployers.docker import DockerDeployer, ProbeEntry as _ProbeEntry, _update_pull_layers
from llmpod.apps.node.exceptions import TaskCancelled, TaskExecutionError


def _chunk(lid, cur, tot):
    return {"id": lid, "progressDetail": {"current": cur, "total": tot}}


class TestUpdatePullLayers:
    def test_aggregates_across_layers(self):
        layers = {}
        done, total = _update_pull_layers(layers, _chunk("a", 30, 100))
        assert (done, total) == (30, 100)
        # 新层出现：分母只计已出现层
        done, total = _update_pull_layers(layers, _chunk("b", 0, 400))
        assert (done, total) == (30, 500)

    def test_same_layer_takes_max(self):
        layers = {}
        _update_pull_layers(layers, _chunk("a", 50, 100))
        done, _ = _update_pull_layers(layers, _chunk("a", 40, 100))  # 回退 chunk
        assert done == 50

    def test_non_progress_chunks_ignored(self):
        layers = {}
        _update_pull_layers(layers, _chunk("a", 10, 100))
        # status/aux 等非进度 chunk 不影响累计
        done, total = _update_pull_layers(layers, {"status": "Pulling from fs", "id": None})
        assert (done, total) == (10, 100)
        done, total = _update_pull_layers(layers, {"id": "a", "status": "Download complete"})  # 无 progressDetail
        assert (done, total) == (10, 100)

    def test_complete_status_credits_known_blob(self):
        """本地已缓存层（Already exists 等完成态 chunk 无 progressDetail）按探测 digest
        清单配对补足分子——否则种入的总量分母到不了头（CUDA 基础层共享场景）。"""
        layers = {}
        known = {"aabbccddeeff0011": 100, "1122334455667788": 400}
        done, total = _update_pull_layers(
            layers,
            {"id": "aabbccddeeff0011", "status": "Already exists"},  # 全串 id 直接命中
            known_blobs=known,
        )
        assert (done, total) == (100, 100)
        # stream id 是 digest 短前缀（docker CLI 同款 12 hex）→ 前缀配对
        done, total = _update_pull_layers(
            layers,
            {"id": "112233445566", "status": "Download complete"},
            known_blobs=known,
        )
        assert (done, total) == (500, 500)
        # 无探测清单时完成态 chunk 维持旧行为（不进分子）
        layers2 = {}
        done, total = _update_pull_layers(layers2, {"id": "aabbccddeeff0011", "status": "Already exists"})
        assert (done, total) == (0, 0)


class _FakeApi:
    """低层 ``client.api.pull`` 的桩：流式语义（返回 chunk 迭代器）。

    高层 ``images.pull`` 在真实 SDK 里会同步消费完整拉取后返回 ``Image`` 对象
    （迭代抛 TypeError），mock 成返回迭代器会把该缺陷完全掩盖——生产代码走
    ``api.pull``，桩也按 ``api.pull`` 仿。

    ``stream`` 可传任意可迭代（生成器/自定义迭代器）：中途抛非 docker 异常的
    截断形态（StreamParseError/requests 系）用它构造。
    """

    def __init__(self, chunks, error=None):
        self._chunks = chunks
        self._error = error

    def pull(self, image, stream=True, decode=True):
        if self._error is not None:
            raise self._error
        return iter(self._chunks)  # iter() 对生成器/自定义迭代器是恒等


class _FakeImages:
    """高层 images.pull 在此直接断言失败：pull_image 必须走低层 api.pull 拿流式进度。

    images.get 供流结束后的终态校验桩使用（present 控制镜像是否真落盘；
    get_error 模拟 daemon 瞬时故障抛非 docker 异常）。
    """

    def __init__(self, present=False):
        self.present = present
        self.get_error: Exception | None = None
        self.get_calls: list[str] = []

    def pull(self, image, stream=True, decode=True):
        raise AssertionError("pull_image 必须走低层 api.pull，不得用高层 images.pull")

    def get(self, image):
        self.get_calls.append(image)
        if self.get_error is not None:
            raise self.get_error
        if not self.present:
            raise ImageNotFound(f"image not found: {image}")
        return object()


class _FakeClient:
    def __init__(self, chunks, error=None, present=False):
        self.api = _FakeApi(chunks, error)
        self.images = _FakeImages(present=present)


def _patch_client(monkeypatch, chunks=(), error=None, present=True):
    client = _FakeClient(chunks, error, present=present)
    monkeypatch.setattr(dmod, "_get_client", lambda: client)
    return client


class TestPullImage:
    async def test_progress_monotonic_on_new_layer(self, monkeypatch):
        """新层出现使分母增大、百分比回退 → 回调序列单调不减（守卫在 reporter，回调只给字节）。"""
        _patch_client(
            monkeypatch,
            [
                _chunk("a", 50, 100),
                _chunk("b", 0, 400),  # 新层出现：done 不动（50/500），不回调
                _chunk("b", 400, 400),  # 450/500
                _chunk("a", 100, 100),  # 500/500
            ],
        )
        seen: list[tuple[int, int]] = []

        async def on_progress(done, total):
            seen.append((done, total))

        await DockerDeployer().pull_image("x:1", None, on_progress)
        # done 单调；百分比计算上移到 reporter（带预探测总量取大）
        dones = [d for d, _ in seen]
        assert dones == sorted(dones)
        assert dones[-1] == 500

    async def test_no_callback_when_not_requested(self, monkeypatch):
        _patch_client(monkeypatch, [_chunk("a", 50, 100)])
        await DockerDeployer().pull_image("x:1")  # on_progress=None 不炸

    async def test_cancel_mid_stream(self, monkeypatch):
        _patch_client(monkeypatch, [_chunk("a", 50, 100), _chunk("a", 60, 100)])
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(TaskCancelled):
            await DockerDeployer().pull_image("x:1", cancel)

    async def test_api_error_wrapped(self, monkeypatch):
        _patch_client(
            monkeypatch,
            error=APIError("unauthorized: authentication required"),
        )
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("private/registry/x:1")
        assert "authentication required" in str(ei.value)

    async def test_stream_end_snaps_to_probed_total(self, monkeypatch):
        """流结束补一报到预探测总量：本地缓存的层不发进度行（分子中途到不了顶），
        收尾回调 (total, total) 让时间线终结在满格而不是悬空。"""
        _patch_client(
            monkeypatch,
            [
                _chunk("650370485ae3", 1048576, 2165603),
                {"id": "650370485ae3", "status": "Download complete", "progressDetail": {"hidecounts": True}},
            ],
        )
        dep = DockerDeployer()
        dep._probe_layers["x:1"] = _ProbeEntry(aliases={"aabb": 100, "650370485ae3": 2165603}, unique_total=2165703)
        seen: list[tuple[int, int]] = []

        async def on_progress(done, total):
            seen.append((done, total))

        await dep.pull_image("x:1", None, on_progress)
        # 中途：下载层 2165603/2165603；收尾：跳到预探测总量 2165703（含缓存层 aabb=100）
        assert seen[-1] == (2165703, 2165703)

    async def test_no_snap_without_probe_cache(self, monkeypatch):
        """探测失败（无清单）时流结束不补报——总量本来就按已出现层聚合，能到顶。"""
        _patch_client(monkeypatch, [_chunk("a", 50, 100), _chunk("a", 100, 100)])
        seen: list[tuple[int, int]] = []

        async def on_progress(done, total):
            seen.append((done, total))

        await DockerDeployer().pull_image("x:1", None, on_progress)
        assert seen[-1] == (100, 100)

    async def test_in_stream_error_raises(self, monkeypatch):
        """daemon 拉取中途失败（典型：解压层失败/磁盘写满）时 HTTP 仍 200，错误以
        ``{"error": ...}`` chunk 随流下发、流随即正常结束——低层 ``api.pull`` 不抛
        异常。不识别该 chunk 会把失败拉取当成功上报（假 SUCCESS，镜像从未落盘）。"""
        _patch_client(
            monkeypatch,
            [
                _chunk("a", 100, 100),
                {
                    "errorDetail": {"message": "failed to register layer: no space left on device"},
                    "error": "failed to register layer: no space left on device",
                },
            ],
        )
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("x:1")
        assert "no space left on device" in str(ei.value)

    async def test_stream_end_but_image_missing_raises(self, monkeypatch):
        """流静默截断（连接中断、无 error chunk）→ 流「正常」结束但镜像未落盘：
        收尾必须复核 images.get，缺失按失败上报，不得返回成功。"""
        client = _patch_client(monkeypatch, [_chunk("a", 100, 100)], present=False)
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("x:1")
        assert client.images.get_calls == ["x:1"]
        assert "未落盘" in str(ei.value)

    async def test_snap_total_counts_each_blob_once(self, monkeypatch):
        """收尾补报的总量 = 探测清单 digest 唯一之和。清单同时存 full digest 与
        12 位前缀别名（流 id 配对用）——按 dict 值求和会把每个 blob 双计
        （真实事故：8.4GB 镜像收尾显示 16.8GB/16.8GB）。"""
        full = "a" * 64
        _patch_client(monkeypatch, [_chunk(full[:12], 100, 2165603)])
        dep = DockerDeployer()
        dep._probe_layers["x:1"] = _ProbeEntry(aliases={full: 2165603, full[:12]: 2165603}, unique_total=2165603)
        seen: list[tuple[int, int]] = []

        async def on_progress(done, total):
            seen.append((done, total))

        await dep.pull_image("x:1", None, on_progress)
        assert seen[-1] == (2165603, 2165603)

    async def test_truncated_stream_parse_error_wrapped(self, monkeypatch):
        """连接中断切在 JSON 行中间：SDK 迭代器抛 StreamParseError（RuntimeError 系，
        不与 APIError 相交）。必须统一包装成 TaskExecutionError 带原文——裸抛会把
        「Unterminated string ...」直接漏给时间线。"""

        def _gen():
            yield _chunk("a", 50, 100)
            raise StreamParseError("Unterminated string starting at: line 1 column 11")

        _patch_client(monkeypatch, chunks=_gen())
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("x:1")
        assert "Unterminated string" in str(ei.value)

    async def test_connection_error_wrapped(self, monkeypatch):
        """连接层错误（requests.exceptions，SDK 不包装）同样进统一 TaskExecutionError。"""

        def _gen():
            yield _chunk("a", 50, 100)
            raise requests_exceptions.ChunkedEncodingError("Connection broken: inbound data overflow")

        _patch_client(monkeypatch, chunks=_gen())
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("x:1")
        assert "Connection broken" in str(ei.value)

    async def test_verify_daemon_error_wrapped_not_raw(self, monkeypatch):
        """流成功但收尾复核撞 daemon 瞬时故障（重启窗口抛 requests ConnectionError）：
        包装成 TaskExecutionError 而非裸英文逃逸——镜像已在盘，上层重试的下一轮
        image_exists 短路即可自愈。"""
        client = _patch_client(monkeypatch, [_chunk("a", 100, 100)], present=True)
        client.images.get_error = requests_exceptions.ConnectionError("daemon rebooting")
        with pytest.raises(TaskExecutionError) as ei:
            await DockerDeployer().pull_image("x:1")
        assert "拉取后校验失败" in str(ei.value)

    async def test_stream_iterator_closed_on_error(self, monkeypatch):
        """error chunk 路径 raise 后底层迭代器被确定性 close（未读的 chunked 响应
        不留给 GC 兜底）。"""
        closed = {"v": False}

        class _Iter:
            def __init__(self):
                self._chunks = [_chunk("a", 100, 100), {"error": "boom"}]

            def __iter__(self):
                return self

            def __next__(self):
                return self._chunks.pop(0)

            def close(self):
                closed["v"] = True

        _patch_client(monkeypatch, chunks=_Iter())
        with pytest.raises(TaskExecutionError):
            await DockerDeployer().pull_image("x:1")
        assert closed["v"] is True
