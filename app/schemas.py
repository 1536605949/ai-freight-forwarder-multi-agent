from pydantic import BaseModel, EmailStr, Field, model_validator
from typing import Any, Literal

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

class ProspectImportIn(BaseModel):
    """A purchased or exported list, not a search result.

    `source` and `data_origin` are required and non-trivial: a list whose provenance nobody
    can name is exactly the list that cannot be lawfully emailed, so it is refused at the
    door rather than stored and sorted out later.
    """
    source:str=Field(min_length=2,max_length=80)
    data_origin:str=Field(min_length=2,max_length=80)
    contract_ref:str|None=Field(default=None,max_length=200)
    # The date the vendor says the data is current to, not the date you imported it.
    # "Updates daily" is a claim about the feed; this field is what makes it checkable.
    data_as_of:str|None=Field(default=None,max_length=32)
    format:Literal['json','csv']='json'
    rows:list[dict[str,Any]]|None=None
    content:str|None=None
    default_lane:str|None=Field(default=None,max_length=160)
    # Customs rows carry two parties. Which one is the lead depends on what you sell, so the
    # caller says it instead of the importer guessing from column order.
    entity_side:Literal['exporter','importer']='exporter'

    @model_validator(mode='after')
    def _payload_matches_format(self):
        if self.format=='json' and not self.rows: raise ValueError('rows_required_for_json')
        if self.format=='csv' and not (self.content or '').strip(): raise ValueError('content_required_for_csv')
        return self

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
    # through the supplier-mailbox webhook instead. `None` = decide by environment: on for
    # demo/test (so the pipeline is runnable), forced off in production.
    auto_supplier_quotes:bool|None=None
    # Demo/test only. Skips the human approval gate. Refused outright in production.
    auto_approve:bool=False
    vessel_name:str|None=None

class OrchestrateContinueIn(BaseModel):
    """Continue after a human decision at a gate."""
    quote_id:str
    vessel_name:str|None=None

class KnowledgeDocumentIn(BaseModel):
    title:str=Field(min_length=1,max_length=300)
    text:str=Field(min_length=1)
    source:str='manual'
    version:str='1'

class KnowledgeQueryIn(BaseModel):
    query:str=Field(min_length=1)
    # Optional override of the configured top-k. Bounded so a caller cannot ask for the
    # whole corpus and turn retrieval into a dump.
    k:int|None=Field(default=None,ge=1,le=20)

class GraphRunIn(BaseModel):
    """Start the LangGraph pipeline on an existing inquiry."""
    inquiry_id:str
    start_node:str='understand'
    vessel_name:str|None=None
    # Demo only. Ignored in production -- see graph/runner._effective_demo_shortcuts.
    allow_synthetic_quotes:bool|None=None

class GraphResumeIn(BaseModel):
    """Re-enter a halted graph run at the node it stopped at.

    No `start_node` here on purpose: the resume point is derived from persisted state, so a
    client cannot ask to skip a gate by naming the node after it.
    """
    inquiry_id:str
    vessel_name:str|None=None

class AgentAskIn(BaseModel):
    """Ask the tool-calling agent a question.

    `inquiry_id` is optional and only a hint: the policy also reads it from the question
    text, and every tool re-checks it against the caller's tenant.
    """
    question:str=Field(min_length=1)
    inquiry_id:str|None=None

