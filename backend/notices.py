"""Per-channel notices: what a channel's last resolve could not find, and what it found
again through a backup number.

Without this a title that no longer matches simply vanishes from the channel and nobody is
told. Kept in its own small file, not in channels.json — that file is the person's own
data, and a notice is a transient report about it. Every write is best-effort: a notice is
information, so failing to save one must never fail a deploy.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

FILE = "channel_notices.json"
MAX_SHOWN = 25  # a collection with 300 unsynced members shouldn't become a 300-line warning
MAX_AMBIGUOUS = 50


def load(data_dir) -> dict:
    try:
        with open(Path(data_dir) / FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def summarize(report) -> dict | None:
    """The notice for one channel from a resolve_content `report`; None when it is clean."""
    missing = (report or {}).get("missing", [])
    healed = (report or {}).get("healed", [])
    ambiguous = (report or {}).get("ambiguous", [])
    if not missing and not healed and not ambiguous:
        return None
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "missing_count": len(missing),
        "missing": [{"label": str(m["label"])[:120], "why": m["why"]} for m in missing[:MAX_SHOWN]],
        "healed_count": len(healed),
        "ambiguous_count": len(ambiguous),
        "ambiguous": ambiguous[:MAX_AMBIGUOUS],
    }


def record(data_dir, number, report) -> dict | None:
    """Replace this channel's notice with what its latest resolve found (or clear it when
    the resolve was clean). Returns the notice, or None."""
    notice = summarize(report)
    try:
        state = load(data_dir)
        if notice is None:
            if state.pop(str(number), None) is None:
                return None  # nothing was there, nothing to write
        else:
            state[str(number)] = notice
        path = Path(data_dir) / FILE
        tmp = path.with_name(FILE + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic swap — a reader never sees half a file
    except OSError:
        pass
    return notice
