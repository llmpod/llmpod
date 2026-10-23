"""会话吊销以数据库为权威：user 增 auth_version 列。

Revision ID: 0011_user_auth_version
Revises: 0010_node_auth_token

auth_version 从 1 起，本迁移前签发的 Redis 会话（版本 0）全部失效。
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_user_auth_version"
down_revision = "0010_node_auth_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Version 1 invalidates Redis sessions signed before this migration (version 0).
    op.add_column(
        "user",
        sa.Column(
            "auth_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="认证版本；权限或密码变化时同事务递增",
        ),
    )
    # 存量库在 0011 之前建列时未带 comment，这里补齐（comment 改动不随 upgrade 自动带上）
    op.alter_column("user", "auth_version", comment="认证版本；权限或密码变化时同事务递增")


def downgrade() -> None:
    op.drop_column("user", "auth_version")
