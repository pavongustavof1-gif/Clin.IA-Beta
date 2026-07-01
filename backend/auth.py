import json
import urllib.request
import jwt
from functools import wraps
from flask import request, jsonify, g
from config import Config
from supabase_client import get_supabase
from logger import logger

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


def get_usuario_context(user_id: str) -> dict | None:
    import urllib.error
    url = (
        Config.SUPABASE_URL.rstrip('/')
        + '/rest/v1/usuarios'
        + f'?id=eq.{user_id}'
        + '&select=id,clinica_id,rol,nombre,email'
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
        return {
            'usuario_id': row['id'],
            'clinica_id': row['clinica_id'],
            'rol':        row['rol'],
            'nombre':     row['nombre'],
            'email':      row['email'],
        }
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
        usuario = get_usuario_context(user_id)
        if usuario is None:
            return jsonify({"error": "Usuario no registrado en ninguna clínica"}), 403

        g.usuario = usuario
        return f(*args, **kwargs)

    return decorated
