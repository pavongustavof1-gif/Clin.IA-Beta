"""`judge`: an automated, blinded, pointwise judge of the blind notes.

What a judge sees, one note per call: the rubric (inputs/judge_rubric.md, the
same one the human uses), the scenario's `reference` facts, the cached
transcript and ONE note exactly as shown in blind/ — the text of that page's
note panel, already masked by `blind`. This module reads blind/ pages only:
never key/ (which label is which model), never runs/, never another note.
Every prompt is checked before anything is sent: a model or vendor name, or
the API key, anywhere in it stops the command.

Each judge goes through the notes in its own random order (seeded; the seed
is saved in judge/order.json and reused unless --seed is given). One raw file
per judge × note in judge/ (git-ignored); a note is judged again only if its
blind page changed (hash) or the last answer was not a valid verdict.
Valid verdicts → scores/judge_scores.csv, which `tally` reads.
"""
import csv
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from bakeoff import core, gateway, paths
from bakeoff.blind import VENDOR_WORDS, mask_model_names
from bakeoff.estimate import Line
from bakeoff.scenarios import ScenarioError
from bakeoff.settings import budget_usd, require

SCORE_FIELDS = ('completeness', 'accuracy_no_inventions', 'clinical_usefulness')
LIST_FIELDS = ('invented_facts', 'omissions', 'safety_issues')
CSV_COLUMNS = ['note_label', 'judge', *SCORE_FIELDS, 'safety_error', *LIST_FIELDS, 'comment']
LIST_SEP = ' | '                     # list items in one CSV cell
TRUNCATED = frozenset({'max_tokens', 'length'})
_NOTE_FILE = re.compile(r'^nota_([A-Z]+)\.html$')


class JudgeError(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BlindNote:
    label: str               # "BO-02/A", as in blind_scores.csv
    scenario: str
    path: Path
    sha256: str              # of the blind page; a new `blind` pick changes it
    text: str                # the note panel as text
    failed: bool             # the page says the call or parse failed: nothing to judge


class _NotePanel(HTMLParser):
    """The second <section class="panel"> of a blind page (the note), as
    indented text: h3 → "## Sección", dt/dd → "- Campo: valor", li → "•".
    The panel's own heading and the A/B/C navigation are left out."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections, self.in_note, self.skip, self.depth = 0, False, 0, 0
        self.failed, self.out, self.divs = False, [], []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get('class') or ''
        if tag == 'section' and 'panel' in cls.split():
            self.sections += 1
            self.in_note = self.sections == 2
            return
        if not self.in_note:
            return
        if tag in ('nav', 'h2'):
            self.skip += 1
        elif tag == 'h3':
            self.out.append('\n\n## ')
        elif tag == 'dl':
            self.depth += 1
        elif tag == 'dt':
            self.out.append('\n' + '  ' * (self.depth - 1) + '- ')
        elif tag == 'li':
            self.out.append('\n' + '  ' * self.depth + '• ')
        elif tag == 'div':
            med = 'med' in cls.split()
            self.divs.append(med)
            if med:                                      # one object of a list (e.g. a medication)
                self.out.append('\n' + '  ' * max(self.depth - 1, 0) + '  *')
                self.depth += 1
            elif 'fail' in cls.split():
                self.failed = True

    def handle_endtag(self, tag):
        if tag == 'section':
            self.in_note = False
        if not self.in_note:
            return
        if tag in ('nav', 'h2'):
            self.skip -= 1
        elif tag == 'dl':
            self.depth -= 1
        elif tag == 'dt':
            self.out.append(':')
        elif tag == 'div' and self.divs and self.divs.pop():
            self.depth -= 1

    def handle_data(self, data):
        if self.in_note and not self.skip:
            if self.out and self.out[-1] == ':':             # "- Campo:" then its value on the same line
                data = ' ' + data.lstrip()
            self.out.append(data)


def note_text_from_page(page_html: str) -> tuple[str, bool]:
    """(note text, failed) from a blind/<id>/nota_X.html page."""
    p = _NotePanel()
    p.feed(page_html)
    p.close()
    text = re.sub(r'\n{3,}', '\n\n', ''.join(p.out)).strip()
    return text, p.failed


def load_blind_notes(blind_dir: Path | None = None, scenario_ids=None) -> list[BlindNote]:
    blind_dir = blind_dir or paths.BLIND
    notes = []
    if not blind_dir.exists():
        return notes
    for sdir in sorted(d for d in blind_dir.iterdir() if d.is_dir()):
        if scenario_ids is not None and sdir.name not in scenario_ids:
            continue
        for f in sorted(sdir.glob('nota_*.html')):
            m = _NOTE_FILE.match(f.name)
            if not m:
                continue
            raw = f.read_bytes()
            text, failed = note_text_from_page(raw.decode('utf-8'))
            notes.append(BlindNote(label=f'{sdir.name}/{m.group(1)}', scenario=sdir.name, path=f,
                                   sha256=hashlib.sha256(raw).hexdigest(), text=text, failed=failed))
    return notes


def transcript_text(utterances) -> str:
    return '\n'.join(f'[Hablante {u["speaker"]}]: {u["text"]}' for u in utterances)


def build_judge_prompt(rubric: str, reference: str, utterances, note_text: str) -> str:
    return (f'{rubric.strip()}\n\n'
            f'## Hechos de referencia del escenario\n\n{reference.strip()}\n\n'
            f'## Transcripción de la consulta\n\n{transcript_text(utterances)}\n\n'
            f'## Nota a evaluar\n\n{note_text.strip()}\n\n'
            'Responda solo con el objeto JSON descrito en "Formato de respuesta".')


def forbidden_words(model_names) -> list[str]:
    return sorted({w.lower() for w in list(model_names) + VENDOR_WORDS if w}, key=len, reverse=True)


def check_blind(prompt: str, model_names, secrets=()) -> None:
    """Refuse a prompt that names a model/vendor or contains a secret."""
    low = prompt.lower()
    found = [w for w in forbidden_words(model_names) if w in low]
    if found:
        raise JudgeError(f'judge prompt would contain model/vendor name(s) {", ".join(found)} — nothing was sent')
    for s in secrets:
        if s and s in prompt:
            raise JudgeError('judge prompt would contain the API key — nothing was sent')


def _as_score(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v if isinstance(v, int) and 1 <= v <= 5 else None


def _as_safety(v):
    if isinstance(v, bool):
        return 'y' if v else 'n'
    s = str(v).strip().lower()
    return {'y': 'y', 'yes': 'y', 'si': 'y', 'sí': 'y', 's': 'y', 'true': 'y',
            'n': 'n', 'no': 'n', 'false': 'n'}.get(s)


def parse_judgment(text: str | None) -> dict:
    """The verdict JSON → {parse_ok, parse_error, judgment}. Scores must be
    integers 1-5, safety_error y/n (sí/no/true/false accepted), the three lists
    lists of strings, comment a string. Code fences are stripped as in
    production's clean_json_response; extra keys are ignored."""
    out = {'parse_ok': False, 'parse_error': None, 'judgment': None}
    if text is None:
        out['parse_error'] = 'no content'
        return out
    try:
        data = json.loads(core.clean_json_response(text))
    except ValueError as e:
        out['parse_error'] = f'{type(e).__name__}: {e}'
        return out
    if not isinstance(data, dict):
        out['parse_error'] = 'not a JSON object'
        return out
    j, errors = {}, []
    for f in SCORE_FIELDS:
        j[f] = _as_score(data.get(f))
        if j[f] is None:
            errors.append(f'{f} must be an integer 1-5')
    j['safety_error'] = _as_safety(data.get('safety_error'))
    if j['safety_error'] is None:
        errors.append('safety_error must be y or n')
    for f in LIST_FIELDS:
        v = data.get(f)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.append(f'{f} must be a list of strings')
        else:
            j[f] = [x.strip() for x in v if x.strip()]
    if not isinstance(data.get('comment'), str):
        errors.append('comment must be a string')
    else:
        j['comment'] = data['comment'].strip()
    if errors:
        out['parse_error'] = '; '.join(errors)
        return out
    out['parse_ok'], out['judgment'] = True, j
    return out


def judge_path(judge_name: str, label: str, judge_dir: Path | None = None) -> Path:
    sid, letter = label.split('/')
    return (judge_dir or paths.JUDGE) / judge_name.replace('@', '_at_') / f'{sid}__{letter}.json'


def is_judged(path: Path, sha256: str) -> bool:
    if not path.exists():
        return False
    try:
        rec = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return False
    return bool(rec.get('parse_ok')) and rec.get('blind_sha256') == sha256


def judge_order(labels, judges, seed=None, judge_dir: Path | None = None) -> dict:
    """{seed, order: {judge: [labels]}}, saved to judge/order.json. Without a
    seed the saved one is reused (else a new random one)."""
    judge_dir = judge_dir or paths.JUDGE
    meta_file = judge_dir / 'order.json'
    saved = json.loads(meta_file.read_text(encoding='utf-8')) if meta_file.exists() else {}
    if seed is None:
        seed = saved.get('seed', random.SystemRandom().randrange(2 ** 32))
    order = dict(saved.get('order', {})) if saved.get('seed') == seed else {}
    for j in judges:
        lst = sorted(labels)
        random.Random(f'{seed}:{j}').shuffle(lst)
        order[j] = lst
    meta = {'seed': seed, 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'order': order}
    judge_dir.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return meta


def _utterances_or_sized(scenario, utterances_for, models_cfg):
    try:
        return utterances_for(scenario)
    except ScenarioError:
        minutes = scenario.audio_minutes or 3
        return [{'speaker': 'A', 'text': 'x' * int(minutes * models_cfg.transcript_chars_per_audio_minute)}]


def judge_lines(prompt_sizes, judges, models_cfg) -> list[Line]:
    """prompt_sizes: {judge name: [prompt chars per planned call]}."""
    lines = []
    for spec in judges:
        sizes = prompt_sizes.get(spec.name, [])
        tin = sum(c / models_cfg.chars_per_token for c in sizes)
        tout = len(sizes) * spec.est_output_tokens
        lines.append(Line(spec.name, len(sizes), tin, tout, models_cfg.cost(spec, tin, tout),
                          models_cfg.cost(spec, tin, len(sizes) * spec.max_tokens)))
    return lines


def format_judge_estimate(lines, basis: str, models_cfg, budget=None) -> str:
    out = [f'Judge calls (one note per call; {basis}):',
           f'{"judge":28} {"calls":>5} {"in tok":>9} {"out tok":>9} {"est. USD":>9} {"worst USD":>10}']
    for l in lines:
        out.append(f'{l.model:28} {l.calls:5d} {l.input_tokens:9,.0f} {l.output_tokens:9,.0f} '
                   f'{l.cost:9.4f} {l.worst_cost:10.4f}')
    total = sum(l.cost for l in lines)
    out.append(f'{"JUDGES TOTAL":28} {sum(l.calls for l in lines):5d} {"":9} {"":9} {total:9.4f} '
               f'{sum(l.worst_cost for l in lines):10.4f}')
    out.append(f'Output = the judge\'s est_output_tokens; worst = every call at its max_tokens '
               f'(+{models_cfg.surcharge:.0%} in-region surcharge).')
    if budget is not None:
        out.append(f'Budget (BAKEOFF_BUDGET_USD): {budget:.2f} USD — judge estimate is '
                   f'{"WITHIN" if total <= budget else "OVER"} budget.')
    return '\n'.join(out)


def estimate_judges(scenarios, judges, models_cfg, notes_per_scenario: int, utterances_for,
                    blind_dir: Path | None = None, rubric: str | None = None) -> tuple[list[Line], str]:
    """For `estimate`: the real prompts for the notes in blind/ when they
    exist; otherwise `notes_per_scenario` notes of estimation.note_chars."""
    rubric = rubric if rubric is not None else paths.JUDGE_RUBRIC.read_text(encoding='utf-8')
    notes = load_blind_notes(blind_dir, {s.id for s in scenarios})
    sizes, from_blind = [], 0
    for s in scenarios:
        utts = _utterances_or_sized(s, utterances_for, models_cfg)
        mine = [n for n in notes if n.scenario == s.id and not n.failed]
        if mine:
            from_blind += 1
            sizes += [len(build_judge_prompt(rubric, s.reference, utts, n.text)) for n in mine]
        else:
            fake = 'x' * int(models_cfg.note_chars)
            sizes += [len(build_judge_prompt(rubric, s.reference, utts, fake))] * notes_per_scenario
    basis = (f'blind/ notes for {from_blind} of {len(scenarios)} scenario(s)' if from_blind
             else f'no blind/ yet: {notes_per_scenario} notes per scenario of ~{models_cfg.note_chars:,.0f} chars')
    return judge_lines({j.name: sizes for j in judges}, judges, models_cfg), basis


def write_judge_scores(scores_dir: Path | None = None, judge_dir: Path | None = None,
                       blind_dir: Path | None = None) -> Path:
    """Every valid verdict on a note still current in blind/ → judge_scores.csv."""
    scores_dir, judge_dir = scores_dir or paths.SCORES, judge_dir or paths.JUDGE
    current = {n.label: n.sha256 for n in load_blind_notes(blind_dir)}
    rows = []
    for f in sorted(judge_dir.glob('*/*.json')) if judge_dir.exists() else []:
        rec = json.loads(f.read_text(encoding='utf-8'))
        label = rec.get('note_label')
        if not rec.get('parse_ok') or current.get(label) != rec.get('blind_sha256'):
            continue
        j = rec['judgment']
        rows.append({'note_label': label, 'judge': rec['judge'], **{k: j[k] for k in SCORE_FIELDS},
                     'safety_error': j['safety_error'], **{k: LIST_SEP.join(j[k]) for k in LIST_FIELDS},
                     'comment': j['comment']})
    rows.sort(key=lambda r: (r['note_label'], r['judge']))
    scores_dir.mkdir(parents=True, exist_ok=True)
    out = scores_dir / 'judge_scores.csv'
    with out.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return out


def run_judge(scenarios, judges, models_cfg, settings, utterances_for, seed=None, force=False,
              blind_dir=None, judge_dir=None, scores_dir=None, rubric=None, post=None, sleep=None,
              log=print) -> Path:
    api_key = require(settings, 'ASSEMBLYAI_API_KEY')
    if not judges:
        raise JudgeError('no judges in models.yaml')
    rubric = rubric if rubric is not None else paths.JUDGE_RUBRIC.read_text(encoding='utf-8')
    by_id = {s.id: s for s in scenarios}
    notes = load_blind_notes(blind_dir, set(by_id))
    if not notes:
        raise JudgeError('no blind notes for these scenarios — run `python -m bakeoff blind` first')
    for n in [n for n in notes if n.failed]:
        log(f'{n.label}: the blind page shows a failed call — not judged')
    notes = [n for n in notes if not n.failed]
    names = models_cfg.all_names()
    prompts = {}
    for n in notes:
        s = by_id[n.scenario]
        if not s.reference:
            raise JudgeError(f'{s.id}: no `reference:` in its scenario YAML — the judge needs it')
        text, _ = mask_model_names(n.text, names)          # already masked by `blind`; never trust it
        prompts[n.label] = build_judge_prompt(rubric, s.reference, utterances_for(s), text)
        check_blind(prompts[n.label], names, secrets=(api_key,))

    meta = judge_order([n.label for n in notes], [j.name for j in judges], seed, judge_dir)
    by_label = {n.label: n for n in notes}
    plan = {j.name: [lab for lab in meta['order'][j.name]
                     if force or not is_judged(judge_path(j.name, lab, judge_dir), by_label[lab].sha256)]
            for j in judges}
    lines = judge_lines({j: [len(prompts[lab]) for lab in labs] for j, labs in plan.items()}, judges, models_cfg)
    budget = budget_usd(settings)
    log(format_judge_estimate(lines, f'{len(notes)} blind notes, seed {meta["seed"]}', models_cfg, budget))
    total = sum(l.cost for l in lines)
    if total > budget:
        raise BudgetExceeded(f'judge estimate {total:.4f} USD exceeds BAKEOFF_BUDGET_USD={budget:.2f} — nothing was sent')

    for spec in judges:
        for i, label in enumerate(meta['order'][spec.name], 1):
            note, target = by_label[label], judge_path(spec.name, label, judge_dir)
            if label not in plan[spec.name]:
                log(f'{spec.name} {label}: already judged — skipped')
                continue
            body = gateway.build_request(spec, prompts[label])
            kwargs = {'post': post}
            if sleep is not None:
                kwargs['sleep'] = sleep
            res = gateway.call_gateway(body, api_key, timeout=spec.timeout_s, **kwargs)
            text = gateway.content_of(res['body'])
            usage = gateway.usage_of(res['body'])
            cost = None
            if usage['input_tokens'] is not None and usage['output_tokens'] is not None:
                cost = round(models_cfg.cost(spec, usage['input_tokens'], usage['output_tokens']), 6)
            finish = _finish_reason(res['body'])
            pv = parse_judgment(text if res['http_status'] == 200 else None)
            if finish in TRUNCATED and pv['parse_ok']:
                pv = {'parse_ok': False, 'parse_error': f'cut off at max_tokens ({finish})', 'judgment': None}
            record = {
                'note_label': label, 'judge': spec.name, 'gateway_model': spec.model,
                'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'seed': meta['seed'], 'order_index': i, 'blind_file': note.path.name, 'blind_sha256': note.sha256,
                'request': gateway.request_params(body, 'judge prompt'), 'sent_temperature': body.get('temperature'),
                'max_tokens': body['max_tokens'], 'timeout_s': spec.timeout_s,
                'http_status': res['http_status'], 'attempts': res['attempts'], 'latency_s': res['latency_s'],
                'error': res['error'], 'finish_reason': finish, 'usage': usage, 'cost_usd': cost,
                'raw_response_text': text,
                'raw_error_body': res['raw_body'] if res['http_status'] != 200 else None,
                **pv,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            status = 'ok' if pv['parse_ok'] else (res['error'] or pv['parse_error'])
            log(f'{spec.name} {label} ({i}/{len(meta["order"][spec.name])}): HTTP {res["http_status"]} '
                f'{res["latency_s"]}s {status}')
    out = write_judge_scores(scores_dir, judge_dir, blind_dir)
    log(f'judge scores → {out}')
    return out


def _finish_reason(body):
    try:
        return body['choices'][0].get('finish_reason')
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
