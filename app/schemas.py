from pydantic import BaseModel, EmailStr, Field
from typing import Any

class LeadCreate(BaseModel):
    company:str; contact_name:str|None=None; email:EmailStr|None=None; country:str|None=None; industry:str|None=None; source:str='manual'; trade_lane:str|None=None; consent_status:str='unknown'; notes:str|None=None
class InboundMessage(BaseModel):
    external_message_id:str; sender_email:EmailStr; sender_name:str|None=None; subject:str=''; body:str
class InquiryPatch(BaseModel):
    origin:str|None=None; destination:str|None=None; equipment:str|None=None; quantity:int|None=Field(default=None,ge=1,le=1000); etd:str|None=None; commodity:str|None=None; weight_kg:float|None=Field(default=None,ge=0); incoterm:str|None=None
class SupplierQuoteIn(BaseModel):
    supplier_name:str; supplier_email:EmailStr; carrier:str|None=None; currency:str='USD'; ocean_freight:float=Field(gt=0); surcharges:float=Field(default=0,ge=0); transit_days:int|None=Field(default=None,ge=1); free_time_days:int|None=Field(default=None,ge=0); validity:str|None=None; reliability_score:float=Field(default=0.8,ge=0,le=1); raw_payload:dict[str,Any]={}
class ApprovalIn(BaseModel):
    action:str=Field(pattern='^(approve|reject)$'); comment:str|None=None
class BookingCreate(BaseModel):
    inquiry_id:str; quote_id:str; cargo_details:dict[str,Any]={}
class TrackingWebhook(BaseModel):
    booking_id:str; event_code:str; event_time:str; location:str|None=None; description:str|None=None; payload:dict[str,Any]={}
class FollowUpCreate(BaseModel):
    customer_id:str; inquiry_id:str|None=None; due_at:str; kind:str='sales_followup'; payload:dict[str,Any]={}

class CRMActivityCreate(BaseModel):
    customer_id:str; activity_type:str; channel:str='email'; summary:str; payload:dict[str,Any]={}
class SupplierInboundEmail(BaseModel):
    rfq_id:str; supplier_name:str; supplier_email:EmailStr; body:str; carrier:str|None=None

class ProspectSearchIn(BaseModel):
    lane:str=Field(min_length=2,max_length=160); industry:str|None=None
    limit:int|None=Field(default=None,ge=1,le=200); dry_run:bool=False

class PromoteIn(BaseModel):
    consent_status:str=Field(pattern='^(opt_in|legitimate_interest_reviewed|unknown)$')
    consent_basis:str|None=None

class OutreachDraftIn(BaseModel):
    subject:str|None=Field(default=None,max_length=300)

class OutreachApproveIn(BaseModel):
    action:str=Field(pattern='^(approve|reject)$'); comment:str|None=None

class OptOutIn(BaseModel):
    email:EmailStr; reason:str|None=None

class OrchestrateIn(BaseModel):
    """Start-or-resume input for the unified pipeline.

    `inquiry_id` is required: the pipeline operates on an inquiry that already exists, so
    there is exactly one way an inquiry enters the system (the inbound-message endpoint)
    rather than two competing ones.
    """
    inquiry_id:str
    # Resume from a stage instead of the beginning. Validated against STAGES at the route.
    start_stage:str|None=None
    # Demo/test convenience: seed synthetic supplier quotes. Real deployments receive these
    # through the supplier-mailbox webhook instead.
    auto_supplier_quotes:bool=True
    # Demo/test only. Skips the human approval gate. Must be explicitly requested.
    auto_approve:bool=False
    vessel_name:str|None=None

class OrchestrateContinueIn(BaseModel):
    """Continue after a human decision at a gate."""
    quote_id:str
    vessel_name:str|None=None

