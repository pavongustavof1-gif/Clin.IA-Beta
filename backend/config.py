# backend/config.py
import os
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
    
    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # Audio constraints (Alpha version)
    MAX_AUDIO_DURATION_SECONDS = 300  # 5 minutes
    ALLOWED_AUDIO_FORMATS = ['wav', 'mp3', 'webm', 'ogg', 'm4a']
    
    # Language
    PRIMARY_LANGUAGE = 'es'  # Spanish
    
    @classmethod
    def validate(cls):
        """Validate that required configuration is present"""
        errors = []
        
        if not cls.ASSEMBLYAI_API_KEY:
            errors.append("ASSEMBLYAI_API_KEY is required")
        
        if not cls.GEMINI_API_KEY and not cls.OPENAI_API_KEY:
            errors.append("Either GEMINI_API_KEY or OPENAI_API_KEY is required")
        
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        return True
