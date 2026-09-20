# backend/alembic/versions/e5f6a7b8c9d0_m13_eval_tables.py
"""m13 eval_runs/eval_items 评估入库两张表

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "eval_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expect_doc_ids", sa.JSON(), nullable=True),
        sa.Column("expect_keywords", sa.JSON(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("refused", sa.Boolean(), nullable=True),
        sa.Column("hit_at_k", sa.Float(), nullable=True),
        sa.Column("mrr", sa.Float(), nullable=True),
        sa.Column("keyword_recall", sa.Float(), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("relevancy", sa.Float(), nullable=True),
        sa.Column("reference_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_eval_items_run_id", "eval_items", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_items_run_id", table_name="eval_items")
    op.drop_table("eval_items")
    op.drop_table("eval_runs")
