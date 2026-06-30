import jwt
from functools import wraps
from flask import request, jsonify, g
from config import Config
from supabase_client import get_supabase
from logger import logger


def verify_jwt(token: str) -> dict | None:
    """Validate a Supabase JWT and return the decoded payload, or None if invalid/expired."""
    try:
        payload = jwt.decode(
            token,
            Config.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Auth: JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Auth: JWT invalid — {e}")
        return None


def get_usuario_context(user_id: str) -> dict | None:
    """
    Query the usuarios table for the given auth.users UUID.
    Returns {usuario_id, clinica_id, rol, nombre, email} or None if not found.
    """
    try:
        result = (
            get_supabase()
            .table("usuarios")
            .select("id, clinica_id, rol, nombre, email")
            .eq("id", user_id)
            .single()
            .execute()
        )
        row = result.data
        if not row:
            return None
        return {
            "usuario_id": row["id"],
            "clinica_id": row["clinica_id"],
            "rol":        row["rol"],
            "nombre":     row["nombre"],
            "email":      row["email"],
        }
    except Exception as e:
        logger.warning(f"Auth: Could not fetch usuario context for {user_id}: {e}")
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
