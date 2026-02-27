# backend/app.py
# fixed potential problem per Gemini
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from config import Config
from transcription import TranscriptionService
from llm_processor import LLMProcessor
#  from docs_generator import GoogleDocsGenerator  <-- removed per Gemini
import os
import tempfile
import json
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
# docs_generator = GoogleDocsGenerator() <-- Gemini says no good

# Temporary storage for Alpha version (in production, use database)
session_storage = {}


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Sospechoso V

@app.route('/<path:path>')
def serve_static(path):
    """Serve static files"""
    return send_from_directory('../frontend', path)


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'ClinIA Alpha',
        'version': '0.1.0',
        'timestamp': datetime.now().isoformat()
    })
# sospechoso ^

@app.route('/api/process-audio', methods=['POST'])
def process_audio():
    """
    Main orchestrator endpoint - processes audio through entire pipeline
    
    Expected: multipart/form-data with 'audio' file
    Returns: Complete medical note with Google Docs link
    """
    docs_generator = GoogleDocsGenerator()  # <--  Pasted here according to Gemini
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
                print_raw=print_raw
            )
            
            transcript_text = transcript_result['text']
            
            if not transcript_text or len(transcript_text.strip()) < 10:
                return jsonify({
                    'error': 'Transcription too short or empty',
                    'transcript': transcript_text
                }), 400
            
            print(f"[Orchestrator] Transcription completed: {len(transcript_text)} characters")
            
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
            structured_data = llm_processor.extract_structured_data(transcript_text)
            
            # Validate extracted data
            is_valid, error_msg = llm_processor.validate_against_schema(structured_data)
            
            if not is_valid:
                print(f"[Orchestrator] Warning: Data validation failed: {error_msg}")
                # Continue anyway for Alpha version
            
            print("[Orchestrator] Structured data extraction completed")
            
        except Exception as e:
            print(f"[Orchestrator] LLM processing failed: {str(e)}")
            return jsonify({
                'error': 'LLM processing failed',
                'details': str(e),
                'transcript': transcript_text
            }), 500
        
        # Step 5: Create Google Doc (if requested)
        doc_info = None
        
        if create_doc:
            print("\n[Orchestrator] PHASE C: Google Docs Generation")
            print("-" * 80)
            
            try:
                # Generate document title
                patient_name = structured_data.get('informacion_paciente', {}).get(
                    'nombre_del_paciente', 'Paciente'
                )
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                doc_title = f"ClinIA - {patient_name} - {timestamp}"
                
                doc_info = docs_generator.create_medical_note(
                    structured_data,
                    title=doc_title
                )
                
                print(f"[Orchestrator] Google Doc created: {doc_info['link']}")
                
            except Exception as e:
                print(f"[Orchestrator] Google Docs creation failed: {str(e)}")
                print("[Orchestrator] Continuing without document creation")
                doc_info = {
                    'error': 'Failed to create Google Doc',
                    'details': str(e)
                }
        
        # Step 6: Prepare response
        session_id = f"session_{datetime.now().timestamp()}"
        
        response = {
            'session_id': session_id,
            'status': 'success',
            'timestamp': datetime.now().isoformat(),
            'transcript': {
                'text': transcript_text,
                'confidence': transcript_result.get('confidence'),
                'duration_seconds': transcript_result.get('audio_duration', 0) / 1000,
                'word_count': transcript_result.get('words', 0)
            },
            'structured_data': structured_data,
            'document': doc_info
        }
        
        # Store in session storage (for Alpha - use DB in production)
        session_storage[session_id] = response
        
        print(f"\n{'='*80}")
        print(f"[Orchestrator] Processing completed successfully!")
        print(f"[Orchestrator] Session ID: {session_id}")
        if doc_info and 'link' in doc_info:
            print(f"[Orchestrator] Document: {doc_info['link']}")
        print(f"{'='*80}\n")
        
        return jsonify(response), 200
        
    except Exception as e:
        print(f"\n[Orchestrator] CRITICAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'error': 'Internal server error',
            'details': str(e)
        }), 500


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
    Process a raw transcript (for testing LLM without audio)
    
    Expected JSON: {"transcript": "text here"}
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
    if session_id in session_storage:
        return jsonify(session_storage[session_id]), 200
    else:
        return jsonify({'error': 'Session not found'}), 404


@app.route('/api/export-json/<session_id>', methods=['GET'])
def export_json(session_id):
    """Export structured data as downloadable JSON"""
    if session_id not in session_storage:
        return jsonify({'error': 'Session not found'}), 404
    
    data = session_storage[session_id]['structured_data']
    
    response = app.response_class(
        response=json.dumps(data, indent=2, ensure_ascii=False),
        mimetype='application/json'
    )
    response.headers['Content-Disposition'] = f'attachment; filename=clinia_{session_id}.json'
    
    return response


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
