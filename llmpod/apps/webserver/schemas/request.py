"""WebServer 请求 schemas：查询参数、请求体。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

# 数值型请求字段的上界 = 对应 DB 列的可容纳范围。超界输入（如前端敲进 10^28）会穿过
# Pydantic 直达 asyncpg 参数绑定：Integer / BigInteger 列抛 DataError(value out of
# int32/int64 range)、Numeric(p,s) 列抛 NumericValueOutOfRangeError(numeric field
# overflow)；二者同属 asyncpg DataError 子类，却被 SQLAlchemy 的 asyncpg 方言塌成通用
# DBAPIError，躲过 errors.py 的 IntegrityError 处理器 → 裸 500。故请求模型一律用这些
# 常量做 le 上界，把越界拦成 422（VALIDATION_ERROR + 字段 loc 供前端定位）。
INT32_MAX = 2**31 - 1  # PG integer：route.context_length、api_key.rpm_limit / tpm_limit
INT64_MAX = 2**63 - 1  # PG bigint：api_key.total_token_quota
NUMERIC_12_6_MAX = Decimal("999999.999999")  # PG Numeric(12,6)：route 三项定价
NUMERIC_20_6_MAX = Decimal("99999999999999.999999")  # PG Numeric(20,6)：预算与钱包余额


class PaginationParams(BaseModel):
    """分页查询参数。"""

    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(default=20, ge=1, le=100, description="Items per page")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
