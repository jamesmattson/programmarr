#!/usr/bin/env python3
"""
fetch_images.py — set every channel's Tunarr icon.

Verified TMDB logos where a lookup is trustworthy, generated badge art
everywhere else (see icon_engine.icon_attempts for the policy):

  * solo-title channels and marathon/franchise/network/studio kinds try a
    VERIFIED TMDB search — the result's name must equal the query after
    normalization. Never results[0] on faith.
  * every other kind (genre, decade, mood, theme, ...) and any TMDB miss
    gets a badge (badge_renderer) uploaded via Tunarr POST /api/upload/image.
  * channels pinned from the Channels editor ("icon": {"pinned": true} in
    channels.json) are skipped. This script NEVER writes channels.json.

"tmdb_api_key" in config.json is OPTIONAL — without it everything badges.

Usage:
    python fetch_images.py              # dry run — shows what would be set
    python fetch_images.py --apply      # actually update Tunarr
    python fetch_images.py --channel 10 [--apply]
    python fetch_images.py --clear      # remove all custom icons
"""

import argparse
import json
import sys
import time
import uuid

import badge_renderer
import channel_engine
import icon_engine

CONFIG_FILE = "config.json"
DEFAULT_CHANNELS_FILE = "channels.json"
PLANNER_STATE_FILE = "planner_state.json"


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {CONFIG_FILE} not found.")
        sys.exit(1)
    if not cfg.get("tunarr_url"):
        print("ERROR: 'tunarr_url' not found in config.json.")
        sys.exit(1)
    return cfg


def get_tunarr_channels(tunarr_url):
    """number -> full channel object for every Tunarr channel."""
    channels = icon_engine.http_get(f"{tunarr_url}/api/channels", timeout=30, tunarr=True) or []
    by_number = {}
    for ch in channels:
        full = icon_engine.get_full_channel(tunarr_url, ch["id"])
        if full:
            by_number[ch["number"]] = full
    return by_number


def run_clear(tunarr_url, apply):
    print("Fetching Tunarr channels...")
    cleared = 0
    for number, tch in sorted(get_tunarr_channels(tunarr_url).items()):
        if not tch.get("icon", {}).get("path"):
            continue
        label = f"#{number} {tch['name']}"
        if apply:
            ok = icon_engine.clear_tunarr_channel_icon(tunarr_url, tch)
            print(f"  {'Cleared' if ok else 'FAIL  '} {label}")
            cleared += 1 if ok else 0
        else:
            print(f"  [DRY RUN] Would clear {label}")
            cleared += 1
    print(f"\nDone: {cleared} icons {'cleared' if apply else 'would be cleared'}")


def main():
    parser = argparse.ArgumentParser(
        description="Set Tunarr channel icons: verified TMDB logos + generated badges")
    parser.add_argument("--json", default=DEFAULT_CHANNELS_FILE, help="channels.json file")
    parser.add_argument("--apply", action="store_true",
                        help="Actually update Tunarr (default is dry run)")
    parser.add_argument("--channel", type=int, help="Process only this channel number")
    parser.add_argument("--clear", action="store_true",
                        help="Remove all custom icons, reset to Tunarr default")
    args = parser.parse_args()

    cfg = load_config()
    channel_engine.set_tunarr_auth_from_config(cfg)
    tunarr_url = cfg["tunarr_url"].rstrip("/")
    tmdb_key = cfg.get("tmdb_api_key", "")

    if not args.apply:
        print("DRY RUN — pass --apply to update Tunarr\n")
    if args.clear:
        run_clear(tunarr_url, args.apply)
        return
    if not tmdb_key:
        print("NOTE: no tmdb_api_key in config.json — every channel gets a "
              "generated badge (no TMDB logo lookups).\n")

    try:
        with open(args.json, encoding="utf-8") as f:
            channels_def = json.load(f).get("channels", [])
    except FileNotFoundError:
        print(f"ERROR: {args.json} not found.")
        sys.exit(1)

    if args.channel:
        channels_def = [c for c in channels_def if c.get("number") == args.channel]
        if not channels_def:
            print(f"Channel #{args.channel} not found in {args.json}")
            sys.exit(1)

    hints = icon_engine.load_spec_hints(PLANNER_STATE_FILE)
    print(f"Processing {len(channels_def)} channel(s)\n")
    print("Fetching Tunarr channels...")
    tunarr_chs = get_tunarr_channels(tunarr_url)
    print()

    stats = {"tmdb": 0, "badge": 0, "pinned": 0, "no_channel": 0, "failed": 0}
    verb = "Set" if args.apply else "[DRY RUN] Would set"

    for ch_def in channels_def:
        number = ch_def.get("number")
        name = (ch_def.get("name") or "").strip()

        if (ch_def.get("icon") or {}).get("pinned"):
            print(f"  PIN  #{number} {name} — user-pinned icon, skipping")
            stats["pinned"] += 1
            continue

        tch = tunarr_chs.get(number)
        if not tch:
            print(f"  SKIP #{number} {name} — not found in Tunarr (not deployed yet?)")
            stats["no_channel"] += 1
            continue

        spec = hints.get(name.lower(), {})
        kind = spec.get("kind")

        logo_url = None
        if tmdb_key:
            attempts = icon_engine.icon_attempts(ch_def, kind)
            if attempts:
                logo_url = icon_engine.resolve_tmdb_logo(attempts, tmdb_key)
                time.sleep(0.25)  # be polite to TMDB rate limits

        if logo_url:
            ok = icon_engine.set_tunarr_channel_icon(tunarr_url, tch, logo_url) \
                if args.apply else True
            if ok:
                print(f"  #{number} {name} — {verb} TMDB logo")
                print(f"    {logo_url}")
            else:
                print(f"  #{number} {name} — FAILED to update Tunarr")
            stats["tmdb" if ok else "failed"] += 1
            continue

        # Badge path — the universal fallback.
        label = f"badge ({kind or 'generic'})"
        if args.apply:
            png = badge_renderer.render_badge(name, kind=kind,
                                              genre=icon_engine.spec_genre(spec))
            try:
                badge_url = icon_engine.upload_image_to_tunarr(
                    tunarr_url, png,
                    f"programmarr-ch{number}-{uuid.uuid4().hex[:8]}.png")
                ok = icon_engine.set_tunarr_channel_icon(tunarr_url, tch, badge_url)
            except Exception as e:
                print(f"  #{number} {name} — FAILED badge upload: {e}")
                stats["failed"] += 1
                continue
        else:
            ok = True
        if ok:
            print(f"  #{number} {name} — {verb} {label}")
        else:
            print(f"  #{number} {name} — FAILED to update Tunarr")
        stats["badge" if ok else "failed"] += 1

    print(f"\n{'Applied' if args.apply else 'Dry run'}: "
          f"{stats['tmdb']} TMDB logos, {stats['badge']} badges, "
          f"{stats['pinned']} pinned (skipped), "
          f"{stats['no_channel']} not in Tunarr"
          + (f", {stats['failed']} failed" if stats["failed"] else ""))


if __name__ == "__main__":
    main()
