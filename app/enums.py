from enum import StrEnum

class InquiryStatus(StrEnum):
    NEEDS_CLARIFICATION='needs_clarification'
    READY='ready'
    SCHEDULED='scheduled'
    WAITING_SUPPLIER_QUOTES='waiting_supplier_quotes'
    QUOTE_READY='quote_ready'
    WAITING_APPROVAL='waiting_approval'
    APPROVED='approved'
    REJECTED='rejected'
    SENT='sent'
    BOOKED='booked'
    CLOSED='closed'

class QuoteStatus(StrEnum):
    DRAFT='draft'; WAITING_APPROVAL='waiting_approval'; APPROVED='approved'; REJECTED='rejected'; SENT='sent'; EXPIRED='expired'

class BookingStatus(StrEnum):
    REQUESTED='requested'; CONFIRMED='confirmed'; REJECTED='rejected'; CANCELLED='cancelled'

class LeadStatus(StrEnum):
    NEW='new'; QUALIFIED='qualified'; NURTURING='nurturing'; CONVERTED='converted'; DO_NOT_CONTACT='do_not_contact'
