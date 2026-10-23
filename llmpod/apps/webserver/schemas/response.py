"""WebServer 响应 schemas：统一三段式 {code, message, data} 封装。"""

from __future__ import annotations

from typing import Any, Generic, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")


class ResultBase(BaseModel, Generic[T]):
    """统一响应基类：所有响应共享 code + message + data 三段式结构。

    - 单对象：ResultBase[UserOut]          → data: UserOut
    - 列表：  ResultBase[list[UserOut]]    → data: list[UserOut]
    - 分页：  PaginatedResponse[UserOut]   → data: PageData[UserOut]
    """

    code: int = 200
    message: str = "success"
    data: T | None = None


# 向后兼容别名：Result[X] 等价于 ResultBase[X]（基类已含 data，Result 不再需要独立定义）
Result = ResultBase


class PageData(BaseModel, Generic[T]):
    """分页数据载荷，作为 ResultBase.data 的内容。"""

    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class PaginatedResponse(ResultBase[PageData[T]]):
    """分页响应：data 内嵌分页载荷 {items, total, page, page_size, total_pages}。

    统一三段式 {code, message, data}，data 即 PageData[T]。
    response_model=PaginatedResponse[GpuOut]  即可。
    """

    @classmethod
    def result(cls, items: list[T], total: int, page: int, page_size: int) -> dict:
        """构建统一分页响应字典，路由中 return 即可。

        例: return PaginatedResponse.result(items=gpus, total=t, page=p, page_size=ps)
        """
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        return cls(
            data=PageData(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
                total_pages=total_pages,
            ),
        ).model_dump()


def success(data: Any = None, message: str = "success") -> dict:
    """构建成功响应字典（code=200）。"""
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        data = dump()
    return {"code": 200, "message": message, "data": data}


class ErrorResult(BaseModel):
    """错误响应形状"""

    code: int = 400
    message: str = "error"
    data: Any = None
    error_code: str | None = Field(
        default=None,
        description="业务错误码（见 core.exceptions.ErrorCode 枚举）；成功响应为 null",
    )


def error(
    message: str = "error",
    code: int = 400,
    data: Any = None,
    error_code: str | None = None,
) -> dict:
    """构建主动错误响应字典（code=400 默认）。

    body 形状与 :class:`ErrorResult`（OpenAPI schema 权威定义）一一对应，但走纯字典
    构建，避免错误热路径上每请求一次 Pydantic 校验/序列化。
    """
    return {"code": code, "message": message, "data": data, "error_code": error_code}
