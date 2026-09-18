# backend/alembic/versions/c1d2e3f4a5b6_m11_api_key_role.py
"""m11 api_keys.role

Revision ID: c1d2e3f4a5b6
Revises: b9c8d7e6f5a4
Create Date: 2026-09-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c8d7e6f5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("role", sa.String(16), nullable=False,
                  server_default="read_only"),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "role")
