"""m5 audit logs and ocr columns

Revision ID: a1b2c3d4e5f6
Revises: 3d15333378b3
Create Date: 2026-09-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '3d15333378b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('ip', sa.String(length=45), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
    )
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'])
    op.add_column(
        'documents',
        sa.Column('ocr_mode', sa.String(length=8), nullable=False, server_default='auto'),
    )
    op.add_column(
        'documents',
        sa.Column('ocr_used', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'ocr_used')
    op.drop_column('documents', 'ocr_mode')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_table('audit_logs')
