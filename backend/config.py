# backend/config.py
import os
import json
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Configuration management for ClinIA"""
    
    # AssemblyAI
    ASSEMBLYAI_API_KEY = os.getenv('ASSEMBLYAI_API_KEY')
    
    # LLM Provider (we'll use Gemini as primary)
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
#    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    
    # WHO ICD-11 API
    ICD_CLIENT_ID        = os.environ.get('ICD_CLIENT_ID', '')
    ICD_CLIENT_SECRET    = os.environ.get('ICD_CLIENT_SECRET', '')
    ICD11_RELEASE_VERSION = os.environ.get('ICD11_RELEASE_VERSION', '2026-01')
    # 0.8.7: last stage of the CIE-11 lookup chain (/autocode). WHO documents
    # matchScore as "a value between 0 and 1 ... The higher the score the
    # better the match" but not its default threshold
    # (https://id.who.int/swagger/v2/swagger.json, AutoCode). 0.80 is a
    # deliberately conservative start: autocode always returns its single best
    # guess, and by this stage three searches have already failed, so a weak
    # match is more likely wrong than right — and a wrong code shown as "OMS"
    # is worse than an empty field the doctor fills. The outcome log carries
    # the score (a number, not PHI) so this can be calibrated from real runs.
    CIE11_AUTOCODE_MIN_SCORE = 0.80

    # #81: which backend extracts the SOAP note. 'gemini' (default) is the
    # Google Gemini path, unchanged; 'gateway' sends the same prompt to
    # claude-haiku-5-5 through the AssemblyAI LLM Gateway with
    # ASSEMBLYAI_API_KEY (llm_gateway.py). Any other value logs an error at
    # startup and falls back to 'gemini'. The Gemini path stays as the
    # fallback until it's removed after 1 Dec 2026.
    LLM_BACKEND = (os.environ.get('LLM_BACKEND') or 'gemini').strip().lower()

    # Resend email delivery
    RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
    RESEND_SENDER  = 'admin@clinianotes.com'

    # TERMINOS_VERSION names the legal texts a user accepts: Términos de
    # Servicio + Aviso para Usuarios + Aviso para Pacientes (AP since 0.9.0:
    # its §4 lists the subencargados). It is stored with every
    # solicitudes_registro row (#53/#54) and, per user, in
    # aceptaciones_terminos: a user with no row for the CURRENT value sees a
    # one-time notice at login (static/terminos.js) until they click
    # "Acepto". It never blocks anything. Bumping it shows the notice to
    # every user again. It must equal legal/build_legal.py's version_code()
    # (tests/test_legal.py).
    TERMINOS_VERSION = 'ToS-1.3-Beta+AU-1.2-Beta+AP-1.3-Beta'
    # Both optional; unset = feature off, never an error. SUPERADMIN_EMAIL
    # gets a notice per new request; SUPERADMIN_USER_IDS (comma-separated
    # usuarios/auth UUIDs) gates the superadmin review page (Review C).
    SUPERADMIN_EMAIL    = (os.environ.get('SUPERADMIN_EMAIL') or '').strip()
    SUPERADMIN_USER_IDS = frozenset(
        uid.strip().lower()
        for uid in (os.environ.get('SUPERADMIN_USER_IDS') or '').split(',')
        if uid.strip()
    )

    # Supabase
    # SUPABASE_JWT_SECRET is no longer needed — JWT verification uses the JWKS endpoint (ES256)
    # Guarded (Stage M5 fix #29): os.getenv() returns None if unset, and
    # None.rstrip('/') raised an AttributeError right here at import time
    # — before validate() below ever got a chance to run and print its
    # intended friendly "SUPABASE_URL is required" message. An empty
    # string still fails validate()'s falsy check the same way None did.
    SUPABASE_URL         = (os.getenv('SUPABASE_URL') or '').rstrip('/')
    SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
    SUPABASE_ANON_KEY    = os.getenv('SUPABASE_ANON_KEY')

    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY')
    
    # Audio validation
    ALLOWED_AUDIO_EXTENSIONS = {'.wav', '.mp3', '.webm', '.m4a', '.ogg', '.mp4'}
    ALLOWED_AUDIO_MIME_TYPES = {
        'audio/wav', 'audio/wave', 'audio/x-wav',
        'audio/mpeg', 'audio/mp3',
        'audio/webm', 'video/webm',
        'audio/mp4', 'video/mp4',
        'audio/x-m4a', 'audio/m4a',
        'audio/ogg', 'application/ogg',
        'application/octet-stream',  # some browsers send this for audio blobs
    }
    MAX_AUDIO_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB
    MIN_AUDIO_SIZE_BYTES = 1024               # 1 KB — reject empty or near-empty files
    
    # Language
    PRIMARY_LANGUAGE = 'es'  # Spanish
    
    @classmethod
    def validate(cls):
        """Validate that required configuration is present"""
        errors = []
        
        if not cls.ASSEMBLYAI_API_KEY:
            errors.append("ASSEMBLYAI_API_KEY is required")
        
        if not cls.GEMINI_API_KEY:
            errors.append("GEMINI_API_KEY is required")

        if not cls.SUPABASE_URL:
            errors.append("SUPABASE_URL is required")
        if not cls.SUPABASE_SERVICE_KEY:
            errors.append("SUPABASE_SERVICE_KEY is required")
        if not cls.SUPABASE_ANON_KEY:
            errors.append("SUPABASE_ANON_KEY is required")

        if not cls.SECRET_KEY:
            errors.append("FLASK_SECRET_KEY is required")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        return True
