# backend/tests/test_route_authorization.py
# Route-level authorization suite. Proves, for every route in app.py:
#   - who can reach it (auth required? admin required?)
#   - for data-bearing routes, that a caller can only reach data they're
#     entitled to — cross-clinic isolation is absolute; same-clinic
#     cross-doctor access on signed notes is intentional and must
#     continue to succeed; owner-only resources stay owner-only.
#
# Run:  cd backend && venv/bin/python -m pytest tests/ -v

import io
import pytest

import app as app_module
from tests import tokens
from tests.conftest import (
    CLINIC_A, CLINIC_B, DOCTOR_A1, DOCTOR_A2, ADMIN_A, DOCTOR_B1, ADMIN_B,
    INACTIVE_DOCTOR, SESSION_CONFIRMED_A, SESSION_PENDING_A, SESSION_CONFIRMED_B, JOB_A1,
)
from tests.route_inventory import ROUTE_INVENTORY


# ── dispatch helper ──────────────────────────────────────────────────────

# Placeholder values substituted into <param> path segments when the
# specific value doesn't matter for the assertion being made (e.g. the
# 401 sweep never reaches resource lookup at all).
_PLACEHOLDER = {
    'job_id': 'dummy-job-id',
    'session_id': 'dummy-session-id',
    'usuario_id': 'dummy-usuario-id',
}


def _fill(path: str, **overrides) -> str:
    for name, value in {**_PLACEHOLDER, **overrides}.items():
        path = path.replace(f'<{name}>', value)
    return path


def _call(client, method: str, path: str, token: str | None, json_body=None, data=None):
    headers = tokens.auth_headers(token)
    kwargs = {'headers': headers}
    if json_body is not None:
        kwargs['json'] = json_body
    if data is not None:
        kwargs['data'] = data
        kwargs['content_type'] = 'multipart/form-data'
    return client.open(path, method=method, **kwargs)


# ── Step 1: the inventory itself must cover the real app exactly ────────

def test_every_app_route_is_inventoried():
    """
    Walks the REAL Flask url_map and cross-checks it against
    ROUTE_INVENTORY. A route that exists in the app but not in the
    inventory (a newly-added route nobody made an auth decision for) or
    an inventory entry for a route that no longer exists both fail this
    test — this is the gap-detector the task asked for.
    """
    inventory_keys = {
        (entry['path'], m) for entry in ROUTE_INVENTORY for m in entry['methods']
    }
    app_keys = set()
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        methods = (rule.methods or set()) - {'HEAD', 'OPTIONS'}
        for m in methods:
            app_keys.add((rule.rule, m))

    missing_from_inventory = app_keys - inventory_keys
    stale_in_inventory = inventory_keys - app_keys
    assert not missing_from_inventory, (
        f"Route(s) exist in app.py with no ROUTE_INVENTORY entry — "
        f"no auth decision was recorded for them: {sorted(missing_from_inventory)}"
    )
    assert not stale_in_inventory, (
        f"ROUTE_INVENTORY entries reference routes that no longer exist "
        f"in app.py — update the inventory: {sorted(stale_in_inventory)}"
    )


# ── Step 2: public routes ────────────────────────────────────────────────

@pytest.mark.parametrize(
    'entry', [e for e in ROUTE_INVENTORY if not e['auth_required']],
    ids=lambda e: f"{e['methods'][0]} {e['path']}",
)
def test_public_routes_reachable_without_token(client, entry):
    method = entry['methods'][0]
    resp = _call(client, method, _fill(entry['path']), token=None)
    assert resp.status_code == 200, (
        f"{method} {entry['path']} is inventoried as public but returned "
        f"{resp.status_code} with no token"
    )


def test_public_route_allowlist_is_exactly_these_six():
    """
    An over-broad 'public' allowlist is itself a finding (per the task
    brief) — pin the exact set so a route silently losing its
    @require_auth would fail this test even if it happened to still
    return 200 for an unrelated reason.
    """
    public_paths = {(e['path'], m) for e in ROUTE_INVENTORY if not e['auth_required'] for m in e['methods']}
    assert public_paths == {
        ('/', 'GET'),
        ('/login', 'GET'),
        ('/set-password', 'GET'),
        ('/admin', 'GET'),
        ('/account', 'GET'),
        ('/api/health', 'GET'),
    }


# ── Step 3: every protected route rejects no/expired/bad-sig/malformed ──

_BAD_TOKENS = [
    pytest.param(None, id='no-token'),
    pytest.param(tokens.EXPIRED, id='expired'),
    pytest.param(tokens.BAD_SIGNATURE, id='bad-signature'),
    pytest.param(tokens.MALFORMED, id='malformed'),
]


@pytest.mark.parametrize('entry', [e for e in ROUTE_INVENTORY if e['auth_required']],
                          ids=lambda e: f"{e['methods'][0]} {e['path']}")
@pytest.mark.parametrize('bad_token', _BAD_TOKENS)
def test_protected_routes_reject_invalid_tokens(client, entry, bad_token):
    method = entry['methods'][0]
    resp = _call(client, method, _fill(entry['path']), token=bad_token)
    assert resp.status_code == 401, (
        f"{method} {entry['path']} should 401 on token kind "
        f"'{bad_token}', got {resp.status_code}"
    )


def test_deactivated_user_rejected(client):
    """UsuarioInactivoError path — a structurally valid token for a
    usuarios.activo=false account must still be rejected (401), not
    treated as authenticated."""
    resp = _call(client, 'GET', '/api/session-check', token=tokens.mint(INACTIVE_DOCTOR))
    assert resp.status_code == 401


def test_unknown_usuario_id_forbidden(client):
    """A structurally-valid token for a sub that has no usuarios row at
    all is the 403 case (distinct from 401) per require_auth's own code."""
    resp = _call(client, 'GET', '/api/session-check', token=tokens.mint('nobody-registered-anywhere'))
    assert resp.status_code == 403


# ── Step 4: admin gating ────────────────────────────────────────────────

@pytest.mark.parametrize('entry', [e for e in ROUTE_INVENTORY if e['admin_required']],
                          ids=lambda e: f"{e['methods'][0]} {e['path']}")
def test_admin_routes_reject_non_admin(client, entry):
    method = entry['methods'][0]
    resp = _call(client, method, _fill(entry['path']), token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 403, (
        f"{method} {entry['path']} is admin-gated but a non-admin doctor "
        f"got {resp.status_code}, not 403"
    )


# ── Step 5: cross-clinic isolation — the invariant that must never regress ──
# One test per data-bearing route: a clinic-B caller must never reach a
# clinic-A resource (or vice versa), regardless of scope (owner,
# owner_or_clinic, or clinic-wide-admin).

CROSS_CLINIC_CASES = [
    # (method, path, path_kwargs, caller_token, expected_status)
    ('GET',  '/api/job-status/<job_id>',                {'job_id': JOB_A1},               DOCTOR_B1, 404),
    ('POST', '/api/confirm-and-generate',                {},                                DOCTOR_B1, 404),
    ('GET',  '/api/session/<session_id>',                {'session_id': SESSION_CONFIRMED_A}, DOCTOR_B1, 404),
    ('GET',  '/api/export-json/<session_id>',            {'session_id': SESSION_CONFIRMED_A}, DOCTOR_B1, 404),
    ('GET',  '/api/download-pdf/<session_id>',           {'session_id': SESSION_CONFIRMED_A}, DOCTOR_B1, 404),
    ('GET',  '/api/patient-history/<session_id>',        {'session_id': SESSION_CONFIRMED_A}, DOCTOR_B1, 404),
    ('GET',  '/api/pending-sessions/<session_id>',       {'session_id': SESSION_PENDING_A},   DOCTOR_B1, 404),
    ('DELETE', '/api/pending-sessions/<session_id>',     {'session_id': SESSION_PENDING_A},   DOCTOR_B1, 404),
    ('PATCH', '/api/admin/usuarios/<usuario_id>/activo', {'usuario_id': DOCTOR_A1},           ADMIN_B, 404),
    ('GET',  '/api/admin/session/<session_id>/pdf',      {'session_id': SESSION_CONFIRMED_A}, ADMIN_B, 404),
    ('POST', '/api/admin/session/<session_id>/addendum', {'session_id': SESSION_CONFIRMED_A}, ADMIN_B, 404),
    ('POST', '/api/admin/session/<session_id>/cancel',   {'session_id': SESSION_CONFIRMED_A}, ADMIN_B, 404),
]


@pytest.mark.parametrize(
    'method,path,path_kwargs,caller,expected',
    CROSS_CLINIC_CASES,
    ids=[f"{m} {p}" for m, p, *_ in CROSS_CLINIC_CASES],
)
def test_cross_clinic_isolation(client, method, path, path_kwargs, caller, expected):
    body = None
    if path == '/api/confirm-and-generate':
        body = {'session_id': SESSION_CONFIRMED_A, 'structured_data': {}, 'create_pdf': False, 'send_email': False}
    elif path == '/api/admin/usuarios/<usuario_id>/activo':
        body = {'activo': False}
    elif path.endswith('/addendum'):
        body = {'texto': 'intento de acceso cruzado (fixture de prueba)'}
    elif path.endswith('/cancel'):
        body = {'cancellation_reason': 'intento de acceso cruzado (fixture de prueba)'}

    resp = _call(client, method, _fill(path, **path_kwargs), token=tokens.mint(caller), json_body=body)
    assert resp.status_code == expected, (
        f"cross-clinic caller on {method} {path}: expected {expected}, got {resp.status_code} — "
        f"a clinic-B caller must never reach a clinic-A resource"
    )


def test_patient_history_scope_clinica_cannot_be_pointed_at_another_clinic(client):
    """scope=clinica is documented as 'the caller's own clinic, never
    client-chosen' — there is no clinica_id parameter to tamper with at
    all, so the invariant here is that clinic-B's doctor never sees
    clinic-A's fixture session even when asking for the same CURP."""
    resp = _call(
        client, 'GET',
        '/api/patient-history?curp=FAKE000101HDFXXX01&scope=clinica',
        token=tokens.mint(DOCTOR_B1),
    )
    assert resp.status_code == 200
    session_ids = {row['session_id'] for row in resp.get_json()}
    assert SESSION_CONFIRMED_A not in session_ids
    assert SESSION_CONFIRMED_B in session_ids


def test_admin_sessions_usuario_id_filter_cannot_leak_another_clinic(client):
    """admin_sessions accepts a client-supplied usuario_id filter — confirm
    it composes with (never replaces) the server-forced clinica_id filter:
    filtering by a real doctor from a DIFFERENT clinic returns empty, not
    that doctor's data."""
    resp = _call(
        client, 'GET',
        f'/api/admin/sessions?usuario_id={DOCTOR_B1}&desde=2020-01-01&hasta=2030-01-01',
        token=tokens.mint(ADMIN_A),
    )
    assert resp.status_code == 200
    assert resp.get_json()['sessions'] == []


# ── Step 6: same-clinic, different doctor — continuity of care ─────────
# Confirmed/signed notes: MUST succeed for a colleague in the same clinic.

SAME_CLINIC_ALLOWED_CASES = [
    ('GET', '/api/session/<session_id>', {'session_id': SESSION_CONFIRMED_A}),
    ('GET', '/api/export-json/<session_id>', {'session_id': SESSION_CONFIRMED_A}),
    ('GET', '/api/download-pdf/<session_id>', {'session_id': SESSION_CONFIRMED_A}),
    ('GET', '/api/patient-history/<session_id>', {'session_id': SESSION_CONFIRMED_A}),
]


@pytest.mark.parametrize(
    'method,path,path_kwargs', SAME_CLINIC_ALLOWED_CASES,
    ids=[f"{m} {p}" for m, p, _ in SAME_CLINIC_ALLOWED_CASES],
)
def test_same_clinic_different_doctor_can_read_colleagues_note(client, method, path, path_kwargs):
    """DOCTOR_A2 (not the author) reading DOCTOR_A1's confirmed note in
    the SAME clinic must succeed — this is intentional continuity-of-care
    behavior, not a bug. A test asserting the opposite would be wrong
    per the task's own policy statement."""
    resp = _call(client, method, _fill(path, **path_kwargs), token=tokens.mint(DOCTOR_A2))
    assert resp.status_code == 200, (
        f"same-clinic colleague access to {method} {path} should succeed "
        f"(continuity of care), got {resp.status_code}"
    )


def test_confirm_and_generate_is_owner_only_even_within_same_clinic(client):
    """Unlike read access, confirming/signing a pending draft is owner-
    only — a same-clinic colleague must NOT be able to sign someone
    else's still-pending note."""
    resp = _call(
        client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A2),
        json_body={'session_id': SESSION_PENDING_A, 'structured_data': {}, 'create_pdf': False, 'send_email': False},
    )
    assert resp.status_code == 404


def test_job_status_is_owner_only_even_within_same_clinic(client):
    resp = _call(client, 'GET', f'/api/job-status/{JOB_A1}', token=tokens.mint(DOCTOR_A2))
    assert resp.status_code == 404


def test_job_status_wrong_owner_is_indistinguishable_from_not_found(client):
    """The existence-leak fix: a wrong-owner request against a job that
    DOES exist must return the exact same status + body as a request
    against a job_id that doesn't exist at all — otherwise the response
    itself reveals the job_id is real."""
    wrong_owner_resp = _call(client, 'GET', f'/api/job-status/{JOB_A1}', token=tokens.mint(DOCTOR_A2))
    not_found_resp = _call(client, 'GET', '/api/job-status/does-not-exist-at-all', token=tokens.mint(DOCTOR_A2))
    assert wrong_owner_resp.status_code == not_found_resp.status_code == 404
    assert wrong_owner_resp.get_json() == not_found_resp.get_json()


def test_pending_session_detail_is_owner_only_even_within_same_clinic(client):
    resp = _call(client, 'GET', f'/api/pending-sessions/{SESSION_PENDING_A}', token=tokens.mint(DOCTOR_A2))
    assert resp.status_code == 404


def test_pending_session_discard_is_owner_only_even_within_same_clinic(client):
    resp = _call(client, 'DELETE', f'/api/pending-sessions/{SESSION_PENDING_A}', token=tokens.mint(DOCTOR_A2))
    assert resp.status_code == 404


def test_pending_sessions_list_never_shows_a_colleagues_drafts(client):
    resp = _call(client, 'GET', '/api/pending-sessions', token=tokens.mint(DOCTOR_A2))
    assert resp.status_code == 200
    assert resp.get_json() == []  # DOCTOR_A1's pending draft must not appear for A2


# ── Step 7: owner access to their own resource succeeds ────────────────

def test_owner_can_read_own_pending_session(client):
    resp = _call(client, 'GET', f'/api/pending-sessions/{SESSION_PENDING_A}', token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 200


def test_owner_can_discard_own_pending_session(client):
    resp = _call(client, 'DELETE', f'/api/pending-sessions/{SESSION_PENDING_A}', token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 200


def test_owner_can_confirm_own_pending_session(client):
    resp = _call(
        client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1),
        json_body={'session_id': SESSION_PENDING_A, 'structured_data': {'informacion_paciente': {}}, 'create_pdf': False, 'send_email': False},
    )
    assert resp.status_code == 200


def test_owner_can_read_own_job_status(client):
    resp = _call(client, 'GET', f'/api/job-status/{JOB_A1}', token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 200


def test_owner_can_read_own_confirmed_session(client):
    resp = _call(client, 'GET', f'/api/session/{SESSION_CONFIRMED_A}', token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 200


# ── Step 8: admin, same-clinic target — allowed ─────────────────────────

def test_admin_can_deactivate_same_clinic_doctor(client):
    resp = _call(
        client, 'PATCH', f'/api/admin/usuarios/{DOCTOR_A1}/activo', token=tokens.mint(ADMIN_A),
        json_body={'activo': False},
    )
    assert resp.status_code == 200


def test_admin_cannot_deactivate_self(client):
    """Not a clinic-isolation case — a distinct, explicit guard in the
    route (usuario_id == g.usuario['usuario_id'])."""
    resp = _call(
        client, 'PATCH', f'/api/admin/usuarios/{ADMIN_A}/activo', token=tokens.mint(ADMIN_A),
        json_body={'activo': False},
    )
    assert resp.status_code == 403


def test_admin_can_download_same_clinic_session_pdf(client):
    resp = _call(client, 'GET', f'/api/admin/session/{SESSION_CONFIRMED_A}/pdf', token=tokens.mint(ADMIN_A))
    assert resp.status_code == 200


def test_admin_can_add_addendum_to_same_clinic_session(client):
    resp = _call(
        client, 'POST', f'/api/admin/session/{SESSION_CONFIRMED_A}/addendum', token=tokens.mint(ADMIN_A),
        json_body={'texto': 'Corrección de prueba (fixture).'},
    )
    assert resp.status_code == 200


def test_admin_can_cancel_same_clinic_session(client):
    resp = _call(
        client, 'POST', f'/api/admin/session/{SESSION_CONFIRMED_A}/cancel', token=tokens.mint(ADMIN_A),
        json_body={'cancellation_reason': 'Solicitud ARCO de prueba (fixture).'},
    )
    assert resp.status_code == 200


def test_admin_usuarios_list_only_shows_own_clinic(client):
    resp = _call(client, 'GET', '/api/admin/usuarios', token=tokens.mint(ADMIN_A))
    assert resp.status_code == 200
    ids = {row['id'] for row in resp.get_json()['doctores']}
    assert DOCTOR_B1 not in ids
    assert DOCTOR_A1 in ids


# ── Step 9: process-audio — 400 on missing consent is NOT an auth bypass ─

def test_process_audio_requires_consent_but_still_requires_auth_first(client):
    """Confirms the consent-gate (business validation) doesn't run before
    the auth gate — a bad token must still 401 even with no body at all."""
    resp = _call(client, 'POST', '/api/process-audio', token=tokens.BAD_SIGNATURE)
    assert resp.status_code == 401


def test_process_audio_authenticated_missing_consent_is_400_not_401(client):
    resp = _call(
        client, 'POST', '/api/process-audio', token=tokens.mint(DOCTOR_A1),
        data={'audio': (io.BytesIO(b'fake-audio-bytes'), 'test.wav')},
    )
    assert resp.status_code == 400
    assert resp.get_json().get('error_code') == 'CONSENT_REQUIRED'
