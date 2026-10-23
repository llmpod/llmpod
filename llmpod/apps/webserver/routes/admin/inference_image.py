"""推理镜像目录 + 推理引擎列举路由。

catalog 数据在内存（``image_catalog``），零 DB；本端点按 engine/arch/engine_version/gpu_model/driver_version
过滤，返回可用镜像列表 + 额外参数（params）。供向导 Step1 选镜像 / Step3 显示推荐参数用。
引擎列表端点遍历 adapter registry，返回各引擎能力（支持架构/格式/模型类别/是否可部署/推荐参数）；
带 ``model_id`` 时额外按该模型判定支持性（与部署预检同源），供前端 Step1 把不支持的引擎置灰。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines
from llmpod.apps.manager.deployment.feasibility import EngineAssessment, assess_engine_support
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.models.model import Model
from llmpod.permission.permissions import INFERENCE_IMAGE_READ
from llmpod.apps.webserver.auth.deps import require_permission

log = logging.getLogger("llmpod.apps.webserver.routes.admin.inference_image")

router = APIRouter()


class ImageEntryOut(BaseModel):
    """镜像目录条目（手写 pydantic：catalog 非 ORM）。"""

    id: str = Field(description="镜像条目 ID")
    engine: str
    arch: str
    gpu_model: list[str] = Field(default_factory=list, description="GPU 型号 glob（fnmatch Gpu.name）")
    driver: str | None = Field(None, description="驱动版本区间，如 >=570 / >=530,<570")
    engine_version: str | None = Field(None, description="引擎版本")
    image: str = Field(description="镜像地址")
    default: bool = False
    params: dict = Field(default_factory=dict, description="额外启动参数，包含 args 和 env")
    notes: str = ""


class EngineOut(BaseModel):
    """推理引擎能力（遍历 adapter registry 合成）。"""

    engine: str = Field(description="引擎唯一标识")
    display_name: str
    supported_formats: list[str] = Field(default_factory=list, description="权重格式：safetensors / gguf")
    supported_gpu_archs: list[str] = Field(default_factory=list, description="软件栈：nvidia / cann / cpu")
    supported_categories: list[str] = Field(default_factory=list, description="模型类别：llm / embedding / ...")
    supported_parallel: list[str] = Field(default_factory=list, description="并行：tp / pp / ep")
    supports_multinode: bool = Field(True, description="是否支持多机部署")
    deployable: bool = Field(True, description="是否可用于部署")
    protected_flags: list[str] = Field(
        default_factory=list, description="系统保留的启动参数，extra_args 中的同名参数会被忽略"
    )
    param_suggestions: list[dict] = Field(
        default_factory=list, description="推荐的部署参数，包含 name、hint、default 和 choices"
    )
    kv_cache_dtype_options: list[dict] = Field(
        default_factory=list,
        description="KV cache 精度选项；value 为空表示默认精度，空列表表示不可设置",
    )
    supports_model: bool | None = Field(
        None,
        description="是否支持指定模型；未提供 model_id 或无法判定时为 null",
    )
    unsupported_reason: str | None = Field(None, description="模型不受支持的原因")


@router.get(
    "",
    response_model=Result[list[ImageEntryOut]],
    summary="列举推理镜像目录",
    dependencies=[Depends(require_permission(INFERENCE_IMAGE_READ))],
)
async def list_inference_images(
    engine: str | None = Query(None, description="引擎：vllm/sglang/llamacpp"),
    arch: str | None = Query(None, description="架构：nvidia/cann/..."),
    engine_version: str | None = Query(None, description="引擎版本（镜像 tag 维度）"),
    gpu_model: str | None = Query(None, description="目标 GPU 型号（fnmatch 过滤）"),
    driver_version: str | None = Query(None, description="目标驱动版本（区间匹配）"),
):
    """列出可用推理镜像 + 额外参数（catalog 内存，零 DB）。"""
    entries = image_catalog.list_entries(
        engine=engine,
        arch=arch,
        engine_version=engine_version,
        gpu_model=gpu_model,
        driver_version=driver_version,
    )
    return success([ImageEntryOut(**e.model_dump()) for e in entries])


@router.get(
    "/engines",
    response_model=Result[list[EngineOut]],
    summary="列举推理引擎",
    dependencies=[Depends(require_permission(INFERENCE_IMAGE_READ))],
)
async def list_engines(
    model_id: int | None = Query(None, description="指定模型 ID 后，同时返回引擎是否支持该模型"),
    image_tag: str | None = Query(None, description="镜像版本，用于查询对应的并行与多机能力"),
    db: AsyncSession = Depends(get_db),
):
    """列出全部注册推理引擎及其能力（adapter registry 内存；``model_id`` 非空时读一次模型）。

    供前端 Step1 引擎选择：``deployable=False`` 的引擎不可选；带 ``model_id`` 时额外返回
    ``supports_model`` / ``unsupported_reason``（与部署预检同源 :func:`assess_engine_support`），
    前端据此把不支持的引擎置灰，避免到 Step2/预检才报错。``image_tag`` 给出时
    ``supported_parallel`` / ``supports_multinode`` 按该版本词表折算（与 inspect 回显同源，
    如旧 vLLM 镜像多机 false、SGLang 0.4.2 无 pp）。
    """
    model = await db.get(Model, model_id) if model_id is not None else None
    verdicts: dict[str, EngineAssessment] = (
        {a.engine: a for a in assess_engine_support(model.model_type, model.format)} if model is not None else {}
    )
    out: list[EngineOut] = []
    for name in registered_engines():
        try:
            a = get_adapter(name)
            verdict = verdicts.get(name)
            out.append(
                EngineOut(
                    engine=name,
                    display_name=name.capitalize(),
                    supported_formats=list(a.supported_formats),
                    supported_gpu_archs=list(a.supported_gpu_archs),
                    supported_categories=list(a.supported_categories),
                    supported_parallel=a.supported_parallel_at(image_tag),
                    supports_multinode=a.supports_multinode_at(image_tag),
                    deployable=a.deployable,
                    protected_flags=list(a.protected_flags),
                    param_suggestions=[p.model_dump() for p in a.param_suggestions],
                    kv_cache_dtype_options=[o.model_dump() for o in a.kv_cache_dtype_options],
                    supports_model=None if verdict is None else verdict.supported,
                    # 原因直接取预检同源的判定结果，避免另写一套「哪一维不匹配」的解释逻辑
                    unsupported_reason=(
                        ";".join(verdict.reasons) if verdict is not None and verdict.supported is False else None
                    ),
                )
            )
        except Exception:
            # 单引擎装配失败不拖垮整个列表，但留日志可追查（静默 continue 会让缺项无从发现）
            log.exception("engine list 组装失败（已跳过）engine=%s", name)
            continue
    return success(out)
