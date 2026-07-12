"""add tenant llm settings

Revision ID: c3f6a812d5e7
Revises: b7e9c204bb9d
Create Date: 2026-07-11 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3f6a812d5e7'
down_revision: Union[str, Sequence[str], None] = 'b7e9c204bb9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tenant_llm_settings',
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('base_url', sa.String(length=2048), nullable=False),
        sa.Column('model', sa.String(length=255), nullable=False),
        sa.Column('api_key_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tenant_llm_settings')
