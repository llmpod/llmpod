"""模型管理路由（Model CRUD + /inspect 按 source 探测元数据）。

部署生命周期与部署日志路由见 :mod:`routes.admin.deployment`（统一聚合，不再散落此处）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Path as PathParam, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.feasibility import FeasibilityReport, assess_feasibility
from llmpod.apps.manager.model_source import ModelFiles, get_inspector
from llmpod.apps.manager.model_source.content_hash import compute_content_hash
from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import ModelOut
from llmpod.config.enums import ModelSource, ModelType
from llmpod.config.settings import settings
from llmpod.core.exceptions import Conflict, LLMPodValidationError, NotFound
from llmpod.db import get_db
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.core.secrets import encrypt as _encrypt_source_auth
from llmpod.utils.safe_path import safe_resolve
from llmpod.permission.permissions import (
    MODEL_CREATE,
    MODEL_READ,
    MODEL_UPDATE,
    MODEL_DELETE,
)
from llmpod.services.audit_service import audit_action

log = logging.getLogger("llmpod.apps.webserver.routes.admin.model")

model_router = APIRouter()


# ============================================================
# Model（元数据 + 权重信息合一，不再拆 ModelVersion）
# ============================================================


class InspectModelRequest(BaseModel):
    source: str = Field(..., description="来源：huggingface | oss | local")
    source_url: str = Field(..., description="来源地址：Hugging Face 仓库、对象存储文件地址或管理端本地路径")
    revision: str | None = Field(None, description="版本/commit-hash（HF 可选，默认 main）")
    context_size: int | None = Field(
        None, description="最大上下文长度（Token）；不填时不计入 KV cache 需求，估算可能偏低"
    )
    cluster_id: int | None = Field(None, description="集群 ID：传入则只评估该集群（模拟部署预检）；不传则全局扫描")
    # —— 用户覆盖（覆盖优先于探测值，用于纠正自动识别错误 / 补 OSS、local 探不到的类别）——
    # 不传（None）则探测值兜底；覆盖同时反映在回显 files 与 feasibility 入参。仅本次预览，不落库。
    size_gb: float | None = Field(
        None,
        description="权重大小（GB）；不填时使用探测结果，探测不到时可在此指定",
    )
    model_type: str | None = Field(
        None,
        description="模型类别：llm、embedding、rerank、image、stt、tts 或 multimodal；不填时使用探测结果",
    )
    model_format: str | None = Field(
        None,
        description="模型格式，如 safetensors、gguf、pytorch 或 onnx；不填时使用探测结果",
    )


class CreateModelRequest(BaseModel):
    name: str = Field(..., description="模型显示名")
    source: str = Field(..., description="来源：huggingface | oss | local（上传落地的模型标 local）")
    source_url: str = Field(..., description="来源地址（HF repo id/URL / OSS 直链 / manager 本地路径）")
    revision: str | None = Field(None, description="版本/commit-hash")
    source_auth: str | None = Field(None, description="模型来源凭证，如 Hugging Face Token；加密保存且不会在响应中返回")
    model_type: str = Field(
        "llm",
        description="模型类别：llm、embedding、rerank、image、stt、tts 或 multimodal",
    )
    files: ModelFiles = Field(
        default_factory=ModelFiles,
        description="模型源探测结果；不填时只保存基础信息",
    )


class UploadResult(BaseModel):
    path: str = Field(..., description="上传暂存目录，可用作本地模型的来源地址")
    files: list[str] = Field(default_factory=list, description="已落地的文件名清单")


class InspectResponse(BaseModel):
    """inspect 响应：文件识别 + 可部署性检测（可选）。"""

    files: ModelFiles = Field(..., description="模型源识别结果（文件事实 + 身份标记 + suggested_type）")
    feasibility: FeasibilityReport | None = Field(
        None, description="可部署性检测（?feasibility=true 时附；best-effort，失败为 None）"
    )


class ValidateManifestRequest(BaseModel):
    """validate-manifest 请求：纯文件清单（名 + 大小，文件还不在盘上）。

    复用 ``apps/manager/model_source/manifest`` 的纯函数做上传前预校验，
    与上传层解耦（上传层零模型知识），模型语义预检由前端编排三步调用。
    """

    files: list["ValidateManifestFile"] = Field(..., min_length=1, description="待上传文件声明清单")


class ValidateManifestFile(BaseModel):
    """声明清单中的一个文件（名 + 大小）。"""

    name: str = Field(..., description="文件名（含扩展名）")
    size: int = Field(..., description="文件大小（字节）")


class ValidateManifestWarning(BaseModel):
    """一条预校验提示。"""

    code: str = Field(..., description="提示码（见规则表）")
    message: str = Field(..., description="可读说明，前端直接展示")
    severity: str = Field(..., description="fatal（应阻止继续）| warn（提示可继续）| info")
    file: str | None = Field(default=None, description="关联文件名，无具体文件为 null")


class ValidateManifestResponse(BaseModel):
    """validate-manifest 响应：warnings 清单（按规则优先级排序）。"""

    warnings: list[ValidateManifestWarning] = Field(default_factory=list, description="预校验提示清单")


@model_router.post(
    "/validate-manifest",
    response_model=Result[ValidateManifestResponse],
    summary="上传前预校验清单（给用户提示，防传几十 GB 后才发现用不了）",
    dependencies=[Depends(require_permission(MODEL_CREATE))],
)
async def validate_model_manifest(request: ValidateManifestRequest):
    """纯逻辑预校验（零 IO，文件还不在盘上）：复用 ``manifest.validate_manifest``
    的 8 条规则，返回 ``warnings``（``fatal`` 应阻止继续 / ``warn`` 提示可继续 / ``info``）。

    与上传层解耦：不调 declare，不写 Redis。前端选完文件 → 调本端点拿提示
    → 用户确认后 → declare → part ×N → finalize → inspect。
    """
    from llmpod.apps.manager.model_source.manifest import ManifestFile, validate_manifest

    files = [ManifestFile(name=f.name, size=f.size) for f in request.files]
    warnings = validate_manifest(files, settings.upload_max_bytes)
    return success(
        ValidateManifestResponse(
            warnings=[
                ValidateManifestWarning(
                    code=w.code,
                    message=w.message,
                    severity=w.severity,
                    file=w.file,
                )
                for w in warnings
            ]
        )
    )


class UpdateModelRequest(BaseModel):
    """更新模型（全可选 PATCH 语义）：覆盖探测结果 / 补全单文件缺字段。

    nullable 列（architecture/format/quantization/hash/source/source_url/source_subpath）
    显式传 null 视为清除该字段；NOT NULL 列（name/model_type/is_moe/is_vision/
    source_kind/hash_mode）传 null 会被忽略（handler 不写入，避免 IntegrityError→500）。
    """

    name: str | None = Field(None, description="模型显示名；不传则不修改")
    model_type: str | None = Field(
        None, description="类别：llm|embedding|rerank|image|stt|tts|multimodal；不传则不修改"
    )
    architecture: str | None = Field(None, description="模型架构；不传则不修改")
    is_moe: bool | None = Field(None, description="是否 MoE；不传则不修改")
    is_vision: bool | None = Field(None, description="是否视觉模型；不传则不修改")
    size_gb: float | None = Field(None, description="权重大小（GB）；不传则不修改")
    format: str | None = Field(None, description="格式：safetensors|gguf|pytorch；不传则不修改")
    quantization: str | None = Field(None, description="量化方案：awq|gptq|q4_k_m|none；不传则不修改")
    num_layers: int | None = Field(None, description="层数（KV 估算与逐层 offload 用）；不传则不修改")
    kv_layers: int | None = Field(None, description="有 KV cache 的层数（混合线性注意力模型少于层数）；不传则不修改")
    num_attention_heads: int | None = Field(None, description="attention head 数（TP 整除校验用）；不传则不修改")
    num_kv_heads: int | None = Field(None, description="KV head 数；不传则不修改")
    head_dim: int | None = Field(None, description="每 head 维度；不传则不修改")
    max_context: int | None = Field(
        None, gt=0, description="模型自带上下文上限（正整数；0/负数会让推荐上下文归零）；不传则不修改"
    )
    needs_trust_remote_code: bool | None = Field(
        None, description="是否带自定义代码（需 --trust-remote-code）；不传则不修改"
    )
    hash: str | None = Field(None, description="权重哈希；不传则不修改")
    source: str | None = Field(None, description="来源；不传则不修改")
    source_url: str | None = Field(None, description="来源地址；不传则不修改")
    source_kind: str | None = Field(
        None, description="盘上布局：unknown|single_gguf|gguf_shards|transformers_folder|diffusers_folder；不传则不修改"
    )
    source_subpath: str | None = Field(None, description="多 variant 仓选变体子路径；不传则不修改")
    mmproj_file: str | None = Field(
        None, description="视觉投影器文件（repo 相对路径，如 mmproj-F16.gguf）；不传则不修改"
    )
    revision: str | None = Field(None, description="版本/commit-hash；不传则不修改")
    hash_mode: str | None = Field(None, description="hash 释读方式：none|revision|etag|sha256；不传则不修改")
    source_auth: str | None = Field(
        None, description="来源凭证明文（如 HF token）；传字符串=设置（加密入库），传 null=清除，不传=不改"
    )


class ModelDeleteData(BaseModel):
    """删除模型回执（连带清盘仅限平台生成的目录，远程仅删库）。"""

    model_id: int = Field(..., description="模型ID")
    deleted: bool = Field(True, description="删除是否生效")
    purged_dir: str | None = Field(None, description="连带清理的盘上目录（未清理为 None）")
    purged_dirs: list[str] = Field(
        default_factory=list, description="连带清理的全部盘上目录（canonical + upload；purged_dir 为首个，兼容旧字段）"
    )


# Model 上 NOT NULL 的列（有 server_default）：传 null 会触发 IntegrityError，故禁清空。
_NOT_NULL_COLUMNS = frozenset(
    {"name", "model_type", "is_moe", "is_vision", "needs_trust_remote_code", "source_kind", "hash_mode"}
)


@model_router.post(
    "/inspect",
    response_model=Result[InspectResponse],
    summary="探测模型源（回显元数据 + 可部署性检测）",
    dependencies=[Depends(require_permission(MODEL_CREATE))],
)
async def inspect_model_source(
    request: InspectModelRequest,
    db: AsyncSession = Depends(get_db),
):
    """按 source 探测模型源（抓文件清单 / config，**不下载权重**）。

    - 返回**识别结果**（文件结构/格式/身份原样标记）+ ``suggested_type``（来自 HF pipeline_tag，映射不了为 None）；
    - 探测失败：``success=false`` + ``message``（软失败，前端可提示后人工填），**不抛异常**；
    - 未知 source（拼写错）：``LLMPodValidationError``（422）；
    - 用户覆盖（``size_gb`` / ``model_type`` / ``model_format``）优先于探测值：覆盖后重算可部署性，
      且回显 ``files`` 反映覆盖值（保证前端正确显示）。仅本次预览生效，**不落库**（落库走 ``/models`` 或 update）。
    """
    inspector = get_inspector(request.source)  # 未知 source → 422
    files = await inspector.inspect(
        source_url=request.source_url,
        revision=request.revision,
    )

    # 用户覆盖优先于探测值（覆盖即「以用户值为准」），None 则探测兜底；覆盖同时
    # 反映在回显 files（前端正确显示）与 feasibility 入参（重算可部署性）。
    eff_size = request.size_gb if request.size_gb is not None else files.size_gb
    eff_type = request.model_type if request.model_type is not None else files.suggested_type
    eff_format = request.model_format if request.model_format is not None else files.format
    files = files.model_copy(update={"size_gb": eff_size, "suggested_type": eff_type, "format": eff_format})

    report: FeasibilityReport | None = None
    if files.success:
        try:
            report = await assess_feasibility(
                db,
                model_size_gb=eff_size,
                model_type=eff_type,
                model_format=eff_format,
                cluster_id=request.cluster_id,
                context_size=request.context_size,
            )
        except Exception as e:  # 可行性检测不得阻塞探测
            log.warning("inspect feasibility failed for %s: %s", request.source_url, e)
    return success(InspectResponse(files=files, feasibility=report))


def _sanitize_name(name: str | None) -> str:
    """清洗上传目录名：仅留字母/数字/._-，否则丢弃；空 → uuid。"""
    raw = (name or "").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-") if raw else ""
    return safe or uuid.uuid4().hex[:12]


@model_router.post(
    "/upload",
    response_model=Result[UploadResult],
    summary="通用文件上传（落地 manager 盘，返回路径供 source=local 用）",
    dependencies=[Depends(require_permission(MODEL_CREATE))],
)
async def upload_model_files(
    files: Annotated[
        list[UploadFile], File(default_factory=list, description="模型文件（config.json / 权重等，可多选）")
    ],
    name: Annotated[str | None, Query(description="staged 目录名，缺省随机；用作 source=local 的子目录")] = None,
):
    """通用文件上传：流式写盘到 ``model_store_dir/upload/<name>/<filename>``。

    不绑模型语义、不写库、不探测。前端拿返回的 ``path`` 作为
    ``source=local`` 的 ``source_url``，再调 ``/inspect`` 预填、``/models`` 建模。
    """
    if not files:
        raise LLMPodValidationError("未提供任何文件")

    base = Path(settings.model_store_dir).expanduser()
    # 每个上传落独立目录（name 只作可读前缀 + 随机后缀）：绝不覆盖已有上传/模型文件，
    # 防「同名 upload 覆盖旧权重」串模型。
    staged = base / "upload" / f"{_sanitize_name(name)}-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(staged.mkdir, parents=True, exist_ok=True)

    landed: list[str] = []
    for up in files:
        # 文件名清洗：仅取末段，去路径穿越（os.path.basename 去目录成分，or 兜底随机名）
        fname = os.path.basename(up.filename or "") or uuid.uuid4().hex[:8]
        dest = staged / fname

        # 流式写盘：1MB chunk 在事件循环里 await 读，同步写（1MB 写阻塞可忽略）
        with open(dest, "wb") as f:
            while True:
                chunk = await up.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        await up.close()
        landed.append(fname)

    return success(UploadResult(path=str(staged.resolve()), files=landed))


async def _manager_content_hash(source: str | None, source_url: str | None) -> str | None:
    """local/manager 源内容指纹（采样哈希：清单 + 最大权重文件）；其余源或无路径返回 None。

    路径必须落在 model_store_dir 之内（与 LocalInspector.inspect 同一约束）——
    建模/更新也能触发对 source_url 的读盘，不做根校验则探测侧的收敛被这里旁路。
    越界返回 None（不落指纹），不报错。
    """
    if not (ModelSource.is_manager_source(source) and source_url):
        return None
    from llmpod.utils.safe_path import safe_resolve

    store_root = Path(settings.model_store_dir).expanduser().resolve()
    resolved = safe_resolve(store_root, source_url)
    if resolved is None:
        return None
    return await asyncio.to_thread(compute_content_hash, resolved)


@model_router.post(
    "", response_model=Result[ModelOut], summary="创建模型", dependencies=[Depends(require_permission(MODEL_CREATE))]
)
@audit_action(action="create", resource="model")
async def create_model(
    request: CreateModelRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建模型。type 来自用户选择（前端预填 /inspect 的 suggested_type）；元数据取自回传 ``files``。"""
    files = request.files

    # local 源内容指纹：采样哈希（最大权重文件）填 Model.hash，让目录身份反映内容，
    # 重传改了权重→换目录→node 重下；没改→不重下。
    content_digest = await _manager_content_hash(request.source, request.source_url)

    m = Model(
        name=request.name,
        source=request.source,
        source_url=request.source_url,
        revision=files.revision or request.revision,
        model_type=request.model_type,
        architecture=files.architecture,
        is_moe=files.is_moe,
        # is_vision 由声明 type 推导（不再从 config 猜）：视觉类别判定走 ModelType 唯一定义处
        is_vision=ModelType.is_vision(request.model_type),
        size_gb=files.size_gb,
        format=files.format,
        quantization=files.quantization,
        # 架构形状：KV cache 真公式、逐层 offload、TP 整除校验的输入（探测不到为 None）
        num_layers=files.num_layers,
        kv_layers=files.kv_layers,
        num_attention_heads=files.num_attention_heads,
        num_kv_heads=files.num_kv_heads,
        head_dim=files.head_dim,
        max_context=files.max_context,
        needs_trust_remote_code=files.needs_trust_remote_code,
        # source_kind 由 inspector 推导（detect_source_kind）；未推导(None) → 'unknown'（NOT NULL，不能传 None）
        source_kind=files.source_kind or "unknown",
        # source_subpath：GGUF 多变体 repo 的变体路径，下发供 node allow_patterns 收窄 + verify 定位
        source_subpath=files.source_subpath,
        # 视觉投影器（GGUF 多模态）：随主模型下发 + 部署期注入 --mmproj
        mmproj_file=files.mmproj_file,
        # 内容指纹（local 源采样哈希，其余源通常为 None）；进 recipe_digest 做内容寻址
        hash=content_digest,
        hash_mode="fingerprint" if content_digest else "none",
        # source_auth：应用层加密入库（HF token 等）；API 永不回显明文（ModelOut exclude）
        source_auth_encrypted=_encrypt_source_auth(request.source_auth) if request.source_auth else None,
        # 模型分发状态不再存 Model（cached_nodes 已移除）——由 node_model 表在部署期表达
    )
    db.add(m)
    await db.flush()
    await db.refresh(m)
    await db.commit()
    return success(m)


@model_router.get(
    "",
    response_model=PaginatedResponse[ModelOut],
    summary="列出模型",
    dependencies=[Depends(require_permission(MODEL_READ))],
)
async def list_models(
    model_type: str | None = Query(None, description="按类别过滤：llm|embedding|rerank|image|stt|tts|multimodal"),
    page: int = Query(1, ge=1, description="页码（从 1 起）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Model)
    cnt = select(func.count(Model.id))
    if model_type:
        stmt = stmt.where(Model.model_type == model_type)
        cnt = cnt.where(Model.model_type == model_type)
    total = (await db.execute(cnt)).scalar() or 0
    stmt = stmt.order_by(Model.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = list((await db.execute(stmt)).scalars())
    return PaginatedResponse.result(items, total, page, page_size)


@model_router.get(
    "/{model_id}",
    response_model=Result[ModelOut],
    summary="获取模型详情",
    dependencies=[Depends(require_permission(MODEL_READ))],
)
async def get_model(
    model_id: Annotated[int, PathParam(description="模型ID")],
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(Model, model_id)
    if m is None:
        raise NotFound("Model", model_id)
    return success(m)


@model_router.post(
    "/{model_id}/update",
    response_model=Result[ModelOut],
    summary="更新模型",
    dependencies=[Depends(require_permission(MODEL_UPDATE))],
)
@audit_action(action="update", resource="model", resource_id="model_id")
async def update_model(
    model_id: Annotated[int, PathParam(description="模型ID")],
    request: UpdateModelRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新模型（人工修正探测结果 / 补全单文件缺字段，PATCH 语义：仅传字段生效）。

    用 ``exclude_unset``：显式传 ``null`` 对 nullable 列视为「清除该字段」，
    与「未传字段不修改」区分。NOT NULL 列（``_NOT_NULL_COLUMNS``）传 null 会被跳过
    （避免 IntegrityError→500）。
    """
    updates = request.model_dump(exclude_unset=True)
    if not updates:
        raise LLMPodValidationError("未提供任何待更新字段")
    m = await db.get(Model, model_id)
    if m is None:
        raise NotFound("Model", model_id)
    # 逐字段显式赋值：NOT NULL 列（_NOT_NULL_COLUMNS）显式传 null 跳过保持原值
    # （写入会 IntegrityError→500）；source_auth 走加密：明文→加密入库，null→清除，
    # 不传→不改（exclude_unset 已挡）。新字段加这里，不走键名循环。
    if "name" in updates and updates["name"] is not None:
        m.name = updates["name"]
    if "model_type" in updates and updates["model_type"] is not None:
        m.model_type = updates["model_type"]
    if "architecture" in updates:
        m.architecture = updates["architecture"]
    if "is_moe" in updates and updates["is_moe"] is not None:
        m.is_moe = updates["is_moe"]
    if "is_vision" in updates and updates["is_vision"] is not None:
        m.is_vision = updates["is_vision"]
    if "size_gb" in updates:
        m.size_gb = updates["size_gb"]
    if "format" in updates:
        m.format = updates["format"]
    if "quantization" in updates:
        m.quantization = updates["quantization"]
    if "num_layers" in updates:
        m.num_layers = updates["num_layers"]
    if "kv_layers" in updates:
        m.kv_layers = updates["kv_layers"]
    if "num_attention_heads" in updates:
        m.num_attention_heads = updates["num_attention_heads"]
    if "num_kv_heads" in updates:
        m.num_kv_heads = updates["num_kv_heads"]
    if "head_dim" in updates:
        m.head_dim = updates["head_dim"]
    if "max_context" in updates:
        m.max_context = updates["max_context"]
    if "needs_trust_remote_code" in updates and updates["needs_trust_remote_code"] is not None:
        m.needs_trust_remote_code = updates["needs_trust_remote_code"]
    if "hash" in updates:
        m.hash = updates["hash"]
    if "source" in updates:
        m.source = updates["source"]
    if "source_url" in updates:
        m.source_url = updates["source_url"]
    if "source_kind" in updates and updates["source_kind"] is not None:
        m.source_kind = updates["source_kind"]
    if "source_subpath" in updates:
        m.source_subpath = updates["source_subpath"]
    if "mmproj_file" in updates:
        m.mmproj_file = updates["mmproj_file"]
    if "revision" in updates:
        m.revision = updates["revision"]
    if "hash_mode" in updates and updates["hash_mode"] is not None:
        m.hash_mode = updates["hash_mode"]
    if "source_auth" in updates:
        value = updates["source_auth"]
        m.source_auth_encrypted = _encrypt_source_auth(value) if value else None
    # 若改了 model_type，重推导 is_vision（与 create_model 口径一致），除非调用方显式传了 is_vision
    if "model_type" in updates and "is_vision" not in updates:
        m.is_vision = ModelType.is_vision(updates["model_type"])
    # local 源：按当前盘上内容刷新内容指纹（覆盖重传后 update 一次即重算，目录随之换）
    digest = await _manager_content_hash(m.source, m.source_url)
    if digest:
        m.hash = digest
        m.hash_mode = "fingerprint"
    await db.flush()
    await db.refresh(m)
    await db.commit()
    return success(m)


@model_router.post(
    "/{model_id}/delete",
    response_model=Result[ModelDeleteData],
    summary="删除模型（local 同步删 manager 本地目录；远程仅删库）",
    dependencies=[Depends(require_permission(MODEL_DELETE))],
)
@audit_action(action="delete", resource="model", resource_id="model_id")
async def delete_model(
    model_id: Annotated[int, PathParam(description="模型ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除模型：先落定 DB 删除，再 best-effort 删盘（盘失败仅留无害孤儿目录，不致「库删权重还在」的错配）。

    级联删盘只动**平台生成的目录**，用户手工预置的目录绝不级联删：

    - canonical 下载目录（``{model_store_dir}/{slug}-{digest}``，配方哈希命名，平台生成）：
      恒 best-effort 清理（单机部署 node 与 manager 同盘，不清会残留孤儿）；
    - ``source_url`` 目录：仅当它是平台上架的 upload 产物（位于 ``{store}/upload/`` 之下）
      才删——用户把模型手工放进 store 的目录（source_url 直指它）只删库行，整目录保留
      （此前无差别 rmtree 误删过用户预置目录）；
    - 远程（huggingface/oss/...）或 source 为空：仅删 DB，不动盘（远程权重本不在 manager 盘）。
    """
    m = await db.get(Model, model_id)
    if m is None:
        raise NotFound("Model", model_id)

    # 有部署引用时拒绝删除——否则删掉配方后部署的 load_deployment 会 NotFound，无法再启动。
    # 用 select 行（非 func.count）：FakeSession 仅支持 select.where 等值过滤，count 会恒 0。
    deps = list((await db.execute(select(ModelDeployment).where(ModelDeployment.model_id == model_id))).scalars())
    if deps:
        raise Conflict(f"模型被 {len(deps)} 个部署引用，请先删除相关部署再删模型")

    source = (m.source or "").strip().lower()
    src_url = m.source_url
    purged_dirs: list[str] = []

    # 先落库删除（commit 落定）；盘删除放后面且 best-effort，避免库未删成却已毁盘的不可逆错配
    await db.delete(m)
    await db.commit()

    if ModelSource.is_manager_source(source) and src_url:
        from llmpod.apps.manager.deployment.placement import canonical_local_dir
        from llmpod.apps.webserver.services.upload import upload_root

        store_root = Path(settings.model_store_dir).expanduser().resolve()
        upload_root = upload_root().resolve()

        async def _purge(raw: Path | str) -> None:
            """best-effort 删一个平台生成目录（严格位于 store 之内、非根、存在才动）。

            rmtree 跳线程执行（大目录数十秒，事件循环内同步删会卡死整个 webserver）；
            删除成功才记入回执——静默吞错却报「已清理」会让运维误以为盘已释放。
            目标 resolve 后是文件（非目录）时按文件删，同样只有成功才入回执。
            """
            resolved = safe_resolve(store_root, raw)
            if resolved is None or resolved == store_root or not resolved.exists():
                return
            if os.path.islink(resolved):
                return  # 不穿透链接（与分发端点同口径）；``..`` 越界已由 safe_resolve 判 None
            if resolved == upload_root:
                return  # upload 根本身不删（source_url 直指根时整棵上传树会被端掉）
            try:
                if resolved.is_dir():
                    await asyncio.to_thread(shutil.rmtree, resolved)
                else:
                    await asyncio.to_thread(resolved.unlink)
            except OSError as e:
                log.warning("delete_model %s: 清理 %s 失败（留孤儿目录）: %s", model_id, resolved, e)
                return
            purged_dirs.append(str(resolved))

        # 1) canonical 目录：平台按配方哈希命名生成，恒属平台，清掉防孤儿残留。
        #    但目录按「名字+内容哈希」命名，可被多条 Model 行共享（同名同内容只落一目录）——
        #    还有别的行指向同一目录时跳过，只删本行（否则会把别行实例正挂载读取的权重删掉）。
        canonical = canonical_local_dir(m)
        siblings = list(
            (
                await db.execute(
                    select(Model).where(
                        Model.source == m.source,
                        Model.name == m.name,
                        Model.id != m.id,
                    )
                )
            ).scalars()
        )
        shared = any(canonical_local_dir(sib) == canonical for sib in siblings)
        if shared:
            log.info(
                "delete_model %s: canonical 目录 %s 被其他同名同内容行共享，保留目录仅删库行",
                model_id,
                canonical,
            )
        else:
            await _purge(canonical)
        # 2) source_url：仅平台上架的 upload 产物才级联删，用户预置目录保留
        #    （store_root 本身不在 upload/ 之下，is_relative_to 已蕴含非根判定）
        src = safe_resolve(store_root, src_url)
        if src is not None and src.is_relative_to(upload_root):
            await _purge(src)
        elif src is not None:
            log.info("delete_model %s: source_url %s 非平台 upload 目录，保留用户目录仅删库行", model_id, src_url)

    return success(
        ModelDeleteData(
            model_id=model_id,
            purged_dir=purged_dirs[0] if purged_dirs else None,
            purged_dirs=purged_dirs,
        )
    )
