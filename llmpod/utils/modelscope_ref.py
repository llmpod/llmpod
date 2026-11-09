"""ModelScope 仓库引用解析与 URL 构造（Manager 探测器 / Node 下载器共用）。

与 HuggingFace 的 ``parse_hf_ref`` 不同，ModelScope 的仓库路径是
``/models/{org}/{name}``，默认分支是 ``master``，文件页/下载页分别是
``/file/view/{rev}/{subpath}`` 与 ``/resolve/{rev}/{subpath}``。
本模块只做纯字符串解析，不读 settings、不发请求。
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

# ModelScope 默认分支（HF 是 main）
MODELSCOPE_DEFAULT_REVISION = "master"


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


def extract_modelscope_files(data: dict) -> list[tuple[str, int | None]]:
    """文件清单响应 → ``[(Path, Size)]``，只保留 blob，路径去前导斜杠。"""
    out: list[tuple[str, int | None]] = []
    for item in (data.get("Data") or {}).get("Files") or []:
        if not isinstance(item, dict) or item.get("Type") != "blob":
            continue
        path = str(item.get("Path") or "").strip().lstrip("/")
        if not path:
            continue
        size = item.get("Size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = None
        out.append((path, size))
    return out


def latest_modelscope_short_id(data: dict) -> str | None:
    """文件清单响应里的 ``Data.LatestCommitter.ShortId``（可作 revision 的短 commit）。"""
    short = ((data.get("Data") or {}).get("LatestCommitter") or {}).get("ShortId")
    return str(short) if short else None
