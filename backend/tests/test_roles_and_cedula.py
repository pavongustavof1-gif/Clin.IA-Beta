# backend/tests/test_roles_and_cedula.py
# Onboarding Review A: the 'auxiliar' role (non-doctor clinic admin),
# the last-admin guard, the #55 cédula-on-PDF fail-closed rule, and the
# provisioning helpers. Offline and PHI-free: every name, email, cédula
# and clinic below is invented; the data layer is conftest's FakeDB.

import io
import json
import re
import urllib.request

import pytest
from pypdf import PdfReader

import app as app_module
from pdf_generator import PDFGenerator
from tests import tokens
from tests.conftest import (
    CLINIC_A, DOCTOR_A1, DOCTOR_A2, ADMIN_A, AUXILIAR_A,
    SESSION_CONFIRMED_A, SESSION_PENDING_A, JOB_A1,
)
from tests.route_inventory import ROUTE_INVENTORY
from tests.test_route_authorization import _call, _fill


# ── helpers ──────────────────────────────────────────────────────────────

def _fail_cedula_lookups(db, monkeypatch):
    """Make every usuarios cédula read fail the way _sb_get really fails
    (returns None on HTTP error/timeout), leaving all other reads alone —
    so the REAL get_usuario_cedula/get_usuarios_cedulas run against it."""
    real_get = db.get

    def flaky_get(path):
        if path.startswith('/rest/v1/usuarios') and 'cedula' in path.split('select=')[-1]:
            return None
        return real_get(path)
    monkeypatch.setattr(app_module, '_sb_get', flaky_get)


def _use_real_pdf(monkeypatch):
    """conftest fakes generate_pdf; these tests need the real ReportLab output."""
    monkeypatch.setattr(app_module, 'pdf_generator', PDFGenerator())


def _pdf_text(pdf_bytes: bytes) -> str:
    pages = PdfReader(io.BytesIO(pdf_bytes)).pages
    return re.sub(r'\s+', ' ', ' '.join(p.extract_text() for p in pages))


def _spy_generate_pdf(monkeypatch):
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return b'%PDF-FAKE-BYTES'
    monkeypatch.setattr(app_module.pdf_generator, 'generate_pdf', spy)
    return calls


def _addendum(author_usuario_id, author='Autor (fixture)'):
    return {
        'id': 'adendum_1', 'type': 'rectificacion_arco',
        'text': 'Corrección de prueba (fixture).', 'author': author,
        'timestamp': '2026-01-03T09:30:00', 'author_usuario_id': author_usuario_id,
    }


# ── 1b. require_medico: an auxiliar is not a doctor ─────────────────────

MEDICO_ROUTES = [e for e in ROUTE_INVENTORY if e.get('medico_required')]


def test_medico_gated_route_set_is_exactly_these():
    """Pin the gated set, so a doctor-only route losing @require_medico
    (or a new one never getting it) shows up as a diff here."""
    gated = {(e['path'], m) for e in MEDICO_ROUTES for m in e['methods']}
    assert gated == {
        ('/api/process-audio', 'POST'),
        ('/api/job-status/<job_id>', 'GET'),
        ('/api/confirm-and-generate', 'POST'),
        ('/api/pending-sessions', 'GET'),
        ('/api/pending-sessions/<session_id>', 'GET'),
        ('/api/pending-sessions/<session_id>', 'DELETE'),
        ('/api/session/<session_id>', 'GET'),
        ('/api/export-json/<session_id>', 'GET'),
        ('/api/download-pdf/<session_id>', 'GET'),
        ('/api/patient-history/<session_id>', 'GET'),
        ('/api/patient-history', 'GET'),
    }


@pytest.mark.parametrize('entry', MEDICO_ROUTES, ids=lambda e: f"{e['methods'][0]} {e['path']}")
def test_auxiliar_gets_404_on_every_medico_route(client, entry):
    method = entry['methods'][0]
    # Real ids (not placeholders), so a 404 here can only come from the
    # role gate — these resources exist and are in the auxiliar's clinic.
    path = _fill(entry['path'], job_id=JOB_A1, session_id=SESSION_CONFIRMED_A)
    resp = _call(client, method, path, token=tokens.mint(AUXILIAR_A))
    assert resp.status_code == 404, f"{method} {entry['path']}: auxiliar got {resp.status_code}"


def test_medico_routes_still_open_to_admin_and_medico(client):
    for caller in (ADMIN_A, DOCTOR_A1):
        resp = _call(client, 'GET', '/api/pending-sessions', token=tokens.mint(caller))
        assert resp.status_code == 200, f"{caller} lost access to a doctor route"


def test_inventory_medico_flag_matches_real_decorators():
    """Cross-check the inventory against the real view functions: every
    medico_required route must actually 404 an auxiliar (above), and no
    route without the flag may be wrapped by require_medico."""
    import auth as auth_module
    for rule in app_module.app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        view = app_module.app.view_functions[rule.endpoint]
        wrapped = False
        fn = view
        while hasattr(fn, '__wrapped__'):
            if fn.__code__ is auth_module.require_medico(lambda: None).__code__:
                wrapped = True
            fn = fn.__wrapped__
        flagged = any(e['path'] == rule.rule and e.get('medico_required') for e in ROUTE_INVENTORY)
        assert wrapped == flagged, f"{rule.rule}: require_medico={wrapped} but inventory says {flagged}"


# ── 1a. admin routes accept an auxiliar ──────────────────────────────────

ADMIN_ROUTES = [e for e in ROUTE_INVENTORY if e['admin_required']]


@pytest.mark.parametrize('entry', ADMIN_ROUTES, ids=lambda e: f"{e['methods'][0]} {e['path']}")
def test_auxiliar_passes_admin_gate_on_every_admin_route(client, entry):
    method = entry['methods'][0]
    path = _fill(entry['path'], session_id=SESSION_CONFIRMED_A, usuario_id=DOCTOR_A2)
    resp = _call(client, method, path, token=tokens.mint(AUXILIAR_A), json_body={})
    # The admin gate is the only thing that 403s on these routes; anything
    # else (a 400 for the empty body, etc.) means the auxiliar got past it.
    assert resp.status_code not in (401, 403), f"{method} {entry['path']}: auxiliar got {resp.status_code}"


@pytest.mark.parametrize('path', [
    '/api/admin/usuarios',
    '/api/admin/clinica',
    f'/api/admin/session/{SESSION_CONFIRMED_A}',
    f'/api/admin/session/{SESSION_CONFIRMED_A}/pdf',
    '/api/admin/sessions?desde=2020-01-01&hasta=2030-01-01',
])
def test_auxiliar_gets_200_on_admin_reads(client, path):
    resp = _call(client, 'GET', path, token=tokens.mint(AUXILIAR_A))
    assert resp.status_code == 200


def test_session_check_reports_auxiliar_rol(client):
    """The front end's / → /admin redirect keys off this (app.js)."""
    resp = _call(client, 'GET', '/api/session-check', token=tokens.mint(AUXILIAR_A))
    assert resp.status_code == 200
    assert resp.get_json()['rol'] == 'auxiliar'


def test_admin_session_detail_is_clinic_scoped(client):
    resp = _call(client, 'GET', '/api/admin/session/SESSION-CONFIRMED-B', token=tokens.mint(AUXILIAR_A))
    assert resp.status_code == 404


def test_admin_session_detail_matches_patient_history_body(client, monkeypatch):
    logged = []
    monkeypatch.setattr(app_module, '_sb_log_lectura', lambda sid, uid: logged.append((sid, uid)))
    admin_view = _call(client, 'GET', f'/api/admin/session/{SESSION_CONFIRMED_A}', token=tokens.mint(ADMIN_A)).get_json()
    doctor_view = _call(client, 'GET', f'/api/patient-history/{SESSION_CONFIRMED_A}', token=tokens.mint(ADMIN_A)).get_json()
    assert admin_view == doctor_view
    assert logged == [(SESSION_CONFIRMED_A, ADMIN_A)] * 2


# ── 1d. admin invite: médico or auxiliar, never admin ───────────────────

def _invite(client, body, caller=ADMIN_A):
    return _call(client, 'POST', '/api/admin/usuarios', token=tokens.mint(caller), json_body=body)


@pytest.fixture
def inserted(monkeypatch):
    rows = []

    def fake_insert(body):
        rows.append(body)
        return {**body}
    monkeypatch.setattr(app_module, '_sb_insert_usuario', fake_insert)
    return rows


def test_invite_auxiliar_without_cedula_works(client, inserted):
    resp = _invite(client, {'nombre': 'Aux Inventado', 'email': 'aux.inventado@example.test', 'rol': 'auxiliar'})
    assert resp.status_code == 200
    assert resp.get_json()['rol'] == 'auxiliar'
    assert inserted[0]['rol'] == 'auxiliar'
    for field in ('cedula', 'especialidad', 'universidad'):
        assert field not in inserted[0]
    assert inserted[0]['clinica_id'] == CLINIC_A


def test_invite_auxiliar_with_cedula_is_refused(client, inserted):
    resp = _invite(client, {'nombre': 'Aux Inventado', 'email': 'aux.dos@example.test',
                            'rol': 'auxiliar', 'cedula': '1234567'})
    assert resp.status_code == 400
    assert inserted == []


def test_invite_medico_without_cedula_fails(client, inserted):
    resp = _invite(client, {'nombre': 'Dra. Inventada', 'email': 'dra.inventada@example.test',
                            'rol': 'medico', 'especialidad': 'Pediatría'})
    assert resp.status_code == 400
    assert inserted == []


def test_invite_medico_with_universidad(client, inserted):
    resp = _invite(client, {'nombre': 'Dra. Inventada', 'email': 'dra.inventada@example.test',
                            'rol': 'medico', 'especialidad': 'Pediatría', 'cedula': '7654321',
                            'universidad': 'Universidad Inventada'})
    assert resp.status_code == 200
    assert inserted[0]['universidad'] == 'Universidad Inventada'
    assert inserted[0]['cedula'] == '7654321'


def test_invite_without_rol_defaults_to_medico_with_unchanged_payload(client, inserted):
    """An older admin.js (or any client) that sends no rol gets today's
    behavior exactly: a médico, and the same insert keys as before."""
    resp = _invite(client, {'nombre': 'Dr. Inventado', 'email': 'dr.inventado@example.test',
                            'especialidad': 'Medicina General', 'cedula': '1122334'})
    assert resp.status_code == 200
    assert inserted[0]['rol'] == 'medico'
    assert set(inserted[0]) == {'id', 'nombre', 'email', 'rol', 'clinica_id', 'activo', 'cedula', 'especialidad'}


@pytest.mark.parametrize('rol', ['admin', 'ADMIN', ' admin ', 'superadmin', 'otro', 1, ['admin']])
def test_admin_route_can_never_create_admin(client, inserted, rol):
    resp = _invite(client, {'nombre': 'Dr. Inventado', 'email': 'dr.inventado@example.test',
                            'rol': rol, 'especialidad': 'Medicina General', 'cedula': '1122334'})
    assert resp.status_code == 400
    assert inserted == []


def test_auxiliar_can_invite_into_own_clinic(client, inserted):
    resp = _invite(client, {'nombre': 'Dr. Inventado', 'email': 'dr.tres@example.test',
                            'especialidad': 'Medicina General', 'cedula': '1122334'}, caller=AUXILIAR_A)
    assert resp.status_code == 200
    assert inserted[0]['clinica_id'] == CLINIC_A




# ── 2. last-admin guard ──────────────────────────────────────────────────

def _deactivate(client, target, caller):
    return _call(client, 'PATCH', f'/api/admin/usuarios/{target}/activo',
                 token=tokens.mint(caller), json_body={'activo': False})


def _set_activo(db, uid, activo):
    for row in db.tables['usuarios']:
        if row['id'] == uid:
            row['activo'] = activo


@pytest.fixture
def bans(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, '_sb_set_user_ban', lambda uid, banned: calls.append((uid, banned)))
    return calls


def test_guard_last_active_admin_is_refused(db):
    # Clinic A's admin-role users are ADMIN_A and AUXILIAR_A.
    _set_activo(db, AUXILIAR_A, False)
    assert app_module._would_leave_clinic_without_admin(CLINIC_A, ADMIN_A) is True


def test_guard_one_of_two_admins_is_allowed(db):
    assert app_module._would_leave_clinic_without_admin(CLINIC_A, ADMIN_A) is False


def test_guard_counts_an_auxiliar_as_admin(db):
    """With ADMIN_A gone, the active auxiliar is what keeps the clinic
    administered — so removing it is refused, and its presence is what
    lets ADMIN_A be removed."""
    _set_activo(db, ADMIN_A, False)
    assert app_module._would_leave_clinic_without_admin(CLINIC_A, AUXILIAR_A) is True
    _set_activo(db, ADMIN_A, True)
    assert app_module._would_leave_clinic_without_admin(CLINIC_A, ADMIN_A) is False


def test_guard_ignores_medicos_and_other_clinics(db):
    """Active médicos in the clinic and admins in OTHER clinics never
    count — only clinic A's own admin/auxiliar rows do."""
    _set_activo(db, AUXILIAR_A, False)
    assert app_module._would_leave_clinic_without_admin(CLINIC_A, ADMIN_A) is True


def test_guard_lookup_failure_fails_closed(client, bans, monkeypatch):
    real_get = app_module._sb_get

    def flaky(path):
        if 'rol=in.' in path:
            return None
        return real_get(path)
    monkeypatch.setattr(app_module, '_sb_get', flaky)
    resp = _deactivate(client, AUXILIAR_A, caller=ADMIN_A)
    assert resp.status_code == 500
    assert bans == []


def test_route_allows_deactivating_one_of_two_admins(client, db, bans):
    resp = _deactivate(client, AUXILIAR_A, caller=ADMIN_A)
    assert resp.status_code == 200
    assert bans == [(AUXILIAR_A, True)]


def test_route_refuses_last_admin_even_with_stale_caller_context(client, db, bans):
    """The only way the route itself can reach a last-admin state today:
    the caller's own usuarios row was deactivated out-of-band (e.g. in the
    Supabase dashboard) while its auth context is still cached for up to
    _USUARIO_CACHE_TTL_SECONDS. The guard reads the DB, not the cache."""
    import time
    import auth as auth_module
    _set_activo(db, ADMIN_A, False)
    auth_module._usuario_cache[ADMIN_A] = (time.monotonic() + 20, {
        'usuario_id': ADMIN_A, 'clinica_id': CLINIC_A, 'rol': 'admin',
        'nombre': 'Dr. admin-a', 'email': 'admin-a@example.test',
    })
    resp = _deactivate(client, AUXILIAR_A, caller=ADMIN_A)
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'La clínica debe tener al menos un administrador activo.'
    assert bans == []
    assert next(r for r in db.tables['usuarios'] if r['id'] == AUXILIAR_A)['activo'] is True


def test_guard_does_not_block_deactivating_a_medico(client, db, bans):
    _set_activo(db, AUXILIAR_A, False)   # ADMIN_A is now the only admin
    resp = _deactivate(client, DOCTOR_A2, caller=ADMIN_A)
    assert resp.status_code == 200


def test_guard_does_not_block_reactivation(client, db, bans):
    _set_activo(db, AUXILIAR_A, False)
    resp = _call(client, 'PATCH', f'/api/admin/usuarios/{AUXILIAR_A}/activo',
                 token=tokens.mint(ADMIN_A), json_body={'activo': True})
    assert resp.status_code == 200


# ── 3. #55 cédula on PDFs ────────────────────────────────────────────────

def test_get_usuario_cedula_distinguishes_empty_from_failed(db, monkeypatch):
    assert app_module.get_usuario_cedula(DOCTOR_A1) == f'CED-{DOCTOR_A1}'
    assert app_module.get_usuario_cedula(AUXILIAR_A) == ''          # null column
    assert app_module.get_usuario_cedula('nobody-at-all') == ''     # no row
    monkeypatch.setattr(app_module, '_sb_get', lambda path: None)
    assert app_module.get_usuario_cedula(DOCTOR_A1) is None         # lookup failed


def test_get_usuarios_cedulas_batch(db, monkeypatch):
    assert app_module.get_usuarios_cedulas([ADMIN_A, AUXILIAR_A, 'nobody-at-all', None]) == {
        ADMIN_A: f'CED-{ADMIN_A}', AUXILIAR_A: '', 'nobody-at-all': '',
    }
    assert app_module.get_usuarios_cedulas([]) == {}
    monkeypatch.setattr(app_module, '_sb_get', lambda path: None)
    assert app_module.get_usuarios_cedulas([ADMIN_A]) is None


PDF_DOWNLOAD_PATHS = [
    pytest.param(f'/api/download-pdf/{SESSION_CONFIRMED_A}', DOCTOR_A1, id='doctor-download'),
    pytest.param(f'/api/admin/session/{SESSION_CONFIRMED_A}/pdf', ADMIN_A, id='admin-download'),
]


@pytest.mark.parametrize('path,caller', PDF_DOWNLOAD_PATHS)
def test_download_prints_author_cedula(client, monkeypatch, path, caller):
    _use_real_pdf(monkeypatch)
    resp = _call(client, 'GET', path, token=tokens.mint(caller))
    assert resp.status_code == 200
    text = _pdf_text(resp.data)
    # Carta + signature block: the AUTHOR's (DOCTOR_A1's) cédula, twice.
    assert text.count(f'Céd. Prof. CED-{DOCTOR_A1}') == 2


@pytest.mark.parametrize('path,caller', PDF_DOWNLOAD_PATHS)
def test_download_with_failed_cedula_lookup_renders_nothing(client, db, monkeypatch, path, caller):
    calls = _spy_generate_pdf(monkeypatch)
    _fail_cedula_lookups(db, monkeypatch)
    resp = _call(client, 'GET', path, token=tokens.mint(caller))
    assert resp.status_code == 503
    assert resp.get_json()['error'] == 'No se pudo generar el PDF. Intente de nuevo.'
    assert resp.mimetype == 'application/json'
    assert calls == []


@pytest.mark.parametrize('path,caller', PDF_DOWNLOAD_PATHS)
def test_download_with_empty_cedula_still_renders(client, db, monkeypatch, path, caller):
    """Historical notes by an author with no cédula on file still download."""
    _use_real_pdf(monkeypatch)
    for row in db.tables['usuarios']:
        if row['id'] == DOCTOR_A1:
            row['cedula'] = None
    resp = _call(client, 'GET', path, token=tokens.mint(caller))
    assert resp.status_code == 200
    assert 'Céd. Prof.' not in _pdf_text(resp.data)


@pytest.mark.parametrize('path,caller', PDF_DOWNLOAD_PATHS)
def test_failed_addendum_author_lookup_also_renders_nothing(client, db, monkeypatch, path, caller):
    calls = _spy_generate_pdf(monkeypatch)
    for row in db.tables['sesiones']:
        if row['session_id'] == SESSION_CONFIRMED_A:
            row['addenda'] = [_addendum(ADMIN_A)]
    real_get = db.get
    monkeypatch.setattr(app_module, '_sb_get',
                        lambda p: None if 'id=in.(' in p and 'cedula' in p else real_get(p))
    resp = _call(client, 'GET', path, token=tokens.mint(caller))
    assert resp.status_code == 503
    assert calls == []


def test_addendum_cedula_shown_for_admin_absent_for_auxiliar(client, db, monkeypatch):
    _use_real_pdf(monkeypatch)
    for row in db.tables['sesiones']:
        if row['session_id'] == SESSION_CONFIRMED_A:
            row['addenda'] = [
                _addendum(ADMIN_A, author='Dr. Admin Inventado'),
                {**_addendum(AUXILIAR_A, author='Aux. Inventada'), 'id': 'adendum_2'},
            ]
    resp = _call(client, 'GET', f'/api/admin/session/{SESSION_CONFIRMED_A}/pdf', token=tokens.mint(ADMIN_A))
    assert resp.status_code == 200
    text = _pdf_text(resp.data)
    assert f'Céd. Prof. CED-{ADMIN_A}' in text
    # The only cédulas in the PDF: the author's two + the admin addendum's one.
    assert text.count('Céd. Prof.') == 3
    assert 'Aux. Inventada' in text


def test_addendum_cedula_is_never_written_back(client, db, monkeypatch):
    _use_real_pdf(monkeypatch)
    for row in db.tables['sesiones']:
        if row['session_id'] == SESSION_CONFIRMED_A:
            row['addenda'] = [_addendum(ADMIN_A)]
    _call(client, 'GET', f'/api/download-pdf/{SESSION_CONFIRMED_A}', token=tokens.mint(DOCTOR_A1))
    stored = next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_CONFIRMED_A)['addenda']
    assert 'author_cedula' not in stored[0]


def test_addendum_pdf_section_unit():
    """PDFGenerator itself: a cédula line only when author_cedula is set."""
    sd = {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
          'subjetivo': {'motivo_de_consulta': 'chequeo (fixture)'}}
    pdf = PDFGenerator().generate_pdf(sd, session_id='SESSION-FX', doctor_info={'nombre': 'Dr. X'}, adenda=[
        {**_addendum('u1', author='Dr. Uno'), 'author_cedula': '1234567'},
        {**_addendum('u2', author='Aux. Dos'), 'author_cedula': ''},
    ])
    text = _pdf_text(pdf)
    assert 'Céd. Prof. 1234567' in text
    assert text.count('Céd. Prof.') == 1


# ── 3c. confirm-and-generate: lookup before confirming ──────────────────

@pytest.fixture
def saved(monkeypatch):
    """save_session does a raw urllib POST conftest doesn't route through
    FakeDB — record calls instead of letting it attempt the network."""
    calls = []
    monkeypatch.setattr(app_module, 'save_session', lambda sid, data, **k: calls.append((sid, data['status'])))
    return calls


def _confirm(client, create_pdf=True, caller=DOCTOR_A1):
    return _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(caller), json_body={
        'session_id': SESSION_PENDING_A,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
                            'subjetivo': {'motivo_de_consulta': 'chequeo (fixture)'}},
        'create_pdf': create_pdf, 'send_email': True,
    })


def test_confirm_with_failed_lookup_leaves_note_unconfirmed(client, db, monkeypatch, saved):
    pdf_calls = _spy_generate_pdf(monkeypatch)
    emails = []
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: emails.append(k) or True)
    patches, deletes = [], []
    monkeypatch.setattr(app_module, '_sb_patch', lambda p, b: patches.append(p) or True)
    monkeypatch.setattr(app_module, '_sb_delete', lambda p: deletes.append(p) or True)
    _fail_cedula_lookups(db, monkeypatch)

    resp = _confirm(client)
    assert resp.status_code == 503
    assert resp.get_json()['error'] == 'No se pudo generar el PDF. Intente de nuevo.'
    # Nothing happened: no PDF, no confirm, no transcript/trabajos delete, no email.
    assert pdf_calls == [] and saved == [] and patches == [] and deletes == [] and emails == []
    row = next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_PENDING_A)
    assert row['status'] == 'pending_review'


def test_confirm_retry_after_failed_lookup_succeeds(client, db, monkeypatch, saved):
    _spy_generate_pdf(monkeypatch)
    real_get = db.get
    state = {'fail': True}
    monkeypatch.setattr(app_module, '_sb_get',
                        lambda p: None if state['fail'] and 'select=cedula' in p else real_get(p))
    assert _confirm(client).status_code == 503
    state['fail'] = False
    resp = _confirm(client)
    assert resp.status_code == 200
    assert saved == [(SESSION_PENDING_A, 'confirmed')]


def test_confirm_prints_signing_doctor_cedula(client, monkeypatch, saved):
    _use_real_pdf(monkeypatch)
    sent = []
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: sent.append(k['pdf_bytes']) or True)
    resp = _confirm(client)
    assert resp.status_code == 200
    # The emailed attachment is the confirm path's PDF.
    assert _pdf_text(sent[0]).count(f'Céd. Prof. CED-{DOCTOR_A1}') == 2


def test_confirm_with_empty_cedula_still_confirms_and_renders(client, db, monkeypatch, saved):
    _use_real_pdf(monkeypatch)
    sent = []
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: sent.append(k['pdf_bytes']) or True)
    for row in db.tables['usuarios']:
        if row['id'] == DOCTOR_A1:
            row['cedula'] = ''
    resp = _confirm(client)
    assert resp.status_code == 200
    assert saved == [(SESSION_PENDING_A, 'confirmed')]
    assert 'Céd. Prof.' not in _pdf_text(sent[0])


def test_confirm_without_pdf_needs_no_cedula_lookup(client, db, monkeypatch, saved):
    _fail_cedula_lookups(db, monkeypatch)
    resp = _confirm(client, create_pdf=False)
    assert resp.status_code == 200
    assert saved == [(SESSION_PENDING_A, 'confirmed')]


# ── 4. provisioning helpers ──────────────────────────────────────────────

def test_provision_usuario_medico(monkeypatch):
    inserted = []
    monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda b: inserted.append(b) or dict(b))
    row, link = app_module._provision_usuario(
        'Dra. Inventada', 'dra@example.test', 'medico', CLINIC_A,
        cedula='7654321', especialidad='Pediatría',
    )
    assert link == 'https://fake.invite/link'
    assert row['id'] == 'fake-invited-user-id'
    assert inserted == [{
        'id': 'fake-invited-user-id', 'nombre': 'Dra. Inventada', 'email': 'dra@example.test',
        'rol': 'medico', 'clinica_id': CLINIC_A, 'activo': True,
        'cedula': '7654321', 'especialidad': 'Pediatría',
    }]


def test_provision_usuario_invite_failure(monkeypatch):
    def boom(email):
        raise RuntimeError('fake generate_link outage')
    inserted = []
    monkeypatch.setattr(app_module, '_sb_generate_invite_link', boom)
    monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda b: inserted.append(b))
    with pytest.raises(app_module.ProvisioningError) as exc:
        app_module._provision_usuario('X', 'x@example.test', 'auxiliar', CLINIC_A)
    assert exc.value.stage == 'invite'
    assert inserted == []


def test_provision_usuario_insert_failure(monkeypatch):
    monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda b: None)
    with pytest.raises(app_module.ProvisioningError) as exc:
        app_module._provision_usuario('X', 'x@example.test', 'auxiliar', CLINIC_A)
    assert exc.value.stage == 'insert'


@pytest.mark.parametrize('stage,fail', [('invite', 'link'), ('insert', 'insert')])
def test_admin_create_usuario_maps_provisioning_failures(client, monkeypatch, stage, fail):
    if fail == 'link':
        def boom(email):
            raise RuntimeError('fake outage')
        monkeypatch.setattr(app_module, '_sb_generate_invite_link', boom)
    else:
        monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda b: None)
    resp = _call(client, 'POST', '/api/admin/usuarios', token=tokens.mint(ADMIN_A), json_body={
        'nombre': 'Dr. Inventado', 'email': 'dr.cuatro@example.test',
        'especialidad': 'Medicina General', 'cedula': '1122334',
    })
    assert resp.status_code == 500
    if stage == 'invite':
        assert resp.get_json()['error'] == 'No se pudo crear la invitación. Intente de nuevo.'
    else:
        assert 'usuario huérfano' in resp.get_json()['error']


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sb_insert_clinica_request_shape(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen['url'] = req.full_url
        seen['method'] = req.get_method()
        seen['prefer'] = req.get_header('Prefer')
        seen['body'] = json.loads(req.data)
        return _FakeResponse([{'id': 'new-clinic-id', **seen['body']}])
    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    row = app_module._sb_insert_clinica('Consultorio Inventado', 'Calle Falsa 123', '5550001111')
    assert row == {'id': 'new-clinic-id', 'nombre': 'Consultorio Inventado',
                   'direccion': 'Calle Falsa 123', 'telefono': '5550001111'}
    assert seen['url'].endswith('/rest/v1/clinicas')
    assert seen['method'] == 'POST'
    assert seen['prefer'] == 'return=representation'
    assert seen['body'] == {'nombre': 'Consultorio Inventado', 'direccion': 'Calle Falsa 123',
                            'telefono': '5550001111'}


def test_sb_insert_clinica_failure_returns_none(monkeypatch):
    def boom(req, timeout):
        raise OSError('fake network down')
    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    assert app_module._sb_insert_clinica('X', '', '') is None


def test_sb_insert_clinica_has_no_route_caller():
    import inspect
    src = inspect.getsource(app_module)
    assert src.count('_sb_insert_clinica(') == 1   # the definition only
