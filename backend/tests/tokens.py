# backend/tests/tokens.py
# A fake token scheme standing in for real Supabase JWTs. auth.verify_jwt
# is monkeypatched (see conftest.py) to fake_verify_jwt below instead of
# doing real ES256/JWKS verification — this file is the only place that
# knows the fake format, so require_auth's own logic (header parsing,
# the None-check, everything after verify_jwt returns) is exercised for
# real against these tokens.

VALID_PREFIX = 'valid:'
EXPIRED = 'expired-token'
BAD_SIGNATURE = 'bad-signature-token'
MALFORMED = 'not-a-jwt-at-all'


def mint(user_id: str) -> str:
    """A token string that fake_verify_jwt will accept as issued to user_id."""
    return f'{VALID_PREFIX}{user_id}'


def fake_verify_jwt(token: str) -> dict | None:
    """Stand-in for auth.verify_jwt. Mirrors its real contract: return a
    payload dict with 'sub' on success, None on any failure — the same
    signature require_auth already expects, so its own code path (not
    this fake) is what the tests actually exercise."""
    if token.startswith(VALID_PREFIX):
        return {'sub': token[len(VALID_PREFIX):]}
    return None  # EXPIRED, BAD_SIGNATURE, MALFORMED, or anything unrecognized


def auth_headers(token: str | None) -> dict:
    """Build the Authorization header dict for a test client request.
    Pass None for 'no token sent at all'."""
    if token is None:
        return {}
    return {'Authorization': f'Bearer {token}'}
