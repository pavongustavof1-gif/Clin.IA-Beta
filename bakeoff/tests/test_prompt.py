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
        body = gateway.build_request(spec, core.build_prompt(case['utterances']), core.gateway_schema(case['utterances']))
        assert body['messages'] == [{'role': 'user', 'content': _golden('diabetes_tres_hablantes')}]


def _keys(sch):
    """Every keyword used in schema position (never property names)."""
    found = set(sch)
    for sub in sch.get('properties', {}).values():
        found |= _keys(sub)
    for key in ('items', 'additionalProperties', 'propertyNames'):
        if isinstance(sch.get(key), dict):
            found |= _keys(sch[key])
    for sub in sch.get('anyOf', []):
        found |= _keys(sub)
    return found


def test_schema_sent_is_the_wire_schema():
    full = core.load_schema()
    utts = _case('diabetes_tres_hablantes')['utterances']
    sent = core.gateway_schema(utts)
    assert sent == core.extraction_core.wire_schema(full, ['Hablante A', 'Hablante C', 'Hablante B'])
    keys = _keys(sent)
    assert not keys & set(core.extraction_core.WIRE_SCHEMA_DROPPED), keys
    assert not keys & core.extraction_core.WIRE_SCHEMA_REFUSED
    assert 'propertyNames' not in json.dumps(sent) and 'additionalProperties' not in keys
    assert set(sent['properties']) == set(full['properties'])
    assert list(sent['properties']['roles_detectados']['properties']) == ['Hablante A', 'Hablante C', 'Hablante B']
    assert 'roles_detectados' not in core.gateway_schema([])      # flat transcript: no roles asked for
    assert sent['properties']['actualizacion_antecedentes']['properties']['detectado']['anyOf'] == \
        full['properties']['actualizacion_antecedentes']['properties']['detectado']['anyOf']


def test_full_schema_is_untouched_by_building_the_wire_schema():
    before = paths.EXTRACTION_SCHEMA.read_bytes()
    full = core.load_schema()
    snapshot = json.loads(json.dumps(full))
    core.gateway_schema()
    core.extraction_core.wire_schema(full)
    assert full == snapshot
    assert paths.EXTRACTION_SCHEMA.read_bytes() == before
    assert full['properties']['roles_detectados']['propertyNames'] == {'pattern': '^Hablante '}


def test_request_body_carries_the_wire_schema(models_cfg):
    spec = models_cfg.models['claude-haiku-5-5']
    body = gateway.build_request(spec, 'x', core.gateway_schema(_case('lumbalgia_dos_hablantes')['utterances']))
    assert 'propertyNames' not in json.dumps(body)


def test_parser_and_validator_are_productions():
    assert core.clean_json_response is core.extraction_core.clean_json_response
    assert core.validate_against_schema is core.extraction_core.validate_against_schema
