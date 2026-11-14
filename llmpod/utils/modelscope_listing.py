"""ModelScope 文件清单获取（Manager 探测 / Node 下载共用，含分目录补枚举）。

``GET /api/v1/models/{org}/{name}/repo/files`` 单次最多回
:data:`~llmpod.utils.modelscope_ref.MODELSCOPE_LISTING_LIMIT` 条，多出来的被静默
丢弃——HTTP 200、``Success=true``、响应里没有任何「还有更多」的标记。少下的文件不会
报错，容器起来才缺件，因此这里在命中上限时**按目录分片重枚举**补齐。

补枚举的入口是 ``Root=<目录>`` 参数（服务端只列该子树）：
1. 整仓递归清单没到上限 → 直接返回，一次请求；
2. 到了上限 → 对根目录做一次**非递归**列出，拿到一级子目录与根级文件；
3. 逐个子目录用 ``Root`` 递归列一次，若该目录自身又到上限，再往下拆一层。

一级子目录数远超上限的情形（单目录几千个文件且都在最深一层）会用尽请求预算后明确
报错，而不是返回一份不知道少了什么的部分清单。

本模块是唯一负责「怎么向服务端要清单」的地方：Manager 与 Node 各自实现一份的话，
两边对「仓库里有哪些文件」的判断可能不一致，那类漂移只会在部署到一半时才暴露。
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import httpx

from llmpod.utils.modelscope_ref import (
    MODELSCOPE_LISTING_LIMIT,
    ModelScopeFile,
    extract_modelscope_files,
    file_entries_missing,
    latest_modelscope_short_id,
    listing_may_be_truncated,
    modelscope_files_api_url,
)

log = logging.getLogger("llmpod.utils.modelscope_listing")

# 补枚举最多发多少个请求：按 3000 条/次估算，够覆盖 150 万文件；超了说明目录结构
# 与「按目录切分」的假设不符（例如单目录内几千个文件），明确失败好过悄悄少下
_MAX_LISTING_REQUESTS = 500


class ModelScopeListing(NamedTuple):
    """一次清单获取的结果。

    ``files`` 是全仓 blob 清单；``short_commit`` 是服务端给的当前 commit 短 id
    （没有显式 revision 时用它钉版本）；``walked`` 表示是否走了分目录补枚举。
    """

    files: list[ModelScopeFile]
    short_commit: str | None
    walked: bool


class ModelScopeListingError(RuntimeError):
    """清单获取失败（仓库不存在、业务码非 200、补枚举用尽预算等）。"""


async def fetch_repo_files(
    client: httpx.AsyncClient,
    endpoint: str,
    repo_id: str,
    revision: str,
    *,
    root: str | None = None,
    recursive: bool = True,
    headers: dict[str, str] | None = None,
    label: str | None = None,
) -> dict:
    """请求一次 ``repo/files``，校验 HTTP 与 ModelScope 业务码后返回原始 JSON。

    ``root`` 非空时只列该子树（服务端 ``Root`` 参数）。``label`` 只用于错误文案。
    """
    params: dict[str, str] = {"Revision": revision, "Recursive": "true" if recursive else "false"}
    if root:
        params["Root"] = root
    shown = label or repo_id
    try:
        resp = await client.get(
            modelscope_files_api_url(endpoint, repo_id), params=params, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ModelScopeListingError(
            f"无法访问 ModelScope 仓库 {shown}（{type(e).__name__}）；"
            "请检查仓库是否存在、是否公开，或私有仓库 token 是否有效"
        ) from e

    if not isinstance(data, dict):
        raise ModelScopeListingError(f"ModelScope 仓库 {shown} 响应不是 JSON 对象")
    code = data.get("Code")
    if str(code) != "200" or data.get("Success") is False:
        message = str(data.get("Message") or "")[:200]
        raise ModelScopeListingError(f"ModelScope 仓库 {shown} 返回错误 {code}: {message}")
    return data


async def list_repo_files(
    client: httpx.AsyncClient,
    endpoint: str,
    repo_id: str,
    revision: str,
    *,
    headers: dict[str, str] | None = None,
    label: str | None = None,
    subpath: str | None = None,
) -> ModelScopeListing:
    """列出仓库全部 blob；命中服务端单次上限时按目录补枚举。

    ``subpath`` 是调用方已经从用户那里拿到的收窄条件，只在补枚举走进死胡同时用来兜底
    （见 :func:`_walk_by_root`）——正常仓库走一次整仓请求，不会因为它多花请求。
    """
    data = await fetch_repo_files(
        client, endpoint, repo_id, revision, headers=headers, label=label
    )
    files = extract_modelscope_files(data)
    short_commit = latest_modelscope_short_id(data)
    shown = label or repo_id
    if not listing_may_be_truncated(data):
        return ModelScopeListing(files, short_commit, False)

    log.warning(
        "ModelScope 仓库 %s 的递归清单达到服务端单次上限 %d，改为按目录补枚举",
        shown,
        MODELSCOPE_LISTING_LIMIT,
    )
    # 补枚举一律钉到首轮拿到的 commit：分支在两次请求之间可能前进，那样补回来的
    # 文件可能不在我们最终用于下载的 revision 上（列得出来、下不到，404）
    walk_rev = (short_commit or "").strip() or revision
    budget = [_MAX_LISTING_REQUESTS]
    walked = await _walk_by_root(
        client,
        endpoint,
        repo_id,
        walk_rev,
        root=None,
        headers=headers,
        label=shown,
        budget=budget,
        subpath=subpath,
    )
    # 补枚举回来的清单同样要去重（Root 列出的 path 已带目录前缀，正常不会重叠，
    # 但服务端行为和 SDK 的实现都表明这里不该假设）
    unique: dict[str, ModelScopeFile] = {}
    for item in walked:
        unique.setdefault(item.path, item)
    if not unique:
        raise ModelScopeListingError(f"ModelScope 仓库 {shown} 补枚举后没有可下载文件")
    return ModelScopeListing(list(unique.values()), short_commit, True)


async def _walk_by_root(
    client: httpx.AsyncClient,
    endpoint: str,
    repo_id: str,
    revision: str,
    *,
    root: str | None,
    headers: dict[str, str] | None,
    label: str,
    budget: list[int],
    subpath: str | None = None,
) -> list[ModelScopeFile]:
    """列 ``root`` 子树（None = 仓库根）；该子树自身仍超限时拆成子目录继续。"""
    if budget[0] <= 0:
        raise ModelScopeListingError(
            f"ModelScope 仓库 {label} 文件过多，补枚举请求次数用尽"
            f"（{_MAX_LISTING_REQUESTS} 次）；请改用子路径（source_subpath）收窄范围"
        )
    budget[0] -= 1

    data = await fetch_repo_files(
        client, endpoint, repo_id, revision, root=root, headers=headers, label=label
    )
    if file_entries_missing(data):
        # ``Root`` 指向不存在的目录时服务端回 HTTP 200 + ``Code: 200`` + ``Files: null``
        # （实测）。git 不跟踪空目录，所以 null 只可能意味着「这个子树列不出来」——
        # 补枚举途中出现（目录被删、名字带多余空格）必须当场失败：静默当空目录会让
        # 最终下载的仓库少一整个子树，而调用方看起来一切正常。
        raise ModelScopeListingError(
            f"ModelScope 仓库 {label} 的目录 {root or '/'} 列不出文件（服务端返回 Files: null）；"
            "该目录可能不存在或已被删除，已中止以免下载出缺件的仓库"
        )
    if not listing_may_be_truncated(data):
        return extract_modelscope_files(data)

    # 该子树自己也到上限：列出直属一层，根级 blob 直接收，子目录各自递归
    budget[0] -= 1
    shallow = await fetch_repo_files(
        client, endpoint, repo_id, revision, root=root, recursive=False, headers=headers, label=label
    )
    entries = (shallow.get("Data") or {}).get("Files") or []
    out: list[ModelScopeFile] = [
        item for item in extract_modelscope_files(shallow) if item.path
    ]
    subdirs = [
        str(entry.get("Path") or "").strip().lstrip("/")
        for entry in entries
        if isinstance(entry, dict) and entry.get("Type") == "tree"
    ]
    if not subdirs:
        narrowed = await _narrow_by_subpath(
            client, endpoint, repo_id, revision, root=root, subpath=subpath, headers=headers, label=label
        )
        if narrowed is not None:
            return narrowed
        raise ModelScopeListingError(
            f"ModelScope 仓库 {label} 的目录 {root or '/'} 内文件数超过服务端单次上限"
            f"（{MODELSCOPE_LISTING_LIMIT}）且没有可继续拆分的子目录；"
            "请改用子路径（source_subpath）收窄范围"
        )
    for subdir in subdirs:
        if not subdir:
            continue
        out.extend(
            await _walk_by_root(
                client,
                endpoint,
                repo_id,
                revision,
                root=subdir,
                headers=headers,
                label=label,
                budget=budget,
                subpath=subpath,
            )
        )
    return out


async def _narrow_by_subpath(
    client: httpx.AsyncClient,
    endpoint: str,
    repo_id: str,
    revision: str,
    *,
    root: str | None,
    subpath: str | None,
    headers: dict[str, str] | None,
    label: str,
) -> list[ModelScopeFile] | None:
    """目录拆不动时，用调用方给的子路径直接列那一段——按它下载本来就不需要别的文件。

    返回 None 表示没有可用的收窄条件（没给子路径、或子路径不在这个拆不动的目录里），
    由调用方报错。
    """
    target = (subpath or "").strip("/")
    if not target:
        return None
    if root and not (target == root or target.startswith(root + "/")):
        return None
    # 先按目录直接列 subpath 自己
    data = await fetch_repo_files(
        client, endpoint, repo_id, revision, root=target, headers=headers, label=label
    )
    if file_entries_missing(data):
        return None
    if not listing_may_be_truncated(data):
        files = extract_modelscope_files(data)
        if files:
            log.info(
                "ModelScope 仓库 %s 目录 %s 拆不动，改用子路径 %s 收窄清单（%d 个文件）",
                label,
                root or "/",
                target,
                len(files),
            )
            return files

    # subpath 指向单个文件时 ``Root`` 只认目录，会回空列表：改列父目录再挑回来
    parent = target.rsplit("/", 1)[0] if "/" in target else ""
    if parent == target:
        return None
    data = await fetch_repo_files(
        client, endpoint, repo_id, revision, root=parent or None, headers=headers, label=label
    )
    if file_entries_missing(data) or listing_may_be_truncated(data):
        return None
    files = [
        item
        for item in extract_modelscope_files(data)
        if item.path == target or item.path.startswith(target + "/")
    ]
    if not files:
        return None
    log.info(
        "ModelScope 仓库 %s 目录 %s 拆不动，改用子路径 %s 收窄清单（%d 个文件）",
        label,
        root or "/",
        target,
        len(files),
    )
    return files


__all__ = [
    "ModelScopeListing",
    "ModelScopeListingError",
    "fetch_repo_files",
    "list_repo_files",
]
