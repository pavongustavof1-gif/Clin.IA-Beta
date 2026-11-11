"""The harness sends production's prompt, byte for byte, built by
backend/extraction_core.py — never a copy."""
import json

from bakeoff import core, gateway, paths

BACKEND_FIXTURES = paths.BACKEND / 'tests' / 'fixtures'


def _case(name):
    return json.loads((BACKEND_FIXTURES / 'prompt_inputs.json').read_text(encoding='utf-8'))[name]


def _golden(name):
    with open(BACKEND_FIXTURES / f'prompt_golden_{name}.txt', encoding='utf-8', newline='') as f:
        return f.read()


def test_extraction_core_is_the_backend_file():
    assert core.extraction_core.__file__ == str(paths.EXTRACTION_CORE)


def test_prompt_is_byte_identical_to_extraction_core():
    case = _case('lumbalgia_dos_hablantes')
    assert core.build_prompt(case['utterances']) == \
        core.extraction_core.create_extraction_prompt(case['transcript'], case['utterances'])


def test_prompt_matches_productions_golden_file():
    for name in ('lumbalgia_dos_hablantes', 'diabetes_tres_hablantes', 'comillas_y_llaves'):
        assert core.build_prompt(_case(name)['utterances']) == _golden(name)


def test_the_request_carries_exactly_that_prompt(models_cfg):
    case = _case('diabetes_tres_hablantes')
    for spec in models_cfg.models.values():
        body = gateway.build_request(spec, core.build_prompt(case['utterances']), core.gateway_schema())
        assert body['messages'] == [{'role': 'user', 'content': _golden('diabetes_tres_hablantes')}]


def test_schema_sent_is_the_backend_schema_minus_meta_keys():
    full = core.load_schema()
    sent = core.gateway_schema()
    assert {k: v for k, v in full.items() if k not in ('$schema', '$id')} == sent
    assert sent['properties'] == full['properties']


def test_parser_and_validator_are_productions():
    assert core.clean_json_response is core.extraction_core.clean_json_response
    assert core.validate_against_schema is core.extraction_core.validate_against_schema
