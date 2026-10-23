"""通用切片上传路由（/admin/uploads/*，与模型解耦）。

5 个端点（仅 GET/POST）：
- POST /uploads/declare         建会话（通用预校验 + 磁盘预检 + 写 Redis）
- POST /uploads/{sid}/part      传分片（sha 校验 + 原子落盘 + 幂等）
- GET  /uploads/{sid}           查会话状态（断点续传依据；sid 不可猜，GET 不加细权限）
- POST /uploads/{sid}/finalize  合并（失败即弃；只返回文件清单，不 inspect）
- POST /uploads/{sid}/abort     取消（删盘 + 删 Redis + 留取消标记；幂等）
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path as PathParam
from pydantic import BaseModel, Field

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.schemas.response import Result, success
from llmpod.apps.webserver.services import upload as upload_service
from llmpod.apps.webserver.services.upload import (
    AbortResponse,
    DeclareResponse,
    FinalizeResponse,
    PartResponse,
    SessionStatusResponse,
)
from llmpod.permission.permissions import UPLOAD_WRITE

log = logging.getLogger("llmpod.apps.webserver.routes.admin.uploads")


# ============================================================
# router 专用请求模型（仅 router 消费请求体，不进 schemas）
# ============================================================


class DeclareFileSpec(BaseModel):
    """单个文件在会话中的声明：目标名 + 总大小 + 分片数 + 单分片大小。

    分片 sha **不在 declare 声明**——上传时每个分片自带 sha（``/part`` 的
    ``sha256`` 表单字段），服务端重算比对。故大文件无需先全量算 hash 即可开传。
    """

    name: str = Field(..., description="目标文件名（合并后的最终名，含扩展名）")
    size: int = Field(..., ge=0, description="文件总大小（字节）")
    total_parts: int = Field(..., ge=1, description="分片总数")
    part_size: int = Field(..., ge=1, description="单分片大小（字节，末片可小于）")


class DeclareRequest(BaseModel):
    """建会话请求：name + 多文件声明（分片数/大小，不含 sha——sha 上传时随分片带）。"""

    name: str = Field(..., description="staged 目录名（清洗后用作磁盘子目录，防穿越）")
    files: list[DeclareFileSpec] = Field(..., min_length=1, description="本会话要上传的文件清单")


# ============================================================
# router
# ============================================================

# 本 router 顶层挂 UPLOAD_WRITE 内层权限校验。GET /{sid} 亦经此门，但 ``require_permission``
# 对平台登录用户（含 superuser 短路）放行，断点续传查询不受限——sid 不可猜，
# 平台角色外层门禁（admin router 顶层的 require_platform_role）已挡未授权访问。
router = APIRouter(
    dependencies=[Depends(require_permission(UPLOAD_WRITE))],
)


@router.post(
    "/declare",
    response_model=Result[DeclareResponse],
    summary="建上传会话（通用切片上传：预校验 + 磁盘预检）",
)
async def declare_upload(request: DeclareRequest):
    """建会话：通用预校验（文件名/单分片/总大小）+ 磁盘预检
    （剩余 < 2.2×总大小 → ``disk_full``）+ 写 Redis 会话 + 建空 staged 目录。

    无模型语义。返回 sid + staged_path + 各文件 file_id，后续 part 用。
    """
    files_spec = [
        {
            "name": f.name,
            "size": f.size,
            "total_parts": f.total_parts,
            "part_size": f.part_size,
        }
        for f in request.files
    ]
    resp = await upload_service.declare_session(name=request.name, files_spec=files_spec)
    return success(resp)


@router.post(
    "/{sid}/part",
    response_model=Result[PartResponse],
    summary="传分片（sha 校验 + 原子落盘 + 幂等）",
)
async def upload_part(
    sid: Annotated[str, PathParam(description="会话 ID（declare 返回）")],
    file_id: Annotated[str, Form(description="文件索引（declare 返回的 file_id，如 f0）")],
    part_no: Annotated[int, Form(description="分片序号（从 1 起）")],
    sha256: Annotated[str, Form(description="该分片 sha256（十六进制，须与 declare 声明一致）")],
    file: Annotated[bytes, File(description="分片二进制内容")],
):
    """收一个分片：取消标记检查 → sha 校验 → 原子落盘 ``.tmp→rename`` → SADD received。

    - sha 不符 → ``part_sha_mismatch``（409）；幂等：已收直接 ack 不重写。
    - 会话 finalizing/aborted → 拒新分片（``session_finalizing`` / ``session_aborted``）。
    """
    resp = await upload_service.receive_part(
        sid=sid,
        file_id=file_id,
        part_no=part_no,
        sha256=sha256,
        body=file,
    )
    return success(resp)


@router.get(
    "/{sid}",
    response_model=Result[SessionStatusResponse],
    summary="查会话状态（断点续传依据）",
)
async def get_upload_status(sid: Annotated[str, PathParam(description="会话 ID")]):
    """查会话状态：各文件 received/missing parts。断点续传时前端据此只补传缺失片。

    sid 不可猜，GET 经平台角色外层门禁 + UPLOAD_WRITE 内层权限校验（登录平台用户放行）。
    Redis 丢则降级读 ``.session.json`` 重建只读状态（标 failed，提示重传）。
    """
    resp = await upload_service.get_session_status(sid=sid)
    return success(resp)


@router.post(
    "/{sid}/finalize",
    response_model=Result[FinalizeResponse],
    summary="合并分片（失败即弃）",
)
async def finalize_upload(sid: Annotated[str, PathParam(description="会话 ID")]):
    """合并所有分片为目标文件 + 删分片（**异步**：校验后立即返回，后台合并）。

    - 全文件 complete 才合并；有未收齐 → ``session_not_finalizable``（409，同步）。
    - 校验通过 → 立即返回 ``status=finalizing``；合并后台跑，**前端轮询**
      ``GET /{sid}`` 等 ``completed``/``failed``。
    - ``completed`` 后再调一次（幂等）拿文件清单 + staged_path；``failed`` 再调 →
      ``finalize_failed``（500，整会话作废，需重新 declare 全传）。
    - 只返回文件清单 + staged_path，**不 inspect**（inspect 是模型层职责）。
    """
    resp = await upload_service.finalize_session(sid=sid)
    return success(resp)


@router.post(
    "/{sid}/abort",
    response_model=Result[AbortResponse],
    summary="取消会话（删盘 + 删 Redis + 留取消标记；幂等）",
)
async def abort_upload(sid: Annotated[str, PathParam(description="会话 ID")]):
    """取消会话：删 staged 目录 + 删 Redis + 写短 TTL 取消标记（拦截迟到的分片）。幂等。

    仅 pending 可 abort；finalizing/completed 拒绝（``session_finalizing`` /
    ``session_not_finalizable``）。
    """
    resp = await upload_service.abort_session(sid=sid)
    return success(resp)


__all__ = ["router"]
