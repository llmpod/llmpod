"""新增 metric_engine_rollup：引擎 gauge 的 5 分钟桶物化（avg/max + 抢占增量）。

Revision ID: 0002_metric_engine_rollup
Revises: 0001_baseline
Create Date: 2026-09-14

背景：KV 水位 / 排队 / running / 前缀命中此前只存在 scraper 内存快照（只有
当前值，画不出历史曲线）。本表按 5min 桶落 avg 与 max（avg 看常态、max 看
峰值），抢占 counter 记桶内增量，samples 区分「没变」与「没抓到」。
不并入 metric_rollup 的原因：该表是 proxy 管道形状（counter + histogram），
塞 gauge 会同时污染两边语义。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_metric_engine_rollup"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metric_engine_rollup",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键ID（自增）"),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="桶起点（与 metric_rollup 同网格，本地时区 5min 对齐）",
        ),
        sa.Column("instance_id", sa.String(64), nullable=False, comment="引擎实例ID"),
        sa.Column("deployment_id", sa.String(64), nullable=False, comment="部署ID"),
        sa.Column("engine", sa.String(50), nullable=True, comment="引擎类型：vllm | sglang | ..."),
        sa.Column("kv_cache_util_avg", sa.Float(), nullable=True, comment="KV 显存水位 0-1 · 桶内均值"),
        sa.Column("kv_cache_util_max", sa.Float(), nullable=True, comment="KV 显存水位 0-1 · 桶内峰值"),
        sa.Column("running_requests_avg", sa.Float(), nullable=True, comment="并发处理中请求数 · 桶内均值"),
        sa.Column("running_requests_max", sa.Float(), nullable=True, comment="并发处理中请求数 · 桶内峰值"),
        sa.Column("waiting_requests_avg", sa.Float(), nullable=True, comment="排队深度 · 桶内均值"),
        sa.Column("waiting_requests_max", sa.Float(), nullable=True, comment="排队深度 · 桶内峰值"),
        sa.Column("prefix_cache_hit_rate_avg", sa.Float(), nullable=True, comment="前缀缓存命中率 0-1 · 桶内均值"),
        sa.Column("prefix_cache_hit_rate_max", sa.Float(), nullable=True, comment="前缀缓存命中率 0-1 · 桶内峰值"),
        sa.Column(
            "preemptions",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="桶内抢占增量（counter 差分；引擎重启按回绕口径）",
        ),
        sa.Column(
            "samples",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="桶内成功抓取次数（区分「没变」与「没抓到」）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="写入时间",
        ),
    )
    op.create_index("idx_metric_engine_rollup_bucket", "metric_engine_rollup", ["bucket_start"])
    op.create_index("idx_metric_engine_rollup_deployment", "metric_engine_rollup", ["deployment_id", "bucket_start"])
    op.create_index("idx_metric_engine_rollup_instance", "metric_engine_rollup", ["instance_id", "bucket_start"])


def downgrade() -> None:
    op.drop_index("idx_metric_engine_rollup_instance", table_name="metric_engine_rollup")
    op.drop_index("idx_metric_engine_rollup_deployment", table_name="metric_engine_rollup")
    op.drop_index("idx_metric_engine_rollup_bucket", table_name="metric_engine_rollup")
    op.drop_table("metric_engine_rollup")
