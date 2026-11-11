import json

import pytest

from bakeoff import core, extract
from bakeoff.tests.conftest import UTTERANCES, FakeResponse, gateway_ok, note

SETTINGS = {'ASSEMBLYAI_API_KEY': 'clave-ficticia', 'BAKEOFF_BUDGET_USD': '5'}


def _post(content_for=lambda body: json.dumps(note(), ensure_ascii=False), status=200):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return FakeResponse(status, gateway_ok(content_for(json)) if status == 200 else {'message': 'x'})
    return post, sent


def _run(tmp_path, scenario, models_cfg, specs=None, runs=2, settings=SETTINGS, post=None, force=False):
    post = post or _post()[0]
    return extract.extract([scenario], specs or models_cfg.default_models(), runs, models_cfg, settings,
                           lambda s: UTTERANCES, runs_dir=tmp_path / 'runs', post=post, sleep=lambda s: None,
                           force=force, log=lambda *a: None)


def test_budget_abort_before_any_call(tmp_path, text_scenario, models_cfg):
    post, sent = _post()
    with pytest.raises(extract.BudgetExceeded):
        _run(tmp_path, text_scenario, models_cfg, settings={**SETTINGS, 'BAKEOFF_BUDGET_USD': '0.0001'}, post=post)
    assert sent == [] and not (tmp_path / 'runs').exists()


def test_writes_one_file_per_call_with_everything(tmp_path, text_scenario, models_cfg):
    post, sent = _post()
    written = _run(tmp_path, text_scenario, models_cfg, runs=2, post=post)
    assert len(written) == len(sent) == 4 * 2
    rec = json.loads((tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5__run2.json').read_text(encoding='utf-8'))
    assert rec['model'] == 'claude-haiku-5-5' and rec['run'] == 2 and rec['gateway_model'] == 'claude-haiku-5-5'
    assert rec['http_status'] == 200 and rec['request_id'] == 'req-ficticio' and rec['finish_reason'] == 'stop'
    assert rec['usage']['input_tokens'] == 3500 and rec['usage']['output_tokens'] == 1400
    assert rec['cost_usd'] == pytest.approx((3500 * 0.10 + 1400 * 0.50) / 1e6 * 1.10, abs=1e-6)
    assert rec['latency_s'] >= 0 and rec['attempts'] == 1
    assert rec['parse_ok'] is True and rec['valid'] is True and rec['validation_error'] is None
    assert rec['sent_temperature'] is None and 'temperature' not in rec['request']
    assert rec['raw_response_text'] == json.dumps(note(), ensure_ascii=False)
    gem = json.loads((tmp_path / 'runs' / 'BO-T1' / 'gemini-2.5-flash__run1.json').read_text(encoding='utf-8'))
    assert gem['sent_temperature'] == 0.1 and gem['request']['temperature'] == 0.1
    assert gem['request']['prompt_sha256'] and 'model_region' not in gem['request']


def test_every_call_sends_the_identical_prompt(tmp_path, text_scenario, models_cfg):
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, runs=2, post=post)
    prompts = {b['messages'][0]['content'] for b in sent}
    assert prompts == {core.build_prompt(UTTERANCES)}


def test_no_cie11_lookup_term_kept_as_returned(tmp_path, text_scenario, models_cfg):
    _run(tmp_path, text_scenario, models_cfg, specs=[models_cfg.models['claude-haiku-5-5']], runs=1)
    rec = json.loads((tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5__run1.json').read_text(encoding='utf-8'))
    assert rec['note']['evaluacion']['termino_busqueda_cie11'] == 'Infección de vías urinarias'
    assert 'codigo_cie11' not in json.dumps(rec['note'])


def test_finished_runs_are_not_repeated_but_failures_are(tmp_path, text_scenario, models_cfg):
    spec = [models_cfg.models['claude-haiku-5-5']]
    _run(tmp_path, text_scenario, models_cfg, specs=spec, runs=1, post=_post(status=500)[0])
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, specs=spec, runs=1, post=post)
    assert len(sent) == 1                       # the failed run was retried
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, specs=spec, runs=1, post=post)
    assert sent == []                           # the successful one is not
    _run(tmp_path, text_scenario, models_cfg, specs=spec, runs=1, post=post, force=True)
    assert len(sent) == 1


@pytest.mark.parametrize('content,parse_ok,valid', [
    ('```json\n{"subjetivo": {"motivo_de_consulta": "x"}}\n```', True, True),
    ('esto no es json', False, False),
    ('{"informacion_paciente": {"nombre_del_paciente": "x"}}', True, False),
    ('[1, 2]', True, False),
])
def test_parse_and_validation_recorded(tmp_path, text_scenario, models_cfg, content, parse_ok, valid):
    _run(tmp_path, text_scenario, models_cfg, specs=[models_cfg.models['claude-haiku-5-5']], runs=1,
         post=_post(content_for=lambda b: content)[0])
    rec = json.loads((tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5__run1.json').read_text(encoding='utf-8'))
    assert rec['parse_ok'] is parse_ok and bool(rec['valid']) is valid
    assert rec['raw_response_text'] == content


def test_http_failure_recorded_without_note(tmp_path, text_scenario, models_cfg):
    _run(tmp_path, text_scenario, models_cfg, specs=[models_cfg.models['claude-haiku-5-5']], runs=1,
         post=_post(status=400)[0])
    rec = json.loads((tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5__run1.json').read_text(encoding='utf-8'))
    assert rec['http_status'] == 400 and rec['error'] == 'http_400' and rec['parse_ok'] is False
    assert rec['raw_error_body'] and rec['cost_usd'] is None


def test_failed_call_prints_the_error_body(tmp_path, text_scenario, models_cfg):
    reason = 'ValidationException: output_config.format.schema: property \'propertyNames\' is not supported'
    long_body = {'message': reason + ' ' + 'x' * 1000, 'code': 400}

    def post(url, json=None, headers=None, timeout=None):
        return FakeResponse(400, long_body)
    lines = []
    extract.extract([text_scenario], [models_cfg.models['claude-haiku-5-5']], 1, models_cfg, SETTINGS,
                    lambda s: UTTERANCES, runs_dir=tmp_path / 'runs', post=post, sleep=lambda s: None,
                    log=lines.append)
    line = lines[-1]
    assert 'http_400: ' in line and reason in line
    preview = line.split('http_400: ', 1)[1].rsplit(' (', 1)[0]
    assert len(preview) == extract.ERROR_PREVIEW_CHARS
    rec = json.loads((tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5__run1.json').read_text(encoding='utf-8'))
    assert json.loads(rec['raw_error_body']) == long_body      # the full body is still kept


def test_successful_call_prints_no_body(tmp_path, text_scenario, models_cfg):
    lines = []
    extract.extract([text_scenario], [models_cfg.models['claude-haiku-5-5']], 1, models_cfg, SETTINGS,
                    lambda s: UTTERANCES, runs_dir=tmp_path / 'runs', post=_post()[0], sleep=lambda s: None,
                    log=lines.append)
    assert ': ok (' in lines[-1]


def test_run_file_records_the_output_mode_and_no_schema_is_sent(tmp_path, text_scenario, models_cfg):
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, runs=1, post=post)
    assert sent and all('response_format' not in b for b in sent)
    for spec in models_cfg.default_models():
        rec = json.loads((tmp_path / 'runs' / 'BO-T1' / f'{spec.name}__run1.json').read_text(encoding='utf-8'))
        assert rec['output_mode'] == spec.output_mode == 'none'
        assert 'response_format' not in rec['request']


def test_json_schema_mode_sends_the_scenario_wire_schema(tmp_path, text_scenario, models_cfg):
    from dataclasses import replace
    post, sent = _post()
    spec = replace(models_cfg.models['claude-haiku-5-5'], output_mode='json_schema')
    _run(tmp_path, text_scenario, models_cfg, specs=[spec], runs=1, post=post)
    assert sent[0]['response_format']['json_schema']['schema'] == core.gateway_schema(UTTERANCES)


def test_each_call_uses_and_records_its_model_timeout(tmp_path, text_scenario, models_cfg):
    timeouts = []

    def post(url, json=None, headers=None, timeout=None):
        timeouts.append((json['model'], timeout))
        return FakeResponse(200, gateway_ok(__import__('json').dumps(note())))
    _run(tmp_path, text_scenario, models_cfg, runs=1, post=post)
    assert dict(timeouts) == {'gemini-2.5-flash': 120, 'gemini-3.5-flash-lite': 120,
                              'claude-haiku-5-5': 300, 'claude-sonnet-5-5': 300}
    for spec in models_cfg.default_models():
        rec = json.loads((tmp_path / 'runs' / 'BO-T1' / f'{spec.name}__run1.json').read_text(encoding='utf-8'))
        assert rec['timeout_s'] == spec.timeout_s
        assert 'reasoning_tokens' in rec['usage']


def test_low_variants_run_only_when_named(tmp_path, text_scenario, models_cfg):
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, runs=1, post=post)
    assert all('reasoning_effort' not in b for b in sent)
    post, sent = _post()
    _run(tmp_path, text_scenario, models_cfg, runs=1, post=post,
         specs=models_cfg.select(['claude-haiku-5-5@low', 'claude-sonnet-5-5@low']))
    assert [(b['model'], b['reasoning_effort']) for b in sent] == [('claude-haiku-5-5', 'low'),
                                                                   ('claude-sonnet-5-5', 'low')]
    assert (tmp_path / 'runs' / 'BO-T1' / 'claude-haiku-5-5_at_low__run1.json').exists()
