"""Scenario YAMLs (bakeoff/inputs/scenarios/*.yaml) and their transcripts."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bakeoff import paths


class ScenarioError(RuntimeError):
    pass


@dataclass
class Scenario:
    id: str
    title: str
    path: Path
    audio: str | None = None
    transcript: str | None = None
    audio_minutes: float | None = None
    speakers_expected: int = 0
    consulta: str = ''
    checks: dict = field(default_factory=dict)

    @property
    def is_audio(self) -> bool:
        return self.audio is not None


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    sid = data.get('id')
    if not sid:
        raise ScenarioError(f'{path.name}: missing id')
    # Hard gate: the harness never runs anything not explicitly marked invented.
    if data.get('datos') != 'inventados':
        raise ScenarioError(f'{sid}: "datos: inventados" is required — refusing to run '
                            '(invented data only; never real patients)')
    audio, transcript = data.get('audio'), data.get('transcript')
    if bool(audio) == bool(transcript):
        raise ScenarioError(f'{sid}: give exactly one of audio or transcript')
    checks = data.get('checks') or {}
    for key in checks:
        if key not in ('must_contain', 'must_not_contain', 'must_have_fields'):
            raise ScenarioError(f'{sid}: unknown check type {key!r}')
    for key in ('must_contain', 'must_not_contain'):
        for item in checks.get(key) or []:
            if not isinstance(item, dict) or ('text' in item) == ('any_of' in item):
                raise ScenarioError(f'{sid}: each {key} item needs exactly one of text or any_of')
            if 'any_of' in item and (not isinstance(item['any_of'], list) or not item['any_of']
                                     or not all(isinstance(a, str) and a.strip() for a in item['any_of'])):
                raise ScenarioError(f'{sid}: {key} any_of must be a non-empty list of strings')
    return Scenario(
        id=sid, title=data.get('title', ''), path=path, audio=audio, transcript=transcript,
        audio_minutes=data.get('audio_minutes'), speakers_expected=int(data.get('speakers_expected') or 0),
        consulta=str(data.get('consulta') or ''), checks=checks,
    )


def load_scenarios(ids: list[str] | None = None, directory: Path | None = None) -> list[Scenario]:
    directory = directory or paths.SCENARIOS
    found = [load_scenario(p) for p in sorted(directory.glob('*.yaml'))]
    if ids:
        by_id = {s.id: s for s in found}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ScenarioError(f'unknown scenario(s): {", ".join(missing)}')
        found = [by_id[i] for i in ids]
    return found


_LINE = re.compile(r'^\[Hablante\s+([^\]]+)\]:\s?(.*)$')


def parse_transcript_text(text: str) -> list[dict]:
    """"[Hablante A]: ..." lines → [{"speaker": "A", "text": ...}]. A line
    without the prefix continues the previous utterance."""
    utterances = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _LINE.match(line.strip())
        if m:
            utterances.append({'speaker': m.group(1).strip(), 'text': m.group(2).strip()})
        elif utterances:
            utterances[-1]['text'] = (utterances[-1]['text'] + ' ' + line.strip()).strip()
        else:
            raise ScenarioError(f'transcript must start with "[Hablante X]:", got {line[:40]!r}')
    if not utterances:
        raise ScenarioError('transcript is empty')
    return utterances


def cache_path(scenario: Scenario, cache_dir: Path | None = None) -> Path:
    return (cache_dir or paths.CACHE) / f'{scenario.id}.json'


def load_utterances(scenario: Scenario, cache_dir: Path | None = None) -> list[dict]:
    """The transcript every model sees: the cached AssemblyAI result for audio
    scenarios, the parsed .txt for text scenarios."""
    if scenario.is_audio:
        cached = cache_path(scenario, cache_dir)
        if not cached.exists():
            raise ScenarioError(f'{scenario.id}: no cached transcript — run `python -m bakeoff transcribe` first')
        return json.loads(cached.read_text(encoding='utf-8'))['utterances']
    path = paths.INPUTS / scenario.transcript
    if not path.exists():
        raise ScenarioError(f'{scenario.id}: transcript file not found: {scenario.transcript}')
    return parse_transcript_text(path.read_text(encoding='utf-8'))
