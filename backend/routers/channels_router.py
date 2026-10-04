import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

# backend/ is on sys.path when running under uvicorn/the app; add it for direct
# imports (tests, type checkers) so channel_engine and scheduler resolve correctly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import channel_engine  # noqa: E402
import notices         # noqa: E402
import scheduler       # noqa: E402  (shared deploy_lock)
import badge_renderer  # noqa: E402
import icon_engine     # noqa: E402

router = APIRouter()
DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))


def _path() -> Path:
    return DATA_DIR / "channels.json"


def _load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def load() -> dict:
    try:
        with open(_path()) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"channels": data, "orphaned": [], "suggested_channels": []}
        return data
    except FileNotFoundError:
        return {"channels": [], "orphaned": [], "suggested_channels": []}
    except Exception as e:
        raise HTTPException(500, f"channels.json unreadable: {e}")


def save(data: dict):
    with open(_path(), "w") as f:
        json.dump(data, f, indent=2)


@router.get("/channel-notices")
def channel_notices():
    """Per channel: what its last resolve skipped (and why) or found again."""
    return notices.load(DATA_DIR)


@router.get("/channels")
def get_channels():
    return load()


@router.put("/channels")
def replace_channels(data: dict):
    save(data)
    return {"ok": True}


@router.get("/channels/{number}")
def get_channel(number: int):
    for ch in load().get("channels", []):
        if ch.get("number") == number:
            return ch
    raise HTTPException(404, f"Channel {number} not found")


@router.put("/channels/{number}")
def update_channel(number: int, channel: dict):
    data = load()
    for i, ch in enumerate(data.get("channels", [])):
        if ch.get("number") == number:
            data["channels"][i] = channel
            save(data)
            return {"ok": True}
    raise HTTPException(404, f"Channel {number} not found")


@router.delete("/channels/{number}")
def delete_channel(number: int):
    data = load()
    before = len(data.get("channels", []))
    data["channels"] = [ch for ch in data.get("channels", []) if ch.get("number") != number]
    if len(data["channels"]) == before:
        raise HTTPException(404, f"Channel {number} not found")
    save(data)
    return {"ok": True}


@router.post("/channels/{number}/apply")
async def apply_channel(number: int):
    """Push ONE channel to Tunarr in place (preserves Tunarr id / Plex mapping).
    Edit-only: the channel must already exist in Tunarr — new channels go through
    the Planner deploy flow."""
    ch = next((c for c in load().get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    def _do():
        movie_map, show_map, id_index = channel_engine.build_library_index_with_ids(tunarr_url)

        plex_sections, collection_cache = [], {}
        if any(isinstance(it, dict) and "collection" in it for it in ch.get("content", [])):
            if plex_url and plex_token:
                plex_sections = channel_engine.get_plex_sections(plex_url, plex_token)

        report = {}
        resolved, _missing = channel_engine.resolve_content(
            ch.get("content", []), movie_map, show_map,
            plex_url=plex_url, plex_token=plex_token,
            plex_sections=plex_sections, collection_cache=collection_cache,
            franchise_index=channel_engine.load_franchise_index(DATA_DIR),
            id_index=id_index, report=report,
        )
        # Say what was skipped (or found again) instead of letting it vanish — recorded
        # before anything below can fail, so even a channel that resolves to nothing
        # explains itself.
        notice = notices.record(DATA_DIR, number, report)
        if not resolved:
            raise channel_engine.ChannelEngineError(
                "resolved to empty — refusing to wipe the channel")

        if channel_engine.find_channel_by_number(tunarr_url, number) is None:
            raise channel_engine.ChannelEngineError(
                f"Channel #{number} not in Tunarr — create it in the Planner first")

        comm = ch.get("commercials") or {}
        pad_ms = int(comm.get("pad_minutes", 5)) * 60000 if comm.get("filler_list_id") else 0
        channel_engine.update_channel_in_place(
            tunarr_url, number, ch.get("shuffle", "shuffle"), resolved, pad_ms=pad_ms,
            expected_name=ch.get("name"), playback=ch.get("playback"))

        # An entry whose main number went stale but was found through a spare gets its
        # current numbers written back, so the next resolve matches on the first number.
        # Re-loaded fresh, and only that channel's content is touched.
        if report.get("healed"):
            data = load()
            for saved in data.get("channels", []):
                if saved.get("number") == number:
                    refreshed = channel_engine.apply_healed(saved.get("content", []), report["healed"])
                    if refreshed != saved.get("content"):
                        saved["content"] = refreshed
                        save(data)
                    break
        return len(resolved), notice

    try:
        async with scheduler.deploy_lock:
            count, notice = await asyncio.to_thread(_do)
        return {"ok": True, "number": number, "program_count": count, "notice": notice}
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(409, str(e))


@router.post("/channels/{number}/icon")
async def channel_icon(number: int, body: dict):
    """Set or pin a channel's Tunarr icon.

    body: {"mode": "badge" | "tmdb" | "custom" | "clear", "url": "..."(custom only)}
    badge/tmdb/custom write a pin into channels.json ("icon": {..., "pinned": true})
    so automatic art passes (fetch_images.py) skip the channel; "clear" resets the
    Tunarr icon to default and removes the pin (back to automatic).
    """
    mode = (body or {}).get("mode")
    if mode not in ("badge", "tmdb", "custom", "clear"):
        raise HTTPException(422, "mode must be one of: badge, tmdb, custom, clear")

    data = load()
    ch = next((c for c in data.get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    name = (ch.get("name") or "").strip()

    def _do():
        summary = channel_engine.find_channel_by_number(tunarr_url, number)
        if summary is None:
            raise channel_engine.ChannelEngineError(
                f"Channel #{number} not in Tunarr — deploy it first")
        tch = icon_engine.get_full_channel(tunarr_url, summary["id"])
        if tch is None:
            raise channel_engine.ChannelEngineError("Could not read channel from Tunarr")

        if mode == "clear":
            if not icon_engine.clear_tunarr_channel_icon(tunarr_url, tch):
                raise channel_engine.ChannelEngineError("Tunarr icon reset failed")
            return ""

        if mode == "custom":
            url = (body.get("url") or "").strip()
            if not url:
                raise channel_engine.ChannelEngineError("url required for custom mode")
        elif mode == "badge":
            spec = icon_engine.load_spec_hints(
                DATA_DIR / "planner_state.json").get(name.lower(), {})
            png = badge_renderer.render_badge(
                name, kind=spec.get("kind"), genre=icon_engine.spec_genre(spec))
            url = icon_engine.upload_image_to_tunarr(
                tunarr_url, png, f"programmarr-ch{number}-{os.urandom(4).hex()}.png")
        else:  # tmdb
            key = cfg.get("tmdb_api_key", "")
            if not key:
                raise channel_engine.ChannelEngineError(
                    "tmdb_api_key not configured — use a badge or custom URL")
            spec = icon_engine.load_spec_hints(
                DATA_DIR / "planner_state.json").get(name.lower(), {})
            url = icon_engine.resolve_tmdb_logo(
                icon_engine.icon_attempts(ch, spec.get("kind")), key)
            if not url:
                raise channel_engine.ChannelEngineError(
                    "No verified TMDB logo for this channel — use a badge or custom URL")

        if not icon_engine.set_tunarr_channel_icon(tunarr_url, tch, url):
            raise channel_engine.ChannelEngineError("Tunarr icon update failed")
        return url

    try:
        async with scheduler.deploy_lock:
            url = await asyncio.to_thread(_do)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(409, str(e))

    if mode == "clear":
        ch.pop("icon", None)
    else:
        ch["icon"] = {"mode": mode, "url": url, "pinned": True}
    save(data)
    return {"ok": True, "mode": mode, "url": url}


# The Add box asks "which items have this title?" on every add. Building the id index
# scans all of Tunarr's libraries, so keep the last one for a few minutes.
_INDEX_TTL_SECONDS = 300
_index_cache = {"at": 0.0, "index": None}  # "index" holds (movie_map, show_map, id_index)
_index_lock = threading.Lock()


def _cached_library(tunarr_url, refresh=False):
    with _index_lock:
        stale = time.monotonic() - _index_cache["at"] > _INDEX_TTL_SECONDS
        if refresh or stale or _index_cache["index"] is None:
            _index_cache.update(at=time.monotonic(), index=channel_engine.build_library_index_with_ids(tunarr_url))
        return _index_cache["index"]


@router.get("/library/lookup")
async def library_lookup(title: str, kind: str = "", refresh: bool = False):
    """Every movie/show in the library with exactly this title, each with a ready-to-save
    channel entry that carries its own numbers ({"movie": .., "year": .., "ids": {..}}).
    One match: the Add box pins it. Several: it asks. None: it adds the text as typed."""
    if kind not in ("", "movie", "show"):
        raise HTTPException(400, "kind must be 'movie' or 'show'")
    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")
    channel_engine.set_tunarr_auth_from_config(cfg)
    try:
        _, _, index = await asyncio.to_thread(_cached_library, tunarr_url, refresh)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))
    matches = []
    for item in channel_engine.find_by_title(title, index, kind or None):
        k = "movie" if item["type"] == "Movie" else "show"
        matches.append({"kind": k, "title": item["title"], "year": item.get("year"),
                        "entry": channel_engine.entry_for_item(item)})
    return {"matches": matches}


def _clean_review():
    return {"missing_count": 0, "missing": [], "healed_count": 0, "ambiguous_count": 0, "ambiguous": []}


def _review_setup():
    """The connection details a review needs, or a clear error."""
    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")
    channel_engine.set_tunarr_auth_from_config(cfg)
    return tunarr_url, cfg.get("plex_url", "").rstrip("/"), cfg.get("plex_token", "")


def _review_channels(channels, tunarr_url, plex_url, plex_token, refresh=False, isolate=False):
    """{channel number: review} for these channels — each resolved against the library
    exactly as a deploy would, but writing nothing and never touching Tunarr's channels.
    One cached library scan and one Plex collection lookup serve them all. With `isolate`, a
    channel that can't be checked is left out instead of failing the rest."""
    movie_map, show_map, id_index = _cached_library(tunarr_url, refresh)
    uses_collection = any(isinstance(it, dict) and "collection" in it
                          for ch in channels for it in ch.get("content", []))
    plex_sections = (channel_engine.get_plex_sections(plex_url, plex_token)
                     if uses_collection and plex_url and plex_token else [])
    franchise_index = channel_engine.load_franchise_index(DATA_DIR)
    collection_cache, reviews = {}, {}
    for ch in channels:
        report = {}
        try:
            channel_engine.resolve_content(
                ch.get("content", []), movie_map, show_map, plex_url=plex_url, plex_token=plex_token,
                plex_sections=plex_sections, collection_cache=collection_cache,
                franchise_index=franchise_index, id_index=id_index, report=report)
        except Exception:
            if not isolate:
                raise
            continue  # a check is information: one odd channel must not blank the others
        reviews[ch.get("number")] = notices.summarize(report) or _clean_review()
    return reviews


@router.get("/channels/{number}/review")
async def channel_review(number: int, refresh: bool = False):
    """Check ONE channel against the library right now, without deploying anything: what
    it would skip (and why), what it would find again through a backup number, and every
    plain title that names more than one movie/show — with the choices. Nothing is written,
    and it never touches Tunarr's channels; the editor shows this when it opens."""
    ch = next((c for c in load().get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")
    tunarr_url, plex_url, plex_token = _review_setup()
    try:
        reviews = await asyncio.to_thread(_review_channels, [ch], tunarr_url, plex_url, plex_token, refresh)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))
    return reviews[number]


@router.get("/channel-reviews")
async def channel_reviews(refresh: bool = False):
    """The same live check for EVERY channel, so the channel list can flag the ones that need
    a look without anyone opening them first (the saved notices only know what the last
    Apply or auto-update saw)."""
    channels = load().get("channels", [])
    if not channels:
        return {}
    tunarr_url, plex_url, plex_token = _review_setup()
    try:
        return await asyncio.to_thread(
            _review_channels, channels, tunarr_url, plex_url, plex_token, refresh, True)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))


@router.get("/library/titles")
def library_titles():
    csv = DATA_DIR / "plex_library.csv"
    if not csv.exists():
        return []
    titles = []
    try:
        with open(csv, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == 0:
                    continue
                parts = line.split(",", 1)
                if parts:
                    titles.append(parts[0].strip().strip('"'))
    except Exception:
        pass
    return titles
