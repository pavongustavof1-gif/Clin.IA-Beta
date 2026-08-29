# Clin.IA — AI Medical Note Taker (Beta)

An intelligent Spanish-language medical note-taking system for clinics: doctors record or upload
consultation audio, and the app transcribes it and extracts a structured, SOAP-format clinical
note for review, correction, and confirmation before it's signed.

## 🚀 Features

- **Audio Recording**: Record consultations directly from the browser (desktop/mobile), or upload
  a file (WAV, MP3, WEBM, OGG, M4A)
- **Spanish Transcription**: Speaker-diarized transcription via AssemblyAI
- **SOAP Extraction**: Google Gemini extracts Subjetivo/Objetivo/Evaluación/Plan from the
  transcript, inferring each speaker's clinical role (médico/paciente/familiar/enfermera) from
  the dialogue itself rather than assuming who talks most
- **ICD-11 Lookup**: Diagnoses are matched against the WHO ICD-11 API and the code attached to
  the note
- **Técnico-médico Language**: Evaluación/Plan are normalized to formal clinical language per
  NOM-004-SSA3-2012 §5.11
- **PDF Generation**: Signed notes render to PDF via ReportLab, branded with the clinic's own
  logo/color, and can be emailed to the confirming doctor
- **Multi-clinic, Role-based Access**: Supabase Auth (JWT/JWKS) backs doctor and admin roles per
  clinic; admins manage their clinic's doctors, branding, and session history
- **NOM-024 Immutability**: A confirmed note is locked; corrections go through a doctor-authored
  addendum, not an edit
- **Rate Limiting**: Per-user and per-IP limits protect the AI-spend routes and admin actions

## 🏗️ Architecture

- **Backend**: Flask (Python), deployed as a Docker image on Render
- **Database/Auth/Storage**: Supabase (Postgres via PostgREST, Supabase Auth, Supabase Storage
  for clinic logos) — accessed via `urllib.request` + the `service_role` key, not the
  `supabase-py` client
- **Transcription**: AssemblyAI
- **LLM Extraction**: Google Gemini
- **PDF**: ReportLab (not WeasyPrint — no matching system libraries are installed)
- **Email**: Resend

## 📋 Prerequisites

- Python 3.11+ (matches the Docker image; `backend/venv` if running locally)
- Docker (for a production-shaped local build)
- A Supabase project (Postgres + Auth + Storage)
- AssemblyAI API key
- Google Gemini API key
- Resend API key (for PDF email delivery)
- WHO ICD-11 API credentials (optional — diagnosis coding degrades gracefully without it)

## 🛠️ Local Setup

```bash
# From the repo root
cd backend

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp ../.env.example .env
# then fill in .env with real values
```

Run the dev server (local only — this is not the production path):

```bash
python app.py
```

## 🚢 Deployment

Render builds and runs `backend/Dockerfile` directly (`gunicorn app:app`), with all secrets set
as Render environment variables — no `.env` file is baked into the image. See `.env.example` for
the full list of variables the app reads.

## 📁 Project Layout

```
backend/
  app.py              # Flask routes, request-scoped Supabase REST helpers, background job worker
  auth.py              # JWT verification (Supabase JWKS), require_auth/require_admin decorators
  config.py             # Env-var-backed configuration + startup validation
  transcription.py      # AssemblyAI integration
  llm_processor.py      # Gemini extraction prompt + schema validation
  pdf_generator.py       # ReportLab PDF rendering
  email_service.py       # Resend integration
  icd_service.py         # WHO ICD-11 lookup
  pg_utils.py            # Shared PostgREST query-encoding helpers
  static/, templates/     # Frontend (vanilla JS, server-rendered Jinja shells)
  Dockerfile, requirements.txt
```
