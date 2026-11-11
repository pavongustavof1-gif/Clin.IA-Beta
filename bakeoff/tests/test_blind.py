import csv
import json
import re

import pytest

from bakeoff import blind
from bakeoff.tests.conftest import UTTERANCES, note

MODELS = ['gemini-2.5-flash', 'gemini-3.5-flash-lite', 'claude-haiku-5-5', 'claude-sonnet-5-5']
FORBIDDEN = MODELS + ['gemini', 'claude', 'anthropic', 'google', 'haiku', 'sonnet', 'flash', 'gpt', 'openai',
                      'vertex', 'bedrock', 'assemblyai', 'gateway', 'request_id', 'req-ficticio']


def _records(scenario_id, extra_note=None):
    recs = []
    for m in MODELS:
        for r in (1, 2, 3):
            n = note(plan={**note()['plan'], 'seguimiento': f'Seguimiento {m} run {r}'}) if extra_note is None else extra_note
            recs.append({'scenario': scenario_id, 'model': m, 'run': r, 'http_status': 200, 'parse_ok': True,
                         'valid': True, 'note': n, 'raw_response_text': '{}', 'request_id': 'req-ficticio',
                         '_file': f'{scenario_id}/{m}__run{r}.json'})
    return recs


def _make(tmp_path, scenario, records, seed=1234, force=False):
    return blind.make_blind([scenario], records, lambda s: UTTERANCES, set(MODELS), seed=seed,
                            blind_dir=tmp_path / 'blind', key_dir=tmp_path / 'key', scores_dir=tmp_path / 'scores',
                            force=force, log=lambda *a: None)


def test_blind_output_contains_no_model_names(tmp_path, text_scenario):
    recs = _records('BO-T1')
    # Even a note that names a model in its text gets masked.
    recs[0]['note'] = note(plan={'seguimiento': 'Generado con Claude Sonnet y gemini-2.5-flash'})
    _make(tmp_path, text_scenario, recs)
    files = list((tmp_path / 'blind').rglob('*'))
    assert files
    for f in files:
        for word in FORBIDDEN:
            assert word.lower() not in f.name.lower(), f.name
        if f.is_file():
            text = f.read_text(encoding='utf-8').lower()
            for word in FORBIDDEN:
                assert word.lower() not in text, (f.name, word)
            assert '<meta name="generator"' not in text


def test_scores_csv_has_no_model_names(tmp_path, text_scenario):
    _make(tmp_path, text_scenario, _records('BO-T1'))
    text = (tmp_path / 'scores' / 'blind_scores.csv').read_text(encoding='utf-8').lower()
    assert not any(w in text for w in FORBIDDEN)
    rows = list(csv.DictReader((tmp_path / 'scores' / 'blind_scores.csv').open(encoding='utf-8')))
    assert [r['note_label'] for r in rows] == ['BO-T1/A', 'BO-T1/B', 'BO-T1/C', 'BO-T1/D']
    assert list(rows[0]) == ['note_label', 'completeness', 'accuracy_no_inventions', 'clinical_usefulness',
                             'safety_error', 'comment']


def test_one_note_per_model_with_transcript_and_key(tmp_path, text_scenario):
    key = _make(tmp_path, text_scenario, _records('BO-T1'))
    labels = key['scenarios']['BO-T1']
    assert sorted(labels) == ['A', 'B', 'C', 'D'] and sorted(v['model'] for v in labels.values()) == sorted(MODELS)
    saved = json.loads((tmp_path / 'key' / 'key.json').read_text(encoding='utf-8'))
    assert saved['seed'] == 1234 and saved['scenarios'] == key['scenarios']
    for lab, info in labels.items():
        page = (tmp_path / 'blind' / 'BO-T1' / f'nota_{lab}.html').read_text(encoding='utf-8')
        assert 'Nitrofurantoína' in page and 'Hablante A:' in page          # transcript + note
        assert f'Seguimiento' in page
        # The chosen run's own follow-up text is the one shown.
        assert f'Seguimiento [modelo] run {info["run"]}' in page


def test_seed_is_reproducible_and_labels_shuffle(tmp_path, text_scenario):
    a = _make(tmp_path / 'a', text_scenario, _records('BO-T1'), seed=7)
    b = _make(tmp_path / 'b', text_scenario, _records('BO-T1'), seed=7)
    assert a['scenarios'] == b['scenarios']
    orders = set()
    for seed in range(12):
        k = _make(tmp_path / f's{seed}', text_scenario, _records('BO-T1'), seed=seed)
        orders.add(tuple(k['scenarios']['BO-T1'][lab]['model'] for lab in 'ABCD'))
    assert len(orders) > 1          # labels are not in a fixed model order


def test_refuses_to_overwrite_key_or_scores(tmp_path, text_scenario):
    _make(tmp_path, text_scenario, _records('BO-T1'))
    with pytest.raises(blind.BlindError):
        _make(tmp_path, text_scenario, _records('BO-T1'))
    _make(tmp_path, text_scenario, _records('BO-T1'), force=True)


def test_failed_runs_render_as_failures(tmp_path, text_scenario):
    recs = _records('BO-T1')
    for r in recs:
        r.update(parse_ok=False, note=None)
    _make(tmp_path, text_scenario, recs)
    page = (tmp_path / 'blind' / 'BO-T1' / 'nota_A.html').read_text(encoding='utf-8')
    assert 'no se pudo leer como JSON' in page


def test_values_are_html_escaped(tmp_path, text_scenario):
    recs = _records('BO-T1', extra_note=note(subjetivo={'motivo_de_consulta': '<script>alert(1)</script>'}))
    _make(tmp_path, text_scenario, recs)
    page = (tmp_path / 'blind' / 'BO-T1' / 'nota_A.html').read_text(encoding='utf-8')
    assert '<script>' not in page and '&lt;script&gt;' in page
