# backend/app.py
# fixed potential problem per Gemini
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from config import Config
from transcription import TranscriptionService
from llm_processor import LLMProcessor
from docs_generator import GoogleDocsGenerator
from pdf_generator import PDFGenerator
#  from docs_generator import GoogleDocsGenerator  <-- removed per Gemini
import os
import tempfile
import json
import sqlite3
from datetime import datetime
from werkzeug.utils import secure_filename

# --- THE TELEPORTER ---
# This checks if we are in the cloud. If so, it creates the secret file from a variable.
# This recreates the physical JSON files from Render Environment Variables
def teleport_secrets():
    if os.environ.get("RENDER"):
        print("[Teleporter] Running in Cloud mode...")
        
        # 1. Teleport the Client Secrets (OAuth Web)
        if "GOOGLE_SECRETS_JSON" in os.environ:
            with open("client_secrets.json", "w") as f:
                f.write(os.environ["GOOGLE_SECRETS_JSON"])
            print("[Teleporter] client_secrets.json created.")

        # 2. Teleport the Service Account (if you use it)
        if "GOOGLE_SERVICE_ACCOUNT_JSON" in os.environ:
            with open("credentials.json", "w") as f:
                f.write(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
            print("[Teleporter] credentials.json created.")

# Run the teleporter immediately
print("[STARTUP] Teleporter starting...") # <--  Test only
teleport_secrets()
print("[STARTUP] Teleporter finished.")   # <--  Test only
# ----------------------


# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Enable CORS for frontend  <-- replaced below
# CORS(app, resources={
#     r"/api/*": {
#         "origins": ["http://localhost:3000", "http://localhost:5000", "http://127.0.0.1:5000"],
#         "methods": ["GET", "POST", "OPTIONS"],
#         "allow_headers": ["Content-Type"]
#     }
# })

# Enable CORS for both local testing and your new Render URL
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "http://localhost:3000", 
            "http://localhost:5000", 
            "https://clin-ia-beta.onrender.com"  # <--- Add your actual Render URL here
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# Validate configuration on startup
try:
    Config.validate()
    print("[Flask] Configuration validated successfully")
except ValueError as e:
    print(f"[Flask] Configuration error: {e}")
    print("[Flask] Please check your .env file")
    exit(1)

# Initialize services
transcription_service = TranscriptionService()
llm_processor = LLMProcessor()
pdf_generator = PDFGenerator()
# docs_generator = GoogleDocsGenerator() <-- Gemini says no good

# ── SQLite session persistence ──────────────────────────────────────────────

DB_PATH = 'clinia_sessions.db'

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
    conn.close()
    print("[DB] SQLite session database initialized.", flush=True)


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
        print(f"[DB] Session {session_id} saved.", flush=True)
    except Exception as e:
        print(f"[DB] Warning: Could not save session {session_id}: {str(e)}", flush=True)


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
        print(f"[DB] Warning: Could not load session {session_id}: {str(e)}", flush=True)
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
        print(f"[DB] Warning: Could not load structured data for {session_id}: {str(e)}", flush=True)
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
        print(f"[PDF] Stored in session {session_id}", flush=True)
    except Exception as e:
        print(f"[PDF] Warning: could not store PDF: {str(e)}", flush=True)


# Initialize on startup
init_db()

# ────────────────────────────────────────────────────────────────────────────


@app.route('/')
def index():
    return render_template('index.html')

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
def process_audio():
    """
    Main orchestrator endpoint - processes audio through entire pipeline
    
    Expected: multipart/form-data with 'audio' file
    Returns: Complete medical note with Google Docs link
    """
    try:
        # Step 1: Validate request
        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400
        
        audio_file = request.files['audio']
        
        if audio_file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400
        
        # Get optional parameters
        print_raw = request.form.get('print_raw', 'true').lower() == 'true'
        create_doc = request.form.get('create_doc', 'true').lower() == 'true'
        speakers_expected = int(request.form.get('speakers_expected', 2))
        local_timestamp = request.form.get('local_timestamp', datetime.now().strftime("%Y-%m-%d %H:%M"))
        consultation_timestamp = request.form.get('consultation_timestamp', local_timestamp)
        consent_given = request.form.get('consent_given', 'false').lower() == 'true'
        consent_timestamp = request.form.get('consent_timestamp', '')
        
        print(f"\n{'='*80}")
        print(f"[Orchestrator] Starting new processing job")
        print(f"[Orchestrator] Filename: {audio_file.filename}")
        print(f"[Orchestrator] Print raw transcript: {print_raw}")
        print(f"[Orchestrator] Create Google Doc: {create_doc}")
        print(f"{'='*80}\n")
        
        # Step 2: Save audio to temporary file
        filename = secure_filename(audio_file.filename)
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"clinia_{datetime.now().timestamp()}_{filename}")
        
        audio_file.save(temp_path)
        print(f"[Orchestrator] Audio saved to: {temp_path}")
        
        # Get file size for validation
        file_size = os.path.getsize(temp_path)
        print(f"[Orchestrator] File size: {file_size / (1024*1024):.2f} MB")
        
        # Step 3: Transcribe audio (Phase A)
        print("\n[Orchestrator] PHASE A: Transcription")
        print("-" * 80)
        
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
            
            print(f"[Orchestrator] Transcription completed: {len(transcript_text)} characters")
            print(f"[Orchestrator] Transcript ID: {transcript_result.get('transcript_id', 'unknown')} — deletion handled by TranscriptionService.", flush=True)
            
        except Exception as e:
            print(f"[Orchestrator] Transcription failed: {str(e)}")
            return jsonify({
                'error': 'Transcription failed',
                'details': str(e)
            }), 500
        finally:
            # Clean up audio file
            if os.path.exists(temp_path):
                os.remove(temp_path)
                print(f"[Orchestrator] Cleaned up temp file: {temp_path}")
        
        # Step 4: Extract structured data (Phase B)
        print("\n[Orchestrator] PHASE B: LLM Processing")
        print("-" * 80)
        
        try:
            structured_data = llm_processor.extract_structured_data(
                transcript_text,
                utterances=transcript_result.get('utterances', []),
                role_map=transcript_result.get('speaker_role_map', {})
            )
            
            # Validate extracted data
            is_valid, error_msg = llm_processor.validate_against_schema(structured_data)
            
            if not is_valid:
                print(f"[Orchestrator] Warning: Data validation failed: {error_msg}")
                # Continue anyway for Alpha version
            
            # Inject consultation timestamp (NOM-004 compliance)
            if 'metadata' not in structured_data:
                structured_data['metadata'] = {}
            structured_data['metadata']['fecha_hora_consulta'] = consultation_timestamp

            print("[Orchestrator] Structured data extraction completed")

        except Exception as e:
            print(f"[Orchestrator] LLM processing failed: {str(e)}")
            return jsonify({
                'error': 'LLM processing failed',
                'details': str(e),
                'transcript': transcript_text
            }), 500
        
        # Step 5: Prepare pending_review response
        session_id = f"session_{datetime.now().timestamp()}"

        utterances = transcript_result.get('utterances', [])
        role_map = transcript_result.get('speaker_role_map', {})
        labeled_text = "\n".join(
            f"[{role_map.get(u['speaker'], f'Hablante {u[\"speaker\"]}')}]: {u['text']}"
            for u in utterances
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

        print(f"[Orchestrator] Ready for review. Session: {session_id}")
        return jsonify(response), 200

    except Exception as e:
        print(f"\n[Orchestrator] CRITICAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500


@app.route('/api/confirm-and-generate', methods=['POST'])
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
        consent_tratamiento = {
            'given': data.get('consent_tratamiento_given', False),
            'timestamp': data.get('consent_tratamiento_timestamp', '')
        }

        if not session_id:
            return jsonify({'error': 'Session not found'}), 404

        session = load_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        local_timestamp = session.get('local_timestamp', datetime.now().strftime("%Y-%m-%d %H:%M"))

        doc_info = None
        if create_doc:
            print("\n[Orchestrator] PHASE C: Google Docs Generation")
            print("-" * 80)
            try:
                docs_generator = GoogleDocsGenerator()
                patient_name = structured_data.get('informacion_paciente', {}).get(
                    'nombre_del_paciente', 'Paciente'
                )
                doc_title = f"ClinIA - {patient_name} - {local_timestamp}"
                doc_info = docs_generator.create_medical_note(structured_data, title=doc_title)
                print(f"[Orchestrator] Google Doc created: {doc_info['link']}")
            except Exception as e:
                print(f"[Orchestrator] Google Docs creation failed: {str(e)}")
                doc_info = {'error': 'Failed to create Google Doc', 'details': str(e)}

        # PDF generation (if requested) — runs independently of Google Docs
        pdf_bytes = None
        if create_pdf:
            print("\n[Orchestrator] PHASE C2: PDF Generation")
            print("-" * 80)
            try:
                pdf_bytes = pdf_generator.generate_pdf(structured_data)
                print(f"[PDF] Generated successfully — {len(pdf_bytes)} bytes", flush=True)
            except Exception as e:
                print(f"[PDF] Warning: generation failed: {str(e)}", flush=True)
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
            'timestamp': response['timestamp'],
            'consent_tratamiento': consent_tratamiento
        })

        # Store PDF bytes AFTER save_session to prevent the INSERT OR REPLACE
        # from wiping the pdf_data column
        if pdf_bytes:
            save_pdf_to_session(session_id, pdf_bytes)

        print(f"[Orchestrator] Confirmation complete. Session: {session_id}")
        return jsonify(response), 200

    except Exception as e:
        print(f"\n[Orchestrator] CRITICAL ERROR in confirm: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500


@app.route('/api/transcribe-only', methods=['POST'])
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
def get_session(session_id):
    """Retrieve session data by ID"""
    session = load_session(session_id)
    if session:
        return jsonify(session), 200
    return jsonify({'error': 'Session not found'}), 404


@app.route('/api/session/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """
    Delete a session record from SQLite.
    Used when a patient exercises their derecho de cancelación under LFPDPPP.
    The corresponding Google Doc must be deleted manually by the doctor from their Drive.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        if affected:
            print(f"[DB] Session {session_id} deleted — patient ARCO request.", flush=True)
            return jsonify({
                'status': 'deleted',
                'session_id': session_id,
                'note': 'El documento en Google Drive debe ser eliminado manualmente por el Responsable.'
            }), 200
        return jsonify({'error': 'Session not found'}), 404
    except Exception as e:
        print(f"[DB] Error deleting session {session_id}: {str(e)}", flush=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/export-json/<session_id>', methods=['GET'])
def export_json(session_id):
    """Export structured data as downloadable JSON"""
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
def download_pdf(session_id):
    """
    Returns the generated PDF as a downloadable file attachment.
    Used when a doctor clicks 'Descargar PDF' in the results screen.
    """
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Debug logging — reveals session_id mismatches in Render logs
        print(f"[PDF] Download requested for session: {session_id}", flush=True)
        cursor.execute(
            'SELECT session_id, pdf_data IS NOT NULL as has_pdf FROM sessions ORDER BY rowid DESC LIMIT 5'
        )
        recent = cursor.fetchall()
        print(f"[PDF] Recent sessions in DB: {recent}", flush=True)

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
        print(f"[PDF] Download error: {str(e)}", flush=True)
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
    print("\n" + "="*80)
    print("ClinIA Beta - Medical Note Taker (Local Mode)")
    print("="*80)
    
    # We use port 5000 locally, but Render will assign its own port via environment variables
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

    
    # Run Flask development server
#     app.run(
#         host='0.0.0.0',
#         port=5000,
#         debug=True
#     )
