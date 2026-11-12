# Bake-off harness (#83)

Compares LLMs on **the exact production extraction prompt, parser and validator**
(`backend/extraction_core.py`, schema `backend/extraction_schema.json`) and the same
transcripts, through the AssemblyAI LLM Gateway. Notes are then scored blind.

> **Invented data only. Never real patients.** Every scenario YAML must contain
> `datos: inventados`, or nothing runs. Audio stays outside the repo.

## Setup (Python 3.11, its own venv)

```bash
/opt/homebrew/bin/python3.11 -m venv bakeoff/venv
bakeoff/venv/bin/pip install -r bakeoff/requirements.txt
cp bakeoff/.env.example bakeoff/.env      # then fill it in
```

`bakeoff/.env` is the **only** settings file the harness reads (never the repo-root
`.env`, never `backend/config.py`, never shell environment variables):

| Variable | Meaning |
|---|---|
| `ASSEMBLYAI_API_KEY` | key for transcription and the LLM Gateway |
| `BAKEOFF_AUDIO_DIR` | folder with the invented audio (e.g. the iCloud Bakeoff folder) |
| `BAKEOFF_BUDGET_USD` | `extract` aborts before the first call if the estimate exceeds it (default 5) |

Run every command from the repo root: `bakeoff/venv/bin/python -m bakeoff <command>`.

## Order of commands

1. **`transcribe`**: one AssemblyAI run per audio scenario with production's exact
   settings (es, speaker labels, `speakers_expected`, `medical-v1`, the custom
   spelling, `speech_models=["universal-3-5-pro"]`). The transcript is deleted from
   AssemblyAI afterwards, as in production. Cached in `cache/<id>.json`; never
   repeated unless you pass `--force`. Text scenarios need no transcription.
2. **Confirm the checks.** Read each `cache/<id>.json` and fix the scenario's
   `checks`. BO-02/06/08 are already confirmed; a new scenario's checks start as a
   draft until compared with its cached transcript.
3. **`estimate`**: dry run with no network: calls, tokens and cost per model, then per judge
   (`--judges`; the real blind notes once `blind/` exists, else one note per model per scenario).
4. **Smoke test**: `extract --scenarios BO-08 --runs 1` (4 calls) to confirm every
   model accepts the request (schema, parameters) before the full run.
5. **`extract`**: scenario × model × `--runs` (default 3). One file per call in
   `runs/`. Successful calls are never repeated (failed ones are), unless you pass
   `--force`; output cut off at `max_tokens` (`finish_reason` `max_tokens`/`length`)
   also counts as failed. No CIE-11/WHO lookup: `termino_busqueda_cie11` stays as the model wrote it.
   How JSON is requested is each model's `output_mode` in `models.yaml` (`none`: no
   `response_format`, the prompt's JSON template only, as production sends no schema;
   `json_object`; `json_schema`: `extraction_core.wire_schema`). All four models use
   `none`; the mode is recorded in every run file and shown by `tally`.
6. **`check`**: automated checks per run → `scores/checks.csv`,
   `scores/checks_by_model.csv`, and run-to-run consistency → `scores/consistency.csv`.
7. **`blind`**: one random run per model per scenario (seeded; the seed is saved),
   labelled A–D in a random order → `blind/index.html`. The key goes to `key/key.json`,
   and an empty `scores/blind_scores.csv` is created. Open the HTML, score, and **don't
   look at the key**. It refuses to overwrite an existing key or scores file unless you
   pass `--force`.
8. **Score** in `scores/blind_scores.csv` with the rubric in `inputs/judge_rubric.md`:
   completeness, accuracy/no inventions and clinical usefulness from 1 to 5;
   `safety_error` is y/n; plus a comment.
9. **`judge`** (optional, `--judges`, `--scenarios`, `--seed`, `--force`): each judge in
   `models.yaml` `judges` scores every blind note, **one note per call**, with the same
   rubric. A judge sees only the rubric, the scenario's `reference`, the cached transcript
   and that one note as shown in `blind/` (masked). Never `key/`, `runs/`, other notes or
   model names; a prompt containing a model/vendor name or the API key stops the command
   before anything is sent. Each judge goes through the notes in its own seeded random
   order (seed in `judge/order.json`, reused unless `--seed`). Same budget guard as
   `extract`. Raw responses → `judge/<judge>/<scenario>__<label>.json`; valid verdicts →
   `scores/judge_scores.csv`. A note is judged again only if its blind page changed or
   the last answer wasn't a valid verdict (including one cut off at `max_tokens`).
10. **`tally`**: everything → `scores/summary.md`, with one table per scenario and the
   overall ranking (fewest safety errors, then blind score, checks, cost). Human scores
   stay primary; a note without them uses the judges' mean, and counts as a safety error
   if any judge flagged one. With judge scores it adds each judge per model, the notes
   where the judges differ by ≥2 points on a criterion or on safety, and a calibration
   table against the human scores (mean absolute difference, safety agreement).

Common options: `--scenarios BO-02,BO-08`, `--models claude-haiku-5-5,...`, `--runs N`.
The optional `@low` variants (`gemini-3.5-flash-lite@low`, `claude-haiku-5-5@low`,
`claude-sonnet-5-5@low`; `reasoning_effort: low`) only run when named in `--models`.
Each model has its own read timeout (`timeout_s`: 120 s Gemini, 300 s Claude) and output
cap (`max_tokens`: default 8192, 16384 for `gemini-2.5-flash`, whose thinking counts toward
it). Both are recorded in every run file, with `usage.reasoning_tokens` when the response
reports it.

## Scenarios

`inputs/scenarios/*.yaml` contain:
- `id`, `title`, `datos: inventados`;
- `audio:` (relative to `BAKEOFF_AUDIO_DIR`) **or** `transcript:` (relative to `inputs/`);
- `audio_minutes` (only used by `estimate` before transcription), `speakers_expected`,
  `consulta`;
- `reference`: what the doctor meant, including known mis-hearings in the transcript
  (for the judge; the ideal note doesn't turn them into facts);
- `checks`:

```yaml
checks:
  must_contain:      [{text: "nitrofurantoína"}, {path: plan.medicamentos, text: "100 mg"},
                      {any_of: ["5 días", "cinco días"]}]
  must_not_contain:  [{path: plan.medicamentos, text: "ibuprofeno"}]
  must_have_fields:  [evaluacion.diagnostico]
```

Each `must_contain` / `must_not_contain` item has `text` **or** `any_of` (alternative
wordings): `must_contain` passes if any alternative is found; `must_not_contain` fails
if any is found. Matching ignores case and accents; `path` is dotted, and numeric
parts index lists.
Text scenarios: `inputs/transcripts/<id>.txt`, one `[Hablante A]: …` line per utterance.

## What gets committed

Code, `models.yaml`, the scenario YAMLs, the judge rubric and invented transcripts.
**Never** `.env`, `cache/`, `runs/`, `blind/`, `key/`, `judge/`, `scores/` or audio (all in
`.gitignore`).

## Tests

All offline (sockets are blocked; HTTP and the AssemblyAI SDK are mocked):

```bash
bakeoff/venv/bin/python -m pytest bakeoff/tests -q
```

The backend suite (`backend/tests`) doesn't depend on `bakeoff/`.
