# backend/tests/test_transcription_speech_model.py
# #89 (0.8.10): the AssemblyAI request pins speech_models=["universal-3-5-pro"]
# and keeps every other setting. The real SDK (assemblyai 0.64.2) builds the
# request from our TranscriptionConfig; only its HTTP calls
# (assemblyai.api.create_transcript / get_transcript / delete_transcript) are
# replaced, so what's asserted is the exact body the SDK would POST to
# /v2/transcript. No network call. Invented data only.

import io
import logging

import assemblyai as aai
import pytest
from assemblyai import api as aai_api
from assemblyai import types as aai_types

import transcription
from logger import logger

AUDIO_URL = 'https://example.test/consulta-ficticia.webm'   # a URL: the SDK skips the upload step
FAKE_TEXT = 'texto ficticio de la consulta de prueba'


@pytest.fixture
def sdk(monkeypatch):
    """Capture the outgoing request bodies; answer as a completed transcript."""
    st = {'bodies': [], 'deleted': [], 'speech_model_used': 'universal-3-5-pro'}

    def response():
        return aai_types.TranscriptResponse.parse_obj({
            'id': 'transcript-fixture', 'status': 'completed', 'audio_url': AUDIO_URL,
            'text': FAKE_TEXT, 'confidence': 0.93, 'audio_duration': 61, 'language_code': 'es',
            'speech_models': ['universal-3-5-pro'], 'speech_model_used': st['speech_model_used'],
            'words': [], 'utterances': [
                {'speaker': 'A', 'text': FAKE_TEXT, 'confidence': 0.9, 'start': 0, 'end': 1000, 'words': []}],
        })

    def create_transcript(client, request):
        # Exactly what assemblyai.api.create_transcript would send.
        st['bodies'].append(request.dict(exclude_none=True, by_alias=True))
        return response()

    monkeypatch.setattr(aai_api, 'create_transcript', create_transcript)
    monkeypatch.setattr(aai_api, 'get_transcript', lambda client, transcript_id: response())
    monkeypatch.setattr(aai.Transcript, 'delete_by_id', classmethod(lambda cls, tid: st['deleted'].append(tid)))
    return st


@pytest.fixture
def log():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger.addHandler(handler)
    yield buf
    logger.removeHandler(handler)


def _transcribe(speakers_expected=2):
    return transcription.TranscriptionService().transcribe_audio(AUDIO_URL, speakers_expected=speakers_expected)


def test_request_pins_universal_3_5_pro(sdk):
    _transcribe()
    assert len(sdk['bodies']) == 1
    body = sdk['bodies'][0]
    assert body['speech_models'] == ['universal-3-5-pro']
    # The deprecated singular parameter is never sent.
    assert 'speech_model' not in body


@pytest.mark.parametrize('speakers_expected', [2, 0])
def test_every_other_setting_is_unchanged(sdk, speakers_expected):
    _transcribe(speakers_expected)
    body = sdk['bodies'][0]
    assert body['audio_url'] == AUDIO_URL
    assert body['language_code'] == 'es'
    assert body['punctuate'] is True and body['format_text'] is True
    assert body['speaker_labels'] is True
    assert body['domain'] == 'medical-v1'
    assert body['custom_spelling'] == [{'from': ['esquinza'], 'to': 'esguince'}]
    if speakers_expected:
        assert body['speakers_expected'] == speakers_expected
    else:
        assert 'speakers_expected' not in body
    # Nothing beyond the settings above (plus the pinned model) is sent.
    assert set(body) <= {'audio_url', 'language_code', 'punctuate', 'format_text', 'speaker_labels',
                         'domain', 'custom_spelling', 'speakers_expected', 'speech_models'}


def test_transcript_is_still_deleted_after_use(sdk):
    result = _transcribe()
    assert sdk['deleted'] == ['transcript-fixture']
    assert result['transcript_id'] == 'transcript-fixture'
    assert result['text'] == FAKE_TEXT
    assert result['utterances'][0]['speaker'] == 'A'


def test_the_pinned_list_is_not_shared_mutable_state(sdk):
    _transcribe()
    sdk['bodies'][0]['speech_models'].append('universal-2')
    _transcribe()
    assert transcription.SPEECH_MODELS == ['universal-3-5-pro']
    assert sdk['bodies'][1]['speech_models'] == ['universal-3-5-pro']


@pytest.mark.parametrize('used,expected', [('universal-3-5-pro', 'universal-3-5-pro'),
                                           ('universal-2', 'universal-2'), (None, 'unknown')])
def test_logs_the_model_the_response_reports_and_never_the_text(sdk, log, used, expected):
    sdk['speech_model_used'] = used
    _transcribe()
    lines = [l for l in log.getvalue().splitlines() if l.startswith('ASR: ')]
    assert lines == [f'ASR: speech_model={expected}']
    assert 'ficticio' not in log.getvalue() and FAKE_TEXT not in log.getvalue()


def test_installed_sdk_types_speech_models_as_plain_strings():
    # Why no upgrade or raw passthrough was needed: in this SDK the plural
    # field is List[str], not the SpeechModel enum (which lacks this value).
    config = aai.TranscriptionConfig(speech_models=['universal-3-5-pro'])
    assert config.raw.speech_models == ['universal-3-5-pro']
    assert 'universal-3-5-pro' not in {m.value for m in aai.SpeechModel}
