"""`judge`: offline (HTTP mocked). Blindness, one note per call, order,
verdict parsing, the CSV, and tally's merge/disagreement/calibration."""
import csv
import json
import re
from dataclasses import replace

import pytest

from bakeoff import blind, judge, paths, tally
from bakeoff.tests.conftest import UTTERANCES, FakeResponse, gateway_ok, note

API_KEY = 'clave-ficticia-SECRETA-1234567890'
SETTINGS = {'ASSEMBLYAI_API_KEY': API_KEY}
REFERENCE = 'Nitrofurantoína 100 mg cada 12 h por 5 días. Examen general de orina.'
MODELS = ['gemini-2.5-flash', 'gemini-3.5-flash-lite', 'claude-haiku-5-5', 'claude-sonnet-5-5']
# One distinct follow-up per model, so each prompt shows which note it carries.
MARKERS = {m: f'Revaloración en {i} semanas' for i, m in enumerate(MODELS, 2)}
VERDICT = {'completeness': 4, 'accuracy_no_inventions': 5, 'clinical_usefulness': 4, 'safety_error': 'n',
           'invented_facts': [], 'omissions': ['No anota la ingesta de agua'], 'safety_issues': [],
           'comment': 'Nota correcta y útil.'}


@pytest.fixture
def scenario(text_scenario):
    return replace(text_scenario, reference=REFERENCE)


def _records(sid='BO-T1'):
    recs = []
    for m in MODELS:
        for r in (1, 2):
            n = note(plan={**note()['plan'], 'seguimiento': MARKERS[m]})
            if m == 'claude-sonnet-5-5':          # a note that names a model is masked by `blind`
                n['subjetivo'] = {'motivo_de_consulta': 'Ardor al orinar (nota de Claude Sonnet)'}
            recs.append({'scenario': sid, 'model': m, 'run': r, 'http_status': 200, 'parse_ok': True, 'valid': True,
                         'note': n, 'raw_response_text': '{}', '_file': f'{sid}/{m}__run{r}.json'})
    return recs


def _blind(tmp_path, scenario, records=None, seed=7):
    return blind.make_blind([scenario], records or _records(), lambda s: UTTERANCES, set(MODELS), seed=seed,
                            blind_dir=tmp_path / 'blind', key_dir=tmp_path / 'key', scores_dir=tmp_path / 'scores',
                            force=True, log=lambda *a: None)


def _post(verdict=VERDICT, finish='stop', status=200):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append({'body': json, 'headers': headers, 'timeout': timeout})
        content = verdict if isinstance(verdict, str) else __import__('json').dumps(verdict, ensure_ascii=False)
        body = gateway_ok(content, 2500, 900)
        body['choices'][0]['finish_reason'] = finish
        return FakeResponse(status, body if status == 200 else {'message': 'x'})
    return post, sent


def _judge(tmp_path, scenario, models_cfg, post, judges=None, settings=SETTINGS, utterances=UTTERANCES, **kw):
    log = []
    out = judge.run_judge([scenario], judges or models_cfg.select_judges(None), models_cfg, settings,
                          lambda s: utterances, blind_dir=tmp_path / 'blind', judge_dir=tmp_path / 'judge',
                          scores_dir=tmp_path / 'scores', post=post, sleep=lambda s: None, log=log.append, **kw)
    return out, log


def _prompt(call):
    msgs = call['body']['messages']
    assert len(msgs) == 1
    return msgs[0]['content']


# --- configuration -----------------------------------------------------------

def test_judges_in_models_yaml(models_cfg):
    js = {j.name: j for j in models_cfg.select_judges(None)}
    assert set(js) == {'claude-sonnet-5-5', 'gemini-2.5-flash'}
    for j in js.values():
        assert j.max_tokens == 16384 and j.timeout_s == 300 and j.output_mode == 'none'
    assert not js['claude-sonnet-5-5'].accepts_temperature
    assert models_cfg.select_judges(['gemini-2.5-flash']) == [js['gemini-2.5-flash']]
    with pytest.raises(ValueError, match='unknown judge'):
        models_cfg.select_judges(['gpt-imaginario'])


def test_committed_scenarios_have_a_reference():
    from bakeoff.scenarios import load_scenarios
    refs = {s.id: s.reference for s in load_scenarios()}
    assert 'se corrigió a 850' in refs['BO-02'] and 'No se dijo la duración' in refs['BO-02']
    assert '"muslo"' in refs['BO-06'] and 'no se afirma ningún' in refs['BO-06']
    assert 'ceftriaxona' in refs['BO-08'] and 'ESE MISMO DÍA' in refs['BO-08']


def test_rubric_matches_the_human_sheet_and_the_parser():
    rubric = paths.JUDGE_RUBRIC.read_text(encoding='utf-8')
    for field in blind.SCORE_COLUMNS[1:5] + list(judge.LIST_FIELDS) + ['comment']:
        assert f'"{field}"' in rubric or f'**{field}**' in rubric or field in rubric, field
    assert 'orden telefónica' in rubric and 'completitud, no' in rubric


# --- blindness ---------------------------------------------------------------

def test_prompts_never_contain_model_names_key_or_other_notes(tmp_path, scenario, models_cfg):
    key = _blind(tmp_path, scenario)
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post)
    assert len(sent) == 2 * 4                                  # 2 judges × 4 notes, one note per call
    key_text = (tmp_path / 'key' / 'key.json').read_text(encoding='utf-8')
    run_files = [info['run_file'] for labels in key['scenarios'].values() for info in labels.values()]
    for call in sent:
        p = _prompt(call)
        low = p.lower()
        for w in judge.forbidden_words(models_cfg.all_names()):
            assert w not in low, w
        assert API_KEY not in p and API_KEY not in json.dumps(call['body'])
        assert str(key['seed']) not in p and not any(f in p for f in run_files) and key_text not in p
        assert sum(m in p for m in MARKERS.values()) == 1       # exactly one note
        assert p.count('## Nota a evaluar') == 1
        assert REFERENCE in p and '[Hablante A]: Buenos días' in p
        assert call['headers']['Authorization'] == API_KEY       # the key only ever goes in the header
    assert any('[modelo]' in _prompt(c) for c in sent)             # the masked note, as shown in blind/


def test_the_judge_never_reads_the_key_or_the_runs():
    src = (paths.BAKEOFF / 'judge.py').read_text(encoding='utf-8')
    assert 'paths.KEY' not in src and 'key.json' not in src and 'paths.RUNS' not in src
    assert 'load_runs' not in src


def test_a_model_name_in_the_prompt_stops_before_any_call(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    post, sent = _post()
    with pytest.raises(judge.JudgeError, match='claude'):
        _judge(tmp_path, scenario, models_cfg, post,
               utterances=UTTERANCES + [{'speaker': 'B', 'text': 'Lo leí en Claude.'}])
    with pytest.raises(judge.JudgeError, match='API key'):
        _judge(tmp_path, scenario, models_cfg, post, utterances=UTTERANCES + [{'speaker': 'B', 'text': API_KEY}])
    assert sent == []


def test_note_text_is_the_blind_note_panel(tmp_path, scenario):
    _blind(tmp_path, scenario)
    notes = judge.load_blind_notes(tmp_path / 'blind')
    assert [n.label for n in notes] == ['BO-T1/A', 'BO-T1/B', 'BO-T1/C', 'BO-T1/D']
    text = notes[0].text
    assert text.startswith('## ') and '## Plan (P)' in text and '- Nombre: Nitrofurantoína' in text
    assert '- Dosis: 100 mg' in text and '• Examen general de orina' in text
    assert 'Transcripción' not in text and 'Buenos días' not in text      # transcript panel left out
    assert 'Nota B' not in text and 'Nota A' not in text                   # heading and A/B/C links left out


def test_failed_blind_note_is_not_judged(tmp_path, scenario, models_cfg):
    recs = _records()
    for r in recs:
        if r['model'] == 'claude-haiku-5-5':
            r.update(http_status=500, raw_response_text=None, parse_ok=False, note=None)
    _blind(tmp_path, scenario, recs)
    post, sent = _post()
    _, log = _judge(tmp_path, scenario, models_cfg, post)
    assert len(sent) == 2 * 3 and any('failed call — not judged' in line for line in log)


def test_missing_reference_is_an_error(tmp_path, text_scenario, models_cfg):
    _blind(tmp_path, text_scenario)
    post, sent = _post()
    with pytest.raises(judge.JudgeError, match='reference'):
        _judge(tmp_path, text_scenario, models_cfg, post)
    assert sent == []


def test_no_blind_notes_is_an_error(tmp_path, scenario, models_cfg):
    with pytest.raises(judge.JudgeError, match='blind'):
        _judge(tmp_path, scenario, models_cfg, _post()[0])


# --- order, budget, files ----------------------------------------------------

def test_random_order_per_judge_is_seeded_and_saved(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post, seed=42)
    meta = json.loads((tmp_path / 'judge' / 'order.json').read_text(encoding='utf-8'))
    assert meta['seed'] == 42 and set(meta['order']) == {'claude-sonnet-5-5', 'gemini-2.5-flash'}
    labels = ['BO-T1/A', 'BO-T1/B', 'BO-T1/C', 'BO-T1/D']
    for j, order in meta['order'].items():
        assert sorted(order) == labels
        expected = sorted(labels)
        __import__('random').Random(f'42:{j}').shuffle(expected)
        assert order == expected
    # Calls follow each judge's order (judges one after the other).
    notes = {n.label: n for n in judge.load_blind_notes(tmp_path / 'blind')}
    seen = [(c['body']['model'], next(lab for lab, n in notes.items() if n.text[-200:] in _prompt(c)))
            for c in sent]
    for j in meta['order']:
        model = models_cfg.judges[j].model
        assert [lab for m, lab in seen if m == model] == meta['order'][j]
    # Without --seed the saved seed is reused.
    assert judge.judge_order(labels, list(meta['order']), judge_dir=tmp_path / 'judge')['seed'] == 42


def test_budget_abort_before_any_call(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    post, sent = _post()
    with pytest.raises(judge.BudgetExceeded):
        _judge(tmp_path, scenario, models_cfg, post, settings={**SETTINGS, 'BAKEOFF_BUDGET_USD': '0.0001'})
    assert sent == []


def test_request_uses_each_judges_settings(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post)
    by_model = {c['body']['model']: c for c in sent}
    assert by_model['claude-sonnet-5-5']['timeout'] == 300 and by_model['gemini-2.5-flash']['timeout'] == 300
    assert all(c['body']['max_tokens'] == 16384 and 'response_format' not in c['body'] for c in sent)
    assert 'temperature' not in by_model['claude-sonnet-5-5']['body']
    assert by_model['gemini-2.5-flash']['body']['temperature'] == 0.1
    rec = json.loads((tmp_path / 'judge' / 'claude-sonnet-5-5' / 'BO-T1__A.json').read_text(encoding='utf-8'))
    assert rec['parse_ok'] and rec['judgment']['completeness'] == 4 and rec['max_tokens'] == 16384
    assert rec['request']['messages'] == [{'role': 'user', 'content': '<judge prompt; see prompt_sha256>'}]
    assert rec['request']['prompt_chars'] > 0 and 'Hablante' not in json.dumps(rec['request'])
    assert rec['blind_file'] == 'nota_A.html' and len(rec['blind_sha256']) == 64


def test_judge_scores_csv(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    verdict = {**VERDICT, 'safety_error': 'y', 'safety_issues': ['Dosis distinta', 'Falta signo de alarma']}
    out, _ = _judge(tmp_path, scenario, models_cfg, _post(verdict)[0])
    rows = list(csv.DictReader(out.open(encoding='utf-8')))
    assert list(rows[0]) == judge.CSV_COLUMNS
    assert len(rows) == 8 and {r['judge'] for r in rows} == {'claude-sonnet-5-5', 'gemini-2.5-flash'}
    assert rows[0]['safety_error'] == 'y' and rows[0]['safety_issues'] == 'Dosis distinta | Falta signo de alarma'
    assert rows[0]['omissions'] == 'No anota la ingesta de agua' and rows[0]['comment'] == 'Nota correcta y útil.'


def test_done_skipped_failures_truncation_and_new_blind_rejudged(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    gemini = models_cfg.select_judges(['gemini-2.5-flash'])
    post, sent = _post(finish='max_tokens')                    # cut off: not a verdict, judged again
    _judge(tmp_path, scenario, models_cfg, post, judges=gemini)
    rec = json.loads((tmp_path / 'judge' / 'gemini-2.5-flash' / 'BO-T1__A.json').read_text(encoding='utf-8'))
    assert not rec['parse_ok'] and 'max_tokens' in rec['parse_error']
    post, sent = _post('esto no es JSON')
    _judge(tmp_path, scenario, models_cfg, post, judges=gemini)
    assert len(sent) == 4
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post, judges=gemini)
    assert len(sent) == 4
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post, judges=gemini)
    assert sent == []                                          # all valid: skipped
    _blind(tmp_path, scenario, seed=99)                         # a new pick → new pages → judged again
    post, sent = _post()
    _judge(tmp_path, scenario, models_cfg, post, judges=gemini)
    changed = [n for n in judge.load_blind_notes(tmp_path / 'blind')]
    assert 0 < len(sent) <= len(changed)


# --- verdict parsing ---------------------------------------------------------

def test_parse_valid_and_tolerated_forms():
    ok = judge.parse_judgment(json.dumps(VERDICT))
    assert ok['parse_ok'] and ok['judgment']['safety_error'] == 'n' and ok['judgment']['omissions'] == [
        'No anota la ingesta de agua']
    fenced = judge.parse_judgment('```json\n' + json.dumps({**VERDICT, 'safety_error': 'sí'}) + '\n```')
    assert fenced['parse_ok'] and fenced['judgment']['safety_error'] == 'y'
    assert judge.parse_judgment(json.dumps({**VERDICT, 'safety_error': True}))['judgment']['safety_error'] == 'y'
    assert judge.parse_judgment(json.dumps({**VERDICT, 'completeness': 4.0}))['judgment']['completeness'] == 4
    assert judge.parse_judgment(json.dumps({**VERDICT, 'extra': 1}))['parse_ok']


@pytest.mark.parametrize('change', [{'completeness': 0}, {'completeness': 6}, {'completeness': '4'},
                                    {'completeness': True}, {'clinical_usefulness': 3.5}, {'safety_error': 'quizá'},
                                    {'safety_error': None}, {'omissions': 'falta algo'}, {'invented_facts': [1]},
                                    {'comment': None}])
def test_parse_rejects_bad_verdicts(change):
    res = judge.parse_judgment(json.dumps({**VERDICT, **change}))
    assert not res['parse_ok'] and res['parse_error'] and res['judgment'] is None


@pytest.mark.parametrize('text', [None, 'no es json', '[1, 2]', json.dumps({k: v for k, v in VERDICT.items()
                                                                            if k != 'safety_issues'})])
def test_parse_rejects_non_verdicts(text):
    assert not judge.parse_judgment(text)['parse_ok']


# --- tally -------------------------------------------------------------------

def _judge_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=judge.CSV_COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({'invented_facts': '', 'omissions': '', 'safety_issues': '', 'comment': '', **r})


def _j(label, j, c, a, u, s='n'):
    return {'note_label': label, 'judge': j, 'completeness': c, 'accuracy_no_inventions': a,
            'clinical_usefulness': u, 'safety_error': s}


def _tally_dataset():
    recs = [{'scenario': 'BO-T1', 'model': m, 'run': 1, 'http_status': 200, 'parse_ok': True, 'valid': True,
             'note': note(), 'raw_response_text': '{}', 'latency_s': 2.0, 'cost_usd': 0.001,
             'usage': {'input_tokens': 1, 'output_tokens': 1}, 'output_mode': 'none'}
            for m in ('m-humano', 'm-jueces', 'm-dudoso')]
    key = {'seed': 1, 'scenarios': {'BO-T1': {'A': {'model': 'm-humano', 'run': 1},
                                              'B': {'model': 'm-jueces', 'run': 1},
                                              'C': {'model': 'm-dudoso', 'run': 1}}}}
    human = {'BO-T1/A': {'completeness': 4, 'accuracy_no_inventions': 5, 'clinical_usefulness': 4, 'total': 13,
                         'safety_error': False, 'comment': ''}}
    return recs, key, human


def test_read_judge_scores_and_merge(tmp_path, text_scenario):
    recs, key, human = _tally_dataset()
    p = tmp_path / 'judge_scores.csv'
    _judge_csv(p, [_j('BO-T1/A', 'j-uno', 5, 5, 5), _j('BO-T1/A', 'j-dos', 3, 4, 4),        # human: 4/5/4
                   _j('BO-T1/B', 'j-uno', 4, 4, 4), _j('BO-T1/B', 'j-dos', 4, 4, 4),        # no human score
                   _j('BO-T1/C', 'j-uno', 5, 5, 5, 'n'), _j('BO-T1/C', 'j-dos', 5, 5, 5, 'y')])
    js = tally.read_judge_scores(p)
    assert js['BO-T1/A']['j-uno']['total'] == 15 and js['BO-T1/C']['j-dos']['safety_error'] is True
    md = tally.tally([text_scenario], recs, key, human, js)
    # Scenario table: human columns, then the judges' mean and safety flags.
    assert '| m-humano | A | 13 | 4 | 5 | 4 | no | 13.0 | 0/2 |' in md
    assert '| m-jueces | B | – | – | – | – | – | 12.0 | 0/2 |' in md
    assert '| m-dudoso | C | – | – | – | – | – | 15.0 | **1/2** |' in md
    ranking = md.split('## Overall ranking')[1].split('## Automated judges')[0]
    assert '| m-humano | none | 0 | 13 (1) |' in ranking                  # human stays primary
    assert '| m-jueces | none | 0 | 12.00 (0 + 1 judge) |' in ranking
    assert '| m-dudoso | none | 1 | 15.00 (0 + 1 judge) |' in ranking         # any judge's flag counts
    assert ranking.index('m-humano') < ranking.index('m-jueces') < ranking.index('m-dudoso')
    section = md.split('## Automated judges')[1]
    assert '| Model | j-dos /15 | j-uno /15 | Judges mean /15 |' in section
    assert '| m-humano | 11.0 | 15.0 | 13.0 | 4.0 | 4.5 | 4.5 | 0/1 | 13.0 |' in section


def test_disagreements_flagged(tmp_path):
    js = {'X/A': {'j1': {'completeness': 5, 'accuracy_no_inventions': 5, 'clinical_usefulness': 5,
                         'safety_error': False},
                  'j2': {'completeness': 3, 'accuracy_no_inventions': 4, 'clinical_usefulness': 5,
                         'safety_error': False}},
          'X/B': {'j1': {'completeness': 4, 'accuracy_no_inventions': 4, 'clinical_usefulness': 4,
                         'safety_error': True},
                  'j2': {'completeness': 4, 'accuracy_no_inventions': 3, 'clinical_usefulness': 4,
                         'safety_error': False}},
          'X/C': {'j1': {'completeness': 4, 'accuracy_no_inventions': 4, 'clinical_usefulness': 4,
                         'safety_error': False},
                  'j2': {'completeness': 3, 'accuracy_no_inventions': 5, 'clinical_usefulness': 3,
                         'safety_error': False}},
          'X/D': {'j1': {'completeness': 1, 'accuracy_no_inventions': 1, 'clinical_usefulness': 1,
                         'safety_error': True}}}
    d = {r['note_label']: r['why'] for r in tally.disagreements(js)}
    assert d == {'X/A': ['completeness differs by 2'], 'X/B': ['safety']}     # 1-point gaps and lone judges aren't


def test_calibration_against_human(text_scenario):
    recs, key, human = _tally_dataset()
    human['BO-T1/C'] = {'completeness': 2, 'accuracy_no_inventions': 2, 'clinical_usefulness': 2, 'total': 6,
                        'safety_error': True, 'comment': ''}
    js = {'BO-T1/A': {'j-uno': {'completeness': 5, 'accuracy_no_inventions': 5, 'clinical_usefulness': 5,
                                'total': 15, 'safety_error': False}},
          'BO-T1/B': {'j-uno': {'completeness': 1, 'accuracy_no_inventions': 1, 'clinical_usefulness': 1,
                                'total': 3, 'safety_error': True}},              # no human: not in calibration
          'BO-T1/C': {'j-uno': {'completeness': 4, 'accuracy_no_inventions': 2, 'clinical_usefulness': 2,
                                'total': 8, 'safety_error': False}}}
    (row,) = tally.calibration(human, js)
    assert row['n'] == 2 and row['completeness'] == 1.5 and row['accuracy_no_inventions'] == 0
    assert row['clinical_usefulness'] == 0.5 and row['total'] == 2 and row['safety_agreement'] == 0.5
    md = tally.tally([text_scenario], recs, key, human, js)
    assert '| j-uno | 2 | 1.50 | 0.00 | 0.50 | 2.00 | 50% |' in md


def test_tally_without_judge_scores_has_no_judge_section(text_scenario):
    recs, key, human = _tally_dataset()
    md = tally.tally([text_scenario], recs, key, human)
    assert '## Automated judges' not in md
    assert 'No note has both' not in md


def test_run_tally_reads_judge_scores(tmp_path, text_scenario):
    recs, key, _ = _tally_dataset()
    (tmp_path / 'key').mkdir()
    (tmp_path / 'key' / 'key.json').write_text(json.dumps(key))
    (tmp_path / 'scores').mkdir()
    _judge_csv(tmp_path / 'scores' / 'judge_scores.csv', [_j('BO-T1/B', 'j-uno', 2, 2, 2, 'y')])
    md = tally.run_tally([text_scenario], recs, key_dir=tmp_path / 'key', scores_dir=tmp_path / 'scores',
                         log=lambda *a: None).read_text(encoding='utf-8')
    assert '| m-jueces | B | – | – | – | – | – | 6.0 | **1/1** |' in md
    assert 'No note has both human and judge scores yet' in md


# --- estimate ----------------------------------------------------------------

def test_estimate_covers_judges_without_blind(capsys, monkeypatch, tmp_path):
    from bakeoff import cli
    monkeypatch.setattr(paths, 'ENV_FILE', tmp_path / 'missing.env')
    monkeypatch.setattr(paths, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(paths, 'BLIND', tmp_path / 'blind')
    assert cli.main(['estimate', '--scenarios', 'BO-02,BO-06,BO-08', '--runs', '3']) == 0
    out = capsys.readouterr().out
    # 4 models → 4 blind notes per scenario × 3 scenarios = 12 per judge.
    assert re.search(r'claude-sonnet-5-5\s+12 ', out.split('Judge calls')[1])
    assert 'JUDGES TOTAL                    24' in out and 'judge estimate is WITHIN budget' in out
    assert 'no blind/ yet' in out
    assert cli.main(['estimate', '--judges', 'gemini-2.5-flash', '--scenarios', 'BO-08']) == 0
    out = capsys.readouterr().out.split('Judge calls')[1]
    assert 'gemini-2.5-flash' in out and 'claude-sonnet-5-5' not in out


def test_estimate_uses_the_real_blind_notes(tmp_path, scenario, models_cfg):
    _blind(tmp_path, scenario)
    lines, basis = judge.estimate_judges([scenario], models_cfg.select_judges(None), models_cfg, 99,
                                         lambda s: UTTERANCES, blind_dir=tmp_path / 'blind')
    assert 'blind/ notes for 1 of 1' in basis and [l.calls for l in lines] == [4, 4]
    assert all(l.worst_cost > l.cost for l in lines)
