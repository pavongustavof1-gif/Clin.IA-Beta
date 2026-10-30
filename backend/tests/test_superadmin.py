# backend/tests/test_superadmin.py
# Superadmin review of access requests (#53, Review C): the gate, the
# three lists, the clinic picker, approve (rules + resumable state
# machine), reject, resend, delete, and that no applicant data reaches a
# log line. Every person, clinic and address here is invented.

import io
import json
import logging
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import auth as auth_module
import email_service
from logger import logger as clinia_logger
from superadmin_utils import normalize_match, suggest_clinica
from tests import tokens
from tests.conftest import (
    CLINIC_A, CLINIC_B, DOCTOR_A1, ADMIN_A, ADMIN_B, AUXILIAR_A, INACTIVE_DOCTOR,
    _usuario_row,
)

# The superadmin is a real usuarios row (the admin of the test clinic);
# the configured id is lowercase, the stored id has upper case — the gate
# compares case-insensitively.
CLINIC_T = 'clinic-t-00000000'
SUPER = 'Super-Admin-0000-AAAA'
SUPER_CONFIG = frozenset({SUPER.lower()})

EMAIL = 'dra.inventada@consultorio-ficticio.mx'
CEDULA = '87654321'
TELEFONO = '5550001234'
DOMICILIO = 'Av. Imaginaria 742, Col. Inventada'
NOMBRE = 'Dra. Ana Inventada'
RAZON = 'Consultorio Ficticio Lomas'

SA_ROUTES = [
    ('GET', '/api/superadmin/solicitudes'),
    ('GET', '/api/superadmin/clinicas'),
    ('POST', '/api/superadmin/solicitudes/sol-1/aprobar'),
    ('POST', '/api/superadmin/solicitudes/sol-1/rechazar'),
    ('POST', '/api/superadmin/solicitudes/sol-1/reenviar'),
    ('POST', '/api/superadmin/solicitudes/sol-1/eliminar'),
]


def _iso(days_ago=0, hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _solicitud(**over):
    row = {
        'id': 'sol-1', 'nombre': NOMBRE, 'email': EMAIL, 'rol_solicitado': 'admin',
        'cedula': CEDULA, 'especialidad': 'Pediatría', 'universidad': 'Universidad Ficticia',
        'razon_social': RAZON, 'domicilio': DOMICILIO, 'telefono': TELEFONO,
        'recomendado_por': None, 'terminos_version': 'ToS-2026-10', 'terminos_aceptados_at': _iso(3),
        'estado': 'pendiente', 'created_at': _iso(3), 'revisado_at': None, 'revisado_por': None,
        'clinica_id': None, 'clinica_creada': None, 'usuario_id': None, 'rol_otorgado': None,
        'motivo_rechazo': None,
    }
    row.update(over)
    return row


class FakeAuth:
    """Supabase Auth admin, in memory. generate_link('invite') mirrors
    GoTrue: an existing UNCONFIRMED user gets a new link for the same id,
    a confirmed one is a 422. Deleting a user cascades to usuarios."""

    def __init__(self, db):
        self.db = db
        self.users = {}
        self.links = []          # (type, email) per generated link
        self.deleted = []

    def _by_email(self, email):
        return next((u for u in self.users.values() if u['email'] == email.lower()), None)

    def invite(self, email):
        user = self._by_email(email)
        if user and user.get('email_confirmed_at'):
            raise urllib.error.HTTPError('https://fake.invalid', 422, 'email_exists', {}, None)
        if not user:
            uid = f'auth-user-{len(self.users) + 1}'
            user = self.users[uid] = {'id': uid, 'email': email.lower(), 'email_confirmed_at': None}
        self.links.append(('invite', email))
        return user['id'], f"https://fake.invite/{user['id']}"

    def recovery(self, email):
        user = self._by_email(email)
        if not user:
            raise urllib.error.HTTPError('https://fake.invalid', 404, 'user_not_found', {}, None)
        self.links.append(('recovery', email))
        return f"https://fake.recovery/{user['id']}"

    def get(self, uid):
        user = self.users.get(uid)
        return ('ok', dict(user)) if user else ('missing', None)

    def delete(self, uid):
        if uid not in self.users:
            return 'missing'
        del self.users[uid]
        self.deleted.append(uid)
        self.db.delete(f'/rest/v1/usuarios?id=eq.{uid}')  # ON DELETE CASCADE
        return 'deleted'

    def accept(self, uid):
        self.users[uid]['email_confirmed_at'] = _iso()


@pytest.fixture
def fake_auth(db, monkeypatch):
    auth = FakeAuth(db)
    monkeypatch.setattr(app_module, '_sb_generate_invite_link', auth.invite)
    monkeypatch.setattr(app_module, '_sb_generate_recovery_link', auth.recovery)
    monkeypatch.setattr(app_module, '_sb_get_auth_user', auth.get)
    monkeypatch.setattr(app_module, '_sb_delete_auth_user', auth.delete)

    def insert_usuario(body):
        db.add('usuarios', body)
        return dict(body)
    monkeypatch.setattr(app_module, '_sb_insert_usuario', insert_usuario)

    def insert_clinica(nombre, direccion, telefono):
        return db.post('/rest/v1/clinicas', {
            'id': f'clinic-new-{len(db.tables["clinicas"]) + 1}',
            'nombre': nombre, 'direccion': direccion, 'telefono': telefono,
        })
    monkeypatch.setattr(app_module, '_sb_insert_clinica', insert_clinica)
    return auth


@pytest.fixture
def emails(monkeypatch):
    sent = {'invite': [], 'agregado': [], 'rechazo': []}

    def invite(to, nombre, clinica, link):
        sent['invite'].append({'to': to, 'nombre': nombre, 'clinica': clinica, 'link': link})
        return True

    def agregado(to, nombre, clinica, rol_label):
        sent['agregado'].append({'to': to, 'nombre': nombre, 'clinica': clinica, 'rol': rol_label})
        return True

    def rechazo(to, nombre, motivo, reply_to):
        sent['rechazo'].append({'to': to, 'nombre': nombre, 'motivo': motivo, 'reply_to': reply_to})
        return True
    monkeypatch.setattr(app_module, 'send_invite_email', invite)
    monkeypatch.setattr(app_module, 'send_usuario_agregado_email', agregado)
    monkeypatch.setattr(app_module, 'send_rejection_email', rechazo)
    return sent


@pytest.fixture(autouse=True)
def superadmin_setup(db, monkeypatch):
    monkeypatch.setattr(app_module.Config, 'SUPERADMIN_USER_IDS', SUPER_CONFIG)
    monkeypatch.setattr(app_module.Config, 'SUPERADMIN_EMAIL', 'superadmin@clinia-ficticia.mx')
    db.add('clinicas', {'id': CLINIC_T, 'nombre': 'CLIN.IA Pruebas (fixture)', 'direccion': '', 'telefono': ''})
    db.add('usuarios', {**_usuario_row(SUPER, CLINIC_T, 'admin', nombre='Superadmin Ficticio')})


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


def _sa(client, method, path, body=None, user=SUPER):
    headers = tokens.auth_headers(tokens.mint(user) if user else None)
    kwargs = {'headers': headers}
    if body is not None:
        kwargs['json'] = body
    return client.open(path, method=method, **kwargs)


def _aprobar(client, body, sid='sol-1'):
    return _sa(client, 'POST', f'/api/superadmin/solicitudes/{sid}/aprobar', body)


def _sol(db, sid='sol-1'):
    return next(r for r in db.tables['solicitudes_registro'] if r['id'] == sid)


def _usuarios_with_email(db, email=EMAIL):
    return [u for u in db.tables['usuarios'] if (u.get('email') or '').lower() == email]


def _new_clinics(db):
    return [c for c in db.tables['clinicas'] if c['id'].startswith('clinic-new-')]


# ── 1. gate ──────────────────────────────────────────────────────────────

_NON_SUPER_CALLERS = [
    pytest.param(None, id='no-token'),
    pytest.param(tokens.EXPIRED, id='expired-token'),
    pytest.param(DOCTOR_A1, id='medico'),
    pytest.param(ADMIN_A, id='admin'),
    pytest.param(AUXILIAR_A, id='auxiliar'),
    pytest.param(INACTIVE_DOCTOR, id='inactive'),
    pytest.param('nobody-registered', id='no-usuarios-row'),
]


@pytest.mark.parametrize('method,path', SA_ROUTES, ids=[f'{m} {p}' for m, p in SA_ROUTES])
@pytest.mark.parametrize('caller', _NON_SUPER_CALLERS)
def test_superadmin_routes_404_for_everyone_else(client, db, method, path, caller):
    db.add('solicitudes_registro', _solicitud())
    if caller == tokens.EXPIRED:
        resp = client.open(path, method=method, headers=tokens.auth_headers(tokens.EXPIRED), json={})
    else:
        resp = _sa(client, method, path, {}, user=caller)
    assert resp.status_code == 404
    assert resp.get_json() == {'error': 'No encontrado'}
    assert _sol(db)['estado'] == 'pendiente'


@pytest.mark.parametrize('method,path', SA_ROUTES, ids=[f'{m} {p}' for m, p in SA_ROUTES])
def test_superadmin_routes_404_when_no_superadmin_configured(client, db, monkeypatch, method, path):
    db.add('solicitudes_registro', _solicitud())
    monkeypatch.setattr(app_module.Config, 'SUPERADMIN_USER_IDS', frozenset())
    resp = _sa(client, method, path, {})
    assert resp.status_code == 404
    assert resp.get_json() == {'error': 'No encontrado'}


@pytest.mark.parametrize('method,path,expected', [
    ('GET', '/api/superadmin/solicitudes', 200),
    ('GET', '/api/superadmin/clinicas', 200),
    ('POST', '/api/superadmin/solicitudes/sol-1/aprobar', 400),     # empty body → rule error
    ('POST', '/api/superadmin/solicitudes/sol-1/rechazar', 200),
    ('POST', '/api/superadmin/solicitudes/sol-1/reenviar', 409),    # still pendiente
    ('POST', '/api/superadmin/solicitudes/sol-1/eliminar', 409),
])
def test_superadmin_reaches_every_route(client, db, fake_auth, emails, method, path, expected):
    db.add('solicitudes_registro', _solicitud())
    assert _sa(client, method, path, {}).status_code == expected


def test_gate_is_case_insensitive_both_ways(monkeypatch):
    monkeypatch.setattr(auth_module.Config, 'SUPERADMIN_USER_IDS', frozenset({'abc-def'}))
    assert auth_module.is_superadmin({'usuario_id': 'ABC-DEF'})
    assert auth_module.is_superadmin({'usuario_id': 'abc-def'})
    assert not auth_module.is_superadmin({'usuario_id': 'abc-de'})
    assert not auth_module.is_superadmin({'usuario_id': None})
    assert not auth_module.is_superadmin(None)


def test_config_parses_superadmin_ids(monkeypatch):
    import importlib
    import config
    monkeypatch.setenv('SUPERADMIN_USER_IDS', ' ABC-1 , abc-2,, ')
    reloaded = importlib.reload(config)
    try:
        assert reloaded.Config.SUPERADMIN_USER_IDS == frozenset({'abc-1', 'abc-2'})
    finally:
        monkeypatch.delenv('SUPERADMIN_USER_IDS')
        importlib.reload(config)


def test_session_check_superadmin_key_only_for_superadmin(client):
    body = _sa(client, 'GET', '/api/session-check').get_json()
    assert body['superadmin'] is True
    for other in (DOCTOR_A1, ADMIN_A, AUXILIAR_A, ADMIN_B):
        assert 'superadmin' not in _sa(client, 'GET', '/api/session-check', user=other).get_json()



# ── 1b. pending-requests banner (0.8.1) ──────────────────────────────────

def _seed_estados(db):
    for i, estado in enumerate(('pendiente', 'pendiente', 'en_proceso', 'aprobada', 'rechazada')):
        db.add('solicitudes_registro', _solicitud(
            id=f'sol-b{i}', estado=estado, email=f'persona{i}@consultorio-ficticio.mx'))


def test_session_check_counts_pendiente_and_en_proceso_only(client, db):
    _seed_estados(db)
    body = _sa(client, 'GET', '/api/session-check').get_json()
    assert body['superadmin'] is True
    assert body['solicitudes_pendientes'] == 3


def test_session_check_count_zero_when_nothing_open(client, db):
    db.add('solicitudes_registro', _solicitud(estado='aprobada'))
    assert _sa(client, 'GET', '/api/session-check').get_json()['solicitudes_pendientes'] == 0


@pytest.mark.parametrize('other', [DOCTOR_A1, ADMIN_A, AUXILIAR_A, ADMIN_B])
def test_session_check_count_absent_for_everyone_else(client, db, monkeypatch, other):
    _seed_estados(db)
    calls = []
    monkeypatch.setattr(app_module, '_sb_count_quiet', lambda path: calls.append(path) or 3)
    resp = _sa(client, 'GET', '/api/session-check', user=other)
    assert resp.status_code == 200
    assert 'solicitudes_pendientes' not in resp.get_json()
    assert calls == []  # not even counted for a non-superadmin


def test_session_check_survives_a_failed_count(client, db, monkeypatch):
    _seed_estados(db)
    monkeypatch.setattr(app_module, '_sb_count_quiet', lambda path: None)
    resp = _sa(client, 'GET', '/api/session-check')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['valid'] is True and body['superadmin'] is True
    assert 'solicitudes_pendientes' not in body


def test_session_check_count_query_shape(client, monkeypatch):
    seen = []
    monkeypatch.setattr(app_module, '_sb_count_quiet', lambda path: seen.append(path) or 0)
    _sa(client, 'GET', '/api/session-check')
    assert seen == ['/rest/v1/solicitudes_registro?estado=in.(pendiente,en_proceso)&select=id']


@pytest.mark.parametrize('page', ['/', '/admin'])
def test_pendientes_banner_markup(client, page):
    html = client.get(page).get_data(as_text=True)
    assert 'pendientes-banner.css' in html
    tag = '<div class="sa-pend-banner" id="saPendBanner" role="status" hidden>'
    assert tag in html
    assert '<a href="/superadmin" class="sa-pend-link">Revisar</a>' in html
    # Right under the nav, and above the Beta 2 notice where there is one.
    assert html.index('</nav>') < html.index(tag)
    if 'id="avisoBeta2"' in html:
        assert html.index(tag) < html.index('id="avisoBeta2"')


def test_pendientes_banner_not_on_superadmin_page(client):
    html = client.get('/superadmin').get_data(as_text=True)
    assert 'saPendBanner' not in html
    src = open(app_module.os.path.join(app_module.app.static_folder, 'superadmin.js')).read()
    assert 'showPendientesBanner' not in src


def test_pendientes_banner_js_uses_textcontent_and_is_wired():
    static = app_module.app.static_folder
    src = open(app_module.os.path.join(static, 'utils.js')).read()
    fn = src[src.index('function pendientesBannerText'):src.index('// Guards a URL')]
    assert 'text.textContent = pendientesBannerText(n)' in fn
    for sink in ('.innerHTML', '.outerHTML', 'insertAdjacentHTML', 'document.write'):
        assert sink not in fn
    for name in ('app.js', 'admin.js'):
        assert 'showPendientesBanner(data)' in open(app_module.os.path.join(static, name)).read()

def test_superadmin_page_shell(client):
    resp = client.get('/superadmin')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert 'superadmin.js' in html and 'superadmin.css' in html
    assert 'class="auth-pending"' in html


def test_superadmin_js_never_uses_innerhtml():
    src = open(app_module.os.path.join(app_module.app.static_folder, 'superadmin.js')).read()
    for sink in ('.innerHTML', '.outerHTML', 'insertAdjacentHTML', 'document.write'):
        assert sink not in src
    assert "if (!data.superadmin) { window.location.replace('/')" in src


@pytest.mark.parametrize('page', ['/', '/admin'])
def test_header_superadmin_link_starts_hidden(client, page):
    html = client.get(page).get_data(as_text=True)
    assert '<a href="/superadmin" id="superadminBtn" class="nav-link" style="display: none;">Superadmin</a>' in html


def test_header_link_is_revealed_only_on_superadmin_true():
    src = open(app_module.os.path.join(app_module.app.static_folder, 'utils.js')).read()
    assert "sessionData.superadmin !== true" in src
    for name in ('app.js', 'admin.js'):
        assert 'showSuperadminLink(data)' in open(app_module.os.path.join(app_module.app.static_folder, name)).read()


# ── 2. lists ─────────────────────────────────────────────────────────────

def test_pendiente_list_oldest_first_with_stuck_rows_flagged(client, db):
    db.add('solicitudes_registro', _solicitud(id='nuevo', created_at=_iso(1)))
    db.add('solicitudes_registro', _solicitud(id='viejo', email='otro@consultorio-ficticio.mx', created_at=_iso(9)))
    db.add('solicitudes_registro', _solicitud(id='atorado', email='tres@consultorio-ficticio.mx',
                                              estado='en_proceso', created_at=_iso(5)))
    db.add('solicitudes_registro', _solicitud(id='hecho', email='cuatro@consultorio-ficticio.mx', estado='aprobada'))
    body = _sa(client, 'GET', '/api/superadmin/solicitudes?estado=pendiente').get_json()
    rows = body['solicitudes']
    assert [r['id'] for r in rows] == ['viejo', 'atorado', 'nuevo']
    assert [r['atascada'] for r in rows] == [False, True, False]
    assert [r['dias'] for r in rows] == [9, 5, 1]
    # every field is returned
    assert rows[0]['cedula'] == CEDULA and rows[0]['domicilio'] == DOMICILIO
    assert rows[0]['rol_solicitado_label'] == 'Médico administrador'
    assert body['plantilla_rechazo'] == email_service.REJECTION_EMAIL_DRAFT


def test_default_list_is_pendiente(client, db):
    db.add('solicitudes_registro', _solicitud())
    assert _sa(client, 'GET', '/api/superadmin/solicitudes').get_json()['estado'] == 'pendiente'


def test_invalid_list_is_400(client):
    assert _sa(client, 'GET', '/api/superadmin/solicitudes?estado=en_proceso').status_code == 400


def test_aprobada_list_newest_first_with_invite_status(client, db, fake_auth):
    a_id, _ = fake_auth.invite('acepto@consultorio-ficticio.mx')
    fake_auth.accept(a_id)
    b_id, _ = fake_auth.invite('pendiente@consultorio-ficticio.mx')
    db.add('usuarios', _usuario_row(a_id, CLINIC_A, 'medico'))
    db.add('usuarios', _usuario_row(b_id, 'clinic-new-x', 'admin'))
    db.add('clinicas', {'id': 'clinic-new-x', 'nombre': RAZON, 'direccion': DOMICILIO, 'telefono': TELEFONO})
    db.add('solicitudes_registro', _solicitud(id='vieja', email='acepto@consultorio-ficticio.mx', estado='aprobada',
                                              usuario_id=a_id, clinica_id=CLINIC_A, clinica_creada=False,
                                              rol_otorgado='medico', revisado_at=_iso(10)))
    db.add('solicitudes_registro', _solicitud(id='reciente', email='pendiente@consultorio-ficticio.mx', estado='aprobada',
                                              usuario_id=b_id, clinica_id='clinic-new-x', clinica_creada=True,
                                              rol_otorgado='admin', revisado_at=_iso(2)))
    rows = _sa(client, 'GET', '/api/superadmin/solicitudes?estado=aprobada').get_json()['solicitudes']
    assert [r['id'] for r in rows] == ['reciente', 'vieja']
    assert [r['invitacion_aceptada'] for r in rows] == [False, True]
    assert [r['dias_desde_aprobacion'] for r in rows] == [2, 10]
    assert [r['clinica_se_elimina'] for r in rows] == [True, False]
    assert rows[0]['clinica_nombre'] == RAZON
    assert rows[1]['rol_otorgado_label'] == 'Médico'


def test_aprobada_invite_status_unknown_when_auth_lookup_fails(client, db, monkeypatch):
    db.add('solicitudes_registro', _solicitud(estado='aprobada', usuario_id='u-1', revisado_at=_iso(1)))
    monkeypatch.setattr(app_module, '_sb_get_auth_user', lambda uid: ('error', None))
    rows = _sa(client, 'GET', '/api/superadmin/solicitudes?estado=aprobada').get_json()['solicitudes']
    assert rows[0]['invitacion_aceptada'] is None


def test_rechazada_list_newest_first_with_motivo(client, db):
    db.add('solicitudes_registro', _solicitud(id='r1', estado='rechazada', revisado_at=_iso(20), motivo_rechazo='Primero'))
    db.add('solicitudes_registro', _solicitud(id='r2', email='x@consultorio-ficticio.mx', estado='rechazada',
                                              revisado_at=_iso(1), motivo_rechazo='Línea 1\nLínea 2'))
    rows = _sa(client, 'GET', '/api/superadmin/solicitudes?estado=rechazada').get_json()['solicitudes']
    assert [r['id'] for r in rows] == ['r2', 'r1']
    assert rows[0]['motivo_rechazo'] == 'Línea 1\nLínea 2'


def test_list_load_purges_only_old_rechazadas(client, db):
    db.add('solicitudes_registro', _solicitud(id='vieja-r', estado='rechazada', revisado_at=_iso(31)))
    db.add('solicitudes_registro', _solicitud(id='nueva-r', estado='rechazada', revisado_at=_iso(29)))
    db.add('solicitudes_registro', _solicitud(id='vieja-p', estado='pendiente', created_at=_iso(60), revisado_at=_iso(60)))
    db.add('solicitudes_registro', _solicitud(id='vieja-a', estado='aprobada', revisado_at=_iso(60)))
    db.add('solicitudes_registro', _solicitud(id='vieja-e', estado='en_proceso', revisado_at=_iso(60)))
    _sa(client, 'GET', '/api/superadmin/solicitudes?estado=pendiente')
    assert sorted(r['id'] for r in db.tables['solicitudes_registro']) == ['nueva-r', 'vieja-a', 'vieja-e', 'vieja-p']


def test_purge_runs_at_startup():
    import inspect
    src = inspect.getsource(app_module)
    tail = src[src.index('_reap_stuck_jobs_on_startup()\n# Old rechazadas'):]
    assert '_purge_old_rechazadas()' in tail.split("if __name__ == '__main__':")[0]


def test_list_db_failure_is_503(client, monkeypatch):
    monkeypatch.setattr(app_module, '_sb_get_quiet', lambda path: None)
    assert _sa(client, 'GET', '/api/superadmin/solicitudes').status_code == 503


# ── 3. clinic picker ─────────────────────────────────────────────────────

def test_normalize_match():
    assert normalize_match('Clínica  Sán-Ángel, S.C.') == 'clinica sanangel sc'
    assert normalize_match('CONSULTORIO​ Ficticio') == 'consultorio ficticio'
    assert normalize_match(None) == ''


@pytest.mark.parametrize('q,expected', [
    ('Clinica San Angel', 'c1'),            # accents
    ('clínica san ángel s.c.', 'c1'),       # punctuation, either way
    ('San Ángel', 'c1'),                    # q inside the name
    ('Consultorio Médico Lomas de Ficticio Norte', 'c2'),  # name inside q
    ('Hospital Inexistente', None),
    ('', None),
])
def test_suggest_clinica(q, expected):
    clinicas = [
        {'id': 'c1', 'nombre': 'Clínica San Ángel'},
        {'id': 'c2', 'nombre': 'Lomas de Ficticio'},
        {'id': 'c3', 'nombre': 'Clínica San Ángel Sur Ampliación'},
    ]
    assert suggest_clinica(q, clinicas) == expected


def test_suggest_prefers_exact_then_closest():
    clinicas = [{'id': 'largo', 'nombre': 'Clínica Norte Ficticia'}, {'id': 'exacto', 'nombre': 'Clinica Norte'}]
    assert suggest_clinica('clínica norte', clinicas) == 'exacto'


def test_clinicas_route_lists_all_with_suggestion(client, db):
    resp = _sa(client, 'GET', '/api/superadmin/clinicas?q=clinica a (FIXTURE)')
    body = resp.get_json()
    assert {c['id'] for c in body['clinicas']} == {CLINIC_A, CLINIC_B, CLINIC_T}
    assert set(body['clinicas'][0]) == {'id', 'nombre', 'direccion'}
    assert body['sugerida'] == CLINIC_A
    assert _sa(client, 'GET', '/api/superadmin/clinicas?q=Nada Parecido').get_json()['sugerida'] is None


# ── 4. approve: rules ────────────────────────────────────────────────────

def test_new_clinic_admin_creates_one_clinic_and_one_user(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud())
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin', 'confirma_admin': False})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['clinica_creada'] is True and body['correo_invitacion_enviado'] is True
    clinics = _new_clinics(db)
    assert len(clinics) == 1
    assert clinics[0] == {'id': body['clinica_id'], 'nombre': RAZON, 'direccion': DOMICILIO, 'telefono': TELEFONO}
    users = _usuarios_with_email(db)
    assert len(users) == 1 and len(fake_auth.users) == 1
    u = users[0]
    assert (u['rol'], u['clinica_id'], u['cedula'], u['especialidad'], u['universidad'], u['activo']) == \
        ('admin', body['clinica_id'], CEDULA, 'Pediatría', 'Universidad Ficticia', True)
    s = _sol(db)
    assert s['estado'] == 'aprobada' and s['usuario_id'] == u['id'] and s['clinica_creada'] is True
    assert s['rol_otorgado'] == 'admin' and s['revisado_por'] == SUPER and s['revisado_at']
    assert emails['invite'] == [{'to': EMAIL, 'nombre': NOMBRE, 'clinica': RAZON, 'link': f"https://fake.invite/{u['id']}"}]
    assert emails['agregado'] == []  # nobody else in a new clinic


def test_new_clinic_auxiliar_has_no_doctor_fields(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='auxiliar', cedula=None, especialidad=None, universidad=None))
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'auxiliar'})
    assert resp.status_code == 200
    u = _usuarios_with_email(db)[0]
    assert u['rol'] == 'auxiliar' and 'cedula' not in u and 'especialidad' not in u


def test_new_clinic_requires_an_admin_role(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'medico'})
    assert resp.status_code == 400
    assert 'Una clínica nueva necesita un administrador' in resp.get_json()['error']
    assert _sol(db)['estado'] == 'pendiente' and not _new_clinics(db) and not fake_auth.users


@pytest.mark.parametrize('rol,solicitado', [('admin', 'admin'), ('auxiliar', 'auxiliar')])
def test_existing_clinic_admin_roles_need_confirmation(client, db, fake_auth, emails, rol, solicitado):
    db.add('solicitudes_registro', _solicitud(rol_solicitado=solicitado))
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': rol, 'confirma_admin': False})
    assert resp.status_code == 400
    assert 'administrador actual' in resp.get_json()['error']
    assert _sol(db)['estado'] == 'pendiente' and not fake_auth.users
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': rol, 'confirma_admin': True})
    assert resp.status_code == 200


@pytest.mark.parametrize('confirma', [True, 'true', 1, None])
def test_confirma_admin_must_be_literal_true(client, db, fake_auth, emails, confirma):
    db.add('solicitudes_registro', _solicitud())
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'admin', 'confirma_admin': confirma})
    assert resp.status_code == (200 if confirma is True else 400)


def test_existing_clinic_medico_creates_no_clinic_and_tells_each_other_admin_once(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    clinics_before = len(db.tables['clinicas'])
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['clinica_creada'] is False and body['avisos_admins'] == {'enviados': 2, 'fallidos': 0}
    assert len(db.tables['clinicas']) == clinics_before
    assert _usuarios_with_email(db)[0]['clinica_id'] == CLINIC_A
    # ADMIN_A and AUXILIAR_A (active admins of clinic A) — not doctors,
    # not the inactive user, not clinic B, not the new user.
    assert sorted(e['to'] for e in emails['agregado']) == sorted([f'{ADMIN_A}@example.test', f'{AUXILIAR_A}@example.test'])
    assert {(e['nombre'], e['clinica'], e['rol']) for e in emails['agregado']} == {(NOMBRE, 'Clínica A (fixture)', 'Médico')}
    assert emails['invite'][0]['clinica'] == 'Clínica A (fixture)'


def test_new_admin_in_existing_clinic_is_not_told_about_themself(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud())
    _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'admin', 'confirma_admin': True})
    assert EMAIL not in {e['to'] for e in emails['agregado']}
    assert len(emails['agregado']) == 2


def test_inactive_clinic_admins_are_not_told(client, db, fake_auth, emails):
    for u in db.tables['usuarios']:
        if u['id'] == AUXILIAR_A:
            u['activo'] = False
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'})
    assert [e['to'] for e in emails['agregado']] == [f'{ADMIN_A}@example.test']


@pytest.mark.parametrize('rol', ['admin', 'medico'])
def test_doctor_roles_need_cedula_and_especialidad(client, db, fake_auth, emails, rol):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='auxiliar', cedula=None, especialidad=None))
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': rol, 'confirma_admin': True})
    assert resp.status_code == 400
    assert 'cédula y especialidad' in resp.get_json()['error']


@pytest.mark.parametrize('solicitado', ['medico', 'admin'])
def test_auxiliar_only_if_requested(client, db, fake_auth, emails, solicitado):
    db.add('solicitudes_registro', _solicitud(rol_solicitado=solicitado))
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'auxiliar'})
    assert resp.status_code == 400
    assert 'Solo se otorga Auxiliar' in resp.get_json()['error']


def test_admin_may_be_granted_to_a_medico_request(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 200


@pytest.mark.parametrize('body', [
    {}, {'rol_otorgado': 'superadmin'}, {'rol_otorgado': 'ADMIN'},
    {'rol_otorgado': 'admin', 'clinica_id': 42}, {'rol_otorgado': 'admin', 'clinica_id': '  '},
])
def test_approve_bad_bodies_are_400(client, db, fake_auth, body):
    db.add('solicitudes_registro', _solicitud())
    assert _aprobar(client, body).status_code == 400
    assert _sol(db)['estado'] == 'pendiente'


def test_approve_non_object_json_is_400(client, db):
    db.add('solicitudes_registro', _solicitud())
    resp = client.post('/api/superadmin/solicitudes/sol-1/aprobar', headers=tokens.auth_headers(tokens.mint(SUPER)),
                       data='[1]', content_type='application/json')
    assert resp.status_code == 400


def test_approve_unknown_solicitud_is_404(client, fake_auth):
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}, sid='no-existe').status_code == 404


def test_approve_unknown_clinic_is_400(client, db, fake_auth):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    resp = _aprobar(client, {'clinica_id': 'clinic-que-no-existe', 'rol_otorgado': 'medico'})
    assert resp.status_code == 400 and _sol(db)['estado'] == 'pendiente'


def test_email_taken_at_approval_time_is_409_and_stays_pendiente(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud())
    db.add('usuarios', {**_usuario_row('ya-existe', CLINIC_B, 'medico'), 'email': EMAIL.upper()})
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    assert resp.status_code == 409
    assert 'Ya existe un usuario con este correo' in resp.get_json()['error']
    s = _sol(db)
    assert s['estado'] == 'pendiente' and s['rol_otorgado'] is None
    assert not _new_clinics(db) and not fake_auth.users and not emails['invite']


@pytest.mark.parametrize('estado', ['aprobada', 'rechazada'])
def test_reviewed_requests_cannot_be_approved(client, db, fake_auth, estado):
    db.add('solicitudes_registro', _solicitud(estado=estado))
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 409


def test_second_approve_is_409_and_creates_nothing(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud())
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 200
    snapshot = (len(db.tables['clinicas']), len(db.tables['usuarios']), len(fake_auth.users), len(emails['invite']))
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    assert resp.status_code == 409
    assert (len(db.tables['clinicas']), len(db.tables['usuarios']), len(fake_auth.users), len(emails['invite'])) == snapshot


def test_lost_claim_race_is_409(client, db, fake_auth, monkeypatch):
    """Two tabs both saw pendiente; the conditional PATCH lets one win."""
    db.add('solicitudes_registro', _solicitud())
    real = app_module._sb_patch_returning_quiet

    def other_tab_first(path, body):
        _sol(db)['estado'] = 'en_proceso'
        return real(path, body)
    monkeypatch.setattr(app_module, '_sb_patch_returning_quiet', other_tab_first)
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    assert resp.status_code == 409 and not fake_auth.users and not _new_clinics(db)


def test_invite_email_failure_keeps_the_approval(client, db, fake_auth, monkeypatch):
    db.add('solicitudes_registro', _solicitud())
    monkeypatch.setattr(app_module, 'send_invite_email', lambda *a: False)
    resp = _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['correo_invitacion_enviado'] is False
    assert body['warning'] == 'Aprobada, pero el correo de invitación no se envió — usa Reenviar.'
    assert _sol(db)['estado'] == 'aprobada'


def test_admin_notice_failures_are_reported_not_fatal(client, db, fake_auth, emails, monkeypatch):
    monkeypatch.setattr(app_module, 'send_usuario_agregado_email', lambda *a: False)
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    resp = _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'})
    assert resp.status_code == 200
    assert resp.get_json()['avisos_admins'] == {'enviados': 0, 'fallidos': 2}


# ── 4b. approve: failure after each step, then retry ─────────────────────

class _FailOnce:
    """Wrap a module function so the call matching `when` fails once."""

    def __init__(self, monkeypatch, name, when, result=None, raise_exc=None):
        self.real = getattr(app_module, name)
        self.when, self.result, self.raise_exc = when, result, raise_exc
        self.fired = False
        monkeypatch.setattr(app_module, name, self)

    def __call__(self, *args, **kwargs):
        if not self.fired and self.when(*args, **kwargs):
            self.fired = True
            if self.raise_exc:
                raise self.raise_exc
            return self.result
        return self.real(*args, **kwargs)


def _patch_body_has(key, value=None):
    def when(path, body):
        return 'solicitudes_registro' in path and key in body and (value is None or body[key] == value)
    return when


# (label, function, matcher, result, exception)
_NEW_CLINIC_FAILURES = [
    ('claim', '_sb_patch_returning_quiet', _patch_body_has('estado', 'en_proceso'), None, None),
    ('clinic-insert', '_sb_insert_clinica', lambda *a: True, None, None),
    ('clinic-record', '_sb_patch_returning_quiet', _patch_body_has('clinica_creada'), [], None),
    ('auth-user', '_sb_generate_invite_link', lambda email: True, None, OSError('fake outage')),
    ('usuario-id-record', '_sb_patch_returning_quiet', _patch_body_has('usuario_id'), None, None),
    ('usuarios-insert', '_sb_insert_usuario', lambda body: True, None, None),
    ('mark-aprobada', '_sb_patch_returning_quiet', _patch_body_has('estado', 'aprobada'), None, None),
]


@pytest.mark.parametrize('label,name,when,result,exc', _NEW_CLINIC_FAILURES, ids=[f[0] for f in _NEW_CLINIC_FAILURES])
def test_new_clinic_approval_fails_then_resumes_without_duplicates(
        client, db, fake_auth, emails, monkeypatch, label, name, when, result, exc):
    db.add('solicitudes_registro', _solicitud())
    failer = _FailOnce(monkeypatch, name, when, result, exc)
    body = {'clinica_id': None, 'rol_otorgado': 'admin', 'confirma_admin': False}

    first = _aprobar(client, body)
    assert failer.fired
    assert first.status_code == 503, first.get_json()
    assert 'Reintentar aprobación' in first.get_json()['error']
    assert _sol(db)['estado'] in ('pendiente', 'en_proceso')
    assert len(_new_clinics(db)) <= 1   # a clinic is never left behind twice

    retry = _aprobar(client, body)
    assert retry.status_code == 200, retry.get_json()
    assert len(_new_clinics(db)) == 1
    assert len(fake_auth.users) == 1
    assert len(_usuarios_with_email(db)) == 1
    s = _sol(db)
    assert s['estado'] == 'aprobada'
    assert s['usuario_id'] == next(iter(fake_auth.users)) == _usuarios_with_email(db)[0]['id']
    assert s['clinica_id'] == _new_clinics(db)[0]['id'] == _usuarios_with_email(db)[0]['clinica_id']
    assert len(emails['invite']) == 1  # only the successful run emails


def test_clinic_recorded_failure_removes_the_unrecorded_clinic(client, db, fake_auth, emails, monkeypatch):
    db.add('solicitudes_registro', _solicitud())
    _FailOnce(monkeypatch, '_sb_patch_returning_quiet', _patch_body_has('clinica_creada'), [])
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 503
    assert _new_clinics(db) == []


def test_resume_after_user_created_sends_a_recovery_link(client, db, fake_auth, emails, monkeypatch):
    """The first run's invite action_link was lost with the failed
    request; the retry must mint a fresh one of the verified type."""
    db.add('solicitudes_registro', _solicitud())
    _FailOnce(monkeypatch, '_sb_patch_returning_quiet', _patch_body_has('estado', 'aprobada'))
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 503
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 200
    assert [t for t, _ in fake_auth.links] == ['invite', 'recovery']
    assert emails['invite'][0]['link'].startswith('https://fake.recovery/')


def test_resume_inserts_only_the_missing_usuarios_row(client, db, fake_auth, emails, monkeypatch):
    db.add('solicitudes_registro', _solicitud())
    _FailOnce(monkeypatch, '_sb_insert_usuario', lambda body: True)
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 503
    stored = _sol(db)['usuario_id']
    assert stored and not _usuarios_with_email(db)
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 200
    assert [t for t, _ in fake_auth.links] == ['invite', 'recovery']  # no second invite → no second auth user
    assert _usuarios_with_email(db)[0]['id'] == stored


def test_existing_clinic_approval_fails_then_resumes(client, db, fake_auth, emails, monkeypatch):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    _FailOnce(monkeypatch, '_sb_insert_usuario', lambda body: True)
    body = {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'}
    assert _aprobar(client, body).status_code == 503
    assert _aprobar(client, body).status_code == 200
    assert not _new_clinics(db) and len(fake_auth.users) == 1 and len(_usuarios_with_email(db)) == 1
    assert len(emails['agregado']) == 2


@pytest.mark.parametrize('first,second', [
    ({'clinica_id': None, 'rol_otorgado': 'admin'}, {'clinica_id': None, 'rol_otorgado': 'auxiliar'}),
    ({'clinica_id': None, 'rol_otorgado': 'admin'}, {'clinica_id': CLINIC_A, 'rol_otorgado': 'admin', 'confirma_admin': True}),
    ({'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'}, {'clinica_id': CLINIC_B, 'rol_otorgado': 'medico'}),
    ({'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'}, {'clinica_id': None, 'rol_otorgado': 'admin'}),
])
def test_resume_with_other_choices_is_409(client, db, fake_auth, emails, monkeypatch, first, second):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='auxiliar', cedula=CEDULA, especialidad='Pediatría'))
    _FailOnce(monkeypatch, '_sb_insert_usuario', lambda body: True)
    assert _aprobar(client, first).status_code == 503
    resp = _aprobar(client, second)
    assert resp.status_code == 409
    assert 'mismos datos' in resp.get_json()['error']


# ── 5. reject ────────────────────────────────────────────────────────────

def _rechazar(client, body, sid='sol-1'):
    return _sa(client, 'POST', f'/api/superadmin/solicitudes/{sid}/rechazar', body)


def test_reject_with_email(client, db, emails):
    db.add('solicitudes_registro', _solicitud())
    resp = _rechazar(client, {'motivo': '  No encontramos la cédula.\r\nGracias.  ', 'enviar_correo': True})
    assert resp.status_code == 200 and resp.get_json()['correo_enviado'] is True
    s = _sol(db)
    assert s['estado'] == 'rechazada' and s['motivo_rechazo'] == 'No encontramos la cédula.\nGracias.'
    assert s['revisado_por'] == SUPER and s['revisado_at']
    assert emails['rechazo'] == [{'to': EMAIL, 'nombre': NOMBRE, 'motivo': 'No encontramos la cédula.\nGracias.',
                                  'reply_to': 'superadmin@clinia-ficticia.mx'}]


@pytest.mark.parametrize('enviar', [False, None, 'true', 1])
def test_reject_without_email_unless_literal_true(client, db, emails, enviar):
    db.add('solicitudes_registro', _solicitud())
    resp = _rechazar(client, {'motivo': None, 'enviar_correo': enviar})
    assert resp.status_code == 200 and resp.get_json()['correo_enviado'] is None
    assert emails['rechazo'] == [] and _sol(db)['motivo_rechazo'] is None


@pytest.mark.parametrize('estado', ['en_proceso', 'aprobada', 'rechazada'])
def test_reject_only_from_pendiente(client, db, emails, estado):
    db.add('solicitudes_registro', _solicitud(estado=estado))
    assert _rechazar(client, {'enviar_correo': True}).status_code == 409
    assert _sol(db)['estado'] == estado and not emails['rechazo']


def test_reject_race_is_409(client, db, emails, monkeypatch):
    """The read saw pendiente, but another tab approved it first: the
    conditional PATCH (estado=eq.pendiente) matches nothing."""
    db.add('solicitudes_registro', _solicitud())
    real = app_module._sb_patch_returning_quiet

    def approved_meanwhile(path, body):
        _sol(db)['estado'] = 'en_proceso'
        return real(path, body)
    monkeypatch.setattr(app_module, '_sb_patch_returning_quiet', approved_meanwhile)
    assert _rechazar(client, {'enviar_correo': True}).status_code == 409
    assert _sol(db)['estado'] == 'en_proceso' and not emails['rechazo']


@pytest.mark.parametrize('motivo,ok', [('x' * 1000, True), ('x' * 1001, False), (12, False), (['a'], False)])
def test_reject_motivo_limits(client, db, emails, motivo, ok):
    db.add('solicitudes_registro', _solicitud())
    assert _rechazar(client, {'motivo': motivo}).status_code == (200 if ok else 400)


def test_reject_email_failure_is_reported(client, db, monkeypatch):
    db.add('solicitudes_registro', _solicitud())
    monkeypatch.setattr(app_module, 'send_rejection_email', lambda *a: False)
    resp = _rechazar(client, {'enviar_correo': True})
    assert resp.status_code == 200 and resp.get_json()['correo_enviado'] is False
    assert _sol(db)['estado'] == 'rechazada'


def test_rejection_text_matches_the_draft():
    with_motivo = email_service.render_rejection_text('Ana', 'Faltan datos.')
    assert with_motivo == (
        'Hola Ana:\n\n'
        'Gracias por tu interés en Clin.IA. Revisamos tu solicitud de acceso y por el momento no podemos aprobarla.\n\n'
        'Motivo: Faltan datos.\n\n'
        'Si crees que hay un error o quieres darnos más información, responde a este correo y con gusto lo revisamos.\n\n'
        'Saludos,\nEquipo Clin.IA'
    )
    without = email_service.render_rejection_text('Ana', None)
    assert 'Motivo' not in without and 'aprobarla.\n\nSi crees' in without
    assert email_service.REJECTION_EMAIL_DRAFT['asunto'] == 'Tu solicitud de acceso a Clin.IA'


def test_rejection_html_escapes_motivo_and_keeps_line_breaks():
    out = email_service.render_rejection_html('Ana <b>', '<script>alert(1)</script>\nlínea {nombre} dos')
    assert '<script>' not in out and '&lt;script&gt;alert(1)&lt;/script&gt;<br>línea {nombre} dos' in out
    assert 'Hola Ana &lt;b&gt;:' in out


def test_send_rejection_email_params(monkeypatch):
    sent = []
    monkeypatch.setattr(email_service.resend.Emails, 'send', lambda p: sent.append(p) or {'id': 'x'})
    assert email_service.send_rejection_email(EMAIL, NOMBRE, 'Motivo de prueba', 'superadmin@clinia-ficticia.mx')
    p = sent[0]
    assert p['to'] == [EMAIL] and p['reply_to'] == 'superadmin@clinia-ficticia.mx'
    assert p['subject'] == 'Tu solicitud de acceso a Clin.IA'
    assert p['text'] == email_service.render_rejection_text(NOMBRE, 'Motivo de prueba')


def test_superadmin_js_preview_uses_the_same_substitution():
    src = open(app_module.os.path.join(app_module.app.static_folder, 'superadmin.js')).read()
    assert "p.bloque_motivo.split('{motivo}').join(motivo)" in src
    assert "p.cuerpo.split('{nombre}').join(nombre).split('{bloque_motivo}').join(bloque)" in src


# ── 6. resend ────────────────────────────────────────────────────────────

def _approved(client, db, fake_auth, **sol):
    db.add('solicitudes_registro', _solicitud(**sol))
    assert _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}).status_code == 200
    return _sol(db)


def test_resend_uses_a_recovery_link(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    resp = _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {})
    assert resp.status_code == 200
    assert fake_auth.links[-1] == ('recovery', EMAIL)
    assert emails['invite'][-1] == {'to': EMAIL, 'nombre': NOMBRE, 'clinica': RAZON,
                                    'link': f"https://fake.recovery/{s['usuario_id']}"}


def test_resend_refused_once_accepted(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    fake_auth.accept(s['usuario_id'])
    resp = _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {})
    assert resp.status_code == 409 and len(emails['invite']) == 1


@pytest.mark.parametrize('estado', ['pendiente', 'en_proceso', 'rechazada'])
def test_resend_only_for_aprobada(client, db, fake_auth, emails, estado):
    db.add('solicitudes_registro', _solicitud(estado=estado, usuario_id='u-x'))
    assert _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {}).status_code == 409


def test_resend_email_failure_is_503(client, db, fake_auth, monkeypatch):
    _approved(client, db, fake_auth)
    monkeypatch.setattr(app_module, 'send_invite_email', lambda *a: False)
    assert _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {}).status_code == 503


def test_resend_auth_lookup_failure_is_503(client, db, fake_auth, monkeypatch):
    _approved(client, db, fake_auth)
    monkeypatch.setattr(app_module, '_sb_get_auth_user', lambda uid: ('error', None))
    assert _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {}).status_code == 503


# ── 7. delete ────────────────────────────────────────────────────────────

def _eliminar(client, sid='sol-1'):
    return _sa(client, 'POST', f'/api/superadmin/solicitudes/{sid}/eliminar', {})


def test_delete_new_clinic_approval_removes_everything(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    resp = _eliminar(client)
    assert resp.status_code == 200 and resp.get_json()['clinica_eliminada'] is True
    assert fake_auth.deleted == [s['usuario_id']]
    assert not _usuarios_with_email(db) and not _new_clinics(db)
    assert not [r for r in db.tables['solicitudes_registro'] if r['id'] == 'sol-1']


def test_delete_never_removes_a_pre_existing_clinic(client, db, fake_auth, emails):
    db.add('solicitudes_registro', _solicitud(rol_solicitado='medico'))
    assert _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'}).status_code == 200
    # Even an (impossible) clinica_creada flag on a clinic that was there first
    # can't take it: the clinic still has other users.
    _sol(db)['clinica_creada'] = True
    resp = _eliminar(client)
    assert resp.status_code == 200 and resp.get_json()['clinica_eliminada'] is False
    assert any(c['id'] == CLINIC_A for c in db.tables['clinicas'])


def test_delete_never_removes_an_empty_pre_existing_clinic(client, db, fake_auth, emails):
    """The clinic existed before the approval and is empty afterwards:
    only clinica_creada may let a delete take a clinic."""
    db.add('clinicas', {'id': 'clinic-vacia', 'nombre': 'Consultorio Vacío (fixture)', 'direccion': '', 'telefono': ''})
    db.add('solicitudes_registro', _solicitud())
    assert _aprobar(client, {'clinica_id': 'clinic-vacia', 'rol_otorgado': 'admin', 'confirma_admin': True}).status_code == 200
    resp = _eliminar(client)
    assert resp.status_code == 200 and resp.get_json()['clinica_eliminada'] is False
    assert any(c['id'] == 'clinic-vacia' for c in db.tables['clinicas'])


def test_delete_keeps_created_clinic_once_someone_else_joined(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    db.add('usuarios', _usuario_row('colega-nuevo', s['clinica_id'], 'medico'))
    resp = _eliminar(client)
    assert resp.status_code == 200 and resp.get_json()['clinica_eliminada'] is False
    assert len(_new_clinics(db)) == 1


def test_delete_keeps_created_clinic_with_sesiones(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    db.add('sesiones', {'session_id': 'S-OTRA', 'usuario_id': 'alguien-mas', 'clinica_id': s['clinica_id']})
    assert _eliminar(client).get_json()['clinica_eliminada'] is False


@pytest.mark.parametrize('table,row', [
    ('sesiones', {'session_id': 'S-NUEVA', 'clinica_id': 'x'}),
    ('trabajos', {'job_id': 'J-NUEVO', 'clinica_id': 'x'}),
    ('lecturas_sesion', {'session_id': 'S-LEIDA'}),
])
def test_delete_refused_while_the_user_has_activity(client, db, fake_auth, emails, table, row):
    s = _approved(client, db, fake_auth)
    db.add(table, {**row, 'usuario_id': s['usuario_id']})
    resp = _eliminar(client)
    assert resp.status_code == 409
    assert not fake_auth.deleted and _usuarios_with_email(db) and len(_new_clinics(db)) == 1
    assert _sol(db)['estado'] == 'aprobada'


def test_delete_refused_once_accepted(client, db, fake_auth, emails):
    s = _approved(client, db, fake_auth)
    fake_auth.accept(s['usuario_id'])
    assert _eliminar(client).status_code == 409 and not fake_auth.deleted


@pytest.mark.parametrize('estado', ['pendiente', 'en_proceso', 'rechazada'])
def test_delete_only_for_aprobada(client, db, fake_auth, estado):
    db.add('solicitudes_registro', _solicitud(estado=estado, usuario_id='u-x'))
    assert _eliminar(client).status_code == 409


def test_delete_is_resumable(client, db, fake_auth, emails, monkeypatch):
    s = _approved(client, db, fake_auth)
    _FailOnce(monkeypatch, '_sb_delete_quiet', lambda path: 'solicitudes_registro' in path, False)
    assert _eliminar(client).status_code == 503
    assert fake_auth.deleted == [s['usuario_id']] and not _new_clinics(db)
    assert _sol(db)['estado'] == 'aprobada'
    retry = _eliminar(client)
    assert retry.status_code == 200
    assert fake_auth.deleted == [s['usuario_id']]  # not deleted twice
    assert not [r for r in db.tables['solicitudes_registro'] if r['id'] == 'sol-1']


def test_delete_resumes_after_auth_user_gone_but_clinic_delete_failed(client, db, fake_auth, emails, monkeypatch):
    _approved(client, db, fake_auth)
    _FailOnce(monkeypatch, '_sb_delete_quiet', lambda path: '/clinicas' in path, False)
    assert _eliminar(client).status_code == 503
    assert len(_new_clinics(db)) == 1
    assert _eliminar(client).get_json()['clinica_eliminada'] is True
    assert not _new_clinics(db)


def test_delete_stops_if_usuarios_row_survives_the_auth_delete(client, db, fake_auth, emails, monkeypatch):
    s = _approved(client, db, fake_auth)
    monkeypatch.setattr(app_module, '_sb_delete_auth_user', lambda uid: 'deleted')  # no cascade
    assert _eliminar(client).status_code == 503
    assert len(_new_clinics(db)) == 1 and _sol(db)['usuario_id'] == s['usuario_id']


# ── 8. real helpers: request shapes, and nothing about the user logged ───

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body=''):
        super().__init__('https://fake.invalid', code, 'err', {}, io.BytesIO(body.encode()))


_REAL = {}


def test_recovery_link_request_shape(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, method=req.get_method(), body=json.loads(req.data))
        return _FakeResponse({'id': 'u-1', 'action_link': 'https://fake.supabase/verify?type=recovery'})
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', fake_urlopen)
    assert _REAL['recovery'](EMAIL) == 'https://fake.supabase/verify?type=recovery'
    assert seen['url'].endswith('/auth/v1/admin/generate_link') and seen['method'] == 'POST'
    assert seen['body'] == {'type': 'recovery', 'email': EMAIL, 'redirect_to': 'https://app.clinianotes.com/set-password'}


def test_auth_user_helpers_request_shapes(monkeypatch):
    seen = []

    def ok(req, timeout):
        seen.append((req.get_method(), req.full_url))
        return _FakeResponse({'id': 'u-1', 'email_confirmed_at': None})
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', ok)
    assert _REAL['get_user']('u-1') == ('ok', {'id': 'u-1', 'email_confirmed_at': None})
    assert _REAL['delete_user']('u-1') == 'deleted'
    # Built from Config, not a literal: CI sets its own fake SUPABASE_URL.
    users_url = f'{app_module.Config.SUPABASE_URL}/auth/v1/admin/users/u-1'
    assert seen == [('GET', users_url), ('DELETE', users_url)]

    def missing(req, timeout):
        raise _FakeHTTPError(404, json.dumps({'msg': 'User not found'}))
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', missing)
    assert _REAL['get_user']('u-1') == ('missing', None)
    assert _REAL['delete_user']('u-1') == 'missing'


def test_patch_returning_quiet_sends_representation(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(method=req.get_method(), prefer=req.get_header('Prefer'))
        return _FakeResponse([])
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', fake_urlopen)
    assert _REAL['patch']('/rest/v1/solicitudes_registro?id=eq.x&estado=eq.pendiente', {'estado': 'en_proceso'}) == []
    assert seen == {'method': 'PATCH', 'prefer': 'return=representation'}



class _FakeHeadResponse:
    def __init__(self, content_range):
        self.headers = {'Content-Range': content_range} if content_range is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_count_quiet_sends_head_with_count_exact(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(method=req.get_method(), prefer=req.get_header('Prefer'), url=req.full_url, data=req.data)
        return _FakeHeadResponse('0-2/3')
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', fake_urlopen)
    path = '/rest/v1/solicitudes_registro?estado=in.(pendiente,en_proceso)&select=id'
    assert _REAL['count'](path) == 3
    assert seen == {'method': 'HEAD', 'prefer': 'count=exact', 'data': None,
                    'url': f'{app_module.Config.SUPABASE_URL}{path}'}


@pytest.mark.parametrize('content_range, expected', [
    ('*/0', 0), ('0-0/1', 1), ('0-11/12', 12), (None, None), ('0-4/*', None), ('garbage', None),
])
def test_count_quiet_parses_content_range(monkeypatch, content_range, expected):
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', lambda req, timeout: _FakeHeadResponse(content_range))
    assert _REAL['count']('/rest/v1/solicitudes_registro?select=id') == expected

def _assert_no_personal_data(lines):
    text = '\n'.join(lines)
    for secret in (EMAIL, CEDULA, TELEFONO, DOMICILIO, 'dra.inventada', '@example.test',
                   'superadmin@clinia-ficticia.mx'):
        assert secret not in text, f'log leaked {secret!r}'


def test_real_quiet_helpers_never_log_personal_data(monkeypatch, log_lines):
    body = json.dumps({'message': f'conflict on {EMAIL}', 'details': f'({CEDULA}, {TELEFONO}, {DOMICILIO})'})

    def http_fail(req, timeout):
        raise _FakeHTTPError(500, body)
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', http_fail)
    assert _REAL['patch'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}', {'telefono': TELEFONO}) is None
    assert _REAL['delete'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}') is False
    assert _REAL['count'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}') is None
    assert _REAL['get_user']('u-1') == ('error', None)
    assert _REAL['delete_user']('u-1') == 'error'
    with pytest.raises(urllib.error.HTTPError):
        _REAL['recovery'](EMAIL)

    def net_fail(req, timeout):
        raise OSError(f'reset while sending {EMAIL} {TELEFONO}')
    monkeypatch.setattr(app_module.urllib.request, 'urlopen', net_fail)
    assert _REAL['patch'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}', {}) is None
    assert _REAL['delete'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}') is False
    assert _REAL['count'](f'/rest/v1/solicitudes_registro?email=eq.{EMAIL}') is None
    assert _REAL['get_user']('u-1') == ('error', None)

    assert log_lines
    _assert_no_personal_data(log_lines)


def test_provisioning_failures_never_log_the_email(monkeypatch, log_lines):
    def boom(email):
        raise _FakeHTTPError(422, json.dumps({'msg': f'{email} already registered'}))
    monkeypatch.setattr(app_module, '_sb_generate_invite_link', boom)
    with pytest.raises(app_module.ProvisioningError):
        app_module._provision_usuario(NOMBRE, EMAIL, 'admin', CLINIC_A, cedula=CEDULA, especialidad='X')

    monkeypatch.setattr(app_module, '_sb_generate_invite_link', lambda email: ('auth-u-9', 'https://fake'))
    monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda body: None)
    with pytest.raises(app_module.ProvisioningError) as exc:
        app_module._provision_usuario(NOMBRE, EMAIL, 'admin', CLINIC_A, cedula=CEDULA, especialidad='X')
    assert exc.value.stage == 'insert' and exc.value.user_id == 'auth-u-9'

    with pytest.raises(app_module.ProvisioningError) as exc:
        app_module._provision_usuario(NOMBRE, EMAIL, 'admin', CLINIC_A, on_auth_user=lambda uid: False)
    assert exc.value.stage == 'record'

    assert log_lines
    _assert_no_personal_data(log_lines)


@pytest.mark.parametrize('send', ['invite', 'agregado', 'rechazo'])
def test_email_failures_never_log_addresses(monkeypatch, log_lines, send):
    def boom(params):
        raise RuntimeError(f"invalid recipient {params['to'][0]} ({EMAIL}, {TELEFONO})")
    monkeypatch.setattr(email_service.resend.Emails, 'send', boom)
    call = {
        'invite': lambda: email_service.send_invite_email(EMAIL, NOMBRE, RAZON, 'https://fake'),
        'agregado': lambda: email_service.send_usuario_agregado_email(f'{ADMIN_A}@example.test', NOMBRE, RAZON, 'Médico'),
        'rechazo': lambda: email_service.send_rejection_email(EMAIL, NOMBRE, 'motivo', 'superadmin@clinia-ficticia.mx'),
    }[send]
    assert call() is False
    monkeypatch.setattr(email_service.resend.Emails, 'send', lambda p: {'id': 'fake-id'})
    assert call() is True
    assert log_lines
    _assert_no_personal_data(log_lines)


def test_every_superadmin_flow_logs_no_personal_data(client, db, fake_auth, emails, monkeypatch, log_lines):
    # approve into a new clinic, with one failure + retry
    db.add('solicitudes_registro', _solicitud())
    _FailOnce(monkeypatch, '_sb_insert_usuario', lambda body: True)
    _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'})
    _sa(client, 'POST', '/api/superadmin/solicitudes/sol-1/reenviar', {})
    _sa(client, 'GET', '/api/superadmin/solicitudes?estado=aprobada')
    _eliminar(client)
    # approve into an existing clinic, duplicate email, reject with email
    db.add('solicitudes_registro', _solicitud(id='sol-2', rol_solicitado='medico'))
    _aprobar(client, {'clinica_id': CLINIC_A, 'rol_otorgado': 'medico'}, sid='sol-2')
    db.add('solicitudes_registro', _solicitud(id='sol-3', email=f'{DOCTOR_A1}@example.test'))
    _aprobar(client, {'clinica_id': None, 'rol_otorgado': 'admin'}, sid='sol-3')
    db.add('solicitudes_registro', _solicitud(id='sol-4', email='otra.persona@consultorio-ficticio.mx'))
    _rechazar(client, {'motivo': f'Cédula {CEDULA} no coincide', 'enviar_correo': True}, sid='sol-4')
    _sa(client, 'GET', '/api/superadmin/solicitudes?estado=rechazada')
    _sa(client, 'GET', f'/api/superadmin/clinicas?q={RAZON}')

    sa_lines = [l for l in log_lines if l.startswith('Superadmin:')]
    assert any('accion=aprobar outcome=failed-at-guardar-el-usuario' in l for l in sa_lines)
    assert any('accion=aprobar outcome=approved' in l for l in sa_lines)
    assert any('accion=aprobar outcome=duplicate-email' in l for l in sa_lines)
    assert any('accion=rechazar outcome=rejected-emailed' in l for l in sa_lines)
    assert any('accion=eliminar outcome=deleted-with-clinic' in l for l in sa_lines)
    _assert_no_personal_data(log_lines)


# Captured at import time, before conftest's autouse fixture patches the
# module attributes for each test.
_REAL.update(
    recovery=app_module._sb_generate_recovery_link,
    get_user=app_module._sb_get_auth_user,
    delete_user=app_module._sb_delete_auth_user,
    patch=app_module._sb_patch_returning_quiet,
    delete=app_module._sb_delete_quiet,
    count=app_module._sb_count_quiet,
)
