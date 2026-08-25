import json
import urllib.request
import jwt
from functools import wraps
from flask import request, jsonify, g
from config import Config
from logger import logger
from pg_utils import pg_val

# Cached public key — fetched once from Supabase JWKS on first request
_jwks_public_key = None

def _get_public_key():
    """Fetch and cache the EC public key from Supabase's JWKS endpoint."""
    global _jwks_public_key
    if _jwks_public_key is not None:
        return _jwks_public_key
    jwks_url = f"{Config.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    with urllib.request.urlopen(jwks_url) as resp:
        jwks = json.loads(resp.read())
    _jwks_public_key = jwt.algorithms.ECAlgorithm.from_jwk(jwks["keys"][0])
    logger.info("Auth: JWKS public key loaded and cached")
    return _jwks_public_key


def verify_jwt(token: str) -> dict | None:
    """Validate a Supabase JWT (ES256) and return the decoded payload, or None if invalid/expired."""
    try:
        public_key = _get_public_key()
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["ES256"],
            audience="authenticated",
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


def get_usuario_context(user_id: str) -> dict | None:
    """
    Per-request DB lookup (not cached) — this is deliberate: it's what lets a
    deactivation take effect on the doctor's very next API call rather than
    waiting for their JWT to expire naturally (up to ~1h). Supabase's own ban
    mechanism only blocks future logins; it does not invalidate an
    already-issued token, so this check is the layer that actually cuts off
    an active session immediately.
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
