# backend/app.py
from flask import Flask, request, jsonify, send_from_directory, render_template, g
from flask_cors import CORS
from config import Config
from transcription import TranscriptionService
from llm_processor import LLMProcessor
from docs_generator import GoogleDocsGenerator
from pdf_generator import PDFGenerator
from logger import logger
from email_service import send_pdf_email, send_invite_email
from auth import require_auth, require_admin
from concurrent.futures import ThreadPoolExecutor
import os
import re
import tempfile
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from io import BytesIO
from PIL import Image as PILImage, UnidentifiedImageError


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


# --- THE TELEPORTER ---
# This checks if we are in the cloud. If so, it creates the secret file from a variable.
# This recreates the physical JSON files from Render Environment Variables
def teleport_secrets():
    if os.environ.get("RENDER"):
        logger.info("Teleporter: Running in Cloud mode...")

        # 1. Teleport the Client Secrets (OAuth Web)
        if "GOOGLE_SECRETS_JSON" in os.environ:
            with open("client_secrets.json", "w") as f:
                f.write(os.environ["GOOGLE_SECRETS_JSON"])
            logger.info("Teleporter: client_secrets.json created.")

        # 2. Teleport the Service Account (if you use it)
        if "GOOGLE_SERVICE_ACCOUNT_JSON" in os.environ:
            with open("credentials.json", "w") as f:
                f.write(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            logger.info("Teleporter: credentials.json created.")

# Run the teleporter immediately
logger.info("Startup: Teleporter starting...")
teleport_secrets()
logger.info("Startup: Teleporter finished.")
# ----------------------


# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB — matches Config.MAX_AUDIO_SIZE_BYTES

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
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{bucket}/{path}'
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
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{bucket}/{path}'
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
    url = Config.SUPABASE_URL.rstrip('/') + f'/storage/v1/object/{bucket}/{path}'
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
    url = Config.SUPABASE_URL.rstrip('/') + f'/auth/v1/admin/users/{user_id}'
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
    _sb_patch(f'/rest/v1/trabajos?job_id=eq.{job_id}', body)


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
            _sb_patch_job(job_id, {'status': 'error', 'error_message': f'Transcripción fallida: {str(e)}'})
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
                utterances=transcript_result.get('utterances', []),
                role_map=transcript_result.get('speaker_role_map', {})
            )
        except Exception as e:
            logger.error(f"Job {job_id}: LLM extraction failed — {e}")
            _sb_patch_job(job_id, {'status': 'error', 'error_message': f'Extracción de datos fallida: {str(e)}'})
            return

        if 'metadata' not in structured_data:
            structured_data['metadata'] = {}
        structured_data['metadata']['fecha_hora_consulta'] = params['consultation_timestamp']

        # Build session
        initials  = _derive_initials(nombre)
        session_id = f"SESSION-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{initials}"

        utterances = transcript_result.get('utterances', [])
        role_map   = transcript_result.get('speaker_role_map', {})

        def _role(u):
            return role_map.get(u['speaker'], 'Hablante ' + u['speaker'])

        labeled_text = "\n".join(f"[{_role(u)}]: {u['text']}" for u in utterances) if utterances else None

        transcript_payload = {
            'text':             transcript_text,
            'labeled_text':     labeled_text,
            'confidence':       transcript_result.get('confidence'),
            'duration_seconds': transcript_result.get('audio_duration', 0) / 1000,
            'word_count':       transcript_result.get('words', 0),
            'speaker_role_map': role_map,
        }

        save_session(session_id, {
            'session_id':        session_id,
            'status':            'pending_review',
            'transcript':        transcript_payload,
            'structured_data':   structured_data,
            'local_timestamp':   params['local_timestamp'],
            'create_doc':        params['create_doc'],
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
        _sb_patch_job(job_id, {'status': 'error', 'error_message': f'Error interno: {str(e)}'})
        if os.path.exists(audio_path):
            os.remove(audio_path)


# ── Supabase session persistence ─────────────────────────────────────────────

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
    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=full_response&limit=1')
    if not rows:
        return None
    return rows[0].get('full_response')


def load_structured_data(session_id: str) -> dict | None:
    """Retrieve only structured_data for a session — used by the export endpoint."""
    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=structured_data&limit=1')
    if not rows:
        return None
    return rows[0].get('structured_data')


def get_clinica_context(clinica_id: str) -> dict:
    """
    Fetch clinic name, primary color, address, and phone from Supabase.
    Returns defaults on error. direccion/telefono default to empty string
    (not placeholder text) — an unfilled field must render as absent, not
    as a fake value.
    """
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{clinica_id}&select=nombre,color_primario,direccion,telefono,logo_url&limit=1')
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
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{usuario_id}&select=cedula&limit=1')
    if rows:
        return rows[0].get('cedula') or ''
    return ''


def get_usuario_nombre(usuario_id: str) -> str:
    """Fetch a doctor's display name only — no email/cédula/other fields. Empty string on error."""
    rows = _sb_get(f'/rest/v1/usuarios?id=eq.{usuario_id}&select=nombre&limit=1')
    if rows:
        return rows[0].get('nombre') or ''
    return ''

# ────────────────────────────────────────────────────────────────────────────


@app.route('/')
def index():
    return render_template('index.html')

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
    return render_template('admin.html')

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
def process_audio():
    """
    Validates audio, saves to temp, captures request context, submits background job.
    Returns 202 {job_id} immediately; caller polls /api/job-status/<job_id>.
    """
    try:
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
            'print_raw':            request.form.get('print_raw', 'true').lower() == 'true',
            'create_doc':           request.form.get('create_doc', 'true').lower() == 'true',
            'speakers_expected':    int(request.form.get('speakers_expected', 2)),
            'local_timestamp':      local_timestamp,
            'consultation_timestamp': request.form.get('consultation_timestamp', local_timestamp),
            'consent_given':        request.form.get('consent_given', 'false').lower() == 'true',
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
        logger.error(f"process_audio: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': 'Error interno', 'details': str(e)}), 500


@app.route('/api/job-status/<job_id>', methods=['GET'])
@require_auth
def job_status(job_id):
    """Poll processing status for a background job."""
    rows = _sb_get(
        f'/rest/v1/trabajos?job_id=eq.{job_id}'
        f'&select=job_id,status,error_message,session_id,structured_data,transcript,usuario_id'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Trabajo no encontrado'}), 404
    row = rows[0]
    if row.get('usuario_id') != g.usuario['usuario_id']:
        return jsonify({'error': 'No autorizado'}), 403

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
def confirm_and_generate():
    """
    Receives doctor-reviewed structured_data, creates Google Doc, returns final response.
    Expected JSON: { "session_id": "...", "structured_data": {...}, "create_doc": true }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        session_id = data.get('session_id')
        structured_data = data.get('structured_data', {})
        create_doc = data.get('create_doc', True)
        create_pdf = data.get('create_pdf', False)
        doctor_email = data.get('doctor_email', '').strip()
        consent_tratamiento = {
            'given': data.get('consent_tratamiento_given', False),
            'timestamp': data.get('consent_tratamiento_timestamp', '')
        }

        logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")

        if not session_id:
            return jsonify({'error': 'Session not found'}), 404

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        # NOM-024 immutability — reject if already confirmed or cancelled
        rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=status&limit=1')
        if rows:
            current_status = rows[0].get('status')
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

        local_timestamp = session.get('local_timestamp', datetime.now().strftime("%Y-%m-%d %H:%M"))

        doc_info = None
        if create_doc:
            logger.info("Orchestrator: PHASE C — Google Docs Generation")
            try:
                docs_generator = GoogleDocsGenerator()
                patient_name = structured_data.get('informacion_paciente', {}).get(
                    'nombre_del_paciente', 'Paciente'
                )
                doc_title = f"ClinIA - {patient_name} - {local_timestamp}"
                doc_info = docs_generator.create_medical_note(structured_data, title=doc_title)
                logger.info(f"Orchestrator: Google Doc created: {doc_info['link']}")
            except Exception as e:
                logger.error(f"Orchestrator: Google Docs creation failed: {str(e)}")
                doc_info = {'error': 'Failed to create Google Doc', 'details': str(e)}

        # PDF generation (if requested) — runs independently of Google Docs
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
            'consent_grabacion': session.get('consent', {}),
            'consent_tratamiento': consent_tratamiento
        }

        # Persist confirmed session
        save_session(session_id, {
            **session,
            'structured_data': structured_data,
            'document': doc_info,
            'status': 'confirmed',
            'locked_at': locked_at,
            'timestamp': locked_at,
            'consent_tratamiento': consent_tratamiento
        }, usuario_id=g.usuario['usuario_id'], clinica_id=g.usuario['clinica_id'])

        # Delete transcript text to minimize LFPDPPP exposure
        ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{session_id}', {'transcript_text': None})
        if ok:
            logger.info(f"DB: Transcript text deleted for session {session_id} — LFPDPPP minimization")
        else:
            logger.warning(f"DB: Could not delete transcript text for {session_id}")

        # Send PDF to doctor's email if generated
        if pdf_bytes and doctor_email:
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
        logger.error(f"Orchestrator: CRITICAL ERROR in confirm: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500


@app.route('/api/transcribe-only', methods=['POST'])
# TODO: remove or auth-gate before production — this is a test-only utility
def transcribe_only():
    """
    Endpoint for transcription only (no LLM processing)
    Useful for testing and verification
    """
    try:
        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400

        audio_file = request.files['audio']
        print_raw = request.form.get('print_raw', 'true').lower() == 'true'

        # Save to temp file
        filename = secure_filename(audio_file.filename)
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        audio_file.save(temp_path)

        try:
            # Transcribe
            result = transcription_service.transcribe_audio(temp_path, print_raw=print_raw)

            return jsonify({
                'status': 'success',
                'transcript': result
            }), 200

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    except Exception as e:
        return jsonify({
            'error': 'Transcription failed',
            'details': str(e)
        }), 500


@app.route('/api/process-transcript', methods=['POST'])
# TODO: remove or auth-gate before production — this is a test-only utility
def process_transcript():
    """
    Process a raw transcript (for testing LLM without audio).
    Expected JSON: {"transcript": "text here"}
    Note: this endpoint does not create a persistent session — it is a
    one-shot testing utility and has never written to session storage.
    """
    try:
        data = request.get_json()

        if not data or 'transcript' not in data:
            return jsonify({'error': 'No transcript provided'}), 400

        transcript = data['transcript']

        # Process with LLM
        structured_data = llm_processor.extract_structured_data(transcript)

        # Optionally create document
        create_doc = data.get('create_doc', False)
        doc_info = None

        if create_doc:
            docs_generator = GoogleDocsGenerator()
            doc_info = docs_generator.create_medical_note(structured_data)

        return jsonify({
            'status': 'success',
            'structured_data': structured_data,
            'document': doc_info
        }), 200

    except Exception as e:
        return jsonify({
            'error': 'Processing failed',
            'details': str(e)
        }), 500


@app.route('/api/session/<session_id>', methods=['GET'])
@require_auth
def get_session(session_id):
    """Retrieve session data by ID"""
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    session = load_session(session_id)
    if session:
        return jsonify(session), 200
    return jsonify({'error': 'Session not found'}), 404


@app.route('/api/session/<session_id>/addendum', methods=['POST'])
@require_auth
def add_addendum(session_id):
    """
    Append an addendum to a confirmed (locked) session.
    Types: 'adendum_clinico' (doctor correction) or 'rectificacion_arco' (patient ARCO request).
    The original structured_data is never modified.
    """
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        addendum_text = data.get('addendum_text', '').strip()
        addendum_type = data.get('addendum_type', 'adendum_clinico')
        author        = data.get('author', 'Médico')

        if not addendum_text:
            return jsonify({'error': 'El texto del adéndum no puede estar vacío'}), 400
        if addendum_type not in ('adendum_clinico', 'rectificacion_arco'):
            return jsonify({'error': 'Tipo de adéndum no válido'}), 400

        rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=status,addenda&limit=1')
        if not rows:
            return jsonify({'error': 'Sesión no encontrada'}), 404

        row = rows[0]
        status  = row.get('status')
        addenda = row.get('addenda') or []

        if status == 'cancelled':
            return jsonify({'error': 'No se pueden agregar adéndum a una sesión cancelada'}), 409
        if status != 'confirmed':
            return jsonify({'error': 'Solo se pueden agregar adéndum a notas confirmadas'}), 409

        new_addendum = {
            'id':        f"adendum_{len(addenda) + 1}",
            'type':      addendum_type,
            'text':      addendum_text,
            'author':    author,
            'timestamp': datetime.now().isoformat(),
        }
        addenda.append(new_addendum)

        ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{session_id}', {'addenda': addenda})
        if not ok:
            return jsonify({'error': 'No se pudo guardar el adéndum'}), 500

        logger.info(f"DB: Addendum added to session {session_id} — type={addendum_type}")
        return jsonify({'status': 'ok', 'addendum': new_addendum, 'total_addenda': len(addenda)}), 200

    except Exception as e:
        logger.error(f"DB: Error adding addendum to session {session_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/export-json/<session_id>', methods=['GET'])
@require_auth
def export_json(session_id):
    """Export structured data as downloadable JSON"""
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    data = load_structured_data(session_id)
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
            f'/rest/v1/sesiones?session_id=eq.{session_id}'
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
        scope_filter = f'clinica_id=eq.{clinica_id}'
    else:
        scope_filter = f'usuario_id=eq.{usuario_id}'

    path = (
        f'/rest/v1/sesiones'
        f'?paciente_curp=eq.{curp}'
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
        f'?session_id=eq.{session_id}'
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
        f'?usuario_id=eq.{usuario_id}'
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
        f'?session_id=eq.{session_id}'
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
        f'?session_id=eq.{session_id}'
        f'&select=usuario_id,status'
        f'&limit=1'
    )
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('usuario_id') != usuario_id or row.get('status') != 'pending_review':
        return jsonify({'error': 'Sesión no encontrada'}), 404

    trabajos_deleted = _sb_delete(f'/rest/v1/trabajos?session_id=eq.{session_id}')
    if not trabajos_deleted:
        logger.warning(f"Discard: could not delete trabajos row(s) for session {session_id} (best-effort, continuing)")

    ok = _sb_delete(f'/rest/v1/sesiones?session_id=eq.{session_id}')
    if not ok:
        return jsonify({'error': 'No se pudo descartar la nota'}), 500

    logger.info(f"Discard: session {session_id} permanently deleted by usuario_id={usuario_id}")
    return jsonify({'status': 'ok'}), 200


@app.route('/api/session-check', methods=['GET'])
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
        f'?clinica_id=eq.{clinica_id}'
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
    existing = _sb_get(f"/rest/v1/usuarios?email=ilike.{email}&select=id&limit=1")
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
        f'/rest/v1/usuarios?id=eq.{usuario_id}'
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
    updated = _sb_patch(f'/rest/v1/usuarios?id=eq.{usuario_id}', {'activo': nuevo_activo})
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
        f'/rest/v1/clinicas?id=eq.{clinica_id}'
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

    ok = _sb_patch(f'/rest/v1/clinicas?id=eq.{clinica_id}', body)
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

    updated = _sb_patch(f'/rest/v1/clinicas?id=eq.{clinica_id}', {'logo_url': storage_path})
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
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{clinica_id}&select=logo_url&limit=1')
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
    rows = _sb_get(f'/rest/v1/clinicas?id=eq.{clinica_id}&select=logo_url&limit=1')
    if not rows or not rows[0].get('logo_url'):
        return jsonify({'error': 'Esta clínica no tiene logo configurado'}), 404

    storage_path = rows[0]['logo_url']
    _storage_delete(CLINIC_LOGOS_BUCKET, storage_path)  # best-effort — clear logo_url regardless

    updated = _sb_patch(f'/rest/v1/clinicas?id=eq.{clinica_id}', {'logo_url': None})
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

    filters = [f'clinica_id=eq.{clinica_id}']
    if desde:
        filters.append(f'timestamp=gte.{desde}')
    if hasta:
        # hasta is a date (YYYY-MM-DD); make it inclusive of the whole day
        filters.append(f'timestamp=lte.{hasta}T23:59:59.999')
    if usuario_id_filter:
        filters.append(f'usuario_id=eq.{usuario_id_filter}')

    path = (
        f'/rest/v1/sesiones'
        f'?{"&".join(filters)}'
        f'&select=session_id,timestamp,usuario_id,status,addenda'
        f'&order=timestamp.desc'
    )
    rows = _sb_get(path)
    if rows is None:
        return jsonify({'error': 'Error al consultar sesiones'}), 500

    # Resolve doctor names via a plain second query per DISTINCT usuario_id
    # (not a PostgREST embed — same reasoning as item 24 Stage 4: embeds
    # couldn't be verified reliably). This is new caching logic for this
    # stage, not reused/tested code from elsewhere.
    nombre_cache = {}
    for r in rows:
        uid = r.get('usuario_id')
        if uid and uid not in nombre_cache:
            nombre_cache[uid] = get_usuario_nombre(uid)

    result = [
        {
            'session_id':    r.get('session_id'),
            'timestamp':     r.get('timestamp'),
            'doctor_nombre': nombre_cache.get(r.get('usuario_id'), ''),
            'status':        r.get('status'),
            'tiene_adenda':  bool(r.get('addenda')) and len(r.get('addenda')) > 0,
        }
        for r in rows
    ]
    return jsonify(result), 200


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
            f'/rest/v1/sesiones?session_id=eq.{session_id}'
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

    Field shape matches the existing (dormant, no frontend caller)
    /api/session/<id>/addendum route's convention exactly — id, type,
    text, author, timestamp — so the already-live adenda renderer
    (session-detail-render.js, unchanged this stage) displays these
    correctly with zero renderer changes. author_usuario_id is an
    ADDITIONAL field beyond that existing shape, recording the specific
    admin account that wrote it (an unspoofable link server-side sets,
    never client-supplied) — the renderer simply ignores it, it isn't a
    conflicting field.
    """
    MAX_ADDENDUM_LENGTH = 2000

    data = request.get_json(silent=True) or {}
    texto = (data.get('texto') or '').strip()

    if not texto:
        return jsonify({'error': 'El texto del adendum no puede estar vacío'}), 400
    if len(texto) > MAX_ADDENDUM_LENGTH:
        return jsonify({'error': f'El texto no puede exceder {MAX_ADDENDUM_LENGTH} caracteres'}), 400

    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=clinica_id,addenda&limit=1')
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

    ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{session_id}', {'addenda': addenda})
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

    rows = _sb_get(f'/rest/v1/sesiones?session_id=eq.{session_id}&select=clinica_id,status&limit=1')
    if not rows:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    row = rows[0]
    if row.get('clinica_id') != g.usuario['clinica_id']:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    ok = _sb_patch(f'/rest/v1/sesiones?session_id=eq.{session_id}', {
        'status':                  'cancelled',
        'cancelled_at':            datetime.now().isoformat(),
        'cancellation_reason':     cancellation_reason,
        'cancelled_by_usuario_id': g.usuario['usuario_id'],
        'transcript_text':         None,
    })
    if not ok:
        logger.error(f"Admin: could not cancel session {session_id}")
        return jsonify({'error': 'No se pudo cancelar la nota'}), 500

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
    return jsonify({
        'error': 'Internal server error',
        'details': str(error)
    }), 500


if __name__ == '__main__':
    # This block only runs when you run 'python app.py' on your computer.
    # It does NOT run on Render.
    logger.info("ClinIA Beta - Medical Note Taker (Local Mode) — starting Flask server")

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
