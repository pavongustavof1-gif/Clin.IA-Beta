# Changelog

Notable changes to Clin.IA, newest first. Started with 0.8.10-beta; earlier
versions are described in their commit messages (`git log`).

## 0.8.11-beta — 2026-11-11

### Changed
- **Extraction split into a pure module for the bake-off** (#83). The prompt
  (`create_extraction_prompt`), the JSON cleaner (`clean_json_response`), the
  validator (`validate_against_schema`, with `normalize_evaluacion`) and the pure
  CIE-11 term helpers (`accept_search_term`, `CIE11_TERM_KEY`, `content_words`)
  moved verbatim from `llm_processor.py` / `icd_service.py` into
  `backend/extraction_core.py`, which imports only the standard library and reads
  no environment. Production imports them from there, so the bake-off harness and
  production run the exact same prompt, parser and validator. **No behaviour
  change:** the prompt is pinned byte-for-byte by golden files captured before
  the move; the model call, temperature and parsing are untouched.

### Added
- `backend/extraction_schema.json`: a JSON Schema (draft 2020-12) of the model
  output the prompt asks for. Every field optional (no `required`), so it is
  compatible with a `strict:false` response format; it includes
  `termino_busqueda_cie11` and excludes the CIE-11 code fields the model must not
  produce.

### Removed
- `backend/schema.json`: stale and unused (nothing has loaded it since Stage E1).

## 0.8.10-beta — 2026-11-10

### Changed
- **Transcription: the AssemblyAI speech model is pinned to `universal-3-5-pro`** (#89).
  The request now sends `speech_models=["universal-3-5-pro"]` instead of relying on
  AssemblyAI's default (`["universal-3-5-pro", "universal-2"]`), so transcripts don't
  shift if that default changes. Every other setting is unchanged: language `es`,
  speaker labels, `speakers_expected`, Medical Mode (`medical-v1`), the custom spelling
  and the deletion of the transcript from AssemblyAI after use.

### Added
- One INFO log line per transcription: `ASR: speech_model=<model>` — the model
  AssemblyAI reports in `speech_model_used` (or `unknown`). No transcript text.

### Dependencies
- None. `assemblyai` stays at 0.64.2: its `TranscriptionConfig` already accepts
  `speech_models` as a list of strings.
