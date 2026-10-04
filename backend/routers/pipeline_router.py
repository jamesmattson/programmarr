import asyncio
import concurrent.futures
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import channel_engine  # noqa: E402
import scheduler  # noqa: E402  (backend/ on sys.path) — shared deploy_lock

# scheduler's import added SCRIPTS_DIR to sys.path, so the pure pipeline modules
# at the repo root (e.g. channel_blocks) are importable in-process here.
if str(Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent.parent))) not in sys.path:
    sys.path.insert(0, str(Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent.parent))))
import channel_blocks  # noqa: E402

router = APIRouter()
DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))
SCRIPTS_DIR = Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent.parent))
LOGS_DIR = DATA_DIR / "logs"

# ── TMDB enrichment scan state ────────────────────────────────────────────────
# Module-level, shared across all requests.  A threading.Lock guards all reads
# and writes so concurrent HTTP calls never see a half-updated dict.
_tmdb_scan_lock = threading.Lock()
_tmdb_scan_state: dict = {
    "running": False,
    "scanned": 0,
    "total": 0,
    "done": False,
    "sig": None,          # library signature the last scan was for
}

# ── TVmaze network scan state ─────────────────────────────────────────────────
# Mirrors the TMDB scan pattern.  Keyed by library signature so stale caches
# are never reused after a new export.
_tvmaze_scan_lock = threading.Lock()
_tvmaze_scan_state: dict = {
    "running": False,
    "scanned": 0,
    "total": 0,
    "done": False,
    "sig": None,
}

# ── Wikidata franchise scan state ─────────────────────────────────────────────
# Mirrors the TMDB scan pattern.  Keyed by library signature.
# Wikidata is the lowest-certainty source; failures degrade to TMDB-only.
_wikidata_scan_lock = threading.Lock()
_wikidata_scan_state: dict = {
    "running": False,
    "done": False,
    "sig": None,
}


def _load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def _plex_get(base_url: str, token: str, path: str, timeout: int = 30):
    sep = "&" if "?" in path else "?"
    url = f"{base_url}{path}{sep}X-Plex-Token={token}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class CollectionSelection(BaseModel):
    name: str
    channel_number: int
    include: bool


class DeploySelection(BaseModel):
    original_number: int
    deploy_number: int
    include: bool


def _env():
    env = os.environ.copy()
    env["PROGRAMMARR_DATA"] = str(DATA_DIR)
    # Force the child's stdout/stderr to UTF-8. On a Windows host the default is the
    # locale codec (cp1252), so a script printing a non-cp1252 title (e.g. a "⧸" in an
    # unsynced-title list) dies with UnicodeEncodeError. _stream decodes as UTF-8, so
    # this makes the pipe round-trip cleanly. No-op on Linux/Docker (already UTF-8).
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _stream(script: str, args: list[str], tag: str) -> AsyncGenerator[str, None]:
    LOGS_DIR.mkdir(exist_ok=True)
    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + args
    log_path = LOGS_DIR / f"{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    yield f"data: {json.dumps({'type': 'start', 'cmd': ' '.join(cmd), 'log': log_path.name})}\n\n"

    lines: list[str] = []

    # The browser waits for a 'done' event to stop showing a running job. Anything
    # that raises AFTER the response has started (a missing interpreter, a bad
    # SCRIPTS_DIR, an unwritable log dir) kills the generator mid-stream and the UI
    # hangs forever with no error and no return code — so every failure below must
    # still end in 'done'.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(DATA_DIR),
            env=_env(),
        )
    except Exception as e:
        msg = f"Could not start {script}: {e}"
        yield f"data: {json.dumps({'type': 'line', 'text': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'returncode': -1, 'error': msg})}\n\n"
        return

    try:
        async for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.decode("utf-8", errors="replace").rstrip()
            lines.append(line)
            yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"
        await proc.wait()
    except Exception as e:
        msg = f"{script} stream failed: {e}"
        yield f"data: {json.dumps({'type': 'line', 'text': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'returncode': -1, 'error': msg})}\n\n"
        return

    log_name = log_path.name
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"# {tag} — {datetime.now().isoformat()}\n")
            f.write(f"# {' '.join(cmd)}\n\n")
            f.write("\n".join(lines))
    except Exception as e:
        # A log we couldn't write must not sink a run that actually succeeded.
        log_name = ""
        yield f"data: {json.dumps({'type': 'line', 'text': f'! Could not write log: {e}'})}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'returncode': proc.returncode, 'log': log_name})}\n\n"


def _sse(gen: AsyncGenerator[str, None]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _locked_stream(script: str, args: list[str], tag: str) -> AsyncGenerator[str, None]:
    """Stream a subprocess while holding the shared deploy_lock for its full duration.

    Used for create.py runs (probe/deploy) so the live-channel scheduler can't patch
    a channel in the middle of a deploy deleting/recreating it (and vice versa).
    """
    async with scheduler.deploy_lock:
        async for chunk in _stream(script, args, tag):
            yield chunk


class ExportOptions(BaseModel):
    no_crossref: bool = False
    movie_sections: Optional[list[str]] = None   # primary Plex section keys; None = auto, [] = skip
    tv_sections: Optional[list[str]] = None
    tunarr_movie_libs: Optional[list[str]] = None  # Tunarr library UUIDs for extra sources
    tunarr_tv_libs: Optional[list[str]] = None


@router.get("/pipeline/libraries")
def list_libraries():
    cfg = _load_config()
    plex_url   = cfg.get("plex_url", "").rstrip("/").lower()
    plex_token = cfg.get("plex_token", "")
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not plex_url or not plex_token:
        raise HTTPException(400, "Plex not configured")

    result = []
    plex_error = ""

    # ── Primary Plex sections (via Plex API, key prefixed "plex:") ─────────────
    srv_name = (cfg.get("plex_servers") or [{}])[0].get("name", "Your Library")
    try:
        data = _plex_get(plex_url, plex_token, "/library/sections")
        for s in data["MediaContainer"].get("Directory", []):
            if s.get("type") not in ("movie", "show"):
                continue
            result.append({"key": f"plex:{s['key']}", "title": s["title"],
                            "type": s["type"], "server": srv_name})
    except urllib.error.HTTPError as e:
        plex_error = (f"Plex rejected the token ({e.code}) — check plex_token in Settings."
                      if e.code in (401, 403) else f"Plex returned HTTP {e.code}.")
    except Exception as e:
        # Was `pass` with a comment claiming the UI shows the error elsewhere; it
        # doesn't — the user just got an empty picker with no explanation.
        plex_error = f"Could not reach Plex at {plex_url}: {e}"

    # ── All other Tunarr-connected sources (key prefixed "tunarr:") ────────────
    if tunarr_url:
        for source in (channel_engine.api(tunarr_url, "GET", "/api/media-sources") or []):
            # Skip the primary Plex — its sections are already listed above.
            if source.get("type") == "plex" and source.get("uri", "").rstrip("/").lower() == plex_url:
                continue
            source_name = source.get("name", source.get("type", "Unknown"))
            for lib in source.get("libraries", []):
                if not lib.get("enabled"):
                    continue
                media_type = lib.get("mediaType", "")
                lib_type = ("movie" if media_type in ("movie", "movies")
                            else "show" if media_type == "shows" else None)
                if not lib_type:
                    continue
                result.append({"key": f"tunarr:{lib['id']}", "title": lib.get("name", lib["id"]),
                                "type": lib_type, "server": source_name})

    # Nothing to show AND a known reason: say the reason instead of rendering an
    # empty picker the user can't act on.
    if not result and plex_error:
        raise HTTPException(502, plex_error)

    return result


@router.post("/pipeline/export")
async def run_export(opts: ExportOptions = ExportOptions()):
    args = []
    if opts.no_crossref:
        args.append("--no-crossref")
    if opts.movie_sections is not None:
        args += ["--movie-sections", ",".join(opts.movie_sections)]
    if opts.tv_sections is not None:
        args += ["--tv-sections", ",".join(opts.tv_sections)]
    if opts.tunarr_movie_libs is not None:
        args += ["--tunarr-movie-libs", ",".join(opts.tunarr_movie_libs)]
    if opts.tunarr_tv_libs is not None:
        args += ["--tunarr-tv-libs", ",".join(opts.tunarr_tv_libs)]
    return _sse(_stream("export.py", args, "export"))


@router.get("/pipeline/csv")
def download_csv():
    p = DATA_DIR / "plex_library.csv"
    if not p.exists():
        raise HTTPException(404, "Run Export first")
    return FileResponse(str(p), filename="plex_library.csv", media_type="text/csv")


@router.get("/pipeline/csv/info")
def csv_info():
    import csv as _csv
    p = DATA_DIR / "plex_library.csv"
    if not p.exists():
        return {"exists": False}
    stat = p.stat()
    rows, movies, tv_shows, preview = 0, 0, 0, []
    try:
        with open(p, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < 21:
                    preview.append(line.rstrip())
        with open(p, encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                rows += 1
                t = row.get("Type", "")
                if t == "Movie":
                    movies += 1
                elif t == "TV":
                    tv_shows += 1
    except Exception:
        pass
    result: dict = {
        "exists": True,
        "size": stat.st_size,
        "rows": rows,
        "movies": movies,
        "tv_shows": tv_shows,
        "modified": stat.st_mtime,
        "preview": preview,
    }
    summary_p = DATA_DIR / "export_summary.json"
    if summary_p.exists():
        try:
            with open(summary_p) as f:
                s = json.load(f)
                result["skipped_movies"] = s.get("skipped_movies", 0)
                result["skipped_shows"] = s.get("skipped_shows", 0)
        except Exception:
            pass
    return result


# Canonical movie genres (display, Plex tag) and decade buckets.
# KEEP IN SYNC with generate_no_ai.py CANONICAL_GENRES / DECADE_RANGES.
CANONICAL_GENRES = [
    ("Comedy", "Comedy"), ("Action", "Action"), ("Horror", "Horror"),
    ("Sci-Fi", "Science Fiction"), ("Drama", "Drama"),
    ("Animation", "Animation"), ("Documentary", "Documentary"),
]
# Buckets start in the 1920s: a classic-film library contributed NOTHING to the
# decade facet when this began at 1970 — every pre-1970 title silently fell out of
# _decade_start and the user saw no decade channels for half their collection.
# Only decades that actually have titles are offered, so the extra buckets are free.
# Pre-1970 labels are spelled in full ("1950s") rather than "50s" — "20s" next to
# "2020s" would be genuinely ambiguous in a channel name. The 70s/80s/90s labels
# are left exactly as they were so existing channel names and saved planner state
# keep matching.
DECADE_BUCKETS = [
    ("1920s", 1920, 1929), ("1930s", 1930, 1939), ("1940s", 1940, 1949),
    ("1950s", 1950, 1959), ("1960s", 1960, 1969),
    ("70s", 1970, 1979), ("80s", 1980, 1989), ("90s", 1990, 1999),
    ("2000s", 2000, 2009), ("2010s", 2010, 2019), ("2020s", 2020, 2029),
]


# Planner v2 surface thresholds (min titles for a candidate to be offered).
COMBO_MIN = 6      # genre × decade
BLEND_MIN = 6      # genre ∩ genre
STUDIO_MIN = 4
DIRECTOR_MIN = 3
ACTOR_MIN = 4
TV_GENRE_MIN = 3
TV_MOVIE_MIX_MIN = 3  # minimum on each side for a cross-library mixed-genre candidate
NETWORK_MIN = 3    # minimum TV shows from a network for it to be offered
BLOCK_MIN = 3      # minimum programming-block members present in library
FRANCHISE_MIN = 2  # minimum library members for a TMDB collection to be offered
THEME_MIN = 4      # minimum library movies matching a themed keyword for it to be offered
COUNTRY_MIN = 3    # minimum movies for a country to appear in the facet
MOOD_MIN = 3       # minimum movies for a mood tag to appear in the facet
STYLE_MIN = 3      # minimum movies for a style tag to appear in the facet
ENTITY_CAP = 60    # cap each entity list; UI searches for the long tail


def _safe_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _multi(val):
    return [x.strip() for x in (val or "").split("|") if x.strip()]


def _decade_start(year):
    for _, start, end in DECADE_BUCKETS:
        if year is not None and start <= year <= end:
            return start
    return None


@router.get("/pipeline/facets")
def library_facets(min_items: int = 5):
    """Library facets that drive the Planner v2 candidate list.

    One pass over plex_library.csv yields: genre counts (canonical always returned,
    'more' above min_items), decades present, genre×decade matrix and genre∩genre
    blend counts (movies, above COMBO_MIN/BLEND_MIN), stand-alone entity lists
    (studio/director/actor above their thresholds, capped), TV genre counts for
    blocks, and the marathon-eligible show count.
    """
    import csv as _csv
    from itertools import combinations
    p = DATA_DIR / "plex_library.csv"
    if not p.exists():
        return {"exists": False}

    DECADE_LABEL = {start: label for label, start, _ in DECADE_BUCKETS}
    genre_counts: dict[str, int] = {}
    decade_counts = {start: 0 for _, start, _ in DECADE_BUCKETS}
    studio_counts: dict[str, int] = {}
    director_counts: dict[str, int] = {}
    actor_counts: dict[str, int] = {}
    country_counts: dict[str, int] = {}
    mood_counts: dict[str, int] = {}
    style_counts: dict[str, int] = {}
    tv_genre_counts: dict[str, int] = {}
    movie_recs: list[tuple[list[str], int | None]] = []  # (genres, decade_start) for matrix/blends
    show_recs: list[dict] = []  # per-show marathon candidates
    tv_show_titles: list[str] = []  # TV show titles for TVmaze network lookup
    movies = tv = marathon = 0

    with open(p, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            t = row.get("Type", "")
            if t == "Movie":
                movies += 1
                genres = _multi(row.get("Genres"))
                for g in genres:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
                ds = _decade_start(_safe_int(row.get("Year")))
                if ds is not None:
                    decade_counts[ds] += 1
                for s in _multi(row.get("Studio")):
                    studio_counts[s] = studio_counts.get(s, 0) + 1
                for d in _multi(row.get("Director")):
                    director_counts[d] = director_counts.get(d, 0) + 1
                for a in _multi(row.get("Actors")):
                    actor_counts[a] = actor_counts.get(a, 0) + 1
                for c in _multi(row.get("Country")):
                    country_counts[c] = country_counts.get(c, 0) + 1
                for m in _multi(row.get("Mood")):
                    mood_counts[m] = mood_counts.get(m, 0) + 1
                for st in _multi(row.get("Style")):
                    style_counts[st] = style_counts.get(st, 0) + 1
                movie_recs.append((genres, ds))
            elif t == "TV":
                tv += 1
                for g in _multi(row.get("Genres")):
                    tv_genre_counts[g] = tv_genre_counts.get(g, 0) + 1
                eps = _safe_int(row.get("Episodes")) or 0
                if eps >= 50:
                    marathon += 1
                title = row.get("Title", "")
                if title:
                    tv_show_titles.append(title)
                if title and eps >= 2:
                    show_recs.append({"title": title, "episodes": eps, "seasons": _safe_int(row.get("Seasons")) or 0})

    # Networks facet: derive from TVmaze cache (keyed by library signature).
    # If the cache is absent/stale (scan not done yet), return empty — the frontend
    # kicks the scan on Planner mount and shows a progress hint while it runs.
    sig = _library_signature()
    tvmaze_networks = _load_tvmaze_cache(DATA_DIR, sig)
    network_counts: dict[str, int] = {}
    if tvmaze_networks is not None:
        for title in tv_show_titles:
            net = tvmaze_networks.get(title)
            if net:
                network_counts[net] = network_counts.get(net, 0) + 1

    # Themes facet: derived from the F9 TMDB enrichment cache (no new TMDB calls).
    # Returns [] while the scan is running or if no cache exists yet — the frontend
    # reloads facets after the TMDB scan completes (same pattern as networks).
    enrichment_cache = _load_enrichment_cache(DATA_DIR, sig)
    themed_catalog = _load_themed_keywords()
    themes_facet: list[dict] = []
    if enrichment_cache is not None and themed_catalog:
        themes_facet = _themes_from_enrichment(enrichment_cache, themed_catalog)

    # Genre chips the UI offers: canonical (always) + 'more' above min_items.
    canonical_tags = {tag.lower() for _, tag in CANONICAL_GENRES}
    ci_counts = {tag.lower(): n for tag, n in genre_counts.items()}
    canonical = [
        {"display": disp, "tag": tag, "count": ci_counts.get(tag.lower(), 0)}
        for disp, tag in CANONICAL_GENRES
    ]
    more = sorted(
        ({"display": tag, "tag": tag, "count": n}
         for tag, n in genre_counts.items()
         if tag.lower() not in canonical_tags and n >= min_items),
        key=lambda x: (-x["count"], x["tag"].lower()),
    )
    shown = canonical + more
    display_of = {g["tag"]: g["display"] for g in shown}
    shown_tags = set(display_of)

    # Genre × decade matrix and genre ∩ genre blends, restricted to shown genres.
    gd_counts: dict[tuple[str, int], int] = {}
    pair_counts: dict[tuple[str, str], int] = {}
    for genres, ds in movie_recs:
        present = sorted({g for g in genres if g in shown_tags})
        if ds is not None:
            for g in present:
                gd_counts[(g, ds)] = gd_counts.get((g, ds), 0) + 1
        for a, b in combinations(present, 2):
            pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1

    genre_decade = sorted(
        ({"genre": g, "display": display_of[g], "decade_start": ds,
          "decade_label": DECADE_LABEL[ds], "count": n}
         for (g, ds), n in gd_counts.items() if n >= COMBO_MIN),
        key=lambda x: (x["decade_start"], -x["count"]),
    )
    blends = sorted(
        ({"genres": [a, b], "displays": [display_of[a], display_of[b]], "count": n}
         for (a, b), n in pair_counts.items() if n >= BLEND_MIN),
        key=lambda x: -x["count"],
    )

    def entity_list(counts, floor):
        return sorted(
            ({"value": v, "count": n} for v, n in counts.items() if v and n >= floor),
            key=lambda x: (-x["count"], x["value"].lower()),
        )[:ENTITY_CAP]

    decades = [
        {"label": label, "start": start, "end": end, "count": decade_counts[start]}
        for label, start, end in DECADE_BUCKETS if decade_counts[start] > 0
    ]
    tv_genres = sorted(
        ({"genre": g, "count": n} for g, n in tv_genre_counts.items() if n >= TV_GENRE_MIN),
        key=lambda x: (-x["count"], x["genre"].lower()),
    )
    marathons = sorted(show_recs, key=lambda s: -s["episodes"])

    # Cross-library genres: genres present in BOTH movies and TV above their respective floors.
    # Shape: [{"genre": "Comedy", "tv_count": N, "movie_count": M}] sorted by tv_count+movie_count desc.
    tv_movie_genres = sorted(
        (
            {"genre": g, "tv_count": tv_genre_counts[g], "movie_count": genre_counts.get(g, 0)}
            for g in tv_genre_counts
            if tv_genre_counts[g] >= TV_MOVIE_MIX_MIN and genre_counts.get(g, 0) >= TV_MOVIE_MIX_MIN
        ),
        key=lambda x: -(x["tv_count"] + x["movie_count"]),
    )

    return {
        "exists": True,
        "movies": movies,
        "tv_shows": tv,
        "marathon_count": marathon,
        "min_items": min_items,
        "genres": {"canonical": canonical, "more": more},
        "decades": decades,
        "genre_decade": genre_decade,
        "blends": blends,
        "studios": entity_list(studio_counts, STUDIO_MIN),
        "directors": entity_list(director_counts, DIRECTOR_MIN),
        "actors": entity_list(actor_counts, ACTOR_MIN),
        "tv_genres": tv_genres,
        "marathons": marathons,
        "tv_movie_genres": tv_movie_genres,
        "networks": entity_list(network_counts, NETWORK_MIN),
        "themes": themes_facet,
        "countries": entity_list(country_counts, COUNTRY_MIN),
        "moods": entity_list(mood_counts, MOOD_MIN),
        "styles": entity_list(style_counts, STYLE_MIN),
    }


@router.get("/pipeline/programming-blocks")
def get_programming_blocks():
    """Return programming-block catalog entries that have >= BLOCK_MIN shows in the library.

    Each returned block includes the subset of its `shows` list that are present
    in the TV library (case-insensitive title match) and a `present_count`.
    Blocks with fewer than BLOCK_MIN matches are omitted.

    The catalog is read from SCRIPTS_DIR/programming_blocks.json (repo root in dev,
    /app in Docker), matching how other root-level pure-data files are located.
    """
    import csv as _csv
    catalog_path = SCRIPTS_DIR / "programming_blocks.json"
    if not catalog_path.exists():
        raise HTTPException(404, "programming_blocks.json not found")
    with open(catalog_path, encoding="utf-8") as f:
        catalog = json.load(f)

    # Build a lower-cased set of TV show titles from the library.
    csv_path = DATA_DIR / "plex_library.csv"
    if not csv_path.exists():
        return []
    show_titles: dict[str, str] = {}  # lower → canonical title
    with open(csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("Type") == "TV":
                t = row.get("Title", "")
                if t:
                    show_titles[t.lower()] = t

    results = []
    for block in catalog:
        present = [show_titles[s.lower()] for s in block.get("shows", []) if s.lower() in show_titles]
        if len(present) >= BLOCK_MIN:
            results.append({
                "name": block["name"],
                "era": block.get("era", ""),
                "network": block.get("network", ""),
                "shows": block.get("shows", []),
                "present_shows": present,
                "present_count": len(present),
            })
    return results


def _normalize_name(name: str) -> str:
    """Lower-case, strip punctuation/articles for de-dupe comparisons."""
    n = name.lower().strip()
    n = re.sub(r"['\"\-:,.]", "", n)
    for prefix in ("the ", "a ", "an "):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n.strip()


def _library_title_set(movies: list[dict], shows: list[dict]) -> dict[str, dict]:
    """Lower-cased title → row, for fast membership checks."""
    index: dict[str, dict] = {}
    for row in movies + shows:
        t = row.get("Title", "")
        if t:
            index[t.lower()] = row
    return index


def _library_signature() -> str:
    """Return a cheap fingerprint of plex_library.csv (mtime + size) for cache keying."""
    p = DATA_DIR / "plex_library.csv"
    try:
        st = p.stat()
        return f"{st.st_mtime:.0f}-{st.st_size}"
    except Exception:
        return "missing"


def _tmdb_get(path: str, api_key: str, timeout: int = 10):
    """Single TMDB API call. Raises on network / non-200 errors."""
    sep = "&" if "?" in path else "?"
    url = f"https://api.themoviedb.org/3{path}{sep}api_key={api_key}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _enrich_movie(movie_row: dict, tmdb_key: str) -> dict | None:
    """Fetch TMDB details (collection + keywords) for one library movie.

    Returns a dict:
        {"title": str, "year": int|None,
         "collection": {"id": int, "name": str} | None,
         "keywords": [{"id": int, "name": str}]}

    Returns None on any TMDB failure (caller skips the movie).
    """
    title = movie_row.get("Title", "")
    year = _safe_int(movie_row.get("Year"))
    if not title:
        return None
    try:
        params = urllib.parse.urlencode({"query": title})
        if year:
            params += f"&year={year}"
        search = _tmdb_get(f"/search/movie?{params}", tmdb_key, timeout=8)
        hits = search.get("results", [])
        if not hits:
            return {"title": title, "year": year, "collection": None, "keywords": []}
        movie_id = hits[0]["id"]
        detail = _tmdb_get(
            f"/movie/{movie_id}?append_to_response=keywords", tmdb_key, timeout=8
        )
        coll_raw = detail.get("belongs_to_collection")
        collection = (
            {"id": coll_raw["id"], "name": (coll_raw.get("name") or "").strip()}
            if coll_raw
            else None
        )
        kw_list = detail.get("keywords", {}).get("keywords", [])
        keywords = [{"id": kw["id"], "name": kw["name"]} for kw in kw_list if kw.get("id")]
        return {"title": title, "year": year, "collection": collection, "keywords": keywords}
    except Exception:
        return None


def _run_tmdb_enrichment_scan(data_dir: Path, movies: list[dict], tmdb_key: str, sig: str) -> None:
    """Background thread: enrich all library movies via TMDB with bounded concurrency.

    Writes data_dir/tmdb_enrichment.json when done (or partially on progress).
    Updates the module-level _tmdb_scan_state throughout.
    """
    global _tmdb_scan_state

    total = len(movies)
    with _tmdb_scan_lock:
        _tmdb_scan_state.update({"running": True, "scanned": 0, "total": total, "done": False, "sig": sig})

    enrichment: dict[str, dict] = {}  # title → enrichment record

    def _enrich_one(movie_row: dict) -> dict | None:
        return _enrich_movie(movie_row, tmdb_key)

    scanned = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(_enrich_one, row): row for row in movies}
            for future in concurrent.futures.as_completed(futures):
                scanned += 1
                result = future.result()
                if result and result.get("title"):
                    enrichment[result["title"]] = result
                with _tmdb_scan_lock:
                    _tmdb_scan_state["scanned"] = scanned
    except Exception:
        pass

    # Write the enrichment cache.
    cache = {"sig": sig, "enrichment": enrichment}
    try:
        enrichment_path = data_dir / "tmdb_enrichment.json"
        enrichment_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    with _tmdb_scan_lock:
        _tmdb_scan_state.update({"running": False, "done": True, "scanned": scanned})


def _load_enrichment_cache(data_dir: Path, sig: str) -> dict[str, dict] | None:
    """Return the enrichment dict if the cache exists and matches sig; else None."""
    try:
        p = data_dir / "tmdb_enrichment.json"
        if not p.exists():
            return None
        cached = json.loads(p.read_text(encoding="utf-8"))
        if cached.get("sig") != sig:
            return None
        return cached.get("enrichment", {})
    except Exception:
        return None


# ── TVmaze per-show network helpers ──────────────────────────────────────────

def _tvmaze_get(path: str, timeout: int = 8):
    """Single TVmaze API call. Free, no API key required. Raises on network / non-200 errors."""
    url = f"https://api.tvmaze.com{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Programmarr/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _lookup_show_network(title: str) -> str | None:
    """Look up a single TV show's network name from TVmaze.

    Returns the network name string (from network.name or webChannel.name),
    or None on any failure or if the show is not found.
    """
    try:
        encoded = urllib.parse.quote(title, safe="")
        data = _tvmaze_get(f"/singlesearch/shows?q={encoded}")
        # Prefer cable/broadcast network; fall back to streaming (webChannel).
        network = data.get("network") or {}
        web = data.get("webChannel") or {}
        name = (network.get("name") or web.get("name") or "").strip()
        return name if name else None
    except Exception:
        return None


def _run_tvmaze_scan(data_dir: Path, shows: list[dict], sig: str) -> None:
    """Background thread: look up every TV show's network from TVmaze with bounded concurrency.

    Writes data_dir/tvmaze_cache.json when done.
    Updates the module-level _tvmaze_scan_state throughout.
    """
    global _tvmaze_scan_state

    total = len(shows)
    with _tvmaze_scan_lock:
        _tvmaze_scan_state.update({"running": True, "scanned": 0, "total": total, "done": False, "sig": sig})

    networks: dict[str, str | None] = {}  # title → network name or null

    scanned = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(_lookup_show_network, row.get("Title", "")): row for row in shows}
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                title = row.get("Title", "")
                scanned += 1
                try:
                    result = future.result()
                except Exception:
                    result = None
                if title:
                    networks[title] = result
                with _tvmaze_scan_lock:
                    _tvmaze_scan_state["scanned"] = scanned
    except Exception:
        pass

    cache = {"sig": sig, "networks": networks}
    try:
        cache_path = data_dir / "tvmaze_cache.json"
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    with _tvmaze_scan_lock:
        _tvmaze_scan_state.update({"running": False, "done": True, "scanned": scanned})


def _load_tvmaze_cache(data_dir: Path, sig: str) -> dict[str, str | None] | None:
    """Return the networks dict if the cache exists and matches sig; else None."""
    try:
        p = data_dir / "tvmaze_cache.json"
        if not p.exists():
            return None
        cached = json.loads(p.read_text(encoding="utf-8"))
        if cached.get("sig") != sig:
            return None
        return cached.get("networks", {})
    except Exception:
        return None


def _start_tvmaze_scan_impl(refresh: bool = False) -> dict:
    """Business logic for starting the TVmaze network scan.

    Mirrors _start_tmdb_scan_impl.  Returns immediately with {"running", "cached"}.
    """
    import csv as _csv

    csv_path = DATA_DIR / "plex_library.csv"
    if not csv_path.exists():
        return {"running": False, "cached": False, "reason": "no_csv"}

    sig = _library_signature()

    with _tvmaze_scan_lock:
        already_running = _tvmaze_scan_state["running"]
        scan_sig = _tvmaze_scan_state.get("sig")

    if already_running and scan_sig == sig and not refresh:
        return {"running": True, "cached": False}

    if not refresh and _load_tvmaze_cache(DATA_DIR, sig) is not None:
        with _tvmaze_scan_lock:
            _tvmaze_scan_state.update({"running": False, "done": True, "sig": sig})
        return {"running": False, "cached": True}

    # Load TV shows to scan.
    shows: list[dict] = []
    with open(csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("Type") == "TV":
                shows.append(row)

    with _tvmaze_scan_lock:
        _tvmaze_scan_state.update({
            "running": True, "scanned": 0, "total": len(shows), "done": False, "sig": sig,
        })

    data_dir = DATA_DIR  # capture for the thread
    t = threading.Thread(
        target=_run_tvmaze_scan,
        args=(data_dir, shows, sig),
        daemon=True,
        name="tvmaze-network-scan",
    )
    t.start()

    return {"running": True, "cached": False}


@router.post("/pipeline/tvmaze-scan")
def start_tvmaze_scan(refresh: bool = Query(False)):
    """Start the TVmaze network scan in the background if not already cached/running.

    Returns immediately with {"running": bool, "cached": bool}.
    Pass ?refresh=1 to force a rescan even if the cache is valid.

    The scan fetches each TV show's network from TVmaze (free, no API key) using
    bounded concurrency (12 workers).  Progress is readable via
    GET /pipeline/tvmaze-scan/status.  The result is cached to data/tvmaze_cache.json
    keyed by the library signature.
    """
    return _start_tvmaze_scan_impl(refresh=bool(refresh))


@router.get("/pipeline/tvmaze-scan/status")
def tvmaze_scan_status():
    """Return the current TVmaze network scan progress.

    Response: {"running": bool, "scanned": int, "total": int, "done": bool}
    """
    with _tvmaze_scan_lock:
        return {
            "running": _tvmaze_scan_state["running"],
            "scanned": _tvmaze_scan_state["scanned"],
            "total": _tvmaze_scan_state["total"],
            "done": _tvmaze_scan_state["done"],
        }


# ── End TVmaze helpers ─────────────────────────────────────────────────────────


# ── Wikidata franchise helpers ────────────────────────────────────────────────
# Wikidata is the second franchise source, merged with TMDB.  It is the
# lowest-certainty source — all matching is conservative and every failure
# path degrades to TMDB-only results with no exception propagation.
#
# SPARQL approach:
#   1. Build a label→title map from all library titles (movies + TV shows).
#   2. Query Wikidata for items that carry wdt:P179 ("part of the series") or
#      wdt:P8345 ("media franchise"), filtering to films (wd:Q11424) and TV
#      series (wd:Q5398426).  The query is VALUES-driven so only labels that
#      exist in the library are sent — no full-dump scan.
#   3. Group results by (series/franchise entity, series label).
#   4. Accept a group ONLY when ≥ FRANCHISE_MIN distinct library titles match
#      AND each matched title maps unambiguously (1-to-1) to that series.
#      A title that matches multiple series is rejected for all of them.
#   5. Shape each accepted group as a franchise dict compatible with the TMDB
#      format: {"name", "source": "wikidata", "members": [{title, year, type}]}.
#
# Limitations acknowledged:
#   - Label matching is case-insensitive exact; variant titles (subtitles, "The"
#     placement) will miss.  This is intentional — precision over recall.
#   - Year matching is used when available to reduce false positives on common
#     short titles (e.g. "It").
#   - The SPARQL query has a hard 20-second timeout and is wrapped in try/except;
#     any failure returns an empty list immediately.

_WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
_WIKIDATA_USER_AGENT = (
    "Programmarr/1.0 (https://github.com/alpinearchitecture/programmarr; "
    "franchise enrichment; contact via GitHub issues)"
)
# Wikidata SPARQL batch size: send at most this many VALUES per query to avoid
# hitting the query-complexity limit or 30s query timeout.
_WIKIDATA_BATCH_SIZE = 50


def _wikidata_sparql(query: str, timeout: int = 20) -> dict:
    """Execute a Wikidata SPARQL query and return the parsed JSON response.

    Raises on any network or HTTP error — callers must catch.
    Wikidata requires a descriptive User-Agent; we set one.
    """
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{_WIKIDATA_SPARQL_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": _WIKIDATA_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wikidata_franchise_batch(
    labels: list[str],
    label_to_rows: dict[str, list[dict]],
) -> dict[str, dict]:
    """Query Wikidata for franchise/series membership for a batch of title labels.

    Returns: series_name → {"name": str, "member_titles": set[str]}
    where member_titles are library titles (canonical case from label_to_rows)
    that Wikidata grouped into the same series/franchise.

    Conservative matching rules applied here:
      - Only films (wd:Q11424) and TV series (wd:Q5398426) are considered.
      - A library title is accepted for a series only when it maps 1-to-1
        (same label appearing in multiple series is rejected for all).
      - Year cross-check: when the Wikidata item has a year and the library
        row has a year, they must match within ±1 to guard against title
        collisions on common short names.
    """
    if not labels:
        return {}

    # Build a VALUES clause with quoted, escaped labels.
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    values_list = " ".join(f'"{_esc(lbl)}"@en' for lbl in labels)
    query = f"""
SELECT ?item ?itemLabel ?seriesLabel ?seriesYear ?itemYear WHERE {{
  VALUES ?titleLabel {{ {values_list} }}
  ?item rdfs:label ?titleLabel .
  ?item wikibase:sitelinks/schema:about ?_ .
  {{
    ?item wdt:P31 wd:Q11424 .
  }} UNION {{
    ?item wdt:P31 wd:Q5398426 .
  }}
  {{
    ?item wdt:P179 ?series .
  }} UNION {{
    ?item wdt:P8345 ?series .
  }}
  ?series rdfs:label ?seriesLabel .
  FILTER(LANG(?seriesLabel) = "en")
  OPTIONAL {{ ?series wdt:P571 ?seriesYear . }}
  OPTIONAL {{ ?item  wdt:P577 ?itemDate  . }}
  BIND(SUBSTR(STR(?itemDate), 1, 4) AS ?itemYear)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}}
LIMIT 500
"""
    try:
        data = _wikidata_sparql(query, timeout=20)
    except Exception:
        return {}

    bindings = data.get("results", {}).get("bindings", [])

    # label_lower → set of series labels it appeared in
    # used to detect ambiguous titles (one label → multiple series → reject)
    label_to_series: dict[str, set[str]] = {}
    series_to_members: dict[str, dict] = {}  # series_label → {name, members: {label: year_ok}}

    for b in bindings:
        item_label_raw = b.get("itemLabel", {}).get("value", "")
        series_label = b.get("seriesLabel", {}).get("value", "").strip()
        item_year_str = b.get("itemYear", {}).get("value", "")
        if not item_label_raw or not series_label:
            continue

        item_label_lower = item_label_raw.lower()
        # Only process labels that appear in our library set
        if item_label_lower not in {lbl.lower() for lbl in labels}:
            continue

        # Year cross-check: if both Wikidata and library have a year, they must
        # be within ±1 (handles films released near year-end on different sides).
        wd_year = None
        try:
            wd_year = int(item_year_str[:4]) if len(item_year_str) >= 4 else None
        except (ValueError, TypeError):
            pass

        lib_rows = label_to_rows.get(item_label_lower, [])
        if wd_year is not None and lib_rows:
            lib_year = None
            for row in lib_rows:
                y = _safe_int(row.get("Year"))
                if y is not None:
                    lib_year = y
                    break
            if lib_year is not None and abs(wd_year - lib_year) > 1:
                continue  # year mismatch — reject this pairing

        if item_label_lower not in label_to_series:
            label_to_series[item_label_lower] = set()
        label_to_series[item_label_lower].add(series_label)

        if series_label not in series_to_members:
            series_to_members[series_label] = {"name": series_label, "members": {}}
        series_to_members[series_label]["members"][item_label_lower] = item_label_raw

    # Reject any label that maps to multiple series (ambiguous).
    ambiguous = {lbl for lbl, series_set in label_to_series.items() if len(series_set) > 1}
    for series_info in series_to_members.values():
        for amb in ambiguous:
            series_info["members"].pop(amb, None)

    return series_to_members


def _run_wikidata_franchise_scan(
    data_dir: Path,
    movies: list[dict],
    shows: list[dict],
    sig: str,
) -> None:
    """Background thread: query Wikidata for franchise memberships.

    Writes data_dir/wikidata_cache.json when done (or on any error — writes
    empty/partial result so the cache key is set and we don't re-scan on every
    request).  Always defensive: never raises.
    """
    global _wikidata_scan_state

    with _wikidata_scan_lock:
        _wikidata_scan_state.update({"running": True, "done": False, "sig": sig})

    franchises: list[dict] = []
    try:
        # Build label → list of library rows map (lower-cased key).
        # Includes both movies and TV shows — Wikidata franchises span both.
        label_to_rows: dict[str, list[dict]] = {}
        for row in movies + shows:
            t = row.get("Title", "").strip()
            if t:
                key = t.lower()
                if key not in label_to_rows:
                    label_to_rows[key] = []
                label_to_rows[key].append(row)

        all_labels = list(label_to_rows.keys())  # unique lower-cased titles

        # Process in batches to avoid SPARQL complexity limits.
        # Use canonical (original-case) labels from the first matching row.
        canonical_label: dict[str, str] = {
            lbl: label_to_rows[lbl][0].get("Title", lbl) for lbl in all_labels
        }

        merged_series: dict[str, dict] = {}  # series_label → {name, members: {lbl: raw_label}}
        # Track globally which labels appear in multiple series across all batches,
        # so cross-batch ambiguity is also rejected.
        global_label_to_series: dict[str, set[str]] = {}

        for i in range(0, len(all_labels), _WIKIDATA_BATCH_SIZE):
            batch = all_labels[i: i + _WIKIDATA_BATCH_SIZE]
            batch_canonical = [canonical_label[lbl] for lbl in batch]
            batch_result = _wikidata_franchise_batch(batch_canonical, label_to_rows)
            for series_label, series_info in batch_result.items():
                if series_label not in merged_series:
                    merged_series[series_label] = {
                        "name": series_info["name"],
                        "members": {},
                    }
                for lbl, raw in series_info["members"].items():
                    merged_series[series_label]["members"][lbl] = raw
                    if lbl not in global_label_to_series:
                        global_label_to_series[lbl] = set()
                    global_label_to_series[lbl].add(series_label)

        # Global ambiguity pass: a label found in 2+ series → reject from all.
        globally_ambiguous = {
            lbl for lbl, series_set in global_label_to_series.items() if len(series_set) > 1
        }
        for series_info in merged_series.values():
            for amb in globally_ambiguous:
                series_info["members"].pop(amb, None)

        # Build franchise list: series with >= FRANCHISE_MIN library members.
        for series_label, series_info in merged_series.items():
            member_map = series_info["members"]
            if len(member_map) < FRANCHISE_MIN:
                continue
            members: list[dict] = []
            for lbl, raw_title in member_map.items():
                # Use canonical library title (prefer original case from library).
                lib_title = canonical_label.get(lbl, raw_title)
                # Find year from library row.
                year = None
                for row in label_to_rows.get(lbl, []):
                    y = _safe_int(row.get("Year"))
                    if y is not None:
                        year = y
                        break
                # Determine type from library.
                row_type = "Movie"
                for row in label_to_rows.get(lbl, []):
                    if row.get("Type") == "TV":
                        row_type = "TV"
                        break
                members.append({"title": lib_title, "year": year, "type": row_type})

            members.sort(key=lambda m: (m["year"] or 9999, m["title"]))
            franchises.append({
                "name": series_info["name"],
                "source": "wikidata",
                "members": members,
            })

        franchises.sort(key=lambda fr: -len(fr["members"]))
    except Exception as exc:
        # Any unexpected failure → log and return empty; never crash.
        print(f"[wikidata] franchise scan error: {exc}", flush=True)
        franchises = []

    cache = {"sig": sig, "franchises": franchises}
    try:
        cache_path = data_dir / "wikidata_cache.json"
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    with _wikidata_scan_lock:
        _wikidata_scan_state.update({"running": False, "done": True})


def _load_wikidata_cache(data_dir: Path, sig: str) -> list[dict] | None:
    """Return the Wikidata franchise list if the cache exists and matches sig; else None."""
    try:
        p = data_dir / "wikidata_cache.json"
        if not p.exists():
            return None
        cached = json.loads(p.read_text(encoding="utf-8"))
        if cached.get("sig") != sig:
            return None
        return cached.get("franchises", [])
    except Exception:
        return None


def _start_wikidata_scan_impl(
    data_dir: Path,
    movies: list[dict],
    shows: list[dict],
    sig: str,
    refresh: bool = False,
) -> None:
    """Kick off a background Wikidata franchise scan if needed.

    Called from _start_tmdb_scan_impl (after TMDB finishes) and from
    get_franchises when no cache exists.  No-ops when already running,
    cache valid, or library is empty.  Never raises.
    """
    try:
        with _wikidata_scan_lock:
            already_running = _wikidata_scan_state["running"]
            scan_sig = _wikidata_scan_state.get("sig")

        if already_running and scan_sig == sig and not refresh:
            return
        if not refresh and _load_wikidata_cache(data_dir, sig) is not None:
            with _wikidata_scan_lock:
                _wikidata_scan_state.update({"running": False, "done": True, "sig": sig})
            return
        if not movies and not shows:
            return

        with _wikidata_scan_lock:
            _wikidata_scan_state.update({"running": True, "done": False, "sig": sig})

        t = threading.Thread(
            target=_run_wikidata_franchise_scan,
            args=(data_dir, movies, shows, sig),
            daemon=True,
            name="wikidata-franchise-scan",
        )
        t.start()
    except Exception:
        pass  # defensive — never let a scan-start failure propagate


def _merge_franchises(tmdb: list[dict], wikidata: list[dict]) -> list[dict]:
    """Merge TMDB and Wikidata franchise lists, de-duplicating by normalized name.

    TMDB is authoritative: if a name normalizes to the same key as a TMDB franchise,
    the TMDB entry wins (members from Wikidata are NOT merged in — TMDB's collection
    data is more precise).  Wikidata-only entries are appended after TMDB entries.
    Final sort: by member count descending.
    """
    seen: set[str] = set()
    result: list[dict] = []

    for fr in tmdb:
        norm = _normalize_name(fr["name"])
        if norm not in seen:
            seen.add(norm)
            result.append(fr)

    for fr in wikidata:
        norm = _normalize_name(fr["name"])
        if norm not in seen:
            seen.add(norm)
            result.append(fr)

    result.sort(key=lambda fr: -len(fr["members"]))
    return result


# ── End Wikidata helpers ───────────────────────────────────────────────────────


def _load_themed_keywords() -> list[dict]:
    """Load the curated themed-keyword catalog from SCRIPTS_DIR/themed_keywords.json.

    Each entry: {"name": str, "keyword_ids": [int, ...]}
    Returns [] if the file is absent or malformed.
    """
    try:
        p = SCRIPTS_DIR / "themed_keywords.json"
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _themes_from_enrichment(enrichment: dict[str, dict], catalog: list[dict]) -> list[dict]:
    """Compute theme facets from the TMDB enrichment cache.

    For each catalog theme, count library movies whose enrichment keywords contain
    ANY of the theme's keyword_ids.  Returns themes with count >= THEME_MIN,
    sorted by count descending, each entry {"name": str, "count": int,
    "keyword_ids": [int, ...], "titles": [str, ...]}.

    No TMDB calls — reads only the local enrichment cache built by F9.
    """
    results: list[dict] = []
    for theme in catalog:
        name = theme.get("name", "")
        kw_ids = set(theme.get("keyword_ids", []))
        if not name or not kw_ids:
            continue
        matched_titles: list[str] = []
        for title, record in enrichment.items():
            movie_kw_ids = {kw["id"] for kw in record.get("keywords", []) if kw.get("id")}
            if movie_kw_ids & kw_ids:
                matched_titles.append(title)
        if len(matched_titles) >= THEME_MIN:
            results.append({
                "name": name,
                "count": len(matched_titles),
                "keyword_ids": list(kw_ids),
                "titles": sorted(matched_titles),
            })
    results.sort(key=lambda t: (-t["count"], t["name"].lower()))
    return results


def _franchises_from_enrichment(enrichment: dict[str, dict]) -> list[dict]:
    """Derive franchise candidates from the TMDB enrichment cache.

    Groups library movies by TMDB belongs_to_collection, filters to
    FRANCHISE_MIN members, de-dupes by normalized name, sorts by member count desc.
    """
    collection_buckets: dict[int, dict] = {}  # collection_id → {name, members}

    for title, record in enrichment.items():
        coll = record.get("collection")
        if not coll:
            continue
        coll_id = coll.get("id")
        coll_name = (coll.get("name") or "").strip()
        if not coll_id or not coll_name:
            continue
        year = record.get("year")
        if coll_id not in collection_buckets:
            collection_buckets[coll_id] = {"name": coll_name, "members": []}
        collection_buckets[coll_id]["members"].append({
            "title": title,
            "year": year,
            "type": "Movie",
        })

    franchises: list[dict] = []
    seen_norm: set[str] = set()
    for coll_info in collection_buckets.values():
        members = coll_info["members"]
        if len(members) < FRANCHISE_MIN:
            continue
        norm = _normalize_name(coll_info["name"])
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        members.sort(key=lambda m: (m["year"] or 9999, m["title"]))
        franchises.append({"name": coll_info["name"], "source": "tmdb", "members": members})

    franchises.sort(key=lambda fr: -len(fr["members"]))
    return franchises


def _start_tmdb_scan_impl(refresh: bool = False) -> dict:
    """Business logic for starting the TMDB enrichment scan.

    Separated from the FastAPI route so it can be called directly in tests without
    FastAPI's Query(...) wrapping the default parameter value.

    Also kicks off the Wikidata franchise scan in parallel (it runs independently of
    the TMDB key requirement, so keyless users still get Wikidata franchises).
    """
    import csv as _csv

    csv_path = DATA_DIR / "plex_library.csv"
    if not csv_path.exists():
        return {"running": False, "cached": False, "reason": "no_csv"}

    sig = _library_signature()

    # Load all library rows once; TMDB needs movies, Wikidata needs both.
    movies: list[dict] = []
    shows: list[dict] = []
    with open(csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row.get("Type") == "Movie":
                movies.append(row)
            elif row.get("Type") == "TV":
                shows.append(row)

    data_dir = DATA_DIR  # capture for threads (DATA_DIR may be monkeypatched in tests)

    # ── Wikidata: always kick off regardless of TMDB key ──────────────────────
    _start_wikidata_scan_impl(data_dir, movies, shows, sig, refresh=refresh)

    # ── TMDB: requires an API key ─────────────────────────────────────────────
    cfg = _load_config()
    tmdb_key = cfg.get("tmdb_api_key", "").strip()
    if not tmdb_key:
        return {"running": False, "cached": False, "reason": "no_tmdb_key"}

    with _tmdb_scan_lock:
        already_running = _tmdb_scan_state["running"]
        scan_sig = _tmdb_scan_state.get("sig")

    if already_running and scan_sig == sig and not refresh:
        return {"running": True, "cached": False}

    # Check cache validity.
    if not refresh and _load_enrichment_cache(data_dir, sig) is not None:
        with _tmdb_scan_lock:
            _tmdb_scan_state.update({"running": False, "done": True, "sig": sig})
        return {"running": False, "cached": True}

    # Reset state and kick off the background TMDB scan.
    with _tmdb_scan_lock:
        _tmdb_scan_state.update({
            "running": True, "scanned": 0, "total": len(movies), "done": False, "sig": sig,
        })

    t = threading.Thread(
        target=_run_tmdb_enrichment_scan,
        args=(data_dir, movies, tmdb_key, sig),
        daemon=True,
        name="tmdb-enrichment-scan",
    )
    t.start()

    return {"running": True, "cached": False}


@router.post("/pipeline/tmdb-scan")
def start_tmdb_scan(refresh: bool = Query(False)):
    """Start the TMDB enrichment scan in the background if not already cached/running.

    Returns immediately with {"running": bool, "cached": bool}.
    Pass ?refresh=1 to force a rescan even if the cache is valid.

    The scan fetches TMDB movie details (belongs_to_collection + keywords) for every
    movie in plex_library.csv using bounded concurrency (12 workers).  Progress is
    readable via GET /pipeline/tmdb-scan/status.  The result is cached to
    data/tmdb_enrichment.json keyed by the library signature.
    """
    return _start_tmdb_scan_impl(refresh=bool(refresh))


@router.get("/pipeline/tmdb-scan/status")
def tmdb_scan_status():
    """Return the current TMDB enrichment scan progress.

    Response: {"running": bool, "scanned": int, "total": int, "done": bool}
    """
    with _tmdb_scan_lock:
        return {
            "running": _tmdb_scan_state["running"],
            "scanned": _tmdb_scan_state["scanned"],
            "total": _tmdb_scan_state["total"],
            "done": _tmdb_scan_state["done"],
        }


@router.get("/pipeline/franchises")
def get_franchises(refresh: bool = False):
    """Return franchise candidates from TMDB (F9) and Wikidata (F13), merged.

    Each entry: {"name", "source": "tmdb"|"wikidata", "members": [{"title","year","type"}]}

    TMDB entries derive from belongs_to_collection via the enrichment scan
    (requires tmdb_api_key).  Wikidata entries come from P179/P8345 SPARQL queries
    (no key required) — keyless users still see Wikidata franchises.

    This endpoint is FAST — both sources read from their respective caches.
    Returns [] only when BOTH caches are absent (scans not yet done).

    Pass ?refresh=1 to trigger a fresh rescan of both sources.
    """
    csv_path = DATA_DIR / "plex_library.csv"
    if not csv_path.exists():
        return []

    sig = _library_signature()

    if refresh:
        # Delegate to the combined scan impl (fire and forget); don't block here.
        _start_tmdb_scan_impl(refresh=True)

    # ── TMDB franchises (requires API key + enrichment cache) ─────────────────
    cfg = _load_config()
    tmdb_key = cfg.get("tmdb_api_key", "").strip()
    tmdb_franchises: list[dict] = []
    if tmdb_key:
        enrichment = _load_enrichment_cache(DATA_DIR, sig)
        if enrichment is not None:
            tmdb_franchises = _franchises_from_enrichment(enrichment)

    # ── Wikidata franchises (no key required) ─────────────────────────────────
    # On first call without a Wikidata cache, kick off the scan lazily.
    # (Normally started by POST /pipeline/tmdb-scan on Planner mount, but this
    # covers the keyless path where tmdb-scan returns early without starting it.)
    wikidata_franchises = _load_wikidata_cache(DATA_DIR, sig)
    if wikidata_franchises is None:
        # No cache yet — try to start a scan if not already running.
        import csv as _csv
        movies: list[dict] = []
        shows: list[dict] = []
        try:
            with open(csv_path, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    if row.get("Type") == "Movie":
                        movies.append(row)
                    elif row.get("Type") == "TV":
                        shows.append(row)
        except Exception:
            pass
        _start_wikidata_scan_impl(DATA_DIR, movies, shows, sig)
        wikidata_franchises = []

    return _merge_franchises(tmdb_franchises, wikidata_franchises)


ANCHOR = "## Channel Numbering Scheme"
TYPE_LABELS = {
    "marathons": "TV Marathons", "tv_blocks": "TV Blocks", "movies": "Movie channels",
    "franchise": "Franchise series", "specialty": "Specialty channels",
}

# Per-category prose for the LLM prompt's numbering guidance section.
_BLOCK_DESC = {
    "marathon":          "TV Marathons — 24/7 single-show loops (needs 50+ episodes to qualify)",
    "tv_block":          "TV Blocks — themed multi-show rotations (era blocks, genre blocks, etc.)",
    "tv_movie_mix":      "TV & Movie Mix — mixed-genre channels spanning both shows and films",
    "movie":             "Movie Channels — genre and decade-based pools",
    "entity":            "Studios / Directors / Actors — curated by creator or studio",
    "network":           "Networks — all shows from a single network (HBO, NBC, etc.)",
    "programming_block": "Classic TV Blocks — historical programming lineups (TGIF, Must See TV, etc.)",
    "franchise":         "Franchise & Curated Series — ordered collections (film series in release order, etc.)",
    "specialty":         "Specialty — single-movie loops, holiday, niche themes",
}
# Matches the ordered category list under "## Channel Numbering Scheme" in PROMPT.md,
# regardless of the numbers/labels in it. PROMPT.md's static list is only the default;
# this is what rewrites it to the user's configured channel_order.
_SCHEME_BULLETS_RE = re.compile(
    r"^(?:\d+\. \*\*[^\n]*\n)+", re.MULTILINE
)


def _read_prompt_source() -> str:
    for candidate in [DATA_DIR / "PROMPT.md", SCRIPTS_DIR / "PROMPT.md"]:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise HTTPException(404, "PROMPT.md not found")


def _strip_meta(content: str) -> str:
    """Drop the human-facing meta header above the first '---' separator line.

    Those lines (model recommendation, attach-vs-paste guidance) belong in the UI
    walkthrough, not the copied prompt. The CLI still reads the full file directly.
    """
    lines = content.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return content


def _regen_numbering_scheme(content: str, start: int) -> str:
    """Rewrite PROMPT.md's numbering bullets + example numbers from the live layout.

    Channel numbers are assigned sequentially tight-packed in category order (no
    fixed sizes).  We produce a representative example using one channel per
    non-empty category so the LLM sees realistic numbers.  PROMPT.md's static text
    is only the default (used verbatim by the CLI).
    """
    cfg_order = channel_blocks.resolve_order(_load_config().get("channel_order"))
    # One representative channel per category to show the packing.
    example_counts = {k: 1 for k in cfg_order}
    numbers = channel_blocks.assign_numbers(cfg_order, example_counts, start)
    bullets = "\n".join(
        f"- **{numbers[k][0]}+**: {_BLOCK_DESC.get(k, channel_blocks.BLOCK_LABELS.get(k, k))}"
        for k in cfg_order if k in numbers
    )
    content, _n = _SCHEME_BULLETS_RE.subn(bullets + "\n", content, count=1)
    if not _n:
        # A silent no-op here means the LLM is told the DEFAULT order while the user
        # configured a different one - exactly the drift this function exists to stop.
        print("WARNING: PROMPT.md numbering-scheme list not found; "
              "channel_order is not being reflected in the prompt.")
    # Update JSONL example numbers using the first three occupied positions.
    occupied = [numbers[k][0] for k in cfg_order if k in numbers]
    if len(occupied) >= 1:
        content = content.replace('"number": 10,', f'"number": {occupied[0]},')
    if len(occupied) >= 2:
        content = content.replace('"number": 20,', f'"number": {occupied[1]},')
    if len(occupied) >= 3:
        content = content.replace('"number": 30,', f'"number": {occupied[2]},')
    return content


def _apply_target_prefs_start(content: str, target: str, preferences: str, start: int) -> str:
    if target:
        content = content.replace("{TARGET}", target)
    if preferences:
        inj = (
            "\n## User Preferences\n\n"
            "The user has specifically requested the following channels or themes. "
            "Treat these as high-priority — if the library has enough content to support them, "
            "they must appear in the output:\n\n"
            f"{preferences}\n"
        )
        content = content.replace(ANCHOR, inj + "\n" + ANCHOR)
    return _regen_numbering_scheme(content, start)


@router.get("/pipeline/prompt")
def get_prompt(target: str = "", preferences: str = "", start: int = 10):
    # Legacy GET: full file (meta included), used by the current Run UI. Kept
    # intact so the live UI isn't degraded before the new flow (PR2) ships.
    content = _apply_target_prefs_start(_read_prompt_source(), target, preferences, start)
    return {"content": content}


class PromptOptions(BaseModel):
    target: str = ""
    preferences: str = ""
    start: int = 10
    include_genres: list[str] = []
    exclude_genres: list[str] = []
    include_decades: list[str] = []   # labels, e.g. "90s"
    exclude_decades: list[str] = []
    include_types: list[str] = []     # marathons, tv_blocks, movies, franchise, specialty
    exclude_types: list[str] = []


def _what_to_build(opts: "PromptOptions") -> str:
    def line(prefix, items, labels=None):
        if not items:
            return None
        names = [labels.get(i, i) if labels else i for i in items]
        return f"- {prefix}: {', '.join(names)}"

    inc = [x for x in (
        line("Channel types", opts.include_types, TYPE_LABELS),
        line("Movie genres", opts.include_genres),
        line("Decades", opts.include_decades),
    ) if x]
    exc = [x for x in (
        line("Channel types", opts.exclude_types, TYPE_LABELS),
        line("Movie genres", opts.exclude_genres),
        line("Decades", opts.exclude_decades),
    ) if x]
    if not inc and not exc:
        return ""

    s = "\n## What To Build\n\n"
    if inc:
        s += "Definitely include channels for these when the library has enough content:\n"
        s += "\n".join(inc) + "\n\n"
    if exc:
        s += "Do NOT create any channels for these:\n"
        s += "\n".join(exc) + "\n\n"
    s += (
        "Beyond the must-include list above, you are encouraged to create additional "
        "themed channels you discover in the library — franchises, directors, sub-genres, "
        "holiday blocks, and other clusters. Surprising, well-curated channels are welcome, "
        "as long as they don't fall under the 'do not create' list.\n"
    )
    return s


@router.post("/pipeline/prompt")
def build_prompt(opts: PromptOptions = PromptOptions()):
    # New flow: meta stripped (UI carries that guidance) + toggle-driven What To Build.
    content = _strip_meta(_read_prompt_source())
    wtb = _what_to_build(opts)
    if wtb:
        content = content.replace(ANCHOR, wtb + "\n" + ANCHOR)
    content = _apply_target_prefs_start(content, opts.target, opts.preferences, opts.start)
    return {"content": content}


@router.post("/pipeline/validate")
async def validate(
    file: Optional[UploadFile] = File(None),
    content: Optional[str] = Form(None),
    append: bool = Form(False),
):
    if file:
        raw = (await file.read()).decode("utf-8", errors="replace")
    elif content:
        raw = content
    else:
        raise HTTPException(400, "Provide file or content")

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            data = {"channels": data, "orphaned": [], "suggested_channels": []}
        elif not (isinstance(data, dict) and "channels" in data):
            raise ValueError("not a channel dict")
    except Exception:
        channels = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    if "number" in obj:
                        channels.append(obj)
                except Exception:
                    pass
        if not channels:
            return {"ok": False, "error": "No valid channel objects found"}
        data = {"channels": channels, "orphaned": [], "suggested_channels": []}

    new_channels = data.get("channels", [])
    if append:
        # Merge AI-discovered channels on top of the in-progress draft lineup,
        # renumbering any collisions to the next free slot so nothing is overwritten.
        existing = {"channels": [], "orphaned": [], "suggested_channels": []}
        cpath = DATA_DIR / "channels.draft.json"
        if cpath.exists():
            with open(cpath, encoding="utf-8") as f:
                existing = json.load(f)
        kept = existing.get("channels", [])
        used = {c.get("number") for c in kept}
        seen_names = {(c.get("name") or "").strip().lower() for c in kept}
        next_free = (max(used) + 1) if used else 1
        added = 0
        skipped_dupes = 0
        for ch in new_channels:
            # Skip anything we already have by name (case-insensitive) — re-running the
            # AI step shouldn't stack a second "Time Travel" channel.
            nm = (ch.get("name") or "").strip().lower()
            if nm and nm in seen_names:
                skipped_dupes += 1
                continue
            n = ch.get("number")
            if n in used or n is None:
                while next_free in used:
                    next_free += 1
                ch["number"] = next_free
            used.add(ch["number"])
            seen_names.add(nm)
            next_free = max(next_free, ch["number"] + 1)
            kept.append(ch)
            added += 1
        existing["channels"] = sorted(kept, key=lambda c: c.get("number", 0))
        data = existing
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return {"ok": True, "count": len(data["channels"]), "added": added,
                "skipped_dupes": skipped_dupes, "channels": data["channels"]}

    with open(DATA_DIR / "channels.draft.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return {"ok": True, "count": len(new_channels), "channels": new_channels}


class DiscoverOptions(BaseModel):
    discover: bool = True
    curate_pools: list[str] = []  # human descriptions of broad pools to split by tone


@router.post("/pipeline/discover-prompt")
def discover_prompt(opts: DiscoverOptions = DiscoverOptions()):
    """Build the AI-extras prompt, seeded with the existing (deterministic) lineup.

    Two jobs the AI can be asked to do, depending on `opts`:
      - curate_pools: replace a broad pool (e.g. "Comedy") with several tonally-coherent
        channels, or trim to a best-of — what genre tags can't express.
      - discover: suggest ADDITIONAL themed channels that don't duplicate the lineup.
    New channels are numbered from the next free channel up; the merge step renumbers
    any collisions anyway.
    """
    existing: list[dict] = []
    cpath = DATA_DIR / "channels.draft.json"
    if cpath.exists():
        try:
            with open(cpath, encoding="utf-8") as f:
                existing = json.load(f).get("channels", [])
        except Exception:
            existing = []
    maxnum = max((c.get("number", 0) for c in existing), default=9)
    start = maxnum + 1
    listing = "\n".join(
        f'- #{c.get("number")} {c.get("name")}'
        for c in sorted(existing, key=lambda c: c.get("number", 0))
    ) or "(none yet)"

    parts = [
        "You are a TV channel programmer. The user has a self-hosted media library "
        "(attached as plex_library.csv) and has ALREADY built these channels:\n\n"
        f"{listing}",
        "Use ONLY exact titles from the attached plex_library.csv. Do not invent titles "
        "or duplicate the channels above. Each channel you output needs at least 4 fitting titles.",
    ]

    if opts.curate_pools:
        pool_lines = "\n".join(f"- {p}" for p in opts.curate_pools)
        parts.append(
            "## Curate these pools by tone\n\n"
            "Each pool below is too broad to feel hand-programmed — a single 'Comedy' channel "
            "lurches from gross-out to rom-com. For EACH pool, replace it with 2–4 tighter "
            "channels grouped by tone/mood/vibe so titles flow without jarring transitions "
            "(e.g. split Comedy into 'Feel-Good Comedies', 'Raunchy Comedies', 'Dark Comedies'; "
            "or trim to a tight best-of). Only use titles that match that pool's described filter.\n\n"
            f"{pool_lines}"
        )

    if opts.discover:
        parts.append(
            "## Discover additional channels\n\n"
            "Suggest ADDITIONAL themed channels that plain genre/decade filters miss — e.g. "
            "Heist Films, Courtroom Dramas, Road Trip Movies, Time Travel, Mind-Benders, "
            "Feel-Good Rainy Day, Whodunits, Coming-of-Age, Holiday/Christmas, Sports Underdogs."
        )

    parts.append(
        f"Number every new channel sequentially starting at {start}. Output one channel per "
        "line as a JSON object (JSONL) — no commentary, no markdown fences, just one {...} per line:\n"
        f'{{"number": {start}, "name": "Feel-Good Comedies", "shuffle": "shuffle", "content": ["Groundhog Day", "Elf", "School of Rock"]}}'
    )

    return {"content": "\n\n".join(parts) + "\n", "start": start, "existing_count": len(existing)}


# ── Planner v2: deterministic candidate composition ──────────────────────────────

class CandidateSpec(BaseModel):
    kind: str  # genre | genre_decade | blend | studio | director | actor | tv_genre | marathon | network | programming_block | theme | country | mood | style
    name: Optional[str] = None
    genre: Optional[str] = None
    genres: Optional[list[str]] = None
    decade_start: Optional[int] = None
    value: Optional[str] = None        # studio/director/actor/network name, or marathon show title
    titles: Optional[list[str]] = None  # programming_block: resolved member titles present in library
    shuffle: Optional[str] = None      # override; else category default
    live: bool = False                 # franchise only: emit identity ref instead of static titles


class ComposeRequest(BaseModel):
    specs: list[CandidateSpec]
    start: int = 1
    # Applied to every channel built in this batch (surfaced as Planner toggles):
    live: bool = False                      # mark channels auto-updating
    commercials: dict | None = None         # {filler_list_id, filler_list_name?, pad_minutes?}


# Which compose category (bucket) each candidate kind maps to, and its shuffle default.
# Buckets align with channel_blocks.CANONICAL_ORDER keys.
_CATEGORY = {
    "marathon":          ("marathon",          "ordered"),
    "tv_genre":          ("tv_block",          "block"),
    "genre":             ("movie",             "shuffle"),
    "genre_decade":      ("movie",             "shuffle"),
    "blend":             ("movie",             "shuffle"),
    "studio":            ("entity",            "shuffle"),
    "director":          ("entity",            "shuffle"),
    "actor":             ("entity",            "shuffle"),
    "tv_movie_mix":      ("tv_movie_mix",      "shuffle"),
    "network":           ("network",           "shuffle"),
    "programming_block": ("programming_block", "ordered"),
    "franchise":         ("franchise",         "ordered"),
    "theme":             ("specialty",         "shuffle"),
    "country":           ("movie",             "shuffle"),
    "mood":              ("movie",             "shuffle"),
    "style":             ("movie",             "shuffle"),
}
# Compose categories in the order they appear in CANONICAL_ORDER (subset).
_CATEGORY_ORDER = ["marathon", "tv_block", "network", "programming_block", "tv_movie_mix", "movie", "entity", "franchise", "specialty"]
_DECADE_LABEL = {start: label for label, start, _ in DECADE_BUCKETS}


def _load_library():
    import csv as _csv
    p = DATA_DIR / "plex_library.csv"
    if not p.exists():
        return [], []
    movies, shows = [], []
    with open(p, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            (movies if row.get("Type") == "Movie" else shows if row.get("Type") == "TV" else []).append(row)
    return movies, shows


def _resolve_spec(spec: CandidateSpec, movies: list[dict], shows: list[dict]) -> list:
    """Return the sorted, de-duplicated content list a candidate spec selects.

    Titles are plain strings, except a title that is BOTH a movie and a show in the
    library: that one becomes a typed ref ({"movie": t} / {"show": t}) so deploy can't
    resolve it to the wrong media type (issue #39).
    """
    collide = {m["Title"].lower() for m in movies} & {s["Title"].lower() for s in shows}

    def tag(t, typ):
        return {typ: t} if t.lower() in collide else t

    def has_genre(row, g):
        gl = g.lower()
        return any(x.lower() == gl for x in _multi(row.get("Genres")))

    titles: list[str] = []
    if spec.kind == "genre" and spec.genre:
        titles = [m["Title"] for m in movies if has_genre(m, spec.genre)]
    elif spec.kind == "genre_decade" and spec.genre and spec.decade_start is not None:
        titles = [m["Title"] for m in movies
                  if has_genre(m, spec.genre) and _decade_start(_safe_int(m.get("Year"))) == spec.decade_start]
    elif spec.kind == "blend" and spec.genres:
        titles = [m["Title"] for m in movies if all(has_genre(m, g) for g in spec.genres)]
    elif spec.kind == "studio" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(s.lower() == v for s in _multi(m.get("Studio")))]
    elif spec.kind == "director" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(d.lower() == v for d in _multi(m.get("Director")))]
    elif spec.kind == "actor" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(a.lower() == v for a in _multi(m.get("Actors")))]
    elif spec.kind == "tv_genre" and spec.genre:
        titles = [s["Title"] for s in shows if has_genre(s, spec.genre)]
    elif spec.kind == "marathon" and spec.value:
        titles = [s["Title"] for s in shows if s["Title"] == spec.value]
    elif spec.kind == "tv_movie_mix" and spec.genre:
        # Mixed channel: both TV shows AND movies that share this genre, shuffled together.
        # A shared title keeps both copies (one per media type) — both match the genre.
        pairs = {(m["Title"], "movie") for m in movies if m["Title"] and has_genre(m, spec.genre)}
        pairs |= {(s["Title"], "show") for s in shows if s["Title"] and has_genre(s, spec.genre)}
        return [tag(t, typ) for t, typ in sorted(pairs)]
    elif spec.kind == "network" and spec.value:
        # Resolve TV shows whose TVmaze network matches the value (case-insensitive).
        # Load the TVmaze cache once; fall back to Studio column if cache is absent.
        sig = _library_signature()
        tvmaze = _load_tvmaze_cache(DATA_DIR, sig)
        v = spec.value.lower()
        if tvmaze is not None:
            titles = [
                s["Title"] for s in shows
                if (tvmaze.get(s.get("Title", "")) or "").lower() == v
            ]
        else:
            # Cache not available yet — fall back to Studio column as a best-effort.
            titles = [s["Title"] for s in shows if any(n.lower() == v for n in _multi(s.get("Studio")))]
    elif spec.kind == "programming_block" and spec.titles:
        # Intersect the block's pre-resolved member titles against the library TV show titles.
        show_title_set = {s["Title"].lower(): s["Title"] for s in shows}
        titles = [show_title_set[t.lower()] for t in spec.titles if t.lower() in show_title_set]
    elif spec.kind == "theme" and spec.titles:
        # Theme: intersect the pre-resolved movie titles (from the facets query) against
        # the library movie titles.  The facets endpoint already resolved which movies match
        # the theme's keyword ids — we just need to intersect with what's currently in the
        # library (in case the library was re-exported since the facet was computed).
        movie_title_set = {m["Title"].lower(): m["Title"] for m in movies}
        titles = [movie_title_set[t.lower()] for t in spec.titles if t.lower() in movie_title_set]
    elif spec.kind == "country" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(c.lower() == v for c in _multi(m.get("Country")))]
    elif spec.kind == "mood" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(mo.lower() == v for mo in _multi(m.get("Mood")))]
    elif spec.kind == "style" and spec.value:
        v = spec.value.lower()
        titles = [m["Title"] for m in movies if any(s.lower() == v for s in _multi(m.get("Style")))]
    elif spec.kind == "franchise" and spec.titles:
        # Franchise: intersect the checked member titles against both movies AND TV shows,
        # then sort by year ascending (so content plays in release order).
        # A title that is both a movie and a show resolves to the movie (movies are
        # indexed last so they win) — the same default as channel_engine.match_franchise.
        row_by_title = {}
        for typ, rows in (("show", shows), ("movie", movies)):
            for row in rows:
                if row.get("Title"):
                    row_by_title[row["Title"].lower()] = (row, typ)
        matched = [row_by_title[t.lower()] for t in spec.titles if t.lower() in row_by_title]
        # Sort by Year ascending; rows without a year go to the end.
        matched.sort(key=lambda rt: (_safe_int(rt[0].get("Year")) or 9999, rt[0]["Title"]))
        return [tag(r["Title"], typ) for r, typ in matched]
    typ = "show" if spec.kind in ("marathon", "tv_genre", "network", "programming_block") else "movie"
    return [tag(t, typ) for t in sorted({t for t in titles if t})]


def _auto_name(spec: CandidateSpec) -> str:
    if spec.kind == "genre":
        return f"{spec.genre} Movies"
    if spec.kind == "genre_decade":
        return f"{_DECADE_LABEL.get(spec.decade_start, '')} {spec.genre}".strip()
    if spec.kind == "blend":
        return " & ".join(spec.genres or [])
    if spec.kind == "studio":
        return spec.value or "Studio"
    if spec.kind == "director":
        return f"Directed by {spec.value}"
    if spec.kind == "actor":
        return f"{spec.value} Movies"
    if spec.kind == "tv_genre":
        return f"{spec.genre} TV"
    if spec.kind == "marathon":
        return f"{spec.value} 24/7"
    if spec.kind == "tv_movie_mix":
        return spec.genre or "Mixed"
    if spec.kind == "network":
        return spec.value or "Network"
    if spec.kind == "programming_block":
        return spec.name or "Programming Block"
    if spec.kind == "franchise":
        return spec.name or "Franchise"
    if spec.kind == "theme":
        return spec.name or "Themed Channel"
    if spec.kind == "country":
        return f"{spec.value} Cinema" if spec.value else "World Cinema"
    if spec.kind == "mood":
        return spec.value or "Mood Channel"
    if spec.kind == "style":
        return spec.value or "Style Channel"
    return spec.name or "Channel"


@router.post("/pipeline/compose")
def compose_channels(req: ComposeRequest):
    """Deterministically build channels.json from picked Planner candidate specs.

    Each spec is resolved against plex_library.csv into a title list; empties are
    skipped and reported.  Numbers are assigned sequentially tight-packed in the
    configured category order (channel_order config key), starting from req.start.
    Empty categories consume no numbers; input order within a category is preserved.
    """
    movies, shows = _load_library()
    if not movies and not shows:
        raise HTTPException(404, "Run Export first — plex_library.csv not found")

    # Resolve + bucket by category, preserving input order within each.
    buckets: dict[str, list[dict]] = {c: [] for c in _CATEGORY_ORDER}
    skipped: list[dict] = []
    for spec in req.specs:
        meta = _CATEGORY.get(spec.kind)
        if not meta:
            skipped.append({"name": spec.name or spec.kind, "reason": f"unknown kind '{spec.kind}'"})
            continue
        category, default_shuffle = meta
        name = spec.name or _auto_name(spec)

        # Live franchise: emit an identity content-ref instead of a static title list.
        # Cache miss → fall back to static so we never create a live channel that would
        # resolve from nothing (the scheduler would refuse to wipe it every cycle).
        per_channel_extras: dict = {}
        if spec.kind == "franchise" and spec.live and spec.titles and spec.name:
            fr_index = channel_engine.load_franchise_index(DATA_DIR)
            entry = fr_index.get(channel_engine._norm_franchise_name(spec.name))
            if entry:
                checked = {t.lower().strip() for t in spec.titles}
                exclude = [t for t in entry["titles"] if t.lower().strip() not in checked]
                content = [{"match": "franchise", "name": entry["name"],
                            "order": "release_date", "exclude": exclude}]
                per_channel_extras["live"] = True
                per_channel_extras["playback"] = {"structure": "interleaved", "episodes_per_block": 4}
                shuffle = spec.shuffle if spec.shuffle in ("ordered", "shuffle", "block") else "ordered"
            else:
                # Cache miss: static fallback (plain titles, not live).
                content = _resolve_spec(spec, movies, shows)
                shuffle = spec.shuffle if spec.shuffle in ("ordered", "shuffle", "block") else default_shuffle
        else:
            content = _resolve_spec(spec, movies, shows)
            shuffle = spec.shuffle if spec.shuffle in ("ordered", "shuffle", "block") else default_shuffle

        if not content:
            skipped.append({"name": name, "reason": "no matching titles"})
            continue
        buckets[category].append({"name": name, "shuffle": shuffle, "content": content,
                                   **per_channel_extras})

    # Batch-wide extras from the Planner toggles, applied to every built channel.
    extras: dict = {}
    if req.live:
        extras["live"] = True
    if req.commercials and req.commercials.get("filler_list_id"):
        extras["commercials"] = req.commercials

    # Sequential tight-packed numbering: categories in configured order, no fixed sizes,
    # empty categories skip entirely.  req.start is the first number (1 for a fresh deploy).
    cfg_order = channel_blocks.resolve_order(_load_config().get("channel_order"))
    counts = {cat: len(buckets.get(cat, [])) for cat in cfg_order}
    numbers = channel_blocks.assign_numbers(cfg_order, counts, req.start)

    channels: list[dict] = []
    for cat in cfg_order:
        items = buckets.get(cat, [])
        cat_numbers = numbers.get(cat, [])
        for num, ch in zip(cat_numbers, items):
            channels.append({"number": num, **ch, **extras})

    data = {"channels": channels, "orphaned": [], "suggested_channels": []}
    with open(DATA_DIR / "channels.draft.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return {
        "ok": True,
        "count": len(channels),
        "channels": [{"number": c["number"], "name": c["name"], "items": len(c["content"])} for c in channels],
        "skipped": skipped,
    }


@router.post("/pipeline/no-ai")
async def run_no_ai(
    start: int = Query(1),
    genres: Optional[str] = Query(None),
    decades: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
    min_items: Optional[int] = Query(None),
):
    args = []
    if start != 1:
        args += ["--start", str(start)]
    if genres is not None:
        args += ["--genres", genres]
    if decades is not None:
        args += ["--decades", decades]
    if types is not None:
        args += ["--types", types]
    if min_items is not None:
        args += ["--min-items", str(min_items)]
    # Pass the configured category order so the CLI generator matches the Planner.
    order = channel_blocks.resolve_order(_load_config().get("channel_order"))
    args += ["--order", ",".join(order)]
    return _sse(_stream("generate_no_ai.py", args, "no_ai"))


@router.post("/pipeline/collections")
async def run_collections(
    base: str = Query("80"),
    min_items: str = Query("3"),
    condense: bool = Query(False),
):
    args = ["--apply", "--base", base, "--min-items", min_items]
    if condense:
        args.append("--condense")
    return _sse(_stream("generate_from_collections.py", args, "collections"))


@router.post("/pipeline/probe")
async def run_probe(from_channel: Optional[str] = Query(None), protected: str = Query("")):
    # Review the in-progress Planner lineup (compose + AI extras + collections all write
    # channels.draft.json), NOT the already-deployed channels.json. The draft is what
    # deploy-selective pushes, so the probe must read the same file. Fall back to
    # create.py's default (channels.json) when there's no draft.
    args = ["--probe"]
    if (DATA_DIR / "channels.draft.json").exists():
        args += ["--json", "channels.draft.json"]
    if from_channel:
        args += ["--from", from_channel]
    if protected:
        # Keep the "would delete" preview honest in keep-mode (protected channels survive).
        args += ["--protect", protected]
    return _sse(_locked_stream("create.py", args, "probe"))


@router.post("/pipeline/deploy")
async def run_deploy(from_channel: Optional[str] = Query(None), protected: str = Query(""), no_delete: bool = Query(False)):
    args = []
    if no_delete:
        args.append("--no-delete")
    if from_channel:
        args += ["--from", from_channel]
    if protected:
        args += ["--protect", protected]
    return _sse(_locked_stream("create.py", args, "deploy"))


class DeployRequest(BaseModel):
    selections: list[DeploySelection]
    protected_numbers: list[int] = []
    no_delete: bool = False


def _reconcile_channels_json(protected_numbers: list[int]) -> None:
    """Write channels.json to mirror what create.py ACTUALLY pushed, then clear staging files.

    The deployed set is deploy_temp.json — the selected, number-remapped subset that
    deploy-selective built and create.py read.  Do NOT read channels.draft.json here:
    the draft may still contain channels the user DESELECTED and pre-remap numbers.
      wipe (no protected) -> channels.json = deployed set.
      keep (protected)    -> channels.json = existing protected entries merged with deployed set.
    Called ONLY after a successful create.py run.
    """
    deployed_path = DATA_DIR / "deploy_temp.json"
    canon_path = DATA_DIR / "channels.json"
    draft_path = DATA_DIR / "channels.draft.json"

    with open(deployed_path, encoding="utf-8") as f:
        deployed = json.load(f)
    deployed_channels = deployed.get("channels", [])

    if protected_numbers:
        protected = set(protected_numbers)
        try:
            with open(canon_path, encoding="utf-8") as f:
                existing = json.load(f).get("channels", [])
        except FileNotFoundError:
            existing = []
        by_num = {c["number"]: c for c in existing if c.get("number") in protected}
        for c in deployed_channels:
            by_num[c["number"]] = c  # just-deployed wins on collision
        out = {**deployed, "channels": sorted(by_num.values(), key=lambda c: c["number"])}
    else:
        out = deployed

    with open(canon_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    # Persist managed_names so surgical-deploy knows which channels the planner owns.
    deployed_names = [(c.get("name") or "").strip() for c in deployed_channels if c.get("name")]
    planner_state_path = DATA_DIR / "planner_state.json"
    existing_ps: dict = {}
    if planner_state_path.exists():
        try:
            with open(planner_state_path, encoding="utf-8") as f:
                existing_ps = json.load(f)
        except Exception:
            existing_ps = {}
    updated_ps = {**existing_ps, "managed_names": deployed_names}
    try:
        tmp = planner_state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(updated_ps, f, indent=2, ensure_ascii=False)
        tmp.replace(planner_state_path)
    except Exception:
        pass  # Non-fatal — deploy already succeeded

    for p in (draft_path, deployed_path):
        if p.exists():
            p.unlink()


async def _deploy_and_reconcile(args: list[str], protected_numbers: list[int]):
    """Stream create.py; reconcile channels.json from the deployed set on success.

    Reconcile runs BEFORE forwarding the terminal 'done' event so any client
    acting on 'done' already sees a consistent channels.json.  A failed deploy
    never writes channels.json.
    """
    async for chunk in _locked_stream("create.py", args, "deploy"):
        if chunk.startswith("data: "):
            try:
                payload = json.loads(chunk[6:].strip())
            except Exception:
                payload = None
            if payload and payload.get("type") == "done" and payload.get("returncode") == 0:
                try:
                    _reconcile_channels_json(protected_numbers)
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'line', 'text': f'WARNING: reconcile failed: {e}'})}\n\n"
        yield chunk


@router.post("/pipeline/deploy-selective")
async def run_deploy_selective(req: DeployRequest):
    draft_path = DATA_DIR / "channels.draft.json"
    if not draft_path.exists():
        raise HTTPException(404, "channels.draft.json not found — compose a lineup first")

    with open(draft_path, encoding="utf-8") as f:
        data = json.load(f)

    sel_map = {s.original_number: s for s in req.selections if s.include}
    new_channels = [
        {**ch, "number": sel_map[ch["number"]].deploy_number}
        for ch in data.get("channels", [])
        if ch.get("number") in sel_map
    ]
    data["channels"] = new_channels

    temp_path = DATA_DIR / "deploy_temp.json"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    args = ["--json", "deploy_temp.json"]
    if req.no_delete:
        args.append("--no-delete")
    if req.protected_numbers:
        args += ["--protect", ",".join(str(n) for n in req.protected_numbers)]
    return _sse(_deploy_and_reconcile(args, req.protected_numbers))


@router.get("/pipeline/draft")
def get_draft():
    """The in-progress Planner lineup (channels.draft.json): compose + AI extras so far.

    Distinct from GET /channels, which is the DEPLOYED record. The Collections step needs
    this so it places collections ABOVE the freshly-built lineup (including AI extras), not
    above the stale deployed set — otherwise apply_collections clobbers the AI channels.
    """
    p = DATA_DIR / "channels.draft.json"
    if not p.exists():
        return {"channels": [], "orphaned": [], "suggested_channels": []}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"channels": [], "orphaned": [], "suggested_channels": []}


@router.get("/pipeline/planner-state")
def get_planner_state():
    """Return the persisted Planner intent (data/planner_state.json), or {} if absent.

    Saved after every successful Build; restored when the user opens Add/Edit mode.
    Cleared (file deleted) when the user chooses Nuke.
    """
    p = DATA_DIR / "planner_state.json"
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@router.put("/pipeline/planner-state")
def save_planner_state(state: dict):
    """Persist the full Planner intent so Add/Edit mode can restore prior selections."""
    p = DATA_DIR / "planner_state.json"
    try:
        tmp = DATA_DIR / "planner_state.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(p)
    except Exception as e:
        raise HTTPException(500, f"Could not save planner state: {e}")
    return {"ok": True}


@router.delete("/pipeline/planner-state")
def delete_planner_state():
    """Clear planner_state.json (called on Nuke, so the next Add/Edit starts blank)."""
    p = DATA_DIR / "planner_state.json"
    if p.exists():
        p.unlink()
    return {"ok": True}


def _load_deploy_inputs() -> tuple[list[dict], list[dict], set[str]]:
    """Load the three inputs shared by deploy-preview and surgical-deploy.

    Returns (desired, deployed, prior_managed) where:
      desired      — channels from channels.draft.json
      deployed     — channels from channels.json (managed set only)
      prior_managed — lowercased name set from planner_state.json["managed_names"]
    """
    draft_path = DATA_DIR / "channels.draft.json"
    canon_path = DATA_DIR / "channels.json"
    planner_state_path = DATA_DIR / "planner_state.json"

    if not draft_path.exists():
        raise HTTPException(404, "channels.draft.json not found — compose a lineup first")

    with open(draft_path, encoding="utf-8") as f:
        draft_data = json.load(f)
    desired: list[dict] = draft_data.get("channels", [])

    if canon_path.exists():
        with open(canon_path, encoding="utf-8") as f:
            canon_data = json.load(f)
        deployed: list[dict] = canon_data.get("channels", []) if isinstance(canon_data, dict) else canon_data
    else:
        deployed = []

    planner_state: dict = {}
    if planner_state_path.exists():
        try:
            with open(planner_state_path, encoding="utf-8") as f:
                planner_state = json.load(f)
        except Exception:
            planner_state = {}
    prior_managed: set[str] = {
        n.strip().lower() for n in planner_state.get("managed_names", []) if n
    }

    return desired, deployed, prior_managed


class DeployPreviewRequest(BaseModel):
    mode: str = "edit"  # "edit" | "nuke"


@router.post("/pipeline/deploy-preview")
def deploy_preview(req: DeployPreviewRequest = DeployPreviewRequest()):
    """Return a diff preview of what the next deploy would do — NO Tunarr writes.

    Reads channels.draft.json (desired), channels.json (deployed managed set), and
    planner_state.json managed_names (prior_managed).

    For **edit** mode: runs channel_engine.classify_channels and returns the five
    named buckets (create / update / delete / unchanged / foreign), each as a list
    of {number, name}.  Update uses the DEPLOYED number (the real Tunarr channel),
    consistent with how surgical-deploy actually targets updates.

    For **nuke** mode: reflects what a wipe-and-rebuild would do — everything in the
    draft is in the "create" bucket; all currently-deployed managed channels are in
    the "delete" bucket (they will be wiped and recreated from scratch).  Foreign
    (hand-authored) channels are listed in "foreign" as always.
    """
    desired, deployed, prior_managed = _load_deploy_inputs()

    def _entry(ch: dict) -> dict:
        return {"number": ch.get("number"), "name": ch.get("name", "")}

    if req.mode == "nuke":
        # Nuke preview: honest about what wipe-and-rebuild does.
        # All draft channels will be (re)created; all managed deployed channels will
        # be deleted first.  Foreign channels are untouched.
        deployed_by_name = {(ch.get("name") or "").strip().lower(): ch for ch in deployed}
        create_items = [_entry(ch) for ch in desired]
        delete_items = []
        foreign_items = []
        for ch in deployed:
            name_key = (ch.get("name") or "").strip().lower()
            if name_key in prior_managed:
                delete_items.append(_entry(ch))
            else:
                foreign_items.append(_entry(ch))
        return {
            "create": create_items,
            "update": [],
            "delete": delete_items,
            "unchanged": [],
            "foreign": foreign_items,
        }

    # Edit mode: surgical classify.
    diff = channel_engine.classify_channels(desired, deployed, prior_managed)

    # For updates, return the DEPLOYED number (the real Tunarr channel number),
    # not the draft number.  This is consistent with how surgical-deploy targets them.
    update_items = [
        {"number": item["deployed"].get("number"), "name": item["desired"].get("name", "")}
        for item in diff["update"]
    ]

    return {
        "create": [_entry(ch) for ch in diff["create"]],
        "update": update_items,
        "delete": [_entry(ch) for ch in diff["delete"]],
        "unchanged": [_entry(ch) for ch in diff["unchanged"]],
        "foreign": [_entry(ch) for ch in diff["foreign"]],
    }


class SurgicalDeployRequest(BaseModel):
    # No extra parameters needed: desired = draft, deployed = channels.json.
    # These flags mirror DeployRequest for the cascade.
    pass


@router.post("/pipeline/surgical-deploy")
async def run_surgical_deploy():
    """Surgical diff deploy for Add/Edit mode.

    Desired state  = channels.draft.json  (the planner's fresh output).
    Current state  = channels.json        (managed channels only — no orphans).

    Classifies each channel as create / delete / update-in-place / unchanged
    using channel_engine.classify_channels, then executes the minimum set of
    Tunarr operations:
      - create  → channels.json is written first, then create.py deploys just
                  those channels (via a temp file).
      - delete  → removed from Tunarr and from channels.json.
      - update  → update_channel_in_place (preserves Tunarr id + Plex DVR mapping).
      - unchanged → no Tunarr calls.

    Invariants (enforced by classify_channels + route):
      1. Live channels are NEVER in the delete bucket — they land in update-in-place.
      2. Orphan channels (in Tunarr but absent from channels.json) are outside both
         input sets and are never touched.

    Holds scheduler.deploy_lock for the full operation.
    """
    # Load draft_data separately (we need the full structure for the create step).
    draft_path = DATA_DIR / "channels.draft.json"
    if not draft_path.exists():
        raise HTTPException(404, "channels.draft.json not found — compose a lineup first")
    with open(draft_path, encoding="utf-8") as f:
        draft_data = json.load(f)

    desired, deployed, prior_managed = _load_deploy_inputs()
    canon_path = DATA_DIR / "channels.json"

    # Also reload planner_state for the managed_names merge after deploy.
    planner_state_path = DATA_DIR / "planner_state.json"
    planner_state: dict = {}
    if planner_state_path.exists():
        try:
            with open(planner_state_path, encoding="utf-8") as f:
                planner_state = json.load(f)
        except Exception:
            planner_state = {}

    diff = channel_engine.classify_channels(desired, deployed, prior_managed)

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    async def _stream_surgical() -> AsyncGenerator[str, None]:
        def _emit(text: str) -> str:
            return f"data: {json.dumps({'type': 'line', 'text': text})}\n\n"

        yield f"data: {json.dumps({'type': 'start', 'cmd': 'surgical-deploy', 'log': 'surgical'})}\n\n"
        yield _emit(f"Surgical deploy: {len(diff['create'])} create, {len(diff['delete'])} delete, "
                    f"{len(diff['update'])} update, {len(diff['unchanged'])} unchanged, "
                    f"{len(diff['foreign'])} foreign (untouched)")

        errors: list[str] = []

        async with scheduler.deploy_lock:
            # ── 0. Build Tunarr library index once for all updates ────────────
            # Hoisted out of the per-channel closure so the index is fetched only once
            # regardless of how many channels need updating (Fix 3 — efficiency).
            if diff["update"]:
                library_index = await asyncio.to_thread(
                    channel_engine.build_library_index_with_ids, tunarr_url
                )
                movie_map_shared, show_map_shared, id_index_shared = library_index
            else:
                movie_map_shared, show_map_shared, id_index_shared = {}, {}, None

            franchise_index_shared = channel_engine.load_franchise_index(DATA_DIR)

            # ── 1. Updates in place ───────────────────────────────────────────
            for item in diff["update"]:
                desired_ch = item["desired"]
                # Target the DEPLOYED channel's number — update-in-place preserves the real
                # Tunarr channel (id + DVR). The draft renumbers existing channels (Add/Edit
                # numbers from highest+1), so desired_ch's number is a fresh high number that
                # does NOT exist in Tunarr; using it would target a missing channel.
                num = item["deployed"].get("number") or desired_ch.get("number")
                name = desired_ch.get("name", "")
                yield _emit(f"  Updating #{num} {name} in place…")

                def _do_update(ch=desired_ch, n=num, mv=movie_map_shared, sh=show_map_shared,
                               fi=franchise_index_shared, ix=id_index_shared):
                    plex_sections, collection_cache = [], {}
                    if any(isinstance(it, dict) and "collection" in it for it in ch.get("content", [])):
                        if plex_url and plex_token:
                            plex_sections = channel_engine.get_plex_sections(plex_url, plex_token)
                    resolved, missing = channel_engine.resolve_content(
                        ch.get("content", []), mv, sh,
                        plex_url=plex_url, plex_token=plex_token,
                        plex_sections=plex_sections, collection_cache=collection_cache,
                        franchise_index=fi, id_index=ix,
                    )
                    if not resolved:
                        raise channel_engine.ChannelEngineError(
                            f"Channel #{n}: resolved to empty — refusing to update")
                    comm = ch.get("commercials") or {}
                    pad_ms = int(comm.get("pad_minutes", 5)) * 60000 if comm.get("filler_list_id") else 0
                    channel_engine.update_channel_in_place(
                        tunarr_url, n, ch.get("shuffle", "shuffle"), resolved, pad_ms=pad_ms,
                        expected_name=ch.get("name"), playback=ch.get("playback"))
                    return missing

                try:
                    missing = await asyncio.to_thread(_do_update)
                    if missing:
                        yield _emit(f"    WARNING: {len(missing)} titles not found in library: {', '.join(missing[:5])}")
                    yield _emit(f"  ✓ Updated #{num} {name}")
                except channel_engine.ChannelEngineError as e:
                    errors.append(f"#{num} {name}: {e}")
                    yield _emit(f"  ! Error updating #{num} {name}: {e}")

            # ── 2. Deletes ────────────────────────────────────────────────────
            for dep_ch in diff["delete"]:
                num = dep_ch.get("number")
                name = dep_ch.get("name", "")
                yield _emit(f"  Deleting #{num} {name}…")

                def _do_delete(n=num):
                    tunarr_ch = channel_engine.find_channel_by_number(tunarr_url, n)
                    if tunarr_ch:
                        channel_engine.api(tunarr_url, "DELETE", f"/api/channels/{tunarr_ch['id']}")
                    return tunarr_ch is not None

                try:
                    found = await asyncio.to_thread(_do_delete)
                    if not found:
                        yield _emit(f"    (#{num} not in Tunarr — skipped)")
                    yield _emit(f"  ✓ Deleted #{num} {name}")
                except Exception as e:
                    errors.append(f"delete #{num} {name}: {e}")
                    yield _emit(f"  ! Error deleting #{num} {name}: {e}")

            # ── 3. Creates — run via create.py on a temp file ─────────────────
            if diff["create"]:
                create_temp = DATA_DIR / "surgical_create_temp.json"
                create_data = {**draft_data, "channels": diff["create"]}
                with open(create_temp, "w", encoding="utf-8") as f:
                    json.dump(create_data, f, indent=2, ensure_ascii=False)
                yield _emit(f"  Creating {len(diff['create'])} new channel(s) via create.py…")

                collected: list[str] = []
                create_ok = False
                async for chunk in _stream("create.py", ["--json", "surgical_create_temp.json", "--no-delete"], "surgical_create"):
                    if chunk.startswith("data: "):
                        try:
                            payload = json.loads(chunk[6:].strip())
                            if payload.get("type") == "line":
                                collected.append(payload["text"])
                                yield chunk
                            elif payload.get("type") == "done":
                                create_ok = payload.get("returncode") == 0
                                yield chunk
                        except Exception:
                            yield chunk
                    else:
                        yield chunk

                if create_temp.exists():
                    create_temp.unlink()

                if not create_ok:
                    errors.append("create.py failed for new channels")

            # ── 4. Write channels.json — merge all buckets ────────────────────
            # The new channels.json contains:
            #   • all desired channels (create + update + unchanged — from the draft)
            #   • all foreign channels (hand-authored, absent from desired, NOT in
            #     prior_managed — must be preserved untouched)
            # Channels in diff["delete"] are intentionally excluded (they're not in desired).
            #
            # Existing channels (update + unchanged) keep their DEPLOYED number; only genuine
            # new (create) channels use their draft number. (See merge_deployed_numbers.)
            new_managed = channel_engine.merge_deployed_numbers(desired, deployed)

            # Append foreign channels — they were not in desired but must be kept.
            new_managed.extend(diff["foreign"])

            new_managed.sort(key=lambda c: c.get("number", 0))
            out_data = {
                "channels": new_managed,
                "orphaned": [],
                "suggested_channels": [],
            }
            with open(canon_path, "w", encoding="utf-8") as f:
                json.dump(out_data, f, indent=2, ensure_ascii=False)

            # Persist managed_names so future surgical deploys know which channels
            # were planner-built (provenance for delete safety).
            new_managed_names = [(ch.get("name") or "").strip() for ch in desired if ch.get("name")]
            updated_planner_state = {**planner_state, "managed_names": new_managed_names}
            try:
                tmp = planner_state_path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(updated_planner_state, f, indent=2, ensure_ascii=False)
                tmp.replace(planner_state_path)
            except Exception as e:
                # Non-fatal — log but don't fail the deploy.
                yield _emit(f"  WARNING: could not update planner_state.json managed_names: {e}")

            # Clear the draft.
            if draft_path.exists():
                draft_path.unlink()

        if errors:
            yield _emit(f"Completed with {len(errors)} error(s): {'; '.join(errors)}")
            yield f"data: {json.dumps({'type': 'done', 'returncode': 1, 'log': 'surgical'})}\n\n"
        else:
            yield _emit(f"Done: {len(diff['create'])} created, {len(diff['delete'])} deleted, "
                        f"{len(diff['update'])} updated, {len(diff['unchanged'])} unchanged, "
                        f"{len(diff['foreign'])} foreign kept")
            yield f"data: {json.dumps({'type': 'done', 'returncode': 0, 'log': 'surgical'})}\n\n"

    return _sse(_stream_surgical())


@router.post("/pipeline/images")
async def run_images():
    return _sse(_stream("fetch_images.py", ["--apply"], "images"))


@router.post("/pipeline/sync")
async def run_sync():
    return _sse(_stream("sync_plex.py", [], "sync"))


@router.get("/pipeline/collections")
def list_collections():
    cfg = _load_config()
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    if not plex_url or not plex_token:
        raise HTTPException(400, "Plex not configured")
    try:
        sections_data = _plex_get(plex_url, plex_token, "/library/sections")
        sections = sections_data["MediaContainer"].get("Directory", [])
    except Exception as e:
        raise HTTPException(502, f"Could not reach Plex: {e}")

    results = []
    seen: set[str] = set()
    for section in sections:
        try:
            col_data = _plex_get(plex_url, plex_token, f"/library/sections/{section['key']}/collections")
            for c in col_data.get("MediaContainer", {}).get("Metadata", []):
                name = c.get("title", "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                results.append({
                    "id": c.get("ratingKey", ""),
                    "name": name,
                    "count": int(c.get("childCount", 0)),
                    "section": section.get("title", ""),
                    "summary": c.get("summary", ""),
                    "has_poster": bool(c.get("thumb", "")),
                })
        except Exception:
            continue
    return results


@router.get("/pipeline/collections/{collection_id}/poster")
def collection_poster(collection_id: str):
    cfg = _load_config()
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    if not plex_url or not plex_token:
        raise HTTPException(400, "Plex not configured")
    url = f"{plex_url}/library/metadata/{collection_id}/thumb?X-Plex-Token={plex_token}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read()
            content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(content=content, media_type=content_type)
    except Exception as e:
        raise HTTPException(502, f"Could not fetch poster: {e}")


@router.post("/pipeline/collections/apply")
def apply_collections(selections: list[CollectionSelection]):
    channels_path = DATA_DIR / "channels.draft.json"
    if channels_path.exists():
        with open(channels_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"channels": [], "orphaned": [], "suggested_channels": []}

    included = [s for s in selections if s.include]
    if not included:
        return {"ok": True, "added": 0}

    min_ch = min(s.channel_number for s in included)
    kept = [ch for ch in data.get("channels", []) if ch.get("number", 0) < min_ch]
    new_channels = [
        {
            "number": s.channel_number,
            "name": s.name,
            "shuffle": "shuffle",
            "content": [{"collection": s.name}],
        }
        for s in sorted(included, key=lambda x: x.channel_number)
    ]
    data["channels"] = kept + new_channels
    with open(channels_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return {"ok": True, "added": len(new_channels)}
