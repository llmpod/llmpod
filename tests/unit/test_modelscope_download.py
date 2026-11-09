"""_modelscope.download_modelscope 单测（httpx.MockTransport 全桩，零网络）。

覆盖：文件清单 → 固定 commit → 下载前 manifest → 逐文件 Range 续传 / 取消 /
进度冻结键 / 噪音过滤 / 公开模型错误。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from llmpod.apps.node.executors._modelscope import ModelScopeDownloadError, download_modelscope
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME

REPO = "Qwen/Qwen2.5-0.5B-Instruct"
_ALLOWED_EXTRA = {"bytes_done", "bytes_total", "bytes_per_sec"}


def _recorder():
    events: list[dict] = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        events.append({"status": status, "progress": progress, "stage": stage, "extra": extra or {}})

    return events, report


def _file_response(content: bytes, request: httpx.Request) -> httpx.Response:
    rng = request.headers.get("range")
    if rng:
        start = int(rng.split("=", 1)[1].split("-", 1)[0])
        if start >= len(content):
            return httpx.Response(416)
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(content) - 1}/{len(content)}"},
            content=content[start:],
        )
    return httpx.Response(200, content=content)


def _make_handler(
    files: dict[str, bytes],
    *,
    short_id: str = "abc1234",
    requests: list[httpx.Request] | None = None,
    on_resolve=None,
):
    async def handler(request: httpx.Request):
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if path.endswith("/repo/files"):
            entries = [{"Path": name, "Size": len(body), "Type": "blob"} for name, body in files.items()]
            return httpx.Response(
                200,
                json={
                    "Code": 200,
                    "Success": True,
                    "Data": {"Files": entries, "LatestCommitter": {"ShortId": short_id}},
                },
            )
        if "/resolve/" in path:
            if on_resolve is not None:
                on_resolve(request)
            rel = path.split("/resolve/", 1)[1].split("/", 1)[1]
            body = files.get(rel)
            if body is None:
                return httpx.Response(404)
            return _file_response(body, request)
        return httpx.Response(404)

    return handler


async def _run(
    dest: Path,
    handler,
    *,
    source_url: str = REPO,
    revision: str | None = None,
    source_subpath: str | None = None,
    extra_files: list[str] | None = None,
    token: str | None = None,
    cancel_event: asyncio.Event | None = None,
):
    events, report = _recorder()
    await download_modelscope(
        source_url=source_url,
        revision=revision,
        source_subpath=source_subpath,
        local_dir=dest,
        report=report,
        cancel_event=cancel_event or asyncio.Event(),
        token=token,
        extra_files=extra_files,
        transport=httpx.MockTransport(handler),
    )
    return events


async def test_full_download_writes_files_and_manifest(tmp_path):
    files = {"config.json": b"{}", "model.safetensors": b"0123456789"}
    dest = tmp_path / "dest"

    events = await _run(dest, _make_handler(files))

    assert (dest / "config.json").read_bytes() == b"{}"
    assert (dest / "model.safetensors").read_bytes() == b"0123456789"
    manifest = json.loads((dest / _MANIFEST_NAME).read_text())
    assert manifest["files"] == ["config.json", "model.safetensors"]
    assert not Path(str(dest / "model.safetensors") + ".part").exists()
    assert any(e["stage"] == "downloading" for e in events)
    assert all(set(e["extra"]) <= _ALLOWED_EXTRA for e in events)


async def test_manifest_written_before_first_file_request(tmp_path):
    files = {"model.safetensors": b"0123456789"}
    dest = tmp_path / "dest"
    seen: list[bool] = []

    def on_resolve(request: httpx.Request):
        seen.append((dest / _MANIFEST_NAME).is_file())

    await _run(dest, _make_handler(files, on_resolve=on_resolve))

    assert seen and all(seen)


async def test_short_commit_is_used_for_resolve(tmp_path):
    files = {"config.json": b"{}"}
    dest = tmp_path / "dest"
    urls: list[str] = []

    def on_resolve(request: httpx.Request):
        urls.append(str(request.url))

    await _run(dest, _make_handler(files, short_id="abc1234", on_resolve=on_resolve))

    assert urls and all("/resolve/abc1234/config.json" in url for url in urls)


async def test_explicit_revision_wins_over_short_commit(tmp_path):
    files = {"config.json": b"{}"}
    dest = tmp_path / "dest"
    urls: list[str] = []

    await _run(
        dest,
        _make_handler(files, short_id="abc1234", on_resolve=lambda r: urls.append(str(r.url))),
        revision="v1",
    )

    assert urls and all("/resolve/v1/config.json" in url for url in urls)


async def test_resume_from_partial_part(tmp_path):
    files = {"model.safetensors": b"0123456789"}
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "model.safetensors") + ".part").write_bytes(b"012")

    seen_range: list[str | None] = []

    def on_resolve(request: httpx.Request):
        seen_range.append(request.headers.get("range"))

    await _run(dest, _make_handler(files, on_resolve=on_resolve))

    assert (dest / "model.safetensors").read_bytes() == b"0123456789"
    assert seen_range == ["bytes=3-"]


async def test_full_part_finalizes_without_request(tmp_path):
    files = {"model.safetensors": b"0123456789"}
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "model.safetensors") + ".part").write_bytes(b"0123456789")
    requested: list[str] = []

    await _run(dest, _make_handler(files, on_resolve=lambda r: requested.append(str(r.url))))

    assert (dest / "model.safetensors").read_bytes() == b"0123456789"
    assert not requested


async def test_416_discards_part_and_rewrites(tmp_path):
    files = {"model.safetensors": b"0123456789"}
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "model.safetensors") + ".part").write_bytes(b"0123456789ABCDEF")

    await _run(dest, _make_handler(files))

    assert (dest / "model.safetensors").read_bytes() == b"0123456789"


async def test_content_range_mismatch_rejected(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    Path(str(dest / "model.safetensors") + ".part").write_bytes(b"012")

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/repo/files"):
            entries = [{"Path": "model.safetensors", "Size": 10, "Type": "blob"}]
            return httpx.Response(
                200,
                json={"Code": 200, "Success": True, "Data": {"Files": entries, "LatestCommitter": {"ShortId": "abc"}}},
            )
        return httpx.Response(206, headers={"Content-Range": "bytes 0-9/10"}, content=b"0123456789")

    with pytest.raises(ModelScopeDownloadError, match="Content-Range 起点不符"):
        await _run(dest, handler)


async def test_cancel_mid_file(tmp_path):
    dest = tmp_path / "dest"
    cancel = asyncio.Event()

    async def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/repo/files"):
            entries = [{"Path": "model.safetensors", "Size": 10, "Type": "blob"}]
            return httpx.Response(
                200,
                json={"Code": 200, "Success": True, "Data": {"Files": entries, "LatestCommitter": {"ShortId": "abc"}}},
            )

        async def gen():
            yield b"01234"
            cancel.set()
            yield b"56789"

        return httpx.Response(200, content=gen())

    with pytest.raises(DownloadCancelled):
        await _run(dest, handler, cancel_event=cancel)


async def test_junk_and_internal_files_skipped(tmp_path):
    files = {
        ".gitattributes": b"git",
        _MANIFEST_NAME: b"noise",
        "model.safetensors.part": b"noise",
        "config.json": b"{}",
        "model.safetensors": b"0123456789",
    }
    dest = tmp_path / "dest"
    requested: list[str] = []

    await _run(dest, _make_handler(files, on_resolve=lambda r: requested.append(r.url.path)))

    assert any(path.endswith("/config.json") for path in requested)
    assert not any(".gitattributes" in path for path in requested)
    assert not any(_MANIFEST_NAME in path for path in requested)
    assert not any("model.safetensors.part" in path for path in requested)
    assert json.loads((dest / _MANIFEST_NAME).read_text())["files"] == ["config.json", "model.safetensors"]


async def test_size_missing_raises(tmp_path):
    dest = tmp_path / "dest"

    async def handler(request: httpx.Request):
        if request.url.path.endswith("/repo/files"):
            entries = [{"Path": "model.safetensors", "Size": None, "Type": "blob"}]
            return httpx.Response(
                200,
                json={"Code": 200, "Success": True, "Data": {"Files": entries, "LatestCommitter": {"ShortId": "abc"}}},
            )
        return httpx.Response(404)

    with pytest.raises(ModelScopeDownloadError, match="文件大小缺失"):
        await _run(dest, handler)


async def test_404_raises_actionable_hint(tmp_path):
    dest = tmp_path / "dest"

    async def handler(request: httpx.Request):
        return httpx.Response(404, json={"Code": 10010205001, "Success": False, "Message": "not found"})

    with pytest.raises(ModelScopeDownloadError, match="私有仓库 token"):
        await _run(dest, handler)


async def test_business_code_failure_raises(tmp_path):
    dest = tmp_path / "dest"

    async def handler(request: httpx.Request):
        return httpx.Response(200, json={"Code": 1001, "Success": False, "Message": "bad"})

    with pytest.raises(ModelScopeDownloadError, match="1001"):
        await _run(dest, handler)


async def test_zero_byte_file(tmp_path):
    files = {"empty.txt": b""}
    dest = tmp_path / "dest"

    await _run(dest, _make_handler(files))

    assert (dest / "empty.txt").exists()
    assert (dest / "empty.txt").stat().st_size == 0


async def test_no_authorization_header(tmp_path):
    files = {"config.json": b"{}"}
    dest = tmp_path / "dest"
    auth: list[str | None] = []

    def on_resolve(request: httpx.Request):
        auth.append(request.headers.get("authorization"))

    await _run(dest, _make_handler(files, on_resolve=on_resolve))

    assert auth and all(value is None for value in auth)


async def test_token_sends_bearer_and_cookie(tmp_path):
    files = {"config.json": b"{}"}
    dest = tmp_path / "dest"
    requests: list[httpx.Request] = []

    await _run(dest, _make_handler(files, requests=requests), token="SECRET")

    assert requests
    assert all(request.headers.get("authorization") == "Bearer SECRET" for request in requests)
    assert all(request.headers.get("cookie") == "m_session_id=SECRET" for request in requests)


async def test_url_subpath_and_extra_files(tmp_path):
    files = {
        "config.json": b"{}",
        "Q4_K_M/model.gguf": b"q4",
        "Q8_0/model.gguf": b"q8",
        "mmproj-F16.gguf": b"mmproj",
    }
    dest = tmp_path / "dest"
    requested: list[str] = []

    await _run(
        dest,
        _make_handler(files, on_resolve=lambda r: requested.append(r.url.path)),
        source_url=f"https://www.modelscope.cn/models/{REPO}/resolve/v1/Q4_K_M",
        source_subpath="ignored",
        extra_files=["mmproj-F16.gguf"],
    )

    assert any(path.endswith("/Q4_K_M/model.gguf") for path in requested)
    assert any(path.endswith("/mmproj-F16.gguf") for path in requested)
    assert not any("Q8_0" in path for path in requested)
    assert not any(path.endswith("/config.json") for path in requested)
    assert all("/resolve/v1/" in path for path in requested)
    manifest = json.loads((dest / _MANIFEST_NAME).read_text())["files"]
    assert manifest == ["Q4_K_M/model.gguf", "mmproj-F16.gguf"]


async def test_payload_subpath_used_when_url_is_bare(tmp_path):
    files = {"Q4_K_M/model.gguf": b"q4", "Q8_0/model.gguf": b"q8", "mmproj.gguf": b"m"}
    dest = tmp_path / "dest"
    requested: list[str] = []

    await _run(
        dest,
        _make_handler(files, on_resolve=lambda r: requested.append(r.url.path)),
        source_url=REPO,
        source_subpath="Q4_K_M",
        extra_files=["mmproj.gguf"],
    )

    assert any(path.endswith("/Q4_K_M/model.gguf") for path in requested)
    assert any(path.endswith("/mmproj.gguf") for path in requested)
    assert not any("Q8_0" in path for path in requested)
    assert all("/resolve/abc1234/" in path for path in requested)


async def test_url_revision_overrides_payload(tmp_path):
    files = {"config.json": b"{}"}
    dest = tmp_path / "dest"
    requested: list[str] = []

    await _run(
        dest,
        _make_handler(files, on_resolve=lambda r: requested.append(r.url.path)),
        source_url=f"https://www.modelscope.cn/models/{REPO}/resolve/v1/config.json",
        revision="form-rev",
    )

    assert requested and all("/resolve/v1/" in path for path in requested)
