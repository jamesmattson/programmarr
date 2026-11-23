"""Live-channel recipe endpoints.

Currently exposes the author-time franchise-match preview. This is the first
place the backend imports channel_engine in-process (rather than spawning it as
a subprocess), so it also establishes the sys.path wiring the future scheduler
reuses: SCRIPTS_DIR holds the pipeline scripts but isn't on the path by default.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))
SCRIPTS_DIR = Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent.parent))

# channel_engine.py lives at SCRIPTS_DIR (repo root in dev, /app in Docker), which
# is not on sys.path for the backend process — add it before importing.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import channel_engine  # noqa: E402
import scheduler  # noqa: E402  (backend/ is on sys.path)

router = APIRouter()


def _load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


class PreviewRequest(BaseModel):
    value: str
    exclude: list[str] = []
    order: Optional[str] = None
    match: str = "title_contains"


@router.post("/recipes/preview")
def preview_recipe(req: PreviewRequest):
    """Show exactly which current Tunarr titles a live ref rule matches.

    Powers the author-time confirm step: the user sees the matched titles (in the
    order they'll air) before saving a live recipe, so a bad rule is caught up front.

    Supports both live ref types:
    - ``title_contains`` (default) — word-boundary name matching via
      ``channel_engine.match_titles``.
    - ``franchise`` — cached-membership identity resolution via
      ``channel_engine.match_franchise`` (same TMDB/Wikidata data the Planner uses).
      ``value`` should be the collection/franchise name (e.g. "Die Hard Collection").
    """
    if not req.value.strip():
        raise HTTPException(400, "match value is required")

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    try:
        movie_map, show_map = channel_engine.build_library_index(tunarr_url)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, f"Tunarr library unavailable: {e}")

    if req.match == "franchise":
        fr_index = channel_engine.load_franchise_index(DATA_DIR)
        _, preview = channel_engine.match_franchise(
            req.value, fr_index, movie_map, show_map,
            order=req.order or "release_date", exclude=req.exclude)
    else:
        _, preview = channel_engine.match_titles(
            req.value, movie_map, show_map, order=req.order, exclude=req.exclude)
    return {"value": req.value, "match": req.match, "order": req.order,
            "count": len(preview), "matches": preview}


# ── Scheduler control / status ─────────────────────────────────────────────────

@router.get("/recipes/status")
def recipes_status():
    """Current scheduler state: enabled flag, pause, interval, live count, last cycle."""
    return scheduler.get_status()


@router.post("/recipes/run")
async def recipes_run(apply: bool = Query(True), only: Optional[int] = Query(None)):
    """Run one cycle on demand. apply=false is a dry run (detect + log, no patch).
    `only=N` limits the run to a single live channel (the per-channel "Sync now").

    This is the manual-refresh / test trigger — it runs the exact same code path
    the background loop does, so you don't have to wait for the interval.
    """
    return await scheduler.run_cycle(apply=apply, only=only)


@router.post("/recipes/pause")
def recipes_pause(paused: bool = Query(True)):
    """Runtime kill switch — halt (or resume) auto-updates without a restart."""
    return scheduler.set_paused(paused)


class RecipeConfigRequest(BaseModel):
    enabled: bool
    interval_hours: float = 12


@router.post("/recipes/config")
def set_recipe_config(req: RecipeConfigRequest):
    """Persist the master enable flag + interval. Merge-writes config.json so it
    never clobbers connection/auth settings (unlike the strict /config model)."""
    cfg = _load_config()
    cfg["recipes_enabled"] = req.enabled
    cfg["recipe_interval_hours"] = req.interval_hours
    try:
        from routers.config_router import write_config
        write_config(cfg, DATA_DIR / "config.json")
    except Exception as e:
        raise HTTPException(500, f"Could not save config: {e}")
    return scheduler.get_status()
