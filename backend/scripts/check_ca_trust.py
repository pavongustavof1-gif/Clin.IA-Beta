# backend/scripts/check_ca_trust.py
# Roadmap #82 (eMudhra CA compromise, Render advisory 23 Oct 2026):
# confirms that the running service distrusts the compromised eMudhra
# intermediates in BOTH trust stores the backend uses —
#   - system store (OpenSSL default paths, Debian ca-certificates):
#     urllib.request → every Supabase REST/Storage/Auth/JWKS call
#   - certifi: requests → ICD-11, Resend, Gemini; httpx → AssemblyAI
#
# MAKES REAL NETWORK CALLS. Run it by hand in the Render Shell, from /app:
#     python scripts/check_ca_trust.py
# Never collected by pytest (lives outside tests/ and isn't named test_*).
#
# Standalone on purpose: imports no backend module (importing app.py would
# start the job reaper and require every env var). Standard library plus
# requests, httpx and certifi only. Reads no secrets and prints only whether
# the CA-override env vars are set, never their values.
#
# Exit 0 = ALL PASS; 1 = NOT OK; 2 = NOT OK and a positive control failed
# (no outbound network, so the eMudhra results can't be trusted either way).

import os
import platform
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 10
CONTROL_URL = 'https://www.google.com/'
TEST_URLS = [
    'https://dv-g3b-emudhra.we-have-a-problem.com/',
    'https://dv-g3b-aaa-emudhra.we-have-a-problem.com/',
]
CA_ENV_VARS = ['SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE']

try:
    import certifi
except ImportError:
    certifi = None
try:
    import requests
except ImportError:
    requests = None
try:
    import httpx
except ImportError:
    httpx = None


# ── Environment ──────────────────────────────────────────────────────────

def print_environment():
    print(f'Python              {platform.python_version()}')
    try:
        with open('/etc/debian_version') as f:
            print(f'Debian              {f.read().strip()}')
    except OSError:
        print('Debian              (no /etc/debian_version)')
    print(f'OpenSSL             {ssl.OPENSSL_VERSION}')
    if certifi is not None:
        print(f'certifi             {certifi.__version__}  {certifi.where()}')
    else:
        print('certifi             NOT IMPORTABLE')
    print(f'System verify       {ssl.get_default_verify_paths()}')
    for name in CA_ENV_VARS:
        print(f'{name:<20}{"set" if os.environ.get(name) else "not set"}')
    print()


# ── One request per client ───────────────────────────────────────────────
# Each returns ('connected', detail) when a TLS session was established and
# an HTTP response came back (any status), ('cert', detail) on a
# certificate-verification failure, or ('other', detail) for anything else.

def _has_cert_verify_error(exc) -> bool:
    """Walk an exception's cause/context/args chain for SSLCertVerificationError.

    requests wraps it several layers deep (SSLError → MaxRetryError →
    urllib3 SSLError → SSLCertVerificationError), so check the chain rather
    than one fixed attribute.
    """
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        if isinstance(e, BaseException):
            stack.extend([e.__cause__, e.__context__, getattr(e, 'reason', None)])
            stack.extend(a for a in e.args if isinstance(a, BaseException))
    return 'CERTIFICATE_VERIFY_FAILED' in str(exc)


def _short(exc) -> str:
    text = f'{type(exc).__name__}: {exc}'
    return text if len(text) <= 160 else text[:157] + '...'


def fetch_urllib(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            return 'connected', f'HTTP {resp.status}'
    except urllib.error.HTTPError as e:   # subclass of URLError: a response came back
        return 'connected', f'HTTP {e.code}'
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            return 'cert', _short(e.reason)
        return 'other', _short(e)
    except Exception as e:
        return 'other', _short(e)


def fetch_requests(url):
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False)
        return 'connected', f'HTTP {resp.status_code}'
    except requests.exceptions.SSLError as e:
        if _has_cert_verify_error(e):
            return 'cert', 'SSLError: certificate verify failed'
        return 'other', _short(e)
    except Exception as e:
        return 'other', _short(e)


def fetch_httpx(url):
    try:
        resp = httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        return 'connected', f'HTTP {resp.status_code}'
    except httpx.ConnectError as e:
        if 'CERTIFICATE_VERIFY_FAILED' in str(e):
            return 'cert', 'ConnectError: CERTIFICATE_VERIFY_FAILED'
        return 'other', _short(e)
    except Exception as e:
        return 'other', _short(e)


CLIENTS = [
    ('urllib', 'system store', fetch_urllib, True),
    ('requests', 'certifi', fetch_requests, requests is not None),
    ('httpx', 'certifi', fetch_httpx, httpx is not None),
]


# ── Checks ───────────────────────────────────────────────────────────────

def line(client, url, verdict, reason):
    print(f'{client:<9} {url:<50} {verdict:<13} {reason}')


def main() -> int:
    print_environment()
    any_fail = False
    any_inconclusive = False
    control_failed = False

    for name, store, fetch, importable in CLIENTS:
        if not importable:
            for url in [CONTROL_URL] + TEST_URLS:
                line(name, url, 'FAIL', f'{name} not importable')
            any_fail = True
            continue

        # Positive control: proves this client can reach the internet at all,
        # so a failure on the eMudhra URLs below means distrust, not no network.
        outcome, detail = fetch(CONTROL_URL)
        if outcome == 'connected':
            line(name, CONTROL_URL, 'OK', f'control reached ({detail}, {store})')
        else:
            line(name, CONTROL_URL, 'INCONCLUSIVE', f'control failed (network): {detail}')
            control_failed = True

        for url in TEST_URLS:
            outcome, detail = fetch(url)
            if outcome == 'cert':
                line(name, url, 'PASS', detail)
            elif outcome == 'connected':
                line(name, url, 'FAIL', f'connected ({detail}) — {store} still trusts eMudhra')
                any_fail = True
            else:
                line(name, url, 'INCONCLUSIVE', detail)
                any_inconclusive = True

    print()
    if control_failed:
        print('NOT OK')
        return 2
    if any_fail or any_inconclusive:
        print('NOT OK')
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
