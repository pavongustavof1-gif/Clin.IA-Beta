"""`tally`: blind scores + key + checks + consistency + cost/latency/failures
→ bakeoff/scores/summary.md.

Ranking (overall): fewest safety errors first, then the highest mean blind
score (sum of the three 1-5 scores), then the highest check pass rate, then the
lowest cost. Safety comes first on purpose: one unsafe note outweighs polish.

Automated judges (scores/judge_scores.csv, from `judge`): human scores stay
primary. A note with human scores uses them; a note without uses the judges'
mean, and counts as a safety error if ANY judge flagged one (conservative).
The summary also shows each judge per model, the notes where the judges
disagree, and how each judge compares with the human where both scored.
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


# Judges disagree on a note when any criterion differs by this much or more,
# or when they disagree on safety.
DISAGREEMENT_POINTS = 2


def read_judge_scores(path: Path) -> dict:
    """judge_scores.csv → {note_label: {judge: scores}} (same parsing as the
    human sheet; rows without all three scores are ignored)."""
    if not path.exists():
        return {}
    out = defaultdict(dict)
    with path.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            label, judge = (row.get('note_label') or '').strip(), (row.get('judge') or '').strip()
            try:
                values = [int(str(row.get(k, '')).strip()) for k in SCORE_FIELDS]
            except ValueError:
                continue
            if not all(1 <= v <= 5 for v in values):
                raise TallyError(f'{label} ({judge}): judge scores must be 1-5, got {values}')
            out[label][judge] = {**dict(zip(SCORE_FIELDS, values)), 'total': sum(values),
                                 'safety_error': str(row.get('safety_error', '')).strip().lower() in
                                 ('y', 'yes', 's', 'si', 'sí', '1', 'true'),
                                 'comment': (row.get('comment') or '').strip()}
    return dict(out)


def judges_mean(per_judge: dict | None):
    """Mean of the judges' scores for one note, with how many flagged safety."""
    if not per_judge:
        return None
    js = list(per_judge.values())
    m = {f: float(mean(j[f] for j in js)) for f in SCORE_FIELDS + ('total',)}
    m['flags'], m['n'] = sum(j['safety_error'] for j in js), len(js)
    return m


def disagreements(judge_scores: dict) -> list[dict]:
    rows = []
    for label, per in sorted(judge_scores.items()):
        if len(per) < 2:
            continue
        gaps = {f: max(j[f] for j in per.values()) - min(j[f] for j in per.values()) for f in SCORE_FIELDS}
        why = [f'{f} differs by {g}' for f, g in gaps.items() if g >= DISAGREEMENT_POINTS]
        if len({j['safety_error'] for j in per.values()}) > 1:
            why.append('safety')
        if why:
            rows.append({'note_label': label, 'why': why, 'per_judge': per})
    return rows


def calibration(scores: dict, judge_scores: dict) -> list[dict]:
    """Per judge: mean |judge − human| per criterion and total, and safety
    agreement, over the notes both scored."""
    judges = sorted({j for per in judge_scores.values() for j in per})
    rows = []
    for j in judges:
        pairs = [(scores[lab], per[j]) for lab, per in judge_scores.items() if j in per and lab in scores]
        row = {'judge': j, 'n': len(pairs)}
        for f in SCORE_FIELDS + ('total',):
            row[f] = float(mean(abs(h[f] - g[f]) for h, g in pairs)) if pairs else None
        row['safety_agreement'] = float(mean(h['safety_error'] == g['safety_error'] for h, g in pairs)) if pairs else None
        rows.append(row)
    return rows


def _fmt(v, pct=False, digits=2):
    if v is None:
        return '–'
    if pct:
        return f'{v:.0%}'
    return f'{v:.{digits}f}' if isinstance(v, float) else str(v)


def _mean(values):
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def tally(scenarios, records, key: dict, scores: dict, judge_scores: dict | None = None) -> str:
    judge_scores = judge_scores or {}
    judge_names = sorted({j for per in judge_scores.values() for j in per})
    check_rows = check_records(records, {s.id: s for s in scenarios})
    cons = {(c['scenario'], c['model']): c for c in consistency(records)}
    runs_by = defaultdict(list)
    for r in records:
        runs_by[(r['scenario'], r['model'])].append(r)
    checks_by = defaultdict(list)
    for c in check_rows:
        checks_by[(c['scenario'], c['model'])].append(c)

    blind_by = {}                       # (scenario, model) → (label, scores or None)
    label_model = {}                    # "BO-02/A" → model
    for sid, labels in key.get('scenarios', {}).items():
        for label, info in labels.items():
            blind_by[(sid, info['model'])] = (label, scores.get(f'{sid}/{label}'))
            label_model[f'{sid}/{label}'] = info['model']

    models = sorted({r['model'] for r in records})
    lines = ['# Bake-off summary (#83)', '',
             'Invented data only. Blind scores are 1-5 (completeness, accuracy/no inventions, '
             'clinical usefulness; total out of 15). Checks and consistency cover every run; '
             'cost and latency are per call. Output mode = how JSON was requested (none: prompt '
             'template only; json_object; json_schema).', '']
    overall = defaultdict(lambda: {'totals': [], 'safety': 0, 'scored': 0, 'checks_p': 0, 'checks_t': 0,
                                   'cons': [], 'costs': [], 'lat': [], 'fail': 0, 'calls': 0, 'out_tok': [], 'reason_tok': [],
                                   'modes': set(), 'judge_filled': 0})
    for s in sorted(scenarios, key=lambda s: s.id):
        present = [m for m in models if runs_by.get((s.id, m))]
        if not present:
            continue
        lines += [f'## {s.id} — {s.title}', '',
                  '| Model | Blind | Score /15 | Compl. | Accur. | Useful | Safety error | Judges /15 | '
                  'Judge safety flags | Checks | Consistency | Latency s | Cost USD | Failures |',
                  '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
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
            o['modes'] |= {r.get('output_mode') or '?' for r in runs}
            o['reason_tok'] += [r['usage']['reasoning_tokens'] for r in runs
                                if r.get('usage') and r['usage'].get('reasoning_tokens') is not None]
            o['out_tok'] += [r['usage']['output_tokens'] for r in runs
                             if r.get('usage') and r['usage'].get('output_tokens') is not None]
            jm = judges_mean(judge_scores.get(f'{s.id}/{label}'))
            if sc:                       # human scores stay primary
                o['totals'].append(sc['total']); o['scored'] += 1; o['safety'] += int(sc['safety_error'])
            elif jm:                     # no human score yet: the judges' mean; any judge's flag counts
                o['totals'].append(jm['total']); o['judge_filled'] += 1; o['safety'] += int(jm['flags'] > 0)
            lines.append('| ' + ' | '.join([
                m, label, _fmt(sc['total']) if sc else '–', _fmt(sc['completeness']) if sc else '–',
                _fmt(sc['accuracy_no_inventions']) if sc else '–', _fmt(sc['clinical_usefulness']) if sc else '–',
                ('**YES**' if sc['safety_error'] else 'no') if sc else '–',
                _fmt(float(jm['total']), digits=1) if jm else '–',
                (f'**{jm["flags"]}/{jm["n"]}**' if jm['flags'] else f'0/{jm["n"]}') if jm else '–',
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
            'model': m, 'safety': o['safety'], 'scored': o['scored'], 'judge_filled': o['judge_filled'],
            'blind_mean': _mean(o['totals']), 'checks': (o['checks_p'] / o['checks_t']) if o['checks_t'] else None,
            'consistency': _mean(o['cons']), 'cost': _mean(o['costs']), 'total_cost': sum(o['costs']),
            'latency': _mean(o['lat']), 'fail': o['fail'], 'calls': o['calls'], 'out_tok': _mean(o['out_tok']),
            'reason_tok': _mean(o['reason_tok']),
            'modes': ', '.join(sorted(o['modes'])),
        })
    ranking.sort(key=lambda r: (r['safety'], -(r['blind_mean'] or 0), -(r['checks'] or 0), r['cost'] or 0))
    lines += ['## Overall ranking', '',
              'Order: fewest safety errors, then highest mean blind score, then check pass rate, then cost. '
              'Human scores are used where present; "+ N judge" = notes scored by the judges\' mean only '
              '(safety error if any judge flagged one).', '',
              '| # | Model | Output mode | Safety errors | Blind mean /15 (notes scored) | Checks | Consistency | '
              'Mean latency s | Mean cost/call USD | Total cost USD | Failures | Mean output tokens | '
              'of which reasoning |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for i, r in enumerate(ranking, 1):
        lines.append('| ' + ' | '.join([
            str(i), r['model'], r['modes'], str(r['safety']),
            f'{_fmt(r["blind_mean"])} ({r["scored"]}' + (f' + {r["judge_filled"]} judge)' if r['judge_filled'] else ')'),
            _fmt(r['checks'], pct=True), _fmt(r['consistency'], pct=True), _fmt(r['latency'], digits=1),
            _fmt(r['cost'], digits=4), _fmt(r['total_cost'], digits=4), f'{r["fail"]}/{r["calls"]}',
            _fmt(r['out_tok'], digits=0), _fmt(r['reason_tok'], digits=0),
        ]) + ' |')
    if not scores:
        lines += ['', '_No blind scores yet: fill in scores/blind_scores.csv and run tally again._']
    if judge_scores:
        lines += _judge_section(judge_scores, judge_names, label_model, scores, models)
    return '\n'.join(lines) + '\n'


def _judge_section(judge_scores, judge_names, label_model, scores, models) -> list[str]:
    def f1(v):                           # one decimal everywhere in this section
        return _fmt(None if v is None else float(v), digits=1)
    lines = ['', '## Automated judges', '',
             'One blind note per call; scores 1-5, total /15. "Judges mean" averages the judges per note, then '
             'per model. Flagged = notes where at least one judge saw a safety error. Human = the blind sheet.', '',
             '| Model | ' + ' | '.join(f'{j} /15' for j in judge_names) +
             ' | Judges mean /15 | Compl. | Accur. | Useful | Flagged unsafe | Human /15 |',
             '|---|' + '---|' * len(judge_names) + '---|---|---|---|---|---|']
    for m in models:
        labs = [lab for lab, mm in label_model.items() if mm == m]
        per_judge = [_mean([judge_scores.get(lab, {}).get(j, {}).get('total') for lab in labs]) for j in judge_names]
        means = [judges_mean(judge_scores.get(lab)) for lab in labs]
        means = [x for x in means if x]
        if not means:
            continue
        human = _mean([scores[lab]['total'] for lab in labs if lab in scores])
        lines.append('| ' + ' | '.join([m, *[f1(v) for v in per_judge], f1(_mean([x['total'] for x in means])),
                                        *[f1(_mean([x[f] for x in means])) for f in SCORE_FIELDS],
                                        f'{sum(x["flags"] > 0 for x in means)}/{len(means)}', f1(human)]) + ' |')

    lines += ['', f'### Judges disagree (a criterion differs by ≥{DISAGREEMENT_POINTS}, or safety differs)', '']
    dis = disagreements(judge_scores)
    if dis:
        lines += ['| Note | Model | ' + ' | '.join(f'{j} C/A/U · safety' for j in judge_names) + ' | Why |',
                  '|---|---|' + '---|' * len(judge_names) + '---|']
        for d in dis:
            cells = []
            for j in judge_names:
                sc = d['per_judge'].get(j)
                cells.append(f'{sc["completeness"]}/{sc["accuracy_no_inventions"]}/{sc["clinical_usefulness"]} · '
                             f'{"**y**" if sc["safety_error"] else "n"}' if sc else '–')
            lines.append('| ' + ' | '.join([d['note_label'], label_model.get(d['note_label'], '?'), *cells,
                                            '; '.join(d['why'])]) + ' |')
    else:
        lines.append('_None._')

    lines += ['', '### Judge calibration against the human scores', '']
    cal = [c for c in calibration(scores, judge_scores) if c['n']]
    if cal:
        lines += ['Mean absolute difference from the human score (lower is closer) and safety agreement, over the '
                  'notes both scored.', '',
                  '| Judge | Notes | Compl. | Accur. | Useful | Total /15 | Safety agreement |',
                  '|---|---|---|---|---|---|---|']
        for c in cal:
            lines.append('| ' + ' | '.join([c['judge'], str(c['n']), *[_fmt(c[f]) for f in SCORE_FIELDS],
                                            _fmt(c['total']), _fmt(c['safety_agreement'], pct=True)]) + ' |')
    else:
        lines.append('_No note has both human and judge scores yet._')
    return lines


def run_tally(scenarios, records, key_dir=None, scores_dir=None, log=print) -> Path:
    key_dir, scores_dir = key_dir or paths.KEY, scores_dir or paths.SCORES
    key_file = key_dir / 'key.json'
    key = json.loads(key_file.read_text(encoding='utf-8')) if key_file.exists() else {}
    scores = read_scores(scores_dir / 'blind_scores.csv')
    judge_scores = read_judge_scores(scores_dir / 'judge_scores.csv')
    out = scores_dir / 'summary.md'
    scores_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(tally(scenarios, records, key, scores, judge_scores), encoding='utf-8')
    log(f'{len(records)} runs, {len(scores)} blind scores, '
        f'{sum(len(v) for v in judge_scores.values())} judge scores → {out}')
    return out
