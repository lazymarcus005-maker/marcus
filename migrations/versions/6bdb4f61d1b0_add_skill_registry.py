"""add skill registry

Revision ID: 6bdb4f61d1b0
Revises: 28c61b6c37cb
Create Date: 2026-07-11 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6bdb4f61d1b0'
down_revision: Union[str, Sequence[str], None] = '28c61b6c37cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('active_revision_id', sa.UUID(), nullable=True),
        sa.Column('owner_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_skills_tenant_name'),
    )
    op.create_index('ix_skills_tenant_id', 'skills', ['tenant_id'], unique=False)

    op.create_table(
        'skill_revisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('skill_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('instruction', sa.Text(), nullable=False),
        sa.Column('manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('output_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('required_tools', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('change_reason', sa.Text(), nullable=False),
        sa.Column('created_from_run_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_from_run_id'], ['agent_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['skill_id'], ['skills.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('skill_id', 'version', name='uq_skill_revisions_skill_version'),
    )
    op.create_index('ix_skill_revisions_skill_id', 'skill_revisions', ['skill_id'], unique=False)

    op.create_foreign_key(
        'fk_skills_active_revision_id_skill_revisions',
        'skills',
        'skill_revisions',
        ['active_revision_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_agent_runs_active_skill_revision_id_skill_revisions',
        'agent_runs',
        'skill_revisions',
        ['active_skill_revision_id'],
        ['id'],
        ondelete='SET NULL',
    )

    op.create_table(
        'skill_usage',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('revision_id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=False),
        sa.Column('success', sa.Boolean(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('feedback', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['revision_id'], ['skill_revisions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('revision_id', 'run_id', name='uq_skill_usage_revision_run'),
    )
    op.create_index('ix_skill_usage_revision_id', 'skill_usage', ['revision_id'], unique=False)
    op.create_index('ix_skill_usage_tenant_id', 'skill_usage', ['tenant_id'], unique=False)

    op.execute(
        """
        CREATE FUNCTION reject_published_skill_revision_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'published' THEN
                RAISE EXCEPTION 'published skill revisions are immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reject_published_skill_revision_update
        BEFORE UPDATE ON skill_revisions
        FOR EACH ROW
        EXECUTE FUNCTION reject_published_skill_revision_update();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP TRIGGER IF EXISTS trg_reject_published_skill_revision_update ON skill_revisions')
    op.execute('DROP FUNCTION IF EXISTS reject_published_skill_revision_update()')

    op.drop_index('ix_skill_usage_tenant_id', table_name='skill_usage')
    op.drop_index('ix_skill_usage_revision_id', table_name='skill_usage')
    op.drop_table('skill_usage')

    op.drop_constraint(
        'fk_agent_runs_active_skill_revision_id_skill_revisions',
        'agent_runs',
        type_='foreignkey',
    )
    op.drop_constraint(
        'fk_skills_active_revision_id_skill_revisions',
        'skills',
        type_='foreignkey',
    )

    op.drop_index('ix_skill_revisions_skill_id', table_name='skill_revisions')
    op.drop_table('skill_revisions')

    op.drop_index('ix_skills_tenant_id', table_name='skills')
    op.drop_table('skills')
