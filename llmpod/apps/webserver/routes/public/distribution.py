"""模型分发公开端点（Node 出站 HTTP 拉取 local 源模型，鉴权 = 短期 HMAC 下载令牌，非用户会话）。

Node 在 local 源下载任务里凭 Manager 下发的 ``download_token``（绑定 model_id）访问：
- ``GET /manifest``：取文件清单（路径 + size），作为本次下载的权威清单；
- ``GET /file``：按 Range 续传单个文件字节。

安全：令牌绑 model_id（跨模型挪用失败），文件路径经 ``safe_resolve`` 强制位于
``model_store_dir`` 之下，禁止符号链接越权。
"""

from __future__ import annotations

import logging
import os
import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from llmpod.apps.manager.model_source.download_manifest import Manifest, build_manifest
from llmpod.apps.webserver.utils.download_token import verify_download_token, download_signing_key
from llmpod.config.settings import settings
from llmpod.core.exceptions import AuthError, NotFound
from llmpod.db import get_db_context
from llmpod.models.model import Model
from llmpod.utils.safe_path import safe_resolve

log = logging.getLogger("llmpod.apps.webserver.routes.public.distribution")

router = APIRouter()

# manifest 进程内 TTL 缓存（模型文件不可变，按 model_id+revision 复用）
_manifest_cache: dict[tuple[int, str | None], tuple[Manifest, float]] = {}


def _verify(token: str, model_id: int) -> None:
    if not verify_download_token(download_signing_key(settings), token, model_id):
        raise AuthError("下载令牌无效或已过期")


async def _load_model(model_id: int) -> Model:
    async with get_db_context() as db:
        m = await db.get(Model, model_id)
    if m is None:
        raise NotFound("Model", model_id)
    return m


def _model_root(model: Model) -> Path:
    """解析 model.source_url 为位于 model_store_dir 之内的绝对路径（目录或单文件）。

    单文件 source_url（直接指向一个 .gguf 等）同样放行：manifest 生成单文件清单、
    file 端点只下发根文件本身，node 下载后按 single_gguf 口径校验在位——
    此前一律要求 ``is_dir()`` 会让单文件源的 manifest 404，node 重试耗尽部署必败。
    拒符号链接（文件服务不穿透链接）。
    """
    store_root = Path(settings.model_store_dir).expanduser().resolve()
    if not model.source_url:
        raise NotFound("Model source_url", model.id)
    root = safe_resolve(store_root, model.source_url)
    if root is None or not (root.is_dir() or root.is_file()) or os.path.islink(root):
        raise NotFound("Model files", model.source_url)
    return root


@router.get(
    "/manifest",
    response_model=Manifest,
    summary="获取模型文件清单（Node local 源下载用）",
)
async def get_manifest(
    model_id: int = Query(..., description="模型ID"),
    token: str = Query(..., description="短期 HMAC 下载令牌"),
):
    _verify(token, model_id)
    model = await _load_model(model_id)
    root = _model_root(model)

    # 单文件源（root.is_file）的清单生成也收在 build_manifest（收录规则单一归属）
    key = (model_id, model.revision)
    now = time.time()
    cached = _manifest_cache.get(key)
    if cached and now < cached[1]:
        return cached[0]

    manifest = await asyncio.to_thread(build_manifest, root, model_id, model.revision)
    _manifest_cache[key] = (manifest, now + settings.download_manifest_cache_ttl)
    return manifest


@router.get(
    "/file",
    response_class=FileResponse,
    summary="下载单个模型文件（支持 Range 续传）",
)
async def get_file(
    model_id: int = Query(..., description="模型ID"),
    path: str = Query(..., description="相对模型根的文件路径"),
    token: str = Query(..., description="短期 HMAC 下载令牌"),
):
    _verify(token, model_id)
    model = await _load_model(model_id)
    root = _model_root(model)

    if root.is_file():
        # 单文件源：唯一可下发的就是根文件本身（path 须等于其文件名）
        if path.strip("/") != root.name:
            raise NotFound("File", path)
        return FileResponse(str(root), filename=root.name)

    # path 必须解析到 root 之下且非 symlink（纵深防御）
    target = safe_resolve(root, root / path)
    if target is None or not target.is_file():
        raise NotFound("File", path)
    if os.path.islink(target):
        # safe_resolve 已跟随 symlink；界内 symlink 仍拒（文件服务不穿透链接）
        raise NotFound("File", path)

    return FileResponse(str(target), filename=target.name)
