"""NPS 满意度问卷路由：前端弹窗的提交入口。

前端只传 ``rate`` 评分与 ``comment`` 评语两项；机器标识（serverid）、
产品编号、版本号等固定字段由服务端补齐后转发收集端。挂平台粗门即可——
产品反馈是登录用户自己的表态，不做细粒度权限。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.services.nps_service import NpsFeedbackRequest, submit_feedback

router = APIRouter()


@router.post("", response_model=Result[None], summary="提交 NPS 满意度反馈")
async def submit_nps_feedback(
    request: NpsFeedbackRequest,
    db: AsyncSession = Depends(get_db),
):
    """转发用户评分与评语到问卷收集端；失败返回 400，前端弹窗保留可重试。"""
    await submit_feedback(db, request)
    return success(None)
