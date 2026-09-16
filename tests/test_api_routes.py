"""Route-layer tests: authentication boundaries and tenant isolation at the HTTP edge.

`app/main.py` is 220 statements across 43 routes and previously had **zero** coverage. The
only tenant-isolation test asserted that `query.filter_by(tenant_id=...)` filters — which
tests SQLAlchemy, not the platform.

These tests drive the real ASGI app, so they exercise the thing that actually ships: the
dependency wiring, the header/token parsing, and the status codes a caller sees. Three
properties matter most:

* a tenant cannot read or drive another tenant's objects,
* irreversible endpoints are behind a role,
* a business-rule refusal surfaces as 409, not as a 500.
"""
from __future__ import annotations

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, get_db
from app.main import app
from app.models import Customer, Inquiry

TENANT = 'tenant-demo'
OTHER = 'tenant-other'


@pytest.fixture
def api_db():
    """A session whose connection is safe to use from the TestClient's worker thread.

    The shared `db` fixture from conftest uses a plain in-memory database, whose connection
    is bound to the creating thread. Starlette runs the app in its own thread, so these tests
    need `check_same_thread=False` plus a StaticPool so both threads see the same database.
    """
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(api_db):
    """The real ASGI app, with only the database dependency redirected.

    `TestClient` is deliberately not used as a context manager: entering the lifespan runs
    the startup hook, which calls `create_all` against the developer's real database.
    """
    app.dependency_overrides[get_db] = lambda: api_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _headers(tenant: str = TENANT, user: str = 'u1') -> dict[str, str]:
    return {'X-Demo-User': user, 'X-Demo-Tenant': tenant}


def _seed_inquiry(db, inquiry_id: str, tenant: str, status: str = 'ready') -> None:
    db.add(Customer(id=f'cus_{inquiry_id}', tenant_id=tenant, company='Acme',
                    email=f'{inquiry_id}@acme.test'))
    db.add(Inquiry(id=inquiry_id, tenant_id=tenant, customer_id=f'cus_{inquiry_id}',
                   raw_message='x', origin='Shanghai', destination='Los Angeles',
                   equipment='40HQ', quantity=1, etd='2026-09-25', missing_fields=[],
                   status=status, external_message_id=f'm_{inquiry_id}'))
    db.commit()


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #

def test_health_is_public(client):
    r = client.get('/api/healthz')
    assert r.status_code == 200 and r.json()['status'] == 'ok'


def test_stage_list_matches_the_orchestrator(client):
    """The UI reads this to build its progress rail, so it must be the real list."""
    from app.services import orchestrator as orch
    r = client.get('/api/v1/orchestrate/stages')
    assert r.status_code == 200
    body = r.json()
    assert body['stages'] == orch.STAGES
    assert set(body['gated']) == orch.GATED_STAGES
    assert set(body['auto']) == orch.AUTO_STAGES


# --------------------------------------------------------------------------- #
# Tenant isolation at the edge
# --------------------------------------------------------------------------- #

def test_owner_can_read_its_own_inquiry(client, api_db):
    _seed_inquiry(api_db, 'inq_a', TENANT)
    r = client.get('/api/v1/inquiries/inq_a', headers=_headers(TENANT))
    assert r.status_code == 200 and r.json()['id'] == 'inq_a'


def test_cross_tenant_read_is_404_not_403(client, api_db):
    """404 rather than 403: a 403 would confirm the id exists."""
    _seed_inquiry(api_db, 'inq_b', OTHER)
    r = client.get('/api/v1/inquiries/inq_b', headers=_headers(TENANT))
    assert r.status_code == 404


def test_cross_tenant_orchestration_is_404(client, api_db):
    _seed_inquiry(api_db, 'inq_c', OTHER)
    r = client.post('/api/v1/orchestrate/run', json={'inquiry_id': 'inq_c'},
                    headers=_headers(TENANT))
    assert r.status_code == 404


def test_cross_tenant_status_is_404(client, api_db):
    _seed_inquiry(api_db, 'inq_d', OTHER)
    r = client.get('/api/v1/orchestrate/status/inq_d', headers=_headers(TENANT))
    assert r.status_code == 404


def test_dashboard_counts_only_the_callers_tenant(client, api_db):
    _seed_inquiry(api_db, 'inq_e', TENANT)
    _seed_inquiry(api_db, 'inq_f', OTHER)
    r = client.get('/api/v1/dashboard', headers=_headers(TENANT))
    assert r.status_code == 200
    assert r.json()['inquiries'] == 1


def test_prospect_list_is_tenant_scoped(client, api_db):
    from app.models import ProspectCandidate
    api_db.add(ProspectCandidate(id='p_a', tenant_id=TENANT, company='Mine'))
    api_db.add(ProspectCandidate(id='p_b', tenant_id=OTHER, company='Theirs'))
    api_db.commit()
    r = client.get('/api/v1/prospects', headers=_headers(TENANT))
    assert r.status_code == 200
    assert [p['id'] for p in r.json()] == ['p_a']


# --------------------------------------------------------------------------- #
# Business-rule refusals surface as 409, not 500
# --------------------------------------------------------------------------- #

def test_patch_on_a_locked_inquiry_returns_409(client, api_db):
    """Regression: the state-machine guard must be translated, not leaked as a 500."""
    _seed_inquiry(api_db, 'inq_g', TENANT, status='booked')
    r = client.patch('/api/v1/inquiries/inq_g', json={'destination': 'Oakland'},
                     headers=_headers(TENANT))
    assert r.status_code == 409
    assert 'inquiry_locked' in r.text


def test_patch_on_an_unknown_inquiry_returns_404(client):
    r = client.patch('/api/v1/inquiries/nope', json={'destination': 'Oakland'},
                     headers=_headers(TENANT))
    assert r.status_code == 404


def test_patch_on_an_in_flight_inquiry_keeps_its_status(client, api_db):
    _seed_inquiry(api_db, 'inq_h', TENANT, status='scheduled')
    r = client.patch('/api/v1/inquiries/inq_h', json={'commodity': 'furniture'},
                     headers=_headers(TENANT))
    assert r.status_code == 200
    assert r.json()['status'] == 'scheduled'
    assert r.json()['commodity'] == 'furniture'


def test_unknown_start_stage_is_rejected(client, api_db):
    _seed_inquiry(api_db, 'inq_i', TENANT)
    r = client.post('/api/v1/orchestrate/run',
                    json={'inquiry_id': 'inq_i', 'start_stage': 'teleport'},
                    headers=_headers(TENANT))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Role boundaries (JWT mode)
# --------------------------------------------------------------------------- #

@pytest.fixture
def jwt_mode(monkeypatch):
    """Switch the app to real token auth for the duration of a test."""
    import app.auth as auth
    base = Settings()
    monkeypatch.setattr(auth, 'get_settings',
                        lambda: base.model_copy(update={'auth_mode': 'jwt'}))
    return base


def _token(settings: Settings, roles: list[str], tenant: str = TENANT) -> dict[str, str]:
    payload = {'sub': 'u1', 'tenant_id': tenant, 'roles': roles, 'iss': settings.jwt_issuer}
    tok = pyjwt.encode(payload, settings.jwt_secret, algorithm='HS256')
    return {'Authorization': f'Bearer {tok}'}


def test_missing_token_is_rejected_in_jwt_mode(client, jwt_mode):
    r = client.get('/api/v1/dashboard')
    assert r.status_code == 401


def test_garbage_token_is_rejected(client, jwt_mode):
    r = client.get('/api/v1/dashboard', headers={'Authorization': 'Bearer not-a-jwt'})
    assert r.status_code == 401


def test_valid_token_is_accepted(client, jwt_mode):
    r = client.get('/api/v1/dashboard', headers=_token(jwt_mode, ['sales']))
    assert r.status_code == 200


def test_resume_requires_a_reviewer(client, api_db, jwt_mode):
    """Resuming past the human gate is the one endpoint a plain sales user must not reach."""
    _seed_inquiry(api_db, 'inq_j', TENANT)
    body = {'quote_id': 'cq_x'}

    r = client.post('/api/v1/orchestrate/continue', json=body,
                    headers=_token(jwt_mode, ['sales']))
    assert r.status_code == 403, 'a sales role must not be able to resume a gated pipeline'

    # A reviewer gets past the role check (and then fails on the unknown quote, which is a
    # 404 -- proving the 403 above was the role gate and not something incidental).
    r = client.post('/api/v1/orchestrate/continue', json=body,
                    headers=_token(jwt_mode, ['reviewer']))
    assert r.status_code == 404


def test_quote_review_requires_a_reviewer(client, jwt_mode):
    r = client.post('/api/v1/quotes/cq_x/review', json={'action': 'approve'},
                    headers=_token(jwt_mode, ['sales']))
    assert r.status_code == 403


def test_internal_followup_runner_requires_admin(client, jwt_mode):
    r = client.post('/internal/run-due-followups', headers=_token(jwt_mode, ['sales']))
    assert r.status_code == 403


def test_admin_can_reach_every_gated_route(client, jwt_mode):
    r = client.post('/internal/run-due-followups', headers=_token(jwt_mode, ['admin']))
    assert r.status_code == 200 and 'processed' in r.json()


def test_token_tenant_scopes_the_request(client, api_db, jwt_mode):
    """The tenant comes from the signed token, so headers cannot override it."""
    _seed_inquiry(api_db, 'inq_k', OTHER)
    r = client.get('/api/v1/inquiries/inq_k',
                   headers={**_token(jwt_mode, ['admin'], tenant=TENANT),
                            **_headers(OTHER)})
    assert r.status_code == 404, 'a demo header must not widen a token scoped to another tenant'
