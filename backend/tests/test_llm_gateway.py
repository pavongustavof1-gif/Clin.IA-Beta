# backend/tests/test_llm_gateway.py
# #81 (0.9.0): the LLM_BACKEND switch and the AssemblyAI LLM Gateway
# backend (llm_gateway.py). Offline: requests.post is replaced by a fake;
# the WHO lookup is stubbed. Every transcript/note below is synthetic.

import io
import json
import logging

import pytest
import requests

import llm_gateway
import llm_processor as llm_module
from config import Config
from extraction_core import create_extraction_prompt
from logger import logger

API_KEY = 'fake-assemblyai-key-for-gateway-tests'
# Distinctive clinical strings: none may ever appear in a log line.
TRANSCRIPT = 'Paciente ficticio refiere ardor al orinar desde hace dos días (FIXTURE-SINTOMA).'
UTTERANCES = [{'speaker': 'A', 'text': '¿Qué le trae hoy? (FIXTURE-PREGUNTA)'},
              {'speaker': 'B', 'text': TRANSCRIPT}]
NOTE = {
    'informacion_paciente': {'nombre_del_paciente': 'Paciente Ficticio FIXTURE-NOMBRE'},
    'subjetivo': {'motivo_de_consulta': 'Ardor al orinar FIXTURE-MOTIVO'},
    'evaluacion': {'diagnostico': 'Cistitis FIXTURE-DX', 'termino_busqueda_cie11': 'Cistitis'},
    'plan': {'medicamentos': [{'nombre': 'Nitrofurantoína', 'dosis': '100 mg FIXTURE-DOSIS'}]},
}
CLINICAL = ['FIXTURE-SINTOMA', 'FIXTURE-PREGUNTA', 'FIXTURE-NOMBRE', 'FIXTURE-MOTIVO', 'FIXTURE-DX',
            'FIXTURE-DOSIS', 'Nitrofurantoína', 'ardor']


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


def ok_body(content=None, finish='end_turn', request_id='req-fixture-1'):
    return {'request_id': request_id,
            'choices': [{'message': {'role': 'assistant',
                                     'content': json.dumps(NOTE, ensure_ascii=False) if content is None else content},
                         'finish_reason': finish}],
            'usage': {'input_tokens': 4099, 'output_tokens': 1200, 'total_tokens': 5299,
                      'completion_tokens_details': {'reasoning_tokens': 300}}}


class FakePost:
    """Replies with the given responses in order (the last one repeats)."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
        r = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def log():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    yield buf
    logger.removeHandler(handler)


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(llm_gateway.time, 'sleep', lambda s: waits.append(s))
    return waits


@pytest.fixture
def gateway_processor(monkeypatch, no_sleep):
    """An LLMProcessor with LLM_BACKEND=gateway, the key set and the WHO
    lookup stubbed (returns a code for any diagnosis)."""
    monkeypatch.setattr(Config, 'LLM_BACKEND', 'gateway')
    monkeypatch.setattr(Config, 'ASSEMBLYAI_API_KEY', API_KEY)
    monkeypatch.setattr(llm_module, 'lookup_cie11', lambda dx, term=None: {'code': 'GC00.3', 'title': 'Cistitis'})
    return llm_module.LLMProcessor()


def _use(monkeypatch, post):
    monkeypatch.setattr(llm_gateway.requests, 'post', post)
    return post


def _assert_no_clinical_text(text):
    for s in CLINICAL + [API_KEY]:
        assert s not in text, s


# ── request ──────────────────────────────────────────────────────────────

def test_request_body_and_headers(monkeypatch, gateway_processor):
    post = _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    gateway_processor.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    (call,) = post.calls
    assert call['url'] == 'https://llm-gateway.assemblyai.com/v1/chat/completions'
    assert call['headers']['Authorization'] == API_KEY                    # no "Bearer"
    assert not call['headers']['Authorization'].startswith('Bearer')
    assert call['timeout'] == 120
    body = call['json']
    assert body == {
        'model': 'claude-haiku-5-5',
        'messages': [{'role': 'user', 'content': create_extraction_prompt(TRANSCRIPT, UTTERANCES)}],
        'max_tokens': 8192,
        'reasoning_effort': 'low',
    }
    for absent in ('temperature', 'response_format', 'model_region'):
        assert absent not in body


def test_prompt_is_production_prompt_byte_for_byte(monkeypatch, gateway_processor):
    post = _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    gateway_processor.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    assert post.calls[0]['json']['messages'][0]['content'] == \
        gateway_processor.create_extraction_prompt(TRANSCRIPT, UTTERANCES)


# ── result: parse, validate, CIE-11 ─────────────────────────────────────

def test_success_parses_validates_and_runs_the_cie11_lookup(monkeypatch, gateway_processor):
    fenced = '```json\n' + json.dumps(NOTE, ensure_ascii=False) + '\n```'
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body(content=fenced))))
    data = gateway_processor.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    ev = data['evaluacion']
    assert ev['codigo_cie11'] == 'GC00.3' and ev['cie11_fuente'] == 'oms'
    assert 'termino_busqueda_cie11' not in ev                               # popped by the lookup step
    assert data['metadata'] == {}                                           # validator normalisation
    assert data['plan']['medicamentos'][0]['nombre'] == 'Nitrofurantoína'


def test_model_supplied_cie11_code_is_replaced_by_the_lookup(monkeypatch, gateway_processor):
    note = {**NOTE, 'evaluacion': {'diagnostico': 'Cistitis', 'codigo_cie11': 'ZZ99', 'titulo_cie11': 'x'}}
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body(content=json.dumps(note)))))
    ev = gateway_processor.extract_structured_data(TRANSCRIPT)['evaluacion']
    assert ev['codigo_cie11'] == 'GC00.3'


@pytest.mark.parametrize('finish', ['max_tokens', 'length'])
def test_truncated_output_is_a_failure(monkeypatch, gateway_processor, finish):
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body(finish=finish))))
    with pytest.raises(llm_module.LLMProcessingError, match='truncated'):
        gateway_processor.extract_structured_data(TRANSCRIPT)


@pytest.mark.parametrize('content, reason', [
    ('{"subjetivo": {"motivo', 'invalid JSON'),
    ('', 'empty response'),
    ('[1, 2]', 'not an object'),
    ('{"informacion_paciente": {}}', 'validation'),                       # no S/O/A/P content
])
def test_unusable_output_is_a_failure(monkeypatch, gateway_processor, content, reason):
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body(content=content))))
    with pytest.raises(llm_module.LLMProcessingError, match=reason):
        gateway_processor.extract_structured_data(TRANSCRIPT)


# ── retries ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('status', [429, 500, 502, 503, 529])
def test_429_and_5xx_retried_up_to_twice(monkeypatch, gateway_processor, no_sleep, status):
    post = _use(monkeypatch, FakePost(FakeResponse(status, {'error': 'busy'}), FakeResponse(status, {'error': 'busy'}),
                                      FakeResponse(200, ok_body())))
    data = gateway_processor.extract_structured_data(TRANSCRIPT)
    assert len(post.calls) == 3 and len(no_sleep) == 2 and data['evaluacion']['cie11_fuente'] == 'oms'
    assert all(1 <= w <= 6 for w in no_sleep)                               # backoff 2-3 s, 4-5 s


def test_gives_up_after_two_retries(monkeypatch, gateway_processor, no_sleep):
    post = _use(monkeypatch, FakePost(FakeResponse(503, {'error': 'busy'})))
    with pytest.raises(llm_module.LLMProcessingError, match='http_503'):
        gateway_processor.extract_structured_data(TRANSCRIPT)
    assert len(post.calls) == 3 and len(no_sleep) == 2


def test_retry_after_is_honoured_and_capped(monkeypatch, gateway_processor, no_sleep):
    _use(monkeypatch, FakePost(FakeResponse(429, {}, {'Retry-After': '7'}), FakeResponse(429, {}, {'Retry-After': '900'}),
                               FakeResponse(200, ok_body())))
    gateway_processor.extract_structured_data(TRANSCRIPT)
    assert no_sleep == [7.0, 30]


@pytest.mark.parametrize('status', [400, 401, 404, 422])
def test_other_errors_not_retried(monkeypatch, gateway_processor, no_sleep, status):
    post = _use(monkeypatch, FakePost(FakeResponse(status, {'error': {'message': 'echo FIXTURE-SINTOMA'}})))
    with pytest.raises(llm_module.LLMProcessingError, match=f'http_{status}'):
        gateway_processor.extract_structured_data(TRANSCRIPT)
    assert len(post.calls) == 1 and no_sleep == []


@pytest.mark.parametrize('exc', [requests.Timeout('read timed out'), requests.ConnectionError('refused')])
def test_timeout_and_connection_errors_fail_without_retry(monkeypatch, gateway_processor, no_sleep, exc):
    post = _use(monkeypatch, FakePost(exc))
    with pytest.raises(llm_module.LLMProcessingError, match=type(exc).__name__):
        gateway_processor.extract_structured_data(TRANSCRIPT)
    assert len(post.calls) == 1 and no_sleep == []


def test_missing_key_fails_before_any_call(monkeypatch, gateway_processor):
    post = _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    monkeypatch.setattr(Config, 'ASSEMBLYAI_API_KEY', '')
    with pytest.raises(llm_module.LLMProcessingError, match='ASSEMBLYAI_API_KEY'):
        gateway_processor.extract_structured_data(TRANSCRIPT)
    assert post.calls == []


# ── logging ──────────────────────────────────────────────────────────────

def test_one_log_line_per_call_with_ids_and_counts_only(monkeypatch, gateway_processor, log):
    _use(monkeypatch, FakePost(FakeResponse(503, {'error': 'busy'}), FakeResponse(200, ok_body())))
    gateway_processor.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    lines = [l for l in log.getvalue().splitlines() if l.startswith('LLM: backend=gateway model=claude-haiku-5-5 request_id')]
    assert len(lines) == 2
    assert lines[0].startswith('LLM: backend=gateway model=claude-haiku-5-5 request_id=- in=- out=- reasoning=- latency=')
    assert 'status=503' in lines[0]
    assert lines[1].startswith('LLM: backend=gateway model=claude-haiku-5-5 request_id=req-fixture-1 '
                               'in=4099 out=1200 reasoning=300 latency=')
    assert lines[1].endswith('status=200 finish=end_turn')
    _assert_no_clinical_text(log.getvalue())


@pytest.mark.parametrize('response', [
    FakeResponse(200, ok_body(content='{"subjetivo": "FIXTURE-SINTOMA sin cerrar')),
    FakeResponse(200, ok_body(finish='max_tokens')),
    FakeResponse(400, {'error': {'message': 'prompt echo: FIXTURE-PREGUNTA FIXTURE-SINTOMA'}}),
    FakeResponse(200, ok_body(content=json.dumps({'informacion_paciente': {'nombre_del_paciente': 'FIXTURE-NOMBRE'}}))),
])
def test_failures_log_no_clinical_text(monkeypatch, gateway_processor, log, response):
    _use(monkeypatch, FakePost(response))
    with pytest.raises(llm_module.LLMProcessingError) as excinfo:
        gateway_processor.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    _assert_no_clinical_text(log.getvalue())
    _assert_no_clinical_text(str(excinfo.value))                           # _run_job logs the exception


def test_usage_parsing():
    assert llm_gateway.usage_of({'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}) == \
        {'in': 1, 'out': 2, 'reasoning': None}
    assert llm_gateway.usage_of(None) == {'in': None, 'out': None, 'reasoning': None}
    assert llm_gateway.usage_of({'usage': {'output_tokens_details': {'reasoning_tokens': 5}}})['reasoning'] == 5


# ── the switch ───────────────────────────────────────────────────────────

class FakeGemini:
    def __init__(self):
        self.calls = []

        class Models:
            def generate_content(inner, model=None, contents=None, config=None):
                self.calls.append({'model': model, 'contents': contents, 'config': config})
                return type('R', (), {'text': json.dumps(NOTE, ensure_ascii=False)})()
        self.models = Models()


def _gemini_processor(monkeypatch, backend):
    monkeypatch.setattr(Config, 'LLM_BACKEND', backend)
    monkeypatch.setattr(llm_module, 'lookup_cie11', lambda dx, term=None: {'code': 'GC00.3', 'title': 'Cistitis'})
    proc = llm_module.LLMProcessor()
    fake = FakeGemini()
    proc.client = fake
    return proc, fake


def test_default_backend_is_gemini():
    assert Config.LLM_BACKEND == 'gemini'                                  # nothing set in the test env
    assert llm_module.LLMProcessor().backend == 'gemini'


def test_gemini_backend_unchanged_and_never_calls_the_gateway(monkeypatch):
    post = _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    proc, fake = _gemini_processor(monkeypatch, 'gemini')
    data = proc.extract_structured_data(TRANSCRIPT, utterances=UTTERANCES)
    assert post.calls == []
    (call,) = fake.calls
    assert call['model'] == 'gemini-2.5-flash'
    assert call['contents'] == create_extraction_prompt(TRANSCRIPT, UTTERANCES)
    assert call['config'].temperature == 0.1 and call['config'].response_mime_type == 'application/json'
    assert data['evaluacion']['codigo_cie11'] == 'GC00.3'


def test_unknown_backend_falls_back_to_gemini_with_an_error(monkeypatch, log):
    post = _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    proc, fake = _gemini_processor(monkeypatch, 'gatway')
    assert proc.backend == 'gemini' and "unknown LLM_BACKEND='gatway'" in log.getvalue()
    proc.extract_structured_data(TRANSCRIPT)
    assert post.calls == [] and len(fake.calls) == 1


def test_gateway_backend_never_calls_gemini(monkeypatch, gateway_processor):
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body())))
    fake = FakeGemini()
    gateway_processor.client = fake
    gateway_processor.extract_structured_data(TRANSCRIPT)
    assert fake.calls == []


def test_llm_backend_read_from_the_environment(monkeypatch):
    import importlib
    import config as config_module
    for raw, expected in [(' Gateway ', 'gateway'), ('', 'gemini'), (None, 'gemini')]:
        if raw is None:
            monkeypatch.delenv('LLM_BACKEND', raising=False)
        else:
            monkeypatch.setenv('LLM_BACKEND', raw)
        assert importlib.reload(config_module).Config.LLM_BACKEND == expected
    monkeypatch.delenv('LLM_BACKEND', raising=False)
    importlib.reload(config_module)


# ── the job: same user-facing message as a Gemini failure ───────────────

def test_job_failure_message_is_the_same_as_gemini(monkeypatch, gateway_processor):
    import app as app_module
    patches = []
    monkeypatch.setattr(app_module, '_sb_patch_job', lambda job_id, body: patches.append(body))
    monkeypatch.setattr(app_module.transcription_service, 'transcribe_audio',
                        lambda *a, **k: {'text': TRANSCRIPT, 'utterances': UTTERANCES})
    monkeypatch.setattr(app_module, 'llm_processor', gateway_processor)
    _use(monkeypatch, FakePost(FakeResponse(200, ok_body(finish='max_tokens'))))
    app_module._run_job('job-fixture', '/nonexistent/audio.webm',
                        {'print_raw': False, 'speakers_expected': 2}, 'u', 'c', 'Dr. Fixture')
    assert patches[-1] == {'status': 'error', 'error_message': 'Extracción de datos fallida. Intente de nuevo.'}
