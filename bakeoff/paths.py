"""Filesystem layout. Every output directory is git-ignored (see .gitignore)."""
from pathlib import Path

BAKEOFF = Path(__file__).resolve().parent
ROOT = BAKEOFF.parent
BACKEND = ROOT / 'backend'

ENV_FILE = BAKEOFF / '.env'                  # the ONLY settings file the harness reads
MODELS_FILE = BAKEOFF / 'models.yaml'
INPUTS = BAKEOFF / 'inputs'
SCENARIOS = INPUTS / 'scenarios'
JUDGE_RUBRIC = INPUTS / 'judge_rubric.md'    # same rubric as the human sheet

CACHE = BAKEOFF / 'cache'                    # transcripts, one <id>.json per scenario
RUNS = BAKEOFF / 'runs'                      # one file per Gateway call
BLIND = BAKEOFF / 'blind'                    # HTML for blind scoring (no model names)
KEY = BAKEOFF / 'key'                        # which blind label is which model
SCORES = BAKEOFF / 'scores'                  # checks, consistency, blind and judge scores, summary
JUDGE = BAKEOFF / 'judge'                    # raw judge responses, one file per judge × note

EXTRACTION_CORE = BACKEND / 'extraction_core.py'
EXTRACTION_SCHEMA = BACKEND / 'extraction_schema.json'
PRODUCTION_TRANSCRIPTION = BACKEND / 'transcription.py'
