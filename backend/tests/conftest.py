# backend/tests/conftest.py
# Test infrastructure for the route-level authorization suite. Mocks the
# auth boundary (JWT verification, JWKS) and the entire data layer
# (Supabase REST + Storage, via app.py's _sb_*/_storage_* functions) so
# the suite runs offline, deterministically, with zero PHI and zero live
# network calls — only the real route/auth code under test is exercised.
#
# Run with:  cd backend && venv/bin/python -m pytest tests/

import os
import sys
import importlib

# Dummy env vars — must exist before config.py/app.py import, but their
# values are never used for anything real: every network call app.py
# would make with them is mocked out below.
os.environ.setdefault('SUPABASE_URL', 'https://fake-test-project.supabase.co')
os.environ.setdefault('SUPABASE_SERVICE_KEY', 'fake-service-key')
os.environ.setdefault('SUPABASE_ANON_KEY', 'fake-anon-key')
os.environ.setdefault('GEMINI_API_KEY', 'fake-gemini-key')
os.environ.setdefault('ASSEMBLYAI_API_KEY', 'fake-assemblyai-key')
os.environ.setdefault('FLASK_SECRET_KEY', 'fake-flask-secret-for-tests-only')
os.environ.setdefault('RESEND_API_KEY', 'fake-resend-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import auth as auth_module
import app as app_module
from tests.fake_db import FakeDB


# ── Synthetic fixture identities (no PHI — every value below is made up) ──

CLINIC_A = 'clinic-a-11111111'
CLINIC_B = 'clinic-b-22222222'

DOCTOR_A1 = 'doctor-a1-33333333'   # clinic A, rol medico
DOCTOR_A2 = 'doctor-a2-44444444'   # clinic A, rol medico — same clinic as A1, different doctor
ADMIN_A   = 'admin-a-55555555'     # clinic A, rol admin
DOCTOR_B1 = 'doctor-b1-66666666'   # clinic B, rol medico
ADMIN_B   = 'admin-b-77777777'     # clinic B, rol admin
INACTIVE_DOCTOR = 'inactive-88888888'  # clinic A, activo=False

SESSION_CONFIRMED_A = 'SESSION-CONFIRMED-A'   # confirmed, authored by DOCTOR_A1, clinic A
SESSION_PENDING_A   = 'SESSION-PENDING-A'     # pending_review, authored by DOCTOR_A1, clinic A
SESSION_CONFIRMED_B = 'SESSION-CONFIRMED-B'   # confirmed, authored by DOCTOR_B1, clinic B

JOB_A1 = 'job-a1-done'  # trabajos row, owned by DOCTOR_A1, status done


def _usuario_row(uid, clinica_id, rol, activo=True, nombre=None):
    return {
        'id': uid, 'clinica_id': clinica_id, 'rol': rol, 'activo': activo,
        'nombre': nombre or f'Dr. {uid}', 'email': f'{uid}@example.test',
        'especialidad': 'Medicina General', 'cedula': f'CED-{uid}',
    }


@pytest.fixture
def db():
    """A fresh FakeDB per test, seeded with the standard fixture set."""
    d = FakeDB()
    d.seed('clinicas', [
        {'id': CLINIC_A, 'nombre': 'Clínica A (fixture)', 'color_primario': '#0F6E56',
         'direccion': '', 'telefono': '', 'logo_url': ''},
        {'id': CLINIC_B, 'nombre': 'Clínica B (fixture)', 'color_primario': '#0F6E56',
         'direccion': '', 'telefono': '', 'logo_url': ''},
    ])
    d.seed('usuarios', [
        _usuario_row(DOCTOR_A1, CLINIC_A, 'medico'),
        _usuario_row(DOCTOR_A2, CLINIC_A, 'medico'),
        _usuario_row(ADMIN_A, CLINIC_A, 'admin'),
        _usuario_row(DOCTOR_B1, CLINIC_B, 'medico'),
        _usuario_row(ADMIN_B, CLINIC_B, 'admin'),
        _usuario_row(INACTIVE_DOCTOR, CLINIC_A, 'medico', activo=False),
    ])
    fake_structured_data = {
        'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba', 'curp': 'FAKE000101HDFXXX01'},
        'subjetivo': {'motivo_de_consulta': 'chequeo de rutina (fixture)'},
        'objetivo': {}, 'evaluacion': {}, 'plan': {},
    }
    fake_full_response = {
        'session_id': SESSION_CONFIRMED_A,
        'status': 'confirmed',
        'transcript': {'text': '', 'labeled_text': None},
        'structured_data': fake_structured_data,
        'consent_given': True, 'consent_timestamp': '2026-01-01T00:00:00',
    }
    d.seed('sesiones', [
        {
            'session_id': SESSION_CONFIRMED_A, 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
            'status': 'confirmed', 'timestamp': '2026-01-01T00:00:00',
            'structured_data': fake_structured_data, 'full_response': fake_full_response,
            'addenda': [], 'cancelled_at': None, 'cancellation_reason': None,
            'paciente_curp': 'FAKE000101HDFXXX01',
        },
        {
            'session_id': SESSION_PENDING_A, 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
            'status': 'pending_review', 'timestamp': '2026-01-02T00:00:00',
            'structured_data': fake_structured_data,
            'full_response': {**fake_full_response, 'session_id': SESSION_PENDING_A, 'status': 'pending_review'},
            'addenda': [], 'cancelled_at': None, 'cancellation_reason': None,
            'paciente_curp': 'FAKE000101HDFXXX01',
        },
        {
            'session_id': SESSION_CONFIRMED_B, 'usuario_id': DOCTOR_B1, 'clinica_id': CLINIC_B,
            'status': 'confirmed', 'timestamp': '2026-01-01T00:00:00',
            'structured_data': fake_structured_data,
            'full_response': {**fake_full_response, 'session_id': SESSION_CONFIRMED_B},
            'addenda': [], 'cancelled_at': None, 'cancellation_reason': None,
            'paciente_curp': 'FAKE000101HDFXXX01',
        },
    ])
    d.seed('trabajos', [
        {'job_id': JOB_A1, 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
         'status': 'done', 'error_message': None, 'updated_at': '2026-01-01T00:00:00',
         'session_id': SESSION_CONFIRMED_A, 'structured_data': fake_structured_data,
         'transcript': {'text': ''}},
    ])
    return d


@pytest.fixture(autouse=True)
def _patch_data_layer(db, monkeypatch):
    """
    Routes every _sb_get/_sb_patch/_sb_delete/_sb_post_job/_sb_patch_job
    call through the FakeDB, and stubs every other external side-effecting
    call (Storage, PDF rendering, Supabase Admin API, ban/invite, the
    background job executor) with a cheap, deterministic fake — nothing
    in this fixture ever performs real network I/O. Autouse: every test
    gets this without asking for it explicitly, since forgetting it on
    even one test would mean a real network attempt.
    """
    def _fake_fetch_usuario_context(user_id):
        # Mirrors auth._fetch_usuario_context's real contract exactly
        # (raise UsuarioInactivoError / return None / return the context
        # dict) — this is a SEPARATE urllib.request call from app.py's
        # _sb_get, in auth.py, so it needs its own mock reading the same
        # FakeDB rather than being covered by the app_module patches below.
        rows = db.get(f'/rest/v1/usuarios?id=eq.{user_id}&limit=1')
        if not rows:
            return None
        row = rows[0]
        if row.get('activo') is False:
            raise auth_module.UsuarioInactivoError()
        return {
            'usuario_id': row['id'], 'clinica_id': row['clinica_id'],
            'rol': row['rol'], 'nombre': row['nombre'], 'email': row['email'],
        }
    monkeypatch.setattr(auth_module, '_fetch_usuario_context', _fake_fetch_usuario_context)

    monkeypatch.setattr(app_module, '_sb_get', db.get)
    monkeypatch.setattr(app_module, '_sb_patch', db.patch)
    monkeypatch.setattr(app_module, '_sb_delete', db.delete)
    monkeypatch.setattr(app_module, '_sb_post_job', lambda body: db.post('/rest/v1/trabajos', body).get('job_id', 'fake-job-id') or 'fake-job-id')
    monkeypatch.setattr(app_module, '_sb_patch_job', lambda job_id, body: db.patch(f'/rest/v1/trabajos?job_id=eq.{job_id}', body))
    monkeypatch.setattr(app_module, '_sb_log_lectura', lambda session_id, usuario_id: None)
    monkeypatch.setattr(app_module, '_sb_set_user_ban', lambda user_id, banned: None)
    monkeypatch.setattr(app_module, '_sb_generate_invite_link', lambda email: ('fake-invited-user-id', 'https://fake.invite/link'))
    monkeypatch.setattr(app_module, '_sb_insert_usuario', lambda body: {**body, 'id': 'fake-new-usuario-id'})
    monkeypatch.setattr(app_module, '_storage_download', lambda bucket, path: (b'FAKE-LOGO-BYTES', 'image/png'))
    monkeypatch.setattr(app_module, '_storage_upload', lambda bucket, path, data, content_type: True)
    monkeypatch.setattr(app_module, '_storage_delete', lambda bucket, path: True)
    monkeypatch.setattr(app_module.pdf_generator, 'generate_pdf', lambda *a, **k: b'%PDF-FAKE-BYTES')
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: True)
    monkeypatch.setattr(app_module, 'send_invite_email', lambda *a, **k: True)
    # Run the "background" job synchronously-as-a-noop instead of via a
    # real thread pool — process_audio's own response (202 + job_id) is
    # what auth tests care about; nothing here should touch a real
    # transcription/LLM service even on a background thread.
    monkeypatch.setattr(app_module._executor, 'submit', lambda fn, *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """
    Every per-process cache this app added (Stage M2 JWKS, Stage E2
    usuario_context + clinic logo) is a plain module-level dict — real
    and correct in production, but a test-isolation hazard here: without
    clearing it, test B could silently read test A's cached auth outcome
    for a reused synthetic user_id. Clears before AND after each test.
    """
    def _clear():
        auth_module._usuario_cache.clear()
        auth_module._jwks_keys_by_kid.clear()
        auth_module._jwks_fetched_at = 0.0
        auth_module._jwks_last_attempt_at = 0.0
        app_module._logo_cache.clear()
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _mock_jwt_verification(monkeypatch):
    """
    Replaces auth.verify_jwt with a fake-token parser instead of real
    ES256/JWKS verification, so the suite needs no real Supabase project.
    require_auth's own code (header presence, the None-check, the
    UsuarioInactivoError path, the g.usuario assignment) all run for
    real — only the cryptographic verification is stubbed. See
    tests/tokens.py for the fake-token format this parses.
    """
    from tests.tokens import fake_verify_jwt
    monkeypatch.setattr(auth_module, 'verify_jwt', fake_verify_jwt)


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()
