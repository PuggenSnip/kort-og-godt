"""Remember-me cookie helpers for Kort og Godt (pure logic, no Streamlit).

The shared-password deployment can persist a login across browser sessions by
storing a cookie that proves knowledge of ``APP_PASSWORD`` *without* holding the
plaintext. The cookie value is deliberately URL-safe so it survives
universal-cookie's ``encodeURIComponent`` write/read round-trip untouched::

    "1.<sha256-hex token>.<base64url person, unpadded>"

* ``token`` = ``sha256("<ns>:" + APP_PASSWORD)`` — one-way, so the password is
  never in the cookie; if the password is later changed, every old cookie is
  invalidated automatically (the token no longer matches).
* Stealing the cookie grants access much like stealing a live session cookie —
  an accepted risk for this small private group, not a public service.

Kept free of any Streamlit import so the security-critical logic is unit-tested
without a browser or a running app.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Optional

COOKIE_NAME = "kog_remember"
REMEMBER_DAYS = 30
_NS = "kog-remember-v1"     # namespace/salt: scopes the token to this feature


def pw_token(password: str) -> str:
    """A non-reversible token that proves knowledge of ``password``."""
    return hashlib.sha256(f"{_NS}:{password}".encode("utf-8")).hexdigest()


def make_remember_cookie(password: str, person: str) -> str:
    """Build the cookie value binding this password to this person name."""
    p = base64.urlsafe_b64encode(person.encode("utf-8")).decode("ascii").rstrip("=")
    return f"1.{pw_token(password)}.{p}"


def parse_remember_cookie(raw, password) -> Optional[str]:
    """Return the remembered person name iff ``raw`` is a valid cookie for
    ``password``; otherwise ``None``. Never raises — any malformed / tampered /
    mismatched value simply means "not logged in"."""
    if not raw or not password:
        return None
    try:
        ver, token, p = raw.split(".", 2)
        if ver != "1":
            return None
        if not hmac.compare_digest(token, pw_token(password)):
            return None
        pad = "=" * (-len(p) % 4)
        person = base64.urlsafe_b64decode(p + pad).decode("utf-8")
        return person or None
    except Exception:       # noqa: BLE001 — any parse failure = not logged in
        return None
