"""ModelScope 探测器（Manager 侧 httpx，**不下载** 权重）。

流程与 HF 对齐：
1. ``GET {endpoint}/api/v1/models/{org}/{name}/repo/files?Revision=...&Recursive=true``
   → 文件清单 + ``LatestCommitter.ShortId``；
2. best-effort 拉仓库元信息（``Tasks[0].Name`` 作类别建议、``Architectures[0]`` 作架构兜底）；
3. best-effort 拉 ``config.json``（transformers）或 ``model_index.json``（diffusers）；
4. GGUF 时 Range 拉文件头，复用 HF 的装配逻辑；
5. 任一步失败 → 软失败（``success=False``），已能拿到的部分仍返回。

仅支持公开模型：私有/不存在仓库在文件清单阶段返回 404，统一给中文提示。
"""

from __future__ import annotations

import logging

import httpx

from llmpod.apps.manager.model_source import inference, remote_gguf
from llmpod.apps.manager.model_source.base import ModelFiles, ModelInspector
from llmpod.apps.manager.model_source.huggingface import build_model_files
from llmpod.config.settings import settings
from llmpod.utils.modelscope_listing import list_repo_files
from llmpod.utils.modelscope_ref import (
    MODELSCOPE_DEFAULT_REVISION,
    ModelScopeFile,
    modelscope_repo_api_url,
    modelscope_resolve_url,
    parse_modelscope_ref,
)

log = logging.getLogger("llmpod.apps.manager.model_source.modelscope")


class ModelScopeInspector(ModelInspector):
    """ModelScope 公开仓库探测器。"""

    async def inspect(
        self,
        *,
        source_url: str,
        revision: str | None = None,
    ) -> ModelFiles:
        try:
            repo_id, endpoint, subpath, url_rev = parse_modelscope_ref(source_url)
        except ValueError as e:
            return ModelFiles(success=False, message=f"自动获取失败：{e}")

        endpoint = (endpoint or settings.model_source_modelscope_endpoint).rstrip("/")
        # URL 内显式 revision 优先于表单；都未指定时用来源默认分支，稍后钉到具体 commit
        explicit_rev = url_rev or (revision or "").strip() or None
        request_rev = explicit_rev or MODELSCOPE_DEFAULT_REVISION
        timeout = settings.model_source_probe_timeout

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # 文件清单是成败分水岭：不存在/私有仓库在这里拿到 404。
            # 命中服务端单次上限时 list_repo_files 会按目录补枚举，保证清单完整
            try:
                listing = await list_repo_files(
                    client, endpoint, repo_id, request_rev, subpath=subpath
                )
            except Exception as e:  # 网络不通 / 仓库不存在 / 私有 / 文件过多补不全
                log.warning("ModelScope probe files failed for %s: %s", repo_id, e)
                return ModelFiles(
                    success=False,
                    message=(
                        f"自动获取失败：无法访问 ModelScope 仓库 {repo_id}（{type(e).__name__}）。"
                        "请确认仓库存在且为公开模型；私有模型暂不支持。"
                    ),
                )

            all_files = listing.files
            if not all_files:
                return ModelFiles(
                    success=False,
                    message=f"自动获取失败：ModelScope 仓库 {repo_id} 没有可下载文件",
                )
            all_names = [item.path for item in all_files]

            # 未显式指定版本时钉到当前 commit，避免仓库前进后目录名不变、内容漂移
            pinned_rev = explicit_rev or listing.short_commit or request_rev

            scoped = _scope_files(all_files, subpath)
            if subpath and not scoped:
                return ModelFiles(
                    success=False,
                    message=f"自动获取失败：ModelScope 仓库 {repo_id} 中不存在子路径 {subpath}",
                )

            task_name, arch_hint = await _repo_meta(client, endpoint, repo_id)

            config: dict = {}
            model_index: dict | None = None
            config_error: str | None = None
            names_lower = [n.lower() for n in all_names]
            if "config.json" in names_lower:
                config, config_error = await _fetch_json(
                    client, modelscope_resolve_url(endpoint, repo_id, pinned_rev, "config.json"), repo_id, "config.json"
                )
            elif "model_index.json" in names_lower:
                model_index, config_error = await _fetch_json(
                    client,
                    modelscope_resolve_url(endpoint, repo_id, pinned_rev, "model_index.json"),
                    repo_id,
                    "model_index.json",
                )

            gguf_header = None
            scoped_names = [item.path for item in scoped]
            if inference.infer_format([n.lower() for n in scoped_names]) == "gguf":
                rel = inference.first_gguf_shard(scoped_names)
                if rel:
                    gguf_header = await remote_gguf.probe_gguf_metadata_remote(
                        client, modelscope_resolve_url(endpoint, repo_id, pinned_rev, rel)
                    )

        files = build_model_files(
            [{"rfilename": item.path, "size": item.size} for item in scoped],
            task_name,
            config=config,
            model_index=model_index,
            revision=explicit_rev,
            sha=pinned_rev,
            config_error=config_error,
            gguf_header=gguf_header,
            source_subpath=subpath,
            repo_names=all_names,
            resolve_revision=pinned_rev,
        )
        # ModelScope 元信息的架构是 config 缺失时的兜底；不污染已有推断结果。
        # 软失败（如多变体 GGUF）也回填，供前端预览。
        if not files.architecture and arch_hint:
            files = files.model_copy(update={"architecture": arch_hint})
        return files


async def _api_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None) -> dict:
    """GET + HTTP 2xx + JSON + ModelScope 业务码校验；失败抛异常由调用方转软失败。"""
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("ModelScope 响应不是 JSON 对象")
    code = data.get("Code")
    if str(code) != "200" or data.get("Success") is False:
        raise RuntimeError(f"ModelScope 业务错误 {code}: {str(data.get('Message') or '')[:200]}")
    return data


def _scope_files(files: list[ModelScopeFile], subpath: str | None) -> list[ModelScopeFile]:
    sp = (subpath or "").strip("/")
    if not sp:
        return files
    return [item for item in files if item.path == sp or item.path.startswith(sp + "/")]


async def _repo_meta(client: httpx.AsyncClient, endpoint: str, repo_id: str) -> tuple[str | None, str | None]:
    """best-effort 取（任务标签, 架构名）；失败不阻断探测。"""
    try:
        data = await _api_json(client, modelscope_repo_api_url(endpoint, repo_id))
    except Exception as e:
        log.warning("ModelScope probe meta failed for %s: %s", repo_id, e)
        return None, None

    meta = data.get("Data") or {}
    tasks = meta.get("Tasks") or []
    task = str(tasks[0].get("Name") or "") if tasks and isinstance(tasks[0], dict) else None
    archs = meta.get("Architectures") or []
    arch = archs[0] if archs and isinstance(archs[0], str) else None
    return task or None, arch or None


async def _fetch_json(client: httpx.AsyncClient, url: str, repo_id: str, name: str) -> tuple[dict, str | None]:
    """best-effort 拉 config/model_index；失败返回空 dict + 简短错误标记。"""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json(), None
    except Exception as e:
        log.warning("ModelScope probe %s failed for %s: %s", name, repo_id, e)
        return {}, f"{name}: {type(e).__name__}"


__all__ = ["ModelScopeInspector"]
