"""Secrets come only from bakeoff/.env: never backend/config.py, never the
repo-root .env, never os.environ."""
import ast
import json
import subprocess
import sys

import pytest

from bakeoff import paths, settings

MODULES = ['bakeoff', 'bakeoff.paths', 'bakeoff.settings', 'bakeoff.core', 'bakeoff.scenarios',
           'bakeoff.models', 'bakeoff.asr', 'bakeoff.gateway', 'bakeoff.estimate', 'bakeoff.extract',
           'bakeoff.results', 'bakeoff.checks', 'bakeoff.blind', 'bakeoff.tally', 'bakeoff.cli']


def test_env_file_path_is_bakeoff_dot_env():
    assert paths.ENV_FILE == paths.BAKEOFF / '.env'
    assert paths.ENV_FILE != paths.ROOT / '.env'


def test_parse_env_file(tmp_path):
    f = tmp_path / '.env'
    f.write_text('# comentario\n\nASSEMBLYAI_API_KEY=clave-ficticia\nBAKEOFF_AUDIO_DIR="/tmp/audio ficticio"\n'
                 "export BAKEOFF_BUDGET_USD='2.5'\nX=valor # nota\nSIN_IGUAL\n")
    assert settings.parse_env_file(f) == {'ASSEMBLYAI_API_KEY': 'clave-ficticia',
                                          'BAKEOFF_AUDIO_DIR': '/tmp/audio ficticio',
                                          'BAKEOFF_BUDGET_USD': '2.5', 'X': 'valor'}


def test_shell_environment_is_never_used(tmp_path, monkeypatch):
    monkeypatch.setenv('ASSEMBLYAI_API_KEY', 'clave-de-produccion-en-el-shell')
    s = settings.load_settings(tmp_path / 'no-existe.env')
    assert s == {}
    with pytest.raises(settings.SettingsError):
        settings.require(s, 'ASSEMBLYAI_API_KEY')


def test_loading_does_not_touch_os_environ(tmp_path, monkeypatch):
    f = tmp_path / '.env'
    f.write_text('ASSEMBLYAI_API_KEY=clave-ficticia\n')
    monkeypatch.delenv('ASSEMBLYAI_API_KEY', raising=False)
    assert settings.load_settings(f)['ASSEMBLYAI_API_KEY'] == 'clave-ficticia'
    import os
    assert 'ASSEMBLYAI_API_KEY' not in os.environ


def test_budget_default_and_parse():
    assert settings.budget_usd({}) == 5.0
    assert settings.budget_usd({'BAKEOFF_BUDGET_USD': '0.5'}) == 0.5
    with pytest.raises(settings.SettingsError):
        settings.budget_usd({'BAKEOFF_BUDGET_USD': 'cinco'})


def test_no_bakeoff_module_imports_config_or_dotenv_or_reads_os_environ():
    for f in sorted(paths.BAKEOFF.glob('*.py')):
        src = f.read_text(encoding='utf-8')
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            else:
                continue
            for n in names:
                assert n.split('.')[0] not in ('config', 'dotenv', 'flask', 'supabase', 'llm_processor',
                                               'transcription', 'icd_service', 'logger'), f'{f.name} imports {n}'
        # Code, not prose: no os.environ / getenv / load_dotenv anywhere in the AST.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ('environ', 'getenv', 'load_dotenv'), f'{f.name}: .{node.attr}'
            if isinstance(node, ast.Name):
                assert node.id not in ('environ', 'getenv', 'load_dotenv'), f'{f.name}: {node.id}'


ISOLATION_SCRIPT = r'''
import builtins, io, json, os, sys
opened = []
_open = builtins.open
def spy(file, *a, **k):
    opened.append(os.path.abspath(str(file))); return _open(file, *a, **k)
builtins.open = io.open = spy
import pathlib
_read_text = pathlib.Path.read_text
def spy_read_text(self, *a, **k):
    opened.append(str(self.resolve())); return _read_text(self, *a, **k)
pathlib.Path.read_text = spy_read_text

root = sys.argv[1]
sys.path.insert(0, root)
import importlib
for m in json.loads(sys.argv[2]):
    importlib.import_module(m)
from bakeoff import settings, paths
paths.ENV_FILE = pathlib.Path(sys.argv[3])
s = settings.load_settings()
print(json.dumps({'opened': opened, 'settings': s, 'environ_has_key': 'ASSEMBLYAI_API_KEY' in os.environ,
                  'modules': sorted(sys.modules)}))
'''


def test_subprocess_reads_only_bakeoff_env(tmp_path):
    """Import every bakeoff module and load settings in a fresh interpreter with
    an empty environment, in a directory holding a decoy .env: the only .env
    opened is the bakeoff one, nothing reaches os.environ, and neither
    backend/config.py nor dotenv is ever loaded."""
    (tmp_path / '.env').write_text('ASSEMBLYAI_API_KEY=decoy-root-env\n')
    bakeoff_env = tmp_path / 'bakeoff.env'
    bakeoff_env.write_text('ASSEMBLYAI_API_KEY=clave-ficticia-bakeoff\n')
    script = tmp_path / 'iso.py'
    script.write_text(ISOLATION_SCRIPT)
    proc = subprocess.run([sys.executable, '-I', str(script), str(paths.ROOT), json.dumps(MODULES), str(bakeoff_env)],
                          cwd=tmp_path, env={}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    env_files = {p for p in out['opened'] if p.endswith('.env')}
    assert env_files == {str(bakeoff_env.resolve())}, env_files
    assert str(paths.ROOT / '.env') not in out['opened']
    assert str(paths.BACKEND / 'config.py') not in out['opened']
    assert out['settings'] == {'ASSEMBLYAI_API_KEY': 'clave-ficticia-bakeoff'}
    assert out['environ_has_key'] is False
    assert not {m.split('.')[0] for m in out['modules']} & {'config', 'dotenv', 'flask', 'supabase', 'llm_processor',
                                                             'transcription', 'icd_service', 'logger'}
