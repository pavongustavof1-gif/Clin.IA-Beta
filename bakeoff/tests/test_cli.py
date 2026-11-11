"""estimate is a dry run: it works with no .env, no cache and no network."""
from bakeoff import cli, paths


def test_estimate_offline(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'ENV_FILE', tmp_path / 'missing.env')
    monkeypatch.setattr(paths, 'CACHE', tmp_path / 'cache')
    assert cli.main(['estimate', '--scenarios', 'BO-02,BO-06,BO-08', '--runs', '3']) == 0
    out = capsys.readouterr().out
    for m in ('gemini-2.5-flash', 'gemini-3.5-flash-lite', 'claude-haiku-5-5', 'claude-sonnet-5-5'):
        assert m in out
    assert 'gemini-3.5-flash-lite@low' not in out          # optional variant only when named
    assert 'TOTAL                           36' in out and 'WITHIN budget' in out


def test_extract_refuses_without_cached_transcripts(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'CACHE', tmp_path / 'cache')
    assert cli.main(['extract', '--scenarios', 'BO-02']) == 1
    assert 'transcribe' in capsys.readouterr().err


def test_unknown_model_is_an_error(capsys):
    assert cli.main(['estimate', '--models', 'gpt-imaginario']) == 1
