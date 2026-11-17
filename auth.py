"""Shared-password authentication for the hosted app.

Single password in ``APP_PASSWORD``, exchanged for a signed session cookie.
Everything except the login endpoints and static assets requires it.

This is deliberately simple. It gives you *access control*, not *identity* —
every logged-in user looks the same, so the audit trail cannot say who
approved a Lexicon write. If you need that, swap this for OAuth.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from hashlib import sha256

COOKIE_NAME = "governance_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours

# Signing key for session cookies. Set SESSION_SECRET in production, or every
# redeploy logs everyone out (a random key is generated per process).
SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

PASSWORD = os.getenv("APP_PASSWORD", "")

PUBLIC_PATHS = frozenset({"/login", "/api/login", "/healthz"})


def auth_required() -> bool:
    """Auth is on whenever a password is configured."""
    return bool(PASSWORD)


def check_password(candidate: str) -> bool:
    """Constant-time comparison, so timing cannot leak the password."""
    if not PASSWORD:
        return True
    return hmac.compare_digest(candidate.encode(), PASSWORD.encode())


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), sha256).hexdigest()


def issue_token() -> str:
    """A signed cookie value: <session_id>.<expiry>.<signature>."""
    session_id = secrets.token_urlsafe(18)
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{session_id}.{expiry}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> str | None:
    """Return the session id if the token is valid and unexpired, else None."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    session_id, expiry, signature = parts
    payload = f"{session_id}.{expiry}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        if int(expiry) < time.time():
            return None
    except ValueError:
        return None
    return session_id
