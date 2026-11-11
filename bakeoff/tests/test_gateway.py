import pytest
import requests

from bakeoff import gateway
from bakeoff.tests.conftest import FakeResponse, gateway_ok

SCHEMA = {'type': 'object', 'properties': {}}


def test_claude_gets_no_temperature_gemini_gets_0_1(models_cfg):
    for name, spec in models_cfg.models.items():
        body = gateway.build_request(spec, 'prompt ficticio', SCHEMA)
        if name.startswith('claude'):
            assert 'temperature' not in body, name
        else:
            assert body['temperature'] == 0.1, name


def test_request_shape(models_cfg):
    body = gateway.build_request(models_cfg.models['gemini-2.5-flash'], 'prompt ficticio', SCHEMA)
    assert body['model'] == 'gemini-2.5-flash'
    assert body['max_tokens'] == 8192
    assert body['response_format'] == {'type': 'json_schema',
                                       'json_schema': {'name': 'nota_soap', 'schema': SCHEMA, 'strict': False}}
    assert 'model_region' not in body
    assert 'reasoning_effort' not in body


def test_reasoning_variant(models_cfg):
    spec = models_cfg.models['gemini-3.5-flash-lite@low']
    body = gateway.build_request(spec, 'prompt ficticio', SCHEMA)
    assert body['model'] == 'gemini-3.5-flash-lite' and body['reasoning_effort'] == 'low'
    assert spec.optional and spec not in models_cfg.default_models()


def test_request_params_record_what_was_sent_without_the_prompt(models_cfg):
    body = gateway.build_request(models_cfg.models['claude-haiku-5-5'], 'prompt ficticio largo', SCHEMA)
    params = gateway.request_params(body)
    assert 'prompt ficticio' not in str(params)
    assert params['prompt_chars'] == len('prompt ficticio largo') and len(params['prompt_sha256']) == 64
    assert params['max_tokens'] == 8192 and 'temperature' not in params
    assert params['response_format']['json_schema']['strict'] is False


def test_auth_header_is_the_bare_key_and_timeout_120():
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse(200, gateway_ok('{}'))
    gateway.call_gateway({'model': 'x'}, 'clave-ficticia', post=post, sleep=lambda s: None)
    assert seen['url'] == 'https://llm-gateway.assemblyai.com/v1/chat/completions'
    assert seen['headers']['Authorization'] == 'clave-ficticia'
    assert seen['timeout'] == 120


def _sequence(responses):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    return post, calls


def test_429_retried_honouring_retry_after():
    waits = []
    post, calls = _sequence([FakeResponse(429, {'message': 'too many'}, {'Retry-After': '7'}),
                             FakeResponse(200, gateway_ok('{"a": 1}'))])
    res = gateway.call_gateway({}, 'k', post=post, sleep=waits.append)
    assert res['http_status'] == 200 and res['attempts'] == 2 and waits == [7.0] and res['error'] is None


def test_5xx_retried_three_times_then_given_up():
    waits = []
    post, calls = _sequence([FakeResponse(503, {'message': 'down'})])
    res = gateway.call_gateway({}, 'k', post=post, sleep=waits.append)
    assert len(calls) == 4 and res['attempts'] == 4 and res['http_status'] == 503 and res['error'] == 'http_503'
    assert len(waits) == 3 and waits[0] < waits[1] < waits[2]


@pytest.mark.parametrize('status', [400, 401, 404])
def test_client_errors_not_retried(status):
    post, calls = _sequence([FakeResponse(status, {'message': 'bad'})])
    res = gateway.call_gateway({}, 'k', post=post, sleep=lambda s: None)
    assert len(calls) == 1 and res['error'] == f'http_{status}'


def test_timeout_recorded_not_retried():
    post, calls = _sequence([requests.Timeout('read timed out')])
    res = gateway.call_gateway({}, 'k', post=post, sleep=lambda s: None)
    assert len(calls) == 1 and res['http_status'] is None and res['error'] == 'Timeout'


def test_usage_and_content_parsing():
    body = gateway_ok('{"x": 1}', 10, 20)
    assert gateway.content_of(body) == '{"x": 1}'
    assert gateway.usage_of(body)['input_tokens'] == 10 and gateway.usage_of(body)['output_tokens'] == 20
    assert gateway.usage_of({'usage': {'prompt_tokens': 1, 'completion_tokens': 2}})['output_tokens'] == 2
    assert gateway.content_of({}) is None and gateway.usage_of(None)['input_tokens'] is None


def test_real_network_is_blocked_in_tests():
    with pytest.raises(Exception):
        requests.get('https://llm-gateway.assemblyai.com', timeout=2)
