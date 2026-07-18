"""add mcp registry and approvals

Revision ID: 28c61b6c37cb
Revises: d9e5613ca04a
Create Date: 2026-07-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '28c61b6c37cb'
down_revision: Union[str, Sequence[str], None] = 'd9e5613ca04a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'agent_runs',
        sa.Column(
            'active_tool_names',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Drop the server default once existing rows are backfilled — new rows go
    # through the ORM (default=list on the model), matching the rest of this
    # schema's convention of not declaring server_default for JSONB columns.
    op.alter_column('agent_runs', 'active_tool_names', server_default=None)

    op.create_table(
        'mcp_servers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('base_url', sa.String(length=2048), nullable=False),
        sa.Column('auth_header_name', sa.String(length=255), nullable=True),
        sa.Column('auth_header_value_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('default_risk_tier', sa.String(length=20), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('health_status', sa.String(length=20), nullable=False),
        sa.Column('last_health_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_mcp_servers_tenant_name'),
    )
    op.create_index('ix_mcp_servers_tenant_id', 'mcp_servers', ['tenant_id'], unique=False)

    op.create_table(
        'mcp_tools',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('mcp_server_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('risk_tier', sa.String(length=20), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('discovered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['mcp_server_id'], ['mcp_servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mcp_server_id', 'name', name='uq_mcp_tools_server_name'),
    )
    op.create_index('ix_mcp_tools_mcp_server_id', 'mcp_tools', ['mcp_server_id'], unique=False)

    op.create_table(
        'approval_requests',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('step_no', sa.Integer(), nullable=False),
        sa.Column('call_index', sa.Integer(), nullable=False),
        sa.Column('tool_name', sa.String(length=255), nullable=False),
        sa.Column('risk_tier', sa.String(length=20), nullable=False),
        sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('decided_by_user_id', sa.UUID(), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'step_no', 'call_index', name='uq_approval_requests_run_step_call'),
    )
    op.create_index('ix_approval_requests_expires_at', 'approval_requests', ['expires_at'], unique=False)
    op.create_index(
        'ix_approval_requests_tenant_status', 'approval_requests', ['tenant_id', 'status'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_approval_requests_tenant_status', table_name='approval_requests')
    op.drop_index('ix_approval_requests_expires_at', table_name='approval_requests')
    op.drop_table('approval_requests')

    op.drop_index('ix_mcp_tools_mcp_server_id', table_name='mcp_tools')
    op.drop_table('mcp_tools')

    op.drop_index('ix_mcp_servers_tenant_id', table_name='mcp_servers')
    op.drop_table('mcp_servers')

    op.drop_column('agent_runs', 'active_tool_names')
