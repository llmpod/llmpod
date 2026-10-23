"""基线版本：schema 的唯一入口，本身不回放任何 DDL。

空库（无业务表）由 ``scripts/init_db.py`` 的 ``sync_schema`` 按当前 ORM 模型
``create_all`` 一次成型，再 stamp 到迁移链头部（head）——表结构的权威出处是
``llmpod/models``，这里只负责给链一个起点；新库不回放任何历史 DDL。

存量库（有 alembic_version）走 ``alembic upgrade head``，从其版本戳起回放
0002 起的增量 revision。因此**模型变更必须附带新的 revision**
（``make db-revision`` 自动生成）；基线本身不回放，只有库由当前镜像新建时
才保证结构一次成型。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-12
"""

from __future__ import annotations

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
