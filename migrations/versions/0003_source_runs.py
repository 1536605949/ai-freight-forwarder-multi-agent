"""scheduled source runs (collection cursor + run log)

Revision ID: 0003_source_runs
Revises: 0002_knowledge_and_graph
Create Date: 2026-10-22

Explicit DDL, same rule as 0001/0002: `tests/test_migrations.py` compares this against
`app/models.py` column for column, so the two have to agree.
"""
import sqlalchemy as sa
from alembic import op

revision = '0003_source_runs'
down_revision = '0002_knowledge_and_graph'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'source_runs',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('cursor_before', sa.String(length=200), nullable=True),
        sa.Column('cursor_after', sa.String(length=200), nullable=True),
        sa.Column('fetched', sa.Integer(), nullable=False),
        sa.Column('accepted', sa.Integer(), nullable=False),
        sa.Column('rejected', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_source_runs_tenant_id', 'source_runs', ['tenant_id'])
    op.create_index('ix_source_runs_lookup', 'source_runs',
                    ['tenant_id', 'source', 'started_at'])


def downgrade() -> None:
    op.drop_index('ix_source_runs_lookup', table_name='source_runs')
    op.drop_index('ix_source_runs_tenant_id', table_name='source_runs')
    op.drop_table('source_runs')
