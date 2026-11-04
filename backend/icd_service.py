# backend/icd_service.py
# WHO ICD-11 API integration — token management and CIE-11 code lookup

import re
import threading
import time

import requests
from config import Config
from logger import logger

# Module-level token cache. expires_at is the token's; failed_until/failure
# are the short failure cache (0.8.6, CIE-11 review #17).
_token_cache = {'token': None, 'expires_at': 0, 'failed_until': 0, 'failure': None}
# One refresh at a time: the job pool has 4 workers, and without this each
# of them fetched its own token when the cached one expired.
_token_lock = threading.Lock()

TOKEN_ENDPOINT = 'https://icdaccessmanagement.who.int/connect/token'

# After a token failure or an unreachable/timed-out WHO endpoint, skip the
# lookup for this long instead of waiting 10-20 s again on every job.
FAILURE_CACHE_SECONDS = 60

# ── Release (0.8.6, CIE-11 review #12) ─────────────────────────────────────
# ICD11_RELEASE_VERSION names an MMS release such as "2026-01" (the releases
# WHO serves are listed at https://icd.who.int/docs/icd-api/SupportedClassifications/).
# Checked once at startup: an empty or malformed value disables the lookup
# with one clear error, instead of a failed request on every job.
_RELEASE_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
SEARCH_ENDPOINT = None


def configure_release(value) -> bool:
    """Validate the release and build SEARCH_ENDPOINT from it. Returns whether
    the lookup is enabled. Run at import; tests call it directly."""
    global SEARCH_ENDPOINT
    release = value.strip() if isinstance(value, str) else ''
    if not _RELEASE_RE.match(release):
        SEARCH_ENDPOINT = None
        # The value is configuration, not PHI; shown (shortened) so the
        # typo is visible in the log.
        logger.error(
            f"ICD: ICD11_RELEASE_VERSION {release[:20]!r} is empty or not YYYY-MM "
            f"(e.g. 2026-01) — CIE-11 lookup disabled until it is fixed"
        )
        return False
    SEARCH_ENDPOINT = f'https://id.who.int/icd/release/11/{release}/mms/search'
    return True


configure_release(Config.ICD11_RELEASE_VERSION)


# ── Outcome log (0.8.6) ────────────────────────────────────────────────────
# One line per lookup, enums and numbers only — never the URL, the query
# (the diagnosis), the code or the title (all PHI):
#   ICD: outcome=<ok|no_results|no_code|http_NNN|timeout|token_failed|
#                 not_configured|bad_json|no_diagnosis> cluster=<0|1> ms=<n>
# timeout also covers an unreachable endpoint (any connection error).
def log_lookup_outcome(outcome: str, cluster: bool = False, ms: int = 0) -> None:
    line = f"ICD: outcome={outcome} cluster={1 if cluster else 0} ms={int(ms)}"
    if outcome in ('ok', 'no_results', 'no_code', 'no_diagnosis'):
        logger.info(line)
    else:
        logger.warning(line)


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


def _remember_failure(outcome: str) -> None:
    _token_cache['failed_until'] = time.time() + FAILURE_CACHE_SECONDS
    _token_cache['failure'] = outcome


def _cached_failure() -> str | None:
    """The outcome of a failure still inside its cache window, else None."""
    if _token_cache['failure'] and time.time() < _token_cache['failed_until']:
        return _token_cache['failure']
    return None


def _get_token() -> str | None:
    """Get a valid WHO OAuth token, refreshing if expired. Thread-safe: one
    refresh at a time, and the others reuse its result (or its failure)."""
    with _token_lock:
        now = time.time()
        if _token_cache['token'] and now < _token_cache['expires_at'] - 60:
            return _token_cache['token']
        if _cached_failure():
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
            _token_cache['failure'] = None
            logger.info("ICD: Token obtained successfully")
            return _token_cache['token']
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            logger.error(f"ICD: Failed to obtain token: {f'HTTP {status}' if status else type(e).__name__}")
            _remember_failure('token_failed')
            return None


def lookup_cie11(diagnosis_text: str) -> dict | None:
    """
    Search the WHO ICD-11 MMS API for the best matching code.

    Args:
        diagnosis_text: Free-text diagnosis in Spanish (from Gemini extraction)

    Returns:
        dict with 'code' and 'title' of the best coded result, or None.
        Every call logs exactly one outcome line (log_lookup_outcome).
    """
    started = time.monotonic()
    elapsed = lambda: (time.monotonic() - started) * 1000

    if not isinstance(diagnosis_text, str) or not diagnosis_text.strip():
        log_lookup_outcome('no_diagnosis')
        return None
    if not SEARCH_ENDPOINT or not Config.ICD_CLIENT_ID or not Config.ICD_CLIENT_SECRET:
        log_lookup_outcome('not_configured')
        return None

    cached = _cached_failure()
    if cached:
        log_lookup_outcome(cached, ms=elapsed())
        return None

    token = _get_token()
    if not token:
        log_lookup_outcome(_cached_failure() or 'token_failed', ms=elapsed())
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
    except requests.HTTPError as e:
        # Never str(e): requests puts the full URL, q=<diagnóstico>
        # included, in HTTPError and ConnectionError messages.
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        log_lookup_outcome(f'http_{status}' if status else 'timeout', ms=elapsed())
        return None
    except requests.RequestException:
        # Timeout or unreachable: cache it so the next jobs don't each wait.
        with _token_lock:
            _remember_failure('timeout')
        log_lookup_outcome('timeout', ms=elapsed())
        return None

    try:
        results = resp.json()
        dest_entities = results.get('destinationEntities', [])
        if not isinstance(dest_entities, list):
            raise ValueError('destinationEntities is not a list')
    except Exception:
        log_lookup_outcome('bad_json', ms=elapsed())
        return None

    if not dest_entities:
        log_lookup_outcome('no_results', ms=elapsed())
        return None

    # 0.8.5 (CIE-11 review #6, #9): the highest-scoring entity that has a
    # code, not simply the first one — the API documents score as "Higher
    # numbers are better matches" but not that results are sorted. Entities
    # with an empty code (blocks, groupings) don't count; if none has a code
    # it's "no result": no code, no title. Clusters ("5A24/5A11") count like
    # any code. Ties keep API order.
    coded = [e for e in dest_entities
             if isinstance(e, dict) and normalize_cie11_code(e.get('theCode'))]
    if not coded:
        log_lookup_outcome('no_code', ms=elapsed())
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

    log_lookup_outcome('ok', cluster=('/' in code or '&' in code), ms=elapsed())
    return {'code': code, 'title': title}
