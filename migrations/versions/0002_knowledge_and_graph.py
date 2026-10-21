"""knowledge base, graph cursor, and token accounting

Revision ID: 0002_knowledge_and_graph
Revises: 0001_initial
Create Date: 2026-10-21

Explicit DDL, same rule as 0001: a migration that introspects the live models is not a
migration. `tests/test_migrations.py` asserts that `upgrade head` on an empty database
produces exactly the schema the ORM declares, so this file and `app/models.py` have to agree
column for column.
"""
import sqlalchemy as sa
from alembic import op

revision = '0002_knowledge_and_graph'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('agent_runs') as batch:
        batch.add_column(sa.Column('prompt_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('completion_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('cost_usd', sa.Float(), nullable=True))
        batch.add_column(sa.Column('model', sa.String(length=120), nullable=True))

    op.create_table(
        'knowledge_documents',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('source', sa.String(length=200), nullable=False),
        sa.Column('version', sa.String(length=40), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'checksum', name='uq_knowledge_document_checksum'),
    )
    op.create_index('ix_knowledge_documents_tenant_id', 'knowledge_documents', ['tenant_id'])

    op.create_table(
        'knowledge_chunks',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('document_id', sa.String(length=64),
                  sa.ForeignKey('knowledge_documents.id'), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('heading', sa.String(length=300), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('embedding', sa.JSON(), nullable=False),
        sa.Column('embedding_model', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_knowledge_chunks_tenant_id', 'knowledge_chunks', ['tenant_id'])
    op.create_index('ix_knowledge_chunks_document_id', 'knowledge_chunks', ['document_id'])

    op.create_table(
        'pipeline_graph_runs',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64),
                  sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('current_node', sa.String(length=60), nullable=True),
        sa.Column('pending_gate', sa.String(length=60), nullable=True),
        sa.Column('steps', sa.JSON(), nullable=False),
        sa.Column('state', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_pipeline_graph_runs_tenant_id', 'pipeline_graph_runs', ['tenant_id'])
    op.create_index('ix_pipeline_graph_runs_inquiry_id', 'pipeline_graph_runs', ['inquiry_id'])
    op.create_index('ix_pipeline_graph_runs_thread_id', 'pipeline_graph_runs', ['thread_id'])


def downgrade() -> None:
    op.drop_index('ix_pipeline_graph_runs_thread_id', table_name='pipeline_graph_runs')
    op.drop_index('ix_pipeline_graph_runs_inquiry_id', table_name='pipeline_graph_runs')
    op.drop_index('ix_pipeline_graph_runs_tenant_id', table_name='pipeline_graph_runs')
    op.drop_table('pipeline_graph_runs')

    op.drop_index('ix_knowledge_chunks_document_id', table_name='knowledge_chunks')
    op.drop_index('ix_knowledge_chunks_tenant_id', table_name='knowledge_chunks')
    op.drop_table('knowledge_chunks')

    op.drop_index('ix_knowledge_documents_tenant_id', table_name='knowledge_documents')
    op.drop_table('knowledge_documents')

    with op.batch_alter_table('agent_runs') as batch:
        batch.drop_column('model')
        batch.drop_column('cost_usd')
        batch.drop_column('completion_tokens')
        batch.drop_column('prompt_tokens')
