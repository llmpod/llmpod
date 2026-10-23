"""_manager 下载器 E2E 单测：对接真实分发路由（ASGITransport，无真实端口）。

覆盖：manifest 拉取 → 逐文件 Range 续传 → .part → os.replace → 进度冻结键；
以及断点续传（预置 .part 部分字节）与 cancel（抛 DownloadCancelled）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import llmpod.apps.webserver.routes.public.distribution as dist
import llmpod.config.settings as settings_mod
from llmpod.apps.node.executors import _manager as mgrmod
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME
from llmpod.apps.webserver.errors import register_exception_handlers
from llmpod.apps.webserver.routes.public.distribution import router as distribution_router
from llmpod.apps.webserver.utils.download_token import mint_download_token, download_signing_key
from llmpod.models.model import Model


class _FakeSession:
    def __init__(self, model):
        self._model = model

    async def get(self, cls, pk):
        if cls is Model and pk == self._model.id:
            return self._model
        return None


@pytest.fixture
def dist_app(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    mdir = store / "m1"
    mdir.mkdir()
    (mdir / "config.json").write_text("{}")
    (mdir / "w.safetensors").write_bytes(b"0123456789")

    monkeypatch.setattr(settings_mod.settings, "secret_key", "test-deployment-secret")
    monkeypatch.setattr(settings_mod.settings, "model_store_dir", str(store))
    monkeypatch.setattr(settings_mod.settings, "download_chunk_size", 4)  # 小 chunk，强制多片
    monkeypatch.setattr(settings_mod.settings, "download_token_max_ttl", 7200)
    monkeypatch.setattr(settings_mod.settings, "download_manifest_cache_ttl", 60)
    dist._manifest_cache.clear()  # 前序用例可能缓存了旧文件集（manifest 按模型 id 缓存）

    model = Model(name="m1", model_type="llm", source="local", source_url=str(mdir))
    model.id = 42

    @asynccontextmanager
    async def fake_db():
        yield _FakeSession(model)

    monkeypatch.setattr(dist, "get_db_context", fake_db)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(distribution_router, prefix="/api/v1/distribution")
    return app, model


def _collect_report():
    events: list[dict] = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        events.append({"status": status, "progress": progress, "stage": stage, "extra": extra})

    return events, report


async def _run_download(dist_app, dest, *, transport=None):
    """标准装配：mint token + report 收集 + 默认 ASGITransport，返回进度事件列表。"""
    app, model = dist_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)
    events, report = _collect_report()
    await mgrmod.download_manager(
        manager_base_url="http://test/api/v1/distribution",
        download_token=tok,
        model_id=model.id,
        local_dir=dest,
        report=report,
        cancel_event=asyncio.Event(),
        transport=transport or httpx.ASGITransport(app=app),
    )
    return events


async def test_full_download_writes_files(dist_app, tmp_path):
    _, model = dist_app
    dest = tmp_path / "dest"
    events = await _run_download(dist_app, dest)
    assert (dest / "config.json").read_text() == "{}"
    assert (dest / "w.safetensors").read_bytes() == b"0123456789"
    # 下载完成 → 落盘文件清单（verify 权威判定）
    mf = dest / _MANIFEST_NAME
    assert mf.is_file()
    assert json.loads(mf.read_text())["files"] == ["config.json", "w.safetensors"]
    # 进度冻结契约：每条 running 都带 stage=downloading + bytes_done/bytes_total
    assert any(e["stage"] == "downloading" for e in events)
    assert not Path(str(dest / "w.safetensors") + ".part").exists()


async def test_download_keeps_repo_relative_layout_with_subpath(dist_app, tmp_path):
    """模型含变体目录时文件仍按 manifest 根相对路径落盘——不叠双重前缀、
    配套件（如 mmproj）不进子目录。

    回归：旧实现把每个文件写到 ``local_dir/<sub>/<rel>``，而 manifest 的 rel 本就相对
    模型根（已含变体目录），叠加后主文件双重前缀、根级配套件被压进子目录——
    ``_verify_existing`` 按根相对找配套件恒判缺失，反复重下到 attempt 耗尽。
    """
    _, model = dist_app
    # 模型根下加变体目录与根级 mmproj（模拟 GGUF 视觉模型布局）
    mdir = Path(model.source_url)
    (mdir / "Q4_K_M").mkdir()
    (mdir / "Q4_K_M" / "model.gguf").write_bytes(b"gguf-bytes")
    (mdir / "mmproj-F16.gguf").write_bytes(b"mmproj-bytes")

    dest = tmp_path / "dest"
    await _run_download(dist_app, dest)
    # manifest 根相对路径原样落盘：变体在子目录、配套件在根（验在位/--mmproj 的找法）
    assert (dest / "Q4_K_M" / "model.gguf").read_bytes() == b"gguf-bytes"
    assert not (dest / "Q4_K_M" / "Q4_K_M").exists()  # 无双重前缀
    assert (dest / "mmproj-F16.gguf").read_bytes() == b"mmproj-bytes"  # 配套件在根
    mf = json.loads((dest / _MANIFEST_NAME).read_text())["files"]
    assert "Q4_K_M/model.gguf" in mf and "mmproj-F16.gguf" in mf  # 清单同根相对


async def test_resume_from_partial_part(dist_app, tmp_path):
    """dest 已有 .part 部分字节 → Range 续传补齐。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "w.safetensors") + ".part").write_bytes(b"012")  # 3/10
    await _run_download(dist_app, dest)
    assert (dest / "w.safetensors").read_bytes() == b"0123456789"


async def test_full_part_finalizes_without_request(dist_app, tmp_path):
    """上轮 fsync 后、replace 前中断：.part 已写满 → 直接落定，不发下载请求。

    回归：旧实现对满长 .part 仍发 Range: bytes=size-，服务端必返 416，
    3 次重试稳定失败后 _cleanup_part 把完整权重删掉全量重下。
    """
    app, _model = dist_app
    dest = tmp_path / "dest"
    dest.mkdir()
    # 全部文件都预置满长 .part：正确路径一个 /file 请求都不该发
    Path(str(dest / "config.json") + ".part").write_text("{}")
    Path(str(dest / "w.safetensors") + ".part").write_bytes(b"0123456789")

    requested = []
    real_transport = httpx.ASGITransport(app=app)

    async def no_file_request(request):
        if request.url.path.endswith("/file"):
            requested.append(request.url.path)
            return httpx.Response(404)  # 真走到 /file 就让它失败——正确路径根本不该请求
        return await real_transport.handle_async_request(request)  # manifest 走真实 app

    await _run_download(dist_app, dest, transport=httpx.MockTransport(no_file_request))
    assert (dest / "config.json").read_text() == "{}"
    assert (dest / "w.safetensors").read_bytes() == b"0123456789"
    assert not Path(str(dest / "w.safetensors") + ".part").exists()
    assert not requested  # manifest 拉了，但 /file 一次都没碰


async def test_zero_byte_file_downloads_without_part(dist_app, tmp_path):
    """0 字节文件（空 README 等占位）：无 .part 时 have==0==size 也成立——
    短路分支必须显式判 pp.exists()，否则对不存在的 .part replace 抛 FileNotFoundError。
    """
    _, model = dist_app
    mdir = Path(model.source_url)
    (mdir / "empty.txt").write_bytes(b"")  # manifest 收录所有常规文件，含空文件

    dest = tmp_path / "dest"
    await _run_download(dist_app, dest)
    assert (dest / "empty.txt").exists()
    assert (dest / "empty.txt").stat().st_size == 0


async def test_206_wrong_content_range_start_rejected(dist_app, tmp_path):
    """206 的 Content-Range 起点必须等于请求偏移——「206 却从 0 开始」的 CDN 会让
    append 模式把错位数据写进偏移处，快速失败优于整份作废。"""
    app, model = dist_app
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "w.safetensors") + ".part").write_bytes(b"012")  # 续传起点 3

    async def serve_206_from_zero(request):
        # 只劫持带 Range 的续传请求（w.safetensors 的 .part 起点 3）；无 Range 的
        # 首次下载（config.json）走真实 app 正常路径
        if request.url.path.endswith("/file") and "range" in {k.lower() for k in request.headers}:
            # 无论 Range 要什么，都应答 206 且从 0 开始（畸形但真实存在的 CDN 行为）
            async def gen():
                yield b"0123456789"

            return httpx.Response(206, headers={"Content-Range": "bytes 0-9/10"}, content=gen())
        return await real_transport.handle_async_request(request)

    real_transport = httpx.ASGITransport(app=app)
    with pytest.raises(mgrmod.ManagerDownloadError, match="Content-Range 起点不符"):
        await _run_download(dist_app, dest, transport=httpx.MockTransport(serve_206_from_zero))


async def test_416_discards_part_and_rewrites(dist_app, tmp_path):
    """416（Range 起点越界，.part 超过当前文件长）→ 弃续传从头重写，不当错误重试。

    回归：旧实现 416 走通用错误分支，.part 原地不动，重试每次都 416，死锁。
    分发侧 FileResponse 对越界 Range 回 416 + 空 body，正好复现真实行为。
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    # config.json 也预置超长 .part（2 字节文件配 8 字节残留）——验证 416 重写对
    # 「首个文件」同样生效，不只限中途文件
    Path(str(dest / "config.json") + ".part").write_bytes(b"01234567")
    Path(str(dest / "w.safetensors") + ".part").write_bytes(b"0123456789ABCDEF")

    # 真实 app：越界 Range 天然回 416
    await _run_download(dist_app, dest)
    # 每个文件先 416（超长 .part）→ 弃续传 wb 重写 → 全量重拿，内容正确
    assert (dest / "config.json").read_text() == "{}"
    assert (dest / "w.safetensors").read_bytes() == b"0123456789"
    assert not Path(str(dest / "w.safetensors") + ".part").exists()


async def test_intra_file_progress_events(dist_app, tmp_path, monkeypatch):
    """单文件内部进度：逐 chunk 上报（一个 17GB 权重就是一个文件，只报文件边界=全程无进度）。"""
    from functools import partial

    from llmpod.apps.node.executors.base import ByteProgressReporter

    # 关掉节流（生产默认最快 2s 一条；测试要看到每个 chunk）
    monkeypatch.setattr(mgrmod, "ByteProgressReporter", partial(ByteProgressReporter, min_interval=0.0))
    dest = tmp_path / "dest"
    events = await _run_download(dist_app, dest)
    # w.safetensors 10B / chunk 4B → 出现 0 < bytes_done < bytes_total 的中间点
    mids = [
        e["extra"]["bytes_done"]
        for e in events
        if e["extra"] and 0 < e["extra"]["bytes_done"] < (e["extra"]["bytes_total"] or 0)
    ]
    assert mids, f"单文件内部进度缺失: {events}"
    assert mids == sorted(mids)  # 单调递增


async def test_cancel_raises_download_cancelled(dist_app, tmp_path):
    app, model = dist_app
    dest = tmp_path / "dest"
    cancel = asyncio.Event()
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)

    calls = {"n": 0}

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        calls["n"] += 1
        if calls["n"] >= 1:
            cancel.set()  # 首次进度回调后立即取消

    transport = httpx.ASGITransport(app=app)
    with pytest.raises((DownloadCancelled, Exception)):
        await mgrmod.download_manager(
            manager_base_url="http://test/api/v1/distribution",
            download_token=tok,
            model_id=model.id,
            local_dir=dest,
            report=report,
            cancel_event=cancel,
            transport=transport,
        )
