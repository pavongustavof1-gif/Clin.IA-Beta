# backend/app.py
from flask import Flask, request, jsonify, send_from_directory, render_template, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config
from transcription import TranscriptionService
from llm_processor import LLMProcessor
from pdf_generator import PDFGenerator
from logger import logger
from email_service import send_pdf_email, send_invite_email
from auth import require_auth, require_admin
from pg_utils import pg_val, pg_path, pg_ilike_val
from concurrent.futures import ThreadPoolExecutor
import os
import re
import secrets
import tempfile
import glob
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from io import BytesIO
from PIL import Image as PILImage, UnidentifiedImageError


def _new_request_id() -> str:
    """Short id for correlating a client-facing generic error with the
    full exception in the server log (finding #20) — not a security
    token, just a grep key."""
    return secrets.token_hex(6)


def _derive_initials(nombre: str) -> str:
    """Return uppercase initials from a name, skipping common title prefixes, max 4 chars."""
    TITLE_PREFIXES = {'dr', 'dra', 'lic', 'mtro', 'mtra', 'ing', 'prof'}
    words = re.split(r'\s+', nombre.strip())
    initials = [
        w[0].upper()
        for w in words
        if w and re.match(r'[a-záéíóúüñA-ZÁÉÍÓÚÜÑ]', w[0])
        and w.rstrip('.').lower() not in TITLE_PREFIXES
    ]
    return ''.join(initials[:4])


# Doctor-facing display labels for the review-screen transcript. Keys are
# the normalized (lowercase, unaccented) values Gemini is asked to put in
# structured_data['roles_detectados'] — see llm_processor.py's
# speaker_instruction (Fix #30). This is a display-only mapping: the
# extraction prompt and the SOAP logic both still use/produce neutral
# "Hablante X" labels; this only changes what the doctor SEES in the
# review transcript, built AFTER extraction so roles_detectados exists.
_ROLE_DISPLAY_LABELS = {
    'medico':    'Doctor',
    'paciente':  'Paciente',
    'familiar':  'Familiar',
    'enfermera': 'Enfermera',
}


def _friendly_speaker_label(speaker: str, roles_detectados) -> str:
    """
    Map an AssemblyAI speaker label (A/B/C...) to a doctor-facing role
    label, using Gemini's own roles_detectados inference — never a guess
    of our own (that was the pre-Fix-#30 bug). Falls back to the neutral
    'Hablante X' label whenever roles_detectados is missing, isn't a
    dict, doesn't cover this speaker, or holds a value that isn't one of
    the four expected roles — never crashes, never invents a role.
    """
    if not isinstance(roles_detectados, dict):
        return f'Hablante {speaker}'
    role_raw = roles_detectados.get(f'Hablante {speaker}')
    if not isinstance(role_raw, str):
        return f'Hablante {speaker}'
    role_key = role_raw.strip().lower().replace('é', 'e')
    return _ROLE_DISPLAY_LABELS.get(role_key, f'Hablante {speaker}')


def validate_audio_file(file) -> tuple[bool, str]:
    """
    Validate an uploaded audio file before processing.
    Returns (is_valid: bool, error_message: str).
    error_message is empty string when valid.
    """
    if not file or file.filename == '':
        return False, "No se recibió ningún archivo de audio."

    _, ext = os.path.splitext(file.filename.lower())
    if ext not in Config.ALLOWED_AUDIO_EXTENSIONS:
        return False, (
            f"Formato de archivo no permitido: '{ext}'. "
            "Formatos aceptados: WAV, MP3, WEBM, M4A."
        )

    mime_type = (file.content_type or '').lower().split(';')[0].strip()
    if mime_type and mime_type not in Config.ALLOWED_AUDIO_MIME_TYPES:
        logger.warning(f"Validation: unexpected MIME type '{mime_type}' for file '{file.filename}'")
        # Log but don't reject — some browsers send non-standard MIME types for audio

    content_length = request.content_length
    if content_length and content_length > Config.MAX_AUDIO_SIZE_BYTES:
        return False, "El archivo es demasiado grande. Tamaño máximo permitido: 200 MB."

    # Seek to end to get actual size, then reset
    file.seek(0, 2)
    actual_size = file.tell()
    file.seek(0)

    if actual_size > Config.MAX_AUDIO_SIZE_BYTES:
        return False, "El archivo es demasiado grande. Tamaño máximo permitido: 200 MB."

    if actual_size < Config.MIN_AUDIO_SIZE_BYTES:
        return False, "El archivo de audio está vacío o es demasiado corto para procesar."

    return True, ''


# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB — matches Config.MAX_AUDIO_SIZE_BYTES

# Render terminates TLS at its own edge and forwards exactly one proxy
# hop, appending the real client IP as the LAST entry of X-Forwarded-For.
# Without this, request.remote_addr is Render's proxy IP for every
# request, which would collapse every caller into one shared rate-limit
# bucket. x_for=1 trusts only that one hop (the entry Render itself
# appends) — a client that prepends fake IPs to X-Forwarded-For can't
# spoof an earlier hop into being trusted (verified locally: ProxyFix
# reads the rightmost entry, not the client-controlled leftmost one).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# Enable CORS for both local testing and production domains
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "http://localhost:5000",
            "https://clin-ia-beta.onrender.com",
            "https://clinianotes.com",
            "https://www.clinianotes.com",
            "https://app.clinianotes.com",
        ],
        "methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})


def _rate_limit_key() -> str:
    """
    Key rate limits by authenticated usuario_id when available, so a
    limit tracks the person rather than whatever IP/device they're
    behind; falls back to client IP for routes with no @require_auth.

    Safe to read g.usuario here: on every @limiter.limit()-decorated
    route in this app, @require_auth is the outer decorator (applied
    above @limiter.limit in the stack), so it runs first and sets
    g.usuario before this key function is ever evaluated — Flask-Limiter's
    per-route decorator behaves like an ordinary nested wrapper, not a
    before_request hook that would run before the route's own decorators
    (verified locally against this Flask-Limiter version).
    """
    usuario = getattr(g, 'usuario', None)
    if usuario:
        return f"user:{usuario['usuario_id']}"
    return f"ip:{get_remote_address()}"


# Rate limiting (finding #19). storage_uri defaults to memory://, which
# is per-process — correct for today's deployment, where the Dockerfile's
# gunicorn command (`gunicorn app:app`) sets no --workers/--threads and
# so runs a single sync worker (confirmed in Stage M2's #17 work). If
# this app ever scales to multiple gunicorn workers/threads or multiple
# Render instances, each process gets its OWN independent counters and
# every limit below effectively multiplies by the process count — at
# that point the store must move to a shared backend (Redis, or a
# Postgres-backed limiter). Logged so that migration isn't a silent
# footgun if concurrency changes later without this being revisited.
logger.warning(
    "RateLimit: using in-memory (per-process) storage. Correct only "
    "while the app runs a single gunicorn worker (current Dockerfile "
    "CMD). If workers/threads/instances are ever added, move to a "
    "shared store (e.g. Redis) or these limits will multiply per process."
)
limiter = Limiter(
    _rate_limit_key,
    app=app,
    storage_uri="memory://",
    default_limits=["200 per hour"],
    headers_enabled=True,
)


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        'error': 'Demasiadas solicitudes. Intente de nuevo más tarde.',
        'error_code': 'RATE_LIMITED',
    }), 429


# Validate configuration on startup
try:
    Config.validate()
    logger.info("Flask: Configuration validated successfully")
except ValueError as e:
    logger.error(f"Flask: Configuration error: {e}")
    logger.error("Flask: Please check your .env file")
    exit(1)

# Initialize services
transcription_service = TranscriptionService()
llm_processor = LLMProcessor()
pdf_generator = PDFGenerator()
_executor = ThreadPoolExecutor(max_workers=4)

# ── Supabase REST helpers ────────────────────────────────────────────────────

def _sb_headers(extra: dict = None) -> dict:
    h = {
        'apikey': Config.SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {Config.SUPABASE_SERVICE_KEY}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    if extra:
        h.update(extra)
    return h

def _sb_get(path: str) -> list | None:
    """GET from Supabase REST. Returns parsed JSON list or None on error."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase GET {path} failed {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        logger.warning(f"DB: Supabase GET {path} error: {e}")
        return None

def _sb_patch(path: str, body: dict) -> bool:
    """PATCH to Supabase REST. Returns True on success."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    data = json.dumps(body, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(url, data=data, headers=_sb_headers(), method='PATCH')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase PATCH {path} failed {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        logger.warning(f"DB: Supabase PATCH {path} error: {e}")
        return False

def _sb_delete(path: str) -> bool:
    """DELETE via Supabase REST. Returns True on success."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    req = urllib.request.Request(url, headers=_sb_headers(), method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase DELETE {path} failed {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        logger.warning(f"DB: Supabase DELETE {path} error: {e}")
        return False


# ── Supabase Storage helpers ─────────────────────────────────────────────────
# Distinct API surface from the PostgREST /rest/v1/ calls above —
# Supabase Storage lives at /storage/v1/object/{bucket}/{path}. Confirmed
# against Supabase's own docs (Storage Access Control; Standard Uploads):
# service_role bypasses Storage RLS unconditionally, same as Postgres RLS,
# no storage.objects policy required. Overwriting an existing object path
# requires the x-upsert header — POST alone 400s with "Asset Already
# Exists" on conflict, there is no PUT-auto-upsert shortcut.

CLINIC_LOGOS_BUCKET = 'clinic-logos'


def _storage_headers(content_type: str = None) -> dict:
    h = {
        'apikey': Config.SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {Config.SUPABASE_SERVICE_KEY}',
    }
    if content_type:
        h['Content-Type'] = content_type
    return h


def _storage_upload(bucket: str, path: str, data: bytes, content_type: str) -> bool:
    """POST raw bytes to Supabase Storage with x-upsert so a re-upload replaces the existing object."""
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{pg_val(bucket)}/{pg_path(path)}'
    headers = _storage_headers(content_type)
    headers['x-upsert'] = 'true'
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"Storage: upload to {bucket}/{path} failed {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        logger.warning(f"Storage: upload to {bucket}/{path} error: {e}")
        return False


def _storage_download(bucket: str, path: str) -> tuple[bytes, str] | None:
    """GET from Supabase Storage. Returns (bytes, content_type) or None on any failure (incl. not-found)."""
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{pg_val(bucket)}/{pg_path(path)}'
    req = urllib.request.Request(url, headers=_storage_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get('Content-Type', 'application/octet-stream')
            return resp.read(), content_type
    except urllib.error.HTTPError as e:
        logger.warning(f"Storage: download {bucket}/{path} failed {e.code}")
        return None
    except Exception as e:
        logger.warning(f"Storage: download {bucket}/{path} error: {e}")
        return None


def _storage_delete(bucket: str, path: str) -> bool:
    """DELETE an object from Supabase Storage. Returns True on success."""
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{pg_val(bucket)}/{pg_path(path)}'
    req = urllib.request.Request(url, headers=_storage_headers(), method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"Storage: delete {bucket}/{path} failed {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        logger.warning(f"Storage: delete {bucket}/{path} error: {e}")
        return False

def _sb_post_job(body: dict) -> str | None:
    """Insert a trabajos row. Returns generated job_id or None on error."""
    url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/trabajos'
    payload = json.dumps(body, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers=_sb_headers(extra={'Prefer': 'return=representation'}),
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
        return rows[0]['job_id'] if rows else None
    except Exception as e:
        logger.warning(f"DB: Could not create job: {e}")
        return None


def _sb_log_lectura(session_id: str, usuario_id: str) -> None:
    """Append a row to lecturas_sesion. Caller catches and logs on failure."""
    url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/lecturas_sesion'
    payload = json.dumps({'session_id': session_id, 'usuario_id': usuario_id},
                         ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=payload, headers=_sb_headers(), method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _sb_generate_invite_link(email: str) -> tuple[str, str]:
    """
    Calls Supabase Admin API to create the auth identity and generate an
    invite action_link, WITHOUT Supabase sending its own email (we send via
    Resend instead). Returns (user_id, action_link). Raises on failure —
    caller is responsible for treating this as a generic failure, NOT as
    evidence of a duplicate email (duplicate-email detection happens
    entirely via our own usuarios-table check, before this is ever called —
    generate_link's own error behavior for duplicates is not reliably
    consistent across Supabase versions/modes, so we don't lean on it).
    """
    url = Config.SUPABASE_URL.rstrip('/') + '/auth/v1/admin/generate_link'
    payload = json.dumps({
        'type': 'invite',
        'email': email,
        'redirect_to': 'https://app.clinianotes.com/set-password',
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=payload, headers=_sb_headers(), method='POST')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data['id'], data['action_link']


def _sb_set_user_ban(user_id: str, banned: bool) -> None:
    """
    Ban/unban a Supabase auth user via the Admin API.
    Endpoint/method confirmed directly from auth-js source
    (GoTrueAdminApi.ts, updateUserById): PUT {SUPABASE_URL}/auth/v1/admin/users/{user_id}.
    ban_duration is a Go duration string (units ns/us/ms/s/m/h); there is no
    literal "permanent" value, so "876000h" (~100 years) is the documented
    community convention for an effectively indefinite ban. "none" lifts it.

    IMPORTANT: this only blocks FUTURE sign-in attempts — it does not
    invalidate a JWT already issued before the ban. An already-logged-in
    user keeps a valid token until it expires (up to ~1h) unless the
    activo check in auth.get_usuario_context catches it first on their
    next request. Both layers are required; this one is not sufficient
    alone for immediate cutoff.

    Raises on failure — caller applies the same partial-failure handling
    used elsewhere in this project (log which side succeeded/failed).
    """
    url = Config.SUPABASE_URL.rstrip('/') + f'/auth/v1/admin/users/{pg_val(user_id)}'
    payload = json.dumps({
        'ban_duration': '876000h' if banned else 'none',
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=payload, headers=_sb_headers(), method='PUT')
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _sb_insert_usuario(body: dict) -> dict | None:
    """Insert a usuarios row. Returns the created row or None on error."""
    url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/usuarios'
    payload = json.dumps(body, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers=_sb_headers(extra={'Prefer': 'return=representation'}),
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read())
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"DB: Could not insert usuarios row: {e}")
        return None


def _sb_patch_job(job_id: str, body: dict) -> None:
    """PATCH a trabajos row. Logs silently on failure."""
    body['updated_at'] = datetime.now().isoformat()
    _sb_patch(f'/rest/v1/trabajos?job_id=eq.{pg_val(job_id)}', body)


# ── Zombie-job reaper (Stage M4 fix #26) ───────────────────────────────────────
# Pragmatic beta-scale reaping, deliberately NOT a durable queue (no
# Celery/Redis) — this app runs everything in-process via a
# ThreadPoolExecutor, so a crash/redeploy kills every in-flight job's
# owning thread with the process. Two layers:
#   1. Startup sweep — anything still in-flight AND already past a short
#      minimum age when the process starts is presumed dead (see the
#      deploy-overlap note below for why the age gate exists). Runs
#      once, at import time, for every process (gunicorn worker or
#      `python app.py`); see the call at the bottom of this file.
#   2. Age-based, on-poll — job_status() calls _reap_if_stale() on every
#      poll, so a job hung (but not crashed) past a generous ceiling
#      self-heals the next time its own client polls it. There is no
#      background timer: an abandoned job (client stopped polling) is
#      only caught by the NEXT restart's startup sweep, not sooner. That
#      gap is accepted for beta volume — a durable queue (survive
#      restarts by resuming, reconcile with AssemblyAI on recovery) is
#      the real long-term fix if beta shows frequent restarts or high
#      job volume; not building that now.
#
# DEPLOY-OVERLAP CAVEAT (verified against Render's own docs, not assumed):
# Render's deploys are NOT a hard stop-then-start even for a single,
# unscaled instance — the OLD instance keeps serving traffic while the
# NEW instance boots, and only gets SIGTERM once the new instance passes
# its health check. So at the moment the new instance's startup sweep
# runs, the old instance may still be alive and legitimately mid-job.
# _STARTUP_SWEEP_MIN_AGE_MINUTES exists specifically to not race that
# window: only rows already stale beyond it get reaped at startup, not
# every in-flight row unconditionally. This narrows the risk but does not
# eliminate it — a job the old instance has been legitimately working on
# for longer than the gate (e.g. a slow health-check handoff overlapping
# a long transcription) could still be wrongly reaped. The fully-correct
# fix is a graceful-shutdown hook so the dying instance self-reports its
# own in-flight jobs before exiting — that needs a gunicorn
# post_worker_init hook or a custom worker class (a new gunicorn.conf.py
# + Dockerfile CMD change), which is real infra restructuring, not a
# pragmatic reaper tweak; flagging as a follow-up, not building it here.
# A true crash (SIGKILL/OOM, no graceful handoff) is unaffected by any of
# this — there is no "old instance" left to race, and the age gate still
# reliably catches it once enough time has passed.
#
# SINGLE-INSTANCE ASSUMPTION: both layers still assume exactly one app
# process is the long-run steady state (confirmed for the current
# deployment in Stage M2 — the Dockerfile's gunicorn CMD sets no
# --workers/--threads, and Render runs one instance; the deploy-overlap
# window above is a brief, expected exception to that, not a case where
# the app is actually scaled out). If the app ever runs multiple
# steady-state instances, this whole scheme needs revisiting — same
# caveat class as Stage M3's memory:// rate-limit store.

_JOB_IN_FLIGHT_STATUSES = ('queued', 'transcribing', 'extracting')
_JOB_MAX_AGE_MINUTES = 25  # frontend gives up polling at 15 min (360 x 2.5s); this is that ceiling + buffer
_STARTUP_SWEEP_MIN_AGE_MINUTES = 3  # deploy-overlap gate — see caveat above
_JOB_INTERRUPTED_MESSAGE = 'El procesamiento fue interrumpido. Por favor, intente de nuevo.'
_JOB_TIMEOUT_MESSAGE = 'El procesamiento está tardando demasiado. Por favor, intente de nuevo.'
_TEMP_AUDIO_GLOB = 'clinia_*'  # matches the naming used at temp_path creation in process_audio()


def _job_age(updated_at: str | None) -> timedelta | None:
    """Parse a trabajos.updated_at value and return its age, or None if
    missing/unparsable (caller must treat that as 'don't reap on a guess')."""
    if not updated_at:
        return None
    try:
        return datetime.now() - datetime.fromisoformat(updated_at.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def _reap_if_stale(job_id: str, row: dict) -> dict:
    """
    If `row` is in an in-flight status and hasn't been updated in over
    _JOB_MAX_AGE_MINUTES, mark it failed (DB + return value) so the
    caller's response reflects the reap immediately instead of on the
    next poll. Returns `row` unchanged otherwise.
    """
    status = row.get('status')
    if status not in _JOB_IN_FLIGHT_STATUSES:
        return row
    age = _job_age(row.get('updated_at'))
    if age is None or age < timedelta(minutes=_JOB_MAX_AGE_MINUTES):
        return row

    logger.warning(f"Job {job_id}: reaped — stuck at '{status}' for {age}, past the {_JOB_MAX_AGE_MINUTES}min ceiling")
    _sb_patch_job(job_id, {'status': 'error', 'error_message': _JOB_TIMEOUT_MESSAGE})
    return {**row, 'status': 'error', 'error_message': _JOB_TIMEOUT_MESSAGE}


def _reap_stuck_jobs_on_startup() -> None:
    """
    Runs once per process start (see call at the bottom of this file).
    Marks failed any trabajos row that is BOTH in an in-flight status AND
    already past _STARTUP_SWEEP_MIN_AGE_MINUTES old — the age gate exists
    because of Render's deploy-overlap behavior (see the module comment
    above); without it, this sweep could wrongly kill a job the outgoing
    instance is still legitimately working on. Also clears any orphaned
    temp audio left behind (a completed/cleanly-failed job always removes
    its own temp file — see _run_job's finally/except paths — so anything
    matching the naming pattern still on disk at a fresh process start
    cannot belong to a job this process is running). Note a genuine
    Render redeploy gets a brand-new container with an empty disk, so
    this glob is normally a no-op then — it only finds anything on an
    in-place process restart within the same container/disk (e.g. a
    crashed worker respawned by gunicorn). Either way it can't collide
    with a still-live old instance during the deploy-overlap window:
    that instance runs in its own separate container/disk entirely.
    Best-effort: logs and continues on any failure, since a DB hiccup
    here must never block the app from starting.
    """
    try:
        in_flight = ','.join(pg_val(s) for s in _JOB_IN_FLIGHT_STATUSES)
        candidates = _sb_get(f'/rest/v1/trabajos?status=in.({in_flight})&select=job_id,updated_at')
        if candidates is None:
            # _sb_get already logged the underlying error — distinguish
            # "couldn't check" from "checked, found none" so a startup DB
            # hiccup doesn't read in the log as a clean sweep.
            logger.error("Startup reaper: could not query for stuck trabajos rows — skipping sweep this start")
        else:
            stale_ids = [
                c['job_id'] for c in candidates
                if (age := _job_age(c.get('updated_at'))) is not None
                and age >= timedelta(minutes=_STARTUP_SWEEP_MIN_AGE_MINUTES)
            ]
            too_fresh = len(candidates) - len(stale_ids)
            if too_fresh:
                logger.info(f"Startup reaper: left {too_fresh} in-flight row(s) alone — under the {_STARTUP_SWEEP_MIN_AGE_MINUTES}min deploy-overlap gate, may still be owned by a live outgoing instance")
            if stale_ids:
                ids = ','.join(pg_val(j) for j in stale_ids)
                ok = _sb_patch(
                    f'/rest/v1/trabajos?job_id=in.({ids})',
                    {
                        'status': 'error',
                        'error_message': _JOB_INTERRUPTED_MESSAGE,
                        'updated_at': datetime.now().isoformat(),
                    },
                )
                logger.info(f"Startup reaper: {'marked' if ok else 'FAILED to mark'} {len(stale_ids)} stuck trabajos row(s) as error")
            elif not too_fresh:
                logger.info("Startup reaper: no stuck trabajos rows found")
    except Exception as e:
        logger.error(f"Startup reaper: failed to sweep stuck jobs: {e}")

    try:
        temp_dir = tempfile.gettempdir()
        orphans = glob.glob(os.path.join(temp_dir, _TEMP_AUDIO_GLOB))
        for path in orphans:
            try:
                os.remove(path)
            except OSError as e:
                logger.warning(f"Startup reaper: could not remove orphaned temp file {path}: {e}")
        if orphans:
            logger.info(f"Startup reaper: removed {len(orphans)} orphaned temp audio file(s)")
    except Exception as e:
        logger.error(f"Startup reaper: failed to sweep orphaned temp audio: {e}")


# ── Background job worker ─────────────────────────────────────────────────────

def _run_job(job_id, audio_path, params, usuario_id, clinica_id, nombre, doctor_info):
    """Runs transcription + LLM pipeline in a background thread."""
    try:
        # Phase A — Transcription
        _sb_patch_job(job_id, {'status': 'transcribing'})
        logger.info(f"Job {job_id}: starting transcription")

        try:
            transcript_result = transcription_service.transcribe_audio(
                audio_path,
                print_raw=params['print_raw'],
                speakers_expected=params['speakers_expected']
            )
        except Exception as e:
            logger.error(f"Job {job_id}: transcription failed — {e}")
            _sb_patch_job(job_id, {'status': 'error', 'error_message': 'Transcripción fallida. Intente de nuevo.'})
            return
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)
                logger.debug(f"Job {job_id}: temp file cleaned up")

        transcript_text = transcript_result['text']
        if not transcript_text or len(transcript_text.strip()) < 10:
            _sb_patch_job(job_id, {'status': 'error', 'error_message': 'Transcripción muy corta o vacía'})
            return

        # Phase B — LLM extraction
        _sb_patch_job(job_id, {'status': 'extracting'})
        logger.info(f"Job {job_id}: starting LLM extraction")

        try:
            structured_data = llm_processor.extract_structured_data(
                transcript_text,
                utterances=transcript_result.get('utterances', [])
            )
        except Exception as e:
            logger.error(f"Job {job_id}: LLM extraction failed — {e}")
            _sb_patch_job(job_id, {'status': 'error', 'error_message': 'Extracción de datos fallida. Intente de nuevo.'})
            return

        # A non-raising extraction can still be malformed/empty (Stage M4
        # fix #24, step 3) — validate before it's allowed to become a
        # persisted pending_review row, same failure path as an outright
        # extraction exception above.
        is_valid, validation_error = llm_processor.validate_against_schema(structured_data)
        if not is_valid:
            logger.error(f"Job {job_id}: extraction failed schema validation — {validation_error}")
            _sb_patch_job(job_id, {'status': 'error', 'error_message': 'Extracción de datos fallida. Intente de nuevo.'})
            return

        if 'metadata' not in structured_data:
            structured_data['metadata'] = {}
        structured_data['metadata']['fecha_hora_consulta'] = params['consultation_timestamp']

        # Build session
        initials  = _derive_initials(nombre)
        session_id = f"SESSION-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{initials}"

        utterances = transcript_result.get('utterances', [])

        # Doctor-facing labels, derived from Gemini's own roles_detectados
        # inference (Fix #30 follow-up) — never our own guess. Built here,
        # after extraction, specifically so structured_data['roles_detectados']
        # already exists. The extraction prompt itself still uses/produces
        # neutral "Hablante X" labels throughout (unchanged) — this only
        # changes what the doctor SEES in the review transcript.
        roles_detectados = structured_data.get('roles_detectados') if isinstance(structured_data, dict) else None
        labeled_text = "\n".join(
            f"[{_friendly_speaker_label(u['speaker'], roles_detectados)}]: {u['text']}"
            for u in utterances
        ) if utterances else None

        transcript_payload = {
            'text':             transcript_text,
            'labeled_text':     labeled_text,
            'confidence':       transcript_result.get('confidence'),
            'duration_seconds': transcript_result.get('audio_duration', 0) / 1000,
            'word_count':       transcript_result.get('words', 0),
        }

        save_session(session_id, {
            'session_id':        session_id,
            'status':            'pending_review',
            'transcript':        transcript_payload,
            'structured_data':   structured_data,
            'local_timestamp':   params['local_timestamp'],
            'consent_given':     params['consent_given'],
            'consent_timestamp': params['consent_timestamp'],
        }, usuario_id=usuario_id, clinica_id=clinica_id)

        _sb_patch_job(job_id, {
            'status':          'done',
            'session_id':      session_id,
            'structured_data': structured_data,
            'transcript':      transcript_payload,
        })
        logger.info(f"Job {job_id}: done — session {session_id}")

    except Exception as e:
        logger.error(f"Job {job_id}: unexpected error — {e}")
        import traceback; traceback.print_exc()
        _sb_patch_job(job_id, {'status': 'error', 'error_message': 'Error interno. Intente de nuevo.'})
        if os.path.exists(audio_path):
            os.remove(audio_path)


# ── Supabase session persistence ─────────────────────────────────────────────

def strip_transcript(payload: dict) -> dict:
    """
    Return a shallow copy of a session payload with the raw transcript
    content removed from its 'transcript' sub-dict — 'text' and
    'labeled_text' only, the two PHI-bearing fields (confidence,
    duration_seconds, word_count are metadata, not patient speech, and
    are kept).

    Used at the two points a session leaves the pending_review window —
    confirm and cancel — so full_response never retains a live copy of
    the transcript once the record is finalized. Before this fix, nulling
    the transcript_text COLUMN gave the appearance of minimization while
    the same content lived on unabated inside full_response's JSONB blob
    (Stage H1 finding #10).

    Does not mutate the input — callers that still need the original in
    the same request (e.g. confirm_and_generate echoing the transcript
    back once in its response) keep it.
    """
    stripped = dict(payload)
    transcript = stripped.get('transcript')
    if isinstance(transcript, dict):
        transcript = dict(transcript)
        transcript.pop('text', None)
        transcript.pop('labeled_text', None)
        stripped['transcript'] = transcript
    return stripped


def save_session(session_id: str, data: dict, usuario_id: str, clinica_id: str):
    """Upsert a session to Supabase sesiones table. Silently logs on failure — never raises."""
    try:
        def _ts(val):
            if not val or str(val).strip() == '':
                return None
            return val

        transcript = data.get('transcript') or {}
        doc        = data.get('document')  or {}
        info     = (data.get('structured_data') or {}).get('informacion_paciente') or {}
        raw_curp = info.get('curp')
        paciente_curp = raw_curp.strip().upper() if raw_curp and isinstance(raw_curp, str) else None

        body = {
            'session_id':           session_id,
            'usuario_id':           usuario_id,
            'clinica_id':           clinica_id,
            'timestamp':            _ts(data.get('timestamp', datetime.now().isoformat())),
            'status':               data.get('status', 'pending_review'),
            'structured_data':      data.get('structured_data', {}),
            'transcript_text':      transcript.get('text'),
            'transcript_confidence': transcript.get('confidence'),
            'transcript_duration':  transcript.get('duration_seconds'),
            'transcript_words':     transcript.get('word_count'),
            'doc_link':             doc.get('link') if doc else None,
            'doc_title':            doc.get('title') if doc else None,
            'consent_given':        bool(data.get('consent_given')),
            'consent_timestamp':    _ts(data.get('consent_timestamp')),
            'consent_tratamiento':  data.get('consent_tratamiento'),
            'addenda':              data.get('addenda', []),
            'locked_at':            _ts(data.get('locked_at')),
            'cancelled_at':         _ts(data.get('cancelled_at')),
            'cancellation_reason':  data.get('cancellation_reason'),
            'paciente_curp':        paciente_curp,
            'full_response':        data,
        }
        url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/sesiones?on_conflict=session_id'
        payload = json.dumps(body, ensure_ascii=False, default=str).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers=_sb_headers(extra={'Prefer': 'resolution=merge-duplicates'}),
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger.info(f"DB: Session {session_id} saved to Supabase.")
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Could not save session {session_id}: {e.code} {e.read().decode()}")
    except Exception as e:
        logger.warning(f"DB: Could not save session {session_id}: {e}")


def load_session(session_id: str) -> dict | None:
    """Retrieve a full session from Supabase. Returns None if not found."""
    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}&select=full_response&limit=1')
    if not rows:
        return None
    return rows[0].get('full_response')


def load_structured_data(session_id: str) -> dict | None:
    """Retrieve only structured_data for a session — used by the export endpoint."""
    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}&select=structured_data&limit=1')
    if not rows:
        return None
    return rows[0].get('structured_data')


def caller_can_read_session(row: dict, usuario: dict) -> bool:
    """
    Read-scoping for a `sesiones` row: the caller is the session's author
    OR shares its clinic. Mirrors patient_history_detail's is_owner/is_clinic
    rule exactly — that route is the existing legitimate detail view backing
    the doctor Historial UI. The "Notas de la clínica" scope toggle only
    parameterizes the separate LIST/search endpoint's filter; it has no
    bearing on this per-session rule, which already always grants
    clinic-wide read regardless of toggle state.
    """
    return (
        row.get('usuario_id') == usuario['usuario_id']
        or row.get('clinica_id') == usuario['clinica_id']
    )


def caller_can_addend_session(row: dict, usuario: dict) -> bool:
    """
    Write-scoping for addenda: owner only. An addendum amends a signed
    clinical record, so it is deliberately narrower than read access —
    mirrors the owner-only convention already used for Notas Pendientes
    (nobody but the authoring doctor, not even an admin, gets special
    access there; see pending_sessions_list's docstring). Clinic-wide
    addendum authority exists only via the separate, admin-gated
    POST /api/admin/session/<id>/addendum route.
    """
    return row.get('usuario_id') == usuario['usuario_id']


def get_clinica_context(clinica_id: str) -> dict:
    """
    Fetch clinic name, primary color, address, and phone from Supabase.
    Returns defaults on error. direccion/telefono default to empty string
    (not placeholder text) — an unfilled field must render as absent, not
    as a fake value.
    """
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}&select=nombre,color_primario,direccion,telefono,logo_url&limit=1')
    if rows:
        return {
            'nombre':         rows[0].get('nombre') or 'Consultorio Médico',
            'color_primario': rows[0].get('color_primario') or '#0F6E56',
            'direccion':      rows[0].get('direccion') or '',
            'telefono':       rows[0].get('telefono') or '',
            'logo_url':       rows[0].get('logo_url') or '',
        }
    return {'nombre': 'Consultorio Médico', 'color_primario': '#0F6E56', 'direccion': '', 'telefono': '', 'logo_url': ''}


def fetch_clinica_logo_bytes(logo_storage_path: str) -> bytes | None:
    """
    Fetch clinic logo bytes from Supabase Storage for PDF rendering.
    Returns None on ANY failure (empty path, missing object, network
    error, stale logo_url after the object was deleted) — a logo fetch
    failure must never break PDF generation, only omit the logo.
    """
    if not logo_storage_path:
        return None
    result = _storage_download(CLINIC_LOGOS_BUCKET, logo_storage_path)
    if result is None:
        logger.warning(f"PDF: could not fetch clinic logo at '{logo_storage_path}' — rendering without it")
        return None
    image_bytes, _content_type = result
    return image_bytes


def get_usuario_cedula(usuario_id: str) -> str:
    """Fetch doctor's cédula professional from Supabase. Returns empty string on error."""
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=cedula&limit=1')
    if rows:
        return rows[0].get('cedula') or ''
    return ''


def get_usuario_nombre(usuario_id: str) -> str:
    """Fetch a doctor's display name only — no email/cédula/other fields. Empty string on error."""
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=nombre&limit=1')
    if rows:
        return rows[0].get('nombre') or ''
    return ''

# ────────────────────────────────────────────────────────────────────────────


# ── Security headers / CSP (Stage 3, findings #7-#9) ─────────────────────────
#
# script-src is nonce-based, not 'self'-only: login.html, account.html, and
# set-password.html carry their entire page logic (login, forgot-password,
# password re-auth + change, invite-link session detection) as inline
# <script> with no external .js file, and moving all of that out — plus
# inventing a way to hand Jinja-rendered Supabase config to a static file —
# was judged a larger, riskier refactor than the security gain over a
# per-request nonce justifies. A nonce blocks any attacker-injected <script>
# or event-handler attribute exactly as well as 'self'-only does; the extra
# protection 'self'-only buys is against an attacker rewriting a *trusted*
# inline block's own content, which isn't how the addenda/patient-data
# stored-XSS vector (#7) works. Nonces do NOT cover inline handler
# attributes (onclick=...) regardless — every one of those was refactored
# to addEventListener as part of #7, so there is no 'unsafe-inline' here.
#
# style-src keeps 'unsafe-inline': inline style="..." attributes are
# pervasive throughout the generated HTML (session-detail-render.js,
# admin.js, app.js). Style injection is a materially lower-severity risk
# than script injection, and hashing/nonce-ing every dynamically generated
# style attribute individually isn't practical here.
@app.before_request
def _set_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def _inject_csp_nonce():
    return {'csp_nonce': getattr(g, 'csp_nonce', '')}


@app.after_request
def _set_security_headers(response):
    nonce = getattr(g, 'csp_nonce', '')
    csp = (
        "default-src 'self'; "
        f"script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src https://fonts.gstatic.com; "
        f"connect-src 'self' {Config.SUPABASE_URL}; "
        "img-src 'self' blob:; "
        "media-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers['Content-Security-Policy'] = csp
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # Only microphone is used anywhere in this app (audio recording via
    # getUserMedia({audio:...}) in app.js) — every other sensitive
    # permission is explicitly denied.
    response.headers['Permissions-Policy'] = (
        'microphone=(self), camera=(), geolocation=(), payment=(), usb=()'
    )
    return response

# ────────────────────────────────────────────────────────────────────────────


@app.route('/')
def index():
    return render_template(
        'index.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )

@app.route('/login')
def login():
    return render_template(
        'login.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )

@app.route('/set-password')
def set_password():
    # Unconditional shell, same pattern as login() — no server-side session
    # to gate on. The invite token itself (in the URL hash, processed
    # client-side by supabase-js) is the credential; there's nothing for
    # Flask to check on a plain page GET here.
    return render_template(
        'set-password.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )

@app.route('/admin')
def admin():
    # Unconditional shell, same pattern as index() — there is no server-side
    # session to gate on for a plain page GET. Role-gating happens client-side
    # (admin.js checks /api/session-check) and the real boundary is
    # @require_admin on /api/admin/* below.
    return render_template(
        'admin.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )

@app.route('/account')
def account():
    # Unconditional shell, same pattern as admin() — no role gate at all,
    # any authenticated user (doctor or admin) reaches this for their own
    # password. Client-side auth guard only (account.js), no @require_*
    # boundary needed server-side since the actual password change goes
    # straight to Supabase Auth via supabaseClient, not through our API.
    return render_template(
        'account.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )

@app.route('/api/health', methods=['GET'])
@limiter.exempt  # Render's own health/readiness polling hits this; must never 429
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'ClinIA Alpha',
        'version': '0.1.0',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/process-audio', methods=['POST'])
@require_auth
@limiter.limit("10 per minute;100 per hour")  # AI-spend: real AssemblyAI/Gemini cost per call (via _run_job)
def process_audio():
    """
    Validates audio, saves to temp, captures request context, submits background job.
    Returns 202 {job_id} immediately; caller polls /api/job-status/<job_id>.
    """
    try:
        # Server-side consent gate (Stage H2 fix #12) — the Aviso de
        # Privacidad states consent is a CONDITION of processing; until
        # now that was only enforced by a disabled button client-side, so
        # a crafted request with no/false consent was processed anyway.
        # This is the earliest point the server is involved at all (the
        # recording itself happens client-side) — checked here, before
        # any transcription, storage, or job enqueue, so "condition of
        # processing" actually holds: no consent means the audio never
        # becomes a stored, processed note. Fails closed on anything
        # falsy/missing/malformed, matching the pre-existing 'false' parse
        # default — this adds the *enforcement*, the recording of consent
        # (params['consent_given'] below, persisted to the session) is
        # unchanged.
        consent_given = request.form.get('consent_given', 'false').lower() == 'true'
        if not consent_given:
            logger.warning("Validation: rejected upload — no patient consent recorded")
            return jsonify({
                'error': 'No se puede procesar el audio sin el consentimiento del paciente.',
                'error_code': 'CONSENT_REQUIRED'
            }), 400

        audio_file = request.files.get('audio')
        is_valid, error_message = validate_audio_file(audio_file)
        if not is_valid:
            logger.warning(f"Validation: rejected upload — {error_message}")
            return jsonify({'error': error_message, 'error_code': 'INVALID_AUDIO_FILE'}), 400

        # Capture all request-scoped values before entering the background thread
        usuario_id = g.usuario['usuario_id']
        clinica_id = g.usuario['clinica_id']
        nombre     = g.usuario.get('nombre', '')
        email      = g.usuario.get('email', '')

        local_timestamp = request.form.get('local_timestamp', datetime.now().strftime("%Y-%m-%d %H:%M"))
        params = {
            'print_raw':            request.form.get('print_raw', 'false').lower() == 'true',
            'speakers_expected':    int(request.form.get('speakers_expected', 2)),
            'local_timestamp':      local_timestamp,
            'consultation_timestamp': request.form.get('consultation_timestamp', local_timestamp),
            'consent_given':        consent_given,
            'consent_timestamp':    request.form.get('consent_timestamp', ''),
        }

        # Fetch clinic/cédula in request thread (g.usuario is available here)
        clinica     = get_clinica_context(clinica_id)
        cedula      = get_usuario_cedula(usuario_id)
        doctor_info = {
            'nombre':            nombre,
            'cedula':            cedula,
            'clinica_nombre':    clinica['nombre'],
            'clinica_color':     clinica['color_primario'],
            'clinica_direccion': clinica['direccion'],
            'clinica_telefono':  clinica['telefono'],
        }

        logger.info(f"Auth: upload by {email} (clinica_id={clinica_id})")

        filename  = secure_filename(audio_file.filename)
        temp_path = os.path.join(tempfile.gettempdir(),
                                 f"clinia_{datetime.now().timestamp()}_{filename}")
        audio_file.save(temp_path)
        logger.info(f"Job: audio saved to {temp_path} ({os.path.getsize(temp_path) / (1024*1024):.2f} MB)")

        job_id = _sb_post_job({
            'usuario_id': usuario_id,
            'clinica_id': clinica_id,
            'status':     'queued',
        })
        if not job_id:
            os.remove(temp_path)
            return jsonify({'error': 'No se pudo crear el trabajo de procesamiento'}), 500

        _executor.submit(_run_job, job_id, temp_path, params,
                         usuario_id, clinica_id, nombre, doctor_info)

        logger.info(f"Job {job_id}: submitted to executor")
        return jsonify({'job_id': job_id}), 202

    except Exception as e:
        rid = _new_request_id()
        logger.error(f"process_audio [{rid}]: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': 'Error interno', 'request_id': rid}), 500


@app.route('/api/job-status/<job_id>', methods=['GET'])
@require_auth
def job_status(job_id):
    """Poll processing status for a background job."""
    rows = _sb_get(
        f'/rest/v1/trabajos?job_id=eq.{pg_val(job_id)}'
        f'&select=job_id,status,error_message,session_id,structured_data,transcript,usuario_id,updated_at'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Trabajo no encontrado'}), 404
    row = rows[0]
    if row.get('usuario_id') != g.usuario['usuario_id']:
        return jsonify({'error': 'No autorizado'}), 403

    row = _reap_if_stale(job_id, row)  # Stage M4 fix #26 — self-heals a hung job on poll
    status = row['status']
    resp   = {'job_id': job_id, 'status': status}

    if status == 'done':
        resp['session_id']       = row['session_id']
        resp['structured_data']  = row['structured_data']
        resp['transcript']       = row['transcript']

    if status == 'error':
        resp['error_message'] = row.get('error_message', 'Error desconocido')

    return jsonify(resp), 200


@app.route('/api/confirm-and-generate', methods=['POST'])
@require_auth
# Not itself an AI-spend call (verified: no Gemini/AssemblyAI/ICD-11 call
# in this function — the LLM/transcription cost is in process_audio's
# background job) but shares process_audio's limit since it's the natural
# 1:1 follow-up per consultation and does its own real work (PDF render,
# email send, several Supabase writes) worth the same protection.
@limiter.limit("10 per minute;100 per hour")
def confirm_and_generate():
    """
    Receives doctor-reviewed structured_data, returns final response.
    Expected JSON: { "session_id": "...", "structured_data": {...} }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        session_id = data.get('session_id')
        structured_data = data.get('structured_data', {})
        create_pdf = data.get('create_pdf', False)
        # send_email only toggles WHETHER the PDF is emailed — it never
        # changes WHERE. The address itself is still never read from the
        # client (doctor_email below, derived from g.usuario) — Stage 2
        # fix #5 stays intact; this is a separate, later product request
        # for an on/off toggle, not a reopening of that fix.
        send_email = data.get('send_email', True)
        # doctor_email is NOT read from the client — a PHI document must
        # only go to the authenticated doctor's own registered address
        # (Stage 2 fix #5). Derived below from g.usuario, not the request.
        consent_tratamiento = {
            'given': data.get('consent_tratamiento_given', False),
            'timestamp': data.get('consent_tratamiento_timestamp', '')
        }

        logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")

        if not session_id:
            return jsonify({'error': 'Session not found'}), 404

        # Owner-only — confirming/signing a pending session is a write to a
        # not-yet-signed record, same rule as addenda (Stage 2 fix #4).
        # Reuses Stage 1's caller_can_addend_session rather than a parallel
        # check, since it's already exactly this owner-only rule.
        rows = _sb_get(
            f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}'
            f'&select=usuario_id,clinica_id,status,full_response&limit=1'
        )
        if not rows:
            return jsonify({'error': 'Session not found'}), 404

        row = rows[0]
        if not caller_can_addend_session(row, g.usuario):
            return jsonify({'error': 'Session not found'}), 404

        session = row.get('full_response')
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        # NOM-024 immutability — reject if already confirmed or cancelled
        current_status = row.get('status')
        if current_status == 'confirmed':
            return jsonify({
                'error': 'Esta nota ya fue confirmada y no puede modificarse. Para hacer correcciones, utilice la función de adéndum.',
                'error_code': 'SESSION_LOCKED'
            }), 409
        if current_status == 'cancelled':
            return jsonify({
                'error': 'Esta nota ha sido cancelada por solicitud ARCO y no puede modificarse.',
                'error_code': 'SESSION_CANCELLED'
            }), 409

        # Google Docs generation removed (Stage H3, finding #13) — every
        # clinic's doc used to land in one hardcoded Drive account with no
        # per-tenant separation; the frontend never requested one anyway
        # (always sent create_doc: false), so this was dormant-but-live
        # API surface with no product behind it. doc_info stays None
        # unconditionally; downstream response/save_session already
        # handle a None document correctly (that was already the normal
        # case before this removal).
        doc_info = None

        # PDF generation
        pdf_bytes = None
        if create_pdf:
            logger.info("Orchestrator: PHASE C2 — PDF Generation")
            try:
                clinica  = get_clinica_context(g.usuario['clinica_id'])
                cedula   = get_usuario_cedula(g.usuario['usuario_id'])
                doctor_info = {
                    'nombre':            g.usuario.get('nombre', ''),
                    'cedula':            cedula,
                    'clinica_nombre':    clinica['nombre'],
                    'clinica_color':     clinica['color_primario'],
                    'clinica_direccion': clinica['direccion'],
                    'clinica_telefono':  clinica['telefono'],
                }
                logo_bytes = fetch_clinica_logo_bytes(clinica['logo_url'])
                if logo_bytes:
                    doctor_info['clinica_logo_bytes'] = logo_bytes
                pdf_bytes = pdf_generator.generate_pdf(
                    structured_data, session_id=session_id, doctor_info=doctor_info
                )
                logger.info(f"PDF: Generated successfully — {len(pdf_bytes)} bytes")
            except Exception as e:
                logger.warning(f"PDF: Generation failed: {str(e)}")
                # Never raise — PDF failure must not block the pipeline

        locked_at = datetime.now().isoformat()

        response = {
            'session_id': session_id,
            'status': 'success',
            'timestamp': locked_at,
            'transcript': session.get('transcript'),
            'structured_data': structured_data,
            'document': doc_info,
            'pdf_available': True,
            # consent is stored top-level as consent_given/consent_timestamp
            # (written at creation — see _run_job/save_session), never under
            # a 'consent' key; that key was never written, so this always
            # read {} (Stage M4 fix #25).
            'consent_grabacion': {
                'given': session.get('consent_given', False),
                'timestamp': session.get('consent_timestamp'),
            },
            'consent_tratamiento': consent_tratamiento
        }

        # Persist confirmed session. usuario_id/clinica_id come from the
        # RECORDED row (already verified above to equal the caller), never
        # from g.usuario directly — ownership is fixed at creation and must
        # never be reassigned by whoever happens to confirm (Stage 2 fix #4).
        #
        # strip_transcript() is applied to the PERSISTED copy only — the
        # `response` dict below still echoes session.get('transcript') in
        # full, since that's this request's own one-time confirmation
        # response, not something retained (Stage H1 fix #10). `session`
        # itself is never mutated (strip_transcript returns a copy), so
        # building `response` before or after this call is equivalent.
        save_session(session_id, strip_transcript({
            **session,
            'structured_data': structured_data,
            'document': doc_info,
            'status': 'confirmed',
            'locked_at': locked_at,
            'timestamp': locked_at,
            'consent_tratamiento': consent_tratamiento
        }), usuario_id=row['usuario_id'], clinica_id=row['clinica_id'])

        # Delete transcript text to minimize LFPDPPP exposure
        ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}', {'transcript_text': None})
        if ok:
            logger.info(f"DB: Transcript text deleted for session {session_id} — LFPDPPP minimization")
        else:
            logger.warning(f"DB: Could not delete transcript text for {session_id}")

        # trabajos independently holds its own full copy of the transcript
        # (written when the job completed) with no remaining legitimate
        # reader once a session leaves pending_review — same reasoning
        # pending_sessions_discard already applies on discard. Best-effort:
        # its absence doesn't indicate anything is wrong (Stage H1 fix #10).
        if not _sb_delete(f'/rest/v1/trabajos?session_id=eq.{pg_val(session_id)}'):
            logger.warning(f"DB: could not delete trabajos row(s) for session {session_id} (best-effort, continuing)")

        # Send PDF to the authenticated doctor's own registered email only
        # (Stage 2 fix #5) — never a client-supplied address, to prevent
        # exfiltrating a PHI document to an arbitrary outside inbox.
        # send_email is the doctor's own on/off choice for this note; it
        # gates whether the send happens at all, not the address.
        doctor_email = g.usuario.get('email', '')
        if pdf_bytes and doctor_email and send_email:
            patient_name = (structured_data
                .get('informacion_paciente', {})
                .get('nombre_del_paciente', 'Paciente'))
            consultation_date = (structured_data
                .get('metadata', {})
                .get('fecha_hora_consulta', '')[:10])
            email_sent = send_pdf_email(
                doctor_email=doctor_email,
                pdf_bytes=pdf_bytes,
                patient_name=patient_name,
                consultation_date=consultation_date,
                session_id=session_id,
            )
            response['email_sent'] = email_sent
            response['email_address'] = doctor_email if email_sent else ''
        else:
            response['email_sent'] = False
            response['email_address'] = ''

        logger.info(f"Orchestrator: Confirmation complete. Session: {session_id}")
        return jsonify(response), 200

    except Exception as e:
        rid = _new_request_id()
        logger.error(f"Orchestrator [{rid}]: CRITICAL ERROR in confirm: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'request_id': rid}), 500


@app.route('/api/session/<session_id>', methods=['GET'])
@require_auth
def get_session(session_id):
    """Retrieve session data by ID. Owner-or-clinic scoped — see
    caller_can_read_session. 404 on any miss (not-found or out-of-scope),
    same convention as patient_history_detail."""
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    rows = _sb_get(
        f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}'
        f'&select=usuario_id,clinica_id,full_response&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Session not found'}), 404

    row = rows[0]
    if not caller_can_read_session(row, g.usuario):
        return jsonify({'error': 'Session not found'}), 404

    return jsonify(row.get('full_response')), 200


@app.route('/api/export-json/<session_id>', methods=['GET'])
@require_auth
def export_json(session_id):
    """Export structured data as downloadable JSON. Owner-or-clinic scoped —
    same rule as get_session, since this is the same data via another door."""
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    rows = _sb_get(
        f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}'
        f'&select=usuario_id,clinica_id,structured_data&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Session not found'}), 404

    row = rows[0]
    if not caller_can_read_session(row, g.usuario):
        return jsonify({'error': 'Session not found'}), 404

    data = row.get('structured_data')
    if not data:
        return jsonify({'error': 'Session not found'}), 404

    response = app.response_class(
        response=json.dumps(data, indent=2, ensure_ascii=False),
        mimetype='application/json'
    )
    response.headers['Content-Disposition'] = f'attachment; filename=clinia_{session_id}.json'

    return response


@app.route('/api/download-pdf/<session_id>', methods=['GET'])
@require_auth
def download_pdf(session_id):
    """
    Regenerate and stream PDF for a confirmed session. Accessible if the
    session belongs to the requesting doctor OR to their clinic (same
    OR pattern as GET /api/patient-history/<id>, item 24 Stage 4) — a
    covering doctor viewing a colleague's session via "Notas de la
    clínica" can already read the full structured_data, so a PDF of the
    same content isn't new exposure. 404 (never 403) for genuinely
    out-of-clinic sessions, consistent with every other route here.
    """
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    try:
        rows = _sb_get(
            f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}'
            f'&select=usuario_id,clinica_id,status,addenda,cancelled_at,cancellation_reason&limit=1'
        )
        if not rows:
            return jsonify({'error': 'Sesión no encontrada'}), 404

        row = rows[0]
        autor_usuario_id = row.get('usuario_id')
        is_owner  = autor_usuario_id == g.usuario['usuario_id']
        is_clinic = row.get('clinica_id') == g.usuario['clinica_id']
        if not (is_owner or is_clinic):
            return jsonify({'error': 'Sesión no encontrada'}), 404

        structured_data = load_structured_data(session_id)
        if not structured_data:
            return jsonify({'error': 'Datos de sesión no disponibles'}), 404

        # doctor_info reflects the SESSION'S ACTUAL AUTHOR, not the
        # downloading doctor — matches the Stage E admin-route convention.
        # A covering doctor downloading a colleague's note should get a
        # PDF correctly attributed to whoever actually saw that patient.
        clinica      = get_clinica_context(g.usuario['clinica_id'])
        cedula       = get_usuario_cedula(autor_usuario_id)
        doctor_info  = {
            'nombre':          get_usuario_nombre(autor_usuario_id),
            'cedula':          cedula,
            'clinica_nombre':  clinica['nombre'],
            'clinica_color':   clinica['color_primario'],
            'clinica_direccion': clinica['direccion'],
            'clinica_telefono':  clinica['telefono'],
        }
        logo_bytes = fetch_clinica_logo_bytes(clinica['logo_url'])
        if logo_bytes:
            doctor_info['clinica_logo_bytes'] = logo_bytes

        pdf_bytes = pdf_generator.generate_pdf(
            structured_data, session_id=session_id, doctor_info=doctor_info,
            adenda=row.get('addenda') or [],
            status=row.get('status'),
            cancelled_at=row.get('cancelled_at'),
            cancellation_reason=row.get('cancellation_reason'),
        )
        logger.info(f"PDF: Regenerated for download — session {session_id}, {len(pdf_bytes)} bytes")

        response = app.response_class(response=pdf_bytes, mimetype='application/pdf')
        response.headers['Content-Disposition'] = f'attachment; filename="ClinIA_{session_id}.pdf"'
        return response

    except Exception as e:
        logger.error(f"PDF: Download failed for {session_id}: {e}")
        return jsonify({'error': 'Error al generar el PDF'}), 500


# Error handlers
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        'error': 'File too large',
        'max_size_mb': 50
    }), 413


@app.route('/api/patient-history', methods=['GET'])
@require_auth
def patient_history_list():
    """
    GET /api/patient-history?curp=<curp>&scope=mine|clinica
    scope=mine (default): sessions written by the authenticated doctor.
    scope=clinica: sessions written by anyone in the doctor's clinic; each
    row includes the authoring doctor's name.
    """
    raw_curp = request.args.get('curp', '')
    curp = raw_curp.strip().upper()
    if not curp:
        return jsonify({'error': 'curp query parameter is required'}), 400

    scope = request.args.get('scope', 'mine')
    usuario_id = g.usuario['usuario_id']
    clinica_id = g.usuario['clinica_id']

    # Build select with PostgREST JSONB path:
    # -> for intermediate segments (returns jsonb), ->> only at the final leaf (returns text)
    # URL-encode > as %3E since urllib won't encode it automatically
    select = (
        'session_id,timestamp,status,usuario_id,'
        'structured_data->informacion_paciente->>motivo_de_consulta'
        .replace('>', '%3E')
    )

    if scope == 'clinica':
        scope_filter = f'clinica_id=eq.{pg_val(clinica_id)}'
    else:
        scope_filter = f'usuario_id=eq.{pg_val(usuario_id)}'

    path = (
        f'/rest/v1/sesiones'
        f'?paciente_curp=eq.{pg_val(curp)}'
        f'&{scope_filter}'
        f'&select={select}'
        f'&order=timestamp.desc'
    )
    rows = _sb_get(path)
    if rows is None:
        return jsonify({'error': 'Error al consultar historial'}), 500

    # Resolve authoring doctor names only when needed, one lookup per distinct author
    nombre_cache = {}
    if scope == 'clinica':
        for r in rows:
            row_usuario_id = r.get('usuario_id')
            if row_usuario_id and row_usuario_id != usuario_id and row_usuario_id not in nombre_cache:
                nombre_cache[row_usuario_id] = get_usuario_nombre(row_usuario_id)

    result = []
    for r in rows:
        row_usuario_id = r.get('usuario_id')
        item = {
            'session_id':         r.get('session_id'),
            'timestamp':          r.get('timestamp'),
            'status':             r.get('status'),
            'motivo_de_consulta': r.get('motivo_de_consulta'),
        }
        if scope == 'clinica' and row_usuario_id != usuario_id:
            item['doctor_nombre'] = nombre_cache.get(row_usuario_id) or ''
        result.append(item)

    return jsonify(result), 200


@app.route('/api/patient-history/<session_id>', methods=['GET'])
@require_auth
def patient_history_detail(session_id):
    """
    GET /api/patient-history/<session_id>
    Full read-only detail for one session. Accessible if the session belongs
    to the authenticated doctor OR to their clinic. 404 if neither (never 403).
    """
    usuario_id = g.usuario['usuario_id']
    clinica_id = g.usuario['clinica_id']

    rows = _sb_get(
        f'/rest/v1/sesiones'
        f'?session_id=eq.{pg_val(session_id)}'
        f'&select=session_id,usuario_id,clinica_id,timestamp,status,structured_data,addenda'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    is_owner  = row.get('usuario_id') == usuario_id
    is_clinic = row.get('clinica_id') == clinica_id
    if not (is_owner or is_clinic):
        return jsonify({'error': 'Sesión no encontrada'}), 404

    # Log the read — fire-and-forget, never fails the request. Logs the
    # READER's usuario_id regardless of which branch (owner/clinic) authorized it.
    try:
        _sb_log_lectura(session_id, usuario_id)
    except Exception as e:
        logger.warning(f"DB: Could not log lectura for session {session_id}: {e}")

    response = {
        'session_id':      row['session_id'],
        'timestamp':       row.get('timestamp'),
        'status':          row.get('status'),
        'structured_data': row['structured_data'],
        'addenda':         row.get('addenda') or [],
    }
    if not is_owner:
        response['autor_nombre'] = get_usuario_nombre(row.get('usuario_id'))

    return jsonify(response), 200


@app.route('/api/pending-sessions', methods=['GET'])
@require_auth
def pending_sessions_list():
    """
    List the CALLER'S OWN pending_review sessions — ownership-based, not
    clinica-wide, unlike patient-history's scope=clinica option. Only the
    doctor who can legally sign a note can complete it; nobody else
    (including an admin) has any special access to someone else's
    unfinished, unsigned draft here.
    """
    usuario_id = g.usuario['usuario_id']
    select = (
        'session_id,timestamp,'
        'structured_data->informacion_paciente->>motivo_de_consulta'
        .replace('>', '%3E')
    )
    rows = _sb_get(
        f'/rest/v1/sesiones'
        f'?usuario_id=eq.{pg_val(usuario_id)}'
        f'&status=eq.pending_review'
        f'&select={select}'
        f'&order=timestamp.desc'
    )
    if rows is None:
        return jsonify({'error': 'Error al consultar notas pendientes'}), 500

    return jsonify([
        {
            'session_id':         r.get('session_id'),
            'timestamp':          r.get('timestamp'),
            'motivo_de_consulta': r.get('motivo_de_consulta'),
        }
        for r in rows
    ]), 200


@app.route('/api/pending-sessions/<session_id>', methods=['GET'])
@require_auth
def pending_sessions_detail(session_id):
    """
    Full structured_data + transcript for one of the caller's own
    pending_review sessions — shaped identically to the job-status 'done'
    response so the frontend can reuse displayReviewScreen() unchanged.
    404 (never 403) if not found, not owned, or not pending_review —
    this route has no business surfacing a confirmed/cancelled session
    even to its own owner, that's patient-history's job.
    """
    usuario_id = g.usuario['usuario_id']
    rows = _sb_get(
        f'/rest/v1/sesiones'
        f'?session_id=eq.{pg_val(session_id)}'
        f'&select=session_id,usuario_id,status,full_response'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('usuario_id') != usuario_id or row.get('status') != 'pending_review':
        return jsonify({'error': 'Sesión no encontrada'}), 404

    full_response = row.get('full_response') or {}
    return jsonify({
        'session_id':      row['session_id'],
        'status':          row['status'],
        'structured_data': full_response.get('structured_data', {}),
        'transcript':      full_response.get('transcript', {}),
    }), 200


@app.route('/api/pending-sessions/<session_id>', methods=['DELETE'])
@require_auth
def pending_sessions_discard(session_id):
    """
    Permanently discard one of the caller's own pending_review sessions —
    a hard DELETE, deliberately NOT the soft-delete/status-flag pattern
    used for confirmed-session cancellation (item 37). A pending_review
    session that's never been confirmed was never a signed clinical
    record — there's nothing requiring retention, and a doctor discarding
    it may mean the patient withdrew consent or the recording was
    abandoned mid-encounter. Holding the raw transcript/structured_data
    indefinitely in that case works against LFPDPPP data minimization
    rather than serving any compliance purpose, unlike a cancelled-but-
    once-signed note, which keeps its audit trail because it WAS a record.

    Also deletes the matching trabajos row: it independently holds its
    own full copy of the same structured_data/transcript (written when
    the job completed), so leaving it behind would defeat the entire
    point of discarding — the sensitive data would just survive under a
    different table. trabajos cleanup is best-effort (logged, not
    blocking) since its absence doesn't indicate anything is wrong.
    lecturas_sesion rows are deliberately left untouched — that table is
    an access-audit log ("who read what, when"), not primary patient
    data, and audit trails are meant to survive deletion of the thing
    they logged access to, not be minimized alongside it.
    """
    usuario_id = g.usuario['usuario_id']
    rows = _sb_get(
        f'/rest/v1/sesiones'
        f'?session_id=eq.{pg_val(session_id)}'
        f'&select=usuario_id,status'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('usuario_id') != usuario_id or row.get('status') != 'pending_review':
        return jsonify({'error': 'Sesión no encontrada'}), 404

    trabajos_deleted = _sb_delete(f'/rest/v1/trabajos?session_id=eq.{pg_val(session_id)}')
    if not trabajos_deleted:
        logger.warning(f"Discard: could not delete trabajos row(s) for session {session_id} (best-effort, continuing)")

    ok = _sb_delete(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}')
    if not ok:
        return jsonify({'error': 'No se pudo descartar la nota'}), 500

    logger.info(f"Discard: session {session_id} permanently deleted by usuario_id={usuario_id}")
    return jsonify({'status': 'ok'}), 200


@app.route('/api/session-check', methods=['GET'])
# This app has no server-side login route — the actual login/password
# flow goes straight to Supabase Auth from the client, which rate-limits
# itself. This is the closest analog to an "auth-adjacent app route":
# hit right after every login and on every page load/pageshow. Note this
# is request-flood / abuse protection, not credential-guessing defense —
# a signed ES256 JWT can't be brute-forced (there's no signature to
# guess), so this doesn't stop password attacks; it caps how much CPU
# and DB lookup a script can burn per IP by hammering the endpoint with
# junk/replayed tokens. Still worth it as defense-in-depth. Order
# matters here and is deliberately the REVERSE of the AI-spend routes
# below: @limiter.limit is OUTER (runs before @require_auth), so a
# rejected (invalid/expired) attempt is counted too, not just a
# successful one — @require_auth would short-circuit on a bad token
# before an inner limiter ever ran. key_func is pinned to IP rather than
# the module default, since a rejected attempt has no g.usuario to key by.
@limiter.limit("30 per minute", key_func=get_remote_address)
@require_auth
def session_check():
    """Lightweight validity check — reload/bfcache-restore guard against a
    stale token that's present but expired. Also returns rol/clinica_id so
    the frontend can conditionally reveal admin-only UI without a second
    round-trip — no session data beyond that, no side effects."""
    return jsonify({
        'valid':      True,
        'rol':        g.usuario['rol'],
        'clinica_id': g.usuario['clinica_id'],
    }), 200


@app.route('/api/admin/usuarios', methods=['GET'])
@require_auth
@require_admin
def admin_usuarios():
    """List doctors in the admin's own clinic. Read-only — no add/deactivate yet."""
    clinica_id = g.usuario['clinica_id']
    rows = _sb_get(
        f'/rest/v1/usuarios'
        f'?clinica_id=eq.{pg_val(clinica_id)}'
        f'&select=id,nombre,email,especialidad,cedula,rol,activo'
        f'&order=nombre'
    )
    if rows is None:
        return jsonify({'error': 'Error al consultar usuarios'}), 500

    clinica = get_clinica_context(clinica_id)
    return jsonify({
        'clinica_nombre': clinica['nombre'],
        'doctores':       rows,
    }), 200


@app.route('/api/admin/usuarios', methods=['POST'])
@require_auth
@require_admin
@limiter.limit("20 per hour")  # per inviting admin — email-bombing resistance
def admin_create_usuario():
    """
    Invite a new doctor into the admin's own clinic. Creates the Supabase
    auth identity via generate_link (invite type), inserts the usuarios
    row, and emails the invite link via Resend.

    rol is ALWAYS forced to 'medico' and clinica_id ALWAYS taken from
    g.usuario — neither is ever read from the request body, so a tampered
    request cannot create an admin or a cross-clinic user.
    """
    data = request.get_json(silent=True) or {}
    nombre       = (data.get('nombre') or '').strip()
    email        = (data.get('email') or '').strip().lower()
    especialidad = (data.get('especialidad') or '').strip()
    cedula       = (data.get('cedula') or '').strip()

    if not nombre or not email or not especialidad or not cedula:
        return jsonify({'error': 'Todos los campos son obligatorios'}), 400
    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'Correo electrónico inválido'}), 400

    clinica_id = g.usuario['clinica_id']

    # Security boundary: check OUR OWN usuarios table for this email,
    # system-wide, BEFORE ever calling generate_link. We deliberately do
    # NOT rely on generate_link's own duplicate-email error behavior —
    # it isn't consistently reliable across Supabase versions/modes.
    #
    # Known limitation: this only catches emails that already have a
    # usuarios row. An email could theoretically exist in Supabase's
    # auth.users without a matching usuarios row (e.g. left over from a
    # prior orphaned-user failure below) and would not be caught here.
    # Acceptable gap: this app has no open registration — every real
    # account is created through this exact endpoint or manual seeding,
    # so that scenario should only ever arise from a previous failed
    # invite attempt, which is already flagged for manual cleanup when it
    # happens (see the orphaned-user handling further down).
    existing = _sb_get(f"/rest/v1/usuarios?email=ilike.{pg_ilike_val(email)}&select=id&limit=1")
    if existing is None:
        return jsonify({'error': 'Error al validar el correo electrónico'}), 500
    if existing:
        return jsonify({'error': 'Este correo ya está registrado'}), 409

    # Step (a): create the auth identity + invite link
    try:
        user_id, action_link = _sb_generate_invite_link(email)
    except Exception as e:
        logger.error(f"Admin: generate_link failed for {email}: {e}")
        return jsonify({'error': 'No se pudo crear la invitación. Intente de nuevo.'}), 500

    # Step (c): insert the usuarios row
    nuevo_usuario = _sb_insert_usuario({
        'id':           user_id,
        'nombre':       nombre,
        'email':        email,
        'especialidad': especialidad,
        'cedula':       cedula,
        'rol':          'medico',
        'clinica_id':   clinica_id,
        'activo':       True,
    })
    if nuevo_usuario is None:
        logger.error(
            f"Admin: ORPHANED AUTH USER — generate_link succeeded for {email} "
            f"(auth user_id={user_id}) but the usuarios insert failed. This "
            f"auth user now exists in Supabase with no matching usuarios row "
            f"and needs manual cleanup in the Supabase dashboard (Authentication "
            f"> Users) before this email can be invited again."
        )
        return jsonify({
            'error': (
                'La invitación se creó en el sistema de autenticación pero no '
                'se pudo guardar el registro del médico. Un usuario huérfano '
                'puede existir y requiere limpieza manual en el panel de '
                'Supabase antes de reintentar con este correo.'
            )
        }), 500

    # Step (d): send the invite email via Resend — failure here is less
    # severe than the orphaned-auth-user case above, since the usuarios
    # row already exists; just needs a manual resend, not dashboard cleanup.
    clinica = get_clinica_context(clinica_id)
    email_sent = send_invite_email(email, nombre, clinica['nombre'], action_link)

    response = {
        'id':           nuevo_usuario['id'],
        'nombre':       nuevo_usuario['nombre'],
        'email':        nuevo_usuario['email'],
        'especialidad': nuevo_usuario['especialidad'],
        'cedula':       nuevo_usuario['cedula'],
        'rol':          nuevo_usuario['rol'],
        'activo':       nuevo_usuario['activo'],
    }
    if not email_sent:
        logger.warning(f"Admin: doctor {email} created but invite email failed to send.")
        response['warning'] = 'Doctor creado pero el correo de invitación no pudo enviarse.'

    return jsonify(response), 200


@app.route('/api/admin/usuarios/<usuario_id>/activo', methods=['PATCH'])
@require_auth
@require_admin
def admin_set_usuario_activo(usuario_id):
    """
    Deactivate/reactivate a doctor within the admin's own clinic.

    Two independent enforcement layers, both required:
    1. Supabase ban (_sb_set_user_ban) — blocks future login attempts.
    2. usuarios.activo — checked on every @require_auth request via
       get_usuario_context, which is what actually cuts off an
       ALREADY-ISSUED session immediately. The Supabase ban alone would
       leave a currently-logged-in doctor with a working token for up to
       ~1h after deactivation, since banning does not invalidate a JWT
       already issued.
    """
    data = request.get_json(silent=True) or {}
    if 'activo' not in data or not isinstance(data['activo'], bool):
        return jsonify({'error': 'El campo activo (booleano) es obligatorio'}), 400
    nuevo_activo = data['activo']

    if usuario_id == g.usuario['usuario_id']:
        return jsonify({'error': 'No puedes desactivar tu propia cuenta'}), 403

    rows = _sb_get(
        f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}'
        f'&select=id,clinica_id&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    if rows[0].get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Usuario no encontrado'}), 404

    # Layer 1: Supabase ban/unban
    try:
        _sb_set_user_ban(usuario_id, banned=not nuevo_activo)
    except Exception as e:
        logger.error(
            f"Admin: Supabase ban update failed for usuario_id={usuario_id} "
            f"(target activo={nuevo_activo}): {e}. usuarios.activo was NOT "
            f"changed — no partial state applied."
        )
        return jsonify({'error': 'No se pudo actualizar el estado de autenticación del usuario. Intente de nuevo.'}), 500

    # Layer 2: usuarios.activo — the layer that cuts off an already-issued
    # session on the doctor's next request, not just future logins.
    updated = _sb_patch(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}', {'activo': nuevo_activo})
    if not updated:
        logger.error(
            f"Admin: INCONSISTENT STATE — Supabase ban succeeded (banned={not nuevo_activo}) "
            f"for usuario_id={usuario_id}, but the usuarios.activo UPDATE failed. "
            f"The Supabase auth side reflects activo={nuevo_activo} but the usuarios "
            f"table does not — this needs manual reconciliation."
        )
        return jsonify({
            'error': (
                'El estado de autenticación se actualizó correctamente, pero no se '
                'pudo guardar el cambio en la base de datos. El sistema quedó en un '
                'estado inconsistente y requiere revisión manual.'
            )
        }), 500

    return jsonify({'id': usuario_id, 'activo': nuevo_activo}), 200


HEX_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


@app.route('/api/admin/clinica', methods=['GET'])
@require_auth
@require_admin
def admin_get_clinica():
    """Marca Stage 1 — read the admin's own clinic profile (name, color, address, phone)."""
    clinica_id = g.usuario['clinica_id']
    rows = _sb_get(
        f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}'
        f'&select=nombre,color_primario,direccion,telefono&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Clínica no encontrada'}), 404

    row = rows[0]
    return jsonify({
        'nombre':         row.get('nombre') or '',
        'color_primario': row.get('color_primario') or '',
        'direccion':      row.get('direccion') or '',
        'telefono':       row.get('telefono') or '',
    }), 200


@app.route('/api/admin/clinica', methods=['PATCH'])
@require_auth
@require_admin
def admin_update_clinica():
    """
    Marca Stage 1 — partial update of color_primario/direccion/telefono.
    clinica_id always from g.usuario, never client-supplied. nombre and
    logo_url are out of scope for this pass (logo is Stage 2).
    """
    data = request.get_json(silent=True) or {}
    clinica_id = g.usuario['clinica_id']

    body = {}
    if 'color_primario' in data:
        color = (data.get('color_primario') or '').strip()
        if not HEX_COLOR_RE.match(color):
            return jsonify({'error': 'Color inválido — debe ser un hex de 6 dígitos, ej. #0F6E56'}), 400
        body['color_primario'] = color
    if 'direccion' in data:
        body['direccion'] = (data.get('direccion') or '').strip()
    if 'telefono' in data:
        body['telefono'] = (data.get('telefono') or '').strip()

    if not body:
        return jsonify({'error': 'No se proporcionaron campos para actualizar'}), 400

    ok = _sb_patch(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}', body)
    if not ok:
        logger.error(f"Admin: could not update clinica {clinica_id}")
        return jsonify({'error': 'No se pudo guardar el perfil de la clínica'}), 500

    logger.info(f"Admin: clinica {clinica_id} profile updated by usuario_id={g.usuario['usuario_id']}")
    return jsonify(body), 200


ALLOWED_LOGO_FORMATS = {'PNG', 'JPEG'}
MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


@app.route('/api/admin/clinica/logo', methods=['POST'])
@require_auth
@require_admin
def admin_upload_clinica_logo():
    """
    Marca Stage 2 — upload/replace the clinic's logo in Supabase Storage
    (private bucket 'clinic-logos', proxied through this app — never a
    public URL). clinicas.logo_url stores a STORAGE OBJECT PATH
    ("logos/<clinica_id>/logo"), NOT a real URL, despite the column name.

    Fixed path per clinic, no file extension — Content-Type is tracked as
    Storage object metadata, not encoded in the path — so a re-upload
    always replaces the same object regardless of format changes across
    uploads (no orphaned old-format file left behind).
    """
    clinica_id = g.usuario['clinica_id']

    file = request.files.get('logo')
    if not file or file.filename == '':
        return jsonify({'error': 'No se recibió ningún archivo de logo'}), 400

    raw_bytes = file.read()
    if len(raw_bytes) > MAX_LOGO_SIZE_BYTES:
        return jsonify({'error': 'El archivo es demasiado grande. Tamaño máximo permitido: 2 MB.'}), 400

    # Validate actual file content via Pillow — the claimed Content-Type
    # and file extension are both trivially spoofable, never trusted.
    # A non-image (including SVG, which PIL has no raster decoder for)
    # fails at open() with UnidentifiedImageError.
    try:
        img = PILImage.open(BytesIO(raw_bytes))
        img.verify()
        image_format = img.format
    except Exception:
        return jsonify({'error': 'El archivo no es una imagen PNG o JPEG válida'}), 400

    if image_format not in ALLOWED_LOGO_FORMATS:
        return jsonify({
            'error': f"Formato no permitido: '{image_format}'. Solo se aceptan PNG o JPEG (SVG no soportado)."
        }), 400

    content_type   = 'image/png' if image_format == 'PNG' else 'image/jpeg'
    storage_path   = f'logos/{clinica_id}/logo'

    ok = _storage_upload(CLINIC_LOGOS_BUCKET, storage_path, raw_bytes, content_type)
    if not ok:
        return jsonify({'error': 'No se pudo subir el logo'}), 500

    updated = _sb_patch(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}', {'logo_url': storage_path})
    if not updated:
        logger.error(f"Admin: logo uploaded to storage but clinicas.logo_url update failed for {clinica_id}")
        return jsonify({'error': 'Logo subido pero no se pudo guardar la referencia. Intente de nuevo.'}), 500

    logger.info(f"Admin: logo uploaded for clinica {clinica_id} by usuario_id={g.usuario['usuario_id']}")
    return jsonify({'logo_url': storage_path}), 200


@app.route('/api/admin/clinica/logo', methods=['GET'])
@require_auth
@require_admin
def admin_get_clinica_logo():
    """
    Proxy GET — the browser never talks to Supabase Storage directly
    (private bucket). Streams the admin's own clinic's logo bytes back
    with the Content-Type Storage has on record for that object.
    """
    clinica_id = g.usuario['clinica_id']
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}&select=logo_url&limit=1')
    if not rows or not rows[0].get('logo_url'):
        return jsonify({'error': 'Esta clínica no tiene logo configurado'}), 404

    result = _storage_download(CLINIC_LOGOS_BUCKET, rows[0]['logo_url'])
    if result is None:
        return jsonify({'error': 'No se pudo obtener el logo'}), 404

    image_bytes, content_type = result
    return app.response_class(response=image_bytes, mimetype=content_type)


@app.route('/api/admin/clinica/logo', methods=['DELETE'])
@require_auth
@require_admin
def admin_delete_clinica_logo():
    """Remove the clinic's logo from Storage and clear clinicas.logo_url."""
    clinica_id = g.usuario['clinica_id']
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}&select=logo_url&limit=1')
    if not rows or not rows[0].get('logo_url'):
        return jsonify({'error': 'Esta clínica no tiene logo configurado'}), 404

    storage_path = rows[0]['logo_url']
    _storage_delete(CLINIC_LOGOS_BUCKET, storage_path)  # best-effort — clear logo_url regardless

    updated = _sb_patch(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}', {'logo_url': None})
    if not updated:
        logger.error(f"Admin: could not clear logo_url for clinica {clinica_id}")
        return jsonify({'error': 'No se pudo actualizar la clínica'}), 500

    logger.info(f"Admin: logo removed for clinica {clinica_id} by usuario_id={g.usuario['usuario_id']}")
    return jsonify({'status': 'ok'}), 200


@app.route('/api/admin/sessions', methods=['GET'])
@require_auth
@require_admin
def admin_sessions():
    """
    ADM-1 Stage D — ARCO session search, list-only. Returns session_id,
    timestamp, doctor_nombre, status, tiene_adenda for each matching
    session. Does NOT return structured_data or full note content —
    that's Stage E (detail view).
    """
    clinica_id = g.usuario['clinica_id']

    desde = request.args.get('desde', '').strip()
    hasta = request.args.get('hasta', '').strip()
    usuario_id_filter = request.args.get('usuario_id', '').strip()

    # Default to the last 30 days rather than an unbounded clinic-wide
    # history query when no range is given.
    if not desde and not hasta:
        hasta_dt = datetime.now()
        desde_dt = hasta_dt - timedelta(days=30)
        desde = desde_dt.strftime('%Y-%m-%d')
        hasta = hasta_dt.strftime('%Y-%m-%d')

    filters = [f'clinica_id=eq.{pg_val(clinica_id)}']
    if desde and hasta:
        # Explicit and=(...) combinator rather than repeating timestamp=
        # twice — PostgREST documents AND-by-default for repeated same-
        # column params, but also documents this explicit form as the
        # unambiguous way to combine multiple conditions on ONE column.
        # hasta is a date (YYYY-MM-DD); T23:59:59.999 makes it inclusive
        # of the whole day. desde/hasta are unvalidated query params —
        # pg_val() so a value like "2026-01-01&select=*" can't smuggle
        # extra PostgREST parameters into this service_role query
        # (Stage M1 fix #16); the and=(...) structure itself is untouched.
        filters.append(f'and=(timestamp.gte.{pg_val(desde)},timestamp.lte.{pg_val(hasta)}T23:59:59.999)')
    elif desde:
        filters.append(f'timestamp=gte.{pg_val(desde)}')
    elif hasta:
        filters.append(f'timestamp=lte.{pg_val(hasta)}T23:59:59.999')
    if usuario_id_filter:
        filters.append(f'usuario_id=eq.{pg_val(usuario_id_filter)}')

    try:
        limit = int(request.args.get('limit', 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0
    offset = max(0, offset)

    # Fetch one extra row to cheaply detect a next page without needing
    # Prefer: count=exact / Content-Range — good enough for next/previous
    # controls, no total-page-count requirement.
    path = (
        f'/rest/v1/sesiones'
        f'?{"&".join(filters)}'
        f'&select=session_id,timestamp,usuario_id,status,addenda'
        f'&order=timestamp.desc'
        f'&limit={limit + 1}'
        f'&offset={offset}'
    )
    rows = _sb_get(path)

    if rows is None:
        return jsonify({'error': 'Error al consultar sesiones'}), 500

    has_more = len(rows) > limit
    rows = rows[:limit]

    # Resolve doctor names via a plain second query per DISTINCT usuario_id
    # (not a PostgREST embed — same reasoning as item 24 Stage 4: embeds
    # couldn't be verified reliably). This is new caching logic for this
    # stage, not reused/tested code from elsewhere.
    nombre_cache = {}
    for r in rows:
        uid = r.get('usuario_id')
        if uid and uid not in nombre_cache:
            nombre_cache[uid] = get_usuario_nombre(uid)

    sessions = [
        {
            'session_id':    r.get('session_id'),
            'timestamp':     r.get('timestamp'),
            'doctor_nombre': nombre_cache.get(r.get('usuario_id'), ''),
            'status':        r.get('status'),
            'tiene_adenda':  bool(r.get('addenda')) and len(r.get('addenda')) > 0,
        }
        for r in rows
    ]
    return jsonify({'sessions': sessions, 'has_more': has_more}), 200


@app.route('/api/admin/session/<session_id>/pdf', methods=['GET'])
@require_auth
@require_admin
def admin_download_pdf(session_id):
    """
    ADM-1 Stage E — admin-authorized PDF download for any session in the
    admin's own clinic. Deliberately a SEPARATE route from the doctor's
    owner-only /api/download-pdf/<id> — that endpoint is not broadened to
    clinic-wide, this is its own explicitly-gated admin route, reusing the
    same underlying pdf_generator invocation.

    doctor_info reflects the SESSION'S AUTHOR (not the admin viewing it) —
    the PDF represents that doctor's clinical note.
    """
    try:
        rows = _sb_get(
            f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}'
            f'&select=usuario_id,clinica_id,structured_data,addenda,status,cancelled_at,cancellation_reason&limit=1'
        )
        if not rows:
            return jsonify({'error': 'Sesión no encontrada'}), 404

        row = rows[0]
        if row.get('clinica_id') != g.usuario['clinica_id']:
            return jsonify({'error': 'Sesión no encontrada'}), 404

        structured_data = row.get('structured_data')
        if not structured_data:
            return jsonify({'error': 'Datos de sesión no disponibles'}), 404

        autor_usuario_id = row.get('usuario_id')
        clinica      = get_clinica_context(row.get('clinica_id'))
        cedula       = get_usuario_cedula(autor_usuario_id)
        logo_bytes   = fetch_clinica_logo_bytes(clinica['logo_url'])
        doctor_info  = {
            'nombre':            get_usuario_nombre(autor_usuario_id),
            'cedula':            cedula,
            'clinica_nombre':    clinica['nombre'],
            'clinica_color':     clinica['color_primario'],
            'clinica_direccion': clinica['direccion'],
            'clinica_telefono':  clinica['telefono'],
        }
        if logo_bytes:
            doctor_info['clinica_logo_bytes'] = logo_bytes

        pdf_bytes = pdf_generator.generate_pdf(
            structured_data, session_id=session_id, doctor_info=doctor_info,
            adenda=row.get('addenda') or [],
            status=row.get('status'),
            cancelled_at=row.get('cancelled_at'),
            cancellation_reason=row.get('cancellation_reason'),
        )
        logger.info(f"PDF: Admin-regenerated for download — session {session_id}, {len(pdf_bytes)} bytes")

        response = app.response_class(response=pdf_bytes, mimetype='application/pdf')
        response.headers['Content-Disposition'] = f'attachment; filename="ClinIA_{session_id}.pdf"'
        return response

    except Exception as e:
        logger.error(f"PDF: Admin download failed for {session_id}: {e}")
        return jsonify({'error': 'Error al generar el PDF'}), 500


@app.route('/api/admin/session/<session_id>/addendum', methods=['POST'])
@require_auth
@require_admin
def admin_add_addendum(session_id):
    """
    ADM-1 Stage F — admin-authored addendum (ARCO rectification), paired
    with the pdf_generator.py adenda-rendering section added this stage.

    Field shape — id, type, text, author, timestamp — matches what
    session-detail-render.js's adenda renderer already expects (unchanged
    this stage). author_usuario_id is an ADDITIONAL field beyond that
    shape, recording the specific admin account that wrote it (an
    unspoofable link server-side sets, never client-supplied) — the
    renderer simply ignores it, it isn't a conflicting field.

    (The doctor-facing POST /api/session/<id>/addendum route this
    originally matched conventions with was removed in the post-
    remediation cleanup pass — it had zero frontend callers.)
    """
    MAX_ADDENDUM_LENGTH = 2000

    data = request.get_json(silent=True) or {}
    texto = (data.get('texto') or '').strip()

    if not texto:
        return jsonify({'error': 'El texto del adendum no puede estar vacío'}), 400
    if len(texto) > MAX_ADDENDUM_LENGTH:
        return jsonify({'error': f'El texto no puede exceder {MAX_ADDENDUM_LENGTH} caracteres'}), 400

    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}&select=clinica_id,addenda&limit=1')
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    addenda = row.get('addenda') or []

    new_entry = {
        'id':                f"adendum_{len(addenda) + 1}",
        'type':              'rectificacion_arco',
        'text':              texto,
        'author':            g.usuario.get('nombre', ''),
        'timestamp':         datetime.now().isoformat(),
        'author_usuario_id': g.usuario['usuario_id'],
    }
    addenda.append(new_entry)  # append, never replace/overwrite existing entries

    ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}', {'addenda': addenda})
    if not ok:
        logger.error(f"Admin: could not save addendum for session {session_id}")
        return jsonify({'error': 'No se pudo guardar el adendum'}), 500

    logger.info(f"Admin: addendum added to session {session_id} by usuario_id={g.usuario['usuario_id']}")
    return jsonify({'addenda': addenda}), 200


@app.route('/api/admin/session/<session_id>/cancel', methods=['POST'])
@require_auth
@require_admin
def admin_cancel_session(session_id):
    """
    ADM-1 Stage G — admin-authorized session cancellation (soft-delete /
    bloqueo, ARCO Cancelación). Replaces the old DELETE /api/session/<id>
    route, which was removed in this same change: that route had NO
    ownership/clinic scoping at all (any authenticated user could cancel
    any session in any clinic), had zero frontend callers anywhere in the
    repo, and silently defaulted cancellation_reason from a query param
    if omitted. This route fixes all three: admin-gated + clinic-scoped,
    a required non-empty reason, and cancelled_by_usuario_id recording
    WHO cancelled it (same reasoning as adenda's author_usuario_id —
    a reason string alone isn't a reliable audit trail of who acted).

    Preserves the retention mechanics the old route already got right,
    unchanged: status='cancelled', transcript_text cleared immediately,
    clinical data otherwise retained (NOM-004 5-year minimum) rather than
    hard-deleted. PDF download remains available for cancelled sessions —
    neither PDF route filters on status — since NOM-004 retention implies
    the record must stay accessible regardless of cancellation status.
    """
    data = request.get_json(silent=True) or {}
    cancellation_reason = (data.get('cancellation_reason') or '').strip()

    if not cancellation_reason:
        return jsonify({'error': 'El motivo de cancelación es obligatorio'}), 400

    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}&select=clinica_id,status,full_response&limit=1')
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    # A session can be cancelled directly from pending_review (never
    # confirmed) or from confirmed — either way this is a point the
    # session leaves the pending_review window, so full_response gets the
    # same transcript strip confirm_and_generate applies (Stage H1 fix
    # #10). strip_transcript is idempotent on an already-stripped payload.
    #
    # Also correct full_response['status'] to 'cancelled' here — found via
    # live testing that this route had never updated it (pre-existing
    # since ADM-1 Stage G, not introduced by this fix): the top-level
    # sesiones.status column was always correctly set to 'cancelled', but
    # full_response's OWN embedded status key was left stale forever
    # after. Not user-visible today (every real UI path reads status from
    # the top-level column — patient_history_detail, admin_sessions —
    # never from inside full_response), but fixing it here is a one-key
    # addition to the exact dict this fix already rewrites, not new scope.
    stripped_full_response = strip_transcript(row.get('full_response') or {})
    stripped_full_response['status'] = 'cancelled'

    ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{pg_val(session_id)}', {
        'status':                  'cancelled',
        'cancelled_at':            datetime.now().isoformat(),
        'cancellation_reason':     cancellation_reason,
        'cancelled_by_usuario_id': g.usuario['usuario_id'],
        'transcript_text':         None,
        'full_response':           stripped_full_response,
    })
    if not ok:
        logger.error(f"Admin: could not cancel session {session_id}")
        return jsonify({'error': 'No se pudo cancelar la nota'}), 500

    # Same reasoning as confirm_and_generate — trabajos has no remaining
    # legitimate reader once a session leaves pending_review. Best-effort.
    if not _sb_delete(f'/rest/v1/trabajos?session_id=eq.{pg_val(session_id)}'):
        logger.warning(f"DB: could not delete trabajos row(s) for session {session_id} (best-effort, continuing)")

    logger.info(f"Admin: session {session_id} cancelled by usuario_id={g.usuario['usuario_id']}")
    return jsonify({
        'session_id': session_id,
        'status':     'cancelled',
        'message':    'La sesión ha sido bloqueada conforme al derecho de cancelación LFPDPPP. '
                      'Los datos clínicos se conservan durante el período de retención obligatorio '
                      'de 5 años (NOM-004) y serán eliminados definitivamente al vencimiento de dicho plazo.',
    }), 200


@app.errorhandler(500)
def internal_server_error(error):
    rid = _new_request_id()
    logger.exception(f"Unhandled 500 [{rid}]: {error}")
    return jsonify({
        'error': 'Internal server error',
        'request_id': rid,
    }), 500


# Runs once per process start — both under gunicorn (module import) and
# `python app.py` locally. Stage M4 fix #26; see the reaper section above
# for the single-instance assumption this relies on.
_reap_stuck_jobs_on_startup()


if __name__ == '__main__':
    # This block only runs when you run 'python app.py' on your computer.
    # It does NOT run on Render.
    logger.info("ClinIA Beta - Medical Note Taker (Local Mode) — starting Flask server")

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
