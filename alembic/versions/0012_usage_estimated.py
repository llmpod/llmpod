"""Mark request logs whose token usage contains estimates.

Revision ID: 0012_usage_estimated
Revises: 0011_user_auth_version
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_usage_estimated"
down_revision = "0011_user_auth_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_log",
        sa.Column(
            "usage_estimated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="用量含断流/缺报估算值",
        ),
    )


def downgrade() -> None:
    op.drop_column("request_log", "usage_estimated")
