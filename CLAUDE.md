# Clin.IA — guide for Claude Code

Flask + Supabase medical-notes app (Mexico, Spanish UI). Doctors record a consultation → AssemblyAI transcribes → Gemini extracts a SOAP note → doctor reviews/edits → confirms (note is then immutable, NOM-024; corrections are addenda) → PDF with a consent letter + the note. Currently **Beta 1** (live with two real doctors, version 0.7.0-beta); **Beta 2** is being planned/built.

**Source of truth for Beta 2 scope and strategy:** `ClinIA_Hoja_de_Ruta_Beta2_v<latest>.md` in iCloud: `~/Library/Mobile Documents/com~apple~CloudDocs/AI/TAS/ClinIA/Roadmap/Beta 2/` (not in this repo; highest version number wins; items are numbered `#45…`). Read it before starting any Beta 2 work.

## Layout
- `backend/` — Flask app (`app.py` routes + background job worker, `auth.py` JWT/JWKS + roles, `llm_processor.py` Gemini extraction + validation, `transcription.py`, `pdf_generator.py` ReportLab, `icd_service.py` CIE-11, `email_service.py` Resend, `config.py` env).
- `backend/static/` — vanilla JS (no build step, no framework): `app.js` (main app), `admin.js`, `auth-common.js`, `utils.js`, `style.css`. `backend/templates/` — Jinja pages.
- `landing/` — separate static marketing site (own Render static site; no link to the backend).
- `backend/tests/` — pytest suite; `backend/tests/frontend/` — browser harness for JS.
- `.github/workflows/auth-tests.yml` — CI runs the pytest suite on push/PR to `main` (Python 3.11 to match `backend/Dockerfile`; dummy env vars only).

## Run the tests
```
cd backend && venv/bin/python -m pytest tests/ -q      # 168 tests, offline, ~0.2 s
```
- Fully offline: `conftest.py` mocks Supabase/AssemblyAI/Gemini/Resend and JWT verification. No real credentials exist or are needed in this sandbox.
- **`tests/route_inventory.py` lists every route and its auth policy; a test fails if a route exists without an entry.** Adding a route means adding an inventory entry (and cross-clinic/auth cases in `test_route_authorization.py`).
- JS has no test runner. `backend/tests/frontend/medicamentos_roundtrip.html` loads the real `static/app.js` against a stand-in DOM; serve `backend/` over HTTP (e.g. `python3 -m http.server`) and open it. It can't run from `file://`. Wiring JS tests into CI is roadmap item #49.
- Local dev server without real credentials: set dummy values for `SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY, GEMINI_API_KEY, ASSEMBLYAI_API_KEY, FLASK_SECRET_KEY, RESEND_API_KEY` (see the CI workflow) and run `venv/bin/python app.py`. Real login/Storage/email can only be tested in the user's own environment.

## Working rules (user's explicit preferences)
- **Git:** never commit or push unless told. "commit" ≠ "push" — push only when the instruction says so. Branch only when asked. Finished work usually goes to `main` by fast-forward (`git merge --ff-only`), push, then delete the branch (local + remote).
- **Deploys are manual** (the user deploys on Render); pushing to `main` does not deploy.
- **Version bump on every merge to `main`, in 3 places** — `backend/app.py` (`/api/health` `'version'`), `backend/templates/index.html` header badge, and its footer string. Grep for the old version afterwards to catch strays.
- **Investigate → report → implement** for anything non-trivial or ambiguous: reproduce first, show evidence (file:line, before/after), and stop for review when asked to "report but don't fix". Prompts arrive as markdown spec files; follow their report-back list.
- **Trace, don't assume.** Say "confirmed by reading X" only after reading X. Verify third-party API shapes (Supabase Admin API, PostgREST JSONB filters) against real docs.
- **Ask when a spec conflicts with existing code** (naming, behavior) rather than silently choosing.
- **No PHI anywhere** — logs, fixtures, tests, sample PDFs. Synthetic data only. Never log transcript or patient content.
- Disclose what was and wasn't verified (e.g. "not verified: real Supabase round-trip").
- Keep changes scoped to the task; no unrelated refactors. Match surrounding comment density and style (this codebase explains the *why*).

## Codebase gotchas
- **`service_role` GRANTs:** Supabase PostgREST needs explicit GRANTs. Before a new table, or the first use of a new operation type (SELECT/INSERT/UPDATE/DELETE) on an existing table, check grants and hand the user the exact `GRANT` SQL. From 30 Oct 2026 Supabase stops auto-exposing new tables: every new table needs `GRANT SELECT, INSERT, UPDATE, DELETE ON public.<table> TO service_role` (never anon/authenticated; serial columns also need the sequence GRANT; missing grants fail with 42501). A past DELETE route failed 100% in prod this way.
- **Authorization conventions:** every data route uses `@require_auth` (+ `@require_admin` for admin). Out-of-scope or missing resources return a uniform **404, never 403** (a distinguishable 403 leaks existence). Read scope = owner OR same clinic (`caller_can_read_session`); write/sign scope = owner only (`caller_can_addend_session`). Admin routes are limited to the admin's own clinic with server-built filters.
- **Single-instance assumption:** the rate limiter (`memory://`), JWKS cache, `_usuario_cache`, logo cache and startup job reaper are per-process. Correct only with one gunicorn worker/instance (current Dockerfile).
- **Render deploys keep the old instance alive until the new one is healthy**, but jobs running on the old instance are interrupted — don't deploy with `trabajos` rows in `queued/transcribing/extracting`.
- **CSP:** inline scripts need the per-request nonce (`g.csp_nonce`); same-origin external scripts are fine. Page config reaches static JS through `window.CLINIA_CONFIG`.
- **Gemini is non-deterministic.** The same recording can yield differently worded notes; that is not a pipeline bug. The note the doctor reviews is frozen: `confirm_and_generate` renders the submitted `structured_data` verbatim (no second extraction).
- **`llm_processor.validate_against_schema` normalizes** `informacion_paciente` and `metadata` to `{}` when Gemini omits them (it is told to omit empty fields); an extraction with no S/O/A/P content still fails on purpose.
- **Medications textarea** joins/splits fields on `" - "` via `MEDICAMENTO_FIELD_SEPARATOR` (a bare `-` collided with "8-12 horas"); untouched medications are sent back as the original objects. The proper fix (separate fields) is roadmap #73.
- **Consent page (PDF):** `_build_consent_page` must fit on page 1 with the note starting on page 2; headroom is only ~2.5 mm in the worst case (roadmap #58 frees space). `tests/test_pdf_consent.py` enforces it.
- **Test pitfall:** `conftest.py` patches `app_module.pdf_generator.generate_pdf`; to exercise the real PDF code instantiate `pdf_generator.PDFGenerator()` directly.
- New/independent UI or tables get their own scoped CSS/logic, not edits to shared rules.
- Prefer ref-based element clicks (`read_page`/`find`) over screenshot pixel coordinates when driving the Browser tool.

## Beta 2 test & deploy strategy (roadmap v3+)
No maintenance window and no staging. Beta 2 features ship dark behind **per-clinic feature flags** (`BETA2_CLINICAS` env var on Render; the server decides and reports active features via `/api/session-check`), are tested in production in the "Clínica de Pruebas" with fictitious patients (confirmed notes there are permanent), then released to all clinics by changing the flag, no deploy. Migrations are additive only, with a manual backup first (Sprint 8 is the exception: separate Supabase project). Release each sprint's features before starting the next. Details and rationale: the roadmap's "Estrategia de pruebas y despliegue".
