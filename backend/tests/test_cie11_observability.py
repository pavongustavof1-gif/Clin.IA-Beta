# backend/tests/test_cie11_observability.py
# 0.8.6 — CIE-11 review, batch 3 (backend half; the frontend half is in
# tests/frontend/review_integrity.html, cases 11-12):
#   #12  ICD11_RELEASE_VERSION checked at startup; empty/malformed disables
#        the lookup with one clear error instead of failing every request.
#   #17  one token refresh at a time; a 60 s failure cache after a token or
#        connection failure.
#   logs one "ICD: outcome=..." line per lookup and one "CIE11: session=..."
#        line per confirm — enums and numbers only, never the diagnosis,
#        the code or the title.
# Invented data only; WHO is mocked, no network.

import io
import logging
import re
import threading
import time

import pytest
import requests

import app as app_module
import icd_service
from llm_processor import apply_cie11_lookup
from logger import logger
from tests import tokens
from tests.conftest import DOCTOR_A1, SESSION_PENDING_A
from tests.test_route_authorization import _call

FAKE_DX = 'neumonía ficticia del paciente de prueba'
FAKE_CODE = '5A24/5A11'
FAKE_TITLE = 'Neumonía ficticia, título combinado'  # 0.8.8: relevant to FAKE_DX
OUTCOME_RE = re.compile(
    r'^ICD: outcome=(ok|no_results|no_code|low_score|irrelevant|http_\d{3}|timeout|token_failed|not_configured|bad_json|no_diagnosis|term_rejected)'
    r' stage=(full|term|autocode|none) cluster=[01] ms=\d+( score=[01]\.\d\d)?$')
CONFIRM_RE = re.compile(r'^CIE11: session=\S+ fuente=(oms|medico|none) cambio=(kept|changed|cleared|added|absent)$')


@pytest.fixture
def log():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    yield buf
    logger.removeHandler(handler)


def _lines(buf, prefix):
    return [l for l in buf.getvalue().splitlines() if l.startswith(prefix)]


def _assert_no_phi(text):
    for secret in (FAKE_DX, 'neumon', 'ficticia', FAKE_CODE, '5A24', '5A11', FAKE_TITLE, 'q=', 'id.who.int'):
        assert secret not in text, secret


class _Resp:
    def __init__(self, status=200, body=None, exc=None):
        self.status_code, self._body, self._exc = status, body, exc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Error for url: https://id.who.int/x?q={FAKE_DX}',
                                     response=self)

    def json(self):
        if self._exc:
            raise self._exc
        return self._body


@pytest.fixture
def who(monkeypatch):
    """Mock the WHO token and search endpoints and count calls to each."""
    st = {'token_calls': 0, 'search_calls': 0, 'token': lambda: _Resp(200, {'access_token': 'tok', 'expires_in': 3600}),
          'search': lambda: _Resp(200, {'destinationEntities': [{'theCode': FAKE_CODE, 'title': FAKE_TITLE, 'score': 1}]})}

    def post(url, data=None, timeout=None):
        st['token_calls'] += 1
        r = st['token']()
        if isinstance(r, Exception):
            raise r
        return r

    def get(url, params=None, headers=None, timeout=None):
        st['search_calls'] += 1
        r = st['search']()
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(icd_service.requests, 'post', post)
    monkeypatch.setattr(icd_service.requests, 'get', get)
    return st


# ── #12: release version ────────────────────────────────────────────────

@pytest.fixture
def restore_release():
    yield
    icd_service.configure_release(icd_service.Config.ICD11_RELEASE_VERSION)


@pytest.mark.parametrize('value', ['2026-01', '2025-12', ' 2024-01 '])
def test_valid_release_enables_lookup(value, restore_release):
    assert icd_service.configure_release(value) is True
    assert icd_service.SEARCH_ENDPOINT == f'https://id.who.int/icd/release/11/{value.strip()}/mms/search'


@pytest.mark.parametrize('value', ['', '   ', None, '2026', '2026-1', '2026-13', '2026-00', '26-01', '2026/01',
                                   '2026-01-01', 'latest', '2026-01; rm'])
def test_invalid_release_disables_lookup_with_one_error(value, log, who, restore_release):
    assert icd_service.configure_release(value) is False
    assert icd_service.SEARCH_ENDPOINT is None
    errors = [l for l in log.getvalue().splitlines() if 'ICD11_RELEASE_VERSION' in l]
    assert len(errors) == 1 and 'CIE-11 lookup disabled' in errors[0]
    # Every later lookup: no request at all, no code, one not_configured line.
    for _ in range(3):
        assert icd_service.lookup_cie11(FAKE_DX) is None
    assert who['token_calls'] == 0 and who['search_calls'] == 0
    assert _lines(log, 'ICD: outcome=') == ['ICD: outcome=not_configured stage=none cluster=0 ms=0'] * 3
    assert len([l for l in log.getvalue().splitlines() if 'ICD11_RELEASE_VERSION' in l]) == 1


def test_invalid_release_leaves_review_without_code(restore_release, who):
    icd_service.configure_release('')
    data = {'evaluacion': {'diagnostico': FAKE_DX, 'codigo_cie11': '9Z99'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': FAKE_DX}


def test_release_is_checked_at_import():
    # configure_release runs at module import with the configured value.
    assert icd_service.SEARCH_ENDPOINT == \
        f'https://id.who.int/icd/release/11/{icd_service.Config.ICD11_RELEASE_VERSION}/mms/search'


def test_missing_credentials_is_not_configured(monkeypatch, log, who):
    monkeypatch.setattr(icd_service.Config, 'ICD_CLIENT_SECRET', '')
    assert icd_service.lookup_cie11(FAKE_DX) is None
    assert who['token_calls'] == 0
    assert _lines(log, 'ICD: outcome=') == ['ICD: outcome=not_configured stage=none cluster=0 ms=0']


# ── #17: token lock and failure cache ───────────────────────────────────

def test_concurrent_lookups_fetch_one_token(who):
    def slow_token():
        time.sleep(0.05)
        return _Resp(200, {'access_token': 'tok', 'expires_in': 3600})
    who['token'] = slow_token
    threads = [threading.Thread(target=icd_service.lookup_cie11, args=(FAKE_DX,)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert who['token_calls'] == 1
    assert who['search_calls'] == 4


def test_token_failure_is_cached_for_60_seconds(who, log, monkeypatch):
    who['token'] = lambda: requests.ConnectionError(f'Max retries exceeded with url: /connect/token')
    clock = {'now': 1_000_000.0}
    monkeypatch.setattr(icd_service.time, 'time', lambda: clock['now'])

    assert icd_service.lookup_cie11(FAKE_DX) is None
    assert icd_service.lookup_cie11(FAKE_DX) is None
    assert who['token_calls'] == 1 and who['search_calls'] == 0
    assert [l.split(' cluster')[0] for l in _lines(log, 'ICD: outcome=')] == ['ICD: outcome=token_failed stage=none'] * 2

    clock['now'] += icd_service.FAILURE_CACHE_SECONDS + 1
    who['token'] = lambda: _Resp(200, {'access_token': 'tok', 'expires_in': 3600})
    assert icd_service.lookup_cie11(FAKE_DX) == {'code': FAKE_CODE, 'title': FAKE_TITLE}
    assert who['token_calls'] == 2 and who['search_calls'] == 1


def test_token_http_error_is_cached_and_logged_without_detail(who, log):
    who['token'] = lambda: _Resp(401, {})
    assert icd_service.lookup_cie11(FAKE_DX) is None
    assert icd_service.lookup_cie11(FAKE_DX) is None
    assert who['token_calls'] == 1
    assert 'ICD: Failed to obtain token: HTTP 401' in log.getvalue()


@pytest.mark.parametrize('exc', [requests.Timeout('Read timed out'), requests.ConnectionError('refused')],
                         ids=['timeout', 'unreachable'])
def test_search_timeout_or_unreachable_is_cached(who, log, exc):
    who['search'] = lambda: exc
    for _ in range(3):
        assert icd_service.lookup_cie11(FAKE_DX) is None
    assert who['search_calls'] == 1
    # The first lookup fails at stage=full; the next two never start (cache).
    assert [l.split(' cluster')[0] for l in _lines(log, 'ICD: outcome=')] == \
        ['ICD: outcome=timeout stage=full'] + ['ICD: outcome=timeout stage=none'] * 2


def test_http_errors_are_not_cached(who, log):
    who['search'] = lambda: _Resp(503, {})
    icd_service.lookup_cie11(FAKE_DX)
    icd_service.lookup_cie11(FAKE_DX)
    assert who['search_calls'] == 2


def test_token_is_reused_until_near_expiry(who, monkeypatch):
    clock = {'now': 2_000_000.0}
    monkeypatch.setattr(icd_service.time, 'time', lambda: clock['now'])
    icd_service.lookup_cie11(FAKE_DX)
    clock['now'] += 3000
    icd_service.lookup_cie11(FAKE_DX)
    assert who['token_calls'] == 1
    clock['now'] += 600   # within 60 s of the 3600 s expiry
    icd_service.lookup_cie11(FAKE_DX)
    assert who['token_calls'] == 2


# ── Per-lookup outcome line ─────────────────────────────────────────────

@pytest.mark.parametrize('search,outcome,cluster', [
    (lambda: _Resp(200, {'destinationEntities': [{'theCode': FAKE_CODE, 'title': FAKE_TITLE, 'score': 1}]}), 'ok', 1),
    (lambda: _Resp(200, {'destinationEntities': [{'theCode': '5A11', 'title': FAKE_TITLE, 'score': 1}]}), 'ok', 0),
    (lambda: _Resp(200, {'destinationEntities': [{'theCode': '1A00&XK8G', 'title': FAKE_TITLE}]}), 'ok', 1),
    (lambda: _Resp(200, {'destinationEntities': []}), 'no_results', 0),
    (lambda: _Resp(200, {}), 'no_results', 0),
    (lambda: _Resp(200, {'destinationEntities': [{'theCode': '', 'title': FAKE_TITLE}]}), 'no_code', 0),
    (lambda: _Resp(404, {}), 'http_404', 0),
    (lambda: _Resp(500, {}), 'http_500', 0),
    (lambda: requests.Timeout(f'timed out: q={FAKE_DX}'), 'timeout', 0),
    (lambda: requests.ConnectionError(f'Max retries exceeded with url: /search?q={FAKE_DX}'), 'timeout', 0),
    (lambda: _Resp(200, exc=ValueError(f'Expecting value: {FAKE_DX}')), 'bad_json', 0),
    (lambda: _Resp(200, ['not', 'a', 'dict']), 'bad_json', 0),
    (lambda: _Resp(200, {'destinationEntities': 'x'}), 'bad_json', 0),
], ids=['ok-cluster', 'ok-stem', 'ok-extension', 'no-results', 'no-key', 'no-code', 'http-404', 'http-500',
        'timeout', 'unreachable', 'bad-json', 'json-list', 'entities-not-list'])
def test_one_outcome_line_per_lookup_without_phi(who, log, search, outcome, cluster):
    who['search'] = search
    icd_service.lookup_cie11(FAKE_DX)
    lines = _lines(log, 'ICD: outcome=')
    # An empty full search moves on to autocode (no term here; 0.8.8: no
    # flexi stage); a hit or a failed request ends the chain at stage=full.
    assert len(lines) == (2 if outcome in ('no_results', 'no_code') else 1), log.getvalue()
    assert all(OUTCOME_RE.match(l) for l in lines), lines
    assert lines[0].startswith(f'ICD: outcome={outcome} stage=full cluster={cluster} ms=')
    _assert_no_phi(log.getvalue())


@pytest.mark.parametrize('dx', ['', '   ', None, 7])
def test_no_diagnosis_outcome(who, log, dx):
    assert icd_service.lookup_cie11(dx) is None
    assert _lines(log, 'ICD: outcome=') == ['ICD: outcome=no_diagnosis stage=none cluster=0 ms=0']
    assert who['token_calls'] == 0


@pytest.mark.parametrize('data', [{'evaluacion': {'impresion_clinica': 'x'}}, {'subjetivo': {}}, {'evaluacion': 'x'}])
def test_extraction_without_diagnosis_still_logs_one_line(who, log, data):
    apply_cie11_lookup(data)
    assert _lines(log, 'ICD: outcome=') == ['ICD: outcome=no_diagnosis stage=none cluster=0 ms=0']


def test_extraction_with_diagnosis_logs_exactly_one_line(who, log):
    data = {'evaluacion': {'diagnostico': FAKE_DX}}
    apply_cie11_lookup(data)
    assert data['evaluacion']['codigo_cie11'] == FAKE_CODE
    assert _lines(log, 'ICD: outcome=') == [l for l in _lines(log, 'ICD: outcome=ok stage=full cluster=1 ms=')]
    assert len(_lines(log, 'ICD: outcome=')) == 1
    _assert_no_phi(log.getvalue())


# ── Per-confirm CIE11 line ──────────────────────────────────────────────

def _set_pending_ev(db, ev):
    row = next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_PENDING_A)
    sd = {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'}, 'evaluacion': ev}
    row['structured_data'] = sd
    row['full_response'] = {**row['full_response'], 'structured_data': sd}


def _confirm(client, ev):
    return _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': SESSION_PENDING_A,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'}, 'evaluacion': ev},
        'create_pdf': False, 'send_email': False,
    })


WHO_EV = {'diagnostico': FAKE_DX, 'codigo_cie11': FAKE_CODE, 'titulo_cie11': FAKE_TITLE, 'cie11_fuente': 'oms'}


@pytest.mark.parametrize('pending,confirmed,fuente,cambio', [
    (WHO_EV, WHO_EV, 'oms', 'kept'),
    (WHO_EV, {'diagnostico': FAKE_DX, 'codigo_cie11': ' 5a24/5a11 '}, 'oms', 'kept'),
    (WHO_EV, {'diagnostico': FAKE_DX, 'codigo_cie11': '5A12'}, 'medico', 'changed'),
    (WHO_EV, {'diagnostico': FAKE_DX, 'codigo_cie11': ''}, 'none', 'cleared'),
    ({'diagnostico': FAKE_DX}, {'diagnostico': FAKE_DX, 'codigo_cie11': '5A12'}, 'medico', 'added'),
    ({'diagnostico': FAKE_DX}, {'diagnostico': FAKE_DX}, 'none', 'absent'),
    ({'diagnostico': FAKE_DX, 'codigo_cie11': '5A12'}, {'codigo_cie11': '5A12'}, 'medico', 'kept'),
], ids=['who-kept', 'who-kept-normalised', 'changed', 'cleared', 'added', 'absent', 'pre-085-kept'])
def test_confirm_logs_one_cie11_line_without_phi(client, db, log, pending, confirmed, fuente, cambio):
    _set_pending_ev(db, pending)
    assert _confirm(client, dict(confirmed)).status_code == 200
    lines = _lines(log, 'CIE11:')
    assert lines == [f'CIE11: session={SESSION_PENDING_A} fuente={fuente} cambio={cambio}']
    assert CONFIRM_RE.match(lines[0])
    _assert_no_phi(log.getvalue())


def test_failed_confirm_logs_no_cie11_line(client, db, log, monkeypatch):
    def boom(body):
        raise OSError('fixture: network down')
    monkeypatch.setattr(app_module, '_sb_upsert_session', boom)
    _set_pending_ev(db, WHO_EV)
    assert _confirm(client, dict(WHO_EV)).status_code == 503
    assert _lines(log, 'CIE11:') == []


def test_cie11_confirm_outcome_tolerates_malformed_shapes():
    assert app_module.cie11_confirm_outcome({}, {}) == ('none', 'absent')
    assert app_module.cie11_confirm_outcome({'evaluacion': 'x'}, None) == ('none', 'absent')
    assert app_module.cie11_confirm_outcome({'evaluacion': {'codigo_cie11': '5A11', 'cie11_fuente': 'gemini'}},
                                            {'evaluacion': ['x']}) == ('none', 'added')
