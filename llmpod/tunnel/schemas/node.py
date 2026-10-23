"""节点级操作 RPC 契约 model。

对应 ``NodeService`` 契约（info/execute_shell）的返回值。
``docker_pull``/``tail_log`` 返回 ``AsyncIterator[str]``，无需 model。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ShellResult(BaseModel):
    """``execute_shell()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    exit_code: int
    stdout: str = ""
    stderr: str = ""
