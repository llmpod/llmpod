"""ModelScope 仓库引用解析与 URL 构造（Manager 探测器 / Node 下载器共用）。

与 HuggingFace 的 ``parse_hf_ref`` 不同，ModelScope 的仓库路径是
``/models/{org}/{name}``，默认分支是 ``master``，文件页/下载页分别是
``/file/view/{rev}/{subpath}`` 与 ``/resolve/{rev}/{subpath}``。
本模块只做纯字符串解析，不读 settings、不发请求。
"""

from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import quote, unquote, urlparse

# ModelScope 默认分支（HF 是 main）
MODELSCOPE_DEFAULT_REVISION = "master"

# ``repo/files`` 单次最多回这么些条目，超出部分静默丢弃（HTTP 200、Success=true，
# 响应里没有任何「还有更多」的标记）。正好等于这个数就意味着可能已经被截断——
# 少下的文件不会报错，容器起来才缺件，所以按「可能不完整」处理而不是当正常结果。
MODELSCOPE_LISTING_LIMIT = 3000

_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


class ModelScopeFile(NamedTuple):
    """文件清单里的一条：``path`` 仓库相对路径、``size`` 字节数、``sha256`` 可选哈希。"""

    path: str
    size: int | None
    sha256: str | None


def parse_modelscope_ref(source_url: str) -> tuple[str, str | None, str | None, str | None]:
    """``source_url`` → ``(repo_id, endpoint, subpath, url_revision)``。

    - 裸 ``org/name``：``(org/name, None, None, None)``。
    - 仓库根 URL ``https://host/models/org/name``：endpoint 取 ``scheme://host``。
    - ``/resolve/{rev}/{subpath}`` 与 ``/file/view/{rev}/{subpath}``：同时取 rev 与 subpath。
    - ``/files``、``/summary`` 等页面视为仓库根，不带子路径。
    - 其他尾段按子路径处理；path 段做 URL decode。
    - 不合法（不足 ``org/name``）抛 ``ValueError``。

    endpoint 的 host 由调用方优先使用；裸 id 由调用方回落到 settings。
    """
    s = (source_url or "").strip()
    if not s:
        raise ValueError("ModelScope 仓库地址为空")

    if not s.startswith(("http://", "https://")):
        repo, subpath = _split_bare(s)
        return repo, None, subpath, None

    parsed = urlparse(s)
    endpoint = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    # 兼容带/不带 /models 前缀的 URL；取 models 之后的两段为组织/模型名
    start = parts.index("models") + 1 if "models" in parts else 0
    if len(parts) < start + 2:
        raise ValueError(f"无法从 URL 解析 ModelScope 仓库：{source_url}")
    repo = f"{parts[start]}/{parts[start + 1]}"
    rest = parts[start + 2 :]

    revision: str | None = None
    subpath: str | None = None
    if len(rest) >= 2 and rest[0] == "resolve":
        revision = rest[1]
        subpath = "/".join(rest[2:]) or None
    elif len(rest) >= 3 and rest[0] == "file" and rest[1] == "view":
        revision = rest[2]
        subpath = "/".join(rest[3:]) or None
    elif rest and rest[0] not in ("files", "summary", "file"):
        # 其他页面尾段（如 /discussions）忽略：子路径只认 resolve / file-view 两种明确形态
        subpath = None

    return repo, endpoint, subpath, revision


def _split_bare(s: str) -> tuple[str, str | None]:
    """裸 ``org/name[/subpath...]`` → ``(org/name, subpath)``。"""
    path = s.split("?", 1)[0].strip("/")
    if path.startswith("models/"):
        path = path[len("models/") :]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"ModelScope 仓库需为 组织/模型名：{s}")
    return f"{parts[0]}/{parts[1]}", "/".join(parts[2:]) or None


def modelscope_repo_api_url(endpoint: str, repo_id: str) -> str:
    """仓库元信息 URL（``/api/v1/models/{org}/{name}``）。"""
    return f"{endpoint.rstrip('/')}/api/v1/models/{quote(repo_id.strip('/'), safe='/')}"


def modelscope_files_api_url(endpoint: str, repo_id: str) -> str:
    """仓库文件清单 URL（``/api/v1/models/{org}/{name}/repo/files``）。"""
    return f"{modelscope_repo_api_url(endpoint, repo_id)}/repo/files"


def modelscope_resolve_url(endpoint: str, repo_id: str, revision: str, rel_path: str) -> str:
    """文件内容 URL（``/models/{org}/{name}/resolve/{rev}/{rel}``）。"""
    repo = quote(repo_id.strip("/"), safe="/")
    rev = quote((revision or "").strip("/"), safe="")
    rel = quote(rel_path.lstrip("/"), safe="/")
    return f"{endpoint.rstrip('/')}/models/{repo}/resolve/{rev}/{rel}"


def extract_modelscope_files(data: dict) -> list[ModelScopeFile]:
    """文件清单响应 → ``[ModelScopeFile]``，只保留 blob，路径去前导斜杠。

    ``Sha256`` 一并取出：它是下载完成后校验内容的唯一依据（响应头 ``X-Linked-Etag``
    只在 LFS 文件上有，且要额外发请求）。
    """
    out: list[ModelScopeFile] = []
    for item in (data.get("Data") or {}).get("Files") or []:
        if not isinstance(item, dict) or item.get("Type") != "blob":
            continue
        path = str(item.get("Path") or "").strip().lstrip("/")
        if not path:
            continue
        size = item.get("Size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        sha = item.get("Sha256")
        sha256 = sha.strip().lower() if isinstance(sha, str) and _SHA256_HEX.fullmatch(sha.strip()) else None
        out.append(ModelScopeFile(path, size, sha256))
    return out


def listing_may_be_truncated(data: dict) -> bool:
    """清单恰好等于服务端单次上限 → 可能被截断（多出来的条目已被静默丢弃）。"""
    files = (data.get("Data") or {}).get("Files") or []
    return len(files) >= MODELSCOPE_LISTING_LIMIT


def file_entries_missing(data: dict) -> bool:
    """响应的 ``Data.Files`` 是 ``null`` 而不是列表。

    出现过在 ``Root`` 指向不存在目录时：HTTP 200、``Code: 200``、``Files: null``。
    与「空目录」不同义——空目录回空列表，这里连列表都没有，说明服务端没找到那个子树。
    """
    return (data.get("Data") or {}).get("Files") is None


def latest_modelscope_short_id(data: dict) -> str | None:
    """文件清单响应里的 ``Data.LatestCommitter.ShortId``（可作 revision 的短 commit）。"""
    short = ((data.get("Data") or {}).get("LatestCommitter") or {}).get("ShortId")
    return str(short) if short else None
