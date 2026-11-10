# Changelog

Notable changes to Clin.IA, newest first. Started with 0.8.10-beta; earlier
versions are described in their commit messages (`git log`).

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
