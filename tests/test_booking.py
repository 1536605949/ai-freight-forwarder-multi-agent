"""Booking is the second irreversible commitment in the workflow, so its gates get real tests.

Two gates protect it, and they are deliberately independent:
  * the quote must be `sent` (service layer, `booking.create_booking`)
  * the inquiry must be in a state from which `booked` is reachable (state graph)
"""
from datetime import datetime, timezone

import pytest

from app.models import CustomerQuote, Inquiry, RFQ, SupplierQuote
from app.services.booking import create_booking
from app.state_machine import IllegalTransition


def _seed(db, *, quote_status: str, inquiry_status: str):
    """An inquiry + RFQ + supplier quote + customer quote.

    `inquiry_status` is a parameter rather than a constant so a test can deliberately build
    an *unreachable* combination and assert it is refused.
    """
    db.add_all([
        Inquiry(id='i', tenant_id='tenant-demo', raw_message='x', missing_fields=[],
                status=inquiry_status),
        RFQ(id='r', tenant_id='tenant-demo', inquiry_id='i', expected_suppliers=1,
            expires_at=datetime.now(timezone.utc)),
        SupplierQuote(id='s', tenant_id='tenant-demo', rfq_id='r', supplier_name='a',
                      supplier_email='a@x', ocean_freight=1),
        CustomerQuote(id='q', tenant_id='tenant-demo', inquiry_id='i', supplier_quote_id='s',
                      cost_amount=1, margin_amount=1, sell_amount=2, status=quote_status,
                      rationale='x'),
    ])
    db.commit()


@pytest.mark.asyncio
async def test_booking_requires_sent_quote(db):
    # The inquiry sits in `sent`, because that is the only state a sent quote can coexist
    # with -- `approval.send_quote` advances both together.
    _seed(db, quote_status='approved', inquiry_status='sent')

    with pytest.raises(ValueError):
        await create_booking(db, 'tenant-demo', 'u', 'i', 'q', {})

    q = db.query(CustomerQuote).filter_by(id='q').one()
    q.status = 'sent'
    db.commit()

    b, idem = await create_booking(db, 'tenant-demo', 'u', 'i', 'q', {})
    assert b.status == 'confirmed' and not idem

    # Re-booking is idempotent rather than a second booking.
    b2, idem2 = await create_booking(db, 'tenant-demo', 'u', 'i', 'q', {})
    assert idem2 and b2.id == b.id


@pytest.mark.asyncio
async def test_booking_refuses_unreachable_inquiry_state(db):
    """A `sent` quote on an `approved` inquiry cannot exist in the real flow.

    Before the state graph existed this combination silently produced a booking and left the
    inquiry in an impossible history. It is now refused loudly.
    """
    _seed(db, quote_status='sent', inquiry_status='approved')

    with pytest.raises(IllegalTransition):
        await create_booking(db, 'tenant-demo', 'u', 'i', 'q', {})
