from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Text, Float, Integer, DateTime, Boolean, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

def now(): return datetime.now(timezone.utc)

class Tenant(Base):
    __tablename__='tenants'
    id:Mapped[str]=mapped_column(String(64), primary_key=True)
    name:Mapped[str]=mapped_column(String(200))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Lead(Base):
    __tablename__='leads'
    id:Mapped[str]=mapped_column(String(64), primary_key=True)
    tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    company:Mapped[str]=mapped_column(String(200)); contact_name:Mapped[str|None]=mapped_column(String(160), nullable=True)
    email:Mapped[str|None]=mapped_column(String(200), nullable=True); country:Mapped[str|None]=mapped_column(String(80), nullable=True)
    industry:Mapped[str|None]=mapped_column(String(120), nullable=True); source:Mapped[str]=mapped_column(String(80), default='manual')
    trade_lane:Mapped[str|None]=mapped_column(String(160), nullable=True); score:Mapped[float]=mapped_column(Float, default=0)
    status:Mapped[str]=mapped_column(String(40), default='new'); consent_status:Mapped[str]=mapped_column(String(40), default='unknown')
    notes:Mapped[str|None]=mapped_column(Text, nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
    # Provenance for leads that originated from automated prospecting rather than manual entry.
    origin_prospect_id:Mapped[str|None]=mapped_column(String(64), nullable=True)
    data_origin:Mapped[str]=mapped_column(String(80), default='manual_entry')
    score_rationale:Mapped[str|None]=mapped_column(Text, nullable=True)

class ProspectCandidate(Base):
    """A company surfaced by a prospecting source, before it is qualified into a Lead.

    Kept separate from Lead on purpose: a prospect is *unverified vendor data* and may be
    rejected for compliance or relevance. Only after enrichment + compliance review does it
    get promoted into a Lead. This separation is what keeps unverified third-party data out
    of the CRM and out of the audit trail of real customer objects.
    """
    __tablename__='prospect_candidates'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    company:Mapped[str]=mapped_column(String(200)); country:Mapped[str|None]=mapped_column(String(80), nullable=True)
    industry:Mapped[str|None]=mapped_column(String(120), nullable=True); website:Mapped[str|None]=mapped_column(String(300), nullable=True)
    trade_lane:Mapped[str|None]=mapped_column(String(160), nullable=True); est_volume_teu:Mapped[float|None]=mapped_column(Float, nullable=True)
    source:Mapped[str]=mapped_column(String(80), default='mock'); source_ref:Mapped[str|None]=mapped_column(String(200), nullable=True)
    # data_origin records *how* this record was obtained; required for GDPR/PIPL auditability.
    data_origin:Mapped[str]=mapped_column(String(80), default='public_business_registry')
    raw_payload:Mapped[dict]=mapped_column(JSON, default=dict)
    # Pipeline: discovered -> enriched -> promoted | rejected
    status:Mapped[str]=mapped_column(String(40), default='discovered')
    score:Mapped[float]=mapped_column(Float, default=0); score_rationale:Mapped[str|None]=mapped_column(Text, nullable=True)
    contact_name:Mapped[str|None]=mapped_column(String(160), nullable=True); contact_title:Mapped[str|None]=mapped_column(String(160), nullable=True)
    email:Mapped[str|None]=mapped_column(String(200), nullable=True); phone:Mapped[str|None]=mapped_column(String(80), nullable=True)
    consent_status:Mapped[str]=mapped_column(String(40), default='unknown'); consent_basis:Mapped[str|None]=mapped_column(String(160), nullable=True)
    lead_id:Mapped[str|None]=mapped_column(String(64), nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__=(UniqueConstraint('tenant_id','source','source_ref',name='uq_prospect_source_ref'),)

class OutreachActivity(Base):
    """Every outbound contact attempt. Drives frequency capping and the compliance audit trail."""
    __tablename__='outreach_activities'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    prospect_id:Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    lead_id:Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    customer_id:Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    channel:Mapped[str]=mapped_column(String(40), default='email'); recipient:Mapped[str]=mapped_column(String(200))
    subject:Mapped[str|None]=mapped_column(String(300), nullable=True); body:Mapped[str]=mapped_column(Text)
    # draft -> pending_approval -> approved -> sent | rejected | suppressed
    status:Mapped[str]=mapped_column(String(40), default='draft')
    compliance_basis:Mapped[str|None]=mapped_column(String(160), nullable=True)
    approved_by:Mapped[str|None]=mapped_column(String(160), nullable=True); approved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    sent_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True); provider_message_id:Mapped[str|None]=mapped_column(String(200), nullable=True)
    trace_id:Mapped[str|None]=mapped_column(String(64), nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class VesselPositionSnapshot(Base):
    """Point-in-time vessel position, retained so delay can be measured over time.

    A single live lookup can only answer "where is it now". Detecting that a delay is
    *growing* requires history, which is what makes a proactive warning possible.
    """
    __tablename__='vessel_positions'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    booking_id:Mapped[str|None]=mapped_column(String(64), nullable=True, index=True)
    vessel_name:Mapped[str]=mapped_column(String(120), index=True); voyage:Mapped[str|None]=mapped_column(String(80), nullable=True)
    mmsi:Mapped[str|None]=mapped_column(String(20), nullable=True); imo:Mapped[str|None]=mapped_column(String(20), nullable=True)
    latitude:Mapped[float|None]=mapped_column(Float, nullable=True); longitude:Mapped[float|None]=mapped_column(Float, nullable=True)
    speed_knots:Mapped[float|None]=mapped_column(Float, nullable=True); course_deg:Mapped[float|None]=mapped_column(Float, nullable=True)
    nav_status:Mapped[str|None]=mapped_column(String(60), nullable=True)
    current_port:Mapped[str|None]=mapped_column(String(20), nullable=True); next_port:Mapped[str|None]=mapped_column(String(20), nullable=True)
    eta:Mapped[str|None]=mapped_column(String(40), nullable=True); delay_hours:Mapped[float|None]=mapped_column(Float, nullable=True)
    source:Mapped[str]=mapped_column(String(80), default='mock_ais'); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Customer(Base):
    __tablename__='customers'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    company:Mapped[str]=mapped_column(String(200)); contact_name:Mapped[str|None]=mapped_column(String(160), nullable=True)
    email:Mapped[str]=mapped_column(String(200)); tier:Mapped[str]=mapped_column(String(40), default='standard')
    preferences:Mapped[dict]=mapped_column(JSON, default=dict); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('tenant_id','email',name='uq_customer_tenant_email'),)

class Inquiry(Base):
    __tablename__='inquiries'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    customer_id:Mapped[str|None]=mapped_column(String(64), ForeignKey('customers.id'), nullable=True)
    external_message_id:Mapped[str|None]=mapped_column(String(200), nullable=True)
    origin:Mapped[str|None]=mapped_column(String(120), nullable=True); destination:Mapped[str|None]=mapped_column(String(120), nullable=True)
    equipment:Mapped[str|None]=mapped_column(String(40), nullable=True); quantity:Mapped[int|None]=mapped_column(Integer, nullable=True)
    etd:Mapped[str|None]=mapped_column(String(32), nullable=True); commodity:Mapped[str|None]=mapped_column(String(160), nullable=True)
    weight_kg:Mapped[float|None]=mapped_column(Float, nullable=True); incoterm:Mapped[str|None]=mapped_column(String(40), nullable=True)
    raw_message:Mapped[str]=mapped_column(Text); missing_fields:Mapped[list]=mapped_column(JSON, default=list)
    status:Mapped[str]=mapped_column(String(60), default='needs_clarification'); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__=(UniqueConstraint('tenant_id','external_message_id',name='uq_inquiry_external_message'),)

class ScheduleOption(Base):
    __tablename__='schedule_options'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    inquiry_id:Mapped[str]=mapped_column(String(64), ForeignKey('inquiries.id'), index=True)
    carrier:Mapped[str]=mapped_column(String(80)); service:Mapped[str]=mapped_column(String(80)); vessel:Mapped[str|None]=mapped_column(String(100), nullable=True)
    voyage:Mapped[str|None]=mapped_column(String(80), nullable=True); origin:Mapped[str]=mapped_column(String(120)); destination:Mapped[str]=mapped_column(String(120))
    etd:Mapped[str]=mapped_column(String(32)); eta:Mapped[str]=mapped_column(String(32)); transit_days:Mapped[int]=mapped_column(Integer)
    direct:Mapped[bool]=mapped_column(Boolean, default=True); co2e_kg:Mapped[float|None]=mapped_column(Float, nullable=True)
    source:Mapped[str]=mapped_column(String(80), default='mock_dcsa'); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class RFQ(Base):
    __tablename__='rfqs'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    inquiry_id:Mapped[str]=mapped_column(String(64), ForeignKey('inquiries.id'), index=True)
    status:Mapped[str]=mapped_column(String(40), default='open'); expected_suppliers:Mapped[int]=mapped_column(Integer, default=0)
    received_quotes:Mapped[int]=mapped_column(Integer, default=0); expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class SupplierQuote(Base):
    __tablename__='supplier_quotes'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    rfq_id:Mapped[str]=mapped_column(String(64), ForeignKey('rfqs.id'), index=True); supplier_name:Mapped[str]=mapped_column(String(160))
    supplier_email:Mapped[str]=mapped_column(String(200)); carrier:Mapped[str|None]=mapped_column(String(80), nullable=True)
    currency:Mapped[str]=mapped_column(String(8), default='USD'); ocean_freight:Mapped[float]=mapped_column(Float)
    surcharges:Mapped[float]=mapped_column(Float, default=0); transit_days:Mapped[int|None]=mapped_column(Integer, nullable=True)
    free_time_days:Mapped[int|None]=mapped_column(Integer, nullable=True); validity:Mapped[str|None]=mapped_column(String(32), nullable=True)
    reliability_score:Mapped[float]=mapped_column(Float, default=0.8); raw_payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
    __table_args__=(UniqueConstraint('tenant_id','rfq_id','supplier_email',name='uq_supplier_quote'),)

class CustomerQuote(Base):
    __tablename__='customer_quotes'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    inquiry_id:Mapped[str]=mapped_column(String(64), ForeignKey('inquiries.id'), index=True)
    supplier_quote_id:Mapped[str]=mapped_column(String(64), ForeignKey('supplier_quotes.id'))
    currency:Mapped[str]=mapped_column(String(8), default='USD'); cost_amount:Mapped[float]=mapped_column(Float)
    margin_amount:Mapped[float]=mapped_column(Float); sell_amount:Mapped[float]=mapped_column(Float)
    status:Mapped[str]=mapped_column(String(40), default='waiting_approval'); rationale:Mapped[str]=mapped_column(Text)
    customer_message:Mapped[str|None]=mapped_column(Text, nullable=True); valid_until:Mapped[str|None]=mapped_column(String(32), nullable=True)
    approved_by:Mapped[str|None]=mapped_column(String(160), nullable=True); approved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    sent_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Approval(Base):
    __tablename__='approvals'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    object_type:Mapped[str]=mapped_column(String(40)); object_id:Mapped[str]=mapped_column(String(64), index=True)
    action:Mapped[str]=mapped_column(String(20)); reviewer:Mapped[str]=mapped_column(String(160)); comment:Mapped[str|None]=mapped_column(Text, nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Booking(Base):
    __tablename__='bookings'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    inquiry_id:Mapped[str]=mapped_column(String(64), ForeignKey('inquiries.id')); quote_id:Mapped[str]=mapped_column(String(64), ForeignKey('customer_quotes.id'))
    provider_ref:Mapped[str|None]=mapped_column(String(120), nullable=True); status:Mapped[str]=mapped_column(String(40), default='requested')
    payload:Mapped[dict]=mapped_column(JSON, default=dict); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class TrackingEvent(Base):
    __tablename__='tracking_events'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    booking_id:Mapped[str]=mapped_column(String(64), ForeignKey('bookings.id'), index=True); event_code:Mapped[str]=mapped_column(String(80))
    event_time:Mapped[str]=mapped_column(String(40)); location:Mapped[str|None]=mapped_column(String(160), nullable=True)
    description:Mapped[str|None]=mapped_column(Text, nullable=True); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class FollowUpTask(Base):
    __tablename__='followup_tasks'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    customer_id:Mapped[str]=mapped_column(String(64), ForeignKey('customers.id')); inquiry_id:Mapped[str|None]=mapped_column(String(64), nullable=True)
    due_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True); kind:Mapped[str]=mapped_column(String(60)); status:Mapped[str]=mapped_column(String(40), default='pending')
    payload:Mapped[dict]=mapped_column(JSON, default=dict); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class AgentRun(Base):
    __tablename__='agent_runs'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    agent_name:Mapped[str]=mapped_column(String(100)); trace_id:Mapped[str]=mapped_column(String(64), index=True); input_summary:Mapped[str]=mapped_column(Text)
    output_summary:Mapped[str|None]=mapped_column(Text, nullable=True); status:Mapped[str]=mapped_column(String(40)); latency_ms:Mapped[int]=mapped_column(Integer, default=0)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class AuditEvent(Base):
    __tablename__='audit_events'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    actor:Mapped[str]=mapped_column(String(160)); event_type:Mapped[str]=mapped_column(String(100)); object_type:Mapped[str]=mapped_column(String(60))
    object_id:Mapped[str]=mapped_column(String(64)); payload:Mapped[dict]=mapped_column(JSON, default=dict); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class OutboxEvent(Base):
    __tablename__='outbox_events'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    event_type:Mapped[str]=mapped_column(String(100)); aggregate_id:Mapped[str]=mapped_column(String(64)); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    status:Mapped[str]=mapped_column(String(30), default='pending'); attempts:Mapped[int]=mapped_column(Integer, default=0)
    next_attempt_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, index=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class CRMActivity(Base):
    __tablename__='crm_activities'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    customer_id:Mapped[str]=mapped_column(String(64), ForeignKey('customers.id'), index=True); activity_type:Mapped[str]=mapped_column(String(60))
    channel:Mapped[str]=mapped_column(String(40), default='email'); summary:Mapped[str]=mapped_column(Text); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class RateSnapshot(Base):
    __tablename__='rate_snapshots'
    id:Mapped[str]=mapped_column(String(64), primary_key=True); tenant_id:Mapped[str]=mapped_column(String(64), index=True)
    inquiry_id:Mapped[str]=mapped_column(String(64), ForeignKey('inquiries.id'), index=True); source:Mapped[str]=mapped_column(String(80))
    carrier:Mapped[str|None]=mapped_column(String(80), nullable=True); currency:Mapped[str]=mapped_column(String(8), default='USD')
    base_rate:Mapped[float]=mapped_column(Float); surcharges:Mapped[float]=mapped_column(Float, default=0); total_cost:Mapped[float]=mapped_column(Float)
    validity:Mapped[str|None]=mapped_column(String(32), nullable=True); payload:Mapped[dict]=mapped_column(JSON, default=dict)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
