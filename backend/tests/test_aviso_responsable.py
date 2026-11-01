# backend/tests/test_aviso_responsable.py
# 0.8.3: the patient aviso modal on / shows the signed-in doctor's own
# nombre, cédula and clinic domicilio in place of the v1.2 brackets, via
# GET /api/aviso-responsable. The Correo line stays bracketed and the
# email is never returned. Offline and PHI-free: every value is invented;
# the data layer is conftest's FakeDB.

import importlib.util
from pathlib import Path

import pytest

import app as app_module
from tests import tokens
from tests.conftest import CLINIC_A, CLINIC_B, DOCTOR_A1, DOCTOR_B1, ADMIN_A, AUXILIAR_A

ROOT = Path(__file__).resolve().parents[2]
URL = '/api/aviso-responsable'

_spec = importlib.util.spec_from_file_location('build_legal', ROOT / 'legal' / 'build_legal.py')
build_legal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_legal)


def _get(client, user_id):
    return client.get(URL, headers=tokens.auth_headers(tokens.mint(user_id)))


def _set_direccion(db, clinica_id, direccion):
    for row in db.tables['clinicas']:
        if row['id'] == clinica_id:
            row['direccion'] = direccion


# ── the endpoint ────────────────────────────────────────────────────────

def test_returns_callers_nombre_cedula_and_clinic_domicilio(client, db):
    _set_direccion(db, CLINIC_A, 'Av. Ficticia 123, Col. Prueba, Saltillo')
    resp = _get(client, DOCTOR_A1)
    assert resp.status_code == 200
    assert resp.get_json() == {
        'nombre':    f'Dr. {DOCTOR_A1}',
        'cedula':    f'CED-{DOCTOR_A1}',
        'domicilio': 'Av. Ficticia 123, Col. Prueba, Saltillo',
    }


def test_never_returns_the_email(client, db):
    _set_direccion(db, CLINIC_A, 'Av. Ficticia 123')
    resp = _get(client, DOCTOR_A1)
    body = resp.get_json()
    assert 'correo' not in body and 'email' not in body
    assert '@' not in resp.get_data(as_text=True)


def test_each_doctor_gets_only_their_own_data(client, db):
    _set_direccion(db, CLINIC_A, 'Domicilio A (ficticio)')
    _set_direccion(db, CLINIC_B, 'Domicilio B (ficticio)')
    b = _get(client, DOCTOR_B1).get_json()
    assert b == {'nombre': f'Dr. {DOCTOR_B1}', 'cedula': f'CED-{DOCTOR_B1}',
                 'domicilio': 'Domicilio B (ficticio)'}


def test_query_string_cannot_pick_another_user(client, db):
    _set_direccion(db, CLINIC_B, 'Domicilio B (ficticio)')
    resp = client.get(f'{URL}?usuario_id={DOCTOR_B1}&clinica_id={CLINIC_B}',
                      headers=tokens.auth_headers(tokens.mint(DOCTOR_A1)))
    assert resp.get_json()['nombre'] == f'Dr. {DOCTOR_A1}'
    assert 'domicilio' not in resp.get_json()   # clinic A has no address


def test_admin_who_is_a_doctor_gets_values(client, db):
    _set_direccion(db, CLINIC_A, 'Av. Ficticia 123')
    assert _get(client, ADMIN_A).get_json()['cedula'] == f'CED-{ADMIN_A}'


def test_auxiliar_gets_404_and_keeps_every_bracket(client):
    assert _get(client, AUXILIAR_A).status_code == 404


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer not-a-real-token'}])
def test_requires_a_valid_token(client, headers):
    assert client.get(URL, headers=headers).status_code == 401


def test_empty_domicilio_is_left_out(client):
    # conftest's clinics have direccion '' — the modal keeps the bracket.
    body = _get(client, DOCTOR_A1).get_json()
    assert 'domicilio' not in body and body['nombre'] and body['cedula']


def test_blank_values_are_left_out(client, db):
    for row in db.tables['usuarios']:
        if row['id'] == DOCTOR_A1:
            row['cedula'] = '   '
    _set_direccion(db, CLINIC_A, '  ')
    assert set(_get(client, DOCTOR_A1).get_json()) == {'nombre'}


def test_failed_lookups_are_left_out_not_an_error(client, db, monkeypatch):
    _set_direccion(db, CLINIC_A, 'Av. Ficticia 123')
    real_get = db.get

    def failing_get(path):
        if path.startswith('/rest/v1/clinicas') or 'select=cedula' in path:
            return None   # what _sb_get returns on HTTP error/timeout
        return real_get(path)
    monkeypatch.setattr(app_module, '_sb_get', failing_get)
    resp = _get(client, DOCTOR_A1)
    assert resp.status_code == 200
    assert resp.get_json() == {'nombre': f'Dr. {DOCTOR_A1}'}


# ── the markers the modal fills ─────────────────────────────────────────

def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_app_copy_marks_exactly_the_responsable_placeholders():
    partial = _read('backend/templates/legal/aviso_pacientes.html')
    assert partial.count('data-responsable="nombre">[Nombre completo del médico]</strong>') == 2
    assert partial.count('data-responsable="cedula">[número]</strong>') == 1
    assert partial.count('data-responsable="domicilio">[domicilio del consultorio o clínica]</strong>') == 2
    assert partial.count('data-responsable=') == 5
    # Correo stays a plain bracket, never filled.
    assert '<strong>[correo]</strong>' in partial


def test_public_legal_page_keeps_the_brackets_unmarked():
    page = _read('landing/legal/aviso-pacientes/index.html')
    assert 'data-responsable' not in page
    assert page.count('<strong>[Nombre completo del médico]</strong>') == 2


def test_app_modal_ships_the_markers(client):
    html = client.get('/').get_data(as_text=True)
    modal = html[html.index('id="avisoModal"'):html.index('<!-- /avisoModal -->')]
    assert modal.count('data-responsable=') == 5


def test_generator_fails_loudly_if_a_placeholder_disappears():
    body = '<p><strong>[número]</strong></p>'
    with pytest.raises(ValueError):
        build_legal._mark_responsable(body)


def test_frontend_fills_with_textcontent_not_innerhtml():
    js = _read('backend/static/app.js')
    start = js.index('async function fillAvisoResponsable')
    fn = js[start:js.index('\n}\n', start)]
    assert '/api/aviso-responsable' in fn
    assert 'textContent' in fn and 'innerHTML' not in fn
