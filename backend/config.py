# backend/config.py
from email import errors
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
    
    # Google Docs
#    GOOGLE_CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), 'credentials.json')

    # Try to load from an environment variable first, then fallback to a file
    GOOGLE_CREDS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON') 
    
    # If you must keep a file path, make it flexible:
    GOOGLE_CREDENTIALS_PATH = os.getenv('GOOGLE_CREDENTIALS_PATH', 'credentials.json')
    
    # WHO ICD-11 API
    ICD_CLIENT_ID     = os.environ.get('ICD_CLIENT_ID', '')
    ICD_CLIENT_SECRET = os.environ.get('ICD_CLIENT_SECRET', '')

    # Resend email delivery
    RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
    RESEND_SENDER  = 'admin@clinianotes.com'

    # Supabase
    # SUPABASE_JWT_SECRET is no longer needed — JWT verification uses the JWKS endpoint (ES256)
    SUPABASE_URL         = os.getenv('SUPABASE_URL')
    SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')
    SUPABASE_ANON_KEY    = os.getenv('SUPABASE_ANON_KEY')

    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
    
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

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        return True
