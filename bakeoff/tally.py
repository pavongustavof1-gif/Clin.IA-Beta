"""`tally`: blind scores + key + checks + consistency + cost/latency/failures
→ bakeoff/scores/summary.md.

Ranking (overall): fewest safety errors first, then the highest mean blind
score (sum of the three 1-5 scores), then the highest check pass rate, then the
lowest cost. Safety comes first on purpose: one unsafe note outweighs polish.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

from bakeoff import paths
from bakeoff.checks import check_records, consistency

SCORE_FIELDS = ('completeness', 'accuracy_no_inventions', 'clinical_usefulness')


class TallyError(RuntimeError):
    pass


def read_scores(path: Path) -> dict:
    """note_label → parsed scores; rows without all three scores are ignored."""
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            label = (row.get('note_label') or '').strip()
            try:
                values = [int(str(row.get(k, '')).strip()) for k in SCORE_FIELDS]
            except ValueError:
                continue
            if not all(1 <= v <= 5 for v in values):
                raise TallyError(f'{label}: scores must be 1-5, got {values}')
            safety = str(row.get('safety_error', '')).strip().lower()
            out[label] = {**dict(zip(SCORE_FIELDS, values)), 'total': sum(values),
                          'safety_error': safety in ('y', 'yes', 's', 'si', 'sí', '1', 'true'),
                          'comment': (row.get('comment') or '').strip()}
    return out


def _fmt(v, pct=False, digits=2):
    if v is None:
        return '–'
    if pct:
        return f'{v:.0%}'
    return f'{v:.{digits}f}' if isinstance(v, float) else str(v)


def _mean(values):
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def tally(scenarios, records, key: dict, scores: dict) -> str:
    check_rows = check_records(records, {s.id: s for s in scenarios})
    cons = {(c['scenario'], c['model']): c for c in consistency(records)}
    runs_by = defaultdict(list)
    for r in records:
        runs_by[(r['scenario'], r['model'])].append(r)
    checks_by = defaultdict(list)
    for c in check_rows:
        checks_by[(c['scenario'], c['model'])].append(c)

    blind_by = {}                       # (scenario, model) → (label, scores or None)
    for sid, labels in key.get('scenarios', {}).items():
        for label, info in labels.items():
            blind_by[(sid, info['model'])] = (label, scores.get(f'{sid}/{label}'))

    models = sorted({r['model'] for r in records})
    lines = ['# Bake-off summary (#83)', '',
             'Invented data only. Blind scores are 1-5 (completeness, accuracy/no inventions, '
             'clinical usefulness; total out of 15). Checks and consistency cover every run; '
             'cost and latency are per call.', '']
    overall = defaultdict(lambda: {'totals': [], 'safety': 0, 'scored': 0, 'checks_p': 0, 'checks_t': 0,
                                   'cons': [], 'costs': [], 'lat': [], 'fail': 0, 'calls': 0, 'out_tok': []})
    for s in sorted(scenarios, key=lambda s: s.id):
        present = [m for m in models if runs_by.get((s.id, m))]
        if not present:
            continue
        lines += [f'## {s.id} — {s.title}', '',
                  '| Model | Blind | Score /15 | Compl. | Accur. | Useful | Safety error | Checks | Consistency | '
                  'Latency s | Cost USD | Failures |',
                  '|---|---|---|---|---|---|---|---|---|---|---|---|']
        for m in present:
            runs = runs_by[(s.id, m)]
            chk = checks_by[(s.id, m)]
            cp, ct = sum(c['checks_passed'] for c in chk), sum(c['checks_total'] for c in chk)
            fails = sum(1 for r in runs if r.get('http_status') != 200 or not r.get('parse_ok') or not r.get('valid'))
            costs = [r.get('cost_usd') for r in runs]
            lats = [r.get('latency_s') for r in runs]
            label, sc = blind_by.get((s.id, m), ('–', None))
            o = overall[m]
            o['checks_p'] += cp; o['checks_t'] += ct
            o['cons'].append(cons[(s.id, m)]['mean_agreement'])
            o['costs'] += [c for c in costs if c is not None]; o['lat'] += [l for l in lats if l is not None]
            o['fail'] += fails; o['calls'] += len(runs)
            o['out_tok'] += [r['usage']['output_tokens'] for r in runs
                             if r.get('usage') and r['usage'].get('output_tokens') is not None]
            if sc:
                o['totals'].append(sc['total']); o['scored'] += 1; o['safety'] += int(sc['safety_error'])
            lines.append('| ' + ' | '.join([
                m, label, _fmt(sc['total']) if sc else '–', _fmt(sc['completeness']) if sc else '–',
                _fmt(sc['accuracy_no_inventions']) if sc else '–', _fmt(sc['clinical_usefulness']) if sc else '–',
                ('**YES**' if sc['safety_error'] else 'no') if sc else '–',
                f'{cp}/{ct} ({_fmt(cp / ct, pct=True)})' if ct else '–',
                _fmt(cons[(s.id, m)]['mean_agreement'], pct=True),
                _fmt(_mean(lats), digits=1), _fmt(_mean(costs), digits=4), f'{fails}/{len(runs)}',
            ]) + ' |')
        comments = [(lab, sc['comment']) for (sid, m), (lab, sc) in blind_by.items()
                    if sid == s.id and sc and sc['comment']]
        if comments:
            lines += ['', 'Comments: ' + '; '.join(f'**{lab}**: {c}' for lab, c in sorted(comments))]
        lines.append('')

    ranking = []
    for m, o in overall.items():
        ranking.append({
            'model': m, 'safety': o['safety'], 'scored': o['scored'],
            'blind_mean': _mean(o['totals']), 'checks': (o['checks_p'] / o['checks_t']) if o['checks_t'] else None,
            'consistency': _mean(o['cons']), 'cost': _mean(o['costs']), 'total_cost': sum(o['costs']),
            'latency': _mean(o['lat']), 'fail': o['fail'], 'calls': o['calls'], 'out_tok': _mean(o['out_tok']),
        })
    ranking.sort(key=lambda r: (r['safety'], -(r['blind_mean'] or 0), -(r['checks'] or 0), r['cost'] or 0))
    lines += ['## Overall ranking', '',
              'Order: fewest safety errors, then highest mean blind score, then check pass rate, then cost.', '',
              '| # | Model | Safety errors | Blind mean /15 (notes scored) | Checks | Consistency | '
              'Mean latency s | Mean cost/call USD | Total cost USD | Failures | Mean output tokens |',
              '|---|---|---|---|---|---|---|---|---|---|---|']
    for i, r in enumerate(ranking, 1):
        lines.append('| ' + ' | '.join([
            str(i), r['model'], str(r['safety']), f'{_fmt(r["blind_mean"])} ({r["scored"]})',
            _fmt(r['checks'], pct=True), _fmt(r['consistency'], pct=True), _fmt(r['latency'], digits=1),
            _fmt(r['cost'], digits=4), _fmt(r['total_cost'], digits=4), f'{r["fail"]}/{r["calls"]}',
            _fmt(r['out_tok'], digits=0),
        ]) + ' |')
    if not scores:
        lines += ['', '_No blind scores yet: fill in scores/blind_scores.csv and run tally again._']
    return '\n'.join(lines) + '\n'


def run_tally(scenarios, records, key_dir=None, scores_dir=None, log=print) -> Path:
    key_dir, scores_dir = key_dir or paths.KEY, scores_dir or paths.SCORES
    key_file = key_dir / 'key.json'
    key = json.loads(key_file.read_text(encoding='utf-8')) if key_file.exists() else {}
    scores = read_scores(scores_dir / 'blind_scores.csv')
    out = scores_dir / 'summary.md'
    scores_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(tally(scenarios, records, key, scores), encoding='utf-8')
    log(f'{len(records)} runs, {len(scores)} blind scores → {out}')
    return out
