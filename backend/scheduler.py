"""Live-channel scheduler.

A single in-process asyncio loop that keeps channels marked "live": true in
channels.json fresh. Each cycle re-resolves a live channel's content against the
live Tunarr library and patches the channel IN PLACE (never delete/recreate) only
when the resolved program set differs from what's currently scheduled.

Design notes:
  - No state file. The "current" side of the diff is read straight from Tunarr
    (read_channel_programming), so the loop survives container restarts for free
    and an unchanged channel is a cheap no-op (no guide churn).
  - Tunarr is the source of truth; the loop never writes channels.json. A live
    channel's content definition (show name, collection ref, or {"match": ...}
    franchise ref) stays as-authored and is simply re-resolved each cycle.
  - deploy_lock serializes the cycle against manual deploys (pipeline endpoints
    that spawn create.py acquire the same lock), so the scheduler never patches a
    channel a deploy is mid-deleting.
  - Blocking network work runs in a worker thread (asyncio.to_thread) so the cycle
    never stalls the FastAPI event loop.

Ships OFF: the loop does nothing unless config.json has recipes_enabled: true.
"""

import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent))
SCRIPTS_DIR = Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent))
LOGS_DIR = DATA_DIR / "logs"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import channel_engine  # noqa: E402
import notices         # noqa: E402

# Shared with pipeline_router: any endpoint that spawns create.py acquires this so
# a manual deploy and a scheduler cycle never touch Tunarr at the same time.
deploy_lock = asyncio.Lock()

# In-memory runtime state. recipes_enabled / recipe_interval_hours are persistent
# (config.json); `paused` is a runtime kill switch that survives until restart.
_state: dict = {
    "paused": False,
    "running": False,
    "last_cycle": None,      # summary dict of the most recent cycle
    "last_auto_run": None,   # wall-clock time.time() of last automatic cycle (None = never)
}

STATE_FILE = "recipe_state.json"  # cosmetic per-channel sync metadata (NOT correctness state)

DEFAULT_INTERVAL_HOURS = 12


# ── Config / channels ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_channels() -> list:
    """Read channels.json defensively — a torn mid-write read just skips the cycle."""
    try:
        with open(DATA_DIR / "channels.json", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return data.get("channels", [])
    except Exception:
        return []


def _live_channels(channels: list) -> list:
    return [ch for ch in channels if ch.get("live")]


# ── Cosmetic per-channel sync state (recipe_state.json) ────────────────────────
# Purely for the UI: last-synced timestamps + last change. NOT used by the diff —
# correctness still reads live Tunarr. Kept in a separate file so it never races
# with channels.json edits or deploys.

def _load_state() -> dict:
    try:
        with open(DATA_DIR / STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        tmp = DATA_DIR / (STATE_FILE + ".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        tmp.replace(DATA_DIR / STATE_FILE)  # atomic swap — no torn reads
    except Exception:
        pass  # cosmetic state must never break a cycle


# ── Diff helpers ───────────────────────────────────────────────────────────────

def _program_ids(resolved: list) -> set:
    return {p["id"] for item in resolved for p in item["programs"]}


def _id_label_map(movie_map: dict, show_map: dict) -> dict:
    """Map each program id -> a human label (movie title, or show title for episodes)."""
    labels: dict = {}
    for p in movie_map.values():
        labels[p["id"]] = p.get("program", {}).get("title", "(movie)")
    for s in show_map.values():
        for p in s.get("programs", []):
            labels[p["id"]] = s["title"]
    return labels


def _summarize(ids: set, id_label: dict) -> list:
    """Render a set of program ids as ['Title', 'Show x8'] (dedup + counts)."""
    counts = Counter(id_label.get(i, "(unknown)") for i in ids)
    return [lbl + (f" x{n}" if n > 1 else "") for lbl, n in counts.most_common()]


# ── The cycle ──────────────────────────────────────────────────────────────────

def _run_cycle_blocking(apply: bool, only: int = None) -> dict:
    """Synchronous body of one cycle. Runs in a worker thread.

    `only` (a channel number) limits the cycle to a single live channel — used by
    the per-channel "Sync now" button. The full library index is still built once
    regardless, so this is about scope, not speed.
    """
    started = datetime.now(timezone.utc)
    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")

    summary: dict = {
        "time": started.isoformat(),
        "apply": apply,
        "live": 0,
        "changed": 0,
        "changes": [],
        "skipped": [],
        "error": None,
    }

    if not tunarr_url:
        summary["error"] = "Tunarr not configured"
        return summary

    live = _live_channels(_load_channels())
    if only is not None:
        live = [ch for ch in live if ch.get("number") == only]
    summary["live"] = len(live)
    if not live:
        return summary

    try:
        movie_map, show_map, id_index = channel_engine.build_library_index_with_ids(tunarr_url)
    except channel_engine.ChannelEngineError as e:
        summary["error"] = f"library index failed: {e}"
        return summary

    id_label = _id_label_map(movie_map, show_map)

    # Plex section lookup only if a live channel uses collection refs
    plex_sections, collection_cache = [], {}
    if any(isinstance(it, dict) and "collection" in it
           for ch in live for it in ch.get("content", [])):
        if plex_url and plex_token:
            plex_sections = channel_engine.get_plex_sections(plex_url, plex_token)

    # Franchise index only if a live channel uses franchise refs
    franchise_index = {}
    if any(isinstance(it, dict) and it.get("match") == "franchise"
           for ch in live for it in ch.get("content", [])):
        franchise_index = channel_engine.load_franchise_index(DATA_DIR)

    for ch in live:
        number = ch.get("number")
        name = ch.get("name", "Unnamed")
        report = {}
        resolved, _missing = channel_engine.resolve_content(
            ch.get("content", []), movie_map, show_map,
            plex_url=plex_url, plex_token=plex_token,
            plex_sections=plex_sections, collection_cache=collection_cache,
            franchise_index=franchise_index, id_index=id_index, report=report,
        )
        if apply:  # a dry run isn't a real check, so it must not rewrite what the person sees
            notices.record(DATA_DIR, number, report)
        fresh_ids = _program_ids(resolved)

        tch = channel_engine.find_channel_by_number(tunarr_url, number)
        if not tch:
            summary["skipped"].append({"number": number, "name": name, "reason": "not in Tunarr"})
            continue
        # Guard the by-number scramble: if the Tunarr channel at this number is not the
        # one our record names, channels.json has drifted out of sync with Tunarr — skip
        # rather than overwrite the wrong channel. (See channel_engine.update_channel_in_place.)
        if (tch.get("name") or "").strip().lower() != name.strip().lower():
            summary["skipped"].append({
                "number": number, "name": name,
                "reason": (f"name mismatch — Tunarr #{number} is '{tch.get('name')}', "
                           f"record expects '{name}'; refusing to overwrite (out of sync)")})
            continue
        cur_ids = channel_engine.read_channel_programming(tunarr_url, tch["id"])
        if cur_ids is None:
            summary["skipped"].append({"number": number, "name": name, "reason": "could not read programming"})
            continue

        if fresh_ids == cur_ids:
            continue  # no change — cheap no-op, no guide churn

        added, removed = fresh_ids - cur_ids, cur_ids - fresh_ids
        change = {
            "number": number,
            "name": name,
            "added": _summarize(added, id_label),
            "added_count": len(added),
            "removed_count": len(removed),
            "applied": False,
        }
        if apply:
            if not fresh_ids:
                # resolves to nothing — refuse to wipe the channel; flag it
                summary["skipped"].append({"number": number, "name": name, "reason": "resolved to empty — not patched"})
                continue
            try:
                # Preserve the commercial gap on live channels (filler stays attached;
                # only the pad needs re-applying each cycle).
                comm = ch.get("commercials") or {}
                pad_ms = int(comm.get("pad_minutes", 5)) * 60000 if comm.get("filler_list_id") else 0
                channel_engine.update_channel_in_place(
                    tunarr_url, number, ch.get("shuffle", "shuffle"), resolved,
                    pad_ms=pad_ms, expected_name=name, playback=ch.get("playback"))
                change["applied"] = True
            except channel_engine.ChannelEngineError as e:
                summary["skipped"].append({"number": number, "name": name, "reason": str(e)})
                continue
        summary["changes"].append(change)

    summary["changed"] = len(summary["changes"])
    _write_log(summary)

    # Record cosmetic per-channel sync metadata (apply cycles only — a dry run
    # isn't a real "sync"). Carries forward prior change info for unchanged channels.
    if apply:
        state = _load_state()
        changed_by_num = {c["number"]: c for c in summary["changes"] if c.get("applied")}
        for ch in live:
            num = str(ch.get("number"))
            entry = state.get(num, {})
            entry["checked_at"] = summary["time"]
            c = changed_by_num.get(ch.get("number"))
            if c:
                entry["changed_at"] = summary["time"]
                entry["change_summary"] = (
                    f"+{c['added_count']}" + (f" −{c['removed_count']}" if c["removed_count"] else "")
                )
            state[num] = entry
        if only is None:
            # Full cycle: drop entries for channels that are no longer live
            live_nums = {str(ch.get("number")) for ch in live}
            state = {k: v for k, v in state.items() if k in live_nums}
        _save_state(state)

    return summary


def _write_log(summary: dict) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "recipes.log", "a", encoding="utf-8") as f:
            mode = "apply" if summary["apply"] else "dry-run"
            f.write(f"[{summary['time']}] cycle {mode} live={summary['live']} "
                    f"changed={summary['changed']}"
                    + (f" error={summary['error']}" if summary['error'] else "") + "\n")
            for c in summary["changes"]:
                tag = "applied" if c["applied"] else "would change"
                added = (": +" + ", ".join(c["added"])) if c["added"] else ""
                f.write(f"    #{c['number']} {c['name']} [{tag}] "
                        f"+{c['added_count']} -{c['removed_count']}{added}\n")
            for s in summary["skipped"]:
                f.write(f"    skip #{s['number']} {s['name']}: {s['reason']}\n")
    except Exception:
        pass  # logging must never break a cycle


async def run_cycle(apply: bool = True, only: int = None) -> dict:
    """Run one cycle under the deploy lock. Blocking work offloaded to a thread."""
    async with deploy_lock:
        _state["running"] = True
        try:
            summary = await asyncio.to_thread(_run_cycle_blocking, apply, only)
        finally:
            _state["running"] = False
    _state["last_cycle"] = summary
    return summary


# ── Background loop ────────────────────────────────────────────────────────────

async def scheduler_loop() -> None:
    """Wake every minute; run a cycle when enabled, not paused, and interval elapsed.

    Checking every 60s (rather than sleeping the full interval) lets config toggles
    — recipes_enabled, recipe_interval_hours — and the runtime pause take effect
    within a minute, mirroring how the auth middleware re-reads config each request.
    """
    while True:
        try:
            cfg = _load_config()
            enabled = bool(cfg.get("recipes_enabled", False))
            interval_h = float(cfg.get("recipe_interval_hours", DEFAULT_INTERVAL_HOURS) or DEFAULT_INTERVAL_HOURS)
            interval_s = max(60.0, interval_h * 3600.0)
            now = time.time()  # wall clock — comparable from the status handler too
            last = _state["last_auto_run"]
            due = last is None or (now - last) >= interval_s
            if enabled and not _state["paused"] and due:
                _state["last_auto_run"] = now
                await run_cycle(apply=True)
        except Exception as e:  # never let the loop die
            _write_log({"time": datetime.now(timezone.utc).isoformat(), "apply": True,
                        "live": 0, "changed": 0, "changes": [], "skipped": [],
                        "error": f"loop error: {e}"})
        await asyncio.sleep(60)


# ── Status (for the recipes router / UI) ───────────────────────────────────────

def get_status() -> dict:
    cfg = _load_config()
    enabled = bool(cfg.get("recipes_enabled", False))
    interval_h = float(cfg.get("recipe_interval_hours", DEFAULT_INTERVAL_HOURS) or DEFAULT_INTERVAL_HOURS)

    # Seconds until the next automatic cycle (null if disabled/paused). last_auto_run
    # is wall-clock, so this is safe to compute from the sync status handler.
    next_run_seconds = None
    if enabled and not _state["paused"]:
        last = _state["last_auto_run"]
        next_run_seconds = 0 if last is None else max(0.0, last + interval_h * 3600.0 - time.time())

    return {
        "enabled": enabled,
        "paused": _state["paused"],
        "running": _state["running"],
        "interval_hours": interval_h,
        "next_run_seconds": next_run_seconds,
        "live_count": len(_live_channels(_load_channels())),
        "last_cycle": _state["last_cycle"],
        "channels": _load_state(),
    }


def set_paused(paused: bool) -> dict:
    _state["paused"] = bool(paused)
    return get_status()
