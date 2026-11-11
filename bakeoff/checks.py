"""`check`: per-run automated checks from the scenario YAML, plus consistency.

Matching ignores case and accents and collapses whitespace. `path` is a dotted
path into the note ("plan.medicamentos", "evaluacion.diagnostico"; numeric
parts index lists); without it the whole note is searched. Only VALUES are
searched, never field names.

A must_contain / must_not_contain item has either `text` or `any_of: [..]`
(alternative wordings, e.g. "3 días" / "tres días"). With any_of, must_contain
passes if ANY alternative is found, and must_not_contain fails if ANY is found.
"""
import csv
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from bakeoff import paths

_MISSING = object()


def normalize(text) -> str:
    folded = unicodedata.normalize('NFKD', str(text).lower())
    folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
    return ' '.join(folded.split())


def get_path(note, path: str | None):
    if not path:
        return note
    value = note
    for part in path.split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def leaves(value) -> list[str]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, dict):
        return [x for v in value.values() for x in leaves(v)]
    if isinstance(value, list):
        return [x for v in value for x in leaves(v)]
    return [str(value)]


def searchable(value) -> str:
    return normalize('\n'.join(leaves(value)))


def alternatives(check: dict) -> list[str]:
    """The wordings a text check accepts: [text], or the any_of list."""
    return [str(a) for a in check['any_of']] if 'any_of' in check else [str(check['text'])]


def _found(note, check: dict, parse_ok: bool) -> bool:
    if not parse_ok:
        return False
    haystack = searchable(get_path(note, check.get('path')))
    return any(normalize(a) in haystack for a in alternatives(check))


def run_checks(note, checks: dict, parse_ok: bool = True) -> list[dict]:
    """[{type, path, text, passed}] — every check fails when there is no note.
    For an any_of item, `text` is the alternatives joined with " | "."""
    results = []
    for check in checks.get('must_contain') or []:
        results.append({'type': 'must_contain', 'path': check.get('path'), 'text': ' | '.join(alternatives(check)),
                        'passed': _found(note, check, parse_ok)})
    for check in checks.get('must_not_contain') or []:
        results.append({'type': 'must_not_contain', 'path': check.get('path'), 'text': ' | '.join(alternatives(check)),
                        'passed': bool(parse_ok and not _found(note, check, parse_ok))})
    for path in checks.get('must_have_fields') or []:
        value = get_path(note, path) if parse_ok else _MISSING
        present = value is not _MISSING and value not in (None, '', [], {})
        results.append({'type': 'must_have_fields', 'path': path, 'text': None, 'passed': present})
    return results


def describe(check: dict) -> str:
    where = f'@{check["path"]}' if check.get('path') else ''
    sign = {'must_contain': '+', 'must_not_contain': '-', 'must_have_fields': 'field '}[check['type']]
    return f'{sign}{check["text"] or ""}{where}'


def check_records(records, scenarios_by_id) -> list[dict]:
    rows = []
    for rec in records:
        scenario = scenarios_by_id.get(rec['scenario'])
        if scenario is None:
            continue
        results = run_checks(rec.get('note'), scenario.checks, rec.get('parse_ok', False))
        passed = sum(r['passed'] for r in results)
        rows.append({
            'scenario': rec['scenario'], 'model': rec['model'], 'run': rec['run'],
            'http_status': rec.get('http_status'), 'parse_ok': rec.get('parse_ok', False),
            'valid': rec.get('valid', False), 'checks_total': len(results), 'checks_passed': passed,
            'pass_rate': round(passed / len(results), 3) if results else None,
            'failed_checks': '; '.join(describe(r) for r in results if not r['passed']),
        })
    return rows


def by_model(rows) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[r['model']].append(r)
    out = []
    for model, rs in sorted(groups.items()):
        total = sum(r['checks_total'] for r in rs)
        out.append({
            'model': model, 'runs': len(rs),
            'parse_ok_rate': round(sum(r['parse_ok'] for r in rs) / len(rs), 3),
            'valid_rate': round(sum(bool(r['valid']) for r in rs) / len(rs), 3),
            'checks_pass_rate': round(sum(r['checks_passed'] for r in rs) / total, 3) if total else None,
            'runs_all_checks_passed': sum(1 for r in rs if r['checks_total'] and r['checks_passed'] == r['checks_total']),
        })
    return out


# ── Consistency across the N runs ─────────────────────────────────────────

def _diagnosis(note):
    return normalize(get_path(note, 'evaluacion.diagnostico')) if isinstance(get_path(note, 'evaluacion.diagnostico'), str) else ''


def _medications(note):
    meds = get_path(note, 'plan.medicamentos')
    if not isinstance(meds, list):
        return ()
    return tuple(sorted(
        tuple(normalize(m.get(k, '')) for k in ('nombre', 'dosis', 'frecuencia', 'duracion')) if isinstance(m, dict)
        else (normalize(m), '', '', '') for m in meds))


def _followup(note):
    value = get_path(note, 'plan.seguimiento')
    return normalize(value) if isinstance(value, str) else ''


CONSISTENCY_FIELDS = {'diagnosis': _diagnosis, 'medications': _medications, 'followup': _followup}


def agreement(values) -> float | None:
    """Share of runs that agree with the most common value. A run without a
    parsed note never agrees with anything."""
    if not values:
        return None
    real = [v for v in values if v is not _MISSING]
    if not real:
        return 0.0
    return round(Counter(real).most_common(1)[0][1] / len(values), 3)


def consistency(records) -> list[dict]:
    groups = defaultdict(list)
    for rec in records:
        groups[(rec['scenario'], rec['model'])].append(rec)
    rows = []
    for (sid, model), recs in sorted(groups.items()):
        row = {'scenario': sid, 'model': model, 'runs': len(recs),
               'parsed': sum(1 for r in recs if r.get('parse_ok') and isinstance(r.get('note'), dict))}
        scores = []
        for name, fn in CONSISTENCY_FIELDS.items():
            values = [fn(r['note']) if r.get('parse_ok') and isinstance(r.get('note'), dict) else _MISSING for r in recs]
            row[f'{name}_agreement'] = agreement(values)
            scores.append(row[f'{name}_agreement'])
        row['mean_agreement'] = round(sum(scores) / len(scores), 3)
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else [])
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run_check_command(records, scenarios, scores_dir: Path | None = None, log=print):
    scores_dir = scores_dir or paths.SCORES
    rows = check_records(records, {s.id: s for s in scenarios})
    write_csv(scores_dir / 'checks.csv', rows, ['scenario', 'model', 'run', 'http_status', 'parse_ok', 'valid',
                                                'checks_total', 'checks_passed', 'pass_rate', 'failed_checks'])
    summary = by_model(rows)
    write_csv(scores_dir / 'checks_by_model.csv', summary, ['model', 'runs', 'parse_ok_rate', 'valid_rate',
                                                            'checks_pass_rate', 'runs_all_checks_passed'])
    cons = consistency(records)
    write_csv(scores_dir / 'consistency.csv', cons, ['scenario', 'model', 'runs', 'parsed', 'diagnosis_agreement',
                                                     'medications_agreement', 'followup_agreement', 'mean_agreement'])
    log(f'{len(rows)} runs checked → {scores_dir / "checks.csv"}')
    for s in summary:
        log(f'  {s["model"]:28} checks {s["checks_pass_rate"] if s["checks_pass_rate"] is not None else "-":>5}  '
            f'parsed {s["parse_ok_rate"]:.0%}  valid {s["valid_rate"]:.0%}')
    log(f'consistency → {scores_dir / "consistency.csv"}')
    return rows, summary, cons
