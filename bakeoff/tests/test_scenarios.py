import json

import pytest

from bakeoff import paths, scenarios


def _yaml(tmp_path, body, name='BO-X.yaml'):
    p = tmp_path / name
    p.write_text(body, encoding='utf-8')
    return p


def test_committed_scenarios_load():
    loaded = {s.id: s for s in scenarios.load_scenarios()}
    assert set(loaded) >= {'BO-02', 'BO-06', 'BO-08'}
    assert loaded['BO-02'].audio == 'BO-02_diabetes/BO-02_diabetes_toma2_3min.m4a'
    assert loaded['BO-06'].audio == 'BO-06_lumbalgia/BO-06_lumbalgia_3min.m4a'
    assert loaded['BO-08'].audio == 'BO-08_ivu/BO-08_ivu_2min.m4a'
    for s in loaded.values():
        assert s.consulta and s.speakers_expected > 0 and s.checks['must_contain']
    assert {'path': 'plan.medicamentos', 'text': 'ibuprofeno'} in loaded['BO-06'].checks['must_not_contain']
    # Checks confirmed against the cached transcripts (no draft notes left).
    assert {'any_of': ['3 meses', 'tres meses']} in loaded['BO-02'].checks['must_contain']
    assert {'text': '1000 miligramos'} in loaded['BO-02'].checks['must_not_contain']
    assert {'text': 'urgencias'} in loaded['BO-06'].checks['must_contain']
    assert {'any_of': ['ardor', 'disuria', 'arde']} in loaded['BO-08'].checks['must_contain']
    assert {'text': 'me hace muchísimo'} in loaded['BO-08'].checks['must_not_contain']
    for f in paths.SCENARIOS.glob('*.yaml'):
        assert 'draft' not in f.read_text(encoding='utf-8')


@pytest.mark.parametrize('datos', ['', 'datos: reales\n', 'datos: Inventados\n', 'datos: true\n'])
def test_refuses_without_datos_inventados(tmp_path, datos):
    p = _yaml(tmp_path, f'id: BO-X\ntitle: t\n{datos}transcript: t.txt\n')
    with pytest.raises(scenarios.ScenarioError, match='datos: inventados'):
        scenarios.load_scenario(p)


def test_needs_exactly_one_source(tmp_path):
    with pytest.raises(scenarios.ScenarioError):
        scenarios.load_scenario(_yaml(tmp_path, 'id: BO-X\ndatos: inventados\n'))
    with pytest.raises(scenarios.ScenarioError):
        scenarios.load_scenario(_yaml(tmp_path, 'id: BO-X\ndatos: inventados\naudio: a.m4a\ntranscript: t.txt\n'))


def test_unknown_check_type_rejected(tmp_path):
    with pytest.raises(scenarios.ScenarioError):
        scenarios.load_scenario(_yaml(tmp_path, 'id: BO-X\ndatos: inventados\ntranscript: t\nchecks:\n  must_be: []\n'))


def test_parse_transcript_text():
    text = '[Hablante A]: Buenos días.\n[Hablante B]:Me duele\nla cabeza.\n\n[Hablante  C]: Soy su hija.\n'
    assert scenarios.parse_transcript_text(text) == [
        {'speaker': 'A', 'text': 'Buenos días.'}, {'speaker': 'B', 'text': 'Me duele la cabeza.'},
        {'speaker': 'C', 'text': 'Soy su hija.'}]
    with pytest.raises(scenarios.ScenarioError):
        scenarios.parse_transcript_text('Sin etiqueta\n')
    with pytest.raises(scenarios.ScenarioError):
        scenarios.parse_transcript_text('\n')


def test_audio_scenario_needs_cache(tmp_path):
    s = scenarios.load_scenario(_yaml(tmp_path, 'id: BO-X\ndatos: inventados\naudio: a.m4a\n'))
    with pytest.raises(scenarios.ScenarioError, match='transcribe'):
        scenarios.load_utterances(s, cache_dir=tmp_path)
    (tmp_path / 'BO-X.json').write_text(json.dumps({'utterances': [{'speaker': 'A', 'text': 'hola'}]}))
    assert scenarios.load_utterances(s, cache_dir=tmp_path) == [{'speaker': 'A', 'text': 'hola'}]


@pytest.mark.parametrize('item', ['{text: a, any_of: [b]}', '{path: plan}', '{any_of: []}', '{any_of: "a"}',
                                  '{any_of: [a, ""]}', 'a'])
def test_bad_text_check_items_rejected(tmp_path, item):
    body = f'id: BO-X\ndatos: inventados\ntranscript: t\nchecks:\n  must_contain: [{item}]\n'
    with pytest.raises(scenarios.ScenarioError):
        scenarios.load_scenario(_yaml(tmp_path, body))


def test_any_of_item_accepted(tmp_path):
    body = 'id: BO-X\ndatos: inventados\ntranscript: t\nchecks:\n  must_not_contain: [{path: objetivo, any_of: [x, y]}]\n'
    assert scenarios.load_scenario(_yaml(tmp_path, body)).checks['must_not_contain'][0]['any_of'] == ['x', 'y']
