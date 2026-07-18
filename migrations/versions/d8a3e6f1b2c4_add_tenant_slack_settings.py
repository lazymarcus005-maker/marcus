"""add tenant slack settings

Revision ID: d8a3e6f1b2c4
Revises: c3f6a812d5e7
Create Date: 2026-07-11 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8a3e6f1b2c4'
down_revision: Union[str, Sequence[str], None] = 'c3f6a812d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tenant_slack_settings',
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('bot_token_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('signing_secret_encrypted', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tenant_slack_settings')
