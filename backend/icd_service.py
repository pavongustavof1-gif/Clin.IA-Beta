# backend/icd_service.py
# WHO ICD-11 API integration — token management and CIE-11 code lookup

import re
import requests
import time
from config import Config
from logger import logger

# Module-level token cache
_token_cache = {'token': None, 'expires_at': 0}

TOKEN_ENDPOINT  = 'https://icdaccessmanagement.who.int/connect/token'
SEARCH_ENDPOINT = 'https://id.who.int/icd/release/11/2026-01/mms/search'


def _get_token() -> str | None:
    """Get a valid WHO OAuth token, refreshing if expired."""
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expires_at'] - 60:
        return _token_cache['token']

    if not Config.ICD_CLIENT_ID or not Config.ICD_CLIENT_SECRET:
        logger.warning("ICD: ICD_CLIENT_ID or ICD_CLIENT_SECRET not configured — skipping CIE-11 lookup")
        return None

    try:
        resp = requests.post(
            TOKEN_ENDPOINT,
            data={
                'grant_type': 'client_credentials',
                'scope': 'icdapi_access',
                'client_id': Config.ICD_CLIENT_ID,
                'client_secret': Config.ICD_CLIENT_SECRET,
            },
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache['token'] = data['access_token']
        _token_cache['expires_at'] = now + data.get('expires_in', 3600)
        logger.info("ICD: Token obtained successfully")
        return _token_cache['token']
    except Exception as e:
        logger.error(f"ICD: Failed to obtain token: {e}")
        return None


def lookup_cie11(diagnosis_text: str) -> dict | None:
    """
    Search the WHO ICD-11 MMS API for the best matching code.

    Args:
        diagnosis_text: Free-text diagnosis in Spanish (from Gemini extraction)

    Returns:
        dict with 'code' and 'title' of the top result, or None if lookup fails
    """
    if not diagnosis_text or not diagnosis_text.strip():
        return None

    token = _get_token()
    if not token:
        return None

    try:
        resp = requests.get(
            SEARCH_ENDPOINT,
            params={'q': diagnosis_text, 'flatResults': 'true', 'highlightingEnabled': 'false'},
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
                'Accept-Language': 'es',
                'API-Version': 'v2',
            },
            timeout=10
        )
        resp.raise_for_status()
        results = resp.json()

        dest_entities = results.get('destinationEntities', [])
        if not dest_entities:
            logger.info(f"ICD: No results for '{diagnosis_text}'")
            return None

        top = dest_entities[0]
        code = top.get('theCode', '')
        title = top.get('title', '')

        # Strip HTML tags the API sometimes includes in title
        title = re.sub(r'<[^>]+>', '', title).strip()

        logger.info(f"ICD: '{diagnosis_text}' → {code} ({title})")
        return {'code': code, 'title': title}

    except Exception as e:
        logger.error(f"ICD: Lookup failed for '{diagnosis_text}': {e}")
        return None
