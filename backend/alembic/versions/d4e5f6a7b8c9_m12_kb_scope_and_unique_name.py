# backend/alembic/versions/d4e5f6a7b8c9_m12_kb_scope_and_unique_name.py
"""m12 api_keys.kb_scope + knowledge_bases.name unique

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("kb_scope", sa.JSON(), nullable=True))
    # 存量同名去重:按 id 升序第 2 条起改后缀,幂等(重跑无同名组即空操作)
    op.execute(
        """
        WITH ranked AS (
            SELECT id, name,
                   ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) AS rn
            FROM knowledge_bases
        )
        UPDATE knowledge_bases k
        SET name = k.name || '-' || ranked.rn
        FROM ranked
        WHERE k.id = ranked.id AND ranked.rn > 1
        """
    )
    op.drop_index("ix_knowledge_bases_name")
    op.create_unique_constraint("uq_knowledge_bases_name",
                                "knowledge_bases", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases")
    op.create_index("ix_knowledge_bases_name", "knowledge_bases", ["name"])
    op.drop_column("api_keys", "kb_scope")
