"""EnsureModelExecutor 单测：verify 矩阵 / check-existing 短路 / 磁盘预检 / local /
默认 downloader 委托 HF / cancel 清 .part / 可注入 downloader 成功与失败。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


from llmpod.apps.node.models import LocalTask
from llmpod.apps.node.executors.ensure_model import (
    DownloadCancelled,
    EnsureModelExecutor,
    _MANIFEST_NAME,
    _verify_existing,
)


def _recorder():
    calls = []

    async def report(status, progress=0, *, stage=None, message=None, extra=None):
        calls.append(
            {
                "status": status,
                "progress": progress,
                "stage": stage,
                "message": message,
                "extra": extra,
            }
        )

    return calls, report


def _task(local_dir, source="huggingface", source_kind="transformers_folder") -> LocalTask:
    return LocalTask(
        task_id="t1",
        type="ENSURE_MODEL",
        resource_type="MODEL_PLACEMENT",
        resource_key="model:1:n1",
        node_id="n1",
        status="PENDING",
        payload={"model_id": 1, "source": source, "source_kind": source_kind, "local_dir": str(local_dir)},
    )


def _touch(p: Path, content="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ---- verify 矩阵（纯函数）----


class TestVerifyExisting:
    def test_single_gguf_one_file(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "model.gguf").write_text("")
        assert _verify_existing(d, "single_gguf") == str(d / "model.gguf")

    def test_single_gguf_two_files_none(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "a.gguf").write_text("")
        (d / "b.gguf").write_text("")
        assert _verify_existing(d, "single_gguf") is None

    def test_gguf_shards_complete(self, tmp_path):
        """分片齐全 → model_path = 首分片**文件**（-m 直吃；返回目录会让 llama-server magic 崩溃）。"""
        d = tmp_path / "m"
        d.mkdir()
        for i in (1, 2, 3):
            (d / f"model-{i:05d}-of-00003.gguf").write_text("")
        assert _verify_existing(d, "gguf_shards") == str(d / "model-00001-of-00003.gguf")

    def test_gguf_shards_unnumbered_first_lexicographic(self, tmp_path):
        """无 -of-N 序号的并列多 gguf：按字典序取首个。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "b.gguf").write_text("")
        (d / "a.gguf").write_text("")
        assert _verify_existing(d, "gguf_shards") == str(d / "a.gguf")

    def test_first_gguf_orders_by_shard_seq_not_total(self, tmp_path):
        """分片序号参与排序（非总分片数）：非分片文件（imatrix 等）不得挤到首片前。"""
        from llmpod.apps.node.executors.ensure_model import _first_gguf

        shards = [f"model-{i:05d}-of-00003.gguf" for i in (1, 2, 3)]
        # 字典序上 imatrix.gguf / a.gguf 都可能排在 model-00001 前——序号必须压过
        names = ["imatrix.gguf", *shards]
        assert _first_gguf(names) == "model-00001-of-00003.gguf"
        # 全部同总数字典序并列时，序号最小者优先
        assert _first_gguf(shards[::-1]) == "model-00001-of-00003.gguf"

    def test_gguf_shards_with_manifest_resolves_file(self, tmp_path):
        """清单权威判定也要解析主模型文件（清单分支曾返回目录）。"""
        import json

        d = tmp_path / "m"
        d.mkdir()
        for i in (1, 2):
            (d / f"model-{i:05d}-of-00002.gguf").write_text("x")
        (d / ".llmpod-manifest.json").write_text(
            json.dumps({"files": ["model-00001-of-00002.gguf", "model-00002-of-00002.gguf"]})
        )
        assert _verify_existing(d, "gguf_shards") == str(d / "model-00001-of-00002.gguf")

    def test_single_gguf_with_manifest_resolves_file(self, tmp_path):
        """主场景：清单在位 + single_gguf → 唯一 .gguf 文件而非目录。"""
        import json

        d = tmp_path / "m"
        d.mkdir()
        (d / "model.gguf").write_text("x")
        (d / ".llmpod-manifest.json").write_text(json.dumps({"files": ["model.gguf"]}))
        assert _verify_existing(d, "single_gguf") == str(d / "model.gguf")

    def test_manifest_with_mmproj_excluded_from_main(self, tmp_path):
        """清单含 mmproj 配套件时主模型解析不受干扰（多模态主/配区分）。"""
        import json

        d = tmp_path / "m"
        d.mkdir()
        (d / "model.gguf").write_text("x")
        (d / "mmproj-F16.gguf").write_text("y")
        (d / ".llmpod-manifest.json").write_text(json.dumps({"files": ["model.gguf", "mmproj-F16.gguf"]}))
        assert _verify_existing(d, "single_gguf", extra_files=["mmproj-F16.gguf"]) == str(d / "model.gguf")

    def test_gguf_shards_incomplete_none(self, tmp_path):
        """H1：3 分片 repo 只下首 1 片 → 判未完成（防半下载误报 present）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "model-00001-of-00003.gguf").write_text("")
        assert _verify_existing(d, "gguf_shards") is None

    def test_transformers_folder_with_weights(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")
        assert _verify_existing(d, "transformers_folder") == str(d)
        assert _verify_existing(tmp_path / "empty", "transformers_folder") is None

    def test_transformers_folder_config_only_none(self, tmp_path):
        """H1：仅 config.json 无权重（config 先到、权重没下完的中间态）→ 判未完成。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")
        assert _verify_existing(d, "transformers_folder") is None

    def test_manifest_authoritative_complete(self, tmp_path):
        """清单存在 → 逐项比对（含 layers-N 分片 + processor 文件）；全在位 → present。"""
        d = tmp_path / "m"
        d.mkdir()
        for name in ("config.json", "layers-0.safetensors", "layers-1.safetensors", "preprocessor_config.json"):
            (d / name).write_text("{}")
        (d / _MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "files": [
                        "config.json",
                        "layers-0.safetensors",
                        "layers-1.safetensors",
                        "preprocessor_config.json",
                    ]
                }
            )
        )
        assert _verify_existing(d, "transformers_folder") == str(d)

    def test_manifest_incomplete_none(self, tmp_path):
        """H1：layers-N 分片半下载（无 -of-N，命名启发式推断不出总数）→ 清单缺任一项即判未完成。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "layers-0.safetensors").write_text("{}")  # 只下了一片
        (d / _MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "files": [
                        "config.json",
                        "layers-0.safetensors",
                        "layers-1.safetensors",
                        "preprocessor_config.json",
                    ]
                }
            )
        )
        assert _verify_existing(d, "transformers_folder") is None

    def test_no_manifest_falls_back_heuristic(self, tmp_path):
        """无清单（pre-seeded 手动预置）→ 回退命名启发式（config.json + ≥1 权重即 present）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "layers-0.safetensors").write_text("")
        assert _verify_existing(d, "transformers_folder") == str(d)

    def test_hf_managed_no_manifest_partial_none(self, tmp_path):
        """H1：HF 下载器管理的目录（.cache/huggingface）无清单 + layers-N 半下载 → 判未完成（强制重下）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / ".cache" / "huggingface").mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "layers-0.safetensors").write_text("")
        assert _verify_existing(d, "transformers_folder") is None

    def test_hf_managed_no_manifest_complete_still_none(self, tmp_path):
        """HF 管理目录即便文件齐也无清单 → 判未完成（旧版下载产物，重下补清单再放行）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / ".cache" / "huggingface").mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")
        assert _verify_existing(d, "transformers_folder") is None

    def test_hf_managed_with_manifest_authoritative(self, tmp_path):
        """HF 管理目录 + 清单全在位 → present（清单覆盖 .cache 标记）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / ".cache" / "huggingface").mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "layers-0.safetensors").write_text("")
        (d / _MANIFEST_NAME).write_text(json.dumps({"files": ["config.json", "layers-0.safetensors"]}))
        assert _verify_existing(d, "transformers_folder") == str(d)

    def test_diffusers_folder_with_weights(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "model_index.json").write_text("{}")
        (d / "diffusion_pytorch_model.safetensors").write_text("")
        assert _verify_existing(d, "diffusers_folder") == str(d)

    def test_diffusers_folder_marker_only_none(self, tmp_path):
        """H1：仅 model_index.json 无权重 → 判未完成。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "model_index.json").write_text("{}")
        assert _verify_existing(d, "diffusers_folder") is None

    def test_diffusers_folder_nested_weights(self, tmp_path):
        """diffusers 权重在子目录（transformer/ vae/）——递归找权重，不误判完整下载未完成。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "model_index.json").write_text("{}")
        (d / "transformer").mkdir()
        for i in (1, 2, 3):
            (d / "transformer" / f"diffusion_pytorch_model-{i:05d}-of-00003.safetensors").write_text("")
        (d / "vae").mkdir()
        (d / "vae" / "diffusion_pytorch_model.safetensors").write_text("")
        assert _verify_existing(d, "diffusers_folder") == str(d)

    def test_diffusers_folder_nested_incomplete_none(self, tmp_path):
        """diffusers 嵌套分片只下部分（3 片缺 2）→ 判未完成。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "model_index.json").write_text("{}")
        (d / "transformer").mkdir()
        (d / "transformer" / "diffusion_pytorch_model-00001-of-00003.safetensors").write_text("")
        assert _verify_existing(d, "diffusers_folder") is None

    def test_unknown_nonempty(self, tmp_path):
        d = tmp_path / "m"
        d.mkdir()
        (d / "weights.bin").write_text("")
        assert _verify_existing(d, "unknown") == str(d)
        e = tmp_path / "empty"
        e.mkdir()
        assert _verify_existing(e, "unknown") is None

    def test_extra_files_at_repo_relative_path_not_under_subpath(self, tmp_path):
        """配套件（mmproj）按 repo 相对路径落在 local_dir 根——不在 subpath 子目录下。

        回归：旧实现判在位时叠 subpath 前缀（local_dir/<sub>/mmproj.gguf），而下载器
        （hf_hub_download 逐文件 repo 相对落盘）把它写在根级——文件下齐也恒判缺失，
        反复重下直到 attempt 耗尽，带视觉投影器的部署永远起不来。
        """
        d = tmp_path / "m"
        d.mkdir()
        (d / "sub").mkdir()
        (d / "sub" / "model.gguf").write_text("")
        (d / "mmproj-F16.gguf").write_text("")  # 配套件在根级（repo 相对位置）
        assert _verify_existing(d, "single_gguf", "sub", ["mmproj-F16.gguf"]) == str(d / "sub" / "model.gguf")
        # 配套件缺失仍判未就位（宁重下不缺件起容器）
        (d / "mmproj-F16.gguf").unlink()
        assert _verify_existing(d, "single_gguf", "sub", ["mmproj-F16.gguf"]) is None

    def test_extra_files_with_full_repo_prefix_no_double_prefix(self, tmp_path):
        """extras 自带完整 repo 前缀时直接命中（不叠 subpath 造成双重前缀恒缺失）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "sub").mkdir()
        (d / "sub" / "model.gguf").write_text("")
        (d / "mmproj" / "mmproj-F16.gguf").parent.mkdir(parents=True, exist_ok=True)
        (d / "mmproj" / "mmproj-F16.gguf").write_text("")
        assert _verify_existing(d, "single_gguf", "sub", ["mmproj/mmproj-F16.gguf"]) == str(d / "sub" / "model.gguf")

    def test_huggingface_metadata_and_part_excluded(self, tmp_path):
        """H1：HF 留 ``.huggingface`` 元数据 + ``.part`` 残片——不可误判为权重/在位。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / ".huggingface").mkdir()
        (d / ".huggingface" / "x.metadata").write_text("")
        (d / "incomplete.part").write_text("partial")
        (d / "config.json").write_text("{}")
        # 仅元数据无真实权重 → transformers 判未完成
        assert _verify_existing(d, "transformers_folder") is None

    def test_nonexistent_dir(self, tmp_path):
        assert _verify_existing(tmp_path / "nope", "transformers_folder") is None

    def test_empty_path_does_not_scan_cwd(self):
        """C1 防御：``Path('')`` 规范化为 '.'、``exists()`` 为真——必须拒绝，绝不扫 CWD。"""
        assert _verify_existing(Path(""), "unknown") is None
        assert _verify_existing(Path("."), "unknown") is None

    def test_subpath_locates_variant_subdir(self, tmp_path):
        """C1：source_subpath 非空 → hf_hub_download 保留 repo 相对结构，模型在 ``local_dir/<sub>/``。"""
        d = tmp_path / "m"
        d.mkdir()
        sub = d / "Q4_K_M"
        sub.mkdir()
        (sub / "config.json").write_text("{}")
        (sub / "model.safetensors").write_text("")
        # 不带 subpath（看根）：根只有 Q4_K_M 目录、无权重 → None
        assert _verify_existing(d, "transformers_folder") is None
        # 带 subpath → 定位到 local_dir/Q4_K_M → present
        assert _verify_existing(d, "transformers_folder", "Q4_K_M") == str(sub)

    def test_subpath_direct_link_single_file(self, tmp_path):
        """C1：直链 subpath（单文件 /resolve/.../x.gguf）→ 校验该文件本身。"""
        d = tmp_path / "m"
        d.mkdir()
        f = d / "Q4_K_M" / "model.gguf"
        f.parent.mkdir(parents=True)
        f.write_text("")
        assert _verify_existing(d, "single_gguf", "Q4_K_M/model.gguf") == str(f)


# ---- execute 端到端 ----


class TestExecute:
    async def test_check_existing_short_circuits_success(self, tmp_path):
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")  # H1：verify 需权重才判在位
        calls, report = _recorder()
        downloader_called = []

        async def dl(*a, **k):
            downloader_called.append(True)

        ex = EnsureModelExecutor(downloader=dl)
        await ex.execute(_task(d), None, report, asyncio.Event())

        assert downloader_called == []  # 未触发下载
        assert calls[-1]["status"] == "SUCCESS"
        assert calls[-1]["extra"]["model_path"] == str(d)

    async def test_disk_precheck_insufficient(self, tmp_path):
        d = tmp_path / "model-1"  # 不存在 → check_existing 失败
        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=1e9)
        await ex.execute(_task(d), None, report, asyncio.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "INSUFFICIENT_DISK" in calls[-1]["message"]

    async def test_local_absent_downloads_from_manager(self, tmp_path, monkeypatch):
        """source=local 缺失 → 走 manager 下载器（不再 FAILED）：manager_base_url/download_token/model_id 透传，token 不入上报。"""
        import llmpod.apps.node.executors._manager as mgrmod

        d = tmp_path / "model-1"
        recorded: dict = {}

        async def fake_download_manager(*, manager_base_url, download_token, model_id, local_dir, report, cancel_event):
            recorded.update(
                manager_base_url=manager_base_url,
                download_token=download_token,
                model_id=model_id,
                local_dir=str(local_dir),
            )
            await report("RUNNING", 50, stage="downloading")
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "config.json").write_text("{}")
            (local_dir / "model.safetensors").write_text("")  # verify 需权重

        monkeypatch.setattr(mgrmod, "download_manager", fake_download_manager)
        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=0)
        task = LocalTask(
            task_id="t1",
            type="ENSURE_MODEL",
            resource_type="MODEL_PLACEMENT",
            resource_key="model:1:n1",
            node_id="n1",
            status="PENDING",
            payload={
                "model_id": 1,
                "source": "local",
                "source_kind": "transformers_folder",
                "manager_base_url": "http://mgr/api/v1/distribution",
                "local_dir": str(d),
            },
        )
        await ex.execute(task, {"download_token": "SECRET-TOKEN"}, report, asyncio.Event())
        assert calls[-1]["status"] == "SUCCESS"
        assert recorded["manager_base_url"] == "http://mgr/api/v1/distribution"
        assert recorded["download_token"] == "SECRET-TOKEN"
        assert recorded["model_id"] == 1
        assert "SECRET-TOKEN" not in str(calls)  # token 不入上报内容

    async def test_default_downloader_delegates_to_hf(self, tmp_path, monkeypatch):
        """默认 downloader（非注入）→ _download_hf 委托 _hf.download_hf：source_url/subpath/revision
        透传，hf_token 仅经 token 参数注入（不入 report / local_dir）。"""
        import llmpod.apps.node.executors._hf as hfmod

        d = tmp_path / "model-1"
        recorded: dict = {}

        async def fake_download_hf(
            *,
            source_url,
            revision,
            source_subpath,
            token,
            local_dir,
            report,
            cancel_event,
            extra_files=None,
        ):
            recorded.update(
                source_url=source_url,
                revision=revision,
                source_subpath=source_subpath,
                token=token,
                local_dir=str(local_dir),
                extra_files=extra_files,
            )
            await report("RUNNING", 50, stage="downloading")
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "config.json").write_text("{}")
            (local_dir / "model.safetensors").write_text("")  # H1：verify 需权重

        monkeypatch.setattr(hfmod, "download_hf", fake_download_hf)
        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=0)
        task = LocalTask(
            task_id="t1",
            type="ENSURE_MODEL",
            resource_type="MODEL_PLACEMENT",
            resource_key="model:1:n1",
            node_id="n1",
            status="PENDING",
            payload={
                "model_id": 1,
                "source": "huggingface",
                "source_kind": "transformers_folder",
                "source_url": "Qwen/Qwen2.5-0.5B",
                "source_subpath": None,
                "local_dir": str(d),
            },
        )
        await ex.execute(task, {"hf_token": "SECRET-TOKEN"}, report, asyncio.Event())
        assert calls[-1]["status"] == "SUCCESS"
        assert recorded["source_url"] == "Qwen/Qwen2.5-0.5B"  # 配方字段透传下载器
        assert recorded["token"] == "SECRET-TOKEN"  # secrets 透传
        assert "SECRET-TOKEN" not in str(calls)  # 不入上报内容

    async def test_injected_downloader_success(self, tmp_path):
        d = tmp_path / "model-1"

        async def dl(payload, secrets, local_dir, report, cancel_event):
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "config.json").write_text("{}")
            (local_dir / "model.safetensors").write_text("")  # H1：verify 需权重

        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=0, downloader=dl)
        await ex.execute(_task(d), None, report, asyncio.Event())
        assert calls[-1]["status"] == "SUCCESS"
        assert calls[-1]["extra"]["model_path"] == str(d)
        # 成功路径报 verifying 阶段（downloading → verifying → finished）
        assert any(c["stage"] == "verifying" for c in calls)
        assert any(c["status"] == "RUNNING" for c in calls)
        # 每条进度事件都带冻结键 local_dir（node_model 投影读它）
        assert all(c["extra"].get("local_dir") == str(d) for c in calls)

    async def test_injected_downloader_failed(self, tmp_path):
        """下载失败 → Node 本地重试 N 次仍失败 → FAILED（不增 manager attempt）。"""
        d = tmp_path / "model-1"
        attempts = []

        async def dl(*a, **k):
            attempts.append(True)
            raise ValueError("network down")

        calls, report = _recorder()
        ex = EnsureModelExecutor(
            store_dir=str(tmp_path),
            min_free_gb=0,
            downloader=dl,
            download_retry_backoff=0,
        )
        await ex.execute(_task(d), None, report, asyncio.Event())
        assert len(attempts) == 3  # 本地重试 3 次
        assert calls[-1]["status"] == "FAILED"
        assert "network down" in calls[-1]["message"]
        assert "重试" in calls[-1]["message"]  # 标注已重试

    async def test_download_retries_then_succeeds(self, tmp_path):
        """前两次失败、第三次成功 → SUCCESS（瞬时故障经本地重试恢复，不升 attempt）。"""
        d = tmp_path / "model-1"
        attempts = []

        async def dl(payload, secrets, local_dir, report, cancel_event):
            attempts.append(True)
            if len(attempts) < 3:
                raise ConnectionError("transient blip")
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "config.json").write_text("{}")
            (local_dir / "model.safetensors").write_text("")  # H1：verify 需权重

        calls, report = _recorder()
        ex = EnsureModelExecutor(
            store_dir=str(tmp_path),
            min_free_gb=0,
            downloader=dl,
            download_retry_backoff=0,
        )
        await ex.execute(_task(d), None, report, asyncio.Event())
        assert len(attempts) == 3
        assert calls[-1]["status"] == "SUCCESS"

    async def test_cancel_checkpoint_cleans_part(self, tmp_path):
        """cancel_event 已置 + 残留 .part → checkpoint 清 .part + 报 CANCELLED。"""
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "x.part").write_text("partial")  # 非 config.json → check_existing 失败
        cancel = asyncio.Event()
        cancel.set()
        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path))
        await ex.execute(_task(d), None, report, cancel)
        assert calls[-1]["status"] == "CANCELLED"
        assert not (d / "x.part").exists()

    async def test_cancel_in_downloader_cleans_part(self, tmp_path):
        """downloader 抛 DownloadCancelled → 清 .part + 报 CANCELLED。"""
        d = tmp_path / "model-1"

        async def dl(payload, secrets, local_dir, report, cancel_event):
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "x.part").write_text("partial")
            raise DownloadCancelled()

        cancel = asyncio.Event()
        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=0, downloader=dl)
        await ex.execute(_task(d), None, report, cancel)
        assert calls[-1]["status"] == "CANCELLED"
        assert not (d / "x.part").exists()

    async def test_verify_failure_after_download(self, tmp_path):
        """下载完成但文件不符合 source_kind → FAILED + 清 .part。"""
        d = tmp_path / "model-1"

        async def dl(payload, secrets, local_dir, report, cancel_event):
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "junk.part").write_text("x")  # 无 config.json

        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=0, downloader=dl)
        await ex.execute(_task(d, source_kind="transformers_folder"), None, report, asyncio.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "verify" in calls[-1]["message"]

    async def test_empty_local_dir_failed_not_cwd(self):
        """C1 回归：空 local_dir 必须按原始串判空（``bool(Path(''))`` 为 True），不扫 CWD 误判在位。"""
        downloader_called = []

        async def dl(*a, **k):
            downloader_called.append(True)

        calls, report = _recorder()
        ex = EnsureModelExecutor(store_dir=".", min_free_gb=0, downloader=dl)
        await ex.execute(_task(""), None, report, asyncio.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "local_dir" in calls[-1]["message"]
        assert downloader_called == []  # 守卫生效，未走到下载
        assert all(c["status"] != "SUCCESS" for c in calls)  # 绝不误报在位

    async def test_reconcile_empty_dir_none(self):
        """C1：``reconcile`` 空 local_dir → None（不扫 CWD）。"""
        ex = EnsureModelExecutor()
        assert await ex.reconcile(_task("")) is None

    async def test_reconcile_present_returns_success(self, tmp_path):
        """reconcile 命中 → ReconcileResult(SUCCESS) + model_path（Executor ABC 契约）。"""
        d = tmp_path / "model-1"
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("")
        r = await EnsureModelExecutor().reconcile(_task(str(d)))
        assert r is not None and r.status == "SUCCESS"
        assert r.progress == 100
        assert r.extra["model_path"] == str(d)

    async def test_executor_reconcile_default_none(self):
        """Executor ABC 默认 reconcile → None（无廉价探测的类型可不覆写）。"""
        from llmpod.apps.node.executors.base import Executor

        class _Noop(Executor):
            async def execute(self, task, secrets, report, cancel_event):
                await report("SUCCESS")

        assert await _Noop().reconcile(_task("")) is None


class TestDownloadFailCleanup:
    """失败只清 ``.part`` 保留已完成文件（下一 attempt 续传）；取消才整目录清（用户意图释放磁盘）。

    部分目录不会被误判 present：manager 源下载开始即前置写入期望清单（逐项比对判未完成）；
    HF 源有 ``.cache/huggingface`` 半下载守卫。
    """

    async def test_failed_download_keeps_completed_files(self, tmp_path):
        """下载失败 → 清 .part、保留已完整落盘文件（续传，不再整轮重下）。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")  # 无权重 → verify 判未就位 → 进入下载
        task = _task(d, source="huggingface", source_kind="transformers_folder")
        calls, report = _recorder()

        async def boom(payload, secrets, local_dir, report, cancel_event):
            (local_dir / "layers-0.safetensors").write_text("partial")
            (local_dir / "done.bin").write_text("x")
            (local_dir / "half.part").write_text("partial")
            raise RuntimeError("network down")

        ex = EnsureModelExecutor(store_dir=tmp_path, downloader=boom, download_retries=1)
        await ex.execute(task, secrets=None, report=report, cancel_event=asyncio.Event())

        assert calls[-1]["status"] == "FAILED"
        assert "下载失败" in calls[-1]["message"]
        assert d.exists()  # 目录保留
        assert (d / "done.bin").exists()  # 已完成文件保留（下轮 attempt size 跳过）
        assert not (d / "half.part").exists()  # 中间态清除

    async def test_cancelled_download_with_preseed_keeps_user_files(self, tmp_path):
        """取消不连坐删预置内容：下载前目录已有文件 → 只清 .part，预置文件保留。"""
        d = tmp_path / "m"
        d.mkdir()
        (d / "config.json").write_text("{}")  # 手动预置（verify 判缺权重 → 进下载分支）
        task = _task(d, source="huggingface", source_kind="transformers_folder")
        calls, report = _recorder()
        cancel = asyncio.Event()

        async def boom(payload, secrets, local_dir, report, cancel_event):
            (local_dir / "w.safetensors.part").write_text("partial")
            raise DownloadCancelled()

        ex = EnsureModelExecutor(store_dir=tmp_path, downloader=boom, download_retries=1)
        await ex.execute(task, secrets=None, report=report, cancel_event=cancel)

        assert (d / "config.json").exists()  # 预置文件保留
        assert not (d / "w.safetensors.part").exists()  # .part 中间态清掉
        assert calls[-1]["status"] == "CANCELLED"

    async def test_cancelled_download_removes_fresh_partial_dir(self, tmp_path):
        """空目录起下载后取消 → 整目录清（纯本次产物，释放磁盘）。"""
        d = tmp_path / "m"
        task = _task(d, source="huggingface", source_kind="transformers_folder")
        calls, report = _recorder()
        cancel = asyncio.Event()

        async def boom(payload, secrets, local_dir, report, cancel_event):
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "w.safetensors.part").write_text("partial")
            raise DownloadCancelled()

        ex = EnsureModelExecutor(store_dir=tmp_path, downloader=boom, download_retries=1)
        await ex.execute(task, secrets=None, report=report, cancel_event=cancel)

        assert not d.exists()  # 本次下载产物整目录清掉
        assert calls[-1]["status"] == "CANCELLED"

    async def test_failed_partial_dir_not_verified_present(self, tmp_path):
        """失败保留的部分目录带期望清单 → verify 逐项比对判「未完成」（防误判 present）。"""
        from llmpod.apps.node.executors.ensure_model import _MANIFEST_NAME

        d = tmp_path / "m"
        d.mkdir()
        (d / _MANIFEST_NAME).write_text(json.dumps({"files": ["config.json", "w.safetensors"]}))
        (d / "config.json").write_text("{}")  # 期望清单里 w.safetensors 缺失
        path = _verify_existing(d, "unknown", None, None)
        assert path is None


class TestDiskPrecheckBySize:
    """磁盘预检按模型体积（payload.total_bytes）：体积×1.2 + 2GB 与固定阈值取大。"""

    async def test_large_model_requires_proportional_space(self, tmp_path):
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=10)
        # 体积 100GB → 需要 ≥ 100×1.2+2GB = 122GB > 固定 10GB 阈值
        assert ex._required_free_bytes(100 * 1024**3) == int(100 * 1024**3 * 1.2) + 2 * 1024**3

    async def test_small_model_falls_back_to_floor(self, tmp_path):
        ex = EnsureModelExecutor(store_dir=str(tmp_path), min_free_gb=10)
        # 体积 1GB → 1.2+2GB < 固定 10GB 阈值 → 按阈值
        assert ex._required_free_bytes(1024**3) == 10 * 1024**3
        assert ex._required_free_bytes(None) == 10 * 1024**3  # 无体积信息回落

    async def test_insufficient_for_declared_size_fails_fast(self, tmp_path, monkeypatch):
        """声明 100GB 模型但磁盘余量按小目录判 12GB（超过固定 10GB 阈值）→ 预检拦下。"""
        import shutil as _shutil

        d = tmp_path / "m"
        task = _task(d, source="local", source_kind="transformers_folder")
        task.payload["total_bytes"] = 100 * 1024**3

        class _FakeUsage:
            free = 12 * 1024**3
            total = 200 * 1024**3
            used = 188 * 1024**3

        monkeypatch.setattr(_shutil, "disk_usage", lambda p: _FakeUsage())
        calls, report = _recorder()

        async def dl(*a, **k):
            raise AssertionError("预检未拦下，进入下载")

        ex = EnsureModelExecutor(store_dir=tmp_path, min_free_gb=10, downloader=dl)
        await ex.execute(task, None, report, asyncio.Event())
        assert calls[-1]["status"] == "FAILED"
        assert "INSUFFICIENT_DISK" in calls[-1]["message"]
