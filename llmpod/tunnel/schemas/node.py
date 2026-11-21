"""节点级操作 RPC 契约 model。

对应 ``NodeService`` 契约（info/execute_shell）的返回值。
``docker_pull``/``tail_log`` 返回 ``AsyncIterator[str]``，无需 model。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ShellResult(BaseModel):
    """``execute_shell()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ModelDirEntry(BaseModel):
    """模型目录中的一个文件（相对目录根的 POSIX 路径 + 字节数）。"""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(..., description="相对目录根的 POSIX 路径")
    size: int = Field(..., description="字节数")


class ModelDirProbe(BaseModel):
    """``probe_model_dir()`` 返回值：节点目录的原始事实（不做模型语义判断）。

    config 为 ``config.json`` / ``model_index.json`` 解析出的 JSON object；``files`` 是
    目录内全部普通文件（跳过符号链接）。权重过滤与 ``ModelFiles`` 装配留在 manager，
    复用「本机路径」的识别口径。
    """

    model_config = ConfigDict(extra="ignore")

    exists: bool = Field(False, description="路径是否存在且可读")
    is_dir: bool = Field(False, description="是否目录（单文件不支持）")
    config_name: str | None = Field(None, description="读到的配置文件名（config.json / model_index.json）")
    config: dict | None = Field(None, description="配置原文；解析失败或不存在为 None")
    files: list[ModelDirEntry] = Field(default_factory=list, description="目录内普通文件清单")
    total_bytes: int = Field(0, description="全部文件字节总和")
    ngram_table_bytes: int | None = Field(
        None,
        description="n-gram 嵌入表字节总和（safetensors 头扫描，Qwen4Exp PLE 表；None=未扫描，旧节点不返回该字段）",
    )
    message: str | None = Field(None, description="探测失败/降级说明，可直接展示")


class RemoveModelDirResult(BaseModel):
    """``remove_model_dir()`` 返回值。"""

    model_config = ConfigDict(extra="ignore")

    removed: bool = Field(False, description="是否已删除")
    path: str = Field("", description="目标路径")
    message: str | None = Field(None, description="失败原因，可直接展示")


class PortCheckResult(BaseModel):
    """``check_ports_free()`` 返回值：端口试绑预检结果（发号前查外部占用）。"""

    model_config = ConfigDict(extra="ignore")

    occupied: list[int] = Field(default_factory=list, description="试绑失败（被占用）的端口清单；不在清单 = 可绑")
