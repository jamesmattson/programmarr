import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

import sys
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import channel_engine  # noqa: E402

GITHUB_LATEST_RELEASE = (
    "https://api.github.com/repos/jamesmattson/programmarr/releases/latest"
)

# Notifier cache: one GitHub hit per _UPDATE_TTL seconds, regardless of UI loads.
_UPDATE_TTL = 6 * 3600
_update_cache: dict = {"at": 0.0, "data": None}


def _parse_semver(s: str) -> tuple[int, int, int]:
    """'v0.10.1' -> (0, 10, 1). Tolerant: missing parts -> 0; a pre-release suffix
    on a part ('1-beta') keeps only the leading digits. Raises ValueError on no digits."""
    s = (s or "").strip().lstrip("vV")
    if not s:
        raise ValueError("empty version")
    out = []
    for part in s.split(".")[:3]:
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    """True iff `latest` is a strictly higher semver than `current`. Never raises;
    any unparseable input (or empty `current`) yields False so a failed check
    degrades to 'no update' rather than crashing the UI."""
    try:
        return _parse_semver(latest) > _parse_semver(current)
    except Exception:
        return False


router = APIRouter()
DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))


def load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def probe(url: str, tunarr: bool = False, headers: dict | None = None) -> dict:
    """Reach a URL and judge whether it actually answered usefully.

    An HTTP error is NOT success. This used to return ok:True for any response
    including 401, which reported an auth-protected Tunarr as healthy — harmless
    before Tunarr had auth, actively misleading now that it does.

    SECURITY: secrets go in `headers`, never in `url`, and error text never
    repeats the URL — both used to leak the Plex token back to the caller.
    """
    try:
        headers = {**(channel_engine.tunarr_headers() if tunarr else {}), **(headers or {})}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return {"ok": True, "status": r.status}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            hint = ("Tunarr requires a username and password — set them in "
                    "Settings -> Connections.") if tunarr else \
                   "Plex rejected the token."
            return {"ok": False, "status": e.code, "error": f"Authentication failed ({e.code}). {hint}"}
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_tunarr(url: str, username: str = "", password: str = "") -> dict:
    """Test a Tunarr URL + optional credentials WITHOUT reading saved config.

    Onboarding needs to validate what the user just typed, before anything is
    written — otherwise a typo'd URL produces a green 'Setup complete'.
    """
    url = (url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "No Tunarr URL given"}
    prior = channel_engine._TUNARR_AUTH
    try:
        channel_engine.set_tunarr_auth(username, password)
        result = {**probe(f"{url}/api/channels", tunarr=True), "url": url}
    finally:
        # Never let a connection test leave the process authenticated as
        # whatever was being tried.
        channel_engine._TUNARR_AUTH = prior
    return result


def check_plex(url: str, token: str) -> dict:
    url = (url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "No Plex URL given"}
    if not token:
        return {"ok": False, "error": "No Plex token given"}
    # /library/sections proves we can read the library, which is what Programmarr
    # actually needs — a bare / accepts any token.
    result = {**probe(f"{url}/library/sections", headers={"X-Plex-Token": token}), "url": url}

    if result.get("ok"):
        # Many self-hosted Plex servers have "Allow unauthenticated access on the
        # local network" on, which makes EVERY local request succeed — including
        # one with a garbage token. Reporting that as "token valid" would be a
        # false green. One extra request tells us whether the token was actually
        # the reason we got in.
        no_token = probe(f"{url}/library/sections")
        if no_token.get("ok"):
            result["token_unverified"] = True
            result["note"] = (
                "Your Plex allows unauthenticated access from this network, so the "
                "token itself could not be verified. Programmarr can read your "
                "library either way — but double-check the token if you later "
                "restrict local access."
            )
    return result


@router.get("/status")
def get_status():
    cfg = load_config()
    tunarr = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    plex = cfg.get("plex_url", "").rstrip("/")
    token = cfg.get("plex_token", "")

    tr = {"ok": False, "error": "Not configured", "url": tunarr}
    pr = {"ok": False, "error": "Not configured", "url": plex}

    if tunarr:
        tr = {**probe(f"{tunarr}/api/channels", tunarr=True), "url": tunarr}
        if tr.get("ok"):
            # Surfaced so a bug report can state it without the user digging —
            # the issue template asks for it.
            tr["version"] = channel_engine.get_tunarr_version(tunarr)
    if plex and token:
        pr = {**probe(f"{plex}/", headers={"X-Plex-Token": token}), "url": plex}

    return {"tunarr": tr, "plex": pr}


class ConnectionTest(BaseModel):
    tunarr_url: str = ""
    tunarr_username: str = ""
    tunarr_password: str = ""
    plex_url: str = ""
    plex_token: str = ""


@router.post("/test-connection")
def test_connection(body: ConnectionTest):
    """Validate credentials the user has typed but not yet saved.

    Tests only the sides that were supplied, so onboarding can check Tunarr and
    Plex independently as each is filled in.
    """
    from routers.config_router import MASK, saved_plex_token_for, saved_tunarr_password_for

    cfg = load_config()
    # The Settings form sends the mask for a secret the user didn't retype — test
    # the stored value rather than literally trying to authenticate as "••••••••".
    # SECURITY: only for the URL that secret is saved against; any other URL must
    # bring its own secret, or the stored one would be sent to the caller's host.
    retype = "Re-enter the {} to test a different URL than the saved one."
    out = {}
    if body.tunarr_url:
        tunarr_pw = body.tunarr_password
        if tunarr_pw == MASK:
            tunarr_pw = saved_tunarr_password_for(cfg, body.tunarr_url)
        if body.tunarr_password == MASK and not tunarr_pw:
            out["tunarr"] = {"ok": False, "error": retype.format("Tunarr password")}
        else:
            out["tunarr"] = check_tunarr(body.tunarr_url, body.tunarr_username, tunarr_pw)
    if body.plex_url or body.plex_token:
        plex_token = body.plex_token
        if plex_token == MASK:
            plex_token = saved_plex_token_for(cfg, body.plex_url)
        if body.plex_token == MASK and not plex_token:
            out["plex"] = {"ok": False, "error": retype.format("Plex token")}
        else:
            out["plex"] = check_plex(body.plex_url, plex_token)
    return out


def _fetch_latest_release() -> dict | None:
    """Hit the GitHub Releases API for the newest published release. Returns
    {latest, name, url} or None on any failure. GitHub requires a User-Agent."""
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_RELEASE,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "programmarr"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            rel = json.loads(r.read())
        return {
            "latest": (rel.get("tag_name") or "").lstrip("vV"),
            "name": rel.get("name") or rel.get("tag_name") or "",
            "url": rel.get("html_url") or "",
        }
    except Exception:
        return None


@router.get("/update-check")
def update_check(current: str = ""):
    """Is a newer release available? `current` is the running app version (the
    frontend passes its baked-in package.json version). Off when the user has
    disabled update checks. Result cached server-side for _UPDATE_TTL."""
    cfg = load_config()
    if not cfg.get("update_check_enabled", True):
        return {"enabled": False}

    now = time.time()
    if now - _update_cache["at"] > _UPDATE_TTL:
        # Stamp `at` BEFORE the blocking fetch so a racing threadpool request sees a
        # fresh timestamp and skips it — worst case is one stale read, not two GitHub
        # hits. Keep any prior (stale) data on failure; accept a 6h gap on outage
        # rather than hammering GitHub from a home lab.
        _update_cache["at"] = now
        fetched = _fetch_latest_release()
        if fetched is not None:
            _update_cache["data"] = fetched

    data = _update_cache["data"]
    if not data:
        return {"enabled": True, "update_available": False, "current": current, "latest": None}
    return {
        "enabled": True,
        "update_available": is_newer(data["latest"], current) if current else False,
        "current": current,
        "latest": data["latest"],
        "name": data["name"],
        "url": data["url"],
    }


@router.get("/tunarr/channels")
def tunarr_channels():
    cfg = load_config()
    url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not url:
        return []
    try:
        req = urllib.request.Request(f"{url}/api/channels",
                                    headers=channel_engine.tunarr_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _parse_xmltv_time(s: str) -> str:
    return datetime.strptime(s.strip(), "%Y%m%d%H%M%S %z").isoformat()


def _num_from_cid(cid: str) -> int | None:
    try:
        return int(cid.split(".")[0][1:])
    except (ValueError, IndexError):
        return None


def parse_guide_xml(xml_text: str) -> dict:
    """Pure: XMLTV string -> {channels:[...], programmes:[...]}. No network. Unit-tested directly."""
    root = ET.fromstring(xml_text)

    channels = []
    for ch in root.findall("channel"):
        num = _num_from_cid(ch.get("id", ""))
        if num is None:
            continue
        name_el = ch.find("display-name")
        icon_el = ch.find("icon")
        channels.append({
            "number": num,
            "name": (name_el.text or "").strip() if name_el is not None else f"Channel {num}",
            "icon": icon_el.get("src") if icon_el is not None else None,
        })

    programmes = []
    for pr in root.findall("programme"):
        num = _num_from_cid(pr.get("channel", ""))
        if num is None:
            continue
        title_el = pr.find("title")
        sub_el = pr.find("sub-title")
        try:
            start = _parse_xmltv_time(pr.get("start", ""))
            stop = _parse_xmltv_time(pr.get("stop", ""))
        except ValueError:
            continue
        programmes.append({
            "number": num,
            "start": start,
            "stop": stop,
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "episode": (sub_el.text or "").strip() if sub_el is not None else "",
        })

    channels.sort(key=lambda c: c["number"])
    return {"channels": channels, "programmes": programmes}


@router.get("/guide")
def get_guide():
    cfg = load_config()
    url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not url:
        return {"channels": [], "programmes": [], "error": "Tunarr not configured"}
    try:
        req = urllib.request.Request(f"{url}/api/xmltv.xml",
                                    headers=channel_engine.tunarr_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            xml_text = r.read().decode("utf-8", errors="replace")
        return parse_guide_xml(xml_text)
    except Exception as e:
        return {"channels": [], "programmes": [], "error": f"Could not reach Tunarr: {e}"}


@router.get("/tunarr/filler-lists")
def tunarr_filler_lists():
    """Filler lists in Tunarr — powers the Commercials picker in the channel editor.

    Returns [{id, name, contentCount}]. The user creates/manages these in Tunarr;
    Programmarr only references one by id when a channel has commercials enabled.
    """
    cfg = load_config()
    url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not url:
        return []
    try:
        req = urllib.request.Request(f"{url}/api/filler-lists",
                                    headers=channel_engine.tunarr_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return []
