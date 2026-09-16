"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: generated from app/models.py

This is a FROZEN SNAPSHOT of the schema, written out explicitly.

It is deliberately not `Base.metadata.create_all()`. A migration that introspects the live
models is not a migration: change a model and this same revision silently produces a
different schema, so two databases migrated to `0001_initial` disagree while both report
success. That is unfixable in production, because the original schema is no longer
recoverable from the code. Writing the DDL out freezes it.

`tests/test_migrations.py` guards the invariant that matters: upgrading an empty database to
head must produce exactly the schema the ORM models declare.
"""
import sqlalchemy as sa
from alembic import op

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_runs',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('agent_name', sa.String(length=100), nullable=False),
        sa.Column('trace_id', sa.String(length=64), nullable=False),
        sa.Column('input_summary', sa.Text(), nullable=False),
        sa.Column('output_summary', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_agent_runs_tenant_id', 'agent_runs', ['tenant_id'])
    op.create_index('ix_agent_runs_trace_id', 'agent_runs', ['trace_id'])
    op.create_table(
        'approvals',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('object_type', sa.String(length=40), nullable=False),
        sa.Column('object_id', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('reviewer', sa.String(length=160), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_approvals_object_id', 'approvals', ['object_id'])
    op.create_index('ix_approvals_tenant_id', 'approvals', ['tenant_id'])
    op.create_table(
        'audit_events',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('actor', sa.String(length=160), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('object_type', sa.String(length=60), nullable=False),
        sa.Column('object_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_audit_events_tenant_id', 'audit_events', ['tenant_id'])
    op.create_table(
        'customers',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('contact_name', sa.String(length=160), nullable=True),
        sa.Column('email', sa.String(length=200), nullable=False),
        sa.Column('tier', sa.String(length=40), nullable=False),
        sa.Column('preferences', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'email', name='uq_customer_tenant_email'),
    )
    op.create_index('ix_customers_tenant_id', 'customers', ['tenant_id'])
    op.create_table(
        'leads',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('contact_name', sa.String(length=160), nullable=True),
        sa.Column('email', sa.String(length=200), nullable=True),
        sa.Column('country', sa.String(length=80), nullable=True),
        sa.Column('industry', sa.String(length=120), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('trade_lane', sa.String(length=160), nullable=True),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('consent_status', sa.String(length=40), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('origin_prospect_id', sa.String(length=64), nullable=True),
        sa.Column('data_origin', sa.String(length=80), nullable=False),
        sa.Column('score_rationale', sa.Text(), nullable=True),
    )
    op.create_index('ix_leads_tenant_id', 'leads', ['tenant_id'])
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('aggregate_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_outbox_events_next_attempt_at', 'outbox_events', ['next_attempt_at'])
    op.create_index('ix_outbox_events_tenant_id', 'outbox_events', ['tenant_id'])
    op.create_table(
        'outreach_activities',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('prospect_id', sa.String(length=64), nullable=True),
        sa.Column('lead_id', sa.String(length=64), nullable=True),
        sa.Column('customer_id', sa.String(length=64), nullable=True),
        sa.Column('channel', sa.String(length=40), nullable=False),
        sa.Column('recipient', sa.String(length=200), nullable=False),
        sa.Column('subject', sa.String(length=300), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('compliance_basis', sa.String(length=160), nullable=True),
        sa.Column('approved_by', sa.String(length=160), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider_message_id', sa.String(length=200), nullable=True),
        sa.Column('trace_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_outreach_activities_customer_id', 'outreach_activities', ['customer_id'])
    op.create_index('ix_outreach_activities_lead_id', 'outreach_activities', ['lead_id'])
    op.create_index('ix_outreach_activities_prospect_id', 'outreach_activities', ['prospect_id'])
    op.create_index('ix_outreach_activities_tenant_id', 'outreach_activities', ['tenant_id'])
    op.create_table(
        'prospect_candidates',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('country', sa.String(length=80), nullable=True),
        sa.Column('industry', sa.String(length=120), nullable=True),
        sa.Column('website', sa.String(length=300), nullable=True),
        sa.Column('trade_lane', sa.String(length=160), nullable=True),
        sa.Column('est_volume_teu', sa.Float(), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('source_ref', sa.String(length=200), nullable=True),
        sa.Column('data_origin', sa.String(length=80), nullable=False),
        sa.Column('raw_payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('score_rationale', sa.Text(), nullable=True),
        sa.Column('contact_name', sa.String(length=160), nullable=True),
        sa.Column('contact_title', sa.String(length=160), nullable=True),
        sa.Column('email', sa.String(length=200), nullable=True),
        sa.Column('phone', sa.String(length=80), nullable=True),
        sa.Column('consent_status', sa.String(length=40), nullable=False),
        sa.Column('consent_basis', sa.String(length=160), nullable=True),
        sa.Column('lead_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'source', 'source_ref', name='uq_prospect_source_ref'),
    )
    op.create_index('ix_prospect_candidates_tenant_id', 'prospect_candidates', ['tenant_id'])
    op.create_table(
        'tenants',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'vessel_positions',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('booking_id', sa.String(length=64), nullable=True),
        sa.Column('vessel_name', sa.String(length=120), nullable=False),
        sa.Column('voyage', sa.String(length=80), nullable=True),
        sa.Column('mmsi', sa.String(length=20), nullable=True),
        sa.Column('imo', sa.String(length=20), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('speed_knots', sa.Float(), nullable=True),
        sa.Column('course_deg', sa.Float(), nullable=True),
        sa.Column('nav_status', sa.String(length=60), nullable=True),
        sa.Column('current_port', sa.String(length=20), nullable=True),
        sa.Column('next_port', sa.String(length=20), nullable=True),
        sa.Column('eta', sa.String(length=40), nullable=True),
        sa.Column('delay_hours', sa.Float(), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_vessel_positions_booking_id', 'vessel_positions', ['booking_id'])
    op.create_index('ix_vessel_positions_tenant_id', 'vessel_positions', ['tenant_id'])
    op.create_index('ix_vessel_positions_vessel_name', 'vessel_positions', ['vessel_name'])
    op.create_table(
        'crm_activities',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('customer_id', sa.String(length=64), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('activity_type', sa.String(length=60), nullable=False),
        sa.Column('channel', sa.String(length=40), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_crm_activities_customer_id', 'crm_activities', ['customer_id'])
    op.create_index('ix_crm_activities_tenant_id', 'crm_activities', ['tenant_id'])
    op.create_table(
        'followup_tasks',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('customer_id', sa.String(length=64), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(length=60), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_followup_tasks_due_at', 'followup_tasks', ['due_at'])
    op.create_index('ix_followup_tasks_tenant_id', 'followup_tasks', ['tenant_id'])
    op.create_table(
        'inquiries',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('customer_id', sa.String(length=64), sa.ForeignKey('customers.id'), nullable=True),
        sa.Column('external_message_id', sa.String(length=200), nullable=True),
        sa.Column('origin', sa.String(length=120), nullable=True),
        sa.Column('destination', sa.String(length=120), nullable=True),
        sa.Column('equipment', sa.String(length=40), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('etd', sa.String(length=32), nullable=True),
        sa.Column('commodity', sa.String(length=160), nullable=True),
        sa.Column('weight_kg', sa.Float(), nullable=True),
        sa.Column('incoterm', sa.String(length=40), nullable=True),
        sa.Column('raw_message', sa.Text(), nullable=False),
        sa.Column('missing_fields', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=60), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'external_message_id', name='uq_inquiry_external_message'),
    )
    op.create_index('ix_inquiries_tenant_id', 'inquiries', ['tenant_id'])
    op.create_table(
        'rate_snapshots',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('carrier', sa.String(length=80), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('base_rate', sa.Float(), nullable=False),
        sa.Column('surcharges', sa.Float(), nullable=False),
        sa.Column('total_cost', sa.Float(), nullable=False),
        sa.Column('validity', sa.String(length=32), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_rate_snapshots_inquiry_id', 'rate_snapshots', ['inquiry_id'])
    op.create_index('ix_rate_snapshots_tenant_id', 'rate_snapshots', ['tenant_id'])
    op.create_table(
        'rfqs',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('expected_suppliers', sa.Integer(), nullable=False),
        sa.Column('received_quotes', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_rfqs_inquiry_id', 'rfqs', ['inquiry_id'])
    op.create_index('ix_rfqs_tenant_id', 'rfqs', ['tenant_id'])
    op.create_table(
        'schedule_options',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('carrier', sa.String(length=80), nullable=False),
        sa.Column('service', sa.String(length=80), nullable=False),
        sa.Column('vessel', sa.String(length=100), nullable=True),
        sa.Column('voyage', sa.String(length=80), nullable=True),
        sa.Column('origin', sa.String(length=120), nullable=False),
        sa.Column('destination', sa.String(length=120), nullable=False),
        sa.Column('etd', sa.String(length=32), nullable=False),
        sa.Column('eta', sa.String(length=32), nullable=False),
        sa.Column('transit_days', sa.Integer(), nullable=False),
        sa.Column('direct', sa.Boolean(), nullable=False),
        sa.Column('co2e_kg', sa.Float(), nullable=True),
        sa.Column('source', sa.String(length=80), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_schedule_options_inquiry_id', 'schedule_options', ['inquiry_id'])
    op.create_index('ix_schedule_options_tenant_id', 'schedule_options', ['tenant_id'])
    op.create_table(
        'supplier_quotes',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('rfq_id', sa.String(length=64), sa.ForeignKey('rfqs.id'), nullable=False),
        sa.Column('supplier_name', sa.String(length=160), nullable=False),
        sa.Column('supplier_email', sa.String(length=200), nullable=False),
        sa.Column('carrier', sa.String(length=80), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('ocean_freight', sa.Float(), nullable=False),
        sa.Column('surcharges', sa.Float(), nullable=False),
        sa.Column('transit_days', sa.Integer(), nullable=True),
        sa.Column('free_time_days', sa.Integer(), nullable=True),
        sa.Column('validity', sa.String(length=32), nullable=True),
        sa.Column('reliability_score', sa.Float(), nullable=False),
        sa.Column('raw_payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'rfq_id', 'supplier_email', name='uq_supplier_quote'),
    )
    op.create_index('ix_supplier_quotes_rfq_id', 'supplier_quotes', ['rfq_id'])
    op.create_index('ix_supplier_quotes_tenant_id', 'supplier_quotes', ['tenant_id'])
    op.create_table(
        'customer_quotes',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('supplier_quote_id', sa.String(length=64), sa.ForeignKey('supplier_quotes.id'), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('cost_amount', sa.Float(), nullable=False),
        sa.Column('margin_amount', sa.Float(), nullable=False),
        sa.Column('sell_amount', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column('customer_message', sa.Text(), nullable=True),
        sa.Column('valid_until', sa.String(length=32), nullable=True),
        sa.Column('approved_by', sa.String(length=160), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_customer_quotes_inquiry_id', 'customer_quotes', ['inquiry_id'])
    op.create_index('ix_customer_quotes_tenant_id', 'customer_quotes', ['tenant_id'])
    op.create_table(
        'bookings',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('inquiry_id', sa.String(length=64), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('quote_id', sa.String(length=64), sa.ForeignKey('customer_quotes.id'), nullable=False),
        sa.Column('provider_ref', sa.String(length=120), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_bookings_tenant_id', 'bookings', ['tenant_id'])
    op.create_table(
        'tracking_events',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('booking_id', sa.String(length=64), sa.ForeignKey('bookings.id'), nullable=False),
        sa.Column('event_code', sa.String(length=80), nullable=False),
        sa.Column('event_time', sa.String(length=40), nullable=False),
        sa.Column('location', sa.String(length=160), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_tracking_events_booking_id', 'tracking_events', ['booking_id'])
    op.create_index('ix_tracking_events_tenant_id', 'tracking_events', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_tracking_events_booking_id', table_name='tracking_events')
    op.drop_index('ix_tracking_events_tenant_id', table_name='tracking_events')
    op.drop_table('tracking_events')
    op.drop_index('ix_bookings_tenant_id', table_name='bookings')
    op.drop_table('bookings')
    op.drop_index('ix_customer_quotes_inquiry_id', table_name='customer_quotes')
    op.drop_index('ix_customer_quotes_tenant_id', table_name='customer_quotes')
    op.drop_table('customer_quotes')
    op.drop_index('ix_supplier_quotes_rfq_id', table_name='supplier_quotes')
    op.drop_index('ix_supplier_quotes_tenant_id', table_name='supplier_quotes')
    op.drop_table('supplier_quotes')
    op.drop_index('ix_schedule_options_inquiry_id', table_name='schedule_options')
    op.drop_index('ix_schedule_options_tenant_id', table_name='schedule_options')
    op.drop_table('schedule_options')
    op.drop_index('ix_rfqs_inquiry_id', table_name='rfqs')
    op.drop_index('ix_rfqs_tenant_id', table_name='rfqs')
    op.drop_table('rfqs')
    op.drop_index('ix_rate_snapshots_inquiry_id', table_name='rate_snapshots')
    op.drop_index('ix_rate_snapshots_tenant_id', table_name='rate_snapshots')
    op.drop_table('rate_snapshots')
    op.drop_index('ix_inquiries_tenant_id', table_name='inquiries')
    op.drop_table('inquiries')
    op.drop_index('ix_followup_tasks_due_at', table_name='followup_tasks')
    op.drop_index('ix_followup_tasks_tenant_id', table_name='followup_tasks')
    op.drop_table('followup_tasks')
    op.drop_index('ix_crm_activities_customer_id', table_name='crm_activities')
    op.drop_index('ix_crm_activities_tenant_id', table_name='crm_activities')
    op.drop_table('crm_activities')
    op.drop_index('ix_vessel_positions_booking_id', table_name='vessel_positions')
    op.drop_index('ix_vessel_positions_tenant_id', table_name='vessel_positions')
    op.drop_index('ix_vessel_positions_vessel_name', table_name='vessel_positions')
    op.drop_table('vessel_positions')
    op.drop_table('tenants')
    op.drop_index('ix_prospect_candidates_tenant_id', table_name='prospect_candidates')
    op.drop_table('prospect_candidates')
    op.drop_index('ix_outreach_activities_customer_id', table_name='outreach_activities')
    op.drop_index('ix_outreach_activities_lead_id', table_name='outreach_activities')
    op.drop_index('ix_outreach_activities_prospect_id', table_name='outreach_activities')
    op.drop_index('ix_outreach_activities_tenant_id', table_name='outreach_activities')
    op.drop_table('outreach_activities')
    op.drop_index('ix_outbox_events_next_attempt_at', table_name='outbox_events')
    op.drop_index('ix_outbox_events_tenant_id', table_name='outbox_events')
    op.drop_table('outbox_events')
    op.drop_index('ix_leads_tenant_id', table_name='leads')
    op.drop_table('leads')
    op.drop_index('ix_customers_tenant_id', table_name='customers')
    op.drop_table('customers')
    op.drop_index('ix_audit_events_tenant_id', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('ix_approvals_object_id', table_name='approvals')
    op.drop_index('ix_approvals_tenant_id', table_name='approvals')
    op.drop_table('approvals')
    op.drop_index('ix_agent_runs_tenant_id', table_name='agent_runs')
    op.drop_index('ix_agent_runs_trace_id', table_name='agent_runs')
    op.drop_table('agent_runs')
