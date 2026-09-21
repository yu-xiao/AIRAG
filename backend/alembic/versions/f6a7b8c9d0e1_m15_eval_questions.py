# backend/alembic/versions/f6a7b8c9d0e1_m15_eval_questions.py
"""m15 eval_questions 题集入库 + eval_runs 触发三列 + JSON 种子

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-21
"""
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "eval_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expect_doc_ids", sa.JSON(), nullable=True),
        sa.Column("expect_keywords", sa.JSON(), nullable=True),
        sa.Column("reference_answer", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"],
                                ondelete="CASCADE"),
    )
    op.create_index("ix_eval_questions_kb_id", "eval_questions", ["kb_id"])
    op.alter_column("eval_runs", "summary", existing_type=sa.JSON(),
                    nullable=True)  # running 行终态前 summary 为 NULL
    op.add_column("eval_runs", sa.Column(
        "status", sa.String(16), nullable=False, server_default="completed"))
    op.add_column("eval_runs", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("eval_runs", sa.Column("triggered_by", sa.Integer(),
                                         nullable=True))
    from app.services.eval_seed import seed_eval_questions
    eval_dir = Path(__file__).resolve().parents[2] / "eval_sets"
    report = seed_eval_questions(op.get_bind(), eval_dir)
    print(f"[m15 seed] {report}")


def downgrade() -> None:
    op.drop_column("eval_runs", "triggered_by")
    op.drop_column("eval_runs", "error")
    op.drop_column("eval_runs", "status")
    op.alter_column("eval_runs", "summary", existing_type=sa.JSON(),
                    nullable=False)
    op.drop_index("ix_eval_questions_kb_id", table_name="eval_questions")
    op.drop_table("eval_questions")
