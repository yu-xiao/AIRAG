"""m21 eval_runs: heartbeat_at

Revision ID: c2d3e4f5a6b7
Revises: b0c1d2e3f4a5
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("eval_runs", sa.Column("heartbeat_at", sa.DateTime(),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column("eval_runs", "heartbeat_at")
