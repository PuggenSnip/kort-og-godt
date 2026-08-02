"""Unit tests for the remember-me cookie logic (auth.py).

Pure logic — no Streamlit, no browser — so the security-critical parse/verify
path is fully exercised offline (the live cookie *behaviour* still needs the
deployed app, but the token/format correctness is pinned here).
"""
from __future__ import annotations

import base64
import urllib.parse

import auth


def test_roundtrip():
    c = auth.make_remember_cookie("hunter2", "Alice")
    assert auth.parse_remember_cookie(c, "hunter2") == "Alice"


def test_wrong_password_rejected():
    c = auth.make_remember_cookie("hunter2", "Alice")
    assert auth.parse_remember_cookie(c, "nope") is None


def test_tampered_token_rejected():
    c = auth.make_remember_cookie("hunter2", "Alice")
    ver, token, p = c.split(".", 2)
    forged = f"{ver}.{'0' * len(token)}.{p}"
    assert auth.parse_remember_cookie(forged, "hunter2") is None


def test_unicode_person_roundtrips():
    for name in ["Søren", "Bjørn Æble", "Åse-Mette Ø", "名前", "a b c"]:
        c = auth.make_remember_cookie("pw", name)
        assert auth.parse_remember_cookie(c, "pw") == name


def test_value_is_url_safe():
    # universal-cookie writes with encodeURIComponent; a URL-safe value must
    # survive that round-trip unchanged, or st.context.cookies would read back a
    # percent-encoded string we couldn't parse.
    c = auth.make_remember_cookie("p@ss w/rd!", "Bjørn+Æble/=?")
    assert all(ch.isalnum() or ch in "._-" for ch in c), c
    assert urllib.parse.quote(c, safe="._-") == c
    # and it still decodes to the original person
    assert auth.parse_remember_cookie(c, "p@ss w/rd!") == "Bjørn+Æble/=?"


def test_password_change_invalidates_old_cookie():
    c = auth.make_remember_cookie("old-password", "Alice")
    assert auth.parse_remember_cookie(c, "new-password") is None


def test_malformed_values_return_none():
    for bad in [None, "", "garbage", "1.only", "..", "1..", "2.tok.cGVyc29u",
                "1." + "a" * 64, "1.a.!!!notbase64!!!"]:
        assert auth.parse_remember_cookie(bad, "pw") is None


def test_missing_password_returns_none():
    c = auth.make_remember_cookie("pw", "Alice")
    assert auth.parse_remember_cookie(c, None) is None
    assert auth.parse_remember_cookie(c, "") is None


def test_plaintext_password_never_in_cookie():
    c = auth.make_remember_cookie("supersecret123", "Alice")
    assert "supersecret123" not in c


def test_token_is_deterministic_and_specific():
    assert auth.pw_token("a") == auth.pw_token("a")
    assert auth.pw_token("a") != auth.pw_token("b")
    # token is a 64-char sha256 hex digest
    assert len(auth.pw_token("a")) == 64
    int(auth.pw_token("a"), 16)  # hex-parseable


def test_empty_person_not_restored():
    c = auth.make_remember_cookie("pw", "")
    # an empty name is meaningless; parse must not resurrect a blank person
    assert auth.parse_remember_cookie(c, "pw") is None


def test_person_with_dots_roundtrips():
    # the format splits on '.' at most twice; base64url has no '.', but guard
    # against a name that (once encoded) still round-trips.
    name = "Dr. A.B. Smith Jr."
    c = auth.make_remember_cookie("pw", name)
    assert auth.parse_remember_cookie(c, "pw") == name
    # sanity: the encoded person segment carries no literal dots
    assert c.count(".") == 2
    b64 = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
    assert c.endswith("." + b64)
