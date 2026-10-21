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


# --- Importing a purchased list ---------------------------------------------------------
# The mock source is a demo convenience. Real acquisition starts with a vendor export, and
# until this path existed the module could not accept one at all -- "pluggable data source"
# described an interface with a single implementation and no way to feed it.

def _row(**kw):
    base={'company':'Meridian Trading','country':'United States','industry':'Auto Parts',
          'est_volume_teu':'420','email':'buyer@meridian-trading.test','trade_lane':'Shanghai -> Los Angeles'}
    base.update(kw); return base


def test_import_stores_good_rows_and_says_why_it_dropped_the_rest(db):
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='vendor_contract_2026',
        contract_ref='TS-2026-0142',rows=[_row(),
            _row(company='',email='x@y.test'),                 # no company
            _row(company='Northfield Ltd',email='not-an-email'),  # bad email
            _row(company='Cobalt Sourcing',email='ops@cobalt.test',est_volume_teu='n/a')])

    assert out['accepted']==2, out['reasons']
    assert out['rejected']==2
    assert out['reasons']=={'missing_company':1,'bad_email':1}
    # A bad volume is a parse miss, not a reason to drop a real company.
    cobalt=[p for p in out['prospects'] if p['company']=='Cobalt Sourcing'][0]
    assert cobalt['est_volume_teu'] is None


def test_reimporting_the_same_export_is_a_no_op(db):
    first=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c1',rows=[_row(),_row(company='Keystone Ltd')])
    assert first['accepted']==2
    again=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c1',rows=[_row(),_row(company='Keystone Ltd')])
    assert again['accepted']==0 and again['reasons']=={'duplicate':2}
    assert db.query(ProspectCandidate).filter_by(tenant_id='tenant-demo').count()==2


def test_the_same_shipper_from_two_vendors_is_one_prospect(db):
    """Two vendors, same company, same email -- one company. Same company, new email -- a
    second contact, kept on purpose."""
    ps.import_prospects(db,'tenant-demo','u',source='vendor_a',data_origin='c-a',rows=[_row()])
    other=ps.import_prospects(db,'tenant-demo','u',source='vendor_b',data_origin='c-b',
        rows=[_row(), _row(email='logistics@meridian-trading.test')])
    assert other['accepted']==1, 'the identical company+email must not be imported twice'
    assert other['reasons']=={'duplicate':1}
    assert other['prospects'][0]['email']=='logistics@meridian-trading.test'


def test_csv_import_reads_the_header_row(db):
    content=('Company,Country,Industry,Est Volume TEU,Email,Trade Lane\n'
             'Larkspur Industrial,Germany,Machinery,380,buyer@larkspur.test,Shanghai -> Hamburg\n'
             'Bluestem Mfg,Netherlands,Furniture,120,,\n')
    rows=ps.parse_csv(content)
    out=ps.import_prospects(db,'tenant-demo','u',source='export_csv',data_origin='c2',rows=rows)
    assert out['accepted']==2
    lark=[p for p in out['prospects'] if p['company']=='Larkspur Industrial'][0]
    assert lark['est_volume_teu']==380.0 and lark['email']=='buyer@larkspur.test'
    assert lark['trade_lane']=='Shanghai -> Hamburg'


def test_csv_headers_are_mapped_not_guessed():
    """A vendor's column names are not ours. Without a map the volume imports as None and
    then scores as a low-volume lead -- a wrong answer that looks like a data problem."""
    rows=ps.parse_csv('公司,国家,行业,货量,邮箱,航线\n铁杉贸易,德国,机械,260,ops@tsuga.test,上海 -> 汉堡\n')
    assert rows[0]['company']=='铁杉贸易' and rows[0]['est_volume_teu']=='260'
    assert rows[0]['trade_lane']=='上海 -> 汉堡'

    short=ps.parse_csv('shipper,market,teu,mail\nHarborline Ltd,US,410,ops@harborline.test\n')
    assert short[0]['company']=='Harborline Ltd' and short[0]['est_volume_teu']=='410'
    assert short[0]['email']=='ops@harborline.test'


@pytest.mark.asyncio
async def test_an_imported_list_cannot_be_emailed_until_it_is_scored(db):
    """The link that makes buying a list safe: import does not confer permission to send.

    Even with a lawful basis recorded and a human approval on file, an unscored row stops at
    the score floor -- which is exactly what an unscored list is: unqualified.
    """
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c3',rows=[_row()])
    p=db.query(ProspectCandidate).filter_by(id=out['prospects'][0]['id']).first()
    assert p.status=='imported' and p.score==0

    p.consent_basis='legitimate_interest_reviewed'; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',p.id)
    ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')

    with pytest.raises(ValueError,match='score_below_outreach_threshold'):
        await ps.send_outreach(db,'tenant-demo','u',o.id)


@pytest.mark.asyncio
async def test_scoring_makes_a_good_row_sendable_and_rejects_a_bad_one(db):
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c4',rows=[
        _row(company='Aurora Trading'),
        _row(company='Cardinal Bulk',industry='Bulk Chemicals',est_volume_teu='0',
             email='ops@cardinal.test')])
    good,bad=[x['id'] for x in out['prospects']]

    scored=await ps.score_prospect(db,'tenant-demo','u','t',good)
    assert scored['verdict']=='qualified' and scored['score']>=70
    assert scored['prospect']['status']=='discovered'

    # An agent verdict of "not worth contacting" is recorded as rejected, not left at 0.
    dropped=await ps.score_prospect(db,'tenant-demo','u','t',bad)
    assert dropped['verdict']=='excluded'
    assert dropped['prospect']['status']=='rejected'
    assert dropped['rationale']


@pytest.mark.asyncio
async def test_a_scored_row_actually_goes_out(db):
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c5',rows=[_row(company='Vantage Import')])
    pid=out['prospects'][0]['id']
    await ps.score_prospect(db,'tenant-demo','u','t',pid)

    p=db.query(ProspectCandidate).filter_by(id=pid).first()
    p.consent_basis='legitimate_interest_reviewed'; db.commit()
    o=await ps.create_outreach_draft(db,'tenant-demo','u','t',pid)
    ps.approve_outreach(db,'tenant-demo','reviewer',o.id,'approve')
    sent,_=await ps.send_outreach(db,'tenant-demo','u',o.id)
    assert sent.status=='sent'


def test_import_is_tenant_scoped(db):
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c6',rows=[_row()])
    pid=out['prospects'][0]['id']
    # The same list imported by another tenant is theirs, not a duplicate of ours.
    theirs=ps.import_prospects(db,'tenant-other','u',source='tradesns',data_origin='c6',rows=[_row()])
    assert theirs['accepted']==1
    assert db.query(ProspectCandidate).filter_by(tenant_id='tenant-other').count()==1
    with pytest.raises(KeyError,match='prospect_not_found'):
        ps.prospect_evidence(db,'tenant-other',pid)


def test_import_requires_a_named_source_and_origin():
    """A list nobody can attribute is the list that cannot be lawfully emailed."""
    from pydantic import ValidationError
    from app.schemas import ProspectImportIn
    for bad in ({'source':'','data_origin':'c','rows':[{}]},
                {'source':'tradesns','data_origin':'','rows':[{}]},
                {'source':'tradesns','data_origin':'c'}):
        with pytest.raises(ValidationError):
            ProspectImportIn(**bad)


def test_scoring_treats_chinese_and_english_industries_alike():
    """Found by importing a Chinese CSV, not by reading the code.

    The heuristic matched English industry words only, so 机械 scored 25 points below
    Machinery -- and then printed "行业 机械 与集装箱航线匹配" next to the lower number.
    A rationale that contradicts its own score is worse than a wrong score: nobody questions it.
    """
    import json
    from app.agents.runtime import _mock_prospecting

    def score(industry):
        payload={'lane':'Shanghai -> Los Angeles','candidates':[{
            'company':'X','country':'Germany','industry':industry,'est_volume_teu':260}]}
        return _mock_prospecting(json.dumps(payload,ensure_ascii=False))['qualified'][0]

    zh,en=score('机械'),score('Machinery')
    assert zh['fit_score']==en['fit_score']==82.0, (zh['fit_score'],en['fit_score'])
    assert '与集装箱航线匹配' in zh['reason']

    # A bulk-only industry must not be described as a container match.
    bulk=score('Bulk Chemicals')
    assert bulk['fit_score']==57.0
    assert '与集装箱航线匹配' not in bulk['reason'], bulk['reason']


def test_a_list_records_how_old_the_data_was(db):
    """"Updates daily" is a claim about the feed. The row has to carry the date it was
    current to, or nobody can check the claim later."""
    out=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c7',
        data_as_of='2026-08-01',rows=[_row()])
    assert out['data_as_of']=='2026-08-01'
    e=ps.prospect_evidence(db,'tenant-demo',out['prospects'][0]['id'])
    assert e['data_as_of']=='2026-08-01'
    assert e['age_days']>=0

    # A missing or nonsensical date is None, never a flattering zero.
    fresh=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c8',
        rows=[_row(company='No Date Ltd')])
    e2=ps.prospect_evidence(db,'tenant-demo',fresh['prospects'][0]['id'])
    assert e2['data_as_of'] is None and e2['age_days'] is None

    future=ps.import_prospects(db,'tenant-demo','u',source='tradesns',data_origin='c9',
        data_as_of='2099-01-01',rows=[_row(company='Future Ltd')])
    e3=ps.prospect_evidence(db,'tenant-demo',future['prospects'][0]['id'])
    assert e3['age_days'] is None, 'a future date is a data error, not freshness'
