# backend/tests/test_cie11_lookup_chain.py
# 0.8.7 — the CIE-11 lookup finds codes for descriptive diagnoses.
# Regular WHO search only returns entities containing ALL the query's words,
# so "Lumbalgia por contractura muscular secundaria a esfuerzo físico" found
# nothing. The lookup is now a chain — full → term → flexi → autocode, first
# coded result wins — fed by a short search term Gemini derives from its own
# diagnosis, which a server guard accepts only if every content word is in
# the diagnosis, and which never leaves the extraction.
# Invented data only; WHO and Gemini are mocked, no network.

import io
import json
import logging
import types

import pytest
import requests

import app as app_module
import icd_service
import llm_processor
from config import Config
from llm_processor import LLMProcessor, accept_search_term, apply_cie11_lookup, CIE11_TERM_KEY
from logger import logger
from pdf_generator import PDFGenerator
from tests import tokens
from tests.conftest import DOCTOR_A1, CLINIC_A, SESSION_PENDING_A
from tests.test_route_authorization import _call
from tests.test_roles_and_cedula import _pdf_text

DX = 'Lumbalgia ficticia por contractura muscular secundaria a esfuerzo físico'
TERM = 'Lumbalgia ficticia'
CODE, TITLE = 'ME84.2Z', 'Título ficticio de lumbalgia'
SECRETS = (DX, 'Lumbalgia', 'lumbalgia', 'contractura', TERM, CODE, 'ME84', TITLE, 'q=', 'searchText')

HIT = {'destinationEntities': [{'theCode': CODE, 'title': TITLE, 'score': 0.9}]}
EMPTY = {'destinationEntities': []}


class _Resp:
    def __init__(self, status=200, body=None, exc=None):
        self.status_code, self._body, self._exc = status, body, exc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Error for url: https://id.who.int/x?q={DX}', response=self)

    def json(self):
        if self._exc:
            raise self._exc
        return self._body


def _stage_of(url, params):
    if url.endswith('/autocode'):
        return 'autocode'
    if params.get('useFlexisearch') == 'true':
        return 'flexi'
    return 'term' if params.get('q') != DX else 'full'


@pytest.fixture
def who(monkeypatch):
    """WHO mock keyed by chain stage. Each stage answers what `answers[stage]`
    says (a body dict, a _Resp, or an exception); unset stages answer EMPTY
    (autocode: no code). Every GET is recorded."""
    st = {'answers': {}, 'calls': []}
    monkeypatch.setattr(icd_service.requests, 'post',
                        lambda *a, **k: _Resp(200, {'access_token': 'tok', 'expires_in': 3600}))

    def get(url, params=None, headers=None, timeout=None):
        stage = _stage_of(url, params or {})
        st['calls'].append((stage, dict(params or {})))
        answer = st['answers'].get(stage, {} if stage == 'autocode' else EMPTY)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, _Resp) else _Resp(200, answer)
    monkeypatch.setattr(icd_service.requests, 'get', get)
    return st


@pytest.fixture
def log():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    yield buf
    logger.removeHandler(handler)


def _outcomes(buf):
    """'outcome stage' pairs from the ICD outcome lines, in order."""
    out = []
    for line in buf.getvalue().splitlines():
        if line.startswith('ICD: outcome='):
            parts = dict(p.split('=', 1) for p in line[len('ICD: '):].split())
            out.append(f"{parts['outcome']} {parts['stage']}")
    return out


def _assert_no_phi(buf):
    text = buf.getvalue()
    for secret in SECRETS:
        assert secret not in text, secret


# ── The chain: a hit at each stage ──────────────────────────────────────

def test_hit_at_full_stops_there(who, log):
    who['answers']['full'] = HIT
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': TITLE}
    assert [c[0] for c in who['calls']] == ['full']
    assert _outcomes(log) == ['ok full']


def test_full_empty_then_term_hit(who, log):
    who['answers']['term'] = HIT
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': TITLE}
    assert who['calls'] == [
        ('full', {'q': DX, 'flatResults': 'true', 'highlightingEnabled': 'false'}),
        ('term', {'q': TERM, 'flatResults': 'true', 'highlightingEnabled': 'false'}),
    ]
    assert _outcomes(log) == ['no_results full', 'ok term']
    _assert_no_phi(log)


def test_term_without_code_then_flexi_hit(who, log):
    who['answers']['term'] = {'destinationEntities': [{'title': 'Bloque ficticio', 'score': 1}]}
    who['answers']['flexi'] = HIT
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': TITLE}
    assert [c[0] for c in who['calls']] == ['full', 'term', 'flexi']
    assert who['calls'][2][1] == {'q': DX, 'flatResults': 'true', 'highlightingEnabled': 'false',
                                  'useFlexisearch': 'true'}
    assert _outcomes(log) == ['no_results full', 'no_code term', 'ok flexi']


def test_autocode_hit_at_or_above_threshold(who, log):
    who['answers']['autocode'] = {'theCode': CODE, 'matchScore': Config.CIE11_AUTOCODE_MIN_SCORE,
                                  'matchingText': 'texto ficticio'}
    # /autocode returns no title: the code is kept without one.
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': ''}
    assert [c[0] for c in who['calls']] == ['full', 'term', 'flexi', 'autocode']
    assert who['calls'][3][1] == {'searchText': DX, 'matchThreshold': str(Config.CIE11_AUTOCODE_MIN_SCORE)}
    assert _outcomes(log) == ['no_results full', 'no_results term', 'no_results flexi', 'ok autocode']
    assert f'score={Config.CIE11_AUTOCODE_MIN_SCORE:.2f}' in log.getvalue()
    _assert_no_phi(log)


def test_autocode_cluster_is_a_hit(who, log):
    who['answers']['autocode'] = {'theCode': '5a24/5a11', 'matchScore': 0.95}
    assert icd_service.lookup_cie11(DX) == {'code': '5A24/5A11', 'title': ''}
    assert 'outcome=ok stage=autocode cluster=1' in log.getvalue()


@pytest.mark.parametrize('body,outcome', [
    ({'theCode': CODE, 'matchScore': 0.79}, 'low_score'),
    ({'theCode': CODE, 'matchScore': 0.1}, 'low_score'),
    ({'theCode': CODE}, 'low_score'),
    ({'theCode': CODE, 'matchScore': 'alto'}, 'low_score'),
    ({'theCode': '', 'matchScore': 0.99}, 'no_code'),
    ({'matchScore': 0.99}, 'no_code'),
    ({}, 'no_code'),
], ids=['just-below', 'low', 'no-score', 'bad-score', 'empty-code', 'no-code', 'empty'])
def test_autocode_below_threshold_or_uncoded_is_rejected(who, log, body, outcome):
    who['answers']['autocode'] = body
    assert icd_service.lookup_cie11(DX, TERM) is None
    assert _outcomes(log)[-1] == f'{outcome} autocode'


def test_threshold_is_a_config_constant():
    assert Config.CIE11_AUTOCODE_MIN_SCORE == 0.80


def test_term_equal_to_diagnosis_skips_the_term_stage(who, log):
    assert icd_service.lookup_cie11(DX, f'  {DX.upper()} ') is None
    assert [c[0] for c in who['calls']] == ['full', 'flexi', 'autocode']


def test_no_term_skips_the_term_stage(who, log):
    icd_service.lookup_cie11(DX, None)
    icd_service.lookup_cie11(DX, '   ')
    assert [c[0] for c in who['calls']] == ['full', 'flexi', 'autocode'] * 2


# ── A failed request at any stage ends the chain ────────────────────────

STAGES = ['full', 'term', 'flexi', 'autocode']


@pytest.mark.parametrize('stage', STAGES)
@pytest.mark.parametrize('failure,outcome', [
    (_Resp(500, {}), 'http_500'),
    (_Resp(404, {}), 'http_404'),
    (requests.Timeout('timed out'), 'timeout'),
    (requests.ConnectionError('refused'), 'timeout'),
    (_Resp(200, exc=ValueError('Expecting value')), 'bad_json'),
    (_Resp(200, ['no', 'es', 'un', 'objeto']), 'bad_json'),
], ids=['http500', 'http404', 'timeout', 'unreachable', 'bad-json', 'json-list'])
def test_failure_at_a_stage_stops_the_chain(who, log, stage, failure, outcome):
    who['answers'][stage] = failure
    assert icd_service.lookup_cie11(DX, TERM) is None
    tried = [c[0] for c in who['calls']]
    assert tried == STAGES[:STAGES.index(stage) + 1]
    assert _outcomes(log)[-1] == f'{outcome} {stage}'
    _assert_no_phi(log)


def test_search_body_with_error_flag_is_bad_json(who, log):
    who['answers']['full'] = {'error': True, 'errorMessage': f'ficticio {DX}', 'destinationEntities': []}
    assert icd_service.lookup_cie11(DX, TERM) is None
    assert _outcomes(log) == ['bad_json full']
    _assert_no_phi(log)


def test_timeout_mid_chain_feeds_the_failure_cache(who, log):
    who['answers']['term'] = requests.Timeout('timed out')
    assert icd_service.lookup_cie11(DX, TERM) is None
    calls = len(who['calls'])
    assert icd_service.lookup_cie11(DX, TERM) is None
    assert len(who['calls']) == calls            # skipped: no request at all
    assert _outcomes(log) == ['no_results full', 'timeout term', 'timeout none']


@pytest.mark.parametrize('stage', STAGES)
def test_failure_at_any_stage_does_not_break_extraction(who, stage):
    who['answers'][stage] = requests.ConnectionError('refused')
    data = {'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: TERM}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': DX}


# ── The term guard ──────────────────────────────────────────────────────

@pytest.mark.parametrize('term,diagnostico', [
    ('Lumbalgia', 'Lumbalgia por contractura muscular secundaria a esfuerzo físico'),
    ('LUMBALGIA', 'lumbalgia por contractura'),
    ('Lumbalgía', 'Lumbalgia por contractura'),             # accents ignored
    ('Diabetes mellitus tipo 2', 'Diabetes mellitus tipo 2 con control glucémico subóptimo'),
    ('Diabetes mellitus tipo 2', 'diabetes MELLITUS tipo 2 descompensada'),
    ('Contractura de la musculatura', 'Contractura muscular de la musculatura lumbar'),  # stopwords ignored
    ('Esguince de tobillo', 'Esguince de tobillo izquierdo'),
    ('Hipertensión arterial', 'Hipertensión arterial sistémica'),
    ('Lumbalgia, contractura', 'Lumbalgia por contractura'),  # punctuation ignored
])
def test_guard_accepts_a_narrowing_term(term, diagnostico):
    assert accept_search_term(term, diagnostico)


@pytest.mark.parametrize('term,diagnostico', [
    ('Lumbalgia aguda', 'Lumbalgia por contractura'),            # adds severity
    ('Hernia discal', 'Lumbalgia por contractura'),              # a different condition
    ('Diabetes mellitus tipo 1', 'Diabetes mellitus tipo 2'),    # changes the type
    ('Diabetes', 'Prediabetes'),                                  # whole words only
    ('Infarto agudo de miocardio', 'Dolor torácico'),
    ('5A11', 'Diabetes mellitus tipo 2'),                         # a code
    ('de la', 'Lumbalgia de la región lumbar'),                   # nothing but stopwords
    ('', 'Lumbalgia'), ('   ', 'Lumbalgia'), (None, 'Lumbalgia'), (['Lumbalgia'], 'Lumbalgia'), (5, 'Lumbalgia'),
])
def test_guard_rejects_any_word_not_in_the_diagnosis(term, diagnostico):
    assert not accept_search_term(term, diagnostico)


def test_rejected_term_is_logged_and_chain_continues_without_it(who, log):
    who['answers']['flexi'] = HIT
    data = {'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: 'Hernia discal ficticia'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': DX, 'codigo_cie11': CODE, 'titulo_cie11': TITLE,
                                  'cie11_fuente': 'oms'}
    assert [c[0] for c in who['calls']] == ['full', 'flexi']
    assert all(c[1].get('q') != 'Hernia discal ficticia' for c in who['calls'])
    assert _outcomes(log) == ['term_rejected term', 'no_results full', 'ok flexi']
    assert 'Hernia' not in log.getvalue()
    _assert_no_phi(log)


def test_accepted_term_reaches_the_term_stage(who, log):
    who['answers']['term'] = HIT
    data = {'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: TERM}}
    apply_cie11_lookup(data)
    assert data['evaluacion']['codigo_cie11'] == CODE and data['evaluacion']['cie11_fuente'] == 'oms'
    assert _outcomes(log) == ['no_results full', 'ok term']


# ── The term never leaves the extraction ────────────────────────────────

def _fake_gemini(sd):
    return types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=lambda **k: types.SimpleNamespace(text=json.dumps(sd))))


GEMINI_SD = {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
             'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: TERM, 'codigo_cie11': '9Z99'}}


@pytest.mark.parametrize('answers', [{'term': HIT}, {}, {'full': requests.Timeout('x')}],
                         ids=['found', 'nothing', 'failed'])
def test_term_is_removed_from_the_extraction(who, answers):
    who['answers'].update(answers)
    p = LLMProcessor.__new__(LLMProcessor)
    p.model_id = 'fake'
    p.client = _fake_gemini(GEMINI_SD)
    sd = p.extract_structured_data('transcripción ficticia')
    assert CIE11_TERM_KEY not in sd['evaluacion']
    assert CIE11_TERM_KEY not in json.dumps(sd)


def test_term_removed_even_without_a_diagnosis(who):
    data = {'evaluacion': {CIE11_TERM_KEY: TERM, 'impresion_clinica': 'x'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'impresion_clinica': 'x'}
    assert who['calls'] == []


def test_term_never_reaches_the_pending_note_the_review_or_the_confirmed_note(client, db, who, monkeypatch, tmp_path):
    """End to end through _run_job (real extraction with a fake Gemini, real
    lookup with a fake WHO) and confirm-and-generate (real PDF)."""
    who['answers']['term'] = HIT
    monkeypatch.setattr(app_module.llm_processor, 'client', _fake_gemini(GEMINI_SD))
    monkeypatch.setattr(app_module.transcription_service, 'transcribe_audio', lambda *a, **k: {
        'text': 'transcripción ficticia suficientemente larga', 'utterances': [],
        'confidence': 0.9, 'audio_duration': 60000, 'words': 5})
    db.add('trabajos', {'job_id': 'job-fixture-term', 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
                        'status': 'queued'})
    audio = tmp_path / 'fixture.webm'
    audio.write_bytes(b'fake-audio')
    app_module._run_job('job-fixture-term', str(audio), {
        'print_raw': False, 'speakers_expected': 2, 'consultation_timestamp': '2026-11-06 10:00',
        'local_timestamp': '2026-11-06 10:00', 'consent_given': True,
        'consent_timestamp': '2026-11-06T10:00:00'}, DOCTOR_A1, CLINIC_A, 'Dr. Fixture')

    job = next(t for t in db.tables['trabajos'] if t['job_id'] == 'job-fixture-term')
    assert job['status'] == 'done'
    session_id = job['session_id']
    pending = next(r for r in db.tables['sesiones'] if r['session_id'] == session_id)
    for blob in (job['structured_data'], pending['structured_data'], pending['full_response']):
        assert CIE11_TERM_KEY not in json.dumps(blob, ensure_ascii=False)
    assert pending['structured_data']['evaluacion']['codigo_cie11'] == CODE

    # What the review screen gets (job-status) has no term either.
    resp = _call(client, 'GET', '/api/job-status/job-fixture-term', token=tokens.mint(DOCTOR_A1))
    assert resp.status_code == 200 and CIE11_TERM_KEY not in resp.get_data(as_text=True)

    # Even if a client sent it back at confirm, it's not saved or printed.
    monkeypatch.setattr(app_module, 'pdf_generator', PDFGenerator())
    sent = []
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: sent.append(k['pdf_bytes']) or True)
    ev = dict(pending['structured_data']['evaluacion'], **{CIE11_TERM_KEY: 'Término ficticio reenviado'})
    resp = _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': session_id,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'}, 'evaluacion': ev},
        'create_pdf': True, 'send_email': True,
    })
    assert resp.status_code == 200
    confirmed = next(r for r in db.tables['sesiones'] if r['session_id'] == session_id)
    assert confirmed['status'] == 'confirmed'
    assert CIE11_TERM_KEY not in json.dumps(confirmed['structured_data'], ensure_ascii=False)
    assert CIE11_TERM_KEY not in json.dumps(confirmed['full_response'], ensure_ascii=False)
    text = _pdf_text(sent[0])
    assert 'Término ficticio reenviado' not in text and 'termino' not in text.lower()
    assert f'Código CIE-11: {CODE}' in text


# ── Prompt ───────────────────────────────────────────────────────────────

def test_prompt_asks_for_the_term_with_both_examples():
    prompt = LLMProcessor.create_extraction_prompt(LLMProcessor.__new__(LLMProcessor), 'texto ficticio')
    assert '9. termino_busqueda_cie11:' in prompt
    assert 'sin causa, gravedad, estado de control, lateralidad ni otros calificativos' in prompt
    assert 'nunca agregues una condición y nunca escribas un código' in prompt
    assert '«Lumbalgia por contractura muscular secundaria a esfuerzo físico» → «Lumbalgia»' in prompt
    assert '«Diabetes mellitus tipo 2 con control glucémico subóptimo» → «Diabetes mellitus tipo 2»' in prompt
    assert '"termino_busqueda_cie11": "string' in prompt
    # The template still never asks for a code.
    assert '"codigo_cie11"' not in prompt and '"titulo_cie11"' not in prompt
