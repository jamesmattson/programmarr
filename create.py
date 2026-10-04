#!/usr/bin/env python3
"""
create.py — Create Tunarr channels from channels.json.

Reads channels.json (output from LLM or generate_no_ai.py), deletes all
existing Tunarr channels, and creates fresh ones with rolling-loop schedules.

Usage:
    python create.py                        # reads channels.json
    python create.py --json myfile.json     # use alternate file
    python create.py --probe                # dry run, no changes
    python create.py --no-delete            # create without deleting existing
"""

import argparse
import glob
import gzip
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from channel_engine import (
    ChannelEngineError,
    set_tunarr_auth_from_config,
    SHUFFLE_MAP,
    api,
    build_library_index_with_ids,
    build_schedule,
    get_plex_sections,
    get_transcode_config,
    load_franchise_index,
    resolve_content,
    set_programming,
)

CONFIG_FILE = "config.json"
DEFAULT_CHANNELS_FILE = "channels.json"


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {CONFIG_FILE} not found.")
        sys.exit(1)


# ── Destructive-op backup ──────────────────────────────────────────────────────

# Measured on a real 156-channel Tunarr: the uncompressed snapshot was 151 MB
# (full program metadata for ~1000 programs per channel). Ten of those would put
# 1.5 GB in a user's data directory, so backups are gzipped (10x, ~1.4s) and we
# keep fewer of them. 3 x ~15 MB is a safety net; 10 x 151 MB is a disk problem.
BACKUP_KEEP = 3
BACKUP_GLOB = "tunarr_backup_*.json.gz"


def backup_channels(tunarr_url, channels):
    """Dump channels + their raw programming to a timestamped file before deleting.

    This is the only safety net for an irreversible wipe: a user who points
    Programmarr at a Tunarr with a hand-built lineup and hits Deploy would
    otherwise lose it with no way back. We store the raw
    /api/channels/{id}/programming payload (not just program ids) so the file is
    actually enough to rebuild from.

    Never raises — a backup failure must not be the thing that blocks a deploy,
    but it IS reported loudly so the user can decide to stop.
    """
    try:
        snapshot = []
        for ch in channels:
            entry = {"channel": ch}
            prog = api(tunarr_url, "GET", f"/api/channels/{ch['id']}/programming")
            if prog is not None:
                entry["programming"] = prog
            snapshot.append(entry)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"tunarr_backup_{ts}.json.gz"
        # No indent: this file is read by a restore, not by a human, and pretty
        # printing 150 MB of JSON buys nothing.
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump({"saved_at": ts, "tunarr_url": tunarr_url,
                       "channels": snapshot}, f)

        # ponytail: keep the last N by filename (timestamps sort lexically); no
        # rotation config until someone asks for one.
        for stale in sorted(glob.glob(BACKUP_GLOB))[:-BACKUP_KEEP]:
            try:
                os.remove(stale)
            except OSError:
                pass

        size_mb = os.path.getsize(path) / 1e6
        print(f"  Backed up {len(snapshot)} channels ({size_mb:.1f} MB) -> {os.path.abspath(path)}")
        return path
    except Exception as e:
        print(f"  ! WARNING: could not write pre-delete backup: {e}")
        print("  ! Deleting anyway — stop now (Ctrl-C) if you need these channels.")
        return None


# ── Channel operations ─────────────────────────────────────────────────────────

def delete_channels(tunarr_url, probe, from_ch=None, protect=None):
    protect = protect or set()
    existing = api(tunarr_url, "GET", "/api/channels") or []
    if not existing:
        print("  No existing channels to delete")
        return
    in_scope = [ch for ch in existing if from_ch is None or ch.get("number", 0) >= from_ch]
    targets = [ch for ch in in_scope if ch.get("number", 0) not in protect]
    preserved = [ch for ch in in_scope if ch.get("number", 0) in protect]
    if not targets and not preserved:
        print(f"  No channels >= {from_ch} to delete")
        return
    scope = f">= #{from_ch}" if from_ch is not None else "all"
    if targets:
        print(f"  Deleting {len(targets)} channels ({scope})...")
        if not probe:
            backup_channels(tunarr_url, targets)
        for ch in targets:
            if probe:
                print(f"    [PROBE] Would delete #{ch['number']} {ch['name']}")
            else:
                result = api(tunarr_url, "DELETE", f"/api/channels/{ch['id']}")
                if result is not None:
                    print(f"    Deleted #{ch['number']} {ch['name']}")
                time.sleep(0.1)
    for ch in preserved:
        print(f"    {'[PROBE] ' if probe else ''}Preserving #{ch['number']} {ch['name']} (protected)")


def create_channel(tunarr_url, number, name, transcode_id, filler_list_id=None,
                   channel_group=None, stream_mode=None):
    channel_id = str(uuid.uuid4())
    # Commercials: attach a filler list at the channel level. Tunarr's FillerPicker
    # fills the schedule's flex gaps (opened by build_schedule's pad_ms) with these
    # clips at playback. Empty list = no commercials (default).
    filler_collections = (
        [{"id": filler_list_id, "weight": 100, "cooldownSeconds": 30}] if filler_list_id else []
    )
    body = {
        "type": "new",
        "channel": {
            "id": channel_id,
            "number": number,
            "name": name,
            "startTime": int(datetime.now(timezone.utc).timestamp() * 1000),
            "duration": 0,
            "groupTitle": channel_group or "tunarr",
            "guideMinimumDuration": 30000,
            "fillerRepeatCooldown": 30000,
            "fillerCollections": filler_collections,
            "disableFillerOverlay": False,
            "transcodeConfigId": transcode_id,
            "streamMode": stream_mode or "hls",
            "stealth": False,
            "subtitlesEnabled": False,
            "icon": {"path": "", "width": 0, "duration": 0, "position": "bottom-right"},
            "offline": {"mode": "pic", "picture": "", "soundtrack": ""},
            "watermark": {
                "enabled": False,
                "width": 10,
                "verticalMargin": 1,
                "horizontalMargin": 1,
                "position": "bottom-right",
                "opacity": 100,
                "animated": False,
                "fixedSize": False,
                "duration": 0,
                "url": "",
            },
            "onDemand": {"enabled": False},
        },
    }
    result = api(tunarr_url, "POST", "/api/channels", body=body)
    if result and "id" not in result:
        result["id"] = channel_id
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Create Tunarr channels from channels.json")
    parser.add_argument("--json", default=DEFAULT_CHANNELS_FILE, help="Channel definition JSON file")
    parser.add_argument("--probe", action="store_true", help="Dry run — show what would be created")
    parser.add_argument("--no-delete", action="store_true", help="Skip deleting existing channels")
    parser.add_argument("--from", dest="from_ch", type=int, default=None, metavar="N",
                        help="Only operate on channels numbered N and above (preserves lower channels)")
    parser.add_argument("--protect", dest="protect", default="", metavar="NUMS",
                        help="Comma-separated channel numbers to protect from deletion")
    args = parser.parse_args()

    cfg = load_config()
    set_tunarr_auth_from_config(cfg)
    tunarr_url = cfg["tunarr_url"].rstrip("/")
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    # Optional advanced config (absent = Tunarr defaults). streamMode is a lowercase
    # enum (hls|hls_slower|mpegts|hls_direct|hls_direct_v2); normalize user input.
    channel_group = cfg.get("tunarr_channel_group") or None
    stream_mode = (cfg.get("tunarr_stream_mode") or "").strip().lower() or None

    # ── Load channel definitions ───────────────────────────────────────────────
    try:
        with open(args.json, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {args.json} not found. Run the LLM step first.")
        sys.exit(1)

    channels = data.get("channels", [])
    if not channels:
        print("ERROR: No channels found in JSON")
        sys.exit(1)

    channels.sort(key=lambda c: c.get("number", 999))
    if args.from_ch is not None:
        channels = [c for c in channels if c.get("number", 0) >= args.from_ch]
        print(f"Loaded {len(channels)} channels from {args.json} (filtered to #{args.from_ch}+)")
    else:
        print(f"Loaded {len(channels)} channels from {args.json}")

    # ── Set up Plex collection lookup if needed ────────────────────────────────
    uses_collections = any(
        isinstance(item, dict) and "collection" in item
        for ch in channels
        for item in ch.get("content", [])
    )
    franchise_index = load_franchise_index(".")
    plex_sections = []
    collection_cache = {}
    if uses_collections:
        if not plex_url or not plex_token:
            print("ERROR: plex_url and plex_token required in config.json for collection support")
            sys.exit(1)
        print("\nDiscovering Plex sections for collection lookup...")
        plex_sections = get_plex_sections(plex_url, plex_token)
        print(f"  Found {len(plex_sections)} sections")

    # ── Build library index ────────────────────────────────────────────────────
    print("\nIndexing Tunarr library...")
    try:
        movie_map, show_map, id_index = build_library_index_with_ids(tunarr_url)
    except ChannelEngineError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    transcode_id = get_transcode_config(tunarr_url)
    if not transcode_id and not args.probe:
        print("ERROR: No transcode config found in Tunarr")
        sys.exit(1)

    # ── Delete existing channels ───────────────────────────────────────────────
    protect_set: set[int] = set()
    if args.protect:
        for n in args.protect.split(","):
            try:
                protect_set.add(int(n.strip()))
            except ValueError:
                pass

    if not args.no_delete:
        print("\nDeleting existing channels...")
        delete_channels(tunarr_url, args.probe, from_ch=args.from_ch, protect=protect_set)

    # ── Create channels ────────────────────────────────────────────────────────
    print(f"\n{'[PROBE] ' if args.probe else ''}Creating {len(channels)} channels...")
    stats = {"created": 0, "skipped": 0, "missing_titles": []}

    for ch in channels:
        number = ch.get("number")
        name = ch.get("name", "Unnamed")
        shuffle = SHUFFLE_MAP.get(ch.get("shuffle", "shuffle"), "shuffle")
        content_list = ch.get("content", [])

        # Commercials (optional): attach a filler list + pad episodes to open the gap.
        comm = ch.get("commercials") or {}
        comm_filler = comm.get("filler_list_id")
        comm_pad_ms = int(comm.get("pad_minutes", 5)) * 60000 if comm_filler else 0

        resolved, missing = resolve_content(
            content_list, movie_map, show_map,
            plex_url=plex_url, plex_token=plex_token,
            plex_sections=plex_sections, collection_cache=collection_cache,
            franchise_index=franchise_index, id_index=id_index,
        )

        if not resolved:
            print(f"  SKIP #{number} {name} — no content found in library")
            stats["skipped"] += 1
            if missing:
                stats["missing_titles"].extend([(name, t) for t in missing])
            continue

        if probe := args.probe:
            tv_count = sum(1 for r in resolved if r["type"] == "TV")
            movie_count = sum(1 for r in resolved if r["type"] == "Movie")
            ep_count = sum(len(r["programs"]) for r in resolved if r["type"] == "TV")
            comm_note = f" | commercials ({comm.get('pad_minutes', 5)}m gaps)" if comm_filler else ""
            print(f"  [PROBE] #{number} {name} | shuffle={shuffle} | "
                  f"{tv_count} shows ({ep_count} eps) + {movie_count} movies{comm_note}")
            if missing:
                print(f"    Missing: {', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}")
            stats["created"] += 1
            continue

        # Create channel
        ch_result = create_channel(tunarr_url, number, name, transcode_id, filler_list_id=comm_filler,
                                   channel_group=channel_group, stream_mode=stream_mode)
        if not ch_result:
            print(f"  FAIL #{number} {name} — channel creation failed")
            stats["skipped"] += 1
            continue

        channel_id = ch_result.get("id")

        # Build and post schedule (pad opens the commercial gap when enabled)
        schedule = build_schedule(shuffle, resolved, pad_ms=comm_pad_ms, playback=ch.get("playback"))
        if not schedule:
            print(f"  FAIL #{number} {name} — could not build schedule")
            stats["skipped"] += 1
            continue

        prog_result = set_programming(tunarr_url, channel_id, schedule)
        if prog_result is not None:
            ep_count = sum(len(r["programs"]) for r in resolved)
            print(f"  Created #{number} {name} ({ep_count} programs, shuffle={shuffle})")
            if missing:
                print(f"    Not found: {', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}")
            stats["created"] += 1
        else:
            print(f"  FAIL #{number} {name} — programming failed")
            stats["skipped"] += 1

        time.sleep(0.2)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'[PROBE] ' if args.probe else ''}Done: {stats['created']} created, {stats['skipped']} skipped")

    if stats["missing_titles"]:
        print(f"\nTitles not found in Tunarr library ({len(stats['missing_titles'])} total):")
        for channel_name, title in stats["missing_titles"][:20]:
            print(f"  [{channel_name}] {title}")
        if len(stats["missing_titles"]) > 20:
            print(f"  ... and {len(stats['missing_titles']) - 20} more")


if __name__ == "__main__":
    main()
