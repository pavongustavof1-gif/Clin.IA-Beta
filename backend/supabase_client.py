from supabase import create_client, Client, ClientOptions
from config import Config
from logger import logger

_client: Client | None = None

def get_supabase() -> Client:
    global _client
    if _client is None:
        logger.info(f"Supabase: initializing with URL={Config.SUPABASE_URL} KEY={Config.SUPABASE_SERVICE_KEY[:20] if Config.SUPABASE_SERVICE_KEY else 'MISSING'}...")
        _client = create_client(
            Config.SUPABASE_URL,
            Config.SUPABASE_SERVICE_KEY,
            options=ClientOptions(
                headers={
                    "apikey": Config.SUPABASE_SERVICE_KEY,
                }
            )
        )
    return _client
