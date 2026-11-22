#!/usr/bin/env python3
"""
channel_engine.py — Shared Tunarr channel resolution engine.

Pure, importable building blocks shared by create.py (CLI deploy), the live-channel
scheduler, and the recipe-preview endpoint. Every function is parameterized by
tunarr_url / plex_url / token — nothing here reads config.json or touches argv, so
it is safe to import into the FastAPI process. CLI-only concerns (config loading,
delete/create, argparse) stay in create.py.

No dependencies beyond the Python standard library.
"""

import base64
import json
import os
import re
import uuid
import urllib.error
import urllib.request


class ChannelEngineError(Exception):
    """Raised for unrecoverable engine conditions (e.g. no Plex source in Tunarr).

    Engine code must never call sys.exit() — it can run inside the long-lived
    FastAPI process. Callers (create.py main()) translate this into an exit.
    """


SHUFFLE_MAP = {
    "ordered": "ordered",
    "shuffle": "shuffle",
    "block":   "block",
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def plex_get(base_url, token, path, timeout=60):
    sep = "&" if "?" in path else "?"
    url = base_url + path + sep + f"X-Plex-Token={token}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  ! Plex HTTP {e.code} [{path[:60]}]")
        return None
    except Exception as e:
        print(f"  ! Plex error [{path[:60]}]: {e}")
        return None


# ── Tunarr auth ────────────────────────────────────────────────────────────────
# Tunarr gained optional HTTP basic auth on its API (tunarr #1865). Threading a
# credential parameter through every engine function that takes tunarr_url would
# touch ~20 signatures, so it lives here as module state instead — still no
# config.json read inside the engine (callers pass the values in).
# ponytail: one Tunarr per process. If Programmarr ever drives two at once, this
# becomes a per-url dict.
_TUNARR_AUTH = None


def set_tunarr_auth(username="", password=""):
    """Set (or clear, with blanks) the basic-auth credential for Tunarr calls."""
    global _TUNARR_AUTH
    if username or password:
        raw = f"{username}:{password}".encode()
        _TUNARR_AUTH = "Basic " + base64.b64encode(raw).decode()
    else:
        _TUNARR_AUTH = None


def set_tunarr_auth_from_config(cfg):
    """Convenience for callers that already hold a loaded config dict."""
    set_tunarr_auth(cfg.get("tunarr_username", ""), cfg.get("tunarr_password", ""))


def tunarr_headers(extra=None):
    """Standard headers for any Tunarr request, including auth when configured.

    Every module that talks to Tunarr (engine, export, icons, status routes)
    goes through this so enabling auth can't half-work.
    """
    headers = {"Accept": "application/json", "User-Agent": "Programmarr"}
    if _TUNARR_AUTH:
        headers["Authorization"] = _TUNARR_AUTH
    if extra:
        headers.update(extra)
    return headers


def api(tunarr_url, method, path, body=None, timeout=60):
    url = tunarr_url + path
    data = json.dumps(body).encode() if body is not None else None
    headers = tunarr_headers()
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        print(f"  ! HTTP {e.code} [{method} {path}]: {raw[:200]}")
        return None
    except Exception as e:
        print(f"  ! Error [{method} {path}]: {e}")
        return None


def get_library_programs(tunarr_url, lib_id, timeout=120):
    """All programs in a Tunarr library. Handles both response shapes: a bare list
    (Tunarr before 2026.11) and the paged {total, result} wrapper (Tunarr #2423).
    Returns None on any failure — never a partial library, which would deploy
    half-empty channels — so callers keep their failed-library handling."""
    path = f"/api/media-libraries/{lib_id}/programs"
    first = api(tunarr_url, "GET", path, timeout=timeout)
    if first is None or isinstance(first, list):
        return first
    programs = list(first.get("result") or [])
    total = first.get("total", len(programs))
    while len(programs) < total:
        page = api(tunarr_url, "GET", f"{path}?offset={len(programs)}&limit=1000", timeout=timeout)
        if page is None:
            return None
        batch = page.get("result") or []
        if not batch:
            break
        programs.extend(batch)
    return programs


# ── Library indexing ───────────────────────────────────────────────────────────

def get_transcode_config(tunarr_url):
    configs = api(tunarr_url, "GET", "/api/transcode_configs") or []
    if not configs:
        return None
    # ponytail: index 0 unless config.json names one. Tunarr 1.3+ users commonly
    # have several (a 4K profile, a hardware profile) and silently picking the
    # wrong one produces channels their box can't play — so at minimum, say which.
    chosen = configs[0]
    if len(configs) > 1:
        print(f"  Note: {len(configs)} transcode configs in Tunarr; using "
              f"'{chosen.get('name', chosen.get('id'))}'")
    return chosen["id"]


def get_plex_source(tunarr_url):
    sources = api(tunarr_url, "GET", "/api/media-sources") or []
    return next((s for s in sources if s.get("type") == "plex"), None)


def get_plex_sources(tunarr_url):
    sources = api(tunarr_url, "GET", "/api/media-sources") or []
    return [s for s in sources if s.get("type") == "plex"]


def get_tunarr_version(tunarr_url):
    """Tunarr's self-reported version string, or None if it won't say.

    Diagnostics only — never gate on this. See _endpoint_status for why.
    """
    v = api(tunarr_url, "GET", "/api/version")
    if isinstance(v, dict):
        return v.get("tunarr") or None
    return None


def _endpoint_status(tunarr_url, path, timeout=10):
    """HTTP status code for a bare GET, or None if the host didn't answer.

    Used only on error paths, to tell "this Tunarr does not HAVE this endpoint"
    (404) from "the call failed" — which api() flattens into the same None.
    """
    try:
        req = urllib.request.Request(tunarr_url + path, headers=tunarr_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def _version_suffix(tunarr_url):
    v = get_tunarr_version(tunarr_url)
    return f" (Tunarr reports version {v}.)" if v else ""


def _no_plex_source_error(tunarr_url):
    """Build an honest error for a Tunarr with no Plex media source.

    Tunarr also supports jellyfin/emby/local sources. Programmarr's metadata
    layer is Plex-only, so those users are genuinely unsupported — but telling
    them "No Plex source found" reads like a broken Plex connection and sends
    them debugging the wrong thing. Name what was actually there.
    """
    sources = api(tunarr_url, "GET", "/api/media-sources")
    if sources is None:
        # Capability detection rather than a version gate: a missing endpoint is
        # the thing that actually breaks us, and it's directly observable —
        # guessing a minimum version number would be less accurate, not more.
        status = _endpoint_status(tunarr_url, "/api/media-sources")
        if status == 404:
            return ChannelEngineError(
                "This Tunarr does not have the /api/media-sources endpoint, which "
                "Programmarr needs to read your libraries. That endpoint arrived in "
                "Tunarr 1.x, so this server is most likely too old — please update "
                "Tunarr." + _version_suffix(tunarr_url)
            )
        if status in (401, 403):
            # We know exactly what's wrong here, so don't make them check three
            # things. Distinguish "you set no credentials" from "yours are wrong".
            if _TUNARR_AUTH:
                return ChannelEngineError(
                    f"Tunarr rejected the username and password ({status}). Check "
                    "tunarr_username / tunarr_password in Settings -> Connections."
                )
            return ChannelEngineError(
                f"This Tunarr requires a username and password ({status}), and none "
                "are configured. Set tunarr_username / tunarr_password in "
                "Settings -> Connections."
            )
        return ChannelEngineError(
            "Could not read media sources from Tunarr — check that tunarr_url is "
            "correct and Tunarr is reachable (and that Tunarr basic auth, if you "
            "enabled it, is configured in Programmarr's settings)."
            + _version_suffix(tunarr_url)
        )
    if not sources:
        return ChannelEngineError(
            "Tunarr has no media sources configured. Add your Plex server in "
            "Tunarr first (Settings -> Media Sources), then run Programmarr."
        )
    found = sorted({s.get("type", "unknown") for s in sources})
    return ChannelEngineError(
        "Programmarr currently requires a Plex-backed Tunarr source. "
        f"Found: {', '.join(found)}. Jellyfin/Emby support is not implemented yet."
    )


def build_library_index(tunarr_url):
    plex_sources = get_plex_sources(tunarr_url)
    if not plex_sources:
        raise _no_plex_source_error(tunarr_url)

    movie_map = {}
    # title-key -> {title, by_lib: {lib_id: {showId, programs}}}
    # Collected across ALL Plex sources before picking the best copy per show.
    tv_candidates = {}
    # api() reports every failure as None, and treating that as "no programs"
    # turns an outage into a silent empty index — which downstream looks like a
    # library with no content and would deploy channels with nothing on them.
    failed_libs = []
    attempted_libs = 0

    for source in plex_sources:
        source_name = source.get("name", "Plex")
        libs = source.get("libraries", [])
        # A Plex server can expose MULTIPLE movie or shows libraries (e.g. 'TV Shows' AND
        # 'Cartoons'). Index every enabled one of each kind across all sources — picking only
        # the first source/library silently drops whole libraries.
        movie_libs = [l for l in libs if l.get("mediaType") in ("movie", "movies") and l.get("enabled")]
        tv_libs    = [l for l in libs if l.get("mediaType") == "shows"              and l.get("enabled")]

        if movie_libs:
            print(f"  Indexing movies ({source_name})...")
            for lib in movie_libs:
                programs = get_library_programs(tunarr_url, lib["id"])
                if programs is None:
                    failed_libs.append(f"{source_name}/{lib.get('name', lib['id'])}")
                    continue
                attempted_libs += 1
                for p in programs:
                    title = p.get("program", {}).get("title", "")
                    if title:
                        key = title.lower().strip()
                        if key not in movie_map:
                            movie_map[key] = p

        if tv_libs:
            print(f"  Indexing TV shows ({source_name})...")
            for lib in tv_libs:
                programs = get_library_programs(tunarr_url, lib["id"])
                if programs is None:
                    failed_libs.append(f"{source_name}/{lib.get('name', lib['id'])}")
                    continue
                attempted_libs += 1
                for p in programs:
                    prog = p.get("program", {})
                    show = prog.get("show", {})
                    show_id = show.get("uuid") or prog.get("showId")
                    title = show.get("title", "")
                    if not show_id or not title:
                        continue
                    key = title.lower().strip()
                    c = tv_candidates.setdefault(key, {"title": title, "by_lib": {}})
                    entry = c["by_lib"].setdefault(lib["id"], {"showId": show_id, "programs": []})
                    entry["programs"].append(p)

    if failed_libs and attempted_libs == 0:
        raise ChannelEngineError(
            "Could not read any library from Tunarr (" + ", ".join(failed_libs) + "). "
            "Tunarr answered, but every library request failed — check that Tunarr is "
            "healthy and finished scanning. Refusing to continue with an empty index."
        )
    if failed_libs:
        print(f"  ! WARNING: {len(failed_libs)} librar"
              f"{'y' if len(failed_libs) == 1 else 'ies'} could not be read and "
              f"{'is' if len(failed_libs) == 1 else 'are'} missing from this index: "
              + ", ".join(failed_libs))
        print("  ! Channels built now may be missing content from those libraries.")

    print(f"  Indexed {len(movie_map)} movies")

    # For each show pick the single copy (across all sources/libraries) with the most
    # PLAYABLE episodes, so a dead duplicate never shadows the real one or inflates the
    # live-channel diff into churn.
    show_map = {}
    def _playable(entry):
        return sum(1 for p in entry["programs"] if p.get("program", {}).get("state") != "missing")
    for key, c in tv_candidates.items():
        best = max(c["by_lib"].values(), key=_playable)
        show_map[key] = {"title": c["title"], "showId": best["showId"], "programs": best["programs"]}
    print(f"  Indexed {len(show_map)} TV shows")

    return movie_map, show_map


# ── Title resolution ───────────────────────────────────────────────────────────

def _playable_count(programs):
    return sum(1 for p in programs if p.get("program", {}).get("state") != "missing")


def resolve_title(title, movie_map, show_map, kind=None):
    key = title.lower().strip()
    movie = movie_map.get(key) if kind != "show" else None
    show = show_map.get(key) if kind != "movie" else None
    # Same exact title for a movie AND a series (e.g. the 2017 "Baywatch" film vs the
    # 1989 series). A plain title can't disambiguate, so prefer whichever has more
    # PLAYABLE programs — the same tie-break build_library_index uses for dupes. A real
    # series (many episodes) beats a lone movie; an all-missing series yields to it.
    # A typed ref ({"movie": t} / {"show": t}, passed as `kind`) skips the tie-break.
    if movie and show:
        if _playable_count(show["programs"]) >= _playable_count([movie]):
            movie = None
        else:
            show = None
    if movie is not None:
        return {"type": "Movie", "title": title, "programs": [movie]}
    if show is not None:
        return {"type": "TV", "title": show["title"], "showId": show["showId"], "programs": show["programs"]}
    return None


# ── Franchise matching (live recipes) ──────────────────────────────────────────

def _word_boundary_match(value, title):
    """True if `value` appears in `title` on word boundaries (case-insensitive).

    Word-boundary, not raw substring: "It" matches "It Follows" but NOT
    "Little Women". Multi-word values work too ("Bad Boys" matches "Bad Boys II").
    """
    if not value:
        return False
    return re.search(r"\b" + re.escape(value) + r"\b", title, re.IGNORECASE) is not None


def match_titles(value, movie_map, show_map, order=None, exclude=None):
    """Franchise matcher for {"match": "title_contains"} content refs.

    Scans the Tunarr library for titles containing `value` on word boundaries and
    returns (resolved_items, preview). `resolved_items` are ready for build_schedule;
    `preview` is a [{title, year}] list (same order) for the author-time confirm UI.

    order="release_date" sorts movies by releaseDate ascending (unknown dates last);
    any other value sorts alphabetically. `exclude` is a case-insensitive list of
    titles to drop (the per-recipe false-positive escape hatch).
    """
    exclude_set = {e.lower().strip() for e in (exclude or [])}
    matched = []  # (sort_release_ms, year, title, item)

    for key, p in movie_map.items():
        if key in exclude_set:
            continue
        prog = p.get("program", {})
        title = prog.get("title", "")
        if _word_boundary_match(value, title):
            release_ms = prog.get("releaseDate")
            matched.append((
                release_ms if release_ms is not None else float("inf"),
                prog.get("year"),
                title,
                {"type": "Movie", "title": title, "programs": [p]},
            ))

    for key, s in show_map.items():
        if key in exclude_set:
            continue
        title = s["title"]
        if _word_boundary_match(value, title):
            first_prog = s["programs"][0].get("program", {}) if s.get("programs") else {}
            matched.append((
                float("inf"),  # shows have no single release date — sort to the end
                first_prog.get("year"),
                title,
                {"type": "TV", "title": title, "showId": s["showId"], "programs": s["programs"]},
            ))

    if order == "release_date":
        matched.sort(key=lambda t: (t[0], t[2].lower()))
    else:
        matched.sort(key=lambda t: t[2].lower())

    resolved = [t[3] for t in matched]
    preview = [{"title": t[2], "year": t[1]} for t in matched]
    return resolved, preview


def _norm_franchise_name(name):
    return " ".join((name or "").lower().split())


def load_franchise_index(data_dir):
    """Franchise membership from the Planner's caches, keyed by normalized name.

    {norm_name: {"name": display_name, "titles": [member title, ...]}}
    Sources: data_dir/wikidata_cache.json (series/franchise members, spans movies
    and TV) and data_dir/tmdb_enrichment.json (belongs_to_collection groups).
    TMDB wins name collisions (its collection data is more precise — same rule as
    the Planner's _merge_franchises). Best-effort: a missing/corrupt cache simply
    contributes nothing; worst case is {}.
    """
    index = {}

    try:
        with open(os.path.join(str(data_dir), "wikidata_cache.json"), encoding="utf-8") as f:
            for fr in (json.load(f).get("franchises") or []):
                name = (fr.get("name") or "").strip()
                titles = [m.get("title") for m in fr.get("members") or [] if m.get("title")]
                if name and titles:
                    index[_norm_franchise_name(name)] = {"name": name, "titles": titles}
    except (OSError, ValueError):
        pass

    try:
        with open(os.path.join(str(data_dir), "tmdb_enrichment.json"), encoding="utf-8") as f:
            enrichment = json.load(f).get("enrichment") or {}
        buckets = {}
        for title, rec in enrichment.items():
            coll = rec.get("collection") or {}
            coll_id, coll_name = coll.get("id"), (coll.get("name") or "").strip()
            if coll_id and coll_name:
                buckets.setdefault(coll_id, {"name": coll_name, "titles": []})["titles"].append(title)
        for b in buckets.values():
            index[_norm_franchise_name(b["name"])] = b  # TMDB overwrites → wins
    except (OSError, ValueError):
        pass

    return index


def match_franchise(name, franchise_index, movie_map, show_map, order=None, exclude=None):
    """Resolver for {"match": "franchise"} content refs.

    Identity-based: members come from the cached TMDB/Wikidata franchise data
    (load_franchise_index), NOT from name matching — so a franchise channel works
    even when members share no words with the franchise name (MCU → "Iron Man").
    Returns (resolved_items, preview) exactly like match_titles. Unknown
    franchise or missing index → ([], []) — callers treat that as "matched
    nothing" and refuse to wipe live channels downstream.
    """
    entry = (franchise_index or {}).get(_norm_franchise_name(name))
    if not entry:
        return [], []

    exclude_set = {e.lower().strip() for e in (exclude or [])}
    matched = []  # (sort_release_ms, year, title, item) — same shape as match_titles

    for member_title in entry["titles"]:
        key = (member_title or "").lower().strip()
        if not key or key in exclude_set:
            continue
        p = movie_map.get(key)
        if p is not None:
            prog = p.get("program", {})
            release_ms = prog.get("releaseDate")
            title = prog.get("title", member_title)
            matched.append((
                release_ms if release_ms is not None else float("inf"),
                prog.get("year"), title,
                {"type": "Movie", "title": title, "programs": [p]},
            ))
            continue
        s = show_map.get(key)
        if s is not None:
            first_prog = s["programs"][0].get("program", {}) if s.get("programs") else {}
            matched.append((
                float("inf"),  # shows have no single release date — sort to the end
                first_prog.get("year"), s["title"],
                {"type": "TV", "title": s["title"], "showId": s["showId"], "programs": s["programs"]},
            ))

    if order == "release_date":
        matched.sort(key=lambda t: (t[0], t[2].lower()))
    else:
        matched.sort(key=lambda t: t[2].lower())

    return [t[3] for t in matched], [{"title": t[2], "year": t[1]} for t in matched]


# ── Plex collection resolution ─────────────────────────────────────────────────

def get_plex_sections(plex_url, token):
    data = plex_get(plex_url, token, "/library/sections")
    if not data:
        return []
    return data["MediaContainer"].get("Directory", [])


def resolve_collection(plex_url, token, name, sections, cache):
    """Return a list of titles from a named Plex collection (cached)."""
    key = name.lower().strip()
    if key in cache:
        return cache[key]

    titles = []
    for section in sections:
        section_key = section.get("key")
        data = plex_get(plex_url, token, f"/library/sections/{section_key}/collections")
        if not data:
            continue
        collections = data["MediaContainer"].get("Metadata", [])
        match = next((c for c in collections if c.get("title", "").lower().strip() == key), None)
        if match:
            rating_key = match["ratingKey"]
            # Some Plex collection types (e.g. Kometa smart collections) return
            # size=0 from /library/metadata/{id}/children but work correctly via
            # /library/collections/{id}/children — try collections endpoint first.
            for children_path in (
                f"/library/collections/{rating_key}/children",
                f"/library/metadata/{rating_key}/children",
            ):
                items_data = plex_get(plex_url, token, children_path)
                if items_data:
                    items = items_data["MediaContainer"].get("Metadata", [])
                    titles = [item["title"] for item in items if item.get("title")]
                    if titles:
                        break
            break

    cache[key] = titles
    return titles


# ── Content resolution ─────────────────────────────────────────────────────────

def resolve_content(content_list, movie_map, show_map,
                    plex_url=None, plex_token=None, plex_sections=None, collection_cache=None,
                    franchise_index=None):
    """Resolve a channel's content list into (resolved_items, missing).

    Each entry is one of:
    - A plain title string — matched against the Tunarr library index by exact title.
    - A {"movie": "Title"} or {"show": "Title"} ref — exact title, restricted to that
      media type (disambiguates a movie and a show that share a title).
    - A {"collection": "Name"} ref — expanded to member titles via Plex, then each
      title is matched against the library index.
    - A {"match": "title_contains", "value": "..."} ref — word-boundary scan of the
      Tunarr library; order/exclude supported.
    - A {"match": "franchise", "name": "..."} ref — identity-based resolution via the
      franchise index (load_franchise_index). Requires franchise_index to be passed;
      a missing index or unknown franchise name degrades to a missing-entry warning so
      downstream refuse-to-wipe guards keep live channels safe.

    Returns (resolved_items, missing): resolved_items are ready for build_schedule;
    missing is a list of titles/ref labels that could not be found.
    """
    plex_sections = plex_sections or []
    collection_cache = collection_cache if collection_cache is not None else {}

    # Expand {"collection": "Name"} → member titles; {"match": ...} → resolved items
    expanded_titles = []
    matched_items = []
    missing = []
    for entry in content_list:
        if isinstance(entry, dict) and ("movie" in entry or "show" in entry):
            kind = "movie" if "movie" in entry else "show"
            expanded_titles.append((entry[kind], kind))  # resolved in order below
        elif isinstance(entry, dict) and "collection" in entry:
            col_name = entry["collection"]
            col_titles = resolve_collection(plex_url, plex_token, col_name, plex_sections, collection_cache)
            if col_titles:
                expanded_titles.extend(col_titles)
                print(f"    Collection '{col_name}': {len(col_titles)} titles")
            else:
                print(f"    WARNING: Collection '{col_name}' not found in Plex")
                missing.append(f"[collection:{col_name}]")
        elif isinstance(entry, dict) and "match" in entry:
            if entry["match"] == "franchise" and entry.get("name"):
                fr_name = entry["name"]
                items, _ = match_franchise(fr_name, franchise_index, movie_map, show_map,
                                           order=entry.get("order"), exclude=entry.get("exclude"))
                if items:
                    matched_items.extend(items)
                    print(f"    Franchise '{fr_name}': {len(items)} titles")
                else:
                    print(f"    WARNING: franchise '{fr_name}' matched nothing (cache missing or no library members)")
                    missing.append(f"[franchise:{fr_name}]")
            elif entry["match"] == "title_contains" and entry.get("value"):
                value = entry["value"]
                items, _ = match_titles(value, movie_map, show_map,
                                        order=entry.get("order"), exclude=entry.get("exclude"))
                if items:
                    matched_items.extend(items)
                    print(f"    Match '{value}': {len(items)} titles")
                else:
                    print(f"    WARNING: match '{value}' matched nothing in library")
                    missing.append(f"[match:{value}]")
            else:
                value = entry.get("value", "")
                print(f"    WARNING: unsupported match ref: {entry}")
                missing.append(f"[match:{value or entry.get('match')}]")
        else:
            expanded_titles.append(entry)

    resolved = []
    for entry in expanded_titles:
        title, kind = entry if isinstance(entry, tuple) else (entry, None)
        item = resolve_title(title, movie_map, show_map, kind=kind)
        if item:
            resolved.append(item)
        else:
            missing.append(title)

    resolved.extend(matched_items)
    return resolved, missing


# ── Schedule builder ───────────────────────────────────────────────────────────

def _episode_sort_key(p):
    """Sort key for a show's episode programs in season/episode order.
    Uses real Tunarr field names: season={index: N} and episodeNumber.
    """
    prog = p.get("program", {})
    season_obj = prog.get("season") or {}
    season_num = season_obj.get("index") or 0 if isinstance(season_obj, dict) else 0
    return (season_num, prog.get("episodeNumber") or 0)


def _item_premiere_ms(item):
    """Premiere timestamp for timeline ordering: a movie's releaseDate; a show's
    earliest episode releaseDate (fallback: Jan 1 of its year). Unknown → +inf
    (sorts to the end, deterministically by title)."""
    progs = item.get("programs") or []
    dates = [p.get("program", {}).get("releaseDate") for p in progs]
    dates = [d for d in dates if d is not None]
    if dates:
        return min(dates)
    years = [p.get("program", {}).get("year") for p in progs]
    years = [y for y in years if y]
    if years:
        from datetime import datetime, timezone
        return datetime(min(years), 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    return float("inf")


def build_schedule(shuffle_type, resolved_items, pad_ms=0, playback=None):
    """Build a rolling random schedule.

    pad_ms > 0 rounds each program up to the next pad_ms boundary, opening a flex
    gap after it (flexPreference="end"). That gap is what the channel's attached
    filler list ("commercials") fills at playback — see the Commercials feature.
    pad_ms == 0 (default) keeps episodes back-to-back, unchanged from before.

    playback is the optional per-channel structure dict:
    - {"structure": "interleaved", "episodes_per_block": N} reweights the random
      slots so movies play chronological at weight n_shows and show slots use "next"
      at weight N — on average N episodes air between consecutive movies.
    - {"structure": "timeline"} posts a Tunarr manual lineup in strict release order:
      items sorted by premiere (movie releaseDate; show's earliest episode date, year
      fallback, unknown → +inf); shows flattened in season/episode order at their
      premiere position. Commercials padding is a no-op in v1 (manual lineups
      don't auto-pad).
    - None (default) = unchanged legacy behavior (all weights 1).
    """
    all_programs = [p for item in resolved_items for p in item["programs"]]
    if not all_programs:
        return None

    if (playback or {}).get("structure") == "timeline":
        # Strict release order as ONE looping manual lineup: each item at its
        # premiere position; a show's full run plays there in season/episode order.
        # Manual lineups don't auto-pad, so commercials padding is ignored here (v1).
        lineup = []
        for item in sorted(resolved_items,
                           key=lambda it: (_item_premiere_ms(it),
                                           (it.get("title") or "").lower())):
            programs = item["programs"]
            if item["type"] == "TV":
                programs = sorted(programs, key=_episode_sort_key)
            for p in programs:
                lineup.append({"type": "content", "id": p["id"],
                               "duration": int(p.get("program", {}).get("duration") or 0) or 1})
        return {"type": "manual", "lineup": lineup, "append": False}

    is_ordered = shuffle_type == "ordered"
    is_block = shuffle_type == "block"

    slots = []
    seen_show_ids = set()
    has_movies = False

    for item in resolved_items:
        if item["type"] == "TV":
            show_id = item.get("showId")
            if show_id and show_id not in seen_show_ids:
                seen_show_ids.add(show_id)
                slots.append({
                    "type": "show",
                    "id": str(uuid.uuid4()),
                    "cooldownMs": 0,
                    "weight": 1,
                    "order": "next" if (is_ordered or is_block) else "shuffle",
                    "showId": show_id,
                })
        else:
            has_movies = True

    if has_movies:
        slots.append({
            "type": "movie",
            "id": str(uuid.uuid4()),
            "cooldownMs": 0,
            "weight": 1,
            "order": "chronological" if is_ordered else "shuffle",
        })

    structure = (playback or {}).get("structure")
    if structure == "interleaved":
        episodes_per_block = max(1, int((playback or {}).get("episodes_per_block") or 4))
        show_slots = [s for s in slots if s["type"] == "show"]
        for s in show_slots:
            s["order"] = "next"
            s["weight"] = episodes_per_block
        for s in slots:
            if s["type"] == "movie":
                s["order"] = "chronological"
                s["weight"] = max(1, len(show_slots))

    return {
        "type": "random",
        "programs": [p["id"] for p in all_programs],
        "schedule": {
            "type": "random",
            "flexPreference": "end",
            "maxDays": 30,
            "padMs": pad_ms,
            "padStyle": "episode",
            "randomDistribution": "uniform",
            "slots": slots,
        },
    }


def set_programming(tunarr_url, channel_id, schedule_payload):
    return api(tunarr_url, "POST", f"/api/channels/{channel_id}/programming", body=schedule_payload, timeout=120)


# ── Surgical diff deploy (Add/Edit mode) ───────────────────────────────────────

def classify_channels(
    desired: list[dict],
    deployed: list[dict],
    prior_managed: set[str] | None = None,
) -> dict:
    """Classify channels for a surgical diff deploy (Add/Edit mode).

    Pure function — no Tunarr or file I/O; fully unit-testable.

    Parameters
    ----------
    desired       : list of channel dicts from channels.draft.json (the planner's output).
    deployed      : list of channel dicts from channels.json (the currently-deployed managed set).
    prior_managed : set of lowercased channel names the planner built/deployed last time
                    (from ``planner_state.json["managed_names"]``).  When ``None`` or empty,
                    defaults to an empty set — no channels are deleted (safe, conservative
                    bootstrapping behaviour for installs that have no planner history yet).

    Returns a dict with five keys:
        create    — channels in desired but NOT in deployed (by name, case-insensitive).
        delete    — channels in deployed, absent from desired, AND whose name is in
                    ``prior_managed`` (i.e. the planner previously owned them and the user
                    intentionally removed them).
        update    — channels in both desired and deployed whose content, shuffle, or live
                    flag differs.  A ``live`` channel whose content changed always lands here
                    (update-in-place) — its Tunarr id is preserved.
        unchanged — channels present in both sets where nothing changed.
        foreign   — channels in deployed, absent from desired, whose name is NOT in
                    ``prior_managed``.  These are hand-authored channels (created outside
                    the planner) and are NEVER touched — not deleted, not created, not
                    updated.

    Identity = channel name (case-insensitive, stripped).  Names are deterministic
    in the Planner, so they are a stable key.

    Invariant enforcement (HARD — never relaxed):
    1. The ``delete`` bucket only ever contains planner-managed channels (prior_managed).
       Hand-authored / foreign channels land in ``foreign`` and are never auto-deleted.
    2. A ``live`` channel in the ``update`` bucket is always patched in place (never
       delete-and-recreated) — the caller's update loop preserves the Tunarr id and
       Plex DVR mapping.  A planner-managed live channel that the user explicitly removes
       (absent from desired, name in prior_managed) IS eligible for deletion — removing a
       channel from the planner is an intentional act and does not violate invariant 2
       (delete-RECREATE is what's forbidden; a plain delete is fine).
    3. Orphan channels (those in Tunarr but absent from channels.json) are not part of
       either input list and therefore cannot appear in any output bucket.
    """
    if prior_managed is None:
        prior_managed = set()

    def _key(ch):
        return (ch.get("name") or "").strip().lower()

    def _content_sig(ch):
        """Canonical content + shuffle + playback signature for change-detection."""
        return (
            json.dumps(ch.get("content", []), sort_keys=True),
            ch.get("shuffle", ""),
            bool(ch.get("live")),
            json.dumps(ch.get("playback") or {}, sort_keys=True),
        )

    desired_by_name: dict[str, dict] = {}
    for ch in desired:
        k = _key(ch)
        if k:
            desired_by_name[k] = ch

    deployed_by_name: dict[str, dict] = {}
    for ch in deployed:
        k = _key(ch)
        if k:
            deployed_by_name[k] = ch

    result: dict[str, list] = {"create": [], "delete": [], "update": [], "unchanged": [], "foreign": []}

    # desired channels: new or changed vs deployed
    for name, d_ch in desired_by_name.items():
        if name not in deployed_by_name:
            result["create"].append(d_ch)
        else:
            dep_ch = deployed_by_name[name]
            if _content_sig(d_ch) != _content_sig(dep_ch):
                result["update"].append({"desired": d_ch, "deployed": dep_ch})
            else:
                result["unchanged"].append(d_ch)

    # deployed channels not in desired: delete only if planner-managed; otherwise foreign
    for name, dep_ch in deployed_by_name.items():
        if name not in desired_by_name:
            if name in prior_managed:
                # Planner previously owned this channel and the user removed it — delete it.
                result["delete"].append(dep_ch)
            else:
                # Hand-authored or foreign — never auto-delete.
                result["foreign"].append(dep_ch)

    return result


def merge_deployed_numbers(desired: list[dict], deployed: list[dict]) -> list[dict]:
    """Return ``desired`` with each EXISTING channel's number set to its deployed number.

    Match is by name (case-insensitive, stripped).  A channel present in ``deployed``
    keeps the deployed number; a channel absent from ``deployed`` (a genuinely new
    channel) keeps its own number.

    Why: in Add/Edit mode ``compose`` renumbers the whole selection from ``highest+1``,
    so an already-deployed channel carries a throwaway high number in the draft.  The
    surgical deploy updates it IN PLACE (preserving the real Tunarr channel), so the
    written record must mirror the deployed number, not the draft number — otherwise
    channels.json desyncs from Tunarr.  New channels keep their draft number (which is
    above the existing set, so it can't collide).
    """
    dep_by_name = {(c.get("name") or "").strip().lower(): c.get("number") for c in deployed}
    out: list[dict] = []
    for ch in desired:
        dep_num = dep_by_name.get((ch.get("name") or "").strip().lower())
        out.append({**ch, "number": dep_num} if dep_num is not None else ch)
    return out


# ── In-place channel updates (live recipes) ────────────────────────────────────

def find_channel_by_number(tunarr_url, number):
    """Return the live Tunarr channel dict (incl. id) for a channel number, or None."""
    for ch in api(tunarr_url, "GET", "/api/channels") or []:
        if ch.get("number") == number:
            return ch
    return None


def read_channel_programming(tunarr_url, channel_id):
    """Return the set of program IDs currently scheduled on a channel, or None on error.

    Uses GET /api/channels/{id}/programming. The `programs` field is a dict keyed by
    program ID (the same id-space as build_library_index's p["id"]), so its keys are
    the current content set. Falls back to distinct content lineup ids if absent.
    This set is the "current" side of the scheduler's change-detection diff.
    """
    pr = api(tunarr_url, "GET", f"/api/channels/{channel_id}/programming")
    if not pr:
        return None
    programs = pr.get("programs")
    if isinstance(programs, dict):
        return set(programs.keys())
    return {i["id"] for i in pr.get("lineup", []) if i.get("type") == "content" and i.get("id")}


def update_channel_in_place(tunarr_url, number, shuffle, resolved, pad_ms=0, expected_name=None, playback=None):
    """Patch an existing channel's programming in place — never delete/recreate.

    Looks the channel up by number (preserving its Tunarr id and Plex DVR mapping),
    rebuilds the schedule from `resolved`, and POSTs it. This is the primitive the
    live-channel scheduler calls after detecting a content change. Raises
    ChannelEngineError if the channel is missing or no schedule can be built.

    pad_ms preserves a commercials channel's gap on live updates (the attached filler
    list survives — only programming is replaced — but the pad must be re-applied or the
    gap, and thus the commercials, would vanish after a cycle). Similarly, playback
    (the per-channel structure dict) must be re-applied on live updates for the same
    reason as pad_ms — omitting it would silently revert an interleaved or timeline
    channel to the default shuffle behavior after each cycle.

    expected_name guards against the by-number scramble: if given, the Tunarr channel
    found at `number` must carry that name (case/space-insensitive) or we refuse to
    patch. channels.json can drift out of sync with Tunarr's numbering (two Programmarr
    instances writing one Tunarr; an orphan channel shifting numbers), in which case
    blind by-number patching overwrites the wrong channel. Mismatch ⇒ skip, never scramble.
    """
    ch = find_channel_by_number(tunarr_url, number)
    if not ch:
        raise ChannelEngineError(f"Channel #{number} not found in Tunarr")
    if expected_name is not None:
        actual = (ch.get("name") or "").strip().lower()
        if actual != expected_name.strip().lower():
            raise ChannelEngineError(
                f"Channel #{number} name mismatch: Tunarr has '{ch.get('name')}', "
                f"expected '{expected_name}' — refusing to overwrite "
                f"(channels.json out of sync with Tunarr)")
    schedule = build_schedule(SHUFFLE_MAP.get(shuffle, "shuffle"), resolved, pad_ms=pad_ms, playback=playback)
    if not schedule:
        raise ChannelEngineError(f"Channel #{number}: no schedule could be built (no content resolved)")
    return set_programming(tunarr_url, ch["id"], schedule)
