# backend/alembic/versions/b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope.py
"""m18 webhook_endpoints: provider + kb_ids

Revision ID: b0c1d2e3f4a5
Revises: a8b9c0d1e2f3
Create Date: 2026-09-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webhook_endpoints", sa.Column(
        "provider", sa.String(16), nullable=False,
        server_default="generic"))
    op.add_column("webhook_endpoints", sa.Column(
        "kb_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("webhook_endpoints", "kb_ids")
    op.drop_column("webhook_endpoints", "provider")
