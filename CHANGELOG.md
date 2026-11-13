# Changelog

Notable changes to Clin.IA, newest first. Started with 0.8.10-beta; earlier
versions are described in their commit messages (`git log`).

## 0.9.0-beta — 2026-11-13

### Added
- **Extraction through the AssemblyAI LLM Gateway, behind a switch** (#81). New
  setting `LLM_BACKEND`: `gemini` (default; the Gemini path, unchanged) or
  `gateway` (`backend/llm_gateway.py`). With `gateway` the same extraction prompt
  goes, as one user message, to `claude-haiku-5-5` (`reasoning_effort` low,
  `max_tokens` 8192, no temperature, no `response_format`, never `model_region`)
  at `POST https://llm-gateway.assemblyai.com/v1/chat/completions` with the
  existing `ASSEMBLYAI_API_KEY` (no "Bearer"). Timeout 120 s; 429/5xx retried up to
  2 times with backoff (Retry-After honoured, capped at 30 s); timeouts are not
  retried; output cut off at `max_tokens` is a failure. The reply is parsed and
  validated with `extraction_core`, then the CIE-11 lookup runs unchanged. Failures
  show the doctor the same message as a Gemini failure. One log line per call:
  `LLM: backend=gateway model=… request_id=… in=… out=… reasoning=… latency=…
  status=… finish=…` — never prompt, output or error-body text. An unknown
  `LLM_BACKEND` logs an error and uses Gemini. Gemini stays as the fallback until
  it's removed after 1 Dec 2026.
- **One-time notice of the updated legal texts at login.** Before: acceptance was
  recorded only on the access-request form (`solicitudes_registro.terminos_version`);
  existing users were never asked again and a version change did nothing. Now a user
  who hasn't accepted the current `TERMINOS_VERSION` sees, at login on `/` and
  `/admin`, a modal (`static/terminos.js`) with what changed, links to the new
  documents and one "Acepto" button. `POST /api/terminos/aceptar {version}` records
  it as a row in the new table `aceptaciones_terminos` (user, clinic, version, date;
  history kept); after that it never appears again for that version. It blocks
  nothing: no route checks acceptance, and if recording fails the modal closes and
  comes back at the next login. `GET /api/session-check` reports
  `terminos: {version, aceptada}`.

### Changed
- **Aviso de Privacidad para Pacientes v1.3** (Noviembre 2026, vigente a partir del
  28 de noviembre de 2026): §4 now lists AssemblyAI for both transcription and the
  structuring of the clinical information (a language model AssemblyAI runs on
  Amazon Web Services, zero retention); the Google (Gemini API) paragraph is gone.
- **Términos de Servicio v1.3** (same dates): clause 12, second paragraph — substantial
  changes are notified by email ≥15 days ahead; continued use from the entry-into-force
  date means acceptance; a user who doesn't accept can end the Service under clause 11
  before that date without penalty (it no longer requires express acceptance in the
  platform).
- Both v1.2 texts stay published at `/legal/terminos/v1.2/` and
  `/legal/aviso-pacientes/v1.2/`, listed under "Versiones anteriores".
  `legal/build_legal.py` now gives each document its own version (`DOCS`) and
  publishes previous versions (`PREVIOUS`).
- `Config.TERMINOS_VERSION` is `ToS-1.3-Beta+AU-1.2-Beta+AP-1.3-Beta` (now names
  the patient aviso too; a test keeps it equal to `build_legal.version_code()`). New
  access requests store it as before. The access-request form also links the
  patient aviso.

### Deploy notes
- Before deploying, create the table (Supabase SQL editor):
  ```sql
  create table public.aceptaciones_terminos (
    id          uuid primary key default gen_random_uuid(),
    usuario_id  uuid not null references public.usuarios(id) on delete cascade,
    clinica_id  uuid not null,
    version     text not null,
    aceptado_at timestamptz not null default now(),
    unique (usuario_id, version)
  );
  alter table public.aceptaciones_terminos enable row level security;
  grant select, insert, update, delete on public.aceptaciones_terminos to service_role;
  ```
  Without it nothing breaks, but no acceptance is recorded: the session check can't
  read the table, so nobody sees the notice.
- The landing static site must be redeployed for the v1.3 / v1.2 pages.
- `LLM_BACKEND` is unset (Gemini) unless you set `LLM_BACKEND=gateway` on Render.

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
