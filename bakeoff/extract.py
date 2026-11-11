"""`extract`: scenario × model × run calls to the Gateway; one file per call.

No CIE-11/WHO lookup here: termino_busqueda_cie11 stays as the model wrote it.
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from bakeoff import core, gateway, paths
from bakeoff.estimate import Line, format_estimate
from bakeoff.settings import budget_usd, require


class BudgetExceeded(RuntimeError):
    pass


# How much of a failed call's error body is printed on the console (the
# full body is always in the run file's raw_error_body).
ERROR_PREVIEW_CHARS = 300


def run_path(scenario_id: str, model_name: str, run: int, runs_dir: Path | None = None) -> Path:
    safe = model_name.replace('@', '_at_')
    return (runs_dir or paths.RUNS) / scenario_id / f'{safe}__run{run}.json'


def is_done(path: Path) -> bool:
    """A finished call (HTTP 200 with content) is never repeated; failures are."""
    if not path.exists():
        return False
    try:
        rec = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return False
    return rec.get('http_status') == 200 and rec.get('raw_response_text') is not None


def parse_and_validate(text: str | None) -> dict:
    result = {'parse_ok': False, 'parse_error': None, 'note': None, 'valid': False, 'validation_error': None}
    if text is None:
        result['parse_error'] = 'no content'
        return result
    try:
        note = json.loads(core.clean_json_response(text))
    except ValueError as e:
        result['parse_error'] = f'{type(e).__name__}: {e}'
        return result
    result['parse_ok'], result['note'] = True, note
    try:
        # On a copy: the validator normalises in place; the run keeps what the
        # model actually returned.
        valid, error = core.validate_against_schema(copy.deepcopy(note))
    except Exception as e:      # e.g. the model returned a list, not an object
        valid, error = False, f'{type(e).__name__}: {e}'
    result['valid'], result['validation_error'] = valid, error
    return result


def extract(scenarios, specs, runs, models_cfg, settings, utterances_for, force=False,
            runs_dir=None, post=None, sleep=None, log=print) -> list[Path]:
    api_key = require(settings, 'ASSEMBLYAI_API_KEY')
    planned_done = (lambda s, m, r: False) if force else \
        (lambda s, m, r: is_done(run_path(s.id, m.name, r, runs_dir)))
    # The budget check uses the real prompts these calls will send.
    prompts = {s.id: core.build_prompt(utterances_for(s)) for s in scenarios}
    # Only a json_schema model needs the wire schema (roles keys = the scenario's speakers).
    needs_schema = any(spec.output_mode == 'json_schema' for spec in specs)
    schemas = {s.id: core.gateway_schema(utterances_for(s)) if needs_schema else None for s in scenarios}
    lines = []
    for spec in specs:
        calls = sum(1 for s in scenarios for r in range(1, runs + 1) if not planned_done(s, spec, r))
        tin = sum(len(prompts[s.id]) / models_cfg.chars_per_token
                  for s in scenarios for r in range(1, runs + 1) if not planned_done(s, spec, r))
        tout = calls * spec.est_output_tokens
        lines.append(Line(spec.name, calls, tin, tout, models_cfg.cost(spec, tin, tout),
                          models_cfg.cost(spec, tin, calls * gateway.MAX_TOKENS)))
    budget = budget_usd(settings)
    sizes = {s.id: (len(prompts[s.id]), 'transcript') for s in scenarios}
    log(format_estimate(sizes, lines, models_cfg, budget))
    total = sum(l.cost for l in lines)
    if total > budget:
        raise BudgetExceeded(f'estimated {total:.4f} USD exceeds BAKEOFF_BUDGET_USD={budget:.2f} — nothing was sent')

    written = []
    for s in scenarios:
        prompt = prompts[s.id]
        for spec in specs:
            body = gateway.build_request(spec, prompt, schemas[s.id])
            for r in range(1, runs + 1):
                target = run_path(s.id, spec.name, r, runs_dir)
                if not force and is_done(target):
                    log(f'{s.id} {spec.name} run {r}: already done — skipped')
                    continue
                kwargs = {'post': post}
                if sleep is not None:
                    kwargs['sleep'] = sleep
                res = gateway.call_gateway(body, api_key, **kwargs)
                text = gateway.content_of(res['body'])
                usage = gateway.usage_of(res['body'])
                cost = None
                if usage['input_tokens'] is not None and usage['output_tokens'] is not None:
                    cost = round(models_cfg.cost(spec, usage['input_tokens'], usage['output_tokens']), 6)
                pv = parse_and_validate(text if res['http_status'] == 200 else None)
                record = {
                    'scenario': s.id, 'model': spec.name, 'gateway_model': spec.model, 'run': r,
                    'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'request': gateway.request_params(body),
                    'sent_temperature': body.get('temperature'),
                    'output_mode': spec.output_mode,
                    'http_status': res['http_status'], 'attempts': res['attempts'],
                    'latency_s': res['latency_s'], 'error': res['error'],
                    'request_id': (res['body'] or {}).get('request_id') if isinstance(res['body'], dict) else None,
                    'finish_reason': _finish_reason(res['body']),
                    'usage': usage, 'cost_usd': cost,
                    'raw_response_text': text,
                    'raw_error_body': res['raw_body'] if res['http_status'] != 200 else None,
                    **pv,
                }
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                written.append(target)
                status = 'ok' if pv['parse_ok'] and pv['valid'] else (res['error'] or pv['parse_error'] or pv['validation_error'])
                if res['http_status'] != 200 and res['raw_body']:
                    # The provider's reason (e.g. a rejected schema keyword), one line.
                    status += ': ' + ' '.join(res['raw_body'].split())[:ERROR_PREVIEW_CHARS]
                log(f'{s.id} {spec.name} run {r}: {status} ({res["latency_s"]:.1f} s, {cost if cost is not None else "?"} USD)')
    return written


def _finish_reason(body):
    try:
        return body['choices'][0].get('finish_reason')
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
