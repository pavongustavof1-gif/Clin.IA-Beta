"""python -m bakeoff <command> — see bakeoff/README.md for the order.

Invented data only; never real patients. Every scenario must say
`datos: inventados` or nothing runs.
"""
import argparse
import sys

from bakeoff import paths
from bakeoff.models import load_models
from bakeoff.scenarios import ScenarioError, load_scenarios, load_utterances
from bakeoff.settings import SettingsError, budget_usd, load_settings


def _csv(value):
    return [v.strip() for v in value.split(',') if v.strip()] if value else None


def cmd_transcribe(args):
    from bakeoff.asr import transcribe_scenario
    settings = load_settings()
    for s in load_scenarios(_csv(args.scenarios)):
        transcribe_scenario(s, settings, force=args.force)


def cmd_estimate(args):
    from bakeoff.estimate import estimate, format_estimate
    models_cfg = load_models()
    scenarios = load_scenarios(_csv(args.scenarios))
    specs = models_cfg.select(_csv(args.models))
    sizes, lines = estimate(scenarios, specs, args.runs, models_cfg)
    print(format_estimate(sizes, lines, models_cfg, budget_usd(load_settings())))


def cmd_extract(args):
    from bakeoff.extract import extract
    models_cfg = load_models()
    scenarios = load_scenarios(_csv(args.scenarios))
    for s in scenarios:            # every transcript must exist before any call is paid for
        load_utterances(s)
    extract(scenarios, models_cfg.select(_csv(args.models)), args.runs, models_cfg, load_settings(),
            load_utterances, force=args.force)


def cmd_check(args):
    from bakeoff.checks import run_check_command
    from bakeoff.results import load_runs
    scenarios = load_scenarios(_csv(args.scenarios))
    run_check_command(load_runs(scenarios={s.id for s in scenarios}, models=_csv(args.models)), scenarios)


def cmd_blind(args):
    from bakeoff.blind import make_blind
    from bakeoff.results import load_runs
    models_cfg = load_models()
    scenarios = load_scenarios(_csv(args.scenarios))
    names = set(models_cfg.models) | {m.model for m in models_cfg.models.values()}
    make_blind(scenarios, load_runs(scenarios={s.id for s in scenarios}, models=_csv(args.models)),
               load_utterances, names, seed=args.seed, force=args.force)


def cmd_tally(args):
    from bakeoff.results import load_runs
    from bakeoff.tally import run_tally
    scenarios = load_scenarios(_csv(args.scenarios))
    run_tally(scenarios, load_runs(scenarios={s.id for s in scenarios}, models=_csv(args.models)))


def build_parser():
    p = argparse.ArgumentParser(prog='python -m bakeoff', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    def common(sp, models=True, runs=False):
        sp.add_argument('--scenarios', help='comma-separated ids (default: all in inputs/scenarios)')
        if models:
            sp.add_argument('--models', help='comma-separated names from models.yaml (default: non-optional)')
        if runs:
            sp.add_argument('--runs', type=int, default=3, help='runs per scenario x model (default 3)')

    sp = sub.add_parser('transcribe', help='AssemblyAI, production settings; cached per scenario')
    common(sp, models=False)
    sp.add_argument('--force', action='store_true', help='transcribe again even if cached')
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser('estimate', help='dry run: calls, tokens, cost per model (no network)')
    common(sp, runs=True)
    sp.set_defaults(func=cmd_estimate)

    sp = sub.add_parser('extract', help='call the LLM Gateway; one file per call in runs/')
    common(sp, runs=True)
    sp.add_argument('--force', action='store_true', help='repeat calls that already succeeded')
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser('check', help='automated checks + consistency → scores/')
    common(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser('blind', help='one random run per model per scenario → blind/ (no model names)')
    common(sp)
    sp.add_argument('--seed', type=int, help='seed for the pick and labels (default: random, saved in key)')
    sp.add_argument('--force', action='store_true', help='replace an existing key/blind_scores.csv')
    sp.set_defaults(func=cmd_blind)

    sp = sub.add_parser('tally', help='everything → scores/summary.md')
    common(sp)
    sp.set_defaults(func=cmd_tally)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ScenarioError, SettingsError, ValueError, RuntimeError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    return 0
