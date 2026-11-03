"""graph checkpoints, pending writes, and acquisition runs

Revision ID: 0004_graph_state
Revises: 0003_source_runs
Create Date: 2026-11-03

Three tables, one reason: the orchestrator now persists what it did while it did it.

* `graph_checkpoints` / `graph_checkpoint_writes` back `app/graph/checkpoint.py`, a
  `BaseCheckpointSaver` over SQL. The existing `pipeline_graph_runs` row is a summary written
  once, after the graph returns; these are written by the graph itself, once per node.
* `acquisition_runs` is the run record for the pre-inquiry graph. It cannot reuse
  `pipeline_graph_runs` because that row requires a non-null `inquiry_id`, and 获客 happens
  before an inquiry exists.

Explicit DDL, same rule as 0001/0002/0003: `tests/test_migrations.py` compares this against
`app/models.py` column for column, so the two have to agree.
"""
import sqlalchemy as sa
from alembic import op

revision = '0004_graph_state'
down_revision = '0003_source_runs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'graph_checkpoints',
        sa.Column('thread_id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=120), primary_key=True, nullable=False),
        sa.Column('checkpoint_id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('parent_checkpoint_id', sa.String(length=64), nullable=True),
        sa.Column('tenant_id', sa.String(length=64), nullable=True),
        sa.Column('run_id', sa.String(length=64), nullable=True),
        sa.Column('checkpoint_type', sa.String(length=40), nullable=False),
        sa.Column('checkpoint_blob', sa.Text(), nullable=False),
        sa.Column('metadata_type', sa.String(length=40), nullable=False),
        sa.Column('metadata_blob', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_graph_checkpoints_tenant_id', 'graph_checkpoints', ['tenant_id'])
    op.create_index('ix_graph_checkpoints_run_id', 'graph_checkpoints', ['run_id'])

    op.create_table(
        'graph_checkpoint_writes',
        sa.Column('thread_id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=120), primary_key=True, nullable=False),
        sa.Column('checkpoint_id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('task_id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('idx', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('channel', sa.String(length=120), nullable=False),
        sa.Column('value_type', sa.String(length=40), nullable=False),
        sa.Column('value_blob', sa.Text(), nullable=False),
    )

    op.create_table(
        'acquisition_runs',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('stop_reason', sa.String(length=120), nullable=True),
        sa.Column('fetched', sa.Integer(), nullable=False),
        sa.Column('accepted', sa.Integer(), nullable=False),
        sa.Column('scored', sa.Integer(), nullable=False),
        sa.Column('reachable', sa.Integer(), nullable=False),
        sa.Column('steps', sa.JSON(), nullable=False),
        sa.Column('state', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_acquisition_runs_tenant_id', 'acquisition_runs', ['tenant_id'])
    op.create_index('ix_acquisition_runs_thread_id', 'acquisition_runs', ['thread_id'])


def downgrade() -> None:
    op.drop_index('ix_acquisition_runs_thread_id', table_name='acquisition_runs')
    op.drop_index('ix_acquisition_runs_tenant_id', table_name='acquisition_runs')
    op.drop_table('acquisition_runs')
    op.drop_table('graph_checkpoint_writes')
    op.drop_index('ix_graph_checkpoints_run_id', table_name='graph_checkpoints')
    op.drop_index('ix_graph_checkpoints_tenant_id', table_name='graph_checkpoints')
    op.drop_table('graph_checkpoints')
