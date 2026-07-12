"""add scheduler and quotas

Revision ID: b7e9c204bb9d
Revises: f1bb4f9e6a12
Create Date: 2026-07-11 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e9c204bb9d'
down_revision: Union[str, Sequence[str], None] = 'f1bb4f9e6a12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'scheduled_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('cron_expression', sa.String(length=255), nullable=False),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('channel_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['last_run_id'], ['agent_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_scheduled_jobs_tenant_name'),
    )
    op.create_index(
        'ix_scheduled_jobs_tenant_enabled',
        'scheduled_jobs',
        ['tenant_id', 'enabled'],
        unique=False,
    )

    op.create_table(
        'tenant_quotas',
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('daily_token_quota', sa.Integer(), nullable=False),
        sa.Column('max_active_runs', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('tenant_quotas')
    op.drop_index('ix_scheduled_jobs_tenant_enabled', table_name='scheduled_jobs')
    op.drop_table('scheduled_jobs')
