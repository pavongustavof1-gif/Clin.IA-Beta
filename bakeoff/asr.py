"""`transcribe`: one AssemblyAI run per audio scenario, with production's settings.

backend/transcription.py imports config (which loads the repo-root .env), so it
can't be imported here. Its settings are duplicated below instead, and
bakeoff/tests/test_asr.py parses transcription.py (without importing it) and
fails if they ever differ.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import assemblyai as aai

from bakeoff import paths
from bakeoff.scenarios import Scenario, ScenarioError, cache_path
from bakeoff.settings import require

# == backend/transcription.py (checked by test_asr.py) ======================
SPEECH_MODELS = ["universal-3-5-pro"]
TRANSCRIPTION_SETTINGS = {
    'language_code': 'es',
    'punctuate': True,
    'format_text': True,
    'speaker_labels': True,
    'domain': 'medical-v1',
}
CUSTOM_SPELLING = {
    "esguince": ["esquinza"],
}
# ===========================================================================


def build_config(speakers_expected: int) -> aai.TranscriptionConfig:
    """Same construction as TranscriptionService.transcribe_audio."""
    config = aai.TranscriptionConfig(
        **TRANSCRIPTION_SETTINGS,
        speech_models=list(SPEECH_MODELS),
        **({'speakers_expected': speakers_expected} if speakers_expected > 0 else {})
    )
    config.set_custom_spelling(CUSTOM_SPELLING)
    return config


def audio_path(scenario: Scenario, settings: dict) -> Path:
    base = Path(require(settings, 'BAKEOFF_AUDIO_DIR')).expanduser()
    path = base / scenario.audio
    if not path.exists():
        raise ScenarioError(f'{scenario.id}: audio not found: {path}')
    return path


def transcribe_scenario(scenario: Scenario, settings: dict, force: bool = False,
                        cache_dir: Path | None = None, log=print) -> str:
    """Returns 'cached', 'skipped' (text scenario) or 'transcribed'."""
    if not scenario.is_audio:
        log(f'{scenario.id}: text scenario — nothing to transcribe')
        return 'skipped'
    target = cache_path(scenario, cache_dir)
    if target.exists() and not force:
        log(f'{scenario.id}: cached ({target.name}); pass --force to transcribe again')
        return 'cached'

    path = audio_path(scenario, settings)
    aai.settings.api_key = require(settings, 'ASSEMBLYAI_API_KEY')
    config = build_config(scenario.speakers_expected)
    log(f'{scenario.id}: transcribing {path.name} ...')
    transcript = aai.Transcriber().transcribe(str(path), config=config)
    try:
        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(f'{scenario.id}: transcription failed: {transcript.error}')
        utterances = [{'speaker': u.speaker, 'text': u.text} for u in (transcript.utterances or [])]
        if not utterances:
            raise RuntimeError(f'{scenario.id}: AssemblyAI returned no utterances')
        record = {
            'scenario': scenario.id,
            'source': 'assemblyai',
            'audio': scenario.audio,
            'speakers_expected': scenario.speakers_expected,
            'speech_models': list(SPEECH_MODELS),
            'speech_model_used': getattr(transcript, 'speech_model_used', None),
            'transcribed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'utterances': utterances,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        log(f'{scenario.id}: {len(utterances)} utterances, speech_model={record["speech_model_used"]} → {target}')
        return 'transcribed'
    finally:
        # As production: don't leave the transcript on AssemblyAI's servers.
        if getattr(transcript, 'id', None):
            try:
                aai.Transcript.delete_by_id(transcript.id)
            except Exception as e:   # never block on deletion, but say so
                log(f'{scenario.id}: WARNING could not delete transcript {transcript.id}: {type(e).__name__}')
