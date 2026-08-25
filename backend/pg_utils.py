# backend/pg_utils.py
# Shared PostgREST query-encoding helper for app.py and auth.py.
# Standalone (no dependency on either module) to avoid a circular import —
# auth.py is imported BY app.py.

import urllib.parse


def pg_val(value) -> str:
    """
    URL-encode a single untrusted value before it is interpolated into a
    PostgREST filter (col=op.value) or a REST/Storage path segment.

    Wrap the VALUE only — never the operator (eq./ilike./gte./...), the
    column name, or a select=/order=/limit= clause the code itself builds.
    Those are query structure, not input; encoding them would corrupt a
    legitimate query rather than protect one.

    Without this, a value containing '&', '=', ',', or '%' can smuggle
    extra PostgREST query parameters (a select= embed, order, limit, an
    additional filter) into a service_role request — which bypasses RLS,
    so an injected select= embed could reach data across the whole schema
    (Stage M1 fix #16).
    """
    return urllib.parse.quote(str(value), safe='')


def pg_ilike_val(value) -> str:
    """
    Encode a value for use in an ILIKE/LIKE filter where the intent is an
    exact (case-insensitive) match, not a wildcard search. Escapes '%' and
    '_' (LIKE metacharacters — Postgres' default LIKE escape character is
    '\\') to their literal form before URL-encoding, so a value containing
    either character can't be (mis)interpreted as a wildcard — e.g. the
    admin_create_usuario duplicate-email check, where an email containing
    '%' must never accidentally match a DIFFERENT email as if it were a
    pattern (Stage M1 fix #16). Kept as ILIKE rather than switched to a
    plain eq — that would only be equivalent if every existing row were
    guaranteed already-lowercased, which isn't something this fix can
    verify against production data; escaping preserves the intended
    case-insensitivity without that assumption.
    """
    escaped = str(value).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return pg_val(escaped)


def pg_path(path: str) -> str:
    """
    URL-encode a Supabase Storage object path — like pg_val, but keeps '/'
    as a safe (structural) character, since a Storage path legitimately
    has multiple meaningful segments (e.g. 'logos/{clinica_id}/logo').
    Everything else in each segment gets percent-encoded.
    """
    return urllib.parse.quote(str(path), safe='/')
