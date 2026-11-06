# backend/tests/test_review_integrity.py
# 0.8.4 — CIE-11 review, batch 1 (backend half; the frontend half is
# tests/frontend/review_integrity.html):
#   #7  icd_service's lookup-failure log line never carries the request URL
#       or the query (the diagnosis — PHI).
#   #3  confirm-and-generate stops with a 503 when the confirmed save fails,
#       before the PDF is emailed or the transcript / trabajos copies are
#       deleted; the note stays pending_review and a retry works.
#   #16 _run_job reports 'error', not 'done', when its pending save fails.
# Invented data only; no network (conftest mocks the data layer, and the
# WHO API is mocked here).

import io
import logging

import pytest
import requests

import app as app_module
import icd_service
from logger import logger
from tests import tokens
from tests.conftest import DOCTOR_A1, SESSION_PENDING_A, CLINIC_A
from tests.test_route_authorization import _call

FAKE_DX = 'diabetes ficticia del paciente de prueba'


# ── #7: no PHI in the ICD failure log ───────────────────────────────────

@pytest.fixture
def icd_log(monkeypatch):
    """Captures the 'clinia' logger (propagate=False, so caplog can't) and
    gives lookup_cie11 a token without touching the WHO token endpoint."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    monkeypatch.setattr(icd_service, '_get_token', lambda: 'fake-token')
    yield buf
    logger.removeHandler(handler)


def _full_url(url, params):
    return requests.Request('GET', url, params=params).prepare().url


def _resp(status, url):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.reason = 'Fixture'
    return r


def _assert_no_phi(text):
    assert text.strip(), 'expected a failure log line'
    assert 'q=' not in text
    assert 'diabetes' not in text and 'ficticia' not in text
    assert 'id.who.int' not in text and '/search' not in text


@pytest.mark.parametrize('status', [400, 404, 500, 503])
def test_icd_http_error_log_has_status_only(icd_log, monkeypatch, status):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _resp(status, _full_url(url, params))
    monkeypatch.setattr(icd_service.requests, 'get', fake_get)

    assert icd_service.lookup_cie11(FAKE_DX) is None
    text = icd_log.getvalue()
    _assert_no_phi(text)
    assert f'ICD: outcome=http_{status} stage=full cluster=0 ms=' in text


def test_icd_connection_error_log_has_type_only(icd_log, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        # The shape requests really raises: the URL, query included, is in the message.
        raise requests.ConnectionError(
            f"HTTPSConnectionPool(host='id.who.int', port=443): Max retries exceeded "
            f"with url: {_full_url(url, params)}")
    monkeypatch.setattr(icd_service.requests, 'get', fake_get)

    assert icd_service.lookup_cie11(FAKE_DX) is None
    text = icd_log.getvalue()
    _assert_no_phi(text)
    assert 'ICD: outcome=timeout stage=full cluster=0 ms=' in text   # unreachable counts as timeout


def test_icd_timeout_and_bad_json_logs_have_type_only(icd_log, monkeypatch):
    def timeout_get(url, params=None, headers=None, timeout=None):
        raise requests.Timeout(f'Read timed out. (url: {_full_url(url, params)})')
    monkeypatch.setattr(icd_service.requests, 'get', timeout_get)
    assert icd_service.lookup_cie11(FAKE_DX) is None
    # 0.8.6: a timeout is cached for FAILURE_CACHE_SECONDS; clear it so the
    # bad-JSON lookup below actually runs.
    icd_service._token_cache.update(failed_until=0, failure=None)

    class BadJson:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): raise ValueError(f'Expecting value: {FAKE_DX}')
    monkeypatch.setattr(icd_service.requests, 'get', lambda *a, **k: BadJson())
    assert icd_service.lookup_cie11(FAKE_DX) is None

    text = icd_log.getvalue()
    _assert_no_phi(text)
    assert 'ICD: outcome=timeout stage=full cluster=0 ms=' in text
    assert 'ICD: outcome=bad_json stage=full cluster=0 ms=' in text


def test_icd_success_still_returns_code(icd_log, monkeypatch):
    class Ok:
        status_code = 200
        def raise_for_status(self): pass
        # 0.8.8: the title shares a word with the search (relevance check).
        def json(self): return {'destinationEntities': [{'theCode': '5A11', 'title': 'Diabetes ficticia (título)'}]}
    monkeypatch.setattr(icd_service.requests, 'get', lambda *a, **k: Ok())
    assert icd_service.lookup_cie11(FAKE_DX) == {'code': '5A11', 'title': 'Diabetes ficticia (título)'}
    assert FAKE_DX not in icd_log.getvalue()


# ── save_session reports its outcome ────────────────────────────────────

def test_save_session_returns_true_and_writes(db):
    assert app_module.save_session('SESSION-FIXTURE-NEW', {'status': 'pending_review', 'structured_data': {}},
                                   usuario_id=DOCTOR_A1, clinica_id=CLINIC_A) is True
    assert any(r['session_id'] == 'SESSION-FIXTURE-NEW' for r in db.tables['sesiones'])


def test_save_session_returns_false_on_failure_without_raising(monkeypatch):
    def boom(body):
        raise OSError('fixture: network down')
    monkeypatch.setattr(app_module, '_sb_upsert_session', boom)
    assert app_module.save_session('SESSION-FIXTURE-NEW', {'structured_data': {}},
                                   usuario_id=DOCTOR_A1, clinica_id=CLINIC_A) is False


# ── #3: a failed confirmed save stops confirm-and-generate ──────────────

def _confirm(client):
    return _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': SESSION_PENDING_A,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
                            'evaluacion': {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A11'}},
        'create_pdf': True, 'send_email': True,
    })


def _pending_row(db):
    return next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_PENDING_A)


@pytest.fixture
def side_effects(db, monkeypatch):
    """Records every irreversible step confirm takes after its save."""
    rec = {'emails': [], 'patches': [], 'deletes': []}
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: rec['emails'].append(k) or True)
    monkeypatch.setattr(app_module, '_sb_patch', lambda p, b: rec['patches'].append(p) or db.patch(p, b))
    monkeypatch.setattr(app_module, '_sb_delete', lambda p: rec['deletes'].append(p) or db.delete(p))
    db.add('trabajos', {'job_id': 'job-fixture-pending', 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
                        'status': 'done', 'session_id': SESSION_PENDING_A, 'transcript': {'text': 'x'}})
    _pending_row(db)['transcript_text'] = 'transcripción ficticia'
    return rec


def test_confirm_with_failed_save_returns_503_and_does_nothing_irreversible(client, db, monkeypatch, side_effects):
    def boom(body):
        raise OSError('fixture: network down')
    monkeypatch.setattr(app_module, '_sb_upsert_session', boom)

    resp = _confirm(client)
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['error'] == 'No se pudo guardar la nota. Intente de nuevo.'
    assert body['error_code'] == 'CONFIRM_SAVE_FAILED'
    assert side_effects == {'emails': [], 'patches': [], 'deletes': []}
    row = _pending_row(db)
    assert row['status'] == 'pending_review'
    assert row['transcript_text'] == 'transcripción ficticia'
    assert any(t['session_id'] == SESSION_PENDING_A for t in db.tables['trabajos'])


def test_confirm_retry_after_failed_save_confirms(client, db, monkeypatch, side_effects):
    real = app_module._sb_upsert_session
    state = {'fail': True}

    def flaky(body):
        if state['fail']:
            raise OSError('fixture: network down')
        return real(body)
    monkeypatch.setattr(app_module, '_sb_upsert_session', flaky)

    assert _confirm(client).status_code == 503
    state['fail'] = False
    resp = _confirm(client)
    assert resp.status_code == 200
    row = _pending_row(db)
    assert row['status'] == 'confirmed'
    assert row['structured_data']['evaluacion']['codigo_cie11'] == '5A11'
    assert row['transcript_text'] is None
    assert not any(t.get('session_id') == SESSION_PENDING_A for t in db.tables['trabajos'])
    assert len(side_effects['emails']) == 1


# ── #16: a failed pending save fails the job ────────────────────────────

FAKE_SD = {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
           'evaluacion': {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A11'},
           'metadata': {}}


@pytest.fixture
def job(db, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.transcription_service, 'transcribe_audio', lambda *a, **k: {
        'text': 'transcripción ficticia suficientemente larga', 'utterances': [],
        'confidence': 0.9, 'audio_duration': 60000, 'words': 5})
    monkeypatch.setattr(app_module.llm_processor, 'extract_structured_data',
                        lambda *a, **k: {k2: dict(v) for k2, v in FAKE_SD.items()})
    monkeypatch.setattr(app_module.llm_processor, 'validate_against_schema', lambda sd: (True, None))
    db.add('trabajos', {'job_id': 'job-fixture-run', 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
                        'status': 'queued'})
    audio = tmp_path / 'fixture.webm'
    audio.write_bytes(b'fake-audio')
    params = {'print_raw': False, 'speakers_expected': 2, 'consultation_timestamp': '2026-11-04 10:00',
              'local_timestamp': '2026-11-04 10:00', 'consent_given': True,
              'consent_timestamp': '2026-11-04T10:00:00'}

    def run():
        app_module._run_job('job-fixture-run', str(audio), params, DOCTOR_A1, CLINIC_A, 'Dr. Fixture')
        return next(t for t in db.tables['trabajos'] if t['job_id'] == 'job-fixture-run')
    return run


def test_run_job_with_failed_save_reports_error(job, db, monkeypatch):
    before = len(db.tables['sesiones'])

    def boom(body):
        raise OSError('fixture: network down')
    monkeypatch.setattr(app_module, '_sb_upsert_session', boom)

    row = job()
    assert row['status'] == 'error'
    assert row['error_message'] == 'No se pudo guardar la nota. Intente de nuevo.'
    assert not row.get('session_id') and not row.get('structured_data')
    assert len(db.tables['sesiones']) == before


def test_run_job_with_successful_save_reports_done(job, db):
    row = job()
    assert row['status'] == 'done'
    session = next(r for r in db.tables['sesiones'] if r['session_id'] == row['session_id'])
    assert session['status'] == 'pending_review'
    assert session['structured_data']['evaluacion']['codigo_cie11'] == '5A11'
