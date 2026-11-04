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
SEARCH_ENDPOINT = f'https://id.who.int/icd/release/11/{Config.ICD11_RELEASE_VERSION}/mms/search'


# ── CIE-11 code format (0.8.5, CIE-11 review #13) ──────────────────────────
# From the ICD-11 Reference Guide (https://icdcdn.who.int/icd11referenceguide/en/refguide.pdf):
#   §1.2.4.1 Code structure — stem codes run "1A00.00 to ZZ9Z.ZZ", shape
#     ED1E.EE: E = base 34 (0-9, A-Z without O and I), D = base 24 (A-Z
#     without O and I), 1 = a digit; the first character is the chapter and
#     "Codes starting with 'X' indicate an extension code".
#   §2.9 Extension codes — never first in a cluster. Their length isn't
#     stated; the guide's examples are 4-6 characters (XK8G, XN6P4, XA7RE3).
#   §2.10.1-2.10.2 Clusters — "/" between stem codes, "&" between a stem
#     code and its extension codes, stem codes before extension codes
#     (e.g. 5A11/5A23, GB51&XK8G&XN6P4, DD51&XK8G/ME24.2&XT5R).
# The same pattern lives in static/app.js (CIE11_CODE_PATTERN); a test keeps
# the two identical. A non-matching code only warns — it is never rejected.
_B34 = '[0-9A-HJ-NP-Z]'
_B24 = '[A-HJ-NP-Z]'
_STEM = f'[1-9A-HJ-NP-WYZ]{_B24}[0-9]{_B34}(?:\\.{_B34}{{1,2}})?'
_EXT = f'X{_B34}{{3,5}}'
_GROUP = f'{_STEM}(?:&{_EXT})*'
CIE11_CODE_PATTERN = f'^{_GROUP}(?:/{_GROUP})*$'
_CIE11_RE = re.compile(CIE11_CODE_PATTERN)


def normalize_cie11_code(value) -> str:
    """Trim, uppercase and remove every internal space. Non-strings → ''."""
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', '', value).upper()


def is_valid_cie11_code(code: str) -> bool:
    """True for a stem code, or a stem/extension cluster, per the pattern above."""
    return bool(_CIE11_RE.match(code or ''))


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
            # No diagnosis text or resulting code/title logged — both are
            # PHI (a specific patient's health condition), even though the
            # code/title themselves look like generic medical vocabulary
            # (Stage H1 fix #11).
            logger.info("ICD: No results for diagnosis lookup")
            return None

        # 0.8.5 (CIE-11 review #6, #9): the highest-scoring entity that has
        # a code, not simply the first one — the API documents score as
        # "Higher numbers are better matches" but not that results are
        # sorted. Entities with an empty code (blocks, groupings) don't
        # count; if none has a code it's "no result": no code, no title.
        # Clusters ("5A24/5A11") count like any code. Ties keep API order.
        coded = [e for e in dest_entities
                 if isinstance(e, dict) and normalize_cie11_code(e.get('theCode'))]
        if not coded:
            logger.info("ICD: No coded result for diagnosis lookup")
            return None

        def _score(entity):
            try:
                return float(entity.get('score') or 0)
            except (TypeError, ValueError):
                return 0.0

        top = max(coded, key=_score)
        code = normalize_cie11_code(top.get('theCode'))
        title = top.get('title') or ''
        if not isinstance(title, str):
            title = ''

        # Strip HTML tags the API sometimes includes in title
        title = re.sub(r'<[^>]+>', '', title).strip()

        logger.info("ICD: lookup succeeded")
        return {'code': code, 'title': title}

    except Exception as e:
        # Never log str(e): requests puts the full request URL in HTTPError
        # and ConnectionError messages ("... for url: ...?q=<diagnóstico>",
        # "Max retries exceeded with url: ...?q=..."), and the query is the
        # patient's diagnosis — PHI. Status code and exception type only.
        response = getattr(e, 'response', None)
        status = getattr(response, 'status_code', None)
        if status is not None:
            logger.error(f"ICD: Lookup failed: HTTP {status}")
        else:
            logger.error(f"ICD: Lookup failed: {type(e).__name__}")
        return None
