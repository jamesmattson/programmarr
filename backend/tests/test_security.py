"""test_security.py — regression guards for the v0.9.1 security fixes.

1. A masked secret ("••••••••") is only swapped for the real one when the request
   targets the server that secret is saved for; it never leaks to another host.
2. No CORS; a state-changing request from another website is refused (CSRF).
3. Tunarr credentials never go to TMDB, and secrets are redacted from log lines.
"""

import asyncio
import base64
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import channel_engine
import icon_engine
import main
from routers import config_router as cr, status_router as sr

MASK = cr.MASK
SAVED = {
    "tunarr_url": "http://tunarr:8000", "tunarr_username": "u", "tunarr_password": "TUNARR-SECRET",
    "plex_url": "http://plex:32400", "plex_token": "PLEX-SECRET",
    "plex_servers": [{"name": "Plex", "url": "http://plex:32400", "token": "PLEX-SECRET"}],
}


class _Client:
    """Calls the route functions directly (no httpx/TestClient in the dev deps)."""

    def test_connection(self, **body):
        return sr.test_connection(sr.ConnectionTest(**body))

    def save(self, **body):
        try:
            cr.save_config(cr.ConfigModel(**body))
            return 200
        except HTTPException as e:
            return e.status_code


@pytest.fixture
def client(tmp_path, monkeypatch):
    for mod in (main, cr, sr):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(SAVED))
    return _Client()


def _through_middleware(method, path, headers=None):
    """Run one request through main.auth_middleware; returns the status code."""
    scope = {"type": "http", "method": method, "path": path, "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}

    async def call_next(_req):
        return Response("ok")

    return asyncio.run(main.auth_middleware(Request(scope), call_next)).status_code


@pytest.fixture
def outbound(monkeypatch):
    """Record every request status_router sends; answer 404 (the leak-by-error case)."""
    sent = []

    def _urlopen(req, timeout=None):
        sent.append((req.full_url, dict(req.header_items())))
        raise sr.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(sr.urllib.request, "urlopen", _urlopen)
    return sent


def _leaked(sent, result):
    blob = json.dumps(sent) + json.dumps(result)
    basic = base64.b64encode(b"u:TUNARR-SECRET").decode()
    return "PLEX-SECRET" in blob or "TUNARR-SECRET" in blob or basic in blob


# ── 1. masked-secret substitution ─────────────────────────────────────────────

def test_masked_plex_token_is_not_sent_to_another_host(client, outbound):
    out = client.test_connection(plex_url="http://attacker", plex_token=MASK)
    assert not _leaked(outbound, out)
    assert "Re-enter" in out["plex"]["error"]


def test_masked_tunarr_password_is_not_sent_to_another_host(client, outbound):
    out = client.test_connection(tunarr_url="http://attacker", tunarr_username="u",
                                 tunarr_password=MASK)
    assert not _leaked(outbound, out)


def test_masked_secret_still_tests_the_saved_server(client, outbound):
    """The Settings 'Test connections' button must keep working without retyping."""
    client.test_connection(plex_url="http://plex:32400/", plex_token=MASK)
    url, headers = outbound[0]
    assert "PLEX-SECRET" not in url  # token is a header, never in the URL
    assert headers.get("X-plex-token") == "PLEX-SECRET"


def test_error_text_never_echoes_the_url(client, outbound):
    out = client.test_connection(plex_url="http://plex:32400", plex_token=MASK)
    assert out["plex"]["error"] == "HTTP 404"
    assert "PLEX-SECRET" not in json.dumps(sr.get_status())


def test_save_refuses_masked_secret_with_a_new_url(client, tmp_path):
    for body in ({"plex_url": "http://attacker", "plex_token": MASK},
                 {"tunarr_url": "http://attacker", "tunarr_password": MASK}):
        assert client.save(**body) == 400
    assert json.loads((tmp_path / "config.json").read_text()) == SAVED


def test_save_with_new_plex_url_and_no_token_drops_the_old_token(client, tmp_path):
    client.save(tunarr_url=SAVED["tunarr_url"], plex_url="http://new:32400")
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert "PLEX-SECRET" not in json.dumps(cfg)


def test_unchanged_settings_save_keeps_secrets(client, tmp_path):
    body = {**SAVED, "plex_servers": [], "tunarr_password": MASK, "plex_token": MASK}
    assert client.save(**body) == 200
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["plex_servers"][0]["token"] == "PLEX-SECRET"
    assert cfg["tunarr_password"] == "TUNARR-SECRET"


# ── 2. CORS / CSRF ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers, blocked", [
    ({"sec-fetch-site": "cross-site"}, True),
    ({"sec-fetch-site": "same-site"}, True),
    ({"sec-fetch-site": "same-origin"}, False),
    ({"sec-fetch-site": "none"}, False),
    ({"origin": "http://evil.example", "host": "nas:7979"}, True),
    ({"origin": "null", "host": "nas:7979"}, True),
    ({"origin": "http://nas:7979", "host": "nas:7979"}, False),
    ({"host": "nas:7979"}, False),  # curl / scripts: not a browser, not CSRF
])
def test_is_cross_site(headers, blocked):
    assert main.is_cross_site(headers) is blocked


def test_cross_site_post_is_refused_and_cors_is_gone(client):
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert _through_middleware("POST", "/api/pipeline/deploy", evil) == 403
    assert _through_middleware("GET", "/api/config/status", evil) == 200  # reads: no CORS, so unreadable
    assert _through_middleware("POST", "/api/config", {"Sec-Fetch-Site": "same-origin"}) == 200
    assert not any("CORS" in m.cls.__name__ for m in main.app.user_middleware)


def test_unreadable_config_fails_closed(client, tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    assert _through_middleware("GET", "/api/config") == 503


# ── 3. TMDB gets no Tunarr credentials; logs get no secrets ───────────────────

def test_tmdb_requests_carry_no_tunarr_auth(monkeypatch, capsys):
    sent = []

    def _urlopen(req, timeout=None):
        sent.append(dict(req.header_items()))
        raise icon_engine.urllib.error.HTTPError(req.full_url, 500, "x", {}, None)

    monkeypatch.setattr(icon_engine.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(channel_engine, "_TUNARR_AUTH", None)
    channel_engine.set_tunarr_auth("u", "p")
    icon_engine.search_movie_verified("Heat", "TMDB-KEY")
    icon_engine.http_get("http://tunarr/api/channels", tunarr=True)
    assert not any("Authorization" in h for h in sent[:-1])  # every TMDB call
    assert "Authorization" in sent[-1]                       # the Tunarr call
    assert "TMDB-KEY" not in capsys.readouterr().out


def test_every_script_that_loads_config_sets_tunarr_auth():
    """#46: export.py and fetch_images.py never set the auth, so their Tunarr calls
    went out with no credentials."""
    import export, fetch_images  # noqa: E401
    from pathlib import Path
    for mod in (export, fetch_images):
        assert "channel_engine.set_tunarr_auth_from_config(cfg)" in Path(mod.__file__).read_text(encoding="utf-8")
