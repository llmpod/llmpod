"""模型存储/分发契约 model。

- ``ManifestFile`` / ``Manifest``：Manager 文件服务 ``GET /manifest`` 的 HTTP 响应契约
  （Manager 生成、Node 出站拉取消费）。双端契约同住 wire 层，两侧 import 同一模型——
  字段演进在两端同时显式暴露，不再各自手拼/本地镜像。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ManifestFile(BaseModel):
    """manifest 单文件条目。"""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(..., description="相对 root 的 POSIX 路径")
    size: int = Field(..., description="字节数")


class Manifest(BaseModel):
    """local 源下载清单：一次下载的权威清单（生成时刻冻结文件集合与大小）。"""

    model_config = ConfigDict(extra="ignore")

    model_id: int = Field(..., description="模型ID")
    root: str = Field(..., description="Manager 上 source root 绝对路径")
    revision: str | None = Field(None, description="版本/commit-hash")
    files: list[ManifestFile] = Field(default_factory=list, description="文件清单（按 path 稳定排序）")
    total_bytes: int = Field(..., description="全部文件字节总和")
