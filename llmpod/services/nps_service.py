"""NPS 满意度问卷上报（宝塔问卷收集端 nps.bt.cn）。

用户在管理端弹窗打 1–5 星 + 选填一句话评语，本服务补齐固定字段后以表单
编码转发到收集端。前端只需要传 ``rate`` 和 ``comment`` 两项，其余字段全部
服务端写死::

    action=submit              固定动作
    serverid=<64 位小写 hex>   本机标识（收集端硬校验形状，不符即 400）
    product_type=<产品编号>    收集端分配给本产品的编号
    panel_version=<版本号>     当前部署版本
    rate=<1-5>                 用户评分（收集端不校验范围，量纲在本层收口）
    questions={"40": 评语}     自由文本题回答，键为题目编号，评语可空

serverid 与遥测 instance_id 的一致性：优先取宿主机器标识的全量
``sha256("llmpod:" + 机器标识)``——前 32 位即遥测 instance_id（同源同盐，
见 :func:`llmpod.services.telemetry_service.derive_machine_hash`），两套
统计按前缀即可对上同一台机器。宿主来源不可读（遥测走了随机兜底）或机器
标识已变（与遥测行所存身份对不上）时，改对遥测行已存的 instance_id 做同
盐 sha256——身份跟随遥测行走，遥测行在即稳定。
"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import llmpod
from llmpod.core.exceptions import LLMPodError
from llmpod.services.telemetry_service import derive_machine_hash, ensure_instance_state

log = logging.getLogger("llmpod.services.nps")

# 收集端端点与产品登记值（产品级固定值，随代码发版变更；不做部署配置——
# 与遥测端点同风格）
NPS_SUBMIT_URL = "https://nps.bt.cn/api/index.php"
NPS_PRODUCT_TYPE = "40"
NPS_QUESTION_ID = "40"  # 自由文本题的题目编号（与 product_type 数值无关，各自独立登记）

NPS_SEND_TIMEOUT_SECONDS = 10.0


class NpsFeedbackRequest(BaseModel):
    """前端弹窗产出的两个字段；其余上报字段由本服务写死。"""

    rate: int = Field(..., ge=1, le=5, description="评分（1–5 星）")
    comment: str | None = Field(None, max_length=500, description="选填一句话评语，最长 500 字")


async def nps_server_id(db: AsyncSession) -> str:
    """本机 NPS serverid（64 位小写 hex），与遥测 instance_id 尽量同源。

    走 ``ensure_instance_state`` 取遥测行身份（行缺失时按遥测同款规则补建，
    之后两套上报共用同一身份）；宿主派生结果与该身份前缀一致时用全量哈希
    （前缀即遥测 instance_id），否则对该身份再哈希。
    """
    state = await ensure_instance_state(db)
    full = derive_machine_hash()
    if full is not None and full[:32] == state.instance_id:
        return full
    return hashlib.sha256(("llmpod:" + state.instance_id).encode()).hexdigest()


async def submit_feedback(db: AsyncSession, request: NpsFeedbackRequest) -> None:
    """转发一条 NPS 反馈；失败 raise LLMPodError（中文文案，前端弹窗保留原地提示重试）。"""
    server_id = await nps_server_id(db)
    form: dict[str, str] = {
        "action": "submit",
        "serverid": server_id,
        "product_type": NPS_PRODUCT_TYPE,
        "panel_version": llmpod.__version__,
        "rate": str(request.rate),
    }
    if request.comment:
        form["questions"] = json.dumps({NPS_QUESTION_ID: request.comment}, ensure_ascii=False)
    try:
        async with httpx.AsyncClient(timeout=NPS_SEND_TIMEOUT_SECONDS) as client:
            response = await client.post(NPS_SUBMIT_URL, data=form)
    except httpx.HTTPError as exc:
        log.warning("NPS 反馈提交网络失败: %s", exc)
        raise LLMPodError("反馈提交失败，请稍后重试") from exc
    try:
        body = response.json()
    except ValueError:
        log.warning("NPS 收集端返回非 JSON（HTTP %s）: %s", response.status_code, response.text[:200])
        raise LLMPodError("反馈提交失败，请稍后重试")
    if str(body.get("code")) != "200":
        # 收集端 msg 已是中文（如「错误的参数【SERVER_ID】」），原样带回便于排查
        raise LLMPodError(f"反馈提交失败：{body.get('msg') or '收集端拒绝'}")
