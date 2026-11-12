import json
from dataclasses import replace

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
    assert body['max_tokens'] == 16384          # per-model cap (models.yaml)
    assert 'response_format' not in body          # output_mode none
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
    assert 'response_format' not in params and 'response_format' not in body


MODES = {'gemini-2.5-flash': 'none', 'gemini-3.5-flash-lite': 'none', 'claude-haiku-5-5': 'none',
         'claude-sonnet-5-5': 'none'}


def test_the_four_models_send_no_schema_and_match_their_output_mode(models_cfg):
    # Decision 2026-11-11: no schema for any model (production sends Gemini none).
    for spec in models_cfg.default_models():
        assert spec.output_mode == MODES[spec.name], spec.name
        body = gateway.build_request(spec, 'prompt ficticio', SCHEMA)
        assert 'json_schema' not in json.dumps(body), spec.name
        expected = {'none': None, 'json_object': {'type': 'json_object'}}[spec.output_mode]
        assert body.get('response_format') == expected, spec.name
    assert set(MODES) == {s.name for s in models_cfg.default_models()}
    assert models_cfg.models['gemini-3.5-flash-lite@low'].output_mode == 'none'


def test_each_output_mode_builds_its_response_format(models_cfg):
    base = models_cfg.models['claude-haiku-5-5']
    assert 'response_format' not in gateway.build_request(replace(base, output_mode='none'), 'p', SCHEMA)
    assert gateway.build_request(replace(base, output_mode='json_object'), 'p')['response_format'] == \
        {'type': 'json_object'}
    body = gateway.build_request(replace(base, output_mode='json_schema'), 'p', SCHEMA)
    assert body['response_format'] == {'type': 'json_schema',
                                       'json_schema': {'name': 'nota_soap', 'schema': SCHEMA, 'strict': False}}
    params = gateway.request_params(body)
    assert params['response_format'] == {'type': 'json_schema', 'json_schema': {'name': 'nota_soap', 'strict': False}}
    assert gateway.request_params(gateway.build_request(replace(base, output_mode='json_object'), 'p'))[
        'response_format'] == {'type': 'json_object'}
    with pytest.raises(ValueError):
        gateway.build_request(replace(base, output_mode='json_schema'), 'p')        # no schema given
    with pytest.raises(ValueError):
        gateway.build_request(replace(base, output_mode='xml'), 'p', SCHEMA)


@pytest.mark.parametrize('mode', ['', 'xml', None])
def test_models_yaml_requires_a_known_output_mode(tmp_path, mode):
    from bakeoff.models import load_models
    line = '' if mode is None else f'    output_mode: "{mode}"\n'
    (tmp_path / 'm.yaml').write_text('models:\n  x:\n    model: x\n    price_in: 1\n    price_out: 1\n'
                                     '    accepts_temperature: true\n    est_output_tokens: 1\n' + line)
    with pytest.raises(ValueError):
        load_models(tmp_path / 'm.yaml')


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


def test_timeout_per_model(models_cfg):
    timeouts = {name: spec.timeout_s for name, spec in models_cfg.models.items()}
    assert timeouts == {'gemini-2.5-flash': 120, 'gemini-3.5-flash-lite': 120, 'gemini-3.5-flash-lite@low': 120,
                        'claude-haiku-5-5': 300, 'claude-sonnet-5-5': 300,
                        'claude-haiku-5-5@low': 300, 'claude-sonnet-5-5@low': 300}


def test_call_gateway_uses_the_given_timeout():
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append(timeout)
        return FakeResponse(200, gateway_ok('{}'))
    gateway.call_gateway({'model': 'x'}, 'clave-ficticia', post=post, sleep=lambda s: None, timeout=300)
    assert seen == [300]


@pytest.mark.parametrize('timeout', ['', '0', '-5', '"lento"', 'true'])
def test_models_yaml_requires_a_positive_timeout(tmp_path, timeout):
    from bakeoff.models import load_models
    line = '' if timeout == '' else f'    timeout_s: {timeout}\n'
    (tmp_path / 'm.yaml').write_text('models:\n  x:\n    model: x\n    price_in: 1\n    price_out: 1\n'
                                     '    accepts_temperature: true\n    est_output_tokens: 1\n'
                                     '    output_mode: none\n' + line)
    with pytest.raises(ValueError):
        load_models(tmp_path / 'm.yaml')


@pytest.mark.parametrize('name,base', [('claude-haiku-5-5@low', 'claude-haiku-5-5'),
                                       ('claude-sonnet-5-5@low', 'claude-sonnet-5-5')])
def test_claude_low_reasoning_variants(models_cfg, name, base):
    spec = models_cfg.models[name]
    assert spec.optional and spec not in models_cfg.default_models()
    assert spec.model == base and spec.reasoning_effort == 'low' and spec.output_mode == 'none'
    assert spec.price_in == models_cfg.models[base].price_in and spec.price_out == models_cfg.models[base].price_out
    body = gateway.build_request(spec, 'prompt ficticio')
    assert body['model'] == base and body['reasoning_effort'] == 'low'
    assert 'temperature' not in body and 'response_format' not in body
    assert 'reasoning_effort' not in gateway.build_request(models_cfg.models[base], 'prompt ficticio')
    assert models_cfg.select([name]) == [spec]


def test_usage_records_reasoning_tokens_when_reported():
    # Shape seen in the 2026-11-11 smoke test (OpenAI-style details block).
    body = {'usage': {'input_tokens': 4099, 'output_tokens': 3359, 'total_tokens': 7458,
                      'completion_tokens_details': {'reasoning_tokens': 2695, 'audio_tokens': 0}}}
    u = gateway.usage_of(body)
    assert (u['input_tokens'], u['output_tokens'], u['reasoning_tokens']) == (4099, 3359, 2695)
    assert gateway.usage_of({'usage': {'output_tokens': 10, 'output_tokens_details': {'reasoning_tokens': 4}}}
                            )['reasoning_tokens'] == 4
    assert gateway.usage_of({'usage': {'output_tokens': 10, 'completion_tokens_details': {'reasoning_tokens': 0}}}
                            )['reasoning_tokens'] == 0
    assert gateway.usage_of({'usage': {'input_tokens': 1, 'output_tokens': 2}})['reasoning_tokens'] is None
    assert gateway.usage_of(None)['reasoning_tokens'] is None


def test_max_tokens_per_model(models_cfg):
    caps = {name: spec.max_tokens for name, spec in models_cfg.models.items()}
    assert caps == {'gemini-2.5-flash': 16384, 'gemini-3.5-flash-lite': 8192, 'gemini-3.5-flash-lite@low': 8192,
                    'claude-haiku-5-5': 8192, 'claude-sonnet-5-5': 8192,
                    'claude-haiku-5-5@low': 8192, 'claude-sonnet-5-5@low': 8192}
    for spec in models_cfg.models.values():
        body = gateway.build_request(spec, 'prompt ficticio')
        assert body['max_tokens'] == spec.max_tokens == gateway.request_params(body)['max_tokens']


def _model_yaml(tmp_path, extra=''):
    (tmp_path / 'm.yaml').write_text('models:\n  x:\n    model: x\n    price_in: 1\n    price_out: 1\n'
                                     '    accepts_temperature: true\n    est_output_tokens: 1\n'
                                     '    output_mode: none\n    timeout_s: 60\n' + extra)
    return tmp_path / 'm.yaml'


def test_max_tokens_defaults_to_8192(tmp_path):
    from bakeoff.models import load_models
    assert load_models(_model_yaml(tmp_path)).models['x'].max_tokens == gateway.MAX_TOKENS == 8192
    assert load_models(_model_yaml(tmp_path, '    max_tokens: 4096\n')).models['x'].max_tokens == 4096


@pytest.mark.parametrize('value', ['0', '-1', '1.5', '"mucho"', 'true', 'null'])
def test_models_yaml_rejects_a_bad_max_tokens(tmp_path, value):
    from bakeoff.models import load_models
    with pytest.raises(ValueError, match='max_tokens'):
        load_models(_model_yaml(tmp_path, f'    max_tokens: {value}\n'))


def test_worst_case_estimate_uses_each_models_cap(models_cfg, text_scenario):
    from bakeoff.estimate import estimate
    specs = models_cfg.select(['gemini-2.5-flash', 'gemini-3.5-flash-lite'])
    _, lines = estimate([text_scenario], specs, 1, models_cfg)
    flash, lite = lines                       # same prices, so the gap is the output cap alone
    gap = models_cfg.cost(specs[0], 0, 16384 - 8192)
    assert flash.worst_cost - lite.worst_cost == pytest.approx(gap)
