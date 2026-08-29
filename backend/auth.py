import json
import threading
import time
import urllib.request
import jwt
from functools import wraps
from flask import request, jsonify, g
from config import Config
from logger import logger
from pg_utils import pg_val

# ─────────────────────────────────────────────
# JWKS cache — bounded TTL, kid-matched, debounced refresh, thread-safe.
# A rotation at Supabase publishes a new key under a new kid; the old
# key stays in the set for a transition window. _get_signing_key looks
# up by kid (never blindly keys[0]) and refreshes on a cache miss or
# TTL expiry, but at most once per _JWKS_REFRESH_DEBOUNCE_SECONDS
# regardless of trigger or outcome — so a burst of tokens carrying
# random/bogus kids, or a JWKS endpoint that's down, can't turn this
# cache into an amplification vector against Supabase.
# ─────────────────────────────────────────────
_jwks_lock = threading.Lock()
_jwks_keys_by_kid: dict = {}
_jwks_fetched_at: float = 0.0        # monotonic time of last successful fetch (0 = never)
_jwks_last_attempt_at: float = 0.0   # monotonic time of last fetch attempt, success or failure (0 = never)

_JWKS_TTL_SECONDS = 3600             # normal cache lifetime
_JWKS_REFRESH_DEBOUNCE_SECONDS = 30  # min gap between forced refetches, any trigger
_JWKS_FETCH_TIMEOUT_SECONDS = 5

_EXPECTED_ISSUER = f"{Config.SUPABASE_URL}/auth/v1"


def _fetch_jwks() -> dict:
    """Fetch the raw JWKS document from Supabase. Raises on network error/timeout."""
    jwks_url = f"{Config.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    req = urllib.request.Request(jwks_url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=_JWKS_FETCH_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _refresh_jwks_locked(now: float) -> None:
    """Must be called while holding _jwks_lock. Fetches and rebuilds the kid->key map."""
    global _jwks_keys_by_kid, _jwks_fetched_at, _jwks_last_attempt_at
    _jwks_last_attempt_at = now
    jwks = _fetch_jwks()
    keys_by_kid = {}
    for jwk in jwks.get('keys', []):
        kid = jwk.get('kid')
        if not kid:
            continue
        try:
            keys_by_kid[kid] = jwt.algorithms.ECAlgorithm.from_jwk(jwk)
        except Exception as e:
            logger.warning(f"Auth: JWKS: could not parse key kid={kid}: {e}")
    _jwks_keys_by_kid = keys_by_kid
    _jwks_fetched_at = now
    logger.info(f"Auth: JWKS refreshed — {len(keys_by_kid)} key(s) cached")


def _get_signing_key(kid: str):
    """
    Return the EC public key matching `kid`, refreshing the cache as needed.
    Thread-safe: the whole check-and-refresh sequence runs under
    _jwks_lock, so concurrent requests can't trigger a thundering herd
    of fetches during a rotation — one thread refreshes, the rest see
    the fresh cache once they acquire the lock. Fails closed: returns
    None (never raises) if the key can't be resolved; callers must
    reject the token when this returns None.
    """
    now = time.monotonic()
    with _jwks_lock:
        key = _jwks_keys_by_kid.get(kid)
        stale = (now - _jwks_fetched_at) > _JWKS_TTL_SECONDS

        if key is None or stale:
            never_attempted = _jwks_last_attempt_at == 0.0
            debounced = (not never_attempted) and (now - _jwks_last_attempt_at) < _JWKS_REFRESH_DEBOUNCE_SECONDS
            if not debounced:
                try:
                    _refresh_jwks_locked(now)
                    key = _jwks_keys_by_kid.get(kid)
                except Exception as e:
                    logger.warning(f"Auth: JWKS fetch failed: {e}")
                    key = _jwks_keys_by_kid.get(kid)  # whatever's still cached, if anything

        return key


def verify_jwt(token: str) -> dict | None:
    """Validate a Supabase JWT (ES256) and return the decoded payload, or None if invalid/expired."""
    try:
        try:
            unverified_header = jwt.get_unverified_header(token)
        except Exception as e:
            logger.warning(f"Auth: JWT malformed header — {e}")
            return None

        kid = unverified_header.get("kid")
        if not kid:
            logger.warning("Auth: JWT missing kid in header")
            return None

        public_key = _get_signing_key(kid)
        if public_key is None:
            logger.warning(f"Auth: JWKS: no signing key for kid={kid}")
            return None

        payload = jwt.decode(
            token,
            public_key,
            algorithms=["ES256"],
            audience="authenticated",
            issuer=_EXPECTED_ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Auth: JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Auth: JWT invalid — {e}")
        return None
    except Exception as e:
        logger.warning(f"Auth: JWT verification error — {e}")
        return None


class UsuarioInactivoError(Exception):
    """Raised by get_usuario_context when the usuario row exists but activo=false.
    Distinguished from a plain None return (not-found) so require_auth can map
    this to 401 specifically, not the generic 403 used for "no usuarios row"."""
    pass


def _fetch_usuario_context(user_id: str) -> dict | None:
    """
    Uncached Supabase lookup — the raw DB call behind get_usuario_context's
    TTL cache below. Not called directly by anything outside this module.
    """
    import urllib.error
    url = (
        Config.SUPABASE_URL.rstrip('/')
        + '/rest/v1/usuarios'
        + f'?id=eq.{pg_val(user_id)}'
        + '&select=id,clinica_id,rol,nombre,email,activo'
        + '&limit=1'
    )
    req = urllib.request.Request(url, headers={
        'apikey': Config.SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {Config.SUPABASE_SERVICE_KEY}',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
        if not rows:
            return None
        row = rows[0]
        if row.get('activo') is False:
            raise UsuarioInactivoError()
        return {
            'usuario_id': row['id'],
            'clinica_id': row['clinica_id'],
            'rol':        row['rol'],
            'nombre':     row['nombre'],
            'email':      row['email'],
        }
    except UsuarioInactivoError:
        raise
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        logger.warning(f'Auth: Supabase REST error {e.code} for {user_id}: {body}')
        return None
    except Exception as e:
        logger.warning(f'Auth: Could not fetch usuario context for {user_id}: {e}')
        return None


# ─────────────────────────────────────────────
# usuario_context cache — short TTL, thread-safe (Stage E2 fix #32).
# get_usuario_context runs on every @require_auth'd request; this was
# deliberately uncached so a deactivation/ban takes effect on the
# doctor's very next request rather than waiting for their JWT to
# expire naturally (up to ~1h) — Supabase's own ban only blocks future
# logins, it doesn't invalidate an already-issued token. The TTL alone
# would only bound that to ≤_USUARIO_CACHE_TTL_SECONDS — invalidate_
# usuario_cache() below closes it back to immediate for the one place
# that actually flips usuarios.activo (admin_set_usuario_activo in
# app.py), same invalidate-on-change pattern as fix #37's logo cache.
# The TTL still matters as a backstop for any future path that changes
# a cached field without going through that invalidation call. Per-
# process, single-instance assumption — same caveat class as the JWKS
# cache above, Stage M3's memory:// rate limiter, and Stage M4's
# reaper: correct only while the app runs one gunicorn worker/instance
# (confirmed in M2).
# ─────────────────────────────────────────────
_usuario_cache_lock = threading.Lock()
_usuario_cache: dict = {}  # user_id -> (expires_at_monotonic, outcome)
_USUARIO_CACHE_TTL_SECONDS = 20

_OUTCOME_INACTIVE = object()   # sentinel: replay UsuarioInactivoError on hit
_OUTCOME_NOT_FOUND = object()  # sentinel: replay a None return on hit


def get_usuario_context(user_id: str) -> dict | None:
    """
    TTL-cached wrapper around _fetch_usuario_context (see that docstring
    for the security-property tradeoff this cache introduces). Replays
    whichever of the three outcomes — a context dict, UsuarioInactivoError,
    or None (not found) — was last observed for this user_id, for up to
    _USUARIO_CACHE_TTL_SECONDS, instead of hitting Supabase every time.

    The lock guards only the cache dict itself, not the network call: a
    cache miss/expiry fetches OUTSIDE the lock, so one user's DB round
    trip never blocks every other user's cache read. The tradeoff is a
    small, harmless window where concurrent requests for the SAME
    user_id during a miss can each trigger their own redundant fetch —
    far cheaper than serializing all users through one global lock.
    """
    now = time.monotonic()
    with _usuario_cache_lock:
        cached = _usuario_cache.get(user_id)
    if cached is not None:
        expires_at, outcome = cached
        if now < expires_at:
            if outcome is _OUTCOME_INACTIVE:
                raise UsuarioInactivoError()
            if outcome is _OUTCOME_NOT_FOUND:
                return None
            return outcome

    try:
        context = _fetch_usuario_context(user_id)
    except UsuarioInactivoError:
        with _usuario_cache_lock:
            _usuario_cache[user_id] = (time.monotonic() + _USUARIO_CACHE_TTL_SECONDS, _OUTCOME_INACTIVE)
        raise

    outcome = context if context is not None else _OUTCOME_NOT_FOUND
    with _usuario_cache_lock:
        _usuario_cache[user_id] = (time.monotonic() + _USUARIO_CACHE_TTL_SECONDS, outcome)
    return context


def invalidate_usuario_cache(user_id: str) -> None:
    """
    Evict a cached usuario_context entry immediately (Stage E2 follow-up
    to fix #32). The TTL cache above means a deactivation/ban would
    otherwise take up to _USUARIO_CACHE_TTL_SECONDS to actually lock
    the doctor out — this closes that window to zero for the one place
    that actually flips usuarios.activo (admin_set_usuario_activo in
    app.py), the same invalidate-on-change pattern fix #37 already
    established for the clinic-logo cache. Best-effort/idempotent: a
    miss (nothing cached yet, or already expired) is a silent no-op.
    """
    with _usuario_cache_lock:
        _usuario_cache.pop(user_id, None)


def require_auth(f):
    """Decorator that validates the Bearer JWT and attaches flask.g.usuario."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "No autorizado"}), 401

        token = auth_header.removeprefix("Bearer ").strip()
        payload = verify_jwt(token)
        if payload is None:
            return jsonify({"error": "No autorizado"}), 401

        user_id = payload.get("sub")
        try:
            usuario = get_usuario_context(user_id)
        except UsuarioInactivoError:
            return jsonify({"error": "No autorizado"}), 401

        if usuario is None:
            return jsonify({"error": "Usuario no registrado en ninguna clínica"}), 403

        g.usuario = usuario
        return f(*args, **kwargs)

    return decorated


def require_admin(f):
    """Decorator that gates on g.usuario['rol'] == 'admin'. Stack after @require_auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if g.usuario.get('rol') != 'admin':
            return jsonify({"error": "No autorizado"}), 403
        return f(*args, **kwargs)

    return decorated
