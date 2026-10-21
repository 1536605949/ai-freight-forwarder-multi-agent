"""Scheduled collection: the cursor must advance, and robots.txt must actually block.

Three properties are tested here because each one fails silently in production:

1. **The cursor advances.** If it does not, every poll re-reads page 1 and dedupe reports
   `accepted=0` forever. The job looks healthy and produces nothing.
2. **robots.txt is enforced, and fails closed.** "We could not read robots.txt" is not
   "we may crawl"; treating it as permission turns a compliance control into a comment.
3. **Contact gaps are reported.** A directory gives company names, not freight bookers.
   Importing 40 rows with zero emails and calling it success is how a team ends up paying
   for a list nobody can use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.providers.fieldmap import HEADER_ALIASES, field_name
from app.models import ProspectCandidate, SourceRun
from app.providers.prospecting import (
    DirectoryProspectingSource,
    MockProspectingSource,
    SourceFetchError,
    parse_directory_html,
)
from app.services import sourcing
from app.services.prospecting import _field_name as service_field_name


@pytest.fixture
def sources_env(monkeypatch):
    """Point the scheduler at a source list, and clear the settings cache afterwards."""
    def apply(raw: str):
        monkeypatch.setenv('PROSPECTING_SOURCES', raw)
        get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


def _fetcher(pages: dict[str, tuple[int, str]]):
    async def fetch(url: str, user_agent: str) -> tuple[int, str]:
        return pages.get(url, (404, ''))
    return fetch


# --------------------------------------------------------------------------- #
# Spec parsing
# --------------------------------------------------------------------------- #

def test_parse_sources_reads_kind_name_url_and_lane():
    specs = sourcing.parse_sources(
        'mock|demo|, directory|canton|https://x.test/list?page={page}|Shanghai -> Los Angeles')
    assert [(s.kind, s.name, s.url, s.lane) for s in specs] == [
        ('mock', 'demo', None, None),
        ('directory', 'canton', 'https://x.test/list?page={page}', 'Shanghai -> Los Angeles'),
    ]


def test_parse_sources_keeps_a_malformed_entry_so_it_fails_loudly():
    """Dropping it would leave a configured source that never runs and never complains."""
    specs = sourcing.parse_sources('nonsense')
    assert len(specs) == 1
    with pytest.raises(ValueError):
        sourcing.build_source(specs[0].kind, specs[0].name, specs[0].url)


@pytest.mark.asyncio
async def test_empty_configuration_collects_nothing(db, sources_env):
    sources_env('')
    assert await sourcing.run_due_sources(db) == []


# --------------------------------------------------------------------------- #
# The cursor: the reason source_runs exists
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_mock_source_paginates_and_reports_exhaustion():
    source = MockProspectingSource()
    first = await source.fetch(cursor=None, limit=3, lane='Shanghai -> Los Angeles')
    second = await source.fetch(cursor=first.next_cursor, limit=3, lane='Shanghai -> Los Angeles')

    assert len(first.records) == 3 and first.next_cursor == '1' and not first.exhausted
    assert len(second.records) == 3
    # Different pages must not be the same companies, or pagination is decorative.
    assert {r.company for r in first.records} != {r.company for r in second.records}


@pytest.mark.asyncio
async def test_a_mock_page_does_not_collide_with_itself():
    """A synthetic page whose companies repeat makes the dedupe count meaningless.

    With two-part names the generator produced ~13 repeats per 50 records, so the import
    reported `accepted: 37` and it looked like dedupe had caught real duplicates. It had
    caught the generator.
    """
    source = MockProspectingSource()
    result = await source.fetch(cursor=None, limit=50, lane='Shanghai -> Los Angeles')
    names = [r.company for r in result.records]
    assert len(names) == len(set(names)) == 50


@pytest.mark.asyncio
async def test_two_mock_sources_on_one_lane_do_not_return_the_same_companies():
    """Otherwise the cross-source dedupe rejects the second source wholesale.

    That failure is indistinguishable from a broken source: `fetched: 50, accepted: 0`.
    """
    a = MockProspectingSource('vendor_a')
    b = MockProspectingSource('vendor_b')
    first = await a.fetch(cursor=None, limit=10, lane='Shanghai -> Los Angeles')
    second = await b.fetch(cursor=None, limit=10, lane='Shanghai -> Los Angeles')

    assert {r.company for r in first.records}.isdisjoint({r.company for r in second.records})
    assert first.records[0].source == 'vendor_a'


@pytest.mark.asyncio
async def test_run_source_advances_the_cursor_instead_of_rereading_page_one(db):
    spec = sourcing.SourceSpec(kind='mock', name='demo')
    first = await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)
    second = await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)

    assert first['cursor_before'] is None and first['cursor_after'] == '1'
    assert second['cursor_before'] == '1', 'the second run must resume, not restart'
    assert second['cursor_after'] == '2'
    assert second['accepted'] == 3, 'a new page should add new companies'


@pytest.mark.asyncio
async def test_a_third_run_exhausts_the_source_and_says_so(db):
    spec = sourcing.SourceSpec(kind='mock', name='demo')
    await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)
    await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)
    third = await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)

    assert third['status'] == 'exhausted'
    assert third['fetched'] == 3


@pytest.mark.asyncio
async def test_failed_run_does_not_advance_the_cursor(db, monkeypatch):
    """A broken page must be retried, not skipped past."""
    class Boom:
        kind = 'directory'

        async def fetch(self, **kwargs):
            raise SourceFetchError('HTTP 503')

    monkeypatch.setattr(sourcing, 'build_source', lambda *a, **k: Boom())
    spec = sourcing.SourceSpec(kind='directory', name='broken', url='https://x.test/list')
    result = await sourcing.run_source(db, 'tenant-demo', 'u', spec)

    assert result['status'] == 'failed' and '503' in result['error']
    run = db.query(SourceRun).filter_by(source='broken').one()
    assert run.cursor_after is None
    assert run.status == 'failed'


@pytest.mark.asyncio
async def test_exhausted_source_is_not_polled_again_until_the_rescan_window(db, sources_env):
    sources_env('mock|demo|')
    settings = get_settings()
    now = datetime.now(timezone.utc)
    step = timedelta(hours=settings.prospecting_source_interval_hours + 1)

    # Three polls, each one a day after the last, drain the mock source's three pages.
    clock = now
    for _ in range(3):
        await sourcing.run_due_sources(db, now=clock)
        clock += step
    assert db.query(SourceRun).filter_by(source='demo').count() == 3

    # Nothing is due: the source ran out and the rescan window has not elapsed.
    later = now + timedelta(hours=settings.prospecting_source_rescan_hours - 1)
    assert await sourcing.run_due_sources(db, now=later) == []
    assert db.query(SourceRun).filter_by(source='demo').count() == 3


@pytest.mark.asyncio
async def test_exhausted_source_restarts_from_the_top_after_the_rescan_window(db, sources_env):
    """A directory grows; re-reading it is how last week's missing companies are found."""
    sources_env('mock|demo|')
    settings = get_settings()
    now = datetime.now(timezone.utc)
    step = timedelta(hours=settings.prospecting_source_interval_hours + 1)

    clock = now
    for _ in range(3):
        await sourcing.run_due_sources(db, now=clock)
        clock += step

    after = now + timedelta(hours=settings.prospecting_source_rescan_hours + 1)
    ran = await sourcing.run_due_sources(db, now=after)
    assert ran, 'the rescan must actually run'
    latest = db.query(SourceRun).filter_by(source='demo').order_by(
        SourceRun.started_at.desc()).first()
    assert latest.cursor_before is None, 'a rescan starts from the top, not the last page'


@pytest.mark.asyncio
async def test_a_running_source_is_not_stacked_on_top_of_itself(db):
    spec = sourcing.SourceSpec(kind='mock', name='demo')
    db.add(SourceRun(id='src_stuck', tenant_id='tenant-demo', source='demo', kind='mock',
                     status='running', started_at=datetime.now(timezone.utc)))
    db.commit()

    result = await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)
    assert result.get('skipped') == 'previous_run_still_running'


@pytest.mark.asyncio
async def test_a_stale_running_row_does_not_block_forever(db, monkeypatch):
    """A process killed mid-fetch must not freeze the source permanently."""
    monkeypatch.setenv('PROSPECTING_RUN_STALE_MINUTES', '1')
    get_settings.cache_clear()
    try:
        db.add(SourceRun(id='src_old', tenant_id='tenant-demo', source='demo', kind='mock',
                         status='running',
                         started_at=datetime.now(timezone.utc) - timedelta(minutes=10)))
        db.commit()

        spec = sourcing.SourceSpec(kind='mock', name='demo')
        result = await sourcing.run_source(db, 'tenant-demo', 'u', spec, limit=3)
        assert 'skipped' not in result
    finally:
        # `monkeypatch` restores the environment at teardown, which is *after* this block,
        # so the cached Settings has to be dropped explicitly or the next test inherits it.
        monkeypatch.delenv('PROSPECTING_RUN_STALE_MINUTES', raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_each_tenant_gets_its_own_cursor(db, sources_env):
    sources_env('mock|demo|')
    from app.models import Tenant
    db.add(Tenant(id='tenant-two', name='Two'))
    db.commit()

    ran = await sourcing.run_due_sources(db)
    assert sorted(ran) == ['tenant-demo:demo', 'tenant-two:demo']
    for tid in ('tenant-demo', 'tenant-two'):
        run = db.query(SourceRun).filter_by(tenant_id=tid).one()
        assert run.cursor_after == '1'


# --------------------------------------------------------------------------- #
# Directory parsing
# --------------------------------------------------------------------------- #

_TABLE_HTML = """<html><body>
<table>
  <tr><th>Exhibitor</th><th>Country</th><th>Website</th><th>Email</th></tr>
  <tr><td>宁波海隅机械有限公司</td><td>China</td><td>https://haiyu.test</td>
      <td>sales@haiyu.test</td></tr>
  <tr><td>Meridian Trading Co., Ltd</td><td>Germany</td><td>https://meridian.test</td><td></td></tr>
</table>
</body></html>"""


def test_table_is_parsed_into_rows_with_semantic_headers():
    rows, mode = parse_directory_html(_TABLE_HTML)
    assert mode.startswith('table')
    assert rows[0]['company'] == '宁波海隅机械有限公司'
    assert rows[0]['country'] == 'China'
    assert rows[0]['email'] == 'sales@haiyu.test'
    assert rows[1]['company'] == 'Meridian Trading Co., Ltd'


def test_a_table_without_semantic_headers_falls_back_to_position():
    html = '<table><tr><td>Alpha Ltd</td><td>China</td></tr>' \
           '<tr><td>Beta Ltd</td><td>Vietnam</td></tr></table>'
    rows, mode = parse_directory_html(html)
    assert mode.startswith('table')
    assert rows == [{'company': 'Alpha Ltd', 'country': 'China'},
                    {'company': 'Beta Ltd', 'country': 'Vietnam'}]


def test_card_style_directory_falls_back_to_links():
    html = ('<div><a href="https://alpha.test">Alpha Components Ltd</a>'
            '<a href="/about">About</a>'
            '<a href="https://beta.test">Beta Textiles</a>'
            '<a href="https://alpha.test">Alpha Components Ltd</a></div>')
    rows, mode = parse_directory_html(html)
    assert mode.startswith('links')
    assert [r['company'] for r in rows] == ['Alpha Components Ltd', 'Beta Textiles']


@pytest.mark.asyncio
async def test_directory_refuses_a_page_robots_txt_disallows():
    fetch = _fetcher({
        'https://directory.test/robots.txt': (200, 'User-agent: *\nDisallow: /exhibitors'),
        'https://directory.test/exhibitors': (200, _TABLE_HTML),
    })
    source = DirectoryProspectingSource('canton', 'https://directory.test/exhibitors', fetcher=fetch)

    with pytest.raises(SourceFetchError, match='robots'):
        await source.fetch()


@pytest.mark.asyncio
async def test_directory_fails_closed_when_robots_txt_cannot_be_read():
    async def unreachable(url, user_agent):
        raise OSError('dns failure')

    source = DirectoryProspectingSource('canton', 'https://directory.test/exhibitors',
                                        fetcher=unreachable)
    with pytest.raises(SourceFetchError, match='robots'):
        await source.fetch()


@pytest.mark.asyncio
async def test_a_missing_robots_txt_is_permission_not_a_block():
    fetch = _fetcher({
        'https://directory.test/robots.txt': (404, ''),
        'https://directory.test/exhibitors': (200, _TABLE_HTML),
    })
    source = DirectoryProspectingSource('canton', 'https://directory.test/exhibitors', fetcher=fetch)
    result = await source.fetch()
    assert len(result.records) == 2


@pytest.mark.asyncio
async def test_directory_paginates_only_when_the_url_template_has_a_page_slot():
    fetch = _fetcher({
        'https://directory.test/robots.txt': (404, ''),
        'https://directory.test/list': (200, _TABLE_HTML),
    })
    source = DirectoryProspectingSource('canton', 'https://directory.test/list', fetcher=fetch)
    result = await source.fetch()
    assert result.exhausted and result.next_cursor is None


@pytest.mark.asyncio
async def test_scraped_rows_report_that_nobody_is_emailable(db, monkeypatch):
    """The honest answer for a directory: names yes, inboxes mostly no."""
    fetch = _fetcher({
        'https://directory.test/robots.txt': (404, ''),
        'https://directory.test/exhibitors': (200, _TABLE_HTML),
    })
    source = DirectoryProspectingSource('canton', 'https://directory.test/exhibitors', fetcher=fetch)
    monkeypatch.setattr(sourcing, 'build_source', lambda *a, **k: source)

    spec = sourcing.SourceSpec(kind='directory', name='canton',
                               url='https://directory.test/exhibitors')
    result = await sourcing.run_source(db, 'tenant-demo', 'u', spec)

    assert result['accepted'] == 2
    assert result['contact_gaps'] == {'emailable': 1, 'no_email': 1, 'no_contact_name': 2}
    assert result['import_note'] is None, 'one emailable row means the list is not a dead end'

    stored = db.query(ProspectCandidate).filter_by(source='canton').all()
    assert {p.company for p in stored} == {'宁波海隅机械有限公司', 'Meridian Trading Co., Ltd'}
    assert all(p.score == 0 and p.status == 'imported' for p in stored)


@pytest.mark.asyncio
async def test_a_scraped_list_with_no_emails_says_so_at_import_time(db, monkeypatch):
    html = '<table><tr><th>Exhibitor</th><th>Country</th></tr>' \
           '<tr><td>Alpha Ltd</td><td>China</td></tr></table>'
    fetch = _fetcher({
        'https://directory.test/robots.txt': (404, ''),
        'https://directory.test/list': (200, html),
    })
    source = DirectoryProspectingSource('canton', 'https://directory.test/list', fetcher=fetch)
    monkeypatch.setattr(sourcing, 'build_source', lambda *a, **k: source)

    spec = sourcing.SourceSpec(kind='directory', name='canton', url='https://directory.test/list')
    result = await sourcing.run_source(db, 'tenant-demo', 'u', spec)

    assert result['contact_gaps']['emailable'] == 0
    assert result['import_note'], 'a list nobody can email must say so, not look like success'


# --------------------------------------------------------------------------- #
# One alias table, not two
# --------------------------------------------------------------------------- #

def test_the_csv_importer_and_the_scraper_share_one_alias_table():
    """Two copies would drift, and the failure is a column that silently stops mapping."""
    assert service_field_name is field_name
    assert HEADER_ALIASES['exhibitor'] == 'company'
    assert HEADER_ALIASES['参展商'] == 'company'
