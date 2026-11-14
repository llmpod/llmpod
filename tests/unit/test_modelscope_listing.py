"""``modelscope_listing`` 单测：清单获取与「命中单次上限后按目录补枚举」。

真实服务端在小仓库上看不出差别（一次就回全），所以这里用 MockTransport 造出
「恰好等于上限」「子目录也超限」「没有可拆的子目录」三种形态——它们决定的是
「一个万文件仓库会不会被静默少下一部分」。
"""

from __future__ import annotations

import httpx
import pytest

from llmpod.utils.modelscope_listing import (
    ModelScopeListingError,
    fetch_repo_files,
    list_repo_files,
)
from llmpod.utils.modelscope_ref import MODELSCOPE_LISTING_LIMIT

ENDPOINT = "https://ms.test"


def _blob(path: str, size: int = 1) -> dict:
    return {"Path": path, "Size": size, "Type": "blob"}


def _tree(path: str) -> dict:
    return {"Path": path, "Size": 0, "Type": "tree"}


def _resp(entries: list[dict], short: str = "abc1234") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "Code": 200,
            "Success": True,
            "Data": {"Files": entries, "LatestCommitter": {"ShortId": short}},
        },
    )


async def _listing(handler, revision: str = "master"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await list_repo_files(client, ENDPOINT, "org/repo", revision)


async def test_short_listing_single_request():
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return _resp([_blob("a.json"), _blob("w.safetensors", 100)])

    listing = await _listing(handler)

    assert [f.path for f in listing.files] == ["a.json", "w.safetensors"]
    assert listing.short_commit == "abc1234"
    assert listing.walked is False
    assert len(calls) == 1
    assert calls[0]["Recursive"] == "true"


async def test_listing_at_limit_walks_subdirectories():
    """整仓递归正好等于上限：拆成「根级非递归 + 逐个子目录递归」补全。"""
    # 每个子目录都在上限以内（真实仓库的常态：总量过万、单目录几千）
    sub_a = [_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT - 100)]
    sub_b = [_blob(f"b/f{i}.bin") for i in range(5)]

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        root = params.get("Root")
        recursive = params.get("Recursive") == "true"
        if root is None and recursive:
            # 整仓递归：命中上限，内容被截断（真实服务端就是这样）
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root is None and not recursive:
            return _resp([_tree("a"), _tree("b"), _blob("README.md")])
        if root == "a" and recursive:
            return _resp(sub_a)
        if root == "b" and recursive:
            return _resp(sub_b)
        raise AssertionError(f"意外请求 root={root} recursive={recursive}")

    listing = await _listing(handler)

    assert listing.walked is True
    paths = {f.path for f in listing.files}
    assert "README.md" in paths
    assert "b/f0.bin" in paths
    assert f"a/f{MODELSCOPE_LISTING_LIMIT - 101}.bin" in paths
    assert len(paths) == MODELSCOPE_LISTING_LIMIT - 100 + 5 + 1


async def test_nested_subdirectory_at_limit_is_split_again():
    """子目录自己又到上限：再拆一层，而不是停在那里少下文件。"""
    inner = [_blob(f"a/deep1/f{i}.bin") for i in range(10)]
    inner2 = [_blob(f"a/deep2/f{i}.bin") for i in range(10)]

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        root, recursive = params.get("Root"), params.get("Recursive") == "true"
        if root is None and recursive:
            # 整仓命中上限
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root is None and not recursive:
            return _resp([_tree("a")])
        if root == "a" and recursive:
            # a 子树同样到上限：被截断
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root == "a" and not recursive:
            return _resp([_tree("a/deep1"), _tree("a/deep2")])
        if root == "a/deep1" and recursive:
            return _resp(inner)
        if root == "a/deep2" and recursive:
            return _resp(inner2)
        raise AssertionError(f"意外请求 root={root} recursive={recursive}")

    listing = await _listing(handler)

    assert listing.walked is True
    assert {f.path for f in listing.files} == {entry["Path"] for entry in inner + inner2}


async def test_flat_directory_at_limit_fails_loudly():
    """单目录内几千个文件、没有子目录可拆：明确失败，不返回残缺清单。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("Recursive") == "true":
            return _resp([_blob(f"f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        return _resp([_blob(f"f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])

    with pytest.raises(ModelScopeListingError, match="子路径"):
        await _listing(handler)


async def test_shallow_listing_at_limit_fails_loudly():
    """非递归列出自己也命中上限（直属条目被截断）：子目录集不完整，明确失败。

    根级文件 + 大量子目录同处一层时会出现：浅层清单被静默截断后，丢掉的子目录
    不会进入递归，最终清单必缺件。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        root = params.get("Root")
        recursive = params.get("Recursive") == "true"
        if root is None and recursive:
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root is None and not recursive:
            # 根下一层同样到上限（真实服务端行为：静默截断，无任何标记）
            entries = [_blob("README.md")] + [_tree(f"d{i}") for i in range(MODELSCOPE_LISTING_LIMIT - 1)]
            return _resp(entries)
        raise AssertionError(f"意外请求 root={root} recursive={recursive}")

    with pytest.raises(ModelScopeListingError, match="非递归列出也被截断"):
        await _listing(handler)


async def test_business_error_code_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"Code": 10010205001, "Success": False, "Message": "record not found"}
        )

    with pytest.raises(ModelScopeListingError, match="10010205001"):
        await _listing(handler)


async def test_http_404_raises_actionable_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(ModelScopeListingError, match="私有仓库 token"):
        await _listing(handler)


async def test_null_files_in_walk_fails_instead_of_silently_dropping_subtree():
    """``Root`` 列不出文件（服务端回 Files: null）必须失败。

    静默当空目录会让最终下载的仓库少一整个子树，而调用方看起来一切正常。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("Root") is None and params.get("Recursive") == "true":
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if params.get("Root") is None:
            return _resp([_tree("a"), _tree("b")])
        if params.get("Root") == "a":
            return _resp([_blob("a/x.bin")])
        # Root=b：服务端找得到目录却列不出文件
        return httpx.Response(
            200,
            json={"Code": 200, "Success": True, "Data": {"Files": None, "LatestCommitter": {"ShortId": "abc"}}},
        )

    with pytest.raises(ModelScopeListingError, match="Files: null"):
        await _listing(handler)


async def test_walk_pins_first_listing_commit():
    """补枚举必须钉到首轮 commit：分支在两次请求之间前进会列出下不到的文件。"""
    seen_revisions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        seen_revisions.append(params.get("Revision", ""))
        if params.get("Root") is None and params.get("Recursive") == "true":
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)], short="deadbee")
        if params.get("Root") is None:
            return _resp([_tree("a")], short="deadbee")
        return _resp([_blob("a/x.bin")], short="deadbee")

    listing = await _listing(handler, revision="master")

    assert listing.short_commit == "deadbee"
    # 首轮用调用方给的 revision，之后全部用钉住的 commit
    assert seen_revisions[0] == "master"
    assert all(r == "deadbee" for r in seen_revisions[1:])


async def test_flat_directory_uses_subpath_instead_of_failing():
    """目录拆不动但调用方给了子路径：直接列那一段，而不是要求用户「用子路径收窄」。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        root, recursive = params.get("Root"), params.get("Recursive") == "true"
        if root is None and recursive:
            return _resp([_blob(f"f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root is None and not recursive:
            # 全是根级 blob，没有可拆的子目录
            return _resp([_blob(f"f{i}.bin") for i in range(20)])
        if root == "Q4_K_M":
            return _resp([_blob("Q4_K_M/model.gguf"), _blob("Q4_K_M/config.json")])
        raise AssertionError(f"意外请求 root={root} recursive={recursive}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        listing = await list_repo_files(
            client, ENDPOINT, "org/repo", "master", subpath="Q4_K_M"
        )

    assert {f.path for f in listing.files} == {"Q4_K_M/model.gguf", "Q4_K_M/config.json"}


async def test_subpath_pointing_at_a_file_lists_its_parent():
    """subpath 是单个文件时 ``Root`` 指到文件会回空列表，改列父目录再挑回来。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        root, recursive = params.get("Root"), params.get("Recursive") == "true"
        if root is None and recursive:
            return _resp([_blob(f"f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if root is None and not recursive:
            return _resp([_blob(f"f{i}.bin") for i in range(20)])
        if root == "Q4_K_M":
            return _resp([_blob("Q4_K_M/model.gguf"), _blob("Q4_K_M/config.json")])
        if root == "Q4_K_M/model.gguf":
            # 真实服务端行为：Root 指到文件时回空列表
            return _resp([])
        raise AssertionError(f"意外请求 root={root} recursive={recursive}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        listing = await list_repo_files(
            client, ENDPOINT, "org/repo", "master", subpath="Q4_K_M/model.gguf"
        )

    assert {f.path for f in listing.files} == {"Q4_K_M/model.gguf"}


async def test_auth_headers_sent_on_every_request():
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        params = request.url.params
        if params.get("Recursive") == "true" and params.get("Root") is None:
            return _resp([_blob(f"a/f{i}.bin") for i in range(MODELSCOPE_LISTING_LIMIT)])
        if params.get("Recursive") == "false":
            return _resp([_tree("a")])
        return _resp([_blob("a/x.bin")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await list_repo_files(
            client, ENDPOINT, "org/repo", "master", headers={"Authorization": "Bearer SECRET"}
        )

    assert seen and all(value == "Bearer SECRET" for value in seen)


async def test_fetch_repo_files_passes_root_and_recursive():
    seen: list[tuple[str | None, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.params.get("Root"), request.url.params.get("Recursive")))
        return _resp([_blob("a/x.bin")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_repo_files(client, ENDPOINT, "org/repo", "master", root="a", recursive=False)

    assert seen == [("a", "false")]
