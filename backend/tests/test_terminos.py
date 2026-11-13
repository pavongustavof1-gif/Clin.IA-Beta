# backend/tests/test_terminos.py
# 0.9.0 (#81): the one-time login notice of the updated legal texts. It
# records acceptance but never blocks anything. conftest seeds an acceptance
# of Config.TERMINOS_VERSION for every fixture user; these tests remove it.
# Synthetic data only.

import io
import logging
import re
from pathlib import Path

import pytest

import app as app_module
from config import Config
from logger import logger
from tests import tokens
from tests.conftest import (ADMIN_A, AUXILIAR_A, CLINIC_A, DOCTOR_A1, SESSION_CONFIRMED_A,
                            SESSION_PENDING_A)
from tests.route_inventory import ROUTE_INVENTORY

OLD_VERSION = 'ToS-1.2-Beta+AU-1.2-Beta'      # what solicitudes stored before 0.9.0
STATIC = Path(__file__).resolve().parent.parent / 'static'


def _headers(uid):
    return tokens.auth_headers(tokens.mint(uid))


def _unaccept(db, *uids):
    db.tables['aceptaciones_terminos'] = [r for r in db.tables['aceptaciones_terminos'] if r['usuario_id'] not in uids]
    app_module._terminos_aceptados.clear()


def _rows(db, uid):
    return [r for r in db.tables['aceptaciones_terminos'] if r['usuario_id'] == uid]


def _aceptada(client, uid):
    return client.get('/api/session-check', headers=_headers(uid)).get_json()['terminos']['aceptada']


@pytest.fixture
def log():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    yield buf
    logger.removeHandler(handler)


# ── nothing is blocked ───────────────────────────────────────────────────

def test_no_route_checks_acceptance():
    assert not hasattr(app_module, 'require_terminos')
    assert not any('terminos_required' in e for e in ROUTE_INVENTORY)
    src = (Path(app_module.__file__)).read_text()
    assert 'require_terminos' not in src and 'TERMINOS_PENDIENTES' not in src and ', 428' not in src


# The routes the old plan gated: each still works with no acceptance at all.
CALLS = [
    (DOCTOR_A1, '/api/process-audio', None, 400),          # 400: no consent — the route ran
    (DOCTOR_A1, '/api/confirm-and-generate', {'session_id': SESSION_PENDING_A}, None),
    (ADMIN_A, f'/api/admin/session/{SESSION_CONFIRMED_A}/addendum', {'texto': 'Adendum de prueba (fixture)'}, None),
]


@pytest.mark.parametrize('uid, url, body, expected', CALLS, ids=[c[1].split('/')[2] for c in CALLS])
def test_processing_works_without_acceptance(client, db, uid, url, body, expected):
    _unaccept(db, uid)
    if body is None:
        resp = client.post(url, headers=_headers(uid), data={})
    else:
        resp = client.post(url, headers=_headers(uid), json=body)
    assert resp.status_code not in (428, 503)
    assert (resp.get_json() or {}).get('error_code') not in ('TERMINOS_PENDIENTES', 'TERMINOS_NO_VERIFICADOS')
    if expected:
        assert resp.status_code == expected
    else:
        assert resp.status_code < 300, resp.get_json()
    assert _rows(db, uid) == []                                # nothing accepted on the user's behalf


def test_processing_works_when_the_lookup_fails(client, db, monkeypatch):
    real_get = app_module._sb_get
    monkeypatch.setattr(app_module, '_sb_get',
                        lambda path: None if 'aceptaciones_terminos' in path else real_get(path))
    _unaccept(db, DOCTOR_A1)
    assert client.post('/api/process-audio', headers=_headers(DOCTOR_A1), data={}).status_code == 400
    # session-check still answers; null → the page shows no notice.
    assert _aceptada(client, DOCTOR_A1) is None


def test_reading_stays_open_without_acceptance(client, db):
    _unaccept(db, DOCTOR_A1, ADMIN_A)
    assert client.get(f'/api/session/{SESSION_CONFIRMED_A}', headers=_headers(DOCTOR_A1)).status_code == 200
    assert client.get('/api/pending-sessions', headers=_headers(DOCTOR_A1)).status_code == 200
    assert client.get(f'/api/download-pdf/{SESSION_CONFIRMED_A}', headers=_headers(DOCTOR_A1)).status_code == 200
    assert client.get('/api/admin/usuarios', headers=_headers(ADMIN_A)).status_code == 200


# ── session-check: the acceptance screen at login ───────────────────────

@pytest.mark.parametrize('uid', [DOCTOR_A1, ADMIN_A, AUXILIAR_A])
def test_session_check_reports_acceptance_for_every_role(client, db, uid):
    data = client.get('/api/session-check', headers=_headers(uid)).get_json()
    assert data['terminos'] == {'version': Config.TERMINOS_VERSION, 'aceptada': True}
    _unaccept(db, uid)
    data = client.get('/api/session-check', headers=_headers(uid)).get_json()
    assert data['terminos'] == {'version': Config.TERMINOS_VERSION, 'aceptada': False}


# ── accepting ────────────────────────────────────────────────────────────

def test_accept_records_version_and_date_once_per_version(client, db, log):
    _unaccept(db, DOCTOR_A1)
    assert _aceptada(client, DOCTOR_A1) is False                            # the notice shows
    resp = client.post('/api/terminos/aceptar', headers=_headers(DOCTOR_A1), json={'version': Config.TERMINOS_VERSION})
    assert resp.status_code == 200 and resp.get_json() == {'version': Config.TERMINOS_VERSION, 'aceptada': True}
    (row,) = _rows(db, DOCTOR_A1)
    assert row['version'] == Config.TERMINOS_VERSION and row['clinica_id'] == CLINIC_A
    assert row['aceptado_at'].startswith('20') and row['aceptado_at'].endswith('+00:00')
    # Never again for this version — also after a restart (cache cleared).
    assert _aceptada(client, DOCTOR_A1) is True
    app_module._terminos_aceptados.clear()
    assert _aceptada(client, DOCTOR_A1) is True
    assert f'Terminos: usuario_id={DOCTOR_A1} version={Config.TERMINOS_VERSION} outcome=created' in log.getvalue()


def test_accept_keeps_the_history(client, db):
    _unaccept(db, DOCTOR_A1)
    db.add('aceptaciones_terminos', {'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A, 'version': OLD_VERSION,
                                     'aceptado_at': '2026-10-20T00:00:00+00:00'})
    client.post('/api/terminos/aceptar', headers=_headers(DOCTOR_A1), json={'version': Config.TERMINOS_VERSION})
    assert [r['version'] for r in _rows(db, DOCTOR_A1)] == [OLD_VERSION, Config.TERMINOS_VERSION]


def test_accepting_twice_is_fine_and_records_once(client, db):
    _unaccept(db, DOCTOR_A1)
    for _ in range(2):
        resp = client.post('/api/terminos/aceptar', headers=_headers(DOCTOR_A1),
                           json={'version': Config.TERMINOS_VERSION})
        assert resp.status_code == 200
    assert len(_rows(db, DOCTOR_A1)) == 1


@pytest.mark.parametrize('body', [{'version': OLD_VERSION}, {'version': ''}, {}, None])
def test_accepting_another_version_is_refused(client, db, body):
    _unaccept(db, DOCTOR_A1)
    resp = client.post('/api/terminos/aceptar', headers=_headers(DOCTOR_A1), json=body)
    assert resp.status_code == 409 and resp.get_json()['error_code'] == 'TERMINOS_VERSION_CAMBIO'
    assert resp.get_json()['version'] == Config.TERMINOS_VERSION
    assert _rows(db, DOCTOR_A1) == []


def test_auxiliar_can_accept(client, db):
    _unaccept(db, AUXILIAR_A)
    resp = client.post('/api/terminos/aceptar', headers=_headers(AUXILIAR_A), json={'version': Config.TERMINOS_VERSION})
    assert resp.status_code == 200 and len(_rows(db, AUXILIAR_A)) == 1


def test_accept_needs_a_token(client):
    assert client.post('/api/terminos/aceptar', json={'version': Config.TERMINOS_VERSION}).status_code == 401


def test_failed_insert_is_503_and_not_cached(client, db, monkeypatch):
    _unaccept(db, DOCTOR_A1)
    monkeypatch.setattr(app_module, '_sb_insert_aceptacion', lambda body: 'error')
    resp = client.post('/api/terminos/aceptar', headers=_headers(DOCTOR_A1), json={'version': Config.TERMINOS_VERSION})
    assert resp.status_code == 503
    assert _aceptada(client, DOCTOR_A1) is False                            # the notice comes back next login


def test_a_version_bump_shows_the_notice_again_but_blocks_nothing(client, db, monkeypatch):
    assert _aceptada(client, DOCTOR_A1) is True
    monkeypatch.setattr(Config, 'TERMINOS_VERSION', Config.TERMINOS_VERSION + '+X-9.9-Beta')
    assert _aceptada(client, DOCTOR_A1) is False                           # the cache holds the old version only
    assert client.post('/api/process-audio', headers=_headers(DOCTOR_A1), data={}).status_code == 400


# ── pages ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('page, script', [('/', 'app.js'), ('/admin', 'admin.js')])
def test_pages_load_the_notice_before_their_script(client, page, script):
    html = client.get(page).get_data(as_text=True)
    assert 'terminos.css' in html
    assert html.index('/static/terminos.js') < html.index(f'/static/{script}')
    assert html.index('/static/auth-common.js') < html.index('/static/terminos.js')


def test_notice_script_is_served_and_posts_the_shown_version(client):
    js = client.get('/static/terminos.js').get_data(as_text=True)
    assert '/api/terminos/aceptar' in js and 'JSON.stringify({ version })' in js
    assert "'Acepto'" in js and 'innerHTML' not in js
    assert 'TERMINOS_PENDIENTES' not in js and 'requireAccepted' not in js


def test_notice_describes_the_current_version():
    js = (STATIC / 'terminos.js').read_text()
    assert f"'{Config.TERMINOS_VERSION}': {{" in js
    assert 'terminos/' in js and 'aviso-pacientes/' in js


@pytest.mark.parametrize('script', ['app.js', 'admin.js'])
def test_page_scripts_only_show_the_notice(script):
    js = (STATIC / script).read_text()
    assert re.findall(r'Terminos\.\w+', js) == ['Terminos.fromSession']
