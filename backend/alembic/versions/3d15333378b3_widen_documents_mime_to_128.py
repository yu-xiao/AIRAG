"""widen documents mime to 128

Revision ID: 3d15333378b3
Revises: e6ad26c40163
Create Date: 2026-09-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3d15333378b3'
down_revision: Union[str, Sequence[str], None] = 'e6ad26c40163'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # docx/xlsx 的 MIME 类型分别 71/66 字符,超出原 VARCHAR(64)
    op.alter_column(
        'documents',
        'mime',
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'documents',
        'mime',
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
