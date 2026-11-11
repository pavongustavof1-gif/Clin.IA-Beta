"""The production extraction core, loaded straight from backend/.

extraction_core.py is stdlib-only and reads no environment (#83, 0.8.11), so
it is loaded by file path rather than by putting backend/ on sys.path — that
way no other backend module (config, logger, ...) can ever be imported by
accident. Nothing here copies or alters the prompt.
"""
import importlib.util
import json
import sys

from bakeoff import paths


def _load_extraction_core():
    name = 'extraction_core'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, paths.EXTRACTION_CORE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


extraction_core = _load_extraction_core()

create_extraction_prompt = extraction_core.create_extraction_prompt
clean_json_response = extraction_core.clean_json_response
validate_against_schema = extraction_core.validate_against_schema


def load_schema() -> dict:
    return json.loads(paths.EXTRACTION_SCHEMA.read_text(encoding='utf-8'))


def gateway_schema() -> dict:
    """The schema as sent in response_format: the same document minus the
    JSON-Schema meta keys ($schema, $id), which describe the file rather than
    the output and which some providers reject. Fields and types unchanged."""
    schema = load_schema()
    return {k: v for k, v in schema.items() if k not in ('$schema', '$id')}


def build_prompt(utterances: list) -> str:
    """Exactly production's call: llm_processor passes the transcript text and
    the speaker-labeled utterances; with utterances present the text is not
    used by the prompt."""
    text = '\n'.join(u['text'] for u in utterances)
    return create_extraction_prompt(text, utterances)
