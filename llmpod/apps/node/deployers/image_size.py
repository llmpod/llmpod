"""镜像下载总量预探测：拉取前读 registry manifest，把 daemon 将传输的 blob 大小拿回来。

为什么需要：docker daemon 的拉取进度流没有「总大小」字段，只逐层报
``progressDetail: {current, total}``，且 layer 是逐步排进下载队列的——按已出现层
聚合出的分母是**正在长大的下界**（前端进度条分母会从 0.0GB 一路跳到真实值）。
拉取前经 registry HTTP API（v2 协议）读一次 manifest，全部 blob 的压缩大小之和
就是 daemon 最终会传输的网络总量，用它当初始分母，第一条进度报文即真实百分比。

计数口径（与 daemon 实际传输对齐，不只是「镜像层」）：

- 选中平台的 manifest ``layers[].size`` + ``config`` blob（大头，daemon 传输的绝大部分）；
- buildx attestation manifest 按 index 条目自带 size 计入（daemon 会取，量级几百 KB~2MB；
  不逐个取体——多架构镜像可能有 8+ 个 attestation，串行取体会拖过探测 deadline）。

解析流程（docker distribution v2 / OCI，全程匿名 pull，不涉及凭据）：

1. ref 经 SDK ``resolve_repository_name`` 拆出 registry / repo（``python:3`` →
   docker.io + library/python；``repo:tag@digest`` 形态先剥 tag 再取 digest）；
2. ``GET {registry}/v2/`` 读 ``WWW-Authenticate`` Bearer challenge（realm/service），
   无 challenge 的私网 registry 直接放行；
3. 用匿名 token ``GET /v2/{repo}/manifests/{tag|digest}``（Accept 带 index/manifest
   两种 mediaType；index 再按 ``platform.architecture=amd64``（本机 arch 自动映射）
   选中对应 manifest；tag+digest 形态优先 digest）；
4. 汇总上述口径返回 ``[(digest_hex, size), ...]``——digest 供拉取流侧把「本地已缓存
   层」的完成态 chunk（Already exists 等）配对补足分子用。

docker.io（走 daemon 配的镜像加速站）的 registry 端点按 ``client.info()`` 的
``RegistryConfig.Mirrors``（即 daemon 本轮拉取会用的加速站列表）逐个试，取第一个
成功者——**与 daemon 实际行为同源**，不会出现「探测走 A 站、拉取走 B 站」的口径
偏差。官方 ``registry-1.docker.io`` 压轴
兜底（镜像站全挂时 daemon 也回落官方源）。daemon 标了 insecure 的 registry
（``IndexConfigs`` 里 ``Secure=False``，典型如 ``http://registry.lan:5000``）按明文
http 探测，与 daemon 行为一致。

失败语义：任何一步网络/协议失败都返回 ``None``，调用方回落「按已出现层聚合」
的旧口径——预探测是纯增强，绝不阻塞或打断拉取。
"""

from __future__ import annotations

import logging
import platform
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import httpx
from docker.auth import resolve_repository_name

log = logging.getLogger("llmpod.apps.node.deployers.image_size")

# manifest 请求的 Accept 头：多 manifest（index/list）与单 manifest 两种都收，
# registry 按内容协商返回其一
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# 官方 docker.io 的 registry 端点（镜像站全挂时的兜底；加速站列表从 daemon info 拿）
_DOCKER_IO_REGISTRY = "registry-1.docker.io"

# platform.machine() → OCI architecture 词表映射
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def node_arch() -> str:
    """本机架构 → OCI architecture 词表（探测多架构 index 选哪个子 manifest 用）。"""
    machine = platform.machine() or "amd64"
    return _ARCH_MAP.get(machine, machine)


def _normalize_base(host: str, insecure: set[str] | None) -> str:
    """registry 地址规范化为 ``scheme://host``：daemon 标 insecure 的按 http，其余 https。"""
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    scheme = "http" if insecure and host in insecure else "https"
    return f"{scheme}://{host}".rstrip("/")


def _is_attestation(entry: dict) -> bool:
    """index 子项是否 attestation manifest（platform 标 unknown/unknown 或缺 platform）。"""
    p = entry.get("platform") or {}
    return p.get("os") == "unknown" or p.get("architecture") == "unknown" or not p


def _pick_platform_manifest(manifest: dict, arch: str) -> dict | None:
    """多架构 index 里选 linux/{arch} 的子 manifest；无匹配返回 None。"""
    for m in manifest.get("manifests") or []:
        p = m.get("platform") or {}
        if p.get("os") == "linux" and p.get("architecture") == arch:
            return m
    return None


def _manifest_blobs(manifest: dict) -> list[tuple[str, int]]:
    """单 manifest 的传输 blob 清单：config + 全部 layer 的 (digest_hex, size)。"""
    blobs: list[tuple[str, int]] = []
    cfg = manifest.get("config") or {}
    if cfg.get("digest") and isinstance(cfg.get("size"), int):
        blobs.append((str(cfg["digest"]).split(":")[-1], int(cfg["size"])))
    for layer in manifest.get("layers") or []:
        if layer.get("digest") and isinstance(layer.get("size"), int):
            blobs.append((str(layer["digest"]).split(":")[-1], int(layer["size"])))
    return blobs


async def _anonymous_token(client: httpx.AsyncClient, base: str, repo: str) -> str | None:
    """读 ``/v2/`` 的 Bearer challenge 并换匿名 pull token；无 challenge 返回 None（直连放行）。"""
    resp = await client.get(f"{base}/v2/")
    auth = resp.headers.get("WWW-Authenticate") or resp.headers.get("www-authenticate")
    if not auth or not auth.lower().startswith("bearer"):
        # 无 challenge：私网直连 registry 或镜像站直接放行匿名 manifest
        return None
    # Bearer realm="...",service="..." → 逐参数解出（值可能带引号/URL 编码）
    params: dict[str, str] = {}
    for part in auth[len("Bearer ") :].split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip()] = unquote(v.strip().strip('"'))
    realm = params.get("realm")
    if not realm:
        return None
    # realm 自带的 query 不能被 params= 整体替换（httpx 的 params 是重建不是合并）——
    # 先解析原有 query，再补 challenge 的 scope/service
    challenge: dict[str, str] = {"scope": f"repository:{repo}:pull"}
    if "service" in params:
        challenge["service"] = params["service"]
    parsed = urlparse(realm)
    merged = dict(parse_qsl(parsed.query))
    for k, v in challenge.items():
        merged.setdefault(k, v)
    tok_url = parsed._replace(query=urlencode(merged)).geturl()
    tok_resp = await client.get(tok_url)
    if tok_resp.status_code != 200:
        return None
    data = tok_resp.json() or {}
    return data.get("token") or data.get("access_token") or None


async def _fetch_manifest(client: httpx.AsyncClient, base: str, repo: str, ref: str, headers: dict) -> dict | None:
    """GET 一个 manifest（200 才返回解析结果，其余返回 None）。"""
    resp = await client.get(f"{base}/v2/{repo}/manifests/{ref}", headers=headers)
    if resp.status_code != 200:
        return None
    return resp.json()


async def _probe_registry(
    client: httpx.AsyncClient, base: str, repo: str, tag_or_digest: str, arch: str
) -> list[tuple[str, int]] | None:
    """单个 registry 端点上解析 manifest 并汇总传输 blob 清单；失败返回 None。"""
    token = await _anonymous_token(client, base, repo)
    headers = {"Accept": _MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    manifest = await _fetch_manifest(client, base, repo, tag_or_digest, headers)
    if manifest is None:
        return None
    blobs: list[tuple[str, int]] = []
    if "manifests" in manifest:
        picked = _pick_platform_manifest(manifest, arch)
        if picked is None or "digest" not in picked:
            return None
        sub = await _fetch_manifest(client, base, repo, picked["digest"], headers)
        if sub is None:
            return None
        blobs.extend(_manifest_blobs(sub))
        # attestation 子 manifest（platform unknown）：daemon 会拉其 blob，按 index 条目
        # 自带的 size 计入（不再逐个拉 manifest 体——多架构镜像可能有 8+ 个，串行取体
        # 会把探测拖过 deadline；若 daemon 还会拉其 layer，层聚合超出种子时由取大兜底）
        for entry in manifest["manifests"]:
            if entry is not picked and _is_attestation(entry) and isinstance(entry.get("size"), int):
                digest = str(entry.get("digest") or "").split(":")[-1]
                if digest:
                    blobs.append((digest, int(entry["size"])))
    else:
        blobs.extend(_manifest_blobs(manifest))
    return blobs or None


async def probe_image_layers(
    image_ref: str,
    mirrors: list[str] | None = None,
    arch: str | None = None,
    insecure_registries: set[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[tuple[str, int]] | None:
    """镜像将传输的全部 blob 清单 ``[(digest_hex, size), ...]``；解析不了返回 None。

    ``mirrors`` 是 daemon 配的 docker.io 加速站（生产从 ``client.info()`` 取，与 daemon
    拉取同源）；``arch`` 缺省按本机架构映射；``insecure_registries`` 是 daemon 标了
    insecure 的 registry 主机名（明文 http 探测）。``transport`` 仅供测试注入。
    """
    try:
        registry, repo_with_ref = resolve_repository_name(image_ref)
    except Exception as e:
        log.debug("image_size %s: ref 解析失败: %s", image_ref, e)
        return None
    # digest 形态（repo[:tag]@sha256:...）优先 digest；tag 形态（repo:tag）取 tag；
    # 都没有则 latest（与 daemon 拉取的默认一致）。digest 分支要先剥可能残留的 tag
    if "@" in repo_with_ref:
        repo, _, digest = repo_with_ref.partition("@")
        tag_or_digest = digest
        if ":" in repo:  # repo:tag@digest → tag 不进 manifest 路径
            repo = repo.split(":", 1)[0]
    elif ":" in repo_with_ref:
        repo, _, tag_or_digest = repo_with_ref.partition(":")
    else:
        repo, tag_or_digest = repo_with_ref, "latest"
    # docker.io 的官方镜像（单段名）住在 library/ 命名空间下，manifest 路径要补前缀
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo

    if registry == "docker.io":
        candidates = [urlparse(m).hostname or m for m in (mirrors or [])]
        candidates.append(_DOCKER_IO_REGISTRY)
    else:
        candidates = [registry]
    bases = [_normalize_base(c, insecure_registries) for c in candidates]

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, transport=transport) as client:
        for base in bases:
            try:
                blobs = await _probe_registry(client, base, repo, tag_or_digest, arch or node_arch())
            except Exception as e:
                log.debug("image_size %s @ %s: 探测失败: %s", image_ref, base, e)
                continue
            if blobs is not None:
                return blobs
    return None
