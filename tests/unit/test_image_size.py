"""image_size 单测：manifest 预探测（ref 解析 / v2 匿名 token / index→arch / 失败回落 None）。

httpx.MockTransport 伪造 registry 行为，不发真实网络请求。
"""

from __future__ import annotations

import httpx

from llmpod.apps.node.deployers.image_size import node_arch, probe_image_layers


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _single_manifest(layers=(("aaaa", 100), ("bbbb", 50)), config=("cccc", 20)):
    """单 manifest（config + layers，均带 digest）。"""
    m: dict = {"schemaVersion": 2}
    if config:
        m["config"] = {"digest": f"sha256:{config[0]}", "size": config[1]}
    m["layers"] = [{"digest": f"sha256:{d}", "size": s} for d, s in layers]
    return m


class TestRefParsing:
    async def test_official_image_gets_library_prefix(self):
        """docker.io 单段名 → library/ 命名空间（manifest 路径要它）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            # /v2/ challenge → token → manifest
            if request.url.path == "/v2/":
                return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://r/auth",service="r"'})
            if request.url.path == "/auth":
                return httpx.Response(200, json={"token": "tok"})
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers("python:3.12-slim", transport=_mock_transport(handler))
        assert dict(blobs) == {"aaaa": 100, "bbbb": 50, "cccc": 20}
        assert "/v2/library/python/manifests/3.12-slim" in seen["url"]

    async def test_tag_and_digest_strips_tag(self):
        """repo:tag@digest 形态：tag 不进 manifest 路径（否则 registry 404）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers("ghcr.io/org/app:v2@sha256:abcd", transport=_mock_transport(handler))
        assert blobs is not None
        assert "/v2/org/app/manifests/sha256:abcd" in seen["url"]
        assert ":v2" not in seen["url"].split("/manifests/")[0]

    async def test_explicit_registry_no_prefix(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers("ghcr.io/org/app:v2", transport=_mock_transport(handler))
        assert blobs is not None
        assert seen["host"] == "ghcr.io"


class TestV2Flow:
    async def test_index_picks_linux_arch(self):
        """多架构 index → 选 linux/{arch} 子 manifest 再取一次；attestation 按 index
        条目自带 size 计入（不逐个取体——多架构可能有 8+ 个会拖过 deadline）。"""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/v2/":
                return httpx.Response(
                    401,
                    headers={"WWW-Authenticate": 'Bearer realm="https://r/auth/token",service="registry.docker.io"'},
                )
            if request.url.path == "/auth/token":
                return httpx.Response(200, json={"token": "tok"})
            if request.url.path.endswith("/manifests/3.12-slim"):
                return httpx.Response(
                    200,
                    json={
                        "manifests": [
                            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
                            # attestation（buildx provenance）：platform unknown，按条目 size 计
                            {
                                "digest": "sha256:att",
                                "size": 7,
                                "platform": {"os": "unknown", "architecture": "unknown"},
                            },
                        ]
                    },
                )
            if request.url.path.endswith("/manifests/sha256:amd"):
                return httpx.Response(200, json=_single_manifest(layers=(("l1", 100),), config=None))
            return httpx.Response(404)

        blobs = await probe_image_layers("python:3.12-slim", arch="amd64", transport=_mock_transport(handler))
        # 平台层 + attestation 条目 size 都计入；arm64 子 manifest 不取
        assert dict(blobs) == {"l1": 100, "att": 7}
        assert any(p.endswith("/manifests/sha256:amd") for p in calls)
        assert not any(p.endswith("/manifests/sha256:att") for p in calls)  # 不取 attestation 体

    async def test_no_challenge_anonymous_ok(self):
        """无 WWW-Authenticate 的直连 registry：不带 token 直接拿 manifest。"""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers("reg.local/app:1", transport=_mock_transport(handler))
        assert dict(blobs)["aaaa"] == 100

    async def test_mirrors_tried_in_order_then_official(self):
        """docker.io：加速站按序试，第一个成功即返回；全挂回落官方端点。"""
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.host)
            if request.url.host.startswith("m1"):
                return httpx.Response(500)
            if request.url.host == "m2.example":
                return httpx.Response(200, json=_single_manifest(layers=(("x", 500),), config=None))
            return httpx.Response(200, json=_single_manifest(layers=(("x", 999),), config=None))

        blobs = await probe_image_layers(
            "python:3.12", mirrors=["https://m1.example", "https://m2.example"], transport=_mock_transport(handler)
        )
        assert dict(blobs)["x"] == 500
        # 每站最多两跳（/v2/ challenge + manifest），成功即止、不探官方
        assert attempts == ["m1.example", "m1.example", "m2.example", "m2.example"]

        attempts.clear()
        blobs2 = await probe_image_layers(
            "python:3.12", mirrors=["https://m1.example"], transport=_mock_transport(handler)
        )
        assert dict(blobs2)["x"] == 999  # 官方端点兜底命中
        assert attempts[-1] == "registry-1.docker.io"

    async def test_realm_query_preserved(self):
        """realm 自带 query（如 ?service=x）不被 scope/service 参数整体替换。"""
        seen_qs: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/":
                return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://auth.lan/tok?a=b"'})
            if request.url.path == "/tok":
                seen_qs.update(dict(request.url.params))
                return httpx.Response(200, json={"token": "tok"})
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers("reg.lan/app:1", transport=_mock_transport(handler))
        assert blobs is not None
        assert seen_qs["a"] == "b"  # realm 原 query 保住了
        assert seen_qs["scope"] == "repository:app:pull"

    async def test_insecure_registry_uses_http(self):
        """daemon 标 insecure 的 registry → 明文 http 探测（与 daemon 拉取行为一致）。"""
        schemes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            schemes.append(request.url.scheme)
            return httpx.Response(200, json=_single_manifest())

        blobs = await probe_image_layers(
            "registry.lan:5000/app:1",
            insecure_registries={"registry.lan:5000"},
            transport=_mock_transport(handler),
        )
        assert blobs is not None
        assert schemes and all(s == "http" for s in schemes)


class TestNodeArch:
    def test_maps_machine_to_oci(self):
        # 本机 x86_64 → amd64；映射表对 aarch64 也成立
        assert node_arch() in ("amd64", "arm64")
        from llmpod.apps.node.deployers.image_size import _ARCH_MAP

        assert _ARCH_MAP["x86_64"] == "amd64"
        assert _ARCH_MAP["aarch64"] == "arm64"


class TestFailurePaths:
    async def test_manifest_404_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/":
                return httpx.Response(200)
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

        assert await probe_image_layers("reg.local/app:nope", transport=_mock_transport(handler)) is None

    async def test_garbage_manifest_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json", headers={"Content-Type": "application/json"})

        assert await probe_image_layers("reg.local/app:1", transport=_mock_transport(handler)) is None

    async def test_connection_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        assert await probe_image_layers("reg.local/app:1", transport=_mock_transport(handler)) is None

    async def test_index_without_matching_arch_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"manifests": [{"digest": "sha256:x", "platform": {"os": "linux", "architecture": "riscv64"}}]},
            )

        assert await probe_image_layers("reg.local/app:1", arch="amd64", transport=_mock_transport(handler)) is None
