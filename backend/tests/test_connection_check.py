"""test_connection_check.py — onboarding must not green-light a broken setup.

Saving config never touched Tunarr or Plex, so a typo'd URL or an expired token
produced a "Setup complete" toast and the user discovered the truth several steps
later. /test-connection validates what was typed, before anything is written.

Also pins the probe() semantics fix: an HTTP error is not success. probe() used to
return ok:True for ANY response including 401, which reports an auth-protected
Tunarr as healthy.
"""

import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
from routers import status_router as sr


class FakeResp:
    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def urlopen_returning(status=200):
    def _f(req, timeout=None):
        return FakeResp(status)
    return _f


def urlopen_raising(exc):
    def _f(req, timeout=None):
        raise exc
    return _f


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


@pytest.fixture(autouse=True)
def reset_auth():
    channel_engine.set_tunarr_auth()
    yield
    channel_engine.set_tunarr_auth()


def test_probe_treats_401_as_failure(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_raising(http_error(401)))
    out = sr.probe("http://tunarr/api/channels", tunarr=True)
    assert out["ok"] is False
    assert "Settings" in out["error"]          # tells them where to fix it


def test_probe_treats_500_as_failure(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_raising(http_error(500)))
    assert sr.probe("http://tunarr/api/channels", tunarr=True)["ok"] is False


def test_probe_ok_on_200(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_returning(200))
    assert sr.probe("http://tunarr/api/channels", tunarr=True)["ok"] is True


def test_unreachable_tunarr_reports_not_ok(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_raising(OSError("refused")))
    out = sr.check_tunarr("http://nope:8000")
    assert out["ok"] is False and "refused" in out["error"]


def test_check_tunarr_does_not_leak_tested_credentials(monkeypatch):
    """A failed test must not leave the process authenticated as the attempt."""
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_returning(200))
    channel_engine.set_tunarr_auth("real", "realpw")
    before = channel_engine._TUNARR_AUTH

    sr.check_tunarr("http://tunarr", "typo", "wrongpw")

    assert channel_engine._TUNARR_AUTH == before


def test_missing_inputs_are_reported_not_crashed():
    assert sr.check_tunarr("")["ok"] is False
    assert sr.check_plex("http://plex", "")["ok"] is False
    assert sr.check_plex("", "tok")["ok"] is False


def test_test_connection_checks_only_what_was_given(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_returning(200))
    out = sr.test_connection(sr.ConnectionTest(tunarr_url="http://tunarr"))
    assert "tunarr" in out and "plex" not in out


def test_plex_check_uses_library_sections(monkeypatch):
    """A bare / accepts any token; /library/sections is what actually proves it."""
    seen = {}
    def _f(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp(200)
    monkeypatch.setattr(sr.urllib.request, "urlopen", _f)

    sr.check_plex("http://plex:32400", "tok")
    assert "/library/sections" in seen["url"]


# ── LAN-permissive Plex: a green result that can't vouch for the token ────────

def test_lan_permissive_plex_flags_unverified_token(monkeypatch):
    """Many self-hosted Plex servers allow unauthenticated local access, so EVERY
    local request succeeds — including one with a garbage token. Found on a real
    server: check_plex returned ok for the string 'garbage'. Reporting that as a
    validated token is a false green."""
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_returning(200))

    out = sr.check_plex("http://plex:32400", "garbage")
    assert out["ok"] is True          # Programmarr CAN read the library
    assert out["token_unverified"] is True
    assert "could not be verified" in out["note"]


def test_locked_down_plex_does_verify_the_token(monkeypatch):
    """When Plex actually enforces the token, a successful call means something —
    no caveat should be attached."""
    def _f(req, timeout=None):
        if not req.get_header("X-plex-token"):  # token travels as a header, not in the URL
            raise http_error(401)
        return FakeResp(200)
    monkeypatch.setattr(sr.urllib.request, "urlopen", _f)

    out = sr.check_plex("http://plex:32400", "good-token")
    assert out["ok"] is True
    assert "token_unverified" not in out
    assert "note" not in out


def test_bad_token_on_a_locked_down_plex_fails(monkeypatch):
    monkeypatch.setattr(sr.urllib.request, "urlopen", urlopen_raising(http_error(401)))
    out = sr.check_plex("http://plex:32400", "bad")
    assert out["ok"] is False
