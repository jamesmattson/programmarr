import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))

MASK = "••••••••"


class ConfigModel(BaseModel):
    tunarr_url: str = ""
    plex_url: str = ""
    plex_token: str = ""
    # List of {name, url, token} Plex server entries. Replaces the legacy scalar
    # plex_url/plex_token pair; both are kept for CLI backward compat.
    plex_servers: list = []
    tmdb_api_key: str = ""
    auth_username: str = ""
    auth_password: str = ""
    # Tunarr's own optional HTTP basic auth (tunarr #1865). Blank = Tunarr is open.
    # Distinct from auth_username/auth_password, which guard Programmarr itself.
    tunarr_username: str = ""
    tunarr_password: str = ""
    # Ordered list of category keys controlling channel numbering order.
    # Empty ⇒ canonical default from channel_blocks.CANONICAL_ORDER at use time.
    channel_order: list = []
    # Whether the app polls GitHub for a newer release (the in-app update banner).
    # Default on; stored explicitly so the falsy-prune below can't drop a False.
    update_check_enabled: bool = True


def _norm_url(u) -> str:
    return (u or "").strip().rstrip("/")


def saved_plex_url(cfg: dict) -> str:
    """The primary Plex URL Settings shows (plex_servers[0] wins over legacy plex_url)."""
    servers = cfg.get("plex_servers") or []
    return _norm_url(servers[0].get("url") if servers else cfg.get("plex_url"))


def saved_plex_token_for(cfg: dict, url: str) -> str:
    """The stored token for exactly this Plex URL, or "" if no saved server has it.

    SECURITY: the UI sends MASK for a secret it didn't retype, and we swap the real
    secret back in. That swap must only ever target the server the secret belongs
    to; otherwise anyone can point the URL at their own host and receive the token.
    """
    url = _norm_url(url)
    for s in cfg.get("plex_servers") or []:
        if _norm_url(s.get("url")) == url:
            return s.get("token", "")
    if _norm_url(cfg.get("plex_url")) == url:
        return cfg.get("plex_token", "")
    return ""


def saved_tunarr_password_for(cfg: dict, url: str) -> str:
    """The stored Tunarr password, only if `url` is the saved Tunarr URL (see above)."""
    if _norm_url(url) == _norm_url(cfg.get("tunarr_url")):
        return cfg.get("tunarr_password", "")
    return ""


def _path() -> Path:
    return DATA_DIR / "config.json"


def load_config() -> dict:
    try:
        with open(_path()) as f:
            return json.load(f)
    except Exception:
        return {}


@router.get("/config")
def get_config():
    config = load_config()
    if config.get("auth_password"):
        config["auth_password"] = MASK
    if config.get("plex_token"):
        config["plex_token"] = MASK
    if config.get("tmdb_api_key"):
        config["tmdb_api_key"] = MASK
    if config.get("tunarr_password"):
        config["tunarr_password"] = MASK
    if config.get("plex_servers"):
        config["plex_servers"] = [
            {**s, "token": MASK} if s.get("token") else s
            for s in config["plex_servers"]
        ]
    return config


@router.post("/config")
def save_config(config: ConfigModel):
    existing = load_config()
    data = config.model_dump()
    # SECURITY: a masked secret is kept only while it stays pointed at its own
    # server. Changing the URL means re-typing the secret, or the real one would
    # be sent to whatever host the new URL names.
    if data.get("plex_token") == MASK and _norm_url(data.get("plex_url")) != saved_plex_url(existing):
        raise HTTPException(400, "Re-enter the Plex token to change the Plex URL.")
    if data.get("tunarr_password") == MASK and \
            _norm_url(data.get("tunarr_url")) != _norm_url(existing.get("tunarr_url")):
        raise HTTPException(400, "Re-enter the Tunarr password to change the Tunarr URL.")
    for field in ("auth_password", "plex_token", "tmdb_api_key", "tunarr_password"):
        if data.get(field) == MASK:
            data[field] = existing.get(field, "")
    # Lists must bypass the falsy-prune below ([] is falsy but meaningful as "no change").
    # channel_order: only overwrite when non-empty so an Onboarding save can't wipe it.
    order = data.pop("channel_order", None) or []
    # plex_servers: restore masked tokens by matching on URL, then write if non-empty.
    plex_servers = data.pop("plex_servers", None) or []
    if plex_servers:
        existing_by_url = {s.get("url", ""): s for s in existing.get("plex_servers", [])}
        restored = []
        for srv in plex_servers:
            if srv.get("token") == MASK:
                srv = {**srv, "token": existing_by_url.get(srv.get("url", ""), {}).get("token", "")}
            restored.append(srv)
        plex_servers = restored
    # Booleans must bypass the falsy-prune below (False is falsy → would be deleted).
    update_check = bool(data.pop("update_check_enabled", True))
    # Merge onto existing so keys the UI form doesn't manage are preserved — e.g.
    # the live-channel keys (recipes_enabled, recipe_interval_hours) and the advanced
    # config keys (tunarr_channel_group, tunarr_stream_mode), which are edited directly
    # in config.json and must survive a Settings save. UI fields left empty are removed.
    merged = dict(existing)
    for k, v in data.items():
        if v:
            merged[k] = v
        else:
            merged.pop(k, None)
    if order:
        merged["channel_order"] = order
    if plex_servers:
        merged["plex_servers"] = plex_servers
        # Keep legacy scalar fields in sync with the primary server for CLI compat.
        merged["plex_url"]   = plex_servers[0].get("url", "")
        merged["plex_token"] = plex_servers[0].get("token", "")
    elif merged.get("plex_servers"):
        # Legacy-format save (plex_url/plex_token fields) — sync plex_servers[0] to match
        # so both representations stay consistent.
        url   = merged.get("plex_url", "")
        token = merged.get("plex_token", "")
        primary = dict(merged["plex_servers"][0])
        if url and _norm_url(url) != _norm_url(primary.get("url")):
            # New server: never carry the old server's token over to it.
            primary["token"] = token
        if url:
            primary["url"] = url
        if token:
            primary["token"] = token
        if not primary.get("token"):
            primary.pop("token", None)
        merged["plex_servers"] = [primary] + merged["plex_servers"][1:]
    merged["update_check_enabled"] = update_check
    # Preserve old channel_blocks key silently (don't crash; just ignore it).
    write_config(merged)
    return {"ok": True}


def write_config(cfg: dict, path: Path | None = None) -> None:
    """Atomic write: the auth middleware fails closed on an unreadable config, so a
    request must never see a half-written file."""
    path = path or _path()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=4)
    os.replace(tmp, path)


@router.get("/config/status")
def config_status():
    config = load_config()
    has_plex = bool(config.get("plex_servers")) or bool(
        config.get("plex_url") and config.get("plex_token")
    )
    return {
        "configured": bool(config.get("tunarr_url") and has_plex),
        "has_tmdb": bool(config.get("tmdb_api_key")),
        "has_auth": bool(config.get("auth_username") and config.get("auth_password")),
    }
