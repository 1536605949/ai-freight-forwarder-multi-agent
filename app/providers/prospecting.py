"""Prospecting data sources.

A `ProspectingSource` answers one question: "which companies plausibly ship on this lane?"
It returns *unverified vendor data*. Nothing here writes to the CRM -- candidates land in
`prospect_candidates` and must clear enrichment + compliance review before becoming a Lead.

Three implementations ship here:

  - `MockProspectingSource` -- deterministic synthetic records, for demos and tests.
  - `DirectoryProspectingSource` -- reads a *public* exhibitor / association / member
    directory page, and refuses to fetch it when robots.txt says no.
  - `LicensedVendorProspectingSource` -- contract-specific skeleton for bill-of-lading data.

What is deliberately absent: a customs bill-of-lading scraper. In mainland China that data
is licensed, not public, and scraping a vendor's portal would breach their contract rather
than the site's robots.txt. The lawful free path is public directories; the lawful paid path
is a data contract. See docs/DATA_SOURCING_CN.md and docs/ACQUISITION_ROADMAP.md.

Pagination, not one-shot search, is the contract that makes scheduled collection possible:
`fetch()` returns a `next_cursor`, and the caller stores it in `source_runs`. A source that
cannot paginate still works -- it just reports `exhausted` on the first call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import hashlib
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.config import get_settings
from app.providers.fieldmap import field_name

_MOCK_PAGES = 3
_DEFAULT_TIMEOUT = 10.0

# Synthetic company names. Three parts, because two (12 x 6 = 72) made a 50-record page
# collide with itself ~13 times, and the dedupe layer then reported `accepted: 37` -- which
# reads as "13 real duplicates were caught" when it was the generator repeating.
_ROOTS = ['Meridian', 'Northfield', 'Cobalt', 'Harborline', 'Vantage', 'Ironwood',
          'Bluestem', 'Keystone', 'Larkspur', 'Stonebridge', 'Aurora', 'Cardinal']
_MIDDLES = ['Precision', 'Pacific', 'Eastern', 'Summit', 'Union', 'Pioneer',
            'Nova', 'Grand', 'Silver', 'Prime']
_SUFFIXES = ['Trading', 'Industrial', 'Import & Export', 'Manufacturing',
             'Global Sourcing', 'Supply Chain']
_POOL_SIZE = len(_ROOTS) * len(_MIDDLES) * len(_SUFFIXES)


def _name_for_slot(slot: int) -> str:
    """A name that is unique for the first `_POOL_SIZE` slots, by construction.

    Uniqueness has to be a property of the generator, not a probability. Pages are spaced
    200 slots apart, so a three-page demo (150 records) cannot repeat a name even though the
    pool is 720.
    """
    s = slot % _POOL_SIZE
    root = _ROOTS[s % len(_ROOTS)]
    middle = _MIDDLES[(s // len(_ROOTS)) % len(_MIDDLES)]
    suffix = _SUFFIXES[(s // (len(_ROOTS) * len(_MIDDLES))) % len(_SUFFIXES)]
    return f'{root} {middle} {suffix}'


class SourceFetchError(RuntimeError):
    """The source could not be read, for a reason the operator needs to see."""


@dataclass
class ProspectRecord:
    company: str
    country: str | None = None
    industry: str | None = None
    website: str | None = None
    trade_lane: str | None = None
    est_volume_teu: float | None = None
    source: str = 'mock'
    source_ref: str | None = None
    data_origin: str = 'mock_synthetic'
    # A directory page sometimes publishes an email; customs exports never do. Keeping the
    # field here means both paths can report contactability in the same shape.
    email: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    payload: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        """Shape expected by `services.prospecting.import_prospects`."""
        return {
            'company': self.company, 'country': self.country, 'industry': self.industry,
            'website': self.website, 'trade_lane': self.trade_lane,
            'est_volume_teu': self.est_volume_teu, 'email': self.email,
            'contact_name': self.contact_name, 'phone': self.phone,
            'source_ref': self.source_ref,
        }


@dataclass
class FetchResult:
    records: list[ProspectRecord]
    # Opaque to the caller: stored verbatim in `source_runs.cursor_after` and handed back.
    next_cursor: str | None = None
    exhausted: bool = False
    # Why this page yielded what it did -- shown in the run log so a silent empty page is
    # distinguishable from a parser that stopped matching.
    note: str | None = None


class ProspectingSource:
    kind = 'abstract'

    async def search(self, lane: str, industry: str | None = None, limit: int = 20) -> list[ProspectRecord]:
        raise NotImplementedError

    async def fetch(self, *, cursor: str | None = None, limit: int = 50,
                    lane: str | None = None) -> FetchResult:
        """Default: a source without pagination is fetched once and reported exhausted."""
        records = await self.search(lane or '', None, limit)
        return FetchResult(records=records, next_cursor=None, exhausted=True,
                           note='source has no pagination; fetched once')


class MockProspectingSource(ProspectingSource):
    """Deterministic synthetic prospects, so a demo is reproducible run to run.

    Determinism matters here: a bid demo that returns different companies every refresh
    looks broken. The same lane and page always yield the same set.
    """
    kind = 'mock'
    _INDUSTRIES = ['Household Appliances', 'Auto Parts', 'Furniture', 'Textiles',
                   'Consumer Electronics', 'Machinery', 'Building Materials', 'Toys']
    _MARKETS = [('US', 'United States'), ('DE', 'Germany'), ('NL', 'Netherlands'),
                ('GB', 'United Kingdom'), ('AU', 'Australia'), ('BR', 'Brazil'),
                ('JP', 'Japan'), ('AE', 'United Arab Emirates')]

    def __init__(self, name: str = 'mock'):
        # Two mock sources on the same lane must not produce the same companies: the
        # cross-source dedupe would then reject the second one wholesale, which looks
        # exactly like a broken source. Real vendors overlap partially, not entirely.
        self.name = name

    async def search(self, lane, industry=None, limit=20):
        return self._generate(lane, industry, limit, page=0)

    async def fetch(self, *, cursor=None, limit=50, lane=None):
        page = int(cursor or 0)
        if page >= _MOCK_PAGES:
            return FetchResult(records=[], next_cursor=None, exhausted=True,
                               note=f'mock source exhausted at page {page}')
        lane = lane or 'Shanghai -> Los Angeles'
        records = self._generate(lane, None, limit, page=page)
        return FetchResult(records=records, next_cursor=str(page + 1),
                           exhausted=page + 1 >= _MOCK_PAGES,
                           note=f'mock page {page}: {len(records)} synthetic records')

    def _generate(self, lane, industry, limit, page):
        origin, destination = _split_lane(lane)
        seed = int(hashlib.sha256(f'{lane}|{page}'.encode('utf-8')).hexdigest()[:8], 16)
        # Names are assigned by slot, not by hash, so uniqueness is a property of the
        # generator rather than a probability. Pages are 200 slots apart and the pool holds
        # 720 combinations, so a three-page demo (150 records) cannot repeat a name.
        lane_offset = int(hashlib.sha256(f'{self.name}|{lane}'.encode('utf-8')).hexdigest()[:6], 16) % 720
        out = []
        for i in range(limit):
            h = hashlib.sha256(f'{lane}:{page}:{i}:{seed}'.encode('utf-8')).hexdigest()
            ind = industry or self._INDUSTRIES[int(h[:2], 16) % len(self._INDUSTRIES)]
            mk = self._MARKETS[int(h[2:4], 16) % len(self._MARKETS)]
            company = _name_for_slot(lane_offset + page * 200 + i)
            slug = company.lower().replace(' ', '').replace('&', '')
            out.append(ProspectRecord(
                company=company, country=mk[1], industry=ind,
                website=f'https://www.{slug}.example',
                trade_lane=lane,
                est_volume_teu=round(20 + (int(h[6:10], 16) % 900) + (int(h[10:12], 16) % 100) / 100, 2),
                source=self.name, source_ref=f'{destination[:3].upper()}-P{page}-{h[:8].upper()}',
                data_origin='mock_synthetic',
                payload={'origin': origin, 'destination': destination, 'demo': True, 'page': page},
            ))
        return out


# --------------------------------------------------------------------------- #
# Public directory reader
# --------------------------------------------------------------------------- #

# Navigation chrome that a link-harvesting fallback would otherwise turn into "companies".
_NAV_WORDS = {
    'home', 'about', 'about us', 'contact', 'contact us', 'login', 'sign in', 'register',
    'next', 'prev', 'previous', 'more', 'view all', 'read more', 'privacy', 'terms',
    'search', 'menu', 'back', 'top', 'english', '中文', '首页', '联系我们', '登录',
    '注册', '更多', '上一页', '下一页', '返回顶部', '隐私政策',
}


class _TableCollector(HTMLParser):
    """Collect every table as rows of cells, ignoring nested layout tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[dict]] = []
        self._depth = 0
        self._rows: list[dict] | None = None
        self._row: dict | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self._depth += 1
            if self._depth == 1:
                self._rows = []
                self.tables.append(self._rows)
            return
        if self._depth != 1:
            return
        if tag == 'tr':
            self._row = {'header': False, 'cells': []}
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
            if tag == 'th':
                self._row['header'] = True
        elif tag == 'br' and self._cell is not None:
            self._cell.append(' ')

    def handle_endtag(self, tag):
        if tag == 'table':
            self._depth -= 1
            if self._depth == 0:
                self._rows = None
            return
        if self._depth != 1:
            return
        if tag in ('td', 'th') and self._cell is not None and self._row is not None:
            self._row['cells'].append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if self._row['cells']:
                self._rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class _LinkCollector(HTMLParser):
    """Fallback for card-style directories that are not tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self._href = dict(attrs).get('href')
            self._text = []

    def handle_endtag(self, tag):
        if tag == 'a' and self._href:
            text = ' '.join(''.join(self._text).split())
            if text:
                self.links.append((text, self._href))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


def parse_directory_html(html: str) -> tuple[list[dict], str]:
    """Turn a directory page into row dicts, plus a note saying which strategy worked.

    Tables first, because an exhibitor list is usually one. When the page has no usable
    table, fall back to harvesting links: a card grid still names companies and usually
    links to their site. The fallback is noisier, which is why the note is returned --
    an operator looking at "3 rows" deserves to know it came from the weak path.
    """
    collector = _TableCollector()
    collector.feed(html)

    best = max(collector.tables, key=len, default=None)
    if best and len(best) >= 2 and max(len(r['cells']) for r in best) >= 2:
        rows = _table_to_rows(best)
        if rows:
            return rows, f'table ({len(rows)} rows)'

    links = _LinkCollector()
    links.feed(html)
    rows = _links_to_rows(links.links)
    return rows, f'links ({len(rows)} rows)'


def _table_to_rows(table: list[dict]) -> list[dict]:
    header = table[0]
    keys: list[str] | None = None
    data = table
    if header['header']:
        mapped = [field_name(c) for c in header['cells']]
        if any(k in ('company', 'exporter', 'importer', 'email', 'website') for k in mapped):
            keys = mapped
            data = table[1:]

    rows = []
    for item in data:
        cells = item['cells']
        if not cells or not any(cells):
            continue
        if keys:
            row = {k: v for k, v in zip(keys, cells) if k}
        else:
            # No semantic header. Position is all we have: a directory's first column is the
            # exhibitor's own name far more often than not.
            row = {'company': cells[0]}
            if len(cells) > 1:
                row['country'] = cells[1]
        if (row.get('company') or row.get('exporter') or row.get('importer')):
            rows.append(row)
    return rows


def _links_to_rows(links: list[tuple[str, str]]) -> list[dict]:
    seen: set[str] = set()
    rows: list[dict] = []
    for text, href in links:
        if not href.startswith('http') or href in seen:
            continue
        if not (3 <= len(text) <= 80):
            continue
        if text.strip().lower() in _NAV_WORDS:
            continue
        seen.add(href)
        rows.append({'company': text, 'website': href})
    return rows


async def _httpx_get(url: str, user_agent: str) -> tuple[int, str]:
    import httpx
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers={'User-Agent': user_agent})
        return response.status_code, response.text


class DirectoryProspectingSource(ProspectingSource):
    """Reads a public exhibitor / member directory. Free, lawful, and worth what you pay.

    Two things it will not do:

      - Fetch a page robots.txt disallows. If robots.txt cannot be read at all it fails
        *closed*, because "we could not check" is not "we may".
      - Invent a contact. A directory gives you a company name and often a country; it
        rarely gives you the person who books freight. Those rows arrive with no email,
        and `import_prospects` reports that as `contact_gaps` instead of pretending the
        list is ready to mail.
    """
    kind = 'directory'

    def __init__(self, name: str, url_template: str, *, fetcher=None,
                 data_origin: str = 'public_directory', default_lane: str | None = None,
                 default_industry: str | None = None, user_agent: str | None = None):
        self.name = name
        self.url_template = url_template
        self.data_origin = data_origin
        self.default_lane = default_lane
        self.default_industry = default_industry
        self.user_agent = user_agent or f'{get_settings().app_name}-directory-reader'
        self._get = fetcher or _httpx_get

    async def search(self, lane, industry=None, limit=20):
        result = await self.fetch(cursor=None, limit=limit, lane=lane)
        return result.records

    async def fetch(self, *, cursor=None, limit=50, lane=None):
        page = int(cursor or 0)
        url = self._url_for(page)
        allowed, why = await self._robots_allows(url)
        if not allowed:
            raise SourceFetchError(f'robots.txt disallows {url}: {why}')

        status, body = await self._get(url, self.user_agent)
        if status >= 400:
            raise SourceFetchError(f'HTTP {status} for {url}')

        rows, mode = parse_directory_html(body)
        records = [self._to_record(r, lane) for r in rows[:limit]]
        paged = '{page}' in self.url_template
        return FetchResult(
            records=records,
            next_cursor=str(page + 1) if paged else None,
            exhausted=not paged,
            note=f'page {page} via {mode}',
        )

    def _url_for(self, page: int) -> str:
        if '{page}' in self.url_template:
            return self.url_template.replace('{page}', str(page))
        return self.url_template

    async def _robots_allows(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False, f'unsupported scheme {parsed.scheme!r}'
        robots_url = f'{parsed.scheme}://{parsed.netloc}/robots.txt'
        try:
            status, body = await self._get(robots_url, self.user_agent)
        except Exception as exc:  # noqa: BLE001 - any failure means "unverified"
            return False, f'robots.txt unreachable ({exc})'
        if status >= 400:
            # RFC 9309: a 4xx robots.txt means no restrictions. A 5xx is a server problem,
            # not permission, so it stays closed.
            return (True, 'no robots.txt (4xx)') if status < 500 else (False, f'robots.txt HTTP {status}')
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return parser.can_fetch('*', url), robots_url

    def _to_record(self, row: dict, lane: str | None) -> ProspectRecord:
        company = (row.get('company') or row.get('exporter') or row.get('importer') or '').strip()
        ref = row.get('source_ref') or hashlib.sha256(
            f'{self.name}|{company}|{row.get("website") or ""}'.encode('utf-8')).hexdigest()[:16]
        return ProspectRecord(
            company=company[:200],
            country=(row.get('country') or '')[:80] or None,
            industry=(row.get('industry') or self.default_industry or '')[:120] or None,
            website=(row.get('website') or '')[:300] or None,
            trade_lane=(lane or self.default_lane or '')[:160] or None,
            source=self.name, source_ref=ref, data_origin=self.data_origin,
            email=(row.get('email') or '')[:200] or None,
            contact_name=(row.get('contact_name') or '')[:160] or None,
            phone=(row.get('phone') or '')[:80] or None,
            payload={'directory_row': row, 'directory_url': self._url_for(0)},
        )


class LicensedVendorProspectingSource(ProspectingSource):
    """Skeleton for a contracted trade-data vendor (bill of lading / customs data).

    Intentionally not implemented: the endpoint, auth model and field mapping are
    contract-specific. Normalize the vendor payload into `ProspectRecord` here so the
    rest of the application never sees vendor-specific shapes.
    """
    kind = 'licensed_vendor'

    def __init__(self):
        settings = get_settings()
        self.base = (getattr(settings, 'prospecting_base_url', '') or '').rstrip('/')
        self.token = getattr(settings, 'prospecting_token', None)

    async def search(self, lane, industry=None, limit=20):
        if not self.base:
            raise RuntimeError('PROSPECTING_BASE_URL is required')
        raise NotImplementedError('Map your vendor payload into ProspectRecord here; contract-specific.')

    async def fetch(self, *, cursor=None, limit=50, lane=None):
        records = await self.search(lane or '', None, limit)
        return FetchResult(records=records, exhausted=True)


def _split_lane(lane: str) -> tuple[str, str]:
    for sep in ('->', '→', '-', 'to'):
        if sep in lane:
            a, b = lane.split(sep, 1)
            return a.strip(), b.strip()
    return lane.strip(), ''


def build_source(kind: str, name: str, url: str | None = None, *, fetcher=None) -> ProspectingSource:
    """Construct a source from an operator-supplied spec.

    Kept as an explicit factory rather than a class lookup so an unknown kind fails loudly
    at startup instead of silently falling back to mock data in production.
    """
    if kind == 'mock':
        return MockProspectingSource(name)
    if kind == 'directory':
        if not url:
            raise ValueError('a directory source needs a url')
        return DirectoryProspectingSource(name, url, fetcher=fetcher)
    if kind == 'licensed_vendor':
        return LicensedVendorProspectingSource()
    raise ValueError(f'unknown source kind: {kind!r}')


def get_prospecting_source() -> ProspectingSource:
    return LicensedVendorProspectingSource() if get_settings().prospecting_provider == 'licensed_vendor' else MockProspectingSource()
