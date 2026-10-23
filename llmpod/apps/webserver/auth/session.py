"""Redis 会话契约（登录写入 / ``get_current_user`` 读回）。

会话以 ``hash_token(token)`` 为键、JSON 序列化存 Redis（TTL = ``token_expire_seconds``），
本模型是这份数据的单一形状契约——登录侧 ``model_dump_json`` 写入、鉴权侧
``model_validate_json`` 读回，键名漂移在读回时显式报错而非静默 None。

兼容：字段尽量给默认值（旧版本会话缺次要键仍可读）；``user_id`` / ``tenant_id`` /
``access_domain`` 是鉴权硬依赖，缺失视为损坏会话（按令牌过期处理，强制重登录）。
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field


class SessionData(BaseModel):
    """一次登录会话的载体（Redis ``llmpod:token:<sha256>`` 的 JSON 形状）。

    同时是 ``/auth/me`` 回显的单一形状来源（``UserInfoData`` 直接继承本模型）。
    """

    model_config = ConfigDict(extra="ignore")

    user_id: int = Field(..., description="用户ID")
    tenant_id: int = Field(..., description="所属租户ID")
    access_domain: str = Field(..., description="访问域：platform=平台/管理端 | tenant=租户端")
    is_superuser: bool = Field(False, description="是否超级管理员（短路所有权限校验）")
    username: str = Field("", description="用户名")
    # 签发时的用户会话版本号（auth_service 维护）：鉴权时与用户表现值比对，
    # 不一致即视为已吊销——禁用/改密/角色变更后旧会话即刻失效，不必等 24h TTL。
    # 存量会话不带此字段（默认 0），与迁移 0011 置的现值 1 不相等 → 强制重登。
    session_ver: int = Field(0, description="签发时的会话版本号（旧会话吊销判据）")

    @property
    def exempt_from_maintenance(self) -> bool:
        """是否豁免维护模式（超管或平台域）。

        登录拦截（routes/public/auth.py）与维护闸（middleware）共用同一条规则——
        两处手抄会在放宽豁免口径时改一漏一，平台域非超管随即陷入「登录放行 → 首调
        被踢回登录页 → 再登录放行」的无限跳转。取值走 AccessDomain 枚举，不手抄
        字符串。
        """
        from llmpod.apps.webserver.constants import AccessDomain

        return self.is_superuser or self.access_domain == AccessDomain.PLATFORM

    role_ids: list[int] = Field(default_factory=list, description="用户绑定的角色ID列表")
    role_codes: list[str] = Field(
        default_factory=list, description="用户拥有的角色编码列表（后端派生，可选，允许为空）"
    )


class LoginResult(NamedTuple):
    """``authenticate`` 的返回：新签发令牌 + 已写入 Redis 的会话。"""

    token: str
    session: SessionData
