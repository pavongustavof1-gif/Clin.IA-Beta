"""Settings come ONLY from bakeoff/.env.

Never backend/config.py, never the repo-root .env, never python-dotenv, and
never os.environ: a production key exported in the shell must not leak into
the bake-off. The file is parsed here and nothing is written to os.environ.
"""
from pathlib import Path

from bakeoff import paths

DEFAULT_BUDGET_USD = 5.0


class SettingsError(RuntimeError):
    pass


def parse_env_file(path: Path) -> dict:
    """KEY=VALUE lines; '#' comments and blank lines ignored; optional quotes."""
    values = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if key.startswith('export '):
            key = key[len('export '):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        elif ' #' in value:
            value = value.split(' #', 1)[0].rstrip()
        values[key] = value
    return values


def load_settings(path: Path | None = None) -> dict:
    """Read bakeoff/.env (or `path`, for tests). Missing file → empty settings;
    commands that need a key say so via require()."""
    path = path or paths.ENV_FILE
    if not path.exists():
        return {}
    return parse_env_file(path)


def require(settings: dict, key: str) -> str:
    value = settings.get(key, '').strip()
    if not value:
        raise SettingsError(f'{key} is not set in {paths.ENV_FILE} (see bakeoff/.env.example)')
    return value


def budget_usd(settings: dict) -> float:
    raw = settings.get('BAKEOFF_BUDGET_USD', '').strip()
    if not raw:
        return DEFAULT_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        raise SettingsError(f'BAKEOFF_BUDGET_USD must be a number, got {raw!r}')
