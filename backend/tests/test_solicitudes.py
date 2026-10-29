# backend/tests/test_solicitudes.py
# Onboarding Review B: the public access-request form (#53/#54) —
# GET /solicitud, GET /aviso-privacidad-usuarios, POST /api/solicitudes.
# Offline and PHI-free: every name, email, cédula, address and phone
# below is invented; the data layer is conftest's FakeDB, and the insert
# and superadmin email are conftest fakes unless a test swaps them.

import io
import json
import logging
import time
import urllib.error

import pytest

import app as app_module
import email_service
import solicitud_validation as sv
from config import Config
from logger import logger as clinia_logger

URL = '/api/solicitudes'
GENERIC = 'Recibimos tu solicitud. Te escribiremos al correo que indicaste.'

# Distinctive invented values, so the log-capture test can grep for them.
NOMBRE = 'Dra. Ximena Inventada Quiroz'
EMAIL = 'ximena.inventada@clinica-ficticia.mx'
CEDULA = '98765432'
TELEFONO = '55 0000 4321'
DOMICILIO = 'Calle Ficticia 123, Col. Imaginaria, CDMX'


def _body(**overrides):
    body = {
        'nombre': NOMBRE,
        'email': EMAIL,
        'rol': 'medico',
        'cedula': CEDULA,
        'especialidad': 'Pediatría',
        'universidad': 'Universidad Inventada',
        'razon_social': 'Consultorio Ficticio S.C.',
        'domicilio': DOMICILIO,
        'telefono': TELEFONO,
        'recomendado_por': 'Dr. Colega Inventado',
        'acepta_terminos': True,
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not ...}


def _post(client, body, ip='203.0.113.10'):
    return client.post(URL, json=body, headers={'X-Forwarded-For': ip})


@pytest.fixture
def rows(db):
    return db.tables['solicitudes_registro']


@pytest.fixture
def notices(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, 'send_solicitud_notice', lambda to, nombre: sent.append((to, nombre)) or True)
    monkeypatch.setattr(Config, 'SUPERADMIN_EMAIL', 'superadmin@clinia-ficticia.mx')
    return sent


@pytest.fixture
def log_lines():
    """The clinia logger doesn't propagate to root (logger.py), so caplog
    can't see it — attach a capturing handler to it directly."""
    lines = []

    class _H(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    h = _H(level=logging.DEBUG)
    old_level = clinia_logger.level
    clinia_logger.addHandler(h)
    clinia_logger.setLevel(logging.DEBUG)
    yield lines
    clinia_logger.removeHandler(h)
    clinia_logger.setLevel(old_level)


# ── pages ────────────────────────────────────────────────────────────────

def test_solicitud_page_is_noindex_and_has_no_supabase(client):
    resp = client.get('/solicitud')
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert '<meta name="robots" content="noindex">' in page
    assert 'supabase' not in page.lower()
    assert 'solicitud.js' in page
    assert 'https://clinianotes.com/#terminos' in page
    assert '/aviso-privacidad-usuarios' in page
    # input types / modes for phone keyboards
    assert 'type="email"' in page and 'type="tel"' in page and 'inputmode="numeric"' in page


def test_solicitud_page_field_order(client):
    page = client.get('/solicitud').get_data(as_text=True)
    order = ['name="nombre"', 'name="email"', 'name="rol"', 'name="cedula"', 'name="especialidad"',
             'name="universidad"', 'name="razon_social"', 'name="domicilio"', 'name="telefono"',
             'name="recomendado_por"', 'name="acepta_terminos"', 'type="submit"']
    positions = [page.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_honeypot_is_hidden_from_people_and_keyboards(client):
    page = client.get('/solicitud').get_data(as_text=True)
    hp = page[page.index('class="sol-hp"') - 10:page.index('name="sitio_web"') + 80]
    assert 'aria-hidden="true"' in hp
    assert 'tabindex="-1"' in hp


def test_aviso_privacidad_usuarios_placeholder(client):
    resp = client.get('/aviso-privacidad-usuarios')
    assert resp.status_code == 200
    assert 'Aviso de Privacidad para usuarios — en preparación' in resp.get_data(as_text=True)


# ── happy paths ──────────────────────────────────────────────────────────

def test_medico_request_is_stored(client, rows, notices):
    resp = _post(client, _body())
    assert resp.status_code == 200
    assert resp.get_json()['mensaje'] == GENERIC
    assert len(rows) == 1
    row = rows[0]
    assert row['rol_solicitado'] == 'medico'
    assert row['cedula'] == CEDULA and row['especialidad'] == 'Pediatría'
    assert row['universidad'] == 'Universidad Inventada'
    assert row['terminos_version'] == Config.TERMINOS_VERSION == 'ToS-2026-10'
    assert row['terminos_aceptados_at'].endswith('+00:00')
    assert row['estado'] == 'pendiente'  # DB default (FakeDB insert mirrors it)
    # nothing the client sent beyond the known columns reaches the row
    assert 'acepta_terminos' not in row and 'rol' not in row and 'sitio_web' not in row


def test_admin_request_is_stored(client, rows, notices):
    assert _post(client, _body(rol='admin')).status_code == 200
    assert rows[0]['rol_solicitado'] == 'admin'


def test_auxiliar_without_cedula_or_especialidad_is_accepted(client, rows, notices):
    resp = _post(client, _body(rol='auxiliar', cedula=..., especialidad=..., universidad=...))
    assert resp.status_code == 200
    assert rows[0]['rol_solicitado'] == 'auxiliar'
    assert rows[0]['cedula'] is None and rows[0]['especialidad'] is None and rows[0]['universidad'] is None


def test_auxiliar_hidden_field_values_are_ignored(client, rows, notices):
    """Values a hand-built request (or a stale hidden input) sends for an
    auxiliar never reach the row — even invalid ones don't cause a 400."""
    resp = _post(client, _body(rol='auxiliar', cedula='ABC-not-digits', especialidad='x' * 500,
                               universidad='y' * 500))
    assert resp.status_code == 200
    assert rows[0]['cedula'] is None and rows[0]['especialidad'] is None and rows[0]['universidad'] is None


def test_optional_fields_absent_are_stored_as_null(client, rows, notices):
    assert _post(client, _body(universidad=..., recomendado_por='   ')).status_code == 200
    assert rows[0]['universidad'] is None and rows[0]['recomendado_por'] is None


# ── normalization ────────────────────────────────────────────────────────

def test_text_normalization(client, rows, notices):
    resp = _post(client, _body(
        nombre='  Dra.​  Ximena \t Inventada﻿  ',
        email=' ⁠Ximena.Inventada@Clinica-Ficticia.MX ',
        cedula=' 98 765 432 ',
        razon_social='Consultorio\n\nFicticio   S.C.',
    ))
    assert resp.status_code == 200
    row = rows[0]
    assert row['nombre'] == 'Dra. Ximena Inventada'
    assert row['email'] == EMAIL
    assert row['cedula'] == CEDULA
    assert row['razon_social'] == 'Consultorio Ficticio S.C.'


def test_normalize_helpers_unit():
    assert sv.normalize_text('​a⁠   b﻿') == 'a b'
    assert sv.normalize_text(None) == ''
    assert sv.normalize_email(' A@B.MX ') == 'a@b.mx'
    assert sv.normalize_cedula(' 12 34\t56 ') == '123456'


# ── validation (400, per-field Spanish messages) ─────────────────────────

@pytest.mark.parametrize('field,value', [
    ('nombre', ...), ('nombre', 'Al'), ('nombre', 'x' * 121), ('nombre', 12345),
    ('email', ...), ('email', 'x' * 245 + '@clinica.mx'),
    ('rol', ...), ('rol', 'superadmin'), ('rol', ['medico']),
    ('cedula', ...), ('cedula', '12A45'), ('cedula', '1234567890123'), ('cedula', '12-345'),
    ('especialidad', ...), ('especialidad', 'x' * 81),
    ('universidad', 'x' * 161),
    ('razon_social', ...), ('razon_social', 'X'), ('razon_social', 'x' * 161),
    ('domicilio', ...), ('domicilio', 'x' * 301),
    ('telefono', ...), ('telefono', 'x' * 31), ('telefono', '12345'), ('telefono', 'sin teléfono'),
    ('recomendado_por', 'x' * 121),
    ('acepta_terminos', ...), ('acepta_terminos', False), ('acepta_terminos', 'true'), ('acepta_terminos', 1),
])
def test_field_validation(client, rows, notices, field, value):
    resp = _post(client, _body(**{field: value}))
    assert resp.status_code == 400, resp.get_json()
    data = resp.get_json()
    assert field in data['errores']
    assert data['error'] and isinstance(data['errores'][field], str)
    assert rows == [] and notices == []


@pytest.mark.parametrize('rol', ['medico', 'admin'])
@pytest.mark.parametrize('missing', ['cedula', 'especialidad'])
def test_doctor_roles_need_cedula_and_especialidad(client, rows, rol, missing):
    resp = _post(client, _body(rol=rol, **{missing: ...}))
    assert resp.status_code == 400
    assert missing in resp.get_json()['errores']
    assert rows == []


def test_cedula_twelve_digits_is_the_limit(client, rows, notices):
    assert _post(client, _body(cedula='1' * 12)).status_code == 200
    assert _post(client, _body(email='otra.inventada@clinica-ficticia.mx', cedula='1' * 13)).status_code == 400


def test_messages_are_spanish(client):
    data = _post(client, _body(email='no-es-correo', acepta_terminos=False)).get_json()
    assert 'correo' in data['errores']['email'].lower()
    assert 'Términos' in data['errores']['acepta_terminos']


# Each case uses a local part no other rule rejects, so it proves its own
# rule (the expected message says which rule fired).
_MALFORMED = 'válido'
_RESERVED = 'no recibe mensajes'
_PLACEHOLDER = 'de ejemplo'
_DISPOSABLE = 'temporales'


@pytest.mark.parametrize('email,why', [
    ('sin-arroba.mx', _MALFORMED), ('dra.lopez@sinpunto', _MALFORMED), ('dra.lopez@clinica.m', _MALFORMED),
    ('dra.lopez@.mx', _MALFORMED), ('dra.lopez@clinica..mx', _MALFORMED), ('dra..lopez@clinica.mx', _MALFORMED),
    ('.dralopez@clinica.mx', _MALFORMED), ('dralopez.@clinica.mx', _MALFORMED),
    ('dra%lopez@clinica.mx', _MALFORMED), ('dra.lopez@clin%ica.mx', _MALFORMED), ('dra lopez@clinica.mx', _MALFORMED),
    ('"dralopez"@clinica.mx', _MALFORMED), ('dra.lopez@clinica.mx@otra.mx', _MALFORMED),
    ('dra.lopez@-clinica.mx', _MALFORMED), ('x' * 65 + '@clinica.mx', _MALFORMED),
    ('dra.lopez@example.com', _RESERVED), ('dra.lopez@example.org', _RESERVED),
    ('dra.lopez@example.com.mx', _RESERVED), ('dra.lopez@mail.example.net', _RESERVED),
    ('dra.lopez@clinica.test', _RESERVED), ('dra.lopez@clinica.invalid', _RESERVED),
    ('dra.lopez@clinica.localhost', _RESERVED), ('dra.lopez@clinica.example', _RESERVED),
    ('test@gmail.com', _PLACEHOLDER), ('prueba@hotmail.com', _PLACEHOLDER), ('correo@outlook.com', _PLACEHOLDER),
    ('dra.lopez@dominio.com', _PLACEHOLDER), ('dra.lopez@tudominio.com', _PLACEHOLDER),
    ('dra.lopez@mailinator.com', _DISPOSABLE), ('dra.lopez@yopmail.com', _DISPOSABLE),
    ('dra.lopez@10minutemail.com', _DISPOSABLE), ('dra.lopez@sub.mailinator.com', _DISPOSABLE),
])
def test_email_rejections(email, why):
    problem = sv.email_problem(email)
    assert problem is not None and why in problem, (email, problem)


@pytest.mark.parametrize('email', [
    'ximena.inventada@clinica-ficticia.mx', 'dr.perez+clinia@gmail.com', 'a_b-c@hospital.com.mx',
    'contacto@consultorio.org', 'dra.lopez@outlook.es',
])
def test_email_acceptances(email):
    assert sv.email_problem(email) is None, email


def test_rejected_email_is_a_400_with_no_insert(client, rows, notices):
    resp = _post(client, _body(email='dra.lopez@mailinator.com'))
    assert resp.status_code == 400 and 'email' in resp.get_json()['errores']
    assert rows == [] and notices == []


# ── silent drops: same generic success, nothing inserted ─────────────────

def test_honeypot_returns_generic_success_and_inserts_nothing(client, rows, notices):
    resp = _post(client, _body(sitio_web='https://spam.invalid'))
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert rows == [] and notices == []


def test_honeypot_beats_validation(client, rows):
    """A bot filling everything with junk learns nothing from a 400."""
    resp = _post(client, {'sitio_web': 'x', 'email': 'basura'})
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert rows == []


def test_email_already_a_user_returns_generic_success(client, db, rows, notices):
    db.add('usuarios', {'id': 'u-existente', 'clinica_id': 'clinic-a-11111111', 'rol': 'medico', 'activo': True,
                        'nombre': 'Ya Existe', 'email': 'Ximena.Inventada@clinica-ficticia.mx'})
    resp = _post(client, _body())
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert rows == [] and notices == []


def test_open_request_for_email_returns_generic_success(client, db, rows, notices):
    db.add('solicitudes_registro', {'id': 's-1', 'email': EMAIL, 'estado': 'en_proceso'})
    resp = _post(client, _body())
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert len(rows) == 1 and notices == []


def test_open_request_is_caught_before_any_insert(client, db, monkeypatch):
    """The pre-check, not just the DB index, stops it: no insert attempt."""
    calls = []
    monkeypatch.setattr(app_module, '_sb_insert_solicitud', lambda body: calls.append(body) or 'created')
    db.add('solicitudes_registro', {'id': 's-1', 'email': EMAIL, 'estado': 'pendiente'})
    assert _post(client, _body()).status_code == 200
    assert calls == []


def test_closed_request_does_not_block_a_new_one(client, db, rows, notices):
    db.add('solicitudes_registro', {'id': 's-1', 'email': EMAIL, 'estado': 'rechazada'})
    assert _post(client, _body()).status_code == 200
    assert len(rows) == 2 and len(notices) == 1


def test_unique_violation_from_db_returns_generic_success(client, rows, notices, monkeypatch):
    """Two submits racing past the pre-check: the partial unique index
    rejects the second, and the applicant still sees the same reply."""
    monkeypatch.setattr(app_module, '_sb_insert_solicitud', lambda body: 'duplicate')
    resp = _post(client, _body())
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert notices == []


def test_second_submit_same_email_is_silent(client, rows, notices):
    assert _post(client, _body()).status_code == 200
    resp = _post(client, _body(nombre='Otra Persona Inventada'))
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert len(rows) == 1 and len(notices) == 1


# ── failures ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('table', ['usuarios', 'solicitudes_registro'])
def test_duplicate_lookup_failure_fails_closed(client, db, rows, notices, monkeypatch, table):
    real_get = db.get
    monkeypatch.setattr(app_module, '_sb_get_quiet',
                        lambda path: None if path.startswith(f'/rest/v1/{table}') else real_get(path))
    resp = _post(client, _body())
    assert resp.status_code == 503
    assert rows == [] and notices == []


def test_insert_failure_is_503_and_no_notice(client, notices, monkeypatch):
    monkeypatch.setattr(app_module, '_sb_insert_solicitud', lambda body: 'error')
    resp = _post(client, _body())
    assert resp.status_code == 503
    assert notices == []


def test_non_json_is_415(client, rows):
    resp = client.post(URL, data='nombre=x', content_type='application/x-www-form-urlencoded')
    assert resp.status_code == 415
    resp = client.post(URL, data=json.dumps(_body()), content_type='text/plain')
    assert resp.status_code == 415
    assert rows == []


@pytest.mark.parametrize('raw', ['not json', '[1, 2]', '"texto"', 'null'])
def test_malformed_or_non_object_json_is_400(client, rows, raw):
    resp = client.post(URL, data=raw, content_type='application/json')
    assert resp.status_code == 400
    assert rows == []


# ── superadmin notice ────────────────────────────────────────────────────

def test_superadmin_notified_once_with_nombre_only(client, notices):
    assert _post(client, _body()).status_code == 200
    assert notices == [('superadmin@clinia-ficticia.mx', NOMBRE)]


def test_no_notice_when_superadmin_email_unset(client, rows, monkeypatch, log_lines):
    sent = []
    monkeypatch.setattr(app_module, 'send_solicitud_notice', lambda *a: sent.append(a) or True)
    monkeypatch.setattr(Config, 'SUPERADMIN_EMAIL', '')
    assert _post(client, _body()).status_code == 200
    assert sent == [] and len(rows) == 1
    assert any('SUPERADMIN_EMAIL not set' in line for line in log_lines)


def test_notice_failure_does_not_fail_the_request(client, rows, monkeypatch):
    def boom(*a):
        raise RuntimeError('provider down')
    monkeypatch.setattr(app_module, 'send_solicitud_notice', boom)
    monkeypatch.setattr(Config, 'SUPERADMIN_EMAIL', 'superadmin@clinia-ficticia.mx')
    resp = _post(client, _body())
    assert resp.status_code == 200 and resp.get_json()['mensaje'] == GENERIC
    assert len(rows) == 1


def test_send_solicitud_notice_content(monkeypatch):
    captured = {}
    monkeypatch.setattr(email_service.resend.Emails, 'send', lambda params: captured.update(params) or {'id': 'x'})
    assert email_service.send_solicitud_notice('superadmin@clinia-ficticia.mx', '<b>Dra. Inventada</b>') is True
    assert captured['subject'] == 'Nueva solicitud de acceso — Clin.IA'
    assert captured['to'] == ['superadmin@clinia-ficticia.mx']
    assert '&lt;b&gt;Dra. Inventada&lt;/b&gt;' in captured['html']  # escaped
    assert 'https://app.clinianotes.com/superadmin' in captured['html']


def test_send_solicitud_notice_never_raises(monkeypatch):
    def boom(params):
        raise RuntimeError('Resend rejected superadmin@clinia-ficticia.mx')
    monkeypatch.setattr(email_service.resend.Emails, 'send', boom)
    assert email_service.send_solicitud_notice('superadmin@clinia-ficticia.mx', 'Dra. Inventada') is False


# ── rate limits ──────────────────────────────────────────────────────────

HP = {'sitio_web': 'x'}  # cheapest counted request (no DB, no validation)


def test_rate_limit_five_per_hour_per_ip(client):
    for _ in range(5):
        assert _post(client, HP, ip='198.51.100.1').status_code == 200
    resp = _post(client, HP, ip='198.51.100.1')
    assert resp.status_code == 429
    assert resp.get_json()['error_code'] == 'RATE_LIMITED'
    # a different IP is its own bucket
    assert _post(client, HP, ip='198.51.100.2').status_code == 200


def test_invalid_and_non_json_requests_count_too(client):
    for _ in range(3):
        client.post(URL, data='x', content_type='text/plain', headers={'X-Forwarded-For': '198.51.100.3'})
    for _ in range(2):
        _post(client, {'email': 'basura'}, ip='198.51.100.3')
    assert _post(client, HP, ip='198.51.100.3').status_code == 429


@pytest.fixture
def clock(monkeypatch):
    """Moves time.time() forward on demand — the memory:// limiter's
    fixed windows read it — so an hour can pass instantly."""
    now = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: now[0])

    def advance(seconds):
        now[0] += seconds
    return advance


def test_rate_limit_twenty_per_day_per_ip(client, clock):
    for hour in range(4):
        for _ in range(5):
            assert _post(client, HP, ip='198.51.100.4').status_code == 200
        clock(3601)
    # hourly window is fresh, but the day's 20 are used
    assert _post(client, HP, ip='198.51.100.4').status_code == 429
    clock(86400)
    assert _post(client, HP, ip='198.51.100.4').status_code == 200


def test_global_fifty_per_day_across_ips(client):
    for i in range(50):
        assert _post(client, HP, ip=f'192.0.2.{i // 5}').status_code == 200
    assert _post(client, HP, ip='192.0.2.200').status_code == 429


def test_limits_reset_between_tests_a(client):
    for _ in range(5):
        _post(client, HP, ip='198.51.100.9')


def test_limits_reset_between_tests_b(client):
    """Runs after _a: conftest's _reset_rate_limits cleared its counters."""
    assert _post(client, HP, ip='198.51.100.9').status_code == 200


# ── logging: outcome only, never the applicant ───────────────────────────

def _assert_no_applicant_data(lines):
    blob = '\n'.join(lines)
    for secret in (NOMBRE, 'Ximena', EMAIL, 'ximena.inventada', CEDULA, TELEFONO, '0000 4321',
                   DOMICILIO, 'Calle Ficticia'):
        assert secret not in blob, f'{secret!r} leaked into logs:\n{blob}'


def test_logs_carry_outcome_and_never_applicant_data(client, db, notices, log_lines, monkeypatch):
    _post(client, _body())                                  # created
    _post(client, _body())                                  # duplicate (open request)
    _post(client, _body(sitio_web='x'))                     # honeypot
    _post(client, _body(email='no-es-correo'))              # invalid
    db.add('usuarios', {'id': 'u-x', 'email': 'otro.inventado@clinica-ficticia.mx', 'activo': True})
    _post(client, _body(email='otro.inventado@clinica-ficticia.mx'))  # duplicate (user)
    _post(client, _body())                                  # rate-limited (6th from this IP)
    outcomes = [l.split('outcome=')[1] for l in log_lines if l.startswith('Solicitud: req=')]
    assert outcomes == ['created', 'duplicate', 'honeypot', 'invalid', 'duplicate', 'rate-limited']
    _assert_no_applicant_data(log_lines)


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__('https://fake.invalid', code, 'err', {}, io.BytesIO(body.encode()))


def test_real_db_helpers_never_log_the_email(monkeypatch, log_lines):
    """The real _sb_get_quiet / _sb_insert_solicitud (not conftest's
    fakes): their failure paths log table + status only, even though the
    query path and PostgREST's error body both carry the email."""
    get_quiet = _REAL_GET_QUIET
    insert = _REAL_INSERT

    pg_body = json.dumps({'code': '23505', 'message': 'duplicate key value violates unique constraint',
                          'details': f'Key (lower(email))=({EMAIL}) already exists.'})

    def raise_dup(req, timeout):
        raise _FakeHTTPError(409, pg_body)
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', raise_dup)
    assert insert({'email': EMAIL, 'nombre': NOMBRE}) == 'duplicate'

    other = json.dumps({'code': '23514', 'message': 'check violation', 'details': f'Failing row ({EMAIL}, {CEDULA})'})

    def raise_check(req, timeout):
        raise _FakeHTTPError(400, other)
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', raise_check)
    assert insert({'email': EMAIL}) == 'error'
    assert get_quiet(f'/rest/v1/usuarios?email=ilike.{EMAIL}&select=id') is None

    def raise_net(req, timeout):
        raise OSError(f'connection reset while sending {EMAIL}')
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', raise_net)
    assert insert({'email': EMAIL}) == 'error'
    assert get_quiet(f'/rest/v1/solicitudes_registro?email=ilike.{EMAIL}&select=id') is None

    assert log_lines  # the failures were logged...
    _assert_no_applicant_data(log_lines)  # ...without the email


def test_notice_failure_log_has_no_addresses(monkeypatch, log_lines):
    def boom(params):
        raise RuntimeError(f'bad recipient superadmin@clinia-ficticia.mx for {NOMBRE}')
    monkeypatch.setattr(email_service.resend.Emails, 'send', boom)
    email_service.send_solicitud_notice('superadmin@clinia-ficticia.mx', NOMBRE)
    assert log_lines
    _assert_no_applicant_data(log_lines)
    assert 'superadmin@clinia-ficticia.mx' not in '\n'.join(log_lines)


# Captured at import time, before conftest's autouse fixture patches the
# module attributes for each test.
_REAL_GET_QUIET = app_module._sb_get_quiet
_REAL_INSERT = app_module._sb_insert_solicitud
