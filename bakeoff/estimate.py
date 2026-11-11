"""`estimate`: dry run — planned calls, tokens and cost per model. No network."""
from dataclasses import dataclass

from bakeoff import core
from bakeoff.gateway import MAX_TOKENS
from bakeoff.scenarios import ScenarioError, load_utterances


@dataclass
class Line:
    model: str
    calls: int
    input_tokens: float
    output_tokens: float
    cost: float
    worst_cost: float          # every call hitting max_tokens


def prompt_chars(scenario, models_cfg, cache_dir=None) -> tuple[int, str]:
    """Real prompt size when the transcript is available, else sized from
    audio_minutes. Returns (chars, basis)."""
    try:
        return len(core.build_prompt(load_utterances(scenario, cache_dir))), 'transcript'
    except ScenarioError:
        template = len(core.build_prompt([{'speaker': 'A', 'text': ''}]))
        minutes = scenario.audio_minutes or 3
        return int(template + minutes * models_cfg.transcript_chars_per_audio_minute), f'~{minutes:g} min audio'


def estimate(scenarios, specs, runs, models_cfg, cache_dir=None, done=lambda s, m, r: False):
    sizes = {s.id: prompt_chars(s, models_cfg, cache_dir) for s in scenarios}
    lines = []
    for spec in specs:
        calls, tin, tout = 0, 0.0, 0.0
        for s in scenarios:
            chars, _ = sizes[s.id]
            for r in range(1, runs + 1):
                if done(s, spec, r):
                    continue
                calls += 1
                tin += chars / models_cfg.chars_per_token
                tout += spec.est_output_tokens
        lines.append(Line(spec.name, calls, tin, tout, models_cfg.cost(spec, tin, tout),
                          models_cfg.cost(spec, tin, calls * MAX_TOKENS)))
    return sizes, lines


def format_estimate(sizes, lines, models_cfg, budget=None) -> str:
    out = ['Prompt size per scenario:']
    for sid, (chars, basis) in sizes.items():
        out.append(f'  {sid}: {chars:,} chars ≈ {chars / models_cfg.chars_per_token:,.0f} input tokens ({basis})')
    out.append('')
    out.append(f'{"model":28} {"calls":>5} {"in tok":>9} {"out tok":>9} {"est. USD":>9} {"worst USD":>10}')
    for l in lines:
        out.append(f'{l.model:28} {l.calls:5d} {l.input_tokens:9,.0f} {l.output_tokens:9,.0f} '
                   f'{l.cost:9.4f} {l.worst_cost:10.4f}')
    total = sum(l.cost for l in lines)
    worst = sum(l.worst_cost for l in lines)
    out.append(f'{"TOTAL":28} {sum(l.calls for l in lines):5d} {"":9} {"":9} {total:9.4f} {worst:10.4f}')
    out.append(f'Prices: bakeoff/models.yaml (listed rate +{models_cfg.surcharge:.0%} in-region surcharge). '
               f'Tokens ≈ chars / {models_cfg.chars_per_token:g}; output = est_output_tokens; '
               f'worst = every call at max_tokens {MAX_TOKENS}.')
    if budget is not None:
        out.append(f'Budget (BAKEOFF_BUDGET_USD): {budget:.2f} USD — estimate is '
                   f'{"WITHIN" if total <= budget else "OVER"} budget.')
    return '\n'.join(out)
