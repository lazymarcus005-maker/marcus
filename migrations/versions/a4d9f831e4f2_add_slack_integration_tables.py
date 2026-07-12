"""add slack integration tables

Revision ID: a4d9f831e4f2
Revises: 6bdb4f61d1b0
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4d9f831e4f2'
down_revision: Union[str, Sequence[str], None] = '6bdb4f61d1b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'slack_thread_mappings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('thread_ts', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'thread_ts', name='uq_slack_thread_mappings_channel_thread'),
        sa.UniqueConstraint('run_id', name='uq_slack_thread_mappings_run_id'),
    )
    op.create_index(
        'ix_slack_thread_mappings_tenant_id',
        'slack_thread_mappings',
        ['tenant_id'],
        unique=False,
    )

    op.create_table(
        'slack_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('event_id', sa.String(length=255), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=True),
        sa.Column('run_id', sa.UUID(), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id'),
    )
    op.create_index('ix_slack_events_run_id', 'slack_events', ['run_id'], unique=False)
    op.create_index('ix_slack_events_tenant_id', 'slack_events', ['tenant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_slack_events_tenant_id', table_name='slack_events')
    op.drop_index('ix_slack_events_run_id', table_name='slack_events')
    op.drop_table('slack_events')

    op.drop_index('ix_slack_thread_mappings_tenant_id', table_name='slack_thread_mappings')
    op.drop_table('slack_thread_mappings')
