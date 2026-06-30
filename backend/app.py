# backend/app.py
# fixed potential problem per Gemini
from flask import Flask, request, jsonify, send_from_directory, render_template, g
from flask_cors import CORS
from config import Config
from transcription import TranscriptionService
from llm_processor import LLMProcessor
from docs_generator import GoogleDocsGenerator
from pdf_generator import PDFGenerator
from logger import logger
from email_service import send_pdf_email
from auth import require_auth
import os
import re
import tempfile
import json
import sqlite3
from datetime import datetime
from werkzeug.utils import secure_filename


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

# Enable CORS for frontend  <-- replaced below
# CORS(app, resources={
#     r"/api/*": {
#         "origins": ["http://localhost:3000", "http://localhost:5000", "http://127.0.0.1:5000"],
#         "methods": ["GET", "POST", "OPTIONS"],
#         "allow_headers": ["Content-Type"]
#     }
# })

# Enable CORS for both local testing and production domains
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "http://localhost:5000",
            "https://clin-ia-beta.onrender.com",
            "https://clinianotes.com",
            "https://www.clinianotes.com",
            "https://app.clinianotes.com",
            "https://clin-ia-beta.onrender.com",
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
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
# docs_generator = GoogleDocsGenerator() <-- Gemini says no good

# ── SQLite session persistence ──────────────────────────────────────────────

DB_PATH = os.environ.get('DB_PATH', '/data/clinia_sessions.db')

def init_db():
    """Initialize SQLite database and create sessions table if not exists."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id         TEXT PRIMARY KEY,
            timestamp          TEXT NOT NULL,
            transcript_text    TEXT,
            transcript_confidence REAL,
            transcript_duration   REAL,
            transcript_words      INTEGER,
            structured_data_json  TEXT,
            doc_link           TEXT,
            doc_title          TEXT,
            consent_given      INTEGER DEFAULT 0,
            consent_timestamp  TEXT,
            full_response_json TEXT NOT NULL
        )
    ''')
    conn.commit()

    migrations = [
        "ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'pending_review'",
        "ALTER TABLE sessions ADD COLUMN locked_at TEXT",
        "ALTER TABLE sessions ADD COLUMN addenda_json TEXT",
        "ALTER TABLE sessions ADD COLUMN cancelled_at TEXT",
        "ALTER TABLE sessions ADD COLUMN cancellation_reason TEXT",
    ]
    for sql in migrations:
        try:
            cursor.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists — safe to ignore
    conn.commit()
    conn.close()
    logger.info("DB: SQLite session database initialized.")


def save_session(session_id: str, data: dict):
    """Persist a session to SQLite. Silently logs on failure — never raises."""
    try:
        transcript = data.get('transcript') or {}
        doc        = data.get('document')  or {}
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO sessions
            (session_id, timestamp, transcript_text, transcript_confidence,
             transcript_duration, transcript_words, structured_data_json,
             doc_link, doc_title, consent_given, consent_timestamp,
             full_response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session_id,
            data.get('timestamp', datetime.now().isoformat()),
            transcript.get('text', ''),
            transcript.get('confidence'),
            transcript.get('duration_seconds'),
            transcript.get('word_count'),
            json.dumps(data.get('structured_data', {}), ensure_ascii=False),
            doc.get('link'),
            doc.get('title'),
            1 if data.get('consent_given') else 0,
            data.get('consent_timestamp'),
            json.dumps(data, ensure_ascii=False)
        ))
        conn.commit()
        conn.close()
        logger.info(f"DB: Session {session_id} saved.")
    except Exception as e:
        logger.warning(f"DB: Could not save session {session_id}: {str(e)}")


def load_session(session_id: str) -> dict | None:
    """Retrieve a full session from SQLite. Returns None if not found."""
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT full_response_json FROM sessions WHERE session_id = ?',
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        logger.warning(f"DB: Could not load session {session_id}: {str(e)}")
        return None


def load_structured_data(session_id: str) -> dict | None:
    """Retrieve only structured_data for a session — used by the export endpoint."""
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT structured_data_json FROM sessions WHERE session_id = ?',
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        logger.warning(f"DB: Could not load structured data for {session_id}: {str(e)}")
        return None


def save_pdf_to_session(session_id: str, pdf_bytes: bytes):
    """Store PDF bytes in the sessions table (adds the column if not yet present)."""
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Migrate existing databases gracefully — ignore if column already exists
        try:
            cursor.execute('ALTER TABLE sessions ADD COLUMN pdf_data BLOB')
        except sqlite3.OperationalError:
            pass
        cursor.execute(
            'UPDATE sessions SET pdf_data = ? WHERE session_id = ?',
            (pdf_bytes, session_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"PDF: Stored in session {session_id}")
    except Exception as e:
        logger.warning(f"PDF: Could not store PDF: {str(e)}")


# Initialize on startup
init_db()

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
    Main orchestrator endpoint - processes audio through entire pipeline
    
    Expected: multipart/form-data with 'audio' file
    Returns: Complete medical note with Google Docs link
    """
    try:
        # Step 1: Validate request
        audio_file = request.files.get('audio')
        is_valid, error_message = validate_audio_file(audio_file)
        if not is_valid:
            logger.warning(f"Validation: rejected upload — {error_message}")
            return jsonify({'error': error_message, 'error_code': 'INVALID_AUDIO_FILE'}), 400
        logger.info(f"Validation: audio file accepted — {audio_file.filename}")
        
        # Get optional parameters
        print_raw = request.form.get('print_raw', 'true').lower() == 'true'
        create_doc = request.form.get('create_doc', 'true').lower() == 'true'
        speakers_expected = int(request.form.get('speakers_expected', 2))
        local_timestamp = request.form.get('local_timestamp', datetime.now().strftime("%Y-%m-%d %H:%M"))
        consultation_timestamp = request.form.get('consultation_timestamp', local_timestamp)
        consent_given = request.form.get('consent_given', 'false').lower() == 'true'
        consent_timestamp = request.form.get('consent_timestamp', '')
        
        logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
        logger.info("Orchestrator: Starting new processing job")
        logger.info(f"Orchestrator: Filename: {audio_file.filename}")
        logger.debug(f"Orchestrator: Print raw transcript: {print_raw}")
        logger.debug(f"Orchestrator: Create Google Doc: {create_doc}")
        
        # Step 2: Save audio to temporary file
        filename = secure_filename(audio_file.filename)
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"clinia_{datetime.now().timestamp()}_{filename}")
        
        audio_file.save(temp_path)
        logger.debug(f"Orchestrator: Audio saved to: {temp_path}")

        # Get file size for validation
        file_size = os.path.getsize(temp_path)
        logger.debug(f"Orchestrator: File size: {file_size / (1024*1024):.2f} MB")

        # Step 3: Transcribe audio (Phase A)
        logger.info("Orchestrator: PHASE A — Transcription")
        
        try:
            transcript_result = transcription_service.transcribe_audio(
                temp_path,
                print_raw=print_raw,
                speakers_expected=speakers_expected
            )
            
            transcript_text = transcript_result['text']
            
            if not transcript_text or len(transcript_text.strip()) < 10:
                return jsonify({
                    'error': 'Transcription too short or empty',
                    'transcript': transcript_text
                }), 400
            
            logger.info(f"Orchestrator: Transcription completed: {len(transcript_text)} characters")
            logger.info(f"Orchestrator: Transcript ID: {transcript_result.get('transcript_id', 'unknown')} — deletion handled by TranscriptionService.")

        except Exception as e:
            logger.error(f"Orchestrator: Transcription failed: {str(e)}")
            return jsonify({
                'error': 'Transcription failed',
                'details': str(e)
            }), 500
        finally:
            # Clean up audio file
            if os.path.exists(temp_path):
                os.remove(temp_path)
                logger.debug(f"Orchestrator: Cleaned up temp file: {temp_path}")

        # Step 4: Extract structured data (Phase B)
        logger.info("Orchestrator: PHASE B — LLM Processing")
        
        try:
            structured_data = llm_processor.extract_structured_data(
                transcript_text,
                utterances=transcript_result.get('utterances', []),
                role_map=transcript_result.get('speaker_role_map', {})
            )
            
            # Validate extracted data
            is_valid, error_msg = llm_processor.validate_against_schema(structured_data)
            
            if not is_valid:
                logger.warning(f"Orchestrator: Data validation failed: {error_msg}")
                # Continue anyway for Alpha version

            # Inject consultation timestamp (NOM-004 compliance)
            if 'metadata' not in structured_data:
                structured_data['metadata'] = {}
            structured_data['metadata']['fecha_hora_consulta'] = consultation_timestamp

            logger.info("Orchestrator: Structured data extraction completed")

        except Exception as e:
            logger.error(f"Orchestrator: LLM processing failed: {str(e)}")
            return jsonify({
                'error': 'LLM processing failed',
                'details': str(e),
                'transcript': transcript_text
            }), 500
        
        # Step 5: Prepare pending_review response
        initials = _derive_initials(g.usuario.get('nombre', ''))
        session_id = f"SESSION-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{initials}"

        utterances = transcript_result.get('utterances', [])
        role_map = transcript_result.get('speaker_role_map', {})
        def _role(u):
            spk = u['speaker']
            return role_map.get(spk, 'Hablante ' + spk)
        labeled_text = "\n".join(
            f"[{_role(u)}]: {u['text']}" for u in utterances
        ) if utterances else None

        transcript_payload = {
            'text': transcript_text,
            'labeled_text': labeled_text,
            'confidence': transcript_result.get('confidence'),
            'duration_seconds': transcript_result.get('audio_duration', 0) / 1000,
            'word_count': transcript_result.get('words', 0),
            'speaker_role_map': role_map
        }

        response = {
            'session_id': session_id,
            'status': 'pending_review',
            'transcript': transcript_payload,
            'structured_data': structured_data
        }

        # Persist session — store extra fields (local_timestamp, create_doc,
        # consent) alongside the response so confirm_and_generate can read them.
        save_session(session_id, {
            **response,
            'local_timestamp': local_timestamp,
            'create_doc': create_doc,
            'consent_given': consent_given,
            'consent_timestamp': consent_timestamp
        })

        logger.info(f"Orchestrator: Ready for review. Session: {session_id}")
        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Orchestrator: CRITICAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500


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
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT status FROM sessions WHERE session_id = ?', (session_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0] == 'confirmed':
                return jsonify({
                    'error': 'Esta nota ya fue confirmada y no puede modificarse. Para hacer correcciones, utilice la función de adéndum.',
                    'error_code': 'SESSION_LOCKED'
                }), 409
            if row and row[0] == 'cancelled':
                return jsonify({
                    'error': 'Esta nota ha sido cancelada por solicitud ARCO y no puede modificarse.',
                    'error_code': 'SESSION_CANCELLED'
                }), 409
        except Exception as e:
            logger.warning(f"DB: Could not check session lock status: {e}")

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
                pdf_bytes = pdf_generator.generate_pdf(structured_data, session_id=session_id)
                logger.info(f"PDF: Generated successfully — {len(pdf_bytes)} bytes")
            except Exception as e:
                logger.warning(f"PDF: Generation failed: {str(e)}")
                # Never raise — PDF failure must not block the pipeline

        response = {
            'session_id': session_id,
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'transcript': session.get('transcript'),
            'structured_data': structured_data,
            'document': doc_info,
            'pdf_available': pdf_bytes is not None,
            'consent_grabacion': session.get('consent', {}),
            'consent_tratamiento': consent_tratamiento
        }

        # Persist confirmed session first — uses INSERT OR REPLACE which
        # rewrites the full row, so pdf_data must be written afterwards
        save_session(session_id, {
            **session,
            'structured_data': structured_data,
            'document': doc_info,
            'status': 'confirmed',
            'locked_at': datetime.now().isoformat(),
            'timestamp': response['timestamp'],
            'consent_tratamiento': consent_tratamiento
        })

        # Explicitly write new columns (INSERT OR REPLACE doesn't cover them)
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE sessions SET status = ?, locked_at = ? WHERE session_id = ?',
                ('confirmed', datetime.now().isoformat(), session_id)
            )
            conn.commit()
            conn.close()
            logger.info(f"DB: Session {session_id} locked — status=confirmed")
        except Exception as e:
            logger.warning(f"DB: Could not lock session {session_id}: {e}")

        # Delete transcript text to minimize LFPDPPP exposure
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE sessions SET transcript_text = NULL WHERE session_id = ?',
                (session_id,)
            )
            conn.commit()
            conn.close()
            logger.info(f"DB: Transcript text deleted for session {session_id} — LFPDPPP minimization")
        except Exception as e:
            logger.warning(f"DB: Could not delete transcript text for {session_id}: {e}")

        # Store PDF bytes AFTER save_session to prevent the INSERT OR REPLACE
        # from wiping the pdf_data column
        if pdf_bytes:
            save_pdf_to_session(session_id, pdf_bytes)

        # Send PDF to doctor's email if provided
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


@app.route('/api/session/<session_id>', methods=['DELETE'])
@require_auth
def cancel_session(session_id):
    """
    Soft-delete (bloqueo) a session in response to a patient ARCO Cancelación request.
    NOM-004 requires clinical records to be retained for 5 years minimum.
    We block access instead of deleting — hard deletion occurs after the retention period.
    """
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    try:
        reason = request.args.get('reason', 'Solicitud ARCO — Cancelación')
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT status FROM sessions WHERE session_id = ?', (session_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Sesión no encontrada'}), 404
        cursor.execute(
            '''UPDATE sessions
               SET status = 'cancelled',
                   cancelled_at = ?,
                   cancellation_reason = ?,
                   transcript_text = NULL
               WHERE session_id = ?''',
            (datetime.now().isoformat(), reason, session_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"DB: Session {session_id} blocked — ARCO Cancelación request.")
        return jsonify({
            'status': 'cancelled',
            'session_id': session_id,
            'message': 'La sesión ha sido bloqueada conforme al derecho de cancelación LFPDPPP. Los datos clínicos se conservan durante el período de retención obligatorio de 5 años (NOM-004) y serán eliminados definitivamente al vencimiento de dicho plazo.',
        }), 200
    except Exception as e:
        logger.error(f"DB: Error cancelling session {session_id}: {str(e)}")
        return jsonify({'error': str(e)}), 500


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

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT status, addenda_json FROM sessions WHERE session_id = ?', (session_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Sesión no encontrada'}), 404

        status, addenda_json = row
        if status == 'cancelled':
            conn.close()
            return jsonify({'error': 'No se pueden agregar adéndum a una sesión cancelada'}), 409
        if status != 'confirmed':
            conn.close()
            return jsonify({'error': 'Solo se pueden agregar adéndum a notas confirmadas'}), 409

        addenda = json.loads(addenda_json) if addenda_json else []
        new_addendum = {
            'id': f"adendum_{len(addenda) + 1}",
            'type': addendum_type,
            'text': addendum_text,
            'author': author,
            'timestamp': datetime.now().isoformat(),
        }
        addenda.append(new_addendum)

        cursor.execute(
            'UPDATE sessions SET addenda_json = ? WHERE session_id = ?',
            (json.dumps(addenda, ensure_ascii=False), session_id)
        )
        conn.commit()
        conn.close()
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
    Returns the generated PDF as a downloadable file attachment.
    Used when a doctor clicks 'Descargar PDF' in the results screen.
    """
    logger.info(f"Auth: request by {g.usuario['email']} (clinica_id={g.usuario['clinica_id']})")
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Debug logging — reveals session_id mismatches in Render logs
        logger.info(f"PDF: Download requested for session: {session_id}")
        cursor.execute(
            'SELECT session_id, pdf_data IS NOT NULL as has_pdf FROM sessions ORDER BY rowid DESC LIMIT 5'
        )
        recent = cursor.fetchall()
        logger.debug(f"PDF: Recent sessions in DB: {recent}")

        cursor.execute(
            'SELECT pdf_data FROM sessions WHERE session_id = ?',
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if not row or not row[0]:
            return jsonify({'error': 'PDF no disponible para esta sesión'}), 404

        pdf_bytes = row[0]

        # Build filename from patient name + date
        session = load_session(session_id)
        patient_name = 'Paciente'
        if session:
            patient_name = (session
                .get('structured_data', {})
                .get('informacion_paciente', {})
                .get('nombre_del_paciente', 'Paciente'))
        safe_name = ''.join(
            c for c in patient_name if c.isalnum() or c in (' ', '-')
        ).strip().replace(' ', '_')[:30]
        date_str  = datetime.now().strftime('%Y%m%d')
        filename  = f"ClinIA_{safe_name}_{date_str}.pdf"

        response = app.response_class(
            response=pdf_bytes,
            mimetype='application/pdf'
        )
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        response.headers['Content-Length'] = len(pdf_bytes)
        return response

    except Exception as e:
        logger.error(f"PDF: Download error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Error handlers
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        'error': 'File too large',
        'max_size_mb': 50
    }), 413


@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({
        'error': 'Internal server error',
        'details': str(error)
    }), 500

# remove per Gemini
# if __name__ == '__main__':
#     print("\n" + "="*80)
#     print("ClinIA Alpha - Medical Note Taker")
#     print("="*80)
#     print("Starting Flask server...")
#     print("Frontend will be available at: http://localhost:5000")
#     print("API endpoints:")
#     print("  - POST /api/process-audio      (Full pipeline)")
#     print("  - POST /api/transcribe-only    (Transcription only)"
#     print("  - POST /api/process-transcript (LLM processing only)")
#     print("  - GET  /api/health             (Health check)")
#     print("="*80 + "\n")

if __name__ == '__main__':
    # This block only runs when you run 'python app.py' on your computer.
    # It does NOT run on Render.
    logger.info("ClinIA Beta - Medical Note Taker (Local Mode) — starting Flask server")
    
    # We use port 5000 locally, but Render will assign its own port via environment variables
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

    
    # Run Flask development server
#     app.run(
#         host='0.0.0.0',
#         port=5000,
#         debug=True
#     )
