"""docker_pull / image_exists RPC 单测：流契约走异常失败通道，正常流原样透传。

docker_pull 是管理端「拉取镜像」的后端（`POST /orchestration/nodes/{id}/tasks/docker-pull`
经 `_stream_ndjson` 逐行 NDJSON）。流消费/错误识别/落盘复核统一在
`deployers.docker.iter_pull_chunks`（那里另有单测），本文件只锁定 RPC 层契约：
失败必须走异常（消费端把异常转 `{"event":"error"}` 收尾，路由文档化的失败通道）
而非产出 `pull failed:` 文本行（会被当普通 data 帧、前端显示成功——拉取假成功
在 RPC 路径的翻版）；以及 image_exists 的三态语义（只塌缩 ImageNotFound）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from docker.errors import APIError, ImageNotFound

import llmpod.apps.node.deployers.docker as docker_mod
import llmpod.apps.node.services.node as node_mod
from llmpod.apps.node.exceptions import TaskExecutionError


def _svc() -> node_mod.NodeServiceImpl:
    return node_mod.NodeServiceImpl(SimpleNamespace(node_id="n1"))


def _patch(monkeypatch, chunks=(), error=None, present=True):
    """stub 流打开与落盘复核的底层（iter_pull_chunks / image_present_sync 消费的
    符号），RPC 层契约不动。error 模拟打开即失败。"""
    if error is not None:

        def _open(image):
            raise error

        monkeypatch.setattr(docker_mod, "open_pull_stream", _open)
    else:
        monkeypatch.setattr(docker_mod, "open_pull_stream", lambda image: iter(list(chunks)))
    monkeypatch.setattr(docker_mod, "fetch_image", _fake_get(present))


def _fake_get(present: bool):
    def _get(image):
        if not present:
            raise ImageNotFound(f"image not found: {image}")
        return object()

    return _get


async def _drain(agen):
    return [line async for line in agen]


class TestDockerPullRpc:
    async def test_normal_stream_passes_through(self, monkeypatch):
        """正常流：进度行原样文本化透传，镜像落盘后正常收尾。"""
        _patch(
            monkeypatch,
            chunks=[
                {"status": "Pulling from vllm/vllm-openai", "id": None},
                {"id": "abc", "status": "Downloading", "progress": "[>                  ]  1MB/8.4GB"},
                {"id": "abc", "status": "Pull complete"},
            ],
        )
        lines = await _drain(_svc().docker_pull("vllm/vllm-openai:v0.8.5"))
        assert any("Pull complete" in ln for ln in lines)
        assert not any("pull failed" in ln for ln in lines)

    async def test_open_failure_raises(self, monkeypatch):
        """流打开失败（401/404）→ 异常（消费端转 error 事件）。"""
        _patch(monkeypatch, error=APIError("unauthorized: authentication required"))
        with pytest.raises(RuntimeError, match="镜像拉取失败"):
            await _drain(_svc().docker_pull("private/x:1"))

    async def test_in_stream_error_chunk_raises(self, monkeypatch):
        """daemon 中途失败（error chunk 随流下发、流随即正常结束）→ 异常收尾，
        不得把失败行混在 data 帧里当普通输出。"""
        _patch(
            monkeypatch,
            chunks=[
                {"id": "abc", "progressDetail": {"current": 100, "total": 100}},
                {
                    "errorDetail": {"message": "failed to register layer: no space left on device"},
                    "error": "failed to register layer: no space left on device",
                },
            ],
        )
        with pytest.raises(RuntimeError, match="no space left on device"):
            await _drain(_svc().docker_pull("x:1"))

    async def test_stream_end_but_image_missing_raises(self, monkeypatch):
        """流静默截断（无 error chunk、流正常结束但镜像未落盘）→ 异常收尾，
        不得发 {"event":"end"} 让前端显示成功。"""
        _patch(monkeypatch, chunks=[{"id": "abc", "progressDetail": {"current": 100, "total": 100}}], present=False)
        with pytest.raises(RuntimeError, match="未落盘"):
            await _drain(_svc().docker_pull("x:1"))

    async def test_task_execution_error_translated(self, monkeypatch):
        """iter_pull_chunks 抛的 TaskExecutionError 统一转 RuntimeError（RPC 边界
        的失败通道），文案原样带出。"""

        async def _boom(image, cancel_event=None):
            raise TaskExecutionError("docker pull failed (image=x:1): boom")
            yield  # pragma: no cover - 使之成为异步生成器

        monkeypatch.setattr(node_mod, "iter_pull_chunks", _boom)
        with pytest.raises(RuntimeError, match="boom"):
            await _drain(_svc().docker_pull("x:1"))


class TestImageExistsRpc:
    async def test_present_true(self, monkeypatch):
        monkeypatch.setattr(node_mod, "image_present_sync", lambda image: True)
        assert await _svc().image_exists("x:1") is True

    async def test_missing_false(self, monkeypatch):
        monkeypatch.setattr(node_mod, "image_present_sync", lambda image: False)
        assert await _svc().image_exists("x:1") is False

    async def test_daemon_error_raises(self, monkeypatch):
        """daemon 错误必须上抛而非塌缩成 False——manager 复核靠异常区分「确不在位」
        与「探测通道不可用」，塌缩会把在位镜像误判缺失触发无谓重拉。"""

        def _daemon_down(image):
            raise APIError("daemon is not running")

        monkeypatch.setattr(node_mod, "image_present_sync", _daemon_down)
        with pytest.raises(APIError):
            await _svc().image_exists("x:1")
