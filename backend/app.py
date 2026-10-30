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
from email_service import (
    send_pdf_email, send_invite_email, send_solicitud_notice,
    send_usuario_agregado_email, send_rejection_email, REJECTION_EMAIL_DRAFT,
)
from auth import (
    require_auth, require_admin, require_medico, require_superadmin, invalidate_usuario_cache,
    is_admin_role, is_medico_role, is_superadmin,
    ROL_ADMIN, ROL_MEDICO, ROL_AUXILIAR, ADMIN_ROLES, ROL_LABELS,
)
from pg_utils import pg_val, pg_path, pg_ilike_val
from solicitud_validation import validate_solicitud
from superadmin_utils import suggest_clinica
from concurrent.futures import ThreadPoolExecutor
import os
import re
import secrets
import tempfile
import glob
import json
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
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
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_AUDIO_SIZE_BYTES  # single source of truth (Stage M5 fix #27)

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
        "methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
    if request.endpoint == 'create_solicitud':
        _log_solicitud('rate-limited')
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
# Stage E2 fix #38 — capacity note, verified not a code bug:
# MAX_CONTENT_LENGTH (200MB) x these 4 worker threads is a real ceiling
# on disk, not RAM, on a small Render instance. Verified: Werkzeug's
# default multipart stream factory (SpooledTemporaryFile, max_size=
# 500KB) only holds an upload in memory below 500KB — every real audio
# upload here is far larger and spools to a real temp file on disk
# almost immediately, so 4 concurrent max-size uploads is up to ~800MB
# of transient disk usage, not RAM. Temp-file cleanup is prompt (the
# _run_job finally/except paths + the Stage M4 startup-reaper sweep for
# anything a crash left behind) — confirmed unchanged since M4. This is
# a documented capacity ceiling, not a fix: the real levers if it
# becomes a problem are the deferred audio-compression work, a smaller
# MAX_AUDIO_SIZE_BYTES cap, or fewer workers — not something to change
# here speculatively.
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


def _sb_insert_clinica(nombre: str, direccion: str, telefono: str) -> dict | None:
    """
    Insert a clinicas row and return it (Prefer: return=representation),
    or None on error. color_primario/logo_url are left to their column
    defaults — get_clinica_context already renders absent values as the
    house color and no logo. Only caller: superadmin_aprobar (#53).
    Needs INSERT on clinicas for service_role.
    """
    url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/clinicas'
    payload = json.dumps({
        'nombre':    nombre,
        'direccion': direccion,
        'telefono':  telefono,
    }, ensure_ascii=False).encode()
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
        logger.error(f"DB: Could not insert clinicas row: {e}")
        return None


# ── Public access requests (solicitudes_registro, #53/#54) ──────────────────
# These two helpers serve the UNAUTHENTICATED POST /api/solicitudes, whose
# query paths and error bodies carry the applicant's email. _sb_get logs
# the full path on failure and PostgREST's 409 body echoes the duplicate
# key — so neither is used there. These log the table and status only.
# _sb_get itself is unchanged for every other route.

def _sb_get_quiet(path: str) -> list | None:
    """Same contract as _sb_get (list, or None on error) without the path
    or response body in any log line."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    table = path.split('?', 1)[0].rsplit('/', 1)[-1]
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase GET {table} failed {e.code}")
        return None
    except Exception as e:
        logger.warning(f"DB: Supabase GET {table} error ({type(e).__name__})")
        return None


def _sb_insert_solicitud(body: dict) -> str:
    """
    Insert a solicitudes_registro row. Returns 'created', 'duplicate'
    (the partial unique index on lower(email) for open requests — two
    submits racing past the pre-check land here: PostgREST maps
    unique_violation 23505 to HTTP 409), or 'error'. return=minimal: the
    route never needs the row back. Needs INSERT on solicitudes_registro
    for service_role.
    """
    url = Config.SUPABASE_URL.rstrip('/') + '/rest/v1/solicitudes_registro'
    payload = json.dumps(body, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers=_sb_headers(extra={'Prefer': 'return=minimal'}),
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return 'created'
    except urllib.error.HTTPError as e:
        code = None
        try:
            code = json.loads(e.read()).get('code')
        except Exception:
            pass
        if e.code == 409 and code == '23505':
            return 'duplicate'
        logger.error(f"DB: solicitudes_registro insert failed {e.code} {code or ''}".rstrip())
        return 'error'
    except Exception as e:
        logger.error(f"DB: solicitudes_registro insert error ({type(e).__name__})")
        return 'error'


# ── Superadmin review (#53, Review C) — quiet helpers ───────────────────────
# Every path and Auth response below can carry an applicant's email or
# phone, so like _sb_get_quiet these log the table (or Auth endpoint) and
# status only — never the path, the body or the exception message.

def _sb_patch_returning_quiet(path: str, body: dict) -> list | None:
    """PATCH with return=representation: the updated rows (an empty list
    when the filter matched nothing — that is how a conditional claim
    like estado=eq.pendiente learns it lost), or None on error."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    table = path.split('?', 1)[0].rsplit('/', 1)[-1]
    data = json.dumps(body, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(
        url, data=data, method='PATCH',
        headers=_sb_headers(extra={'Prefer': 'return=representation'}),
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase PATCH {table} failed {e.code}")
        return None
    except Exception as e:
        logger.warning(f"DB: Supabase PATCH {table} error ({type(e).__name__})")
        return None


def _sb_delete_quiet(path: str) -> bool:
    """DELETE; True on success (including zero rows matched)."""
    url = Config.SUPABASE_URL.rstrip('/') + path
    table = path.split('?', 1)[0].rsplit('/', 1)[-1]
    req = urllib.request.Request(url, headers=_sb_headers(), method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase DELETE {table} failed {e.code}")
        return False
    except Exception as e:
        logger.warning(f"DB: Supabase DELETE {table} error ({type(e).__name__})")
        return False


def _sb_count_quiet(path: str) -> int | None:
    """
    Row count for a filtered table without fetching rows: a HEAD request
    with Prefer: count=exact (what supabase-js sends for
    {count: 'exact', head: true}); PostgREST answers with no body and the
    total after the slash in Content-Range ("0-4/5", or "*/0" when
    nothing matches). None on any error or an unparseable header.
    """
    url = Config.SUPABASE_URL.rstrip('/') + path
    table = path.split('?', 1)[0].rsplit('/', 1)[-1]
    req = urllib.request.Request(
        url, method='HEAD',
        headers=_sb_headers(extra={'Prefer': 'count=exact'}),
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            total = (resp.headers.get('Content-Range') or '').rsplit('/', 1)[-1]
        return int(total)
    except urllib.error.HTTPError as e:
        logger.warning(f"DB: Supabase COUNT {table} failed {e.code}")
        return None
    except Exception as e:
        logger.warning(f"DB: Supabase COUNT {table} error ({type(e).__name__})")
        return None


def _sb_get_auth_user(user_id: str) -> tuple[str, dict | None]:
    """
    GET {SUPABASE_URL}/auth/v1/admin/users/{id} (the endpoint behind
    auth-js admin.getUserById). Returns ('ok', user), ('missing', None)
    on 404, or ('error', None). The user object carries the email, so
    nothing from it is logged.
    """
    url = Config.SUPABASE_URL.rstrip('/') + f'/auth/v1/admin/users/{pg_val(user_id)}'
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 'ok', json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 'missing', None
        logger.warning(f"Auth admin: get user failed {e.code}")
        return 'error', None
    except Exception as e:
        logger.warning(f"Auth admin: get user error ({type(e).__name__})")
        return 'error', None


def _sb_delete_auth_user(user_id: str) -> str:
    """
    DELETE {SUPABASE_URL}/auth/v1/admin/users/{id} (auth-js
    admin.deleteUser; hard delete). The usuarios row goes with it
    (ON DELETE CASCADE). 'deleted', 'missing' (404 — already gone, so a
    resumed delete just moves on) or 'error'. The DB refuses the delete
    while sesiones/trabajos/lecturas_sesion rows reference the user.
    """
    url = Config.SUPABASE_URL.rstrip('/') + f'/auth/v1/admin/users/{pg_val(user_id)}'
    req = urllib.request.Request(url, headers=_sb_headers(), method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return 'deleted'
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 'missing'
        logger.warning(f"Auth admin: delete user failed {e.code}")
        return 'error'
    except Exception as e:
        logger.warning(f"Auth admin: delete user error ({type(e).__name__})")
        return 'error'


def _sb_generate_recovery_link(email: str) -> str:
    """
    A fresh set-password link for an auth user that ALREADY exists
    (superadmin "Reenviar", and an approval resumed after the user was
    created). Type 'recovery', not 'invite' or 'magiclink' — verified in
    the GoTrue source (supabase/auth internal/api/mail.go,
    adminGenerateLink): recovery for a missing email is a 404 and never
    creates a user, while magiclink silently signs a missing email up.
    Verifying a recovery token also confirms a still-unconfirmed user
    (verify.go, recoverVerify), and supabase-js reports the redirect as
    PASSWORD_RECOVERY, which /set-password already handles. Raises on
    failure; callers log the exception type only.
    """
    url = Config.SUPABASE_URL.rstrip('/') + '/auth/v1/admin/generate_link'
    payload = json.dumps({
        'type': 'recovery',
        'email': email,
        'redirect_to': 'https://app.clinianotes.com/set-password',
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=payload, headers=_sb_headers(), method='POST')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data['action_link']


class ProvisioningError(Exception):
    """
    Raised by _provision_usuario. `stage` says which side failed, because
    the two need different handling by the caller:
      'invite' — generate_link failed; nothing was created anywhere.
      'record' — the auth user exists but the caller's on_auth_user hook
                 failed (superadmin approval could not save usuario_id);
                 no usuarios row was attempted.
      'insert' — the auth user EXISTS but has no usuarios row (orphan,
                 already logged here for manual cleanup in Supabase).
    `user_id` is the auth user's id whenever one was created.
    """
    def __init__(self, stage: str, user_id: str = None):
        super().__init__(stage)
        self.stage = stage
        self.user_id = user_id


def _insert_usuario_row(user_id: str, nombre: str, email: str, rol: str, clinica_id: str,
                        cedula: str = None, especialidad: str = None,
                        universidad: str = None) -> dict | None:
    """
    Insert the usuarios row for an existing auth user (activo=True).
    Optional fields left as None are omitted rather than sent as null, so
    an auxiliar's row simply has no cédula/especialidad. Returns the row
    or None. Split out of _provision_usuario so a resumed superadmin
    approval can insert the row for an auth user it already created.
    """
    body = {
        'id':         user_id,
        'nombre':     nombre,
        'email':      email,
        'rol':        rol,
        'clinica_id': clinica_id,
        'activo':     True,
    }
    for key, value in (('cedula', cedula), ('especialidad', especialidad),
                       ('universidad', universidad)):
        if value is not None:
            body[key] = value
    return _sb_insert_usuario(body)


def _provision_usuario(nombre: str, email: str, rol: str, clinica_id: str,
                       cedula: str = None, especialidad: str = None,
                       universidad: str = None, on_auth_user=None) -> tuple[dict, str]:
    """
    Create one account: the Supabase auth identity + invite link, then
    its usuarios row (activo=True). Returns (usuarios_row, action_link);
    raises ProvisioningError otherwise. Does NOT validate input, check
    for a duplicate email, or send the invite — callers own those, since
    each caller has its own rules for who may create which rol where
    (admin_create_usuario: always the caller's clinic; superadmin
    approval: the clinic it chose or created).

    on_auth_user(user_id) -> bool, if given, runs right after the auth
    user exists and before the usuarios insert; False aborts with stage
    'record'. Superadmin approval uses it to save usuario_id first, so a
    retry never creates a second auth user.

    Logs carry the auth user_id only, never the email: this also runs
    for public applicants (superadmin approval).
    """
    try:
        user_id, action_link = _sb_generate_invite_link(email)
    except Exception as e:
        status = getattr(e, 'code', None)
        logger.error(f"Provisioning: generate_link failed ({type(e).__name__}{f' {status}' if status else ''})")
        raise ProvisioningError('invite')

    if on_auth_user is not None and not on_auth_user(user_id):
        logger.error(f"Provisioning: auth user_id={user_id} created but the caller could not record it")
        raise ProvisioningError('record', user_id)

    nuevo_usuario = _insert_usuario_row(user_id, nombre, email, rol, clinica_id,
                                        cedula=cedula, especialidad=especialidad,
                                        universidad=universidad)
    if nuevo_usuario is None:
        logger.error(
            f"Provisioning: ORPHANED AUTH USER — generate_link succeeded "
            f"(auth user_id={user_id}) but the usuarios insert failed. This "
            f"auth user now exists in Supabase with no matching usuarios row. "
            f"An admin invite needs manual cleanup in the Supabase dashboard "
            f"(Authentication > Users) before this email can be invited again; "
            f"a superadmin approval stored the id and resumes on retry."
        )
        raise ProvisioningError('insert', user_id)

    return nuevo_usuario, action_link


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

def _run_job(job_id, audio_path, params, usuario_id, clinica_id, nombre):
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

        # llm_processor.validate_against_schema (just above) already
        # guarantees structured_data['metadata'] is a dict.
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


# ─────────────────────────────────────────────
# Clinic logo cache — short TTL, thread-safe (Stage E2 fix #37). A PDF
# is regenerated (and its clinic's logo re-fetched from Storage) on
# every download, but the logo itself only changes when an admin
# updates branding — keyed by the storage path itself, which is stable
# per clinic (logos/{clinica_id}/logo, overwritten in place on upload,
# not a fresh path each time — see submit_clinica_logo), so a TTL is
# genuinely needed for staleness rather than the path naturally
# changing. Only successful fetches are cached — a transient Storage
# failure is retried next time rather than blacking out the logo for
# the whole TTL window. Best-effort invalidation on upload keeps a
# freshly-changed logo from waiting out the TTL. Per-process,
# single-instance assumption — same caveat class as the auth.py/JWKS
# caches, Stage M3's memory:// limiter, and Stage M4's reaper.
# ─────────────────────────────────────────────
_logo_cache_lock = threading.Lock()
_logo_cache: dict = {}  # logo_storage_path -> (expires_at_monotonic, bytes)
_LOGO_CACHE_TTL_SECONDS = 300  # a few minutes — staleness window after a logo update


def fetch_clinica_logo_bytes(logo_storage_path: str) -> bytes | None:
    """
    Fetch clinic logo bytes from Supabase Storage for PDF rendering.
    Returns None on ANY failure (empty path, missing object, network
    error, stale logo_url after the object was deleted) — a logo fetch
    failure must never break PDF generation, only omit the logo.
    """
    if not logo_storage_path:
        return None

    now = time.monotonic()
    with _logo_cache_lock:
        cached = _logo_cache.get(logo_storage_path)
    if cached is not None and now < cached[0]:
        return cached[1]

    result = _storage_download(CLINIC_LOGOS_BUCKET, logo_storage_path)
    if result is None:
        logger.warning(f"PDF: could not fetch clinic logo at '{logo_storage_path}' — rendering without it")
        return None
    image_bytes, _content_type = result
    with _logo_cache_lock:
        _logo_cache[logo_storage_path] = (time.monotonic() + _LOGO_CACHE_TTL_SECONDS, image_bytes)
    return image_bytes


def _invalidate_logo_cache(logo_storage_path: str) -> None:
    """Best-effort cache-clear on logo upload/removal, so the new logo
    doesn't wait out the TTL to appear (Stage E2 fix #37, optional
    nicety — cheap enough to include)."""
    if not logo_storage_path:
        return
    with _logo_cache_lock:
        _logo_cache.pop(logo_storage_path, None)


def get_usuario_cedula(usuario_id: str) -> str | None:
    """
    Fetch a doctor's cédula profesional for the PDF (#55). Two outcomes
    that used to be indistinguishable are now kept apart:
      ''   — the lookup worked and there is no cédula (null/empty column,
             or no usuarios row: an auxiliar, a hand-seeded account, a
             deleted author on a historical note). The PDF renders
             without the "Céd. Prof." line, as it always has.
      None — the lookup itself FAILED (Supabase error/timeout). Callers
             must not render a PDF at all: a signed note silently missing
             a cédula that does exist is worse than "intente de nuevo".
    """
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=cedula&limit=1')
    if rows is None:
        return None
    if rows:
        return rows[0].get('cedula') or ''
    return ''


def get_usuarios_cedulas(usuario_ids: list) -> dict | None:
    """
    Batch cédula lookup for addendum authors (#55) — one query for N ids,
    same in.() discipline as get_usuarios_nombres. Returns {usuario_id:
    cedula} with '' for an id that has no cédula or no row; None if the
    lookup itself failed (same fail-closed contract as get_usuario_cedula).
    """
    ids = list({uid for uid in usuario_ids if uid})
    if not ids:
        return {}
    id_list = ','.join(pg_val(uid) for uid in ids)
    rows = _sb_get(f'/rest/v1/usuarios?id=in.({id_list})&select=id,cedula')
    if rows is None:
        return None
    found = {row['id']: (row.get('cedula') or '') for row in rows}
    return {uid: found.get(uid, '') for uid in ids}


PDF_LOOKUP_FAILED_ERROR = 'No se pudo generar el PDF. Intente de nuevo.'


def _adenda_with_author_cedulas(adenda: list) -> list | None:
    """
    Copy of a session's addenda with each entry's author cédula attached
    under 'author_cedula', for the PDF only — never written back to
    sesiones.addenda. Looked up at render time from author_usuario_id
    (the field admin_add_addendum already stores) rather than snapshotted
    at write time, the same way the note author's cédula is resolved at
    render time — so existing addenda get it too, with no schema change.
    Entries without author_usuario_id (none should exist; the field has
    been written since ADM-1 Stage F) simply get no cédula. Returns None
    if the lookup failed, so the caller fails closed like the note cédula.
    """
    if not adenda:
        return []
    cedulas = get_usuarios_cedulas([e.get('author_usuario_id') for e in adenda])
    if cedulas is None:
        return None
    return [
        {**e, 'author_cedula': cedulas.get(e.get('author_usuario_id'), '')}
        for e in adenda
    ]


def get_usuario_nombre(usuario_id: str) -> str:
    """Fetch a doctor's display name only — no email/cédula/other fields. Empty string on error."""
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=nombre&limit=1')
    if rows:
        return rows[0].get('nombre') or ''
    return ''


def get_usuarios_nombres(usuario_ids: list) -> dict:
    """
    Batch version of get_usuario_nombre — one query for N ids instead of
    N sequential ones (Stage E2 fix #36). Returns {usuario_id: nombre};
    an id with no matching row is simply absent, matching how callers
    already do nombre_cache.get(uid, '') against this kind of map.

    Composite in.() filter — same discipline M1 established: encode
    each VALUE with pg_val, join with a literal structural comma, never
    encode the in.() scaffolding itself.
    """
    ids = list({uid for uid in usuario_ids if uid})
    if not ids:
        return {}
    id_list = ','.join(pg_val(uid) for uid in ids)
    rows = _sb_get(f'/rest/v1/usuarios?id=in.({id_list})&select=id,nombre')
    if not rows:
        return {}
    return {row['id']: (row.get('nombre') or '') for row in rows}

# ────────────────────────────────────────────────────────────────────────────


# ── Security headers / CSP (Stage 3, findings #7-#9) ─────────────────────────
#
# script-src is nonce-based, not 'self'-only: login.html, account.html, and
# set-password.html each carry their own page-specific logic (login,
# forgot-password, password re-auth + change, invite-link session
# detection) as inline <script>, not moved to an external file. That
# is now a narrower, deliberate carve-out than it used to be: the
# shared auth plumbing every template used to duplicate inline
# (createClient + getAuthHeaders/logout/handleSessionExpired) DID move
# out, to static/auth-common.js, in Stage E3 — the "how do you hand
# Jinja-rendered Supabase config to a static file" problem that
# previously ruled this out is solved by a minimal nonce'd inline
# window.CLINIA_CONFIG setter per template, read by auth-common.js.
# What's left inline per-page is genuinely page-specific flow (not
# shared, not duplicated across templates), where forcing it into a
# shared abstraction was judged worse than the marginal security gain
# a per-request nonce still fully provides: it blocks any attacker-
# injected <script> or event-handler attribute exactly as well as
# 'self'-only does; the extra protection 'self'-only buys is against
# an attacker rewriting a *trusted* inline block's own content, which
# isn't how the addenda/patient-data stored-XSS vector (#7) works.
# Nonces do NOT cover inline handler attributes (onclick=...)
# regardless — every one of those was refactored to addEventListener
# as part of #7, so there is no 'unsafe-inline' here.
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

# ── Public access request (#53/#54) ─────────────────────────────────────────
# /solicitud is the one page an outsider can use to ask for an account; a
# superadmin reviews each request (Review C) before anything is created.
# Ships unlinked and noindex until the doctor privacy notice is final.

@app.route('/solicitud')
def solicitud():
    # No supabase-js and no Supabase config on this page: the applicant
    # has no account, and the only call it makes is POST /api/solicitudes.
    return render_template('solicitud.html')


@app.route('/aviso-privacidad-usuarios')
def aviso_privacidad_usuarios():
    # Placeholder until the real "Aviso de Privacidad para usuarios" text
    # is written (later roadmap item); /solicitud links here.
    return render_template('aviso-privacidad-usuarios.html')


SOLICITUD_RECIBIDA = 'Recibimos tu solicitud. Te escribiremos al correo que indicaste.'
SOLICITUD_ERROR = 'No pudimos registrar tu solicitud. Intenta de nuevo en unos minutos.'
SOLICITUD_GLOBAL_LIMIT_KEY = 'solicitudes-global'


def _log_solicitud(outcome: str) -> None:
    """The ONLY log line this flow writes about a request: an opaque
    per-request id and the outcome. Never the email, cédula, nombre,
    teléfono or domicilio — an applicant isn't a user yet and hasn't
    agreed to anything we'd keep in Render's logs."""
    rid = getattr(g, 'solicitud_rid', None)
    if rid is None:
        rid = g.solicitud_rid = secrets.token_hex(6)
    logger.info(f"Solicitud: req={rid} outcome={outcome}")


def _solicitud_recibida():
    return jsonify({'ok': True, 'mensaje': SOLICITUD_RECIBIDA}), 200


@app.route('/api/solicitudes', methods=['POST'])
# No @require_auth: the applicant has no account. Both limits are checked
# before the body is read, so junk, honeypot and invalid submits count
# too. Per IP 5/hour, 20/day; plus a constant-key 50/day across ALL IPs,
# so a botnet rotating addresses still can't flood the superadmin's queue
# and inbox (the cost: a flood can block real applicants until tomorrow).
@limiter.limit("5 per hour;20 per day", key_func=get_remote_address)
@limiter.limit("50 per day", key_func=lambda: SOLICITUD_GLOBAL_LIMIT_KEY)
def create_solicitud():
    """
    Record a request for access. Every accepted-or-silently-dropped case
    answers with the SAME generic success: a filled honeypot, an email
    that already has an account, or one with an open request — so the
    form can't be used to learn who is a Clin.IA user. Only field
    validation (400), non-JSON (415) and a DB failure are visible.
    """
    if not request.is_json:
        _log_solicitud('invalid')
        return jsonify({'error': 'Formato no soportado.'}), 415
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        _log_solicitud('invalid')
        return jsonify({'error': 'Solicitud inválida.'}), 400

    # Honeypot: a visually hidden, aria-hidden, untabbable input no person
    # fills. Checked before validation so a bot learns nothing either way.
    if data.get('sitio_web'):
        _log_solicitud('honeypot')
        return _solicitud_recibida()

    clean, errores = validate_solicitud(data)
    if errores:
        _log_solicitud('invalid')
        first = next(iter(errores.values()))
        return jsonify({'error': first, 'errores': errores}), 400

    email = clean['email']
    existing = _sb_get_quiet(f"/rest/v1/usuarios?email=ilike.{pg_ilike_val(email)}&select=id&limit=1")
    open_req = None
    if existing is not None and not existing:
        open_req = _sb_get_quiet(
            f"/rest/v1/solicitudes_registro?email=ilike.{pg_ilike_val(email)}"
            f"&estado=in.(pendiente,en_proceso)&select=id&limit=1"
        )
    if existing is None or (not existing and open_req is None):
        # Fail closed: without the usuarios check we can't promise the
        # superadmin that an approval won't collide with a live account.
        _log_solicitud('error')
        return jsonify({'error': SOLICITUD_ERROR}), 503
    if existing or open_req:
        _log_solicitud('duplicate')
        return _solicitud_recibida()

    result = _sb_insert_solicitud({
        **clean,
        'terminos_version':      Config.TERMINOS_VERSION,
        'terminos_aceptados_at': datetime.now(timezone.utc).isoformat(),
    })
    if result == 'duplicate':
        _log_solicitud('duplicate')
        return _solicitud_recibida()
    if result != 'created':
        _log_solicitud('error')
        return jsonify({'error': SOLICITUD_ERROR}), 503

    _log_solicitud('created')
    if not Config.SUPERADMIN_EMAIL:
        logger.warning("Solicitud: SUPERADMIN_EMAIL not set — skipping new-request notice.")
    else:
        try:
            send_solicitud_notice(Config.SUPERADMIN_EMAIL, clean['nombre'])
        except Exception as e:
            # send_solicitud_notice already never raises; belt and braces,
            # since the row is saved and the applicant must see success.
            logger.error(f"Solicitud: notice failed ({type(e).__name__})")
    return _solicitud_recibida()


@app.route('/api/health', methods=['GET'])
@limiter.exempt  # Render's own health/readiness polling hits this; must never 429
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'ClinIA Beta',
        'version': '0.8.1-beta',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/process-audio', methods=['POST'])
@require_auth
@require_medico
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

        # Stage E2 fix #34: process_audio used to fetch clinic context/
        # cédula here to build a doctor_info dict passed into _run_job —
        # traced whole-repo, _run_job never referenced it (confirm_and_
        # generate, addendum, and admin-download PDF paths each build
        # their own doctor_info independently at generation time, from
        # g.usuario, which is where it's actually used). Removed as dead
        # weight on the upload hot path.
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
                         usuario_id, clinica_id, nombre)

        logger.info(f"Job {job_id}: submitted to executor")
        return jsonify({'job_id': job_id}), 202

    except Exception as e:
        rid = _new_request_id()
        logger.error(f"process_audio [{rid}]: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': 'Error interno', 'request_id': rid}), 500


@app.route('/api/job-status/<job_id>', methods=['GET'])
@require_auth
@require_medico
def job_status(job_id):
    """
    Poll processing status for a background job. Polled every ~2-8s for
    up to 15 minutes per upload (app.js's startJobPolling), so the query
    shape here matters: while a job isn't terminal, only light fields
    are selected — no structured_data/transcript — since the doctor
    can't act on that payload until the note is actually done anyway
    (Stage E2 fix #33). The heavy payload is fetched once, only on the
    poll that finds status == 'done'.
    """
    rows = _sb_get(
        f'/rest/v1/trabajos?job_id=eq.{pg_val(job_id)}'
        f'&select=job_id,status,error_message,usuario_id,updated_at'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Trabajo no encontrado'}), 404
    row = rows[0]
    if row.get('usuario_id') != g.usuario['usuario_id']:
        # Same status + body as the not-found case above, deliberately —
        # a wrong-owner request against a job that DOES exist must be
        # indistinguishable from one that doesn't, or the response itself
        # leaks the job_id's existence (a route-authorization test-suite
        # finding; every other data-bearing route already uses this same
        # uniform 404 for both "not found" and "out of scope").
        return jsonify({'error': 'Trabajo no encontrado'}), 404

    row = _reap_if_stale(job_id, row)  # Stage M4 fix #26 — self-heals a hung job on poll
    status = row['status']
    resp   = {'job_id': job_id, 'status': status}

    if status == 'done':
        done_rows = _sb_get(
            f'/rest/v1/trabajos?job_id=eq.{pg_val(job_id)}'
            f'&select=session_id,structured_data,transcript'
            f'&limit=1'
        )
        if done_rows:
            resp['session_id']      = done_rows[0]['session_id']
            resp['structured_data'] = done_rows[0]['structured_data']
            resp['transcript']      = done_rows[0]['transcript']
        else:
            # Row vanished between the two queries — vanishingly unlikely
            # once a job is 'done' (nothing deletes a trabajos row at
            # this point in its lifecycle), but fail safe rather than
            # KeyError if it ever happens.
            logger.warning(f"Job {job_id}: status was 'done' but the follow-up payload query returned nothing")

    if status == 'error':
        resp['error_message'] = row.get('error_message', 'Error desconocido')

    return jsonify(resp), 200


@app.route('/api/confirm-and-generate', methods=['POST'])
@require_auth
@require_medico
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

        # #55: resolve the signing doctor's cédula BEFORE anything below
        # writes — the PDF render, save_session's status='confirmed', the
        # transcript/trabajos deletes and the email all come after this.
        # A failed lookup (None, not '') returns here with the note still
        # pending_review and untouched, so the doctor just presses Confirmar
        # again; the frontend already restores the review form on any
        # non-2xx. Only needed when a PDF is requested at all.
        cedula = ''
        if create_pdf:
            cedula = get_usuario_cedula(g.usuario['usuario_id'])
            if cedula is None:
                logger.warning(f"PDF: cédula lookup failed for session {session_id} — note left unconfirmed")
                return jsonify({'error': PDF_LOOKUP_FAILED_ERROR, 'error_code': 'PDF_LOOKUP_FAILED'}), 503

        # PDF generation
        pdf_bytes = None
        if create_pdf:
            logger.info("Orchestrator: PHASE C2 — PDF Generation")
            try:
                clinica  = get_clinica_context(g.usuario['clinica_id'])
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
@require_medico
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
@require_medico
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
@require_medico
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
        # #55: a failed lookup never renders — see get_usuario_cedula.
        cedula = get_usuario_cedula(autor_usuario_id)
        adenda = _adenda_with_author_cedulas(row.get('addenda') or [])
        if cedula is None or adenda is None:
            logger.warning(f"PDF: cédula lookup failed for session {session_id} — download refused")
            return jsonify({'error': PDF_LOOKUP_FAILED_ERROR, 'error_code': 'PDF_LOOKUP_FAILED'}), 503

        clinica      = get_clinica_context(g.usuario['clinica_id'])
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
            adenda=adenda,
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
    # Derived from the same value Flask actually enforces the limit with
    # (app.config['MAX_CONTENT_LENGTH'], itself sourced from
    # Config.MAX_AUDIO_SIZE_BYTES) rather than a second hardcoded literal
    # — this used to say 50 against a real 200MB cap (Stage M5 fix #27).
    max_size_mb = app.config['MAX_CONTENT_LENGTH'] / (1024 * 1024)
    return jsonify({
        'error': 'File too large',
        'max_size_mb': max_size_mb
    }), 413


@app.route('/api/patient-history', methods=['GET'])
@require_auth
@require_medico
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

    # Resolve authoring doctor names only when needed — one batch query
    # for every distinct author instead of one query each (Stage E2 #36)
    nombre_cache = {}
    if scope == 'clinica':
        other_authors = {
            r.get('usuario_id') for r in rows
            if r.get('usuario_id') and r.get('usuario_id') != usuario_id
        }
        nombre_cache = get_usuarios_nombres(list(other_authors))

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
@require_medico
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

    return jsonify(_session_detail_response(row, usuario_id)), 200


def _session_detail_response(row: dict, reader_usuario_id: str) -> dict:
    """
    Shared body of the two session-detail reads — patient_history_detail
    (doctors) and admin_session_detail (admin panel) — once the caller's
    route has already scope-checked the row. Logs the read (fire-and-
    forget, never fails the request) under the READER's usuario_id, and
    names the author when the reader isn't them.
    """
    session_id = row['session_id']
    try:
        _sb_log_lectura(session_id, reader_usuario_id)
    except Exception as e:
        logger.warning(f"DB: Could not log lectura for session {session_id}: {e}")

    response = {
        'session_id':      session_id,
        'timestamp':       row.get('timestamp'),
        'status':          row.get('status'),
        'structured_data': row['structured_data'],
        'addenda':         row.get('addenda') or [],
    }
    if row.get('usuario_id') != reader_usuario_id:
        response['autor_nombre'] = get_usuario_nombre(row.get('usuario_id'))
    return response


@app.route('/api/pending-sessions', methods=['GET'])
@require_auth
@require_medico
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
@require_medico
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
@require_medico
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
    body = {
        'valid':      True,
        'rol':        g.usuario['rol'],
        'clinica_id': g.usuario['clinica_id'],
    }
    # Key present only for the superadmin, absent for everyone else, so
    # the response doesn't even hint that the role exists.
    if is_superadmin(g.usuario):
        body['superadmin'] = True
        # Drives the "solicitudes pendientes" banner on / and /admin.
        # en_proceso counts too: an approval that stopped half-way still
        # needs the superadmin. A failed count omits the key (no banner)
        # rather than failing the session check every page load depends on.
        pendientes = _sb_count_quiet(
            '/rest/v1/solicitudes_registro?estado=in.(pendiente,en_proceso)&select=id'
        )
        if pendientes is not None:
            body['solicitudes_pendientes'] = pendientes
    return jsonify(body), 200


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
    Invite a new user (médico, médico administrador or auxiliar) into the
    admin's own clinic. Creates
    the Supabase auth identity via generate_link (invite type), inserts
    the usuarios row (both via _provision_usuario), and emails the invite
    link via Resend.

    rol is 'medico' (the default, so older clients that send no rol keep
    working), 'admin' (médico administrador — a doctor who also runs the
    clinic) or 'auxiliar' — anything else is a 400. clinica_id is ALWAYS
    taken from g.usuario, never the request body, so a tampered request
    cannot create a cross-clinic user.

    médico / admin: cédula and especialidad required, universidad optional.
    auxiliar: none of the three may be sent — an auxiliar is not a doctor
    and must never end up with a cédula a PDF could print.
    """
    MAX_UNIVERSIDAD_LENGTH = 160

    data = request.get_json(silent=True) or {}
    nombre       = (data.get('nombre') or '').strip()
    email        = (data.get('email') or '').strip().lower()
    rol          = data.get('rol') or ROL_MEDICO
    rol          = rol.strip().lower() if isinstance(rol, str) else None
    especialidad = (data.get('especialidad') or '').strip()
    cedula       = (data.get('cedula') or '').strip()
    universidad  = (data.get('universidad') or '').strip()

    if rol not in (ROL_ADMIN, ROL_MEDICO, ROL_AUXILIAR):
        return jsonify({'error': 'Rol inválido'}), 400
    if not nombre or not email:
        return jsonify({'error': 'Todos los campos son obligatorios'}), 400
    if is_medico_role(rol) and (not especialidad or not cedula):
        return jsonify({'error': 'Todos los campos son obligatorios'}), 400
    if rol == ROL_AUXILIAR and (especialidad or cedula or universidad):
        return jsonify({'error': 'Un auxiliar no lleva cédula, especialidad ni universidad'}), 400
    if len(universidad) > MAX_UNIVERSIDAD_LENGTH:
        return jsonify({'error': f'La universidad no puede exceder {MAX_UNIVERSIDAD_LENGTH} caracteres'}), 400
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

    # Steps (a)+(c): auth identity + invite link, then the usuarios row.
    # Orphan logging for an insert failure lives in _provision_usuario.
    try:
        if is_medico_role(rol):
            nuevo_usuario, action_link = _provision_usuario(
                nombre, email, rol, clinica_id,
                cedula=cedula, especialidad=especialidad,
                universidad=universidad or None,
            )
        else:
            nuevo_usuario, action_link = _provision_usuario(nombre, email, rol, clinica_id)
    except ProvisioningError as e:
        if e.stage == 'invite':
            return jsonify({'error': 'No se pudo crear la invitación. Intente de nuevo.'}), 500
        return jsonify({
            'error': (
                'La invitación se creó en el sistema de autenticación pero no '
                'se pudo guardar el registro del usuario. Un usuario huérfano '
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
        'especialidad': nuevo_usuario.get('especialidad'),
        'cedula':       nuevo_usuario.get('cedula'),
        'universidad':  nuevo_usuario.get('universidad'),
        'rol':          nuevo_usuario['rol'],
        'activo':       nuevo_usuario['activo'],
    }
    if not email_sent:
        logger.warning(f"Admin: doctor {email} created but invite email failed to send.")
        response['warning'] = 'Usuario creado pero el correo de invitación no pudo enviarse.'

    return jsonify(response), 200


LAST_ADMIN_ERROR = 'La clínica debe tener al menos un administrador activo.'


def _would_leave_clinic_without_admin(clinica_id: str, usuario_id: str) -> bool | None:
    """
    Last-admin guard: True if deactivating usuario_id would leave the
    clinic with no active admin or auxiliar — nobody left to invite,
    reactivate or answer ARCO requests for it. None if the lookup failed
    (callers fail closed). activo is filtered here in Python rather than
    in the query, so the rule reads plainly: "is anyone ELSE in this
    clinic an active admin-role user?"

    Through admin_set_usuario_activo today this can only trip in a stale
    or racing state: the caller is itself an active admin of the clinic
    and can't target itself. It becomes the real boundary once a
    superadmin can deactivate users from outside the clinic (#53). Code
    check only, no DB trigger — two admins deactivating each other at
    the same instant can both pass it; accepted for now.
    """
    admin_role_list = ','.join(sorted(ADMIN_ROLES))
    admins = _sb_get(
        f'/rest/v1/usuarios?clinica_id=eq.{pg_val(clinica_id)}'
        f'&rol=in.({admin_role_list})&select=id,activo'
    )
    if admins is None:
        return None
    return not any(a.get('activo') is True and a.get('id') != usuario_id for a in admins)


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
        f'&select=id,clinica_id,rol&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    if rows[0].get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Usuario no encontrado'}), 404

    if not nuevo_activo and is_admin_role(rows[0].get('rol')):
        sin_admin = _would_leave_clinic_without_admin(g.usuario['clinica_id'], usuario_id)
        if sin_admin is None:
            return jsonify({'error': 'No se pudo verificar los administradores de la clínica. Intente de nuevo.'}), 500
        if sin_admin:
            return jsonify({'error': LAST_ADMIN_ERROR}), 409

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

    # Evict the cached usuario_context entry so this change takes effect
    # on the doctor's very next request instead of waiting out the #32
    # TTL — restores the exact immediacy property the uncached version
    # had, for this one security-relevant action (same invalidate-on-
    # change pattern as #37's logo cache).
    invalidate_usuario_cache(usuario_id)

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

    _invalidate_logo_cache(storage_path)  # Stage E2 fix #37 — new logo visible immediately, not after the TTL
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
    _invalidate_logo_cache(storage_path)  # Stage E2 fix #37

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

    # Resolve doctor names via a single batch query for every distinct
    # usuario_id on this page (not a PostgREST embed — same reasoning as
    # item 24 Stage 4: embeds couldn't be verified reliably). Was one
    # sequential query per distinct id (up to ~20 round trips on a full
    # admin page); now one round trip total (Stage E2 fix #36).
    distinct_uids = {r.get('usuario_id') for r in rows if r.get('usuario_id')}
    nombre_cache = get_usuarios_nombres(list(distinct_uids))

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


@app.route('/api/admin/session/<session_id>', methods=['GET'])
@require_auth
@require_admin
def admin_session_detail(session_id):
    """
    Read-only detail of any session in the admin's own clinic, for the
    admin panel's session viewer (ARCO search → open a note). Same body
    and same lecturas_sesion logging as GET /api/patient-history/<id>,
    which the admin panel used to call directly — that route is now
    doctor-only (@require_medico), and an auxiliar admin, who is not a
    doctor, still needs to open notes here to answer ARCO requests.
    """
    rows = _sb_get(
        f'/rest/v1/sesiones'
        f'?session_id=eq.{pg_val(session_id)}'
        f'&select=session_id,usuario_id,clinica_id,timestamp,status,structured_data,addenda'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    return jsonify(_session_detail_response(row, g.usuario['usuario_id'])), 200


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
        # #55: a failed lookup never renders — see get_usuario_cedula.
        cedula = get_usuario_cedula(autor_usuario_id)
        adenda = _adenda_with_author_cedulas(row.get('addenda') or [])
        if cedula is None or adenda is None:
            logger.warning(f"PDF: cédula lookup failed for session {session_id} — admin download refused")
            return jsonify({'error': PDF_LOOKUP_FAILED_ERROR, 'error_code': 'PDF_LOOKUP_FAILED'}), 503

        clinica      = get_clinica_context(row.get('clinica_id'))
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
            adenda=adenda,
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


# ── Superadmin review of access requests (#53, Review C) ────────────────────
# One person (Config.SUPERADMIN_USER_IDS) reviews solicitudes_registro:
# approve (creates the clinic if needed, the account, the invite), reject,
# resend an unaccepted invite, or delete an approval nobody accepted.
# Every route is @require_superadmin, which answers everyone else —
# including no token — with the same 404. Applicant data never reaches a
# log line: only the solicitud id, the step and a status.

SOLICITUD_SELECT = (
    'id,nombre,email,rol_solicitado,cedula,especialidad,universidad,razon_social,'
    'domicilio,telefono,recomendado_por,terminos_version,terminos_aceptados_at,'
    'estado,created_at,revisado_at,revisado_por,clinica_id,clinica_creada,'
    'usuario_id,rol_otorgado,motivo_rechazo'
)
SUPERADMIN_LISTAS = ('pendiente', 'aprobada', 'rechazada')
RECHAZADA_RETENTION_DAYS = 30
MAX_MOTIVO_RECHAZO = 1000
SUPERADMIN_DB_ERROR = 'No se pudo consultar la base de datos. Intenta de nuevo.'
APROBACION_PASO_ERROR = ('No se pudo completar la aprobación (paso: {paso}). '
                         'Usa "Reintentar aprobación"; lo ya hecho no se repite.')
APROBACION_OTROS_DATOS = ('Esta aprobación quedó a medias con otra clínica u otro rol. '
                          'Reintenta con los mismos datos.')
INVITE_FALLO_AVISO = 'Aprobada, pero el correo de invitación no se envió — usa Reenviar.'
_INVISIBLE_CHARS_RE = re.compile('[​⁠﻿]')


def _sa_log(solicitud_id, accion: str, outcome: str) -> None:
    """The superadmin flows' only log line: id, action, outcome."""
    logger.info(f"Superadmin: solicitud={solicitud_id} accion={accion} outcome={outcome}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dias_desde(iso_value) -> int | None:
    """Whole days from an ISO timestamp to now (UTC), or None."""
    if not iso_value:
        return None
    try:
        then = datetime.fromisoformat(str(iso_value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, (_utc_now() - then).days)


def _purge_old_rechazadas() -> bool:
    """
    Delete rechazadas reviewed more than RECHAZADA_RETENTION_DAYS ago (the
    applicant's data is kept no longer than that). Filtered on estado, so
    pendiente/en_proceso/aprobada rows can never match. Runs on every list
    load and once at startup; best-effort.
    """
    cutoff = (_utc_now() - timedelta(days=RECHAZADA_RETENTION_DAYS)).isoformat()
    ok = _sb_delete_quiet(
        f'/rest/v1/solicitudes_registro?estado=eq.rechazada&revisado_at=lt.{pg_val(cutoff)}'
    )
    if not ok:
        logger.warning("Superadmin: purge of old rechazadas failed")
    return ok


def _load_solicitud(solicitud_id: str) -> dict | None | bool:
    """The row, False if it doesn't exist, None if the lookup failed."""
    rows = _sb_get_quiet(
        f'/rest/v1/solicitudes_registro?id=eq.{pg_val(solicitud_id)}&select={SOLICITUD_SELECT}&limit=1'
    )
    if rows is None:
        return None
    return rows[0] if rows else False


def _patch_solicitud(solicitud_id: str, body: dict, estado: str = 'en_proceso') -> bool:
    """Update one solicitud only while it is still in `estado`. True iff a
    row was updated — a False after a successful call means the row moved
    on (another tab finished or undid it)."""
    rows = _sb_patch_returning_quiet(
        f'/rest/v1/solicitudes_registro?id=eq.{pg_val(solicitud_id)}&estado=eq.{pg_val(estado)}',
        body,
    )
    return bool(rows)


def _invitacion_aceptada(usuario_id: str) -> tuple[str, bool | None]:
    """
    ('ok', accepted) / ('missing', None) / ('error', None). Accepted =
    the auth user's email_confirmed_at is set. That is the field Supabase
    sets when an invite (or recovery) link is used — signupVerify /
    recoverVerify confirm the user — so "not confirmed" reliably means
    the link was never opened. It does NOT prove a password was saved:
    someone who opened the link and closed the page counts as accepted.
    last_sign_in_at isn't used: I could not confirm from the source that
    verifying a link always sets it.
    """
    status, user = _sb_get_auth_user(usuario_id)
    if status != 'ok':
        return status, None
    return 'ok', bool(user.get('email_confirmed_at') or user.get('confirmed_at'))


def _superadmin_row(row: dict) -> dict:
    out = {k: row.get(k) for k in SOLICITUD_SELECT.split(',')}
    out['rol_solicitado_label'] = ROL_LABELS.get(row.get('rol_solicitado'), row.get('rol_solicitado'))
    out['rol_otorgado_label'] = ROL_LABELS.get(row.get('rol_otorgado')) if row.get('rol_otorgado') else None
    return out


@app.route('/superadmin')
def superadmin():
    # Unconditional shell, same pattern as admin(): superadmin.js asks
    # /api/session-check and sends anyone without "superadmin": true back
    # to /. The real boundary is @require_superadmin on the API below.
    return render_template(
        'superadmin.html',
        supabase_url=Config.SUPABASE_URL,
        supabase_anon_key=Config.SUPABASE_ANON_KEY
    )


@app.route('/api/superadmin/solicitudes', methods=['GET'])
@require_superadmin
def superadmin_solicitudes():
    """
    ?estado=pendiente (default; oldest first, en_proceso rows included and
    flagged atascada so the page offers "Reintentar aprobación"),
    aprobada (newest first, with invite status from Supabase Auth) or
    rechazada (newest first, with motivo). Purges old rechazadas first.
    Also returns REJECTION_EMAIL_DRAFT so the page previews exactly the
    email the server would send.
    """
    estado = (request.args.get('estado') or 'pendiente').strip().lower()
    if estado not in SUPERADMIN_LISTAS:
        return jsonify({'error': 'Lista inválida.'}), 400

    _purge_old_rechazadas()

    filtro = 'estado=in.(pendiente,en_proceso)' if estado == 'pendiente' else f'estado=eq.{estado}'
    rows = _sb_get_quiet(f'/rest/v1/solicitudes_registro?{filtro}&select={SOLICITUD_SELECT}')
    if rows is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503

    if estado == 'pendiente':
        # Sorted here rather than trusted to order=: ISO strings sort by time.
        rows.sort(key=lambda r: r.get('created_at') or '')
    else:
        rows.sort(key=lambda r: r.get('revisado_at') or '', reverse=True)

    clinicas = {}
    usuarios_por_clinica = {}
    if estado == 'aprobada':
        ids = sorted({r['clinica_id'] for r in rows if r.get('clinica_id')})
        if ids:
            id_list = ','.join(pg_val(i) for i in ids)
            crows = _sb_get_quiet(f'/rest/v1/clinicas?id=in.({id_list})&select=id,nombre')
            urows = _sb_get_quiet(f'/rest/v1/usuarios?clinica_id=in.({id_list})&select=id,clinica_id')
            if crows is None or urows is None:
                return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
            clinicas = {c['id']: c.get('nombre') for c in crows}
            for u in urows:
                usuarios_por_clinica.setdefault(u['clinica_id'], set()).add(u['id'])

    out = []
    for row in rows:
        item = _superadmin_row(row)
        if estado == 'pendiente':
            item['dias'] = _dias_desde(row.get('created_at'))
            item['atascada'] = row.get('estado') == 'en_proceso'
        elif estado == 'aprobada':
            item['dias_desde_aprobacion'] = _dias_desde(row.get('revisado_at'))
            item['clinica_nombre'] = clinicas.get(row.get('clinica_id'))
            aceptada = None
            if row.get('usuario_id'):
                status, aceptada = _invitacion_aceptada(row['usuario_id'])
                if status == 'missing':
                    aceptada = False  # account already gone (a half-finished delete)
            item['invitacion_aceptada'] = aceptada  # None = Auth lookup failed
            otros = usuarios_por_clinica.get(row.get('clinica_id'), set()) - {row.get('usuario_id')}
            # Shown in the delete confirmation: the clinic goes too only if
            # this approval created it and nobody else is in it yet.
            item['clinica_se_elimina'] = bool(row.get('clinica_creada')) and not otros
        else:
            item['dias'] = _dias_desde(row.get('revisado_at'))
        out.append(item)

    return jsonify({
        'estado':             estado,
        'solicitudes':        out,
        'plantilla_rechazo':  REJECTION_EMAIL_DRAFT,
        'max_motivo':         MAX_MOTIVO_RECHAZO,
    }), 200


@app.route('/api/superadmin/clinicas', methods=['GET'])
@require_superadmin
def superadmin_clinicas():
    """Every clinic (id, nombre, direccion) for the approval picker, plus
    "sugerida": the best normalized match for ?q (the request's razón
    social), or null. A suggestion only — nothing is preselected server-side."""
    rows = _sb_get_quiet('/rest/v1/clinicas?select=id,nombre,direccion&order=nombre')
    if rows is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    rows.sort(key=lambda c: (c.get('nombre') or '').lower())
    return jsonify({
        'clinicas': [{'id': c['id'], 'nombre': c.get('nombre'), 'direccion': c.get('direccion')} for c in rows],
        'sugerida': suggest_clinica(request.args.get('q') or '', rows),
    }), 200


def _aprobacion_fallo(solicitud_id, paso: str):
    _sa_log(solicitud_id, 'aprobar', f'failed-at-{paso.replace(" ", "-")}')
    return jsonify({'error': APROBACION_PASO_ERROR.format(paso=paso), 'paso': paso}), 503


@app.route('/api/superadmin/solicitudes/<solicitud_id>/aprobar', methods=['POST'])
@require_superadmin
def superadmin_aprobar(solicitud_id):
    """
    Approve a request. Resumable and idempotent: each step records its
    result on the solicitud before the next starts, and a retry skips
    whatever is already recorded.
      (1) claim pendiente → en_proceso (conditional PATCH), store rol_otorgado
      (2) clinic: create one from razón social/domicilio/teléfono (new) and
          store clinica_id + clinica_creada, or store the chosen clinica_id
      (3) auth user + usuarios row; usuario_id stored the moment the auth
          user exists, so a retry inserts only the missing usuarios row
      (4) estado aprobada, revisado_at, revisado_por
      (5) invite email (a fresh recovery link if this request didn't
          create the user — the original action_link is gone)
      (6) existing clinic: tell its other active admins/auxiliares
    Steps 5–6 never undo the approval; their failures are reported.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Solicitud inválida.'}), 400
    clinica_id = data.get('clinica_id')
    if clinica_id is not None and (not isinstance(clinica_id, str) or not clinica_id.strip()):
        return jsonify({'error': 'Clínica inválida.'}), 400
    clinica_id = clinica_id.strip() if clinica_id else None
    rol = data.get('rol_otorgado')
    if rol not in (ROL_ADMIN, ROL_MEDICO, ROL_AUXILIAR):
        return jsonify({'error': 'Elige el rol que se otorga.'}), 400
    confirma_admin = data.get('confirma_admin') is True

    row = _load_solicitud(solicitud_id)
    if row is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    if row is False:
        return jsonify({'error': 'No encontrado'}), 404
    estado = row.get('estado')
    if estado not in ('pendiente', 'en_proceso'):
        return jsonify({'error': 'Esta solicitud ya fue revisada.'}), 409

    # A resumed approval must finish the way it started.
    if estado == 'en_proceso':
        if row.get('rol_otorgado') and row['rol_otorgado'] != rol:
            return jsonify({'error': APROBACION_OTROS_DATOS}), 409
        if row.get('clinica_id'):
            if row.get('clinica_creada') and clinica_id is not None:
                return jsonify({'error': APROBACION_OTROS_DATOS}), 409
            if not row.get('clinica_creada') and clinica_id != row['clinica_id']:
                return jsonify({'error': APROBACION_OTROS_DATOS}), 409
    nueva = clinica_id is None

    if nueva and not is_admin_role(rol):
        return jsonify({'error': 'Una clínica nueva necesita un administrador: otorga '
                                 'Médico administrador o Auxiliar administrativo.'}), 400
    if not nueva and is_admin_role(rol) and not confirma_admin:
        return jsonify({'error': 'Confirma que ya lo hablaste con el administrador actual '
                                 'de la clínica antes de agregar otro administrador.'}), 400
    if is_medico_role(rol) and not (row.get('cedula') and row.get('especialidad')):
        return jsonify({'error': 'La solicitud no trae cédula y especialidad; '
                                 'no se puede otorgar un rol de médico.'}), 400
    if rol == ROL_AUXILIAR and row.get('rol_solicitado') != ROL_AUXILIAR:
        return jsonify({'error': 'Solo se otorga Auxiliar administrativo a quien lo solicitó.'}), 400

    clinica_nombre = row.get('razon_social')
    if not nueva:
        crows = _sb_get_quiet(f'/rest/v1/clinicas?id=eq.{pg_val(clinica_id)}&select=id,nombre&limit=1')
        if crows is None:
            return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
        if not crows:
            return jsonify({'error': 'La clínica elegida no existe.'}), 400
        clinica_nombre = crows[0].get('nombre')

    # Re-checked now, not trusted from submit time: an account may have
    # been created for this email since. A usuarios row that is THIS
    # approval's own (resumed after step 3) is not a duplicate.
    email = row['email']
    existing = _sb_get_quiet(f"/rest/v1/usuarios?email=ilike.{pg_ilike_val(email)}&select=id")
    if existing is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    if any(u.get('id') != row.get('usuario_id') for u in existing):
        _sa_log(solicitud_id, 'aprobar', 'duplicate-email')
        return jsonify({'error': 'Ya existe un usuario con este correo. La solicitud no se aprobó '
                                 'y sigue pendiente.'}), 409

    # (1) claim
    if estado == 'pendiente':
        claimed = _sb_patch_returning_quiet(
            f'/rest/v1/solicitudes_registro?id=eq.{pg_val(solicitud_id)}&estado=eq.pendiente',
            {'estado': 'en_proceso', 'rol_otorgado': rol},
        )
        if claimed is None:
            return _aprobacion_fallo(solicitud_id, 'reservar la solicitud')
        if not claimed:
            return jsonify({'error': 'Otra aprobación de esta solicitud está en curso o ya terminó.'}), 409
    elif not row.get('rol_otorgado'):
        if not _patch_solicitud(solicitud_id, {'rol_otorgado': rol}):
            return _aprobacion_fallo(solicitud_id, 'reservar la solicitud')

    # (2) clinic
    if nueva:
        if row.get('clinica_id'):
            clinica_id = row['clinica_id']
        else:
            clinica = _sb_insert_clinica(row['razon_social'], row['domicilio'], row['telefono'])
            if clinica is None:
                return _aprobacion_fallo(solicitud_id, 'crear la clínica')
            if not _patch_solicitud(solicitud_id, {'clinica_id': clinica['id'], 'clinica_creada': True}):
                # Unrecorded, a retry would create a second clinic: undo it.
                if not _sb_delete_quiet(f"/rest/v1/clinicas?id=eq.{pg_val(clinica['id'])}"):
                    logger.error(f"Superadmin: clinica={clinica['id']} created for solicitud={solicitud_id} "
                                 f"but not recorded, and could not be removed — delete it by hand")
                return _aprobacion_fallo(solicitud_id, 'registrar la clínica')
            clinica_id = clinica['id']
    elif row.get('clinica_id') != clinica_id:
        if not _patch_solicitud(solicitud_id, {'clinica_id': clinica_id, 'clinica_creada': False}):
            return _aprobacion_fallo(solicitud_id, 'registrar la clínica')

    # (3) account
    medico_fields = {}
    if is_medico_role(rol):
        medico_fields = {'cedula': row.get('cedula'), 'especialidad': row.get('especialidad'),
                         'universidad': row.get('universidad') or None}
    action_link = None
    usuario_id = row.get('usuario_id')
    if usuario_id:
        have = _sb_get_quiet(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=id&limit=1')
        if have is None:
            return _aprobacion_fallo(solicitud_id, 'guardar el usuario')
        if not have and _insert_usuario_row(usuario_id, row['nombre'], email, rol, clinica_id,
                                            **medico_fields) is None:
            return _aprobacion_fallo(solicitud_id, 'guardar el usuario')
    else:
        recorded = {}

        def _record(uid):
            recorded['id'] = uid
            return _patch_solicitud(solicitud_id, {'usuario_id': uid})
        try:
            _, action_link = _provision_usuario(row['nombre'], email, rol, clinica_id,
                                                on_auth_user=_record, **medico_fields)
        except ProvisioningError as e:
            paso = {'invite': 'crear la cuenta', 'record': 'registrar la cuenta'}.get(e.stage, 'guardar el usuario')
            return _aprobacion_fallo(solicitud_id, paso)
        usuario_id = recorded['id']
    invalidate_usuario_cache(usuario_id)

    # (4) approved
    if not _patch_solicitud(solicitud_id, {
        'estado':       'aprobada',
        'revisado_at':  _utc_now().isoformat(),
        'revisado_por': g.usuario['usuario_id'],
    }):
        return _aprobacion_fallo(solicitud_id, 'marcar como aprobada')

    # (5) invite
    if action_link is None:
        try:
            action_link = _sb_generate_recovery_link(email)
        except Exception as e:
            logger.warning(f"Superadmin: solicitud={solicitud_id} fresh link failed ({type(e).__name__})")
    invite_ok = bool(action_link) and send_invite_email(email, row['nombre'], clinica_nombre, action_link)

    # (6) the clinic's other admins
    avisos = {'enviados': 0, 'fallidos': 0}
    if not nueva:
        admins = _sb_get_quiet(
            f'/rest/v1/usuarios?clinica_id=eq.{pg_val(clinica_id)}&rol=in.(admin,auxiliar)'
            f'&activo=eq.true&select=id,email'
        )
        if admins is None:
            avisos['fallidos'] = None  # couldn't even list them
        else:
            for admin_row in admins:
                if admin_row.get('id') == usuario_id or not admin_row.get('email'):
                    continue
                if send_usuario_agregado_email(admin_row['email'], row['nombre'], clinica_nombre, ROL_LABELS[rol]):
                    avisos['enviados'] += 1
                else:
                    avisos['fallidos'] += 1

    _sa_log(solicitud_id, 'aprobar', 'approved' if invite_ok else 'approved-invite-failed')
    response = {
        'ok':                         True,
        'estado':                     'aprobada',
        'usuario_id':                 usuario_id,
        'clinica_id':                 clinica_id,
        'clinica_creada':             nueva,
        'correo_invitacion_enviado':  invite_ok,
        'avisos_admins':              None if nueva else avisos,
    }
    if not invite_ok:
        response['warning'] = INVITE_FALLO_AVISO
    return jsonify(response), 200


def _clean_motivo(value):
    """Plain text: invisible chars dropped, CRLF → LF, outer whitespace
    stripped, line breaks kept. Returns (motivo or None, error or None)."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, 'Motivo inválido.'
    value = _INVISIBLE_CHARS_RE.sub('', value).replace('\r\n', '\n').replace('\r', '\n').strip()
    if len(value) > MAX_MOTIVO_RECHAZO:
        return None, f'El motivo no puede exceder {MAX_MOTIVO_RECHAZO} caracteres.'
    return value or None, None


@app.route('/api/superadmin/solicitudes/<solicitud_id>/rechazar', methods=['POST'])
@require_superadmin
def superadmin_rechazar(solicitud_id):
    """Reject a pendiente request; optionally email the applicant
    (REJECTION_EMAIL_DRAFT, Reply-To SUPERADMIN_EMAIL)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Solicitud inválida.'}), 400
    motivo, error = _clean_motivo(data.get('motivo'))
    if error:
        return jsonify({'error': error}), 400
    enviar = data.get('enviar_correo') is True

    row = _load_solicitud(solicitud_id)
    if row is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    if row is False:
        return jsonify({'error': 'No encontrado'}), 404
    if row.get('estado') != 'pendiente':
        return jsonify({'error': 'Solo se puede rechazar una solicitud pendiente.'}), 409

    updated = _sb_patch_returning_quiet(
        f'/rest/v1/solicitudes_registro?id=eq.{pg_val(solicitud_id)}&estado=eq.pendiente',
        {
            'estado':         'rechazada',
            'motivo_rechazo': motivo,
            'revisado_at':    _utc_now().isoformat(),
            'revisado_por':   g.usuario['usuario_id'],
        },
    )
    if updated is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    if not updated:
        return jsonify({'error': 'Solo se puede rechazar una solicitud pendiente.'}), 409

    correo = None
    if enviar:
        correo = send_rejection_email(row['email'], row['nombre'], motivo, Config.SUPERADMIN_EMAIL or None)
    _sa_log(solicitud_id, 'rechazar', 'rejected' + ('' if not enviar else '-emailed' if correo else '-email-failed'))
    return jsonify({'ok': True, 'estado': 'rechazada', 'correo_enviado': correo}), 200


def _aprobada_sin_aceptar(solicitud_id):
    """Shared gate for reenviar/eliminar. Returns (row, accepted_status,
    None) or (None, None, error_response)."""
    row = _load_solicitud(solicitud_id)
    if row is None:
        return None, None, (jsonify({'error': SUPERADMIN_DB_ERROR}), 503)
    if row is False:
        return None, None, (jsonify({'error': 'No encontrado'}), 404)
    if row.get('estado') != 'aprobada' or not row.get('usuario_id'):
        return None, None, (jsonify({'error': 'Solo aplica a solicitudes aprobadas cuya invitación '
                                              'no se ha aceptado.'}), 409)
    status, aceptada = _invitacion_aceptada(row['usuario_id'])
    if status == 'error':
        return None, None, (jsonify({'error': 'No se pudo consultar la cuenta. Intenta de nuevo.'}), 503)
    if aceptada:
        return None, None, (jsonify({'error': 'El usuario ya aceptó la invitación.'}), 409)
    return row, status, None


@app.route('/api/superadmin/solicitudes/<solicitud_id>/reenviar', methods=['POST'])
@require_superadmin
def superadmin_reenviar(solicitud_id):
    """New set-password link (type recovery — see _sb_generate_recovery_link)
    for an approved user who never opened the first one."""
    row, status, error = _aprobada_sin_aceptar(solicitud_id)
    if error:
        return error
    if status == 'missing':
        return jsonify({'error': 'La cuenta de esta solicitud ya no existe.'}), 409

    try:
        link = _sb_generate_recovery_link(row['email'])
    except Exception as e:
        logger.warning(f"Superadmin: solicitud={solicitud_id} resend link failed ({type(e).__name__})")
        return jsonify({'error': 'No se pudo generar el enlace. Intenta de nuevo.'}), 503

    clinica_nombre = row.get('razon_social')
    if row.get('clinica_id'):
        crows = _sb_get_quiet(f"/rest/v1/clinicas?id=eq.{pg_val(row['clinica_id'])}&select=nombre&limit=1")
        if crows:
            clinica_nombre = crows[0].get('nombre') or clinica_nombre
    if not send_invite_email(row['email'], row['nombre'], clinica_nombre, link):
        _sa_log(solicitud_id, 'reenviar', 'email-failed')
        return jsonify({'error': 'No se pudo enviar el correo. Intenta de nuevo.'}), 503
    _sa_log(solicitud_id, 'reenviar', 'sent')
    return jsonify({'ok': True, 'correo_enviado': True}), 200


@app.route('/api/superadmin/solicitudes/<solicitud_id>/eliminar', methods=['POST'])
@require_superadmin
def superadmin_eliminar(solicitud_id):
    """
    Undo an approval nobody accepted: auth user (its usuarios row goes by
    cascade) → the clinic, only if this approval created it and it is
    still empty → the solicitud row. Each step is skipped on a retry once
    done (a missing auth user is "already deleted"), so an interrupted
    delete just runs again. Refused while the user has sesiones, trabajos
    or lecturas_sesion rows — the DB would refuse anyway (no cascade).
    """
    row, status, error = _aprobada_sin_aceptar(solicitud_id)
    if error:
        return error
    usuario_id = row['usuario_id']

    for table in ('sesiones', 'trabajos', 'lecturas_sesion'):
        hits = _sb_get_quiet(f'/rest/v1/{table}?usuario_id=eq.{pg_val(usuario_id)}&select=usuario_id&limit=1')
        if hits is None:
            return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
        if hits:
            _sa_log(solicitud_id, 'eliminar', f'refused-{table}')
            return jsonify({'error': 'Este usuario ya tiene actividad registrada (notas, trabajos o '
                                     'lecturas); no se puede eliminar.'}), 409

    if status == 'ok':
        if _sb_delete_auth_user(usuario_id) == 'error':
            return jsonify({'error': 'No se pudo eliminar la cuenta. Intenta de nuevo.'}), 503
    invalidate_usuario_cache(usuario_id)
    left = _sb_get_quiet(f'/rest/v1/usuarios?id=eq.{pg_val(usuario_id)}&select=id&limit=1')
    if left is None:
        return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
    if left:
        # The cascade should have taken it; never delete a clinic under it.
        logger.error(f"Superadmin: solicitud={solicitud_id} auth user gone but usuarios row remains")
        return jsonify({'error': 'La cuenta se eliminó, pero su registro de usuario sigue. '
                                 'Revisa en Supabase antes de reintentar.'}), 503

    clinica_eliminada = False
    if row.get('clinica_creada') and row.get('clinica_id'):
        cid = pg_val(row['clinica_id'])
        users = _sb_get_quiet(f'/rest/v1/usuarios?clinica_id=eq.{cid}&select=id&limit=1')
        sesiones = _sb_get_quiet(f'/rest/v1/sesiones?clinica_id=eq.{cid}&select=session_id&limit=1')
        if users is None or sesiones is None:
            return jsonify({'error': SUPERADMIN_DB_ERROR}), 503
        if not users and not sesiones:
            if not _sb_delete_quiet(f'/rest/v1/clinicas?id=eq.{cid}'):
                return jsonify({'error': 'No se pudo eliminar la clínica. Intenta de nuevo.'}), 503
            clinica_eliminada = True

    if not _sb_delete_quiet(f'/rest/v1/solicitudes_registro?id=eq.{pg_val(solicitud_id)}&estado=eq.aprobada'):
        return jsonify({'error': 'No se pudo eliminar la solicitud. Intenta de nuevo.'}), 503
    _sa_log(solicitud_id, 'eliminar', 'deleted' + ('-with-clinic' if clinica_eliminada else ''))
    return jsonify({'ok': True, 'clinica_eliminada': clinica_eliminada}), 200


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
# Old rechazadas are also purged on every superadmin list load; this
# covers a stretch with no reviews. Best-effort, never blocks startup.
try:
    _purge_old_rechazadas()
except Exception as e:
    logger.error(f"Startup: rechazadas purge failed ({type(e).__name__})")


if __name__ == '__main__':
    # This block only runs when you run 'python app.py' on your computer.
    # It does NOT run on Render.
    logger.info("ClinIA Beta - Medical Note Taker (Local Mode) — starting Flask server")

    port = int(os.environ.get("PORT", 5000))
    # debug=False (Stage M5 fix #23) — debug=True on a host reachable
    # beyond localhost exposes the Werkzeug interactive debugger, which
    # is RCE. Opt in explicitly and locally via FLASK_DEBUG if ever
    # needed, rather than defaulting to it.
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
