"""Container 生命周期 RPC 契约 model。

对应 ``ContainerService`` 契约（deploy/stop/remove/list/inspect）的返回值。
wire 上以 JSON dict 传输，Pydantic model 做边界校验 + 类型安全。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DeployResult(BaseModel):
    """``deploy()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    success: bool
    container_name: str


class StopResult(BaseModel):
    """``stop()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    success: bool
    container_name: str


class RemoveResult(BaseModel):
    """``remove()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    success: bool
    removed: bool
    container_name: str


class ContainerSummary(BaseModel):
    """``list()`` 返回列表的单项。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    id: str = ""
    state: str = ""
    labels: dict[str, str] = {}


class ContainerState(BaseModel):
    """``inspect()`` 返回值——docker State 子树（PascalCase，由 docker daemon 决定形状）。

    gate 判定：``Status == "running" AND Running AND NOT Restarting AND RestartCount == 0``。
    extra="ignore" 容忍 docker 版本差异带来的额外字段。
    """

    model_config = ConfigDict(extra="ignore")

    Status: str = ""
    Running: bool = False
    Restarting: bool = False
    RestartCount: int = 0
    ExitCode: int = 0
