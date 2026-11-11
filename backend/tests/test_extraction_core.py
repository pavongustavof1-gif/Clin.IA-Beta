# backend/tests/test_extraction_core.py
# #83 (0.8.11): extraction_core is the pure half of the extraction, shared by
# production (llm_processor) and the bake-off harness.
#   - Prompt pinning: tests/fixtures/prompt_golden_*.txt were captured from
#     llm_processor.LLMProcessor.create_extraction_prompt at 0.8.10-beta,
#     BEFORE the move; both the new module and production's wrapper must
#     reproduce them byte for byte.
#   - extraction_schema.json describes the output the prompt asks for (all
#     fields optional) and matches the prompt's own JSON template.
#   - Importing extraction_core reads no environment and loads no .env
#     (checked in a subprocess with an empty environment).
# Invented data only.

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import extraction_core
import llm_processor
from llm_processor import LLMProcessor

BACKEND = Path(__file__).resolve().parent.parent
FIXTURES = BACKEND / 'tests' / 'fixtures'
INPUTS = {k: v for k, v in json.loads((FIXTURES / 'prompt_inputs.json').read_text(encoding='utf-8')).items()
          if not k.startswith('_')}
OUTPUTS = {k: v for k, v in json.loads((FIXTURES / 'extraction_outputs.json').read_text(encoding='utf-8')).items()
           if not k.startswith('_')}
SCHEMA = json.loads((BACKEND / 'extraction_schema.json').read_text(encoding='utf-8'))


def _golden(name):
    with open(FIXTURES / f'prompt_golden_{name}.txt', encoding='utf-8', newline='') as f:
        return f.read()


# ── Prompt pinning ──────────────────────────────────────────────────────

def test_fixture_set():
    # 3 diarized consultations (the [Hablante X]: format) + the flat path.
    assert set(INPUTS) == set(OUTPUTS) == {'lumbalgia_dos_hablantes', 'diabetes_tres_hablantes',
                                           'comillas_y_llaves', 'plano_sin_hablantes'}
    assert sum(1 for c in INPUTS.values() if c['utterances']) == 3


@pytest.mark.parametrize('name', sorted(INPUTS))
def test_extraction_core_prompt_is_byte_identical(name):
    case = INPUTS[name]
    assert extraction_core.create_extraction_prompt(case['transcript'], case['utterances']) == _golden(name)


@pytest.mark.parametrize('name', sorted(INPUTS))
def test_production_wrapper_prompt_is_byte_identical(name):
    case = INPUTS[name]
    assert LLMProcessor.create_extraction_prompt(None, case['transcript'], case['utterances']) == _golden(name)


def test_utterances_render_as_hablante_lines():
    golden = _golden('lumbalgia_dos_hablantes')
    for u in INPUTS['lumbalgia_dos_hablantes']['utterances']:
        assert f"[Hablante {u['speaker']}]: {u['text']}\n" in golden
    assert '[Hablante' not in _golden('plano_sin_hablantes')


def test_production_sends_the_core_prompt(monkeypatch):
    """extract_structured_data sends exactly the shared prompt to the model."""
    sent = []
    p = LLMProcessor.__new__(LLMProcessor)
    p.model_id = 'fake'
    p.client = type('C', (), {'models': type('M', (), {'generate_content': staticmethod(
        lambda **k: sent.append(k['contents']) or type('R', (), {'text': '{"subjetivo": {"motivo_de_consulta": "x"}}'})())})()})()
    monkeypatch.setattr(llm_processor, 'apply_cie11_lookup', lambda data: None)
    case = INPUTS['diabetes_tres_hablantes']
    p.extract_structured_data(case['transcript'], case['utterances'])
    assert sent == [_golden('diabetes_tres_hablantes')]


# ── Parser and validator: same functions, same behaviour ────────────────

def test_production_uses_the_core_functions():
    for name in ('create_extraction_prompt', 'clean_json_response', 'validate_against_schema',
                 'normalize_evaluacion', 'accept_search_term', 'content_words', 'CIE11_TERM_KEY'):
        assert getattr(llm_processor, name) is getattr(extraction_core, name), name
    import icd_service
    assert icd_service.content_words is extraction_core.content_words
    assert icd_service.CONTENT_STOPWORDS is extraction_core.CONTENT_STOPWORDS


@pytest.mark.parametrize('raw,cleaned', [
    ('{"a": 1}', '{"a": 1}'),
    ('  \n```json\n{"a": {"b": 2}}\n```  ', '{"a": {"b": 2}}'),
    ('Aquí está: {"a": 1} gracias', '{"a": 1}'),
    ('sin llaves', 'sin llaves'),
    ('', ''),
    ('}{', ''),          # pinned as-is: last '}' before first '{' slices to empty
])
def test_clean_json_response(raw, cleaned):
    assert extraction_core.clean_json_response(raw) == cleaned
    assert LLMProcessor._clean_json_response(None, raw) == cleaned


@pytest.mark.parametrize('data,expected,after', [
    ({'subjetivo': {'motivo_de_consulta': 'x'}}, (True, None),
     {'subjetivo': {'motivo_de_consulta': 'x'}, 'informacion_paciente': {}, 'metadata': {}}),
    ({'informacion_paciente': 'x', 'metadata': None, 'plan': {'tratamiento': 'y'}}, (True, None),
     {'informacion_paciente': {}, 'metadata': {}, 'plan': {'tratamiento': 'y'}}),
    ({'evaluacion': {'diagnostico_principal': 'Dx ficticio'}}, (True, None),
     {'evaluacion': {'diagnostico': 'Dx ficticio'}, 'informacion_paciente': {}, 'metadata': {}}),
    ({'informacion_paciente': {'nombre_del_paciente': 'x'}}, (False, 'No meaningful medical data extracted'),
     {'informacion_paciente': {'nombre_del_paciente': 'x'}, 'metadata': {}}),
    ({'subjetivo': {}}, (False, 'No meaningful medical data extracted'),
     {'subjetivo': {}, 'informacion_paciente': {}, 'metadata': {}}),
])
def test_validate_against_schema_unchanged(data, expected, after):
    a, b = json.loads(json.dumps(data)), json.loads(json.dumps(data))
    assert extraction_core.validate_against_schema(a) == expected
    assert LLMProcessor.validate_against_schema(None, b) == expected
    assert a == b == after


@pytest.mark.parametrize('name', sorted(OUTPUTS))
def test_fixture_outputs_pass_the_production_validator(name):
    data = json.loads(json.dumps(OUTPUTS[name]))
    assert extraction_core.validate_against_schema(data) == (True, None)


# ── extraction_schema.json ──────────────────────────────────────────────
# jsonschema isn't a dependency; this validator covers exactly the keywords
# the schema uses, and test_schema_uses_only_supported_keywords keeps it so.

SUPPORTED = {'$schema', '$id', 'title', 'description', '$comment', 'type', 'properties', 'items',
             'additionalProperties', 'propertyNames', 'pattern', 'anyOf', 'enum'}
TYPES = {'object': dict, 'array': list, 'string': str}


def _validate(inst, sch, path='$'):
    errors = []
    if 'anyOf' in sch and not any(not _validate(inst, s, path) for s in sch['anyOf']):
        errors.append(f'{path}: matches no anyOf branch')
    t = sch.get('type')
    if t == 'boolean':
        if not isinstance(inst, bool):
            return errors + [f'{path}: not boolean']
    elif t is not None and (not isinstance(inst, TYPES[t]) or isinstance(inst, bool)):
        return errors + [f'{path}: not {t}']
    if 'enum' in sch and inst not in sch['enum']:
        errors.append(f'{path}: {inst!r} not in enum')
    if 'pattern' in sch and isinstance(inst, str) and not re.search(sch['pattern'], inst):
        errors.append(f'{path}: {inst!r} does not match {sch["pattern"]}')
    if isinstance(inst, dict):
        props = sch.get('properties', {})
        for k, v in inst.items():
            if 'propertyNames' in sch:
                errors += _validate(k, sch['propertyNames'], f'{path}.<key {k}>')
            if k in props:
                errors += _validate(v, props[k], f'{path}.{k}')
            elif isinstance(sch.get('additionalProperties'), dict):
                errors += _validate(v, sch['additionalProperties'], f'{path}.{k}')
    if isinstance(inst, list) and 'items' in sch:
        for i, v in enumerate(inst):
            errors += _validate(v, sch['items'], f'{path}[{i}]')
    return errors


def _walk(sch):
    yield sch
    for key in ('properties',):
        for sub in sch.get(key, {}).values():
            yield from _walk(sub)
    for key in ('items', 'additionalProperties', 'propertyNames'):
        if isinstance(sch.get(key), dict):
            yield from _walk(sch[key])
    for sub in sch.get('anyOf', []):
        yield from _walk(sub)


def test_schema_uses_only_supported_keywords_and_no_required():
    for node in _walk(SCHEMA):
        assert set(node) <= SUPPORTED, set(node) - SUPPORTED
        assert 'required' not in node
    assert '"required"' not in (BACKEND / 'extraction_schema.json').read_text(encoding='utf-8')
    assert SCHEMA['$schema'] == 'https://json-schema.org/draft/2020-12/schema'


def _template():
    """The JSON template inside the prompt (the diarized one has roles_detectados)."""
    golden = _golden('lumbalgia_dos_hablantes')
    start = golden.index('siga este esquema:\n\n') + len('siga este esquema:\n\n')
    end = golden.index('\n\nREGLAS IMPORTANTES')
    return json.loads(golden[start:end])


def _shape(tmpl):
    """Field names of the prompt template, nested."""
    if isinstance(tmpl, dict):
        return {k: _shape(v) for k, v in tmpl.items()}
    if isinstance(tmpl, list) and tmpl and isinstance(tmpl[0], dict):
        return [_shape(tmpl[0])]
    return None


def _schema_shape(sch):
    if sch.get('type') == 'object' and 'properties' in sch:
        return {k: _schema_shape(v) for k, v in sch['properties'].items()}
    if sch.get('type') == 'array' and sch.get('items', {}).get('type') == 'object':
        return [_schema_shape(sch['items'])]
    return None


def test_schema_fields_match_the_prompt_template_exactly():
    tmpl = _shape(_template())
    roles = tmpl.pop('roles_detectados')
    shape = _schema_shape(SCHEMA)
    shape.pop('roles_detectados')
    assert shape == tmpl
    assert set(roles) == {'Hablante A', 'Hablante B'}
    assert 'termino_busqueda_cie11' in shape['evaluacion']
    for forbidden in ('codigo_cie11', 'titulo_cie11', 'cie11_fuente'):
        assert forbidden not in json.dumps(SCHEMA['properties'])


def test_the_prompt_template_itself_validates():
    tmpl = _template()
    tmpl['actualizacion_antecedentes']['detectado'] = True   # the template holds a description there
    assert _validate(tmpl, SCHEMA) == []


@pytest.mark.parametrize('name', sorted(OUTPUTS))
def test_fixture_outputs_validate_against_the_schema(name):
    assert _validate(OUTPUTS[name], SCHEMA) == []


def test_empty_object_is_valid_all_fields_optional():
    assert _validate({}, SCHEMA) == []
    assert _validate({'subjetivo': {}, 'plan': {'medicamentos': [{}]}}, SCHEMA) == []


@pytest.mark.parametrize('bad', [
    [],
    {'subjetivo': {'sintomas': 'tos'}},
    {'subjetivo': {'motivo_de_consulta': 5}},
    {'plan': {'medicamentos': ['Paracetamol']}},
    {'plan': {'medicamentos': [{'dosis': 500}]}},
    {'objetivo': {'signos_vitales': {'temperatura': 38.5}}},
    {'evaluacion': {'diagnosticos_adicionales': 'Dx'}},
    {'evaluacion': {'termino_busqueda_cie11': ['Lumbalgia']}},
    {'actualizacion_antecedentes': {'detectado': 'sí'}},
    {'actualizacion_antecedentes': {'detectado': 1}},
    {'roles_detectados': {'Speaker A': 'médico'}},
    {'roles_detectados': {'Hablante A': 1}},
    {'metadata': 'hoy'},
], ids=['top-array', 'sintomas-str', 'motivo-int', 'med-str', 'dosis-int', 'temp-float', 'adicionales-str',
        'term-list', 'detectado-si', 'detectado-int', 'roles-key', 'roles-value', 'metadata-str'])
def test_schema_rejects_wrong_types(bad):
    assert _validate(bad, SCHEMA) != []


def test_stale_schema_json_is_gone():
    assert not (BACKEND / 'schema.json').exists()


# ── Purity ──────────────────────────────────────────────────────────────

STDLIB_ALLOWED = {'re', 'unicodedata', 'typing'}


def test_extraction_core_imports_only_the_stdlib_it_needs():
    tree = ast.parse((BACKEND / 'extraction_core.py').read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    assert imported == STDLIB_ALLOWED


PURITY_SCRIPT = r'''
import builtins, io, json, os, sys
reads, opened = [], []

class Recorder(dict):
    def __getitem__(self, k): reads.append(k); return dict.__getitem__(self, k)
    def get(self, k, d=None): reads.append(k); return dict.get(self, k, d)
    def __contains__(self, k): reads.append(k); return dict.__contains__(self, k)
    def __setitem__(self, k, v): reads.append('SET:' + k); dict.__setitem__(self, k, v)

os.environ = Recorder(os.environ)
_open = builtins.open
def spy_open(file, *a, **k):
    opened.append(str(file)); return _open(file, *a, **k)
builtins.open = io.open = spy_open

sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
import extraction_core
new = sorted(m for m in set(sys.modules) - before)
prompt = extraction_core.create_extraction_prompt('texto ficticio', [{'speaker': 'A', 'text': 'hola'}])
ok = extraction_core.validate_against_schema({'subjetivo': {'motivo_de_consulta': 'x'}})
print(json.dumps({'env': dict(os.environ), 'reads': reads, 'opened': opened, 'new_modules': new,
                  'prompt_ok': '[Hablante A]: hola' in prompt, 'valid': list(ok)}))
'''


def test_importing_extraction_core_loads_no_environment(tmp_path):
    # A decoy .env right where a careless load_dotenv() would find it.
    (tmp_path / '.env').write_text('GEMINI_API_KEY=decoy-should-never-load\nEXTRACTION_DECOY=1\n')
    script = tmp_path / 'purity_check.py'
    script.write_text(PURITY_SCRIPT)
    proc = subprocess.run([sys.executable, '-I', '-S', str(script), str(BACKEND)],
                          cwd=tmp_path, env={}, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out['reads'] == [], out['reads']                       # no os.environ access at all
    assert not any(p.endswith('.env') for p in out['opened']), out['opened']
    assert 'GEMINI_API_KEY' not in out['env'] and 'EXTRACTION_DECOY' not in out['env']
    forbidden = {'config', 'dotenv', 'flask', 'supabase', 'google', 'logger', 'icd_service',
                 'llm_processor', 'requests'}
    assert not {m.split('.')[0] for m in out['new_modules']} & forbidden, out['new_modules']
    assert out['prompt_ok'] is True and out['valid'] == [True, None]
