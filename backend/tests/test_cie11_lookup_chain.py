# backend/tests/test_cie11_lookup_chain.py
# 0.8.7 — the CIE-11 lookup finds codes for descriptive diagnoses.
# Regular WHO search only returns entities containing ALL the query's words,
# so "Lumbalgia por contractura muscular secundaria a esfuerzo físico" found
# nothing. The lookup is now a chain — full → term → autocode →
# autocode_term (0.8.8: the flexible-search stage is gone, every result must
# pass the relevance check; 0.8.9: autocode_term, and matched property
# values count as relevance), first coded, relevant result wins — fed by a short search term Gemini derives from its own
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
        return 'autocode' if params.get('searchText') == DX else 'autocode_term'
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


def test_term_without_code_then_autocode_hit(who, log):
    who['answers']['term'] = {'destinationEntities': [{'title': 'Bloque ficticio', 'score': 1}]}
    who['answers']['autocode'] = {'theCode': CODE, 'matchScore': 0.9, 'matchingText': 'Lumbalgia'}
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': ''}
    assert [c[0] for c in who['calls']] == ['full', 'term', 'autocode']
    assert _outcomes(log) == ['no_results full', 'no_code term', 'ok autocode']


def test_flexible_search_is_never_requested(who, log):
    # Every stage empty: the whole chain runs, and no request asks for
    # useFlexisearch (0.8.8 — it produced an unrelated code in production).
    assert icd_service.lookup_cie11(DX, TERM) is None
    assert [c[0] for c in who['calls']] == ['full', 'term', 'autocode', 'autocode_term']
    assert all('useFlexisearch' not in params for _, params in who['calls'])
    assert 'stage=flexi' not in log.getvalue()


def test_autocode_hit_at_or_above_threshold(who, log):
    who['answers']['autocode'] = {'theCode': CODE, 'matchScore': Config.CIE11_AUTOCODE_MIN_SCORE,
                                  'matchingText': 'Lumbalgia'}
    # /autocode returns no title: the code is kept without one.
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': ''}
    assert [c[0] for c in who['calls']] == ['full', 'term', 'autocode']
    assert who['calls'][2][1] == {'searchText': DX, 'matchThreshold': str(Config.CIE11_AUTOCODE_MIN_SCORE)}
    assert _outcomes(log) == ['no_results full', 'no_results term', 'ok autocode']
    assert f'score={Config.CIE11_AUTOCODE_MIN_SCORE:.2f}' in log.getvalue()
    _assert_no_phi(log)


def test_autocode_cluster_is_a_hit(who, log):
    who['answers']['autocode'] = {'theCode': '5a24/5a11', 'matchScore': 0.95, 'matchingText': 'Contractura muscular'}
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
    assert icd_service.lookup_cie11(DX) is None    # no term: autocode is the last stage
    assert _outcomes(log)[-1] == f'{outcome} autocode'


def test_threshold_is_a_config_constant():
    assert Config.CIE11_AUTOCODE_MIN_SCORE == 0.80


def test_term_equal_to_diagnosis_skips_the_term_stage(who, log):
    assert icd_service.lookup_cie11(DX, f'  {DX.upper()} ') is None
    assert [c[0] for c in who['calls']] == ['full', 'autocode']


def test_no_term_skips_the_term_stage(who, log):
    icd_service.lookup_cie11(DX, None)
    icd_service.lookup_cie11(DX, '   ')
    assert [c[0] for c in who['calls']] == ['full', 'autocode'] * 2


# ── A failed request at any stage ends the chain ────────────────────────

STAGES = ['full', 'term', 'autocode', 'autocode_term']


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
    who['answers']['autocode'] = {'theCode': CODE, 'matchScore': 0.9, 'matchingText': 'Lumbalgia'}
    data = {'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: 'Hernia discal ficticia'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': DX, 'codigo_cie11': CODE, 'cie11_fuente': 'oms'}
    assert [c[0] for c in who['calls']] == ['full', 'autocode']
    assert all(c[1].get('q') != 'Hernia discal ficticia' for c in who['calls'])
    assert _outcomes(log) == ['term_rejected term', 'no_results full', 'ok autocode']
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


# ── 0.8.8: relevance check ──────────────────────────────────────────────

def test_production_case_unrelated_flexible_style_result_gives_no_code(who, log, monkeypatch):
    """0.8.7 production: "Control glucémico inadecuado" (term rejected, full
    search empty) → flexible search → MB24.64 "Afecto inapropiado". The
    flexi stage is gone; and even if any stage returned that entity it is
    irrelevant to the searched text."""
    dx = 'Control glucémico inadecuado'
    unrelated = {'destinationEntities': [{'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 0.7}]}

    def get(url, params=None, headers=None, timeout=None):
        params = params or {}
        who['calls'].append((_stage_of(url, params), dict(params)))
        if url.endswith('/autocode'):
            return _Resp(200, {'theCode': 'MB24.64', 'matchScore': 0.97, 'matchingText': 'Afecto inapropiado'})
        if params.get('useFlexisearch') == 'true':
            return _Resp(200, unrelated)
        return _Resp(200, EMPTY)
    monkeypatch.setattr(icd_service.requests, 'get', get)   # the fixture's token mock stays

    data = {'evaluacion': {'diagnostico': dx, CIE11_TERM_KEY: 'Diabetes mellitus'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': dx}                 # no code, no title, no fuente
    assert all('useFlexisearch' not in p for _, p in who['calls'])
    assert _outcomes(log) == ['term_rejected term', 'no_results full', 'irrelevant autocode']
    text = log.getvalue()
    for secret in (dx, 'glucémico', 'Control', 'MB24', 'Afecto', 'inapropiado', 'Diabetes'):
        assert secret not in text, secret


@pytest.mark.parametrize('stage', ['full', 'term'])
def test_search_result_unrelated_to_the_searched_text_is_irrelevant(who, log, stage):
    who['answers'][stage] = {'destinationEntities': [{'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 1}]}
    assert icd_service.lookup_cie11(DX, TERM) is None
    assert f'irrelevant {stage}' in _outcomes(log)
    # irrelevant is "no result": the chain goes on to the last stage.
    assert who['calls'][-1][0] == 'autocode_term'
    _assert_no_phi(log)


def test_irrelevant_full_result_then_term_hit(who, log):
    who['answers']['full'] = {'destinationEntities': [{'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 1}]}
    who['answers']['term'] = HIT
    assert icd_service.lookup_cie11(DX, TERM) == {'code': CODE, 'title': TITLE}
    assert _outcomes(log) == ['irrelevant full', 'ok term']


def test_relevance_is_checked_against_the_term_at_the_term_stage(who, log):
    # TITLE shares "lumbalgia" with TERM; a title sharing a word only with
    # the long diagnosis ("contractura") is irrelevant to the term search.
    who['answers']['term'] = {'destinationEntities': [{'theCode': 'FB30', 'title': 'Contractura ficticia', 'score': 1}]}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') is None
    assert 'irrelevant term' in _outcomes(log)


def test_top_irrelevant_entity_is_not_replaced_by_a_lower_relevant_one(who, log):
    who['answers']['full'] = {'destinationEntities': [
        {'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 0.9},
        {'theCode': CODE, 'title': TITLE, 'score': 0.2}]}
    assert icd_service.lookup_cie11(DX) is None
    assert _outcomes(log)[0] == 'irrelevant full'


@pytest.mark.parametrize('matching_text', ['Afecto inapropiado', '', None, 'de la por', 'Lumbalgias'],
                         ids=['unrelated', 'empty', 'missing', 'stopwords-only', 'not-a-whole-word'])
def test_autocode_above_threshold_but_irrelevant_is_rejected(who, log, matching_text):
    body = {'theCode': CODE, 'matchScore': 0.99}
    if matching_text is not None:
        body['matchingText'] = matching_text
    who['answers']['autocode'] = body
    assert icd_service.lookup_cie11(DX) is None    # no term: autocode is the last stage
    assert _outcomes(log)[-1] == 'irrelevant autocode'
    assert 'score=0.99' in log.getvalue()
    _assert_no_phi(log)


def test_autocode_below_threshold_is_low_score_before_relevance(who, log):
    who['answers']['autocode'] = {'theCode': CODE, 'matchScore': 0.5, 'matchingText': 'Afecto inapropiado'}
    assert icd_service.lookup_cie11(DX) is None
    assert _outcomes(log)[-1] == 'low_score autocode'


@pytest.mark.parametrize('matched,searched,relevant', [
    ('Lumbalgia', 'Lumbalgia por contractura', True),
    ('LUMBALGÍA, sin especificación', 'lumbalgia', True),
    ('Diabetes mellitus tipo 2', 'Diabetes mellitus tipo 2 descompensada', True),
    ('Afecto inapropiado', 'Control glucémico inadecuado', False),
    ('Prediabetes', 'Diabetes mellitus', False),            # whole words only
    ('Dolor de la espalda', 'de la por con', False),        # stopwords never count
    ('', 'Lumbalgia', False), (None, 'Lumbalgia', False), ('Lumbalgia', None, False), (5, 'Lumbalgia', False),
])
def test_is_relevant(matched, searched, relevant):
    assert icd_service.is_relevant(matched, searched) is relevant


def test_term_guard_and_relevance_share_one_normaliser():
    assert llm_processor.content_words is icd_service.content_words
    assert icd_service.content_words('Diabetes de tipo 2, con Lumbalgía') == {'diabetes', 'tipo', '2', 'lumbalgia'}


# ── 0.8.9: synonyms through matchingPVs, and autocode on the term ───────
# Production (Dialogo 6): term "Lumbalgia" → ME84.2Z "Dolor lumbar, sin
# especificación" was rejected as irrelevant: the title shares no word with
# "Lumbalgia", but WHO matched it through an index term. Each search entity
# lists what matched in matchingPVs ("List of property values that match the
# search string"), each with a `label` (ISimpleEntity / ISimplePropertyValue,
# https://id.who.int/swagger/v2/swagger.json).

LUMBAR = {'theCode': 'ME84.2Z', 'title': 'Dolor lumbar, sin especificación', 'score': 0.8}


def _with_pvs(entity, *labels, **extra):
    return {**entity, 'matchingPVs': [{'propertyId': 'Synonym', 'label': l, 'score': 0.8, 'important': True,
                                       'propertyValueType': 0, **extra} for l in labels]}


def test_term_matched_through_a_property_value_is_accepted(who, log):
    who['answers']['term'] = {'destinationEntities': [_with_pvs(LUMBAR, 'Lumbalgia')]}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') == {'code': 'ME84.2Z', 'title': 'Dolor lumbar, sin especificación'}
    assert _outcomes(log) == ['no_results full', 'ok term']
    _assert_no_phi(log)
    assert 'Dolor' not in log.getvalue()


def test_highlighted_property_value_label_still_counts(who, log):
    # The spec says labels carry highlight markup (<em class="found">…</em>).
    who['answers']['term'] = {'destinationEntities': [_with_pvs(LUMBAR, '<em class="found">Lumbalgia</em>')]}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia')['code'] == 'ME84.2Z'


@pytest.mark.parametrize('pvs', [None, [], 'Lumbalgia', [{'label': 'Dolor de espalda baja'}], [{'label': None}],
                                 [{'propertyId': 'Synonym'}], ['Lumbalgia'], [{'label': 'Lumbalgias'}]],
                         ids=['missing', 'empty', 'not-a-list', 'unrelated-label', 'null-label', 'no-label',
                              'item-not-dict', 'not-a-whole-word'])
def test_same_entity_without_a_matching_property_is_rejected(who, log, pvs):
    entity = dict(LUMBAR)
    if pvs is not None:
        entity['matchingPVs'] = pvs
    who['answers']['term'] = {'destinationEntities': [entity]}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') is None
    assert 'irrelevant term' in _outcomes(log)


def test_unrelated_entity_with_unrelated_property_values_is_still_rejected(who, log):
    dx = 'Control glucémico inadecuado'
    # The mock keys stages by the fixture DX, so this search is filed as 'term'.
    who['answers']['term'] = {'destinationEntities': [
        _with_pvs({'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 0.9},
                  'Afecto inapropiado', 'Afectividad <em class="found">inadecuada</em> ficticia')]}
    assert icd_service.lookup_cie11(dx) is None
    assert _outcomes(log)[0] == 'irrelevant full'
    for secret in (dx, 'glucémico', 'Afecto', 'MB24'):
        assert secret not in log.getvalue()


def test_property_values_of_a_lower_entity_never_promote_it(who, log):
    who['answers']['term'] = {'destinationEntities': [
        {'theCode': 'MB24.64', 'title': 'Afecto inapropiado', 'score': 0.9, 'matchingPVs': []},
        _with_pvs(LUMBAR, 'Lumbalgia')]}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') is None
    assert 'irrelevant term' in _outcomes(log)


def test_autocode_term_hit_above_threshold_is_accepted(who, log):
    who['answers']['autocode_term'] = {'theCode': 'ME84.2Z', 'matchScore': 0.92, 'matchingText': 'Lumbalgia'}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') == {'code': 'ME84.2Z', 'title': ''}
    assert [c[0] for c in who['calls']] == ['full', 'term', 'autocode', 'autocode_term']
    assert who['calls'][3][1] == {'searchText': 'Lumbalgia', 'matchThreshold': str(Config.CIE11_AUTOCODE_MIN_SCORE)}
    assert _outcomes(log) == ['no_results full', 'no_results term', 'no_code autocode', 'ok autocode_term']
    assert 'score=0.92' in log.getvalue()
    _assert_no_phi(log)


@pytest.mark.parametrize('body,outcome', [
    ({'theCode': 'ME84.2Z', 'matchScore': 0.79, 'matchingText': 'Lumbalgia'}, 'low_score'),
    ({'theCode': 'ME84.2Z', 'matchingText': 'Lumbalgia'}, 'low_score'),
    ({'theCode': '', 'matchScore': 0.95, 'matchingText': 'Lumbalgia'}, 'no_code'),
    ({'theCode': 'MB24.64', 'matchScore': 0.99, 'matchingText': 'Afecto inapropiado'}, 'irrelevant'),
], ids=['below-threshold', 'no-score', 'no-code', 'irrelevant'])
def test_autocode_term_rejections(who, log, body, outcome):
    who['answers']['autocode_term'] = body
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') is None
    assert _outcomes(log)[-1] == f'{outcome} autocode_term'


def test_autocode_term_relevance_is_checked_against_the_term(who, log):
    # "Contractura" is in the diagnosis but not in the term searched.
    who['answers']['autocode_term'] = {'theCode': 'FB30', 'matchScore': 0.95, 'matchingText': 'Contractura ficticia'}
    assert icd_service.lookup_cie11(DX, 'Lumbalgia') is None
    assert _outcomes(log)[-1] == 'irrelevant autocode_term'


def test_no_autocode_term_without_an_accepted_term(who, log):
    icd_service.lookup_cie11(DX)
    icd_service.lookup_cie11(DX, f' {DX} ')            # same as the diagnosis
    data = {'evaluacion': {'diagnostico': DX, CIE11_TERM_KEY: 'Hernia discal ficticia'}}   # rejected
    apply_cie11_lookup(data)
    assert 'autocode_term' not in [c[0] for c in who['calls']]
    assert all(p.get('searchText') in (None, DX) for _, p in who['calls'])


def test_production_dialogo_6_now_gets_the_code(who, log, monkeypatch):
    """The production sequence, end to end through apply_cie11_lookup: full
    search empty, term search returns ME84.2Z by synonym."""
    dx = 'Lumbalgia por contractura muscular secundaria a esfuerzo físico'

    def get(url, params=None, headers=None, timeout=None):
        params = params or {}
        who['calls'].append(('x', dict(params)))
        if params.get('q') == 'Lumbalgia':
            return _Resp(200, {'destinationEntities': [_with_pvs(LUMBAR, 'Lumbalgia')]})
        return _Resp(200, EMPTY if 'q' in params else {'theCode': '', 'matchScore': 0})
    monkeypatch.setattr(icd_service.requests, 'get', get)   # the fixture's token mock stays
    data = {'evaluacion': {'diagnostico': dx, CIE11_TERM_KEY: 'Lumbalgia'}}
    apply_cie11_lookup(data)
    assert data['evaluacion'] == {'diagnostico': dx, 'codigo_cie11': 'ME84.2Z',
                                  'titulo_cie11': 'Dolor lumbar, sin especificación', 'cie11_fuente': 'oms'}
    assert _outcomes(log) == ['no_results full', 'ok term']
    for secret in (dx, 'Lumbalgia', 'ME84', 'Dolor lumbar'):
        assert secret not in log.getvalue()


@pytest.mark.parametrize('entity,searched,relevant', [
    (LUMBAR, 'Lumbalgia', False),
    (_with_pvs(LUMBAR, 'Lumbalgia'), 'Lumbalgia', True),
    (_with_pvs(LUMBAR, 'Lumbalgía, inespecífica'), 'lumbalgia', True),
    (_with_pvs(LUMBAR, 'Dolor de espalda'), 'Lumbalgia', False),
    (LUMBAR, 'Dolor lumbar', True),                      # the title alone still counts
    (_with_pvs({'theCode': 'X', 'title': 'Afecto inapropiado'}, 'Afecto'), 'Control glucémico inadecuado', False),
])
def test_is_relevant_entity(entity, searched, relevant):
    title = icd_service._strip_tags(entity.get('title'))
    assert icd_service.is_relevant_entity(entity, title, searched) is relevant
