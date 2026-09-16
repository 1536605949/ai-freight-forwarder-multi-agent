import pytest
from app.models import Customer,Inquiry,Booking,CustomerQuote,FollowUpTask,VesselPositionSnapshot
from app.services import vessel as vs


def _seed_booking(db,do_not_contact=False):
    db.add(Customer(id='c1',tenant_id='tenant-demo',company='Acme',email='a@acme.com',
        preferences={'do_not_contact':True} if do_not_contact else {}))
    db.add(Inquiry(id='q1',tenant_id='tenant-demo',customer_id='c1',raw_message='x',status='booked'))
    db.add(CustomerQuote(id='cq1',tenant_id='tenant-demo',inquiry_id='q1',supplier_quote_id='sq1',
        cost_amount=100,margin_amount=20,sell_amount=120,status='sent',rationale='r'))
    db.add(Booking(id='b1',tenant_id='tenant-demo',inquiry_id='q1',quote_id='cq1',status='confirmed'))
    db.commit()


@pytest.mark.asyncio
async def test_position_is_deterministic(db):
    a=await vs.refresh_position(db,'tenant-demo','u','MV EVER GIVEN','024E')
    b=await vs.refresh_position(db,'tenant-demo','u','MV EVER GIVEN','024E')
    assert a['position']['latitude']==b['position']['latitude']
    assert a['position']['longitude']==b['position']['longitude']
    assert a['position']['mmsi']==b['position']['mmsi']


@pytest.mark.asyncio
async def test_position_persisted_and_history(db):
    await vs.refresh_position(db,'tenant-demo','u','MV HANOVER EXPRESS','001W')
    await vs.refresh_position(db,'tenant-demo','u','MV HANOVER EXPRESS','001W')
    h=vs.position_history(db,'tenant-demo','MV HANOVER EXPRESS')
    assert len(h)==2
    assert db.query(VesselPositionSnapshot).filter_by(tenant_id='tenant-demo').count()==2


@pytest.mark.asyncio
async def test_tenant_isolation(db):
    await vs.refresh_position(db,'tenant-demo','u','MV ISOLATED','1')
    assert vs.position_history(db,'tenant-other','MV ISOLATED')==[]


@pytest.mark.asyncio
async def test_delay_alert_raised_for_booking(db):
    _seed_booking(db)
    # Find a vessel/voyage whose synthetic delay exceeds the threshold.
    found=None
    for i in range(60):
        r=await vs.refresh_position(db,'tenant-demo','u',f'MV TEST {i}','V1',booking_id='b1')
        if r['delayed']: found=r; break
    assert found is not None,'expected at least one synthetic vessel to be delayed'
    assert found['alert'] and found['alert'].get('task_id')
    t=db.query(FollowUpTask).filter_by(kind='vessel_delay').first()
    assert t is not None and t.payload['booking_id']=='b1'


@pytest.mark.asyncio
async def test_delay_alert_deduplicated(db):
    _seed_booking(db)
    first=None
    for i in range(60):
        r=await vs.refresh_position(db,'tenant-demo','u',f'MV DEDUP {i}','V1',booking_id='b1')
        if r['delayed']: first=r; break
    assert first is not None
    # Re-polling the same delayed booking must not spawn a second task.
    await vs.refresh_position(db,'tenant-demo','u',first['position']['vessel_name'],'V1',booking_id='b1')
    assert db.query(FollowUpTask).filter_by(kind='vessel_delay').count()==1


@pytest.mark.asyncio
async def test_delay_alert_suppressed_for_do_not_contact(db):
    _seed_booking(db,do_not_contact=True)
    for i in range(60):
        r=await vs.refresh_position(db,'tenant-demo','u',f'MV DNC {i}','V1',booking_id='b1')
        if r['delayed']:
            assert r['alert'].get('suppressed') is True
            assert db.query(FollowUpTask).filter_by(kind='vessel_delay').count()==0
            return
    pytest.fail('expected a delayed synthetic vessel')


@pytest.mark.asyncio
async def test_no_booking_means_no_customer_alert(db):
    for i in range(60):
        r=await vs.refresh_position(db,'tenant-demo','u',f'MV NOBKG {i}','V1')
        if r['delayed']:
            # Without a booking there is no customer to notify; we must not invent one.
            assert r['alert'] is None
            return
    pytest.fail('expected a delayed synthetic vessel')


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_booking(db):
    with pytest.raises(KeyError,match='booking_not_found'):
        await vs.refresh_position(db,'tenant-demo','u','MV X','1',booking_id='nope')
