"""_hf.download_hf 单测：repo_id / allow_patterns 纯函数 + 逐文件下载循环（stubbed HfApi）。

无网络：monkeypatch ``huggingface_hub.HfApi`` 为可控 fake（``repo_info(files_metadata=True)``
返回带 size 的 siblings），覆盖 cancel / 粗粒度 bytes 进度 / 空清单报错 / allow_patterns
收窄 / hf_token 仅入 HfApi 不入 report（安全）。

进度 extra 契约：只含 ``stage``("downloading") / ``bytes_done`` / ``bytes_total``。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from llmpod.apps.node.executors import _hf
from llmpod.apps.node.executors.ensure_model import DownloadCancelled, _MANIFEST_NAME

# frozen 上报 schema：download_hf 的 extra 只应含这两键（绝不 files_done/file/downloaded
# 等），stage 是一等字段且恒为 downloading
_ALLOWED_EXTRA = {"bytes_done", "bytes_total", "bytes_per_sec"}


def _recorder():
    calls = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        calls.append(
            {
                "status": status,
                "progress": progress,
                "stage": stage,
                "message": message,
                "extra": extra or {},
            }
        )

    return calls, report


class _FakeSibling:
    def __init__(self, rfilename, size):
        self.rfilename = rfilename
        self.size = size


class _FakeRepoInfo:
    def __init__(self, siblings):
        self.siblings = siblings


def _make_fake_hfapi(file_specs, downloads, sink=None):
    """构造可注入 ``huggingface_hub.HfApi`` 的 fake 类。

    ``file_specs`` 是 ``(name, size_bytes)`` 序列；``repo_info(files_metadata=True)`` 返回带 size
    的 siblings（download_hf 自行按 allow_patterns 收窄）；``hf_hub_download`` 落空文件并记录。
    """

    class FakeHfApi:
        def __init__(self, *args, **kwargs):
            self.init = kwargs
            if sink is not None:
                sink["init"] = kwargs

        def repo_info(self, repo_id, revision=None, files_metadata=False):
            if sink is not None:
                sink["repo_info_args"] = (repo_id, revision, files_metadata)
            return _FakeRepoInfo([_FakeSibling(n, sz) for n, sz in file_specs])

        def hf_hub_download(self, *, repo_id, filename, revision, local_dir, tqdm_class=None):
            from pathlib import Path

            p = Path(local_dir) / filename
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("data")
            downloads.append(filename)
            return str(p)

    return FakeHfApi


# ---- 纯函数 ----


class TestRepoId:
    def test_bare_repo_id(self):
        assert _hf._repo_id("Qwen/Qwen2.5-0.5B") == "Qwen/Qwen2.5-0.5B"

    def test_url_extracts_org_name(self):
        assert _hf._repo_id("https://huggingface.co/Qwen/Qwen2.5-0.5B") == "Qwen/Qwen2.5-0.5B"

    def test_url_with_tree_ignores_rev_subpath(self):
        assert _hf._repo_id("https://hf-mirror.com/unsloth/X-GGUF/tree/main/Q4_K_M") == "unsloth/X-GGUF"

    def test_url_resolve_file(self):
        assert _hf._repo_id("https://hf-mirror.com/org/m/resolve/main/Q4/x.gguf") == "org/m"

    def test_query_stripped(self):
        assert _hf._repo_id("org/m?download=true") == "org/m"


class TestEndpointFromUrl:
    """用户贴镜像 URL → node 下载用其 host（与 manager inspect 一致）；bare id → settings 兜底。"""

    def test_bare_id_uses_fallback(self):
        assert _hf._endpoint_from_url("Qwen/Qwen2.5-0.5B", "https://hf-mirror.com") == "https://hf-mirror.com"

    def test_mirror_url_host_honored(self):
        assert (
            _hf._endpoint_from_url("https://hf-mirror.com/Qwen/Qwen2.5-0.5B", "https://huggingface.co")
            == "https://hf-mirror.com"
        )

    def test_official_url_host_honored(self):
        assert (
            _hf._endpoint_from_url("https://huggingface.co/Qwen/Qwen2.5-0.5B", "https://hf-mirror.com")
            == "https://huggingface.co"
        )

    def test_url_with_tree_uses_host(self):
        assert (
            _hf._endpoint_from_url("https://hf-mirror.com/unsloth/X-GGUF/tree/main/Q4_K_M", "https://huggingface.co")
            == "https://hf-mirror.com"
        )

    def test_empty_uses_fallback(self):
        assert _hf._endpoint_from_url("", "https://x.com") == "https://x.com"


class TestAllowPatterns:
    def test_empty_is_none(self):
        assert _hf._allow_patterns(None) is None
        assert _hf._allow_patterns("") is None
        assert _hf._allow_patterns("   ") is None

    def test_subpath(self):
        assert _hf._allow_patterns("Q4_K_M") == ["Q4_K_M", "Q4_K_M/**"]

    def test_subpath_stripped(self):
        assert _hf._allow_patterns("/Q4_K_M/") == ["Q4_K_M", "Q4_K_M/**"]


class TestMatches:
    def test_none_passes_all(self):
        assert _hf._matches("anything", None) is True

    def test_subpath_dir_match(self):
        pats = _hf._allow_patterns("Q4_K_M")
        assert _hf._matches("Q4_K_M/model.gguf", pats) is True
        assert _hf._matches("Q8_0/model.gguf", pats) is False

    def test_exact_file_match(self):
        pats = _hf._allow_patterns("config.json")
        assert _hf._matches("config.json", pats) is True
        assert _hf._matches("model.safetensors", pats) is False


# ---- download_hf 循环 ----


class TestDownloadHf:
    async def test_happy_path_downloads_all_with_byte_progress(self, tmp_path, monkeypatch):
        file_specs = [("config.json", 10), ("model.safetensors", 1000), ("tokenizer.json", 20)]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="Qwen/Qwen2.5-0.5B",
            revision="main",
            source_subpath=None,
            token="tok",
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert downloads == ["config.json", "model.safetensors", "tokenizer.json"]  # 全部下载
        # 进度序列：init(0) + 逐文件**下完后**的边界报点（字节刻度——按文件数刻度会在
        # 大文件下完后跳回，进度条倒退；边界中间值封顶 99，终值 100 由末尾报）。
        # 本例 model 占 1000/1030 → 边界 98；tokenizer 收尾 1030/1030 → 99 → 100
        assert [c["progress"] for c in calls] == [0, 0, 98, 99, 100]
        # bytes_done 序列：累计已下载（init，+config=10，+model=1010，+tokenizer=1030，终值同）
        assert [c["extra"]["bytes_done"] for c in calls] == [0, 10, 1010, 1030, 1030]
        assert all(c["extra"]["bytes_total"] == 1030 for c in calls)  # bytes_total 恒定
        # frozen 契约：extra 只含两键，stage 一等字段恒 downloading（无 verifying——那是 executor 的事）
        for c in calls:
            assert set(c["extra"].keys()) <= _ALLOWED_EXTRA
            assert c["stage"] == "downloading"

    async def test_intra_file_progress_reported(self, tmp_path, monkeypatch):
        """单文件内部进度：tqdm 钩子写的字节被轮询协程转述上报（不再只报文件边界）。

        17GB 的 GGUF 就是一个文件——只报边界的话整段下载前端都停在 0%，看起来像没开始。
        """
        import time
        from pathlib import Path

        class FakeHfApi:
            def __init__(self, *args, **kwargs):
                pass

            def repo_info(self, repo_id, revision=None, files_metadata=False):
                return _FakeRepoInfo([_FakeSibling("big.gguf", 1000)])

            def hf_hub_download(self, *, repo_id, filename, revision, local_dir, tqdm_class=None):
                assert tqdm_class is not None  # 必须注入钩子，否则单文件内无进度
                bar = tqdm_class(total=1000)
                for _ in range(5):
                    bar.update(200)
                    time.sleep(0.05)
                p = Path(local_dir) / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
                return str(p)

        monkeypatch.setattr("huggingface_hub.HfApi", FakeHfApi)
        monkeypatch.setattr(_hf, "_PROGRESS_POLL_SECONDS", 0.01)
        # 关掉节流（生产默认最快 2s 一条；测试要看到每个中间点）
        from functools import partial

        from llmpod.apps.node.executors.base import ByteProgressReporter

        monkeypatch.setattr(_hf, "ByteProgressReporter", partial(ByteProgressReporter, min_interval=0.0))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="org/m",
            revision="main",
            source_subpath="big.gguf",
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        mids = [c["extra"]["bytes_done"] for c in calls if 0 < c["extra"]["bytes_done"] < 1000]
        assert mids, f"单文件内部进度缺失: {calls}"
        assert mids == sorted(mids)  # 单调递增
        # 双计回归守卫：轮询报的累计绝不能超过文件大小/总量（base+cur 重复叠加会呈 Σ 膨胀）
        assert all(c["extra"]["bytes_done"] <= 1000 for c in calls), f"过计: {calls}"

    async def test_multi_file_polling_never_double_counts(self, tmp_path, monkeypatch):
        """多文件轮询防双计：文件基座由调用方持有（base+cur），任何报点的累计
        bytes_done 都不得超过 bytes_total——reporter.last_bytes_done 复合基座的
        双计 bug 会把 10s 下载在第 4s 顶到 99% 钳位。"""
        import time
        from pathlib import Path

        class FakeHfApi:
            def __init__(self, *args, **kwargs):
                pass

            def repo_info(self, repo_id, revision=None, files_metadata=False):
                return _FakeRepoInfo([_FakeSibling("a.bin", 1000), _FakeSibling("b.bin", 1000)])

            def hf_hub_download(self, *, repo_id, filename, revision, local_dir, tqdm_class=None):
                bar = tqdm_class(total=1000)
                for _ in range(5):
                    bar.update(200)
                    time.sleep(0.02)
                p = Path(local_dir) / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
                return str(p)

        monkeypatch.setattr("huggingface_hub.HfApi", FakeHfApi)
        monkeypatch.setattr(_hf, "_PROGRESS_POLL_SECONDS", 0.01)
        from functools import partial

        from llmpod.apps.node.executors.base import ByteProgressReporter

        monkeypatch.setattr(_hf, "ByteProgressReporter", partial(ByteProgressReporter, min_interval=0.0))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="org/m",
            revision="main",
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        for c in calls:
            done, total = c["extra"]["bytes_done"], c["extra"]["bytes_total"]
            assert done <= total, f"双计/过计: done={done} total={total}: {calls}"
            assert c["progress"] <= 99 or c is calls[-1]  # 中间报点不触 100/99 钳位过早

    async def test_cancel_mid_file_aborts_download(self, tmp_path, monkeypatch):
        """单文件中途取消：tqdm 钩子每 chunk 检查 cancel → 抛 DownloadCancelled 立即中断。

        一个 17GB 权重就是一个文件，只按文件边界取消等于「下载中途停不下来」。
        """
        import time

        cancel = asyncio.Event()

        class FakeHfApi:
            def __init__(self, *args, **kwargs):
                pass

            def repo_info(self, repo_id, revision=None, files_metadata=False):
                return _FakeRepoInfo([_FakeSibling("big.gguf", 1000)])

            def hf_hub_download(self, *, repo_id, filename, revision, local_dir, tqdm_class=None):
                bar = tqdm_class(total=1000)
                for i in range(10):
                    bar.update(100)
                    if i == 1:
                        cancel.set()  # 下载中途用户取消
                    time.sleep(0.02)
                raise AssertionError("cancel 未中断下载")  # 循环跑完即失败

        monkeypatch.setattr("huggingface_hub.HfApi", FakeHfApi)
        calls, report = _recorder()
        with pytest.raises(DownloadCancelled):
            await _hf.download_hf(
                source_url="org/m",
                revision="main",
                source_subpath="big.gguf",
                token=None,
                local_dir=tmp_path / "m",
                report=report,
                cancel_event=cancel,
            )
        assert not (tmp_path / "m" / "big.gguf").exists()  # 未落定半成品

    async def test_cancel_between_files_raises(self, tmp_path, monkeypatch):
        file_specs = [("a.bin", 100), ("b.bin", 100), ("c.bin", 100)]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        cancel = asyncio.Event()

        async def report(status, progress=0, *, stage=None, message=None, extra=None):
            # 第二个文件下完后（bytes_done>=200）触发 cancel → 下轮 check 命中
            if extra and extra.get("bytes_done", 0) >= 200:
                cancel.set()

        with pytest.raises(DownloadCancelled):
            await _hf.download_hf(
                source_url="org/m",
                revision=None,
                source_subpath=None,
                token=None,
                local_dir=tmp_path / "m",
                report=report,
                cancel_event=cancel,
            )
        assert downloads == ["a.bin", "b.bin"]  # c.bin 因 cancel 未下载

    async def test_empty_file_list_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi([], []))
        calls, report = _recorder()
        with pytest.raises(RuntimeError, match="无匹配文件"):
            await _hf.download_hf(
                source_url="org/m",
                revision="main",
                source_subpath="nonexistent_variant",
                token=None,
                local_dir=tmp_path / "m",
                report=report,
                cancel_event=asyncio.Event(),
            )

    async def test_allow_patterns_narrows_to_subpath(self, tmp_path, monkeypatch):
        file_specs = [
            ("config.json", 10),
            ("Q4_K_M/model.gguf", 5000),
            ("Q8_0/model.gguf", 8000),
            ("Q4_K_M/tokenizer.json", 20),
        ]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="org/m",
            revision="main",
            source_subpath="Q4_K_M",
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert downloads == ["Q4_K_M/model.gguf", "Q4_K_M/tokenizer.json"]  # 只下 Q4_K_M 下

    async def test_token_only_in_hfapi_never_in_report(self, tmp_path, monkeypatch):
        """安全：hf_token 仅入 HfApi(token=...)，绝不入 report 的 message/extra。"""
        file_specs = [("config.json", 10)]
        sink: dict = {}
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, [], sink=sink))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="org/m",
            revision="main",
            source_subpath=None,
            token="hf_SUPER_SECRET_TOKEN",
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert sink["init"]["token"] == "hf_SUPER_SECRET_TOKEN"  # 经参数注入 HfApi
        assert "hf_SUPER_SECRET_TOKEN" not in str(calls)  # 不入任何上报内容

    async def test_writes_manifest_after_full_download(self, tmp_path, monkeypatch):
        """下载完成 → 落盘 .llmpod-manifest.json（含 layers-N 分片 + processor 文件，verify 的判定依据）。"""
        file_specs = [
            ("config.json", 10),
            ("layers-0.safetensors", 1000),
            ("layers-1.safetensors", 1000),
            ("preprocessor_config.json", 20),
        ]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        calls, report = _recorder()
        local_dir = tmp_path / "m"

        await _hf.download_hf(
            source_url="Qwen/Qwen3.5-VL",
            revision="main",
            source_subpath=None,
            token=None,
            local_dir=local_dir,
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert downloads == [f for f, _ in file_specs]
        mf = local_dir / _MANIFEST_NAME
        assert mf.is_file()
        assert json.loads(mf.read_text())["files"] == [f for f, _ in file_specs]

    async def test_cancel_does_not_write_manifest(self, tmp_path, monkeypatch):
        """中断下载不落清单——verify 据此判未完成，防半下载（layers-N 无 -of-N 推断不出总数）误报 present。"""
        file_specs = [("a.bin", 100), ("b.bin", 100), ("c.bin", 100)]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        cancel = asyncio.Event()

        async def report(status, progress=0, *, stage=None, message=None, extra=None):
            if extra and extra.get("bytes_done", 0) >= 1:
                cancel.set()

        with pytest.raises(DownloadCancelled):
            await _hf.download_hf(
                source_url="org/m",
                revision=None,
                source_subpath=None,
                token=None,
                local_dir=tmp_path / "m",
                report=report,
                cancel_event=cancel,
            )
        assert not (tmp_path / "m" / _MANIFEST_NAME).exists()

    async def test_revision_defaults_main_and_files_metadata(self, tmp_path, monkeypatch):
        from llmpod.config.settings import settings

        file_specs = [("config.json", 10)]
        sink: dict = {}
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, [], sink=sink))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="Qwen/Qwen2.5-0.5B",
            revision=None,
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        # revision 缺省 main；files_metadata=True（拿 size 供 bytes_total）
        assert sink["repo_info_args"] == ("Qwen/Qwen2.5-0.5B", "main", True)
        assert sink["init"]["endpoint"] == settings.model_source_hf_endpoint  # endpoint 来自 settings

    async def test_repo_id_extracted_from_url(self, tmp_path, monkeypatch):
        file_specs = [("config.json", 10)]
        sink: dict = {}
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, [], sink=sink))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="https://hf-mirror.com/Qwen/Qwen2.5-0.5B/tree/main",
            revision=None,
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert sink["repo_info_args"][0] == "Qwen/Qwen2.5-0.5B"  # URL → repo_id

    async def test_mirror_url_endpoint_honored(self, tmp_path, monkeypatch):
        """用户贴镜像 URL → HfApi 用该镜像 endpoint（与 manager inspect 一致），不必配 node env。"""
        file_specs = [("config.json", 10)]
        sink: dict = {}
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, [], sink=sink))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="https://hf-mirror.com/Qwen/Qwen2.5-0.5B",
            revision=None,
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert sink["init"]["endpoint"] == "https://hf-mirror.com"  # URL host → endpoint
        assert sink["repo_info_args"][0] == "Qwen/Qwen2.5-0.5B"

    async def test_bare_id_uses_settings_endpoint(self, tmp_path, monkeypatch):
        """bare repo id（无 scheme://host）→ endpoint 用 settings 兜底。"""
        from llmpod.config.settings import settings

        file_specs = [("config.json", 10)]
        sink: dict = {}
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, [], sink=sink))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="Qwen/Qwen2.5-0.5B",
            revision=None,
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert sink["init"]["endpoint"] == settings.model_source_hf_endpoint

    async def test_unknown_sizes_yield_null_bytes_total(self, tmp_path, monkeypatch):
        """size 全部未知（None）→ bytes_total=None（消费方写 NULL），不报 0 误判满进度。"""
        file_specs = [("config.json", None), ("model.bin", None)]
        downloads = []
        monkeypatch.setattr("huggingface_hub.HfApi", _make_fake_hfapi(file_specs, downloads))
        calls, report = _recorder()

        await _hf.download_hf(
            source_url="org/m",
            revision="main",
            source_subpath=None,
            token=None,
            local_dir=tmp_path / "m",
            report=report,
            cancel_event=asyncio.Event(),
        )
        assert all(c["extra"]["bytes_total"] is None for c in calls)
        assert calls[-1]["extra"]["bytes_done"] == 0  # 未知 size 不累加
