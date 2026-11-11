"""transcribe: production's AssemblyAI settings, checked against
backend/transcription.py WITHOUT importing it (it imports config)."""
import ast
import json

import assemblyai as aai
import pytest
from assemblyai import api as aai_api
from assemblyai import types as aai_types

from bakeoff import asr, paths
from bakeoff.scenarios import Scenario


def _production_settings():
    tree = ast.parse(paths.PRODUCTION_TRANSCRIPTION.read_text(encoding='utf-8'))
    speech_models = custom = kwargs = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'SPEECH_MODELS' for t in node.targets):
            speech_models = ast.literal_eval(node.value)
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'TranscriptionConfig':
            kwargs = {}
            for kw in node.keywords:
                if kw.arg is None:                 # **({'speakers_expected': ...} if ... else {})
                    kwargs['**'] = ast.unparse(kw.value)
                elif isinstance(kw.value, ast.Constant):
                    kwargs[kw.arg] = kw.value.value
                else:
                    kwargs[kw.arg] = ast.unparse(kw.value)
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'set_custom_spelling':
            custom = ast.literal_eval(node.args[0])
    return speech_models, kwargs, custom


def test_settings_equal_productions():
    speech_models, kwargs, custom = _production_settings()
    assert asr.SPEECH_MODELS == speech_models == ['universal-3-5-pro']
    assert asr.CUSTOM_SPELLING == custom
    constants = {k: v for k, v in kwargs.items() if k not in ('speech_models', '**')}
    assert asr.TRANSCRIPTION_SETTINGS == constants
    assert kwargs['speech_models'] == 'list(SPEECH_MODELS)'
    assert kwargs['**'] == "{'speakers_expected': speakers_expected} if speakers_expected > 0 else {}"


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    st = {'bodies': [], 'deleted': [], 'status': 'completed'}

    def response():
        return aai_types.TranscriptResponse.parse_obj({
            'id': 'transcript-ficticio', 'status': st['status'], 'audio_url': 'https://example.test/a.m4a',
            'text': 'texto ficticio', 'language_code': 'es', 'speech_model_used': 'universal-3-5-pro',
            'error': 'fallo ficticio' if st['status'] == 'error' else None,
            'utterances': [{'speaker': 'A', 'text': 'Buenos días ficticios.', 'confidence': 0.9, 'start': 0, 'end': 900,
                            'words': []},
                           {'speaker': 'B', 'text': 'Me duele la espalda.', 'confidence': 0.9, 'start': 900, 'end': 1800,
                            'words': []}]})

    def create_transcript(client, request):
        st['bodies'].append(request.dict(exclude_none=True, by_alias=True))
        return response()
    monkeypatch.setattr(aai_api, 'create_transcript', create_transcript)
    monkeypatch.setattr(aai_api, 'get_transcript', lambda client, transcript_id: response())
    monkeypatch.setattr(aai_api, 'upload_file', lambda client, audio_file: 'https://example.test/a.m4a')
    monkeypatch.setattr(aai.Transcript, 'delete_by_id', classmethod(lambda cls, tid: st['deleted'].append(tid)))
    audio_dir = tmp_path / 'audio'
    (audio_dir / 'BO-T2').mkdir(parents=True)
    (audio_dir / 'BO-T2' / 'ficticio.m4a').write_bytes(b'audio ficticio')
    st['settings'] = {'ASSEMBLYAI_API_KEY': 'clave-ficticia', 'BAKEOFF_AUDIO_DIR': str(audio_dir)}
    st['scenario'] = Scenario(id='BO-T2', title='t', path=tmp_path / 'x.yaml', audio='BO-T2/ficticio.m4a',
                              speakers_expected=2)
    return st


def test_transcribe_sends_production_body_caches_and_deletes(sdk, tmp_path):
    out = asr.transcribe_scenario(sdk['scenario'], sdk['settings'], cache_dir=tmp_path / 'cache', log=lambda *a: None)
    assert out == 'transcribed'
    body = sdk['bodies'][0]
    assert body['speech_models'] == ['universal-3-5-pro'] and 'speech_model' not in body
    assert body['language_code'] == 'es' and body['speaker_labels'] is True and body['domain'] == 'medical-v1'
    assert body['punctuate'] is True and body['format_text'] is True and body['speakers_expected'] == 2
    assert body['custom_spelling'] == [{'from': ['esquinza'], 'to': 'esguince'}]
    assert sdk['deleted'] == ['transcript-ficticio']
    cached = json.loads((tmp_path / 'cache' / 'BO-T2.json').read_text(encoding='utf-8'))
    assert cached['utterances'] == [{'speaker': 'A', 'text': 'Buenos días ficticios.'},
                                    {'speaker': 'B', 'text': 'Me duele la espalda.'}]
    assert cached['speech_model_used'] == 'universal-3-5-pro'


def test_cached_scenario_not_transcribed_again_unless_forced(sdk, tmp_path):
    kw = dict(cache_dir=tmp_path / 'cache', log=lambda *a: None)
    asr.transcribe_scenario(sdk['scenario'], sdk['settings'], **kw)
    assert asr.transcribe_scenario(sdk['scenario'], sdk['settings'], **kw) == 'cached'
    assert len(sdk['bodies']) == 1
    assert asr.transcribe_scenario(sdk['scenario'], sdk['settings'], force=True, **kw) == 'transcribed'
    assert len(sdk['bodies']) == 2


def test_failed_transcription_is_still_deleted_and_not_cached(sdk, tmp_path):
    sdk['status'] = 'error'
    with pytest.raises(RuntimeError):
        asr.transcribe_scenario(sdk['scenario'], sdk['settings'], cache_dir=tmp_path / 'cache', log=lambda *a: None)
    assert sdk['deleted'] == ['transcript-ficticio'] and not (tmp_path / 'cache').exists()


def test_speakers_expected_zero_is_not_sent(sdk, tmp_path):
    sdk['scenario'].speakers_expected = 0
    asr.transcribe_scenario(sdk['scenario'], sdk['settings'], cache_dir=tmp_path / 'cache', log=lambda *a: None)
    assert 'speakers_expected' not in sdk['bodies'][0]


def test_text_scenario_is_not_transcribed(text_scenario, sdk):
    assert asr.transcribe_scenario(text_scenario, sdk['settings'], log=lambda *a: None) == 'skipped'
    assert sdk['bodies'] == []
