"""Reading run files back."""
import json
from pathlib import Path

from bakeoff import paths


def load_runs(runs_dir: Path | None = None, scenarios=None, models=None) -> list[dict]:
    runs_dir = runs_dir or paths.RUNS
    records = []
    if not runs_dir.exists():
        return records
    for f in sorted(runs_dir.glob('*/*.json')):
        rec = json.loads(f.read_text(encoding='utf-8'))
        if scenarios and rec['scenario'] not in scenarios:
            continue
        if models and rec['model'] not in models:
            continue
        rec['_file'] = str(f.relative_to(runs_dir))
        records.append(rec)
    return records
