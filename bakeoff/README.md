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
   `checks` (they are marked `# draft: confirm against cached transcript`).
3. **`estimate`**: dry run with no network: calls, tokens and cost per model.
4. **Smoke test**: `extract --scenarios BO-08 --runs 1` (4 calls) to confirm every
   model accepts the request (schema, parameters) before the full run.
5. **`extract`**: scenario × model × `--runs` (default 3). One file per call in
   `runs/`. Successful calls are never repeated (failed ones are), unless you pass
   `--force`. No CIE-11/WHO lookup: `termino_busqueda_cie11` stays as the model wrote it.
6. **`check`**: automated checks per run → `scores/checks.csv`,
   `scores/checks_by_model.csv`, and run-to-run consistency → `scores/consistency.csv`.
7. **`blind`**: one random run per model per scenario (seeded; the seed is saved),
   labelled A–D in a random order → `blind/index.html`. The key goes to `key/key.json`,
   and an empty `scores/blind_scores.csv` is created. Open the HTML, score, and **don't
   look at the key**. It refuses to overwrite an existing key or scores file unless you
   pass `--force`.
8. **Score** in `scores/blind_scores.csv`: completeness, accuracy/no inventions and
   clinical usefulness from 1 to 5; `safety_error` is y/n; plus a comment.
9. **`tally`**: everything → `scores/summary.md`, with one table per scenario and the
   overall ranking (fewest safety errors, then blind score, checks, cost).

Common options: `--scenarios BO-02,BO-08`, `--models claude-haiku-5-5,...`, `--runs N`.
The optional `gemini-3.5-flash-lite@low` variant only runs when named in `--models`.

## Scenarios

`inputs/scenarios/*.yaml` contain:
- `id`, `title`, `datos: inventados`;
- `audio:` (relative to `BAKEOFF_AUDIO_DIR`) **or** `transcript:` (relative to `inputs/`);
- `audio_minutes` (only used by `estimate` before transcription), `speakers_expected`,
  `consulta`;
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

Code, `models.yaml`, the scenario YAMLs and invented transcripts. **Never** `.env`,
`cache/`, `runs/`, `blind/`, `key/`, `scores/` or audio (all in `.gitignore`).

## Tests

All offline (sockets are blocked; HTTP and the AssemblyAI SDK are mocked):

```bash
bakeoff/venv/bin/python -m pytest bakeoff/tests -q
```

The backend suite (`backend/tests`) doesn't depend on `bakeoff/`.
