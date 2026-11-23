"""icon_engine.py — pure, importable channel-icon resolution logic.

Like channel_engine.py: no config.json, no argv, no sys.exit — safe to
import into the long-lived FastAPI process AND from the fetch_images.py CLI.
Must stay in the Dockerfile COPY line.

Three layers (built across plan Tasks 2-4):
  1. Verified TMDB searches — a result is accepted only if its name equals
     the query after normalize_title(). Never results[0] on faith.
  2. Icon policy (icon_attempts) — which channels may consult TMDB at all;
     everything else gets a generated badge (badge_renderer.py).
  3. Tunarr helpers — multipart image upload + set/clear a channel's icon.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

import channel_engine

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"

_ARTICLES = re.compile(r"^(the|a|an)\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_title(s):
    """Lowercase, strip punctuation, drop a leading article, collapse spaces."""
    s = (s or "").strip().lower()
    s = _PUNCT.sub("", s)
    s = _ARTICLES.sub("", s)
    return _WS.sub(" ", s).strip()


# ── HTTP ───────────────────────────────────────────────────────────────────────

_SECRET_PARAM = re.compile(r"(api_key|X-Plex-Token)=[^&\s]+", re.IGNORECASE)


def redact_url(url):
    """Hide secrets carried in a query string before the URL reaches a log line."""
    return _SECRET_PARAM.sub(r"\1=***", url)


def http_get(url, timeout=15, tunarr=False):
    """GET JSON. Tunarr credentials are attached ONLY when tunarr=True: this is
    also used for TMDB, and the Tunarr login must never go to a third party."""
    headers = channel_engine.tunarr_headers() if tunarr else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  ! HTTP {e.code}: {redact_url(url)}")
        return None
    except Exception as e:
        print(f"  ! Error fetching {redact_url(url)}: {e}")
        return None


# ── Verified TMDB searches ─────────────────────────────────────────────────────

def _tmdb_search(search_kind, query, api_key):
    q = urllib.parse.urlencode({"query": query, "api_key": api_key})
    data = http_get(f"https://api.themoviedb.org/3/search/{search_kind}?{q}")
    return (data or {}).get("results") or []


def _first_verified(results, query, *name_fields):
    """First result (top 5) whose normalized name equals the normalized query."""
    want = normalize_title(query)
    if not want:
        return None
    for r in results[:5]:
        for field in name_fields:
            if normalize_title(r.get(field)) == want:
                return r["id"]
    return None


def search_tv_verified(title, api_key):
    return _first_verified(_tmdb_search("tv", title, api_key), title,
                           "name", "original_name")


def search_movie_verified(title, api_key):
    return _first_verified(_tmdb_search("movie", title, api_key), title,
                           "title", "original_title")


def search_company_verified(name, api_key):
    return _first_verified(_tmdb_search("company", name, api_key), name, "name")


# ── Logo selection ─────────────────────────────────────────────────────────────

def best_logo_path(images, prefer_lang="en"):
    """Best logo file_path from a TMDB images response, or None.
    Prefers prefer_lang, then no-language, then highest vote overall.
    SVG logos are excluded outright — Plex renders guide icons from raster
    files only, and a broken icon is worse than the badge fallback."""
    logos = [l for l in ((images or {}).get("logos") or [])
             if not (l.get("file_path") or "").lower().endswith(".svg")]
    if not logos:
        return None
    for lang in (prefer_lang, None, ""):
        candidates = [l for l in logos if l.get("iso_639_1") == lang]
        if candidates:
            return max(candidates, key=lambda l: l.get("vote_average", 0))["file_path"]
    return max(logos, key=lambda l: l.get("vote_average", 0))["file_path"]


def tv_logo_url(tv_id, api_key):
    q = urllib.parse.urlencode({"api_key": api_key, "include_image_language": "en,null"})
    path = best_logo_path(http_get(f"https://api.themoviedb.org/3/tv/{tv_id}/images?{q}"))
    return TMDB_IMAGE_BASE + path if path else None


def movie_logo_url(movie_id, api_key):
    q = urllib.parse.urlencode({"api_key": api_key, "include_image_language": "en,null"})
    path = best_logo_path(http_get(f"https://api.themoviedb.org/3/movie/{movie_id}/images?{q}"))
    return TMDB_IMAGE_BASE + path if path else None


def company_logo_url(company_id, api_key):
    q = urllib.parse.urlencode({"api_key": api_key})
    path = best_logo_path(http_get(f"https://api.themoviedb.org/3/company/{company_id}/images?{q}"))
    return TMDB_IMAGE_BASE + path if path else None


# ── Icon policy ────────────────────────────────────────────────────────────────

# Kinds for which NO TMDB lookup is trustworthy — always a generated badge.
BADGE_ONLY_KINDS = {
    "genre", "genre_decade", "blend", "tv_genre", "tv_movie_mix", "theme",
    "country", "mood", "style", "programming_block", "director", "actor",
}

_FRANCHISE_SUFFIXES = re.compile(
    r"\s+(Collection|Series|Franchise|Universe|Saga|Trilogy|Tetralogy|"
    r"Anthology|Films?|Movies?|Pictures?)\s*$",
    re.IGNORECASE,
)


def icon_attempts(ch_def, kind):
    """Ordered (search_kind, query) attempts for a verified TMDB logo.

    Empty list => badge-only channel. search_kind is "tv" | "movie" | "company".
    Solo-title channels always try their one title (the channel IS that title),
    regardless of kind.
    """
    name = (ch_def.get("name") or "").strip()
    content = ch_def.get("content") or []
    strings = [c for c in content if isinstance(c, str)]
    solo = len(content) == 1 and len(strings) == 1

    if solo:
        return [("tv", strings[0]), ("movie", strings[0])]
    if len(content) == 1 and isinstance(content[0], dict):
        # Typed title ref ({"movie": t} / {"show": t}): search only its own kind.
        if "movie" in content[0]:
            return [("movie", content[0]["movie"])]
        if "show" in content[0]:
            return [("tv", content[0]["show"])]
    if kind in BADGE_ONLY_KINDS:
        return []
    if kind == "marathon":
        title = strings[0] if strings else name
        return [("tv", title), ("movie", title)]
    if kind == "franchise":
        cleaned = _FRANCHISE_SUFFIXES.sub("", name).strip() or name
        return [("tv", cleaned), ("movie", cleaned)]
    if kind in ("network", "studio"):
        return [("company", name)]
    return []  # unknown kind, multi-title: the old false-positive class — badge.


def resolve_tmdb_logo(attempts, api_key):
    """Run verified-search attempts in order; return the first logo URL or None."""
    searchers = {
        "tv": (search_tv_verified, tv_logo_url),
        "movie": (search_movie_verified, movie_logo_url),
        "company": (search_company_verified, company_logo_url),
    }
    for search_kind, query in attempts:
        search, logo = searchers[search_kind]
        found_id = search(query, api_key)
        if found_id:
            url = logo(found_id, api_key)
            if url:
                return url
    return None


# ── Planner spec hints ─────────────────────────────────────────────────────────

def load_spec_hints(path):
    """planner_state.json 'selected' specs keyed by lowercased channel name.
    Returns {} on any failure — hints are best-effort."""
    try:
        with open(path, encoding="utf-8") as f:
            ps = json.load(f)
        return {
            v["name"].strip().lower(): v
            for v in (ps.get("selected") or {}).values()
            if isinstance(v, dict) and v.get("name")
        }
    except (OSError, ValueError, AttributeError):
        return {}


def spec_genre(spec):
    """Best genre hint from a CandidateSpec dict (genre, else first of genres)."""
    return spec.get("genre") or (spec.get("genres") or [None])[0]


# ── Tunarr helpers ────────────────────────────────────────────────────────────

def _tunarr_put(tunarr_url, path, body):
    url = tunarr_url + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers=channel_engine.tunarr_headers({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        print(f"  ! HTTP {e.code} [PUT {path}]: {raw[:200]}")
        return None
    except Exception as e:
        print(f"  ! Error [PUT {path}]: {e}")
        return None


def get_full_channel(tunarr_url, channel_id):
    """Full Tunarr channel object (the summary from /api/channels lacks fields
    the PUT round-trip needs)."""
    return http_get(f"{tunarr_url}/api/channels/{channel_id}", timeout=30, tunarr=True)


def upload_image_to_tunarr(tunarr_url, png_bytes, filename):
    """POST multipart to Tunarr /api/upload/image. Returns the Tunarr-served
    fileUrl. Raises on HTTP failure (callers decide how to report)."""
    boundary = uuid.uuid4().hex
    body = (
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
         f"Content-Type: image/png\r\n\r\n").encode()
        + png_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        tunarr_url + "/api/upload/image", data=body, method="POST",
        headers=channel_engine.tunarr_headers(
            {"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["fileUrl"]


def set_tunarr_channel_icon(tunarr_url, channel, icon_url):
    """PUT the channel back with icon.path set. Returns True on success."""
    updated = dict(channel)
    updated["icon"] = dict(channel.get("icon") or {})
    updated["icon"]["path"] = icon_url
    updated["icon"]["useDefaultIconFallback"] = False
    return _tunarr_put(tunarr_url, f"/api/channels/{channel['id']}", updated) is not None


def clear_tunarr_channel_icon(tunarr_url, channel):
    """Reset a channel to the Tunarr default icon. Returns True on success."""
    updated = dict(channel)
    updated["icon"] = dict(channel.get("icon") or {})
    updated["icon"]["path"] = ""
    updated["icon"]["useDefaultIconFallback"] = True
    return _tunarr_put(tunarr_url, f"/api/channels/{channel['id']}", updated) is not None
