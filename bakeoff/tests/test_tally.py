import csv
import json

from bakeoff import tally
from bakeoff.tests.conftest import note

MODELS = ['m-seguro', 'm-bonito', 'm-fallon']


def _dataset(text_scenario):
    recs = []
    for m in MODELS:
        for r in (1, 2, 3):
            ok = not (m == 'm-fallon' and r == 3)
            recs.append({'scenario': 'BO-T1', 'model': m, 'run': r, 'http_status': 200 if ok else 500,
                         'parse_ok': ok, 'valid': ok, 'note': note() if ok else None,
                         'raw_response_text': '{}' if ok else None, 'latency_s': 2.0 + r, 'cost_usd': 0.001 * (r + 1),
                         'usage': {'input_tokens': 3500, 'output_tokens': 1000 + r, 'reasoning_tokens': 600 + r},
                         'output_mode': 'json_object' if m == 'm-bonito' else 'none'})
    key = {'seed': 1, 'scenarios': {'BO-T1': {'A': {'model': 'm-seguro', 'run': 1},
                                              'B': {'model': 'm-bonito', 'run': 2},
                                              'C': {'model': 'm-fallon', 'run': 3}}}}
    return recs, key


def _scores(path, rows):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['note_label', 'completeness', 'accuracy_no_inventions',
                                          'clinical_usefulness', 'safety_error', 'comment'])
        w.writeheader()
        w.writerows(rows)


def test_tally_on_a_fake_dataset(tmp_path, text_scenario):
    recs, key = _dataset(text_scenario)
    (tmp_path / 'key').mkdir()
    (tmp_path / 'key' / 'key.json').write_text(json.dumps(key))
    (tmp_path / 'scores').mkdir()
    _scores(tmp_path / 'scores' / 'blind_scores.csv', [
        {'note_label': 'BO-T1/A', 'completeness': 4, 'accuracy_no_inventions': 5, 'clinical_usefulness': 4,
         'safety_error': 'n', 'comment': 'correcta'},
        {'note_label': 'BO-T1/B', 'completeness': 5, 'accuracy_no_inventions': 5, 'clinical_usefulness': 5,
         'safety_error': 'y', 'comment': 'dosis equivocada'},
        {'note_label': 'BO-T1/C', 'completeness': '', 'accuracy_no_inventions': '', 'clinical_usefulness': '',
         'safety_error': '', 'comment': ''},
    ])
    out = tally.run_tally([text_scenario], recs, key_dir=tmp_path / 'key', scores_dir=tmp_path / 'scores',
                          log=lambda *a: None)
    md = out.read_text(encoding='utf-8')
    assert '## BO-T1 — Escenario ficticio de prueba' in md and '## Overall ranking' in md
    assert '| m-seguro | A | 13 | 4 | 5 | 4 | no | – | – | 9/9 (100%)' in md     # no judge scores
    assert '| m-bonito | B | 15 | 5 | 5 | 5 | **YES** |' in md
    assert '| m-fallon | C | – |' in md and '1/3 |' in md            # unscored; 1 failed run
    ranking = md.split('## Overall ranking')[1]
    # Safety first: the prettier note with a safety error ranks below the safe one.
    assert ranking.index('m-seguro') < ranking.index('m-bonito')
    assert '**B**: dosis equivocada' in md
    assert '| Output mode |' in ranking and '| m-seguro | none |' in ranking
    assert '| m-bonito | json_object |' in ranking
    assert 'of which reasoning |' in ranking and '| 1002 | 602 |' in ranking


def test_tally_without_scores_still_reports(tmp_path, text_scenario):
    recs, key = _dataset(text_scenario)
    md = tally.tally([text_scenario], recs, key, {})
    assert 'No blind scores yet' in md and '| m-fallon |' in md


def test_scores_out_of_range_rejected(tmp_path):
    p = tmp_path / 's.csv'
    _scores(p, [{'note_label': 'X/A', 'completeness': 6, 'accuracy_no_inventions': 1, 'clinical_usefulness': 1,
                 'safety_error': 'n', 'comment': ''}])
    import pytest
    with pytest.raises(tally.TallyError):
        tally.read_scores(p)
