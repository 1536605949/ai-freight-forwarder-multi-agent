import pytest
from app.models import ProspectCandidate
from app.services import prospecting as ps


@pytest.mark.asyncio
async def test_search_is_deterministic_and_idempotent(db):
    a=await ps.search_prospects(db,'tenant-demo','u','t1','Shanghai -> Los Angeles')
    assert a['discovered']>0 and a['qualified']>0
    first=a['prospects'][0]['id']
    # Re-running the same search must not duplicate rows.
    b=await ps.search_prospects(db,'tenant-demo','u','t2','Shanghai -> Los Angeles')
    assert b['qualified']==a['qualified']
    ids=[x['id'] for x in b['prospects']]
    assert len(ids)==len(set(ids))
    assert first in ids


@pytest.mark.asyncio
async def test_dry_run_persists_nothing(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Rotterdam',dry_run=True)
    assert r['dry_run'] is True and r['discovered']>0
    assert db.query(ProspectCandidate).filter_by(tenant_id='tenant-demo').count()==0


@pytest.mark.asyncio
async def test_tenant_isolation(db):
    await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    r=await ps.search_prospects(db,'tenant-other','u','t','Shanghai -> Los Angeles')
    assert r['discovered']>0
    assert db.query(ProspectCandidate).filter_by(tenant_id='tenant-demo').count()>0
    for p in r['prospects']:
        assert db.query(ProspectCandidate).filter_by(id=p['id'],tenant_id='tenant-demo').first() is None


@pytest.mark.asyncio
async def test_outreach_requires_approval(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first(); p.email='buyer@example.test'; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
    assert o.status=='draft'
    with pytest.raises(ValueError,match='approval_required'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)


@pytest.mark.asyncio
async def test_outreach_requires_lawful_basis(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first(); p.email='buyer@example.test'; p.consent_basis=None; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
    ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')
    with pytest.raises(ValueError,match='compliance_basis_required'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)


@pytest.mark.asyncio
async def test_frequency_cap(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first()
    p.email='buyer@example.test'; p.consent_basis='legitimate_interest_reviewed'; db.commit()
    sent=0
    for _ in range(3):
        o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
        ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')
        try:
            await ps.send_outreach(db,'tenant-demo','u',o.id); sent+=1
        except ValueError as e:
            assert 'frequency_cap_exceeded' in str(e); break
    assert sent==2


@pytest.mark.asyncio
async def test_opt_out_blocks_everything(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first()
    p.email='buyer@example.test'; p.consent_basis='legitimate_interest_reviewed'; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
    ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')
    affected=ps.mark_opt_out(db,'tenant-demo','u','buyer@example.test','replied STOP')
    assert affected['outreach']==1
    db.refresh(o); assert o.status=='suppressed'
    with pytest.raises(ValueError,match='approval_required'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)


@pytest.mark.asyncio
async def test_promote_requires_human_consent_decision(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first(); p.email='buyer@example.test'; db.commit()
    lead=ps.promote_to_lead(db,'tenant-demo','reviewer',pid,'legitimate_interest_reviewed','公开企业信息，已做合法利益评估')
    assert lead.origin_prospect_id==pid
    assert lead.data_origin=='mock_synthetic'
    db.refresh(p); assert p.status=='promoted' and p.lead_id==lead.id
    # Second promotion is idempotent.
    again=ps.promote_to_lead(db,'tenant-demo','reviewer',pid,'legitimate_interest_reviewed',None)
    assert again.id==lead.id


@pytest.mark.asyncio
async def test_promote_rejects_bad_consent_status(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    with pytest.raises(ValueError,match='invalid_consent_status'):
        ps.promote_to_lead(db,'tenant-demo','reviewer',pid,'whatever','x')


@pytest.mark.asyncio
async def test_a_low_score_blocks_the_send_but_not_the_draft(db):
    """Drafting harms nobody; sending does. The floor applies at the door, not at the desk.

    This is the borrowed half of "fit_score >= 7 keeps outreach small": a numeric floor is
    what stops a demo from becoming a spam run, and it has to live in code, not in a prompt.
    """
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first()
    p.email='buyer@example.test'; p.consent_basis='legitimate_interest_reviewed'; p.score=30; db.commit()

    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
    assert o.status=='draft', 'a low score must not stop the draft from existing'
    ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')

    with pytest.raises(ValueError,match='score_below_outreach_threshold'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)
    db.refresh(o); assert o.status=='approved', 'a blocked send must not mark the row as sent'


@pytest.mark.asyncio
async def test_authorization_is_checked_before_quality(db):
    """A low-score *unapproved* draft must fail on approval, not on score.

    Order matters for the operator: "you have not approved this" is actionable, "the score
    is low" is not, and reporting the second when the first is true sends them down the
    wrong path.
    """
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    p=db.query(ProspectCandidate).filter_by(id=pid).first()
    p.email='buyer@example.test'; p.score=10; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)

    with pytest.raises(ValueError,match='approval_required'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)


@pytest.mark.asyncio
async def test_evidence_admits_synthetic_data_cannot_be_checked(db):
    """The mock source writes `https://www.<slug>.example`. That URL never resolves.

    Saying so is the point: a "source URL" field that everyone assumes is openable is worse
    than an explicit "unverifiable", because it survives review without being checked.
    """
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    e=ps.prospect_evidence(db,'tenant-demo',pid)

    assert e['verifiability']=='synthetic'
    assert e['human_can_check'] is False
    assert '合成' in e['note']
    sites=[s for s in e['sources'] if s['kind']=='company_site']
    assert sites, 'the mock source does supply a website; it just cannot be opened'
    for s in sites:
        assert s['url'] is None, 'a reserved domain must not be handed out as a link'
        assert '无法解析' in s['note']


def test_a_reserved_domain_is_not_evidence():
    """Direct unit check on the predicate, independent of what the mock source happens to emit."""
    assert ps._is_publicly_openable('https://www.meridian.example') is False
    assert ps._is_publicly_openable('https://foo.test') is False
    assert ps._is_publicly_openable('http://localhost:8000') is False
    assert ps._is_publicly_openable('') is False
    assert ps._is_publicly_openable(None) is False
    assert ps._is_publicly_openable('https://www.meridian-trading.com') is True


@pytest.mark.asyncio
async def test_evidence_is_tenant_scoped(db):
    r=await ps.search_prospects(db,'tenant-demo','u','t','Shanghai -> Los Angeles')
    pid=r['prospects'][0]['id']
    with pytest.raises(KeyError,match='prospect_not_found'):
        ps.prospect_evidence(db,'tenant-other',pid)
