"""plex_world.py — a fake Plex server + Plex-backed Tunarr for the snapshot tests.

The Plex snapshot tests (test_plex_snapshots*.py) run the real CLI scripts and web
routes end to end against this world and compare what they produce with saved
files. The point is the Plex rule on issue #36: every Jellyfin/Emby PR must leave
a Plex user's export and deploys byte-for-byte the same, and these snapshots are
how a PR shows that instead of just saying it.

Two choices matter for that:

* **The fake sits at the HTTP boundary** (`urllib.request.urlopen`), not on any
  Programmarr function. Later PRs move Plex code behind a `MediaServer`
  interface; a fake on the old function names would silently stop being called.
  Every script and route reaches Plex and Tunarr through urlopen, so they can't
  route around this one.
* **It is deterministic.** It is built from the committed `demo/plex_library.csv`
  plus a handful of hand-placed edge cases (see EXTRA_* below), with every id,
  tag and episode derived from row order — no RNG, no clock. Regenerating the
  demo dataset therefore changes the snapshots; rerun with --update-snapshots.

`World(tunarr_auth=(user, password))` makes the fake Tunarr demand that basic
auth on every request (401 otherwise), the way a locked-down Tunarr does, so a
refactor that stops sending `channel_engine.tunarr_headers()` fails here.

`World(second_server=True)` adds a second Plex server (and its Tunarr source)
whose Plex ids overlap the first's and which repeats some of its titles — the
setup where mixing up servers would go wrong.

Unknown routes raise, so a script that starts calling a new endpoint fails
loudly here rather than getting a quiet empty answer.
"""

import base64
import csv
import io
import json
import urllib.error
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO_CSV = ROOT / "demo" / "plex_library.csv"

PLEX_URL = "http://plex.test:32400"
PLEX_TOKEN = "snapshot-plex-token"
PLEX2_URL = "http://plex2.test:32400"
PLEX2_TOKEN = "snapshot-plex2-token"
TUNARR_URL = "http://tunarr.test:8000"

PLEX_SOURCE_ID = "src-plex"
PLEX2_SOURCE_ID = "src-plex-2"
MOVIE_SECTION = "1"
SHOW_SECTION = "2"
MUSIC_SECTION = "3"          # must be ignored by everything
DISABLED_LIB = "lib-disabled"

# Max episodes the fake Tunarr holds per show. Plex still reports the real
# leafCount (so the export's Episodes column is the demo's), but keeping the
# Tunarr side small keeps the deploy snapshot reviewable.
EPISODES_PER_SHOW = 4

# Rotations used to give the demo rows tags the demo CSV doesn't carry. Empty
# strings are deliberate: some titles have no tag, as in a real library.
COUNTRIES = ["United States of America", "United Kingdom", "", "France",
             "Japan|United States of America"]
MOODS = ["Witty", "", "Tense", "Bleak|Brooding", ""]
STYLES = ["", "Buddy Film", "", "Film Noir", "Ensemble Film"]
NETWORKS = ["NBC", "CBS", "FOX", "HBO", "ABC"]

# ── Hand-placed edge cases ─────────────────────────────────────────────────────
# Each exists to pin one behavior a Plex user relies on.

# In Plex but not in Tunarr: the export's cross-reference must skip these.
EXTRA_PLEX_ONLY_MOVIES = [{"title": "Unsynced Movie", "year": 2001, "genres": ["Drama"]}]
EXTRA_PLEX_ONLY_SHOWS = [{"title": "Unsynced Show", "seasons": 1, "episodes": 8,
                          "genres": ["Drama"]}]
# In Tunarr's Plex source but not in the Plex sections we read: the export's
# "extra rows from Tunarr" path picks it up.
EXTRA_TUNARR_ONLY_MOVIES = [{"title": "Tunarr Only Movie", "year": 1999, "genres": ["Comedy"]}]
# A movie that shares its title with a demo show (issue #39 / #42): typed refs
# must pick the right one, and a plain title resolves to whichever has more
# programs. The demo already has the Fargo *show*; this adds the 1996 film.
EXTRA_COLLISION = {"title": "Fargo", "movie_year": 1996}
# A Plex collection, resolved to members by Plex id at deploy time. It has a poster.
COLLECTION = {"title": "Snapshot Action Classics", "ratingKey": "9001",
              "members": ["Die Hard", "Aliens", "Predator", "Speed", "Fargo"],
              "summary": "Explosions, mostly.", "thumb": "/library/metadata/9001/thumb/1"}
# A collection with members Tunarr doesn't have. No poster.
COLLECTION_PARTIAL = {"title": "Snapshot Partly Synced", "ratingKey": "9002",
                      "members": ["Unsynced Movie", "The Matrix"]}

# The second server. Its Plex ids restart at 1001, so they collide with the first
# server's (Plex ids are only unique per server). It repeats "Die Hard" and has a
# BIGGER copy of "Cheers" than the first server, plus titles of its own.
SECOND_SERVER = {
    "movies": [("Die Hard", 1988, ["Action"]), ("Second Server Movie", 2005, ["Drama"])],
    "shows": [("Cheers", 11, 275, ["Comedy"]), ("Second Server Show", 2, 20, ["Drama"])],
    "episodes_per_show": 6,
}


def _split(v):
    return [x for x in (v or "").split("|") if x]


def _tags(values):
    return [{"tag": v} for v in values]


def _tunarr_tags(values):
    return [{"name": v} for v in values]


class PlexServer:
    """One Plex server's libraries, plus the programs its Tunarr source holds."""

    def __init__(self, name, url, token, source_id, id_prefix, episodes_per_show):
        self.name, self.url, self.token = name, url, token
        self.source_id, self.id_prefix = source_id, id_prefix
        lib = "lib" if id_prefix == "tunarr" else f"{id_prefix}-lib"   # primary keeps plain ids
        self.movie_lib, self.show_lib = f"{lib}-movies", f"{lib}-shows"
        self.episodes_per_show = episodes_per_show
        self.movies, self.shows, self.collections = [], [], []
        self.tunarr_movies, self.tunarr_episodes = [], []
        self._rk = 1000

    def next_rk(self):
        self._rk += 1
        return str(self._rk)

    def add_movie(self, title, year, rating, genres, directors, studio, actors, i,
                  in_plex=True, in_tunarr=True):
        rk = self.next_rk()
        # Two extra billed cast past the CSV's three, so the export's top-3 cut is
        # actually exercised (a regression to "all cast" would show in the diff).
        cast = actors + ([f"Supporting Actor {i % 7}", f"Bit Part {i % 5}"] if actors else [])
        m = {
            "ratingKey": rk, "key": f"/library/metadata/{rk}", "type": "movie",
            "guid": f"plex://movie/{int(rk):024x}",
            "title": title, "year": year, "contentRating": rating, "studio": studio,
            "duration": 5_400_000 + (i % 9) * 300_000,
            "originallyAvailableAt": f"{year}-06-{(i % 27) + 1:02d}" if year else None,
            "Genre": _tags(genres), "Director": _tags(directors), "Role": _tags(cast),
            "Country": _tags(_split(COUNTRIES[i % len(COUNTRIES)])),
            "Mood": _tags(_split(MOODS[i % len(MOODS)])),
            "Style": _tags(_split(STYLES[i % len(STYLES)])),
            "Guid": [{"id": f"tmdb://{int(rk) * 7}"}, {"id": f"imdb://tt{int(rk):07d}"}],
        }
        m = {k: v for k, v in m.items() if v is not None}
        if in_plex:
            self.movies.append(m)
        if in_tunarr:
            self.tunarr_movies.append(self._tunarr_movie(m))
        return m

    def add_show(self, title, rating, genres, seasons, episodes, i, in_tunarr=True):
        rk = self.next_rk()
        s = {
            "ratingKey": rk, "key": f"/library/metadata/{rk}/children", "type": "show",
            "guid": f"plex://show/{int(rk):024x}",
            "title": title, "year": 1985 + (i * 3) % 35, "contentRating": rating,
            "studio": NETWORKS[i % len(NETWORKS)],
            "childCount": seasons, "leafCount": episodes,
            "Genre": _tags(genres),
            "Role": _tags([f"Lead {i}", f"Co-Star {i}", f"Regular {i}", f"Guest {i}"]),
            "Country": _tags(_split(COUNTRIES[i % len(COUNTRIES)])),
            "Guid": [{"id": f"tmdb://{int(rk) * 11}"}, {"id": f"tvdb://{int(rk) * 13}"}],
        }
        self.shows.append(s)
        if in_tunarr:
            self.tunarr_episodes.extend(self._tunarr_episodes(s))
        return s

    def add_collection(self, spec, section):
        by_title = {}
        for item in self.movies + self.shows:
            by_title.setdefault(item["title"], []).append(item)
        col = {"section": section, "ratingKey": spec["ratingKey"], "title": spec["title"],
               "members": [item for t in spec["members"] for item in by_title.get(t, [])]}
        for k in ("summary", "thumb"):
            if spec.get(k):
                col[k] = spec[k]
        self.collections.append(col)

    def _identifiers(self, plex_item, tmdb_mult):
        rk = plex_item["ratingKey"]
        return [{"type": "plex", "id": rk},
                {"type": "tmdb", "id": str(int(rk) * tmdb_mult)},
                {"type": "imdb", "id": f"tt{int(rk):07d}"}]

    def _tunarr_movie(self, m):
        rk = m["ratingKey"]
        uid = f"{self.id_prefix}-movie-{rk}"
        prog = {
            "uuid": uid, "type": "movie", "subtype": "movie",
            "title": m["title"], "year": m.get("year"), "rating": m.get("contentRating", ""),
            "duration": m.get("duration", 5_400_000),
            "releaseDate": f"{m['year']}-06-01T00:00:00.000Z" if m.get("year") else None,
            "state": "ok", "mediaSourceId": self.source_id, "externalKey": rk,
            "genres": _tunarr_tags(t["tag"] for t in m.get("Genre", [])),
            "directors": _tunarr_tags(t["tag"] for t in m.get("Director", [])),
            "studios": _tunarr_tags([m["studio"]] if m.get("studio") else []),
            "actors": [{"name": t["tag"]} for t in m.get("Role", [])],
            "identifiers": self._identifiers(m, 7),
        }
        return {"type": "content", "id": uid, "program": prog}

    def _tunarr_episodes(self, s):
        rk = s["ratingKey"]
        show = {
            "uuid": f"{self.id_prefix}-show-{rk}", "title": s["title"], "year": s["year"],
            "genres": _tunarr_tags(t["tag"] for t in s["Genre"]),
            "studios": _tunarr_tags([s["studio"]]),
            "actors": [{"name": t["tag"]} for t in s["Role"]],
            "identifiers": self._identifiers(s, 11),
            "mediaSourceId": self.source_id,
        }
        n = min(self.episodes_per_show, s["leafCount"])
        n_seasons = min(s["childCount"], 2)
        out = []
        for e in range(n):
            season = (e % n_seasons) + 1
            ep_index = e // n_seasons + 1
            uid = f"{self.id_prefix}-ep-{rk}-{e + 1}"
            out.append({"type": "content", "id": uid, "program": {
                "uuid": uid, "type": "episode", "subtype": "episode",
                "title": f"{s['title']} S{season}E{ep_index}",
                "duration": 1_320_000, "state": "ok", "mediaSourceId": self.source_id,
                "releaseDate": f"{s['year'] + season - 1}-09-{ep_index:02d}T00:00:00.000Z",
                "rating": s["contentRating"],
                "season": {"index": season}, "episodeNumber": ep_index,
                "seasonNumber": season,
                "show": show, "showId": show["uuid"],
                "identifiers": [{"type": "plex", "id": f"{rk}{e + 1:03d}"}],
            }})
        return out

    def media_source(self, extra_libs=()):
        return {
            "id": self.source_id, "name": self.name, "type": "plex", "uri": self.url,
            "libraries": [
                {"id": self.movie_lib, "name": "Movies", "mediaType": "movies", "enabled": True},
                {"id": self.show_lib, "name": "TV Shows", "mediaType": "shows", "enabled": True},
                *extra_libs,
            ],
        }


def _primary_server():
    """The demo library, plus the hand-placed edge cases."""
    srv = PlexServer("Plex", PLEX_URL, PLEX_TOKEN, PLEX_SOURCE_ID, "tunarr", EPISODES_PER_SHOW)
    with open(DEMO_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for i, r in enumerate(r for r in rows if r["Type"] == "Movie"):
        srv.add_movie(r["Title"], int(r["Year"]) if r["Year"] else None, r["Rating"],
                      _split(r["Genres"]), _split(r["Director"]), r["Studio"],
                      _split(r["Actors"]), i)
    for i, r in enumerate(r for r in rows if r["Type"] == "TV"):
        srv.add_show(r["Title"], r["Rating"], _split(r["Genres"]),
                     int(r["Seasons"] or 1), int(r["Episodes"] or 1), i)

    c = EXTRA_COLLISION
    assert any(s["title"] == c["title"] for s in srv.shows), "demo lost its Fargo show"
    srv.add_movie(c["title"], c["movie_year"], "R", ["Crime", "Drama"], ["Joel Coen"],
                  "PolyGram", ["Frances McDormand", "William H. Macy"], 1)

    for e in EXTRA_PLEX_ONLY_MOVIES:
        srv.add_movie(e["title"], e["year"], "PG", e["genres"], [], "", [], 0, in_tunarr=False)
    for e in EXTRA_PLEX_ONLY_SHOWS:
        srv.add_show(e["title"], "TV-PG", e["genres"], e["seasons"], e["episodes"], 0,
                     in_tunarr=False)
    for e in EXTRA_TUNARR_ONLY_MOVIES:
        srv.add_movie(e["title"], e["year"], "PG-13", e["genres"], ["Some Director"], "Indie",
                      ["A", "B", "C", "D"], 2, in_plex=False)

    srv.add_collection(COLLECTION, MOVIE_SECTION)
    srv.add_collection(COLLECTION_PARTIAL, MOVIE_SECTION)
    return srv


def _second_server():
    spec = SECOND_SERVER
    srv = PlexServer("Plex 2", PLEX2_URL, PLEX2_TOKEN, PLEX2_SOURCE_ID, "tunarr2",
                     spec["episodes_per_show"])
    for i, (title, year, genres) in enumerate(spec["movies"]):
        srv.add_movie(title, year, "R", genres, ["Second Director"], "Second Studio",
                      ["Second Lead", "Second Co-Star"], i + 3)
    for i, (title, seasons, episodes, genres) in enumerate(spec["shows"]):
        srv.add_show(title, "TV-PG", genres, seasons, episodes, i + 2)
    return srv


class World:
    """Plex + Tunarr state. Build once per test; it records every request."""

    def __init__(self, existing_channels=None, dvr_mapped=None, second_server=False,
                 programming=None, tunarr_auth=None):
        self.requests = []          # every request, in order: {method, url, body}
        self.created = {}           # channel id -> body["channel"], from POST /api/channels
        # channel id -> programming payload: last POSTed, or seeded for existing channels
        self.programming = dict(programming or {})
        self.dvr_mapped = list(dvr_mapped if dvr_mapped is not None else ["10", "11"])
        self.existing_channels = list(existing_channels or [])
        self.servers = [_primary_server()] + ([_second_server()] if second_server else [])
        self.tunarr_auth = None
        if tunarr_auth:
            raw = ":".join(tunarr_auth).encode()
            self.tunarr_auth = "Basic " + base64.b64encode(raw).decode()

    # Back-compat views of the primary server, for tests that look items up.
    @property
    def plex_movies(self):
        return self.servers[0].movies

    @property
    def plex_shows(self):
        return self.servers[0].shows

    # ── Tunarr views ───────────────────────────────────────────────────────────

    def media_sources(self):
        disabled = {"id": DISABLED_LIB, "name": "Home Videos", "mediaType": "movies",
                    "enabled": False}
        return [srv.media_source(extra_libs=[disabled] if i == 0 else [])
                for i, srv in enumerate(self.servers)]

    def all_channels(self):
        made = [{"id": cid, "number": ch["number"], "name": ch["name"]}
                for cid, ch in self.created.items()]
        return sorted(self.existing_channels + made, key=lambda c: c["number"])

    def xmltv(self):
        chans = "".join(f'<channel id="C{c["number"]}.97.tunarr.com"><display-name>{c["name"]}'
                        f"</display-name></channel>" for c in self.all_channels())
        return f'<?xml version="1.0" encoding="UTF-8"?><tv>{chans}</tv>'

    # ── What a run produced ────────────────────────────────────────────────────

    def writes(self):
        """Requests that change Plex or Tunarr — the part of a run that is output."""
        return [r for r in self.requests if r["method"] in ("POST", "PUT", "DELETE", "PATCH")]

    def plex_requests(self):
        """How many times each Plex request was made, order ignored.

        Pinned so a refactor can't quietly add Plex calls (the maintainer's condition
        on #36: no new required Plex calls) or drop a section it used to read. A
        deliberate change to Plex calls shows up here and gets called out in the PR.
        """
        origins = {srv.url for srv in self.servers}
        n = Counter(f"{r['method']} {r['url']}" for r in self.requests
                    if any(r["url"].startswith(o) for o in origins))
        return dict(sorted(n.items()))

    def outputs(self):
        return {"plex_requests": self.plex_requests(), "writes": self.writes()}

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def urlopen(self, req, timeout=None, **_):
        if isinstance(req, str):
            url, method, data, headers = req, "GET", None, {}
        else:
            url, data, headers = req.full_url, req.data, dict(req.header_items())
            method = req.get_method()
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        origin = f"{parts.scheme}://{parts.netloc}"
        body = self._decode_body(data, headers)
        self.requests.append({"method": method, "url": self._public_url(url), "body": body})

        for srv in self.servers:
            if origin == srv.url:
                if query.get("X-Plex-Token") != [srv.token]:
                    raise _http_error(url, 401)
                return self._plex(srv, method, parts.path, body, url)
        if origin == TUNARR_URL:
            sent = {k.lower(): v for k, v in headers.items()}.get("authorization")
            if self.tunarr_auth and sent != self.tunarr_auth:
                raise _http_error(url, 401)
            return self._tunarr(method, parts.path, body)
        raise AssertionError(f"request to an unknown host: {method} {url}")

    @staticmethod
    def _public_url(url):
        """The URL as a snapshot should show it: token stripped, query sorted."""
        parts = urllib.parse.urlsplit(url)
        q = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "X-Plex-Token")
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))

    @staticmethod
    def _decode_body(data, headers):
        if data is None:
            return None
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        text = data.decode() if isinstance(data, bytes) else data
        if "x-www-form-urlencoded" in ctype:
            return dict(sorted(urllib.parse.parse_qsl(text)))
        try:
            return json.loads(text)
        except ValueError:
            return text

    def _plex(self, srv, method, path, body, url):
        mc = lambda **kw: _json({"MediaContainer": kw})
        if method == "GET" and path == "/library/sections":
            return mc(Directory=[
                {"key": MOVIE_SECTION, "type": "movie", "title": "Movies"},
                {"key": SHOW_SECTION, "type": "show", "title": "TV Shows"},
                {"key": MUSIC_SECTION, "type": "artist", "title": "Music"},
            ])
        if method == "GET" and path == f"/library/sections/{MOVIE_SECTION}/all":
            return mc(Metadata=srv.movies, size=len(srv.movies))
        if method == "GET" and path == f"/library/sections/{SHOW_SECTION}/all":
            return mc(Metadata=srv.shows, size=len(srv.shows))
        if (method == "GET" and path.startswith("/library/sections/")
                and path.endswith("/collections")):
            key = path.split("/")[3]
            cols = [{"ratingKey": c["ratingKey"], "title": c["title"], "type": "collection",
                     "childCount": len(c["members"]),
                     **{k: c[k] for k in ("summary", "thumb") if k in c}}
                    for c in srv.collections if c["section"] == key]
            return mc(Metadata=cols, size=len(cols))
        if (method == "GET" and path.startswith("/library/collections/")
                and path.endswith("/children")):
            rk = path.split("/")[3]
            col = next((c for c in srv.collections if c["ratingKey"] == rk), None)
            if col is None:
                raise _http_error(url, 404)
            return mc(Metadata=col["members"], size=len(col["members"]))
        if method == "GET" and path.startswith("/library/metadata/") and path.endswith("/thumb"):
            rk = path.split("/")[3]
            if not any(c["ratingKey"] == rk and "thumb" in c for c in srv.collections):
                raise _http_error(url, 404)
            return _Response(f"PNG-poster-{rk}".encode(), content_type="image/png")
        if method == "GET" and path == "/livetv/dvrs":
            return mc(Dvr=[{"key": "dvr-1", "Device": [{
                "key": "dev-1",
                "ChannelMapping": [{"deviceIdentifier": n, "lineupIdentifier": n, "enabled": "1"}
                                   for n in self.dvr_mapped],
            }]}])
        if method == "PUT" and path == "/livetv/dvrs/dvr-1/devices/dev-1":
            for k, v in (body or {}).items():
                if k.endswith(".deviceIdentifier") and v not in self.dvr_mapped:
                    self.dvr_mapped.append(v)
            return mc(status=0)
        raise AssertionError(f"unexpected Plex request: {method} {path}")

    def _tunarr(self, method, path, body):
        if method == "GET" and path == "/api/media-sources":
            return _json(self.media_sources())
        for srv in self.servers:
            if method == "GET" and path == f"/api/media-libraries/{srv.movie_lib}/programs":
                # Paged shape (Tunarr 2026.11+), one page.
                return _json({"total": len(srv.tunarr_movies), "result": srv.tunarr_movies})
            if method == "GET" and path == f"/api/media-libraries/{srv.show_lib}/programs":
                return _json(srv.tunarr_episodes)     # bare-list shape (older Tunarr)
        if method == "GET" and path == "/api/transcode_configs":
            return _json([{"id": "tc-default", "name": "Default"}])
        if method == "GET" and path == "/api/channels":
            return _json(self.all_channels())
        if method == "GET" and path.startswith("/api/channels/") and path.endswith("/programming"):
            cid = path.split("/")[3]
            empty = {"programs": {}, "lineup": [], "totalPrograms": 0}
            return _json(self.programming.get(cid, empty))
        if method == "POST" and path == "/api/channels":
            ch = body["channel"]
            self.created[ch["id"]] = ch
            return _json(ch)
        if method == "POST" and path.startswith("/api/channels/") and path.endswith("/programming"):
            self.programming[path.split("/")[3]] = body
            return _json({})
        if method == "DELETE" and path.startswith("/api/channels/"):
            cid = path.split("/")[3]
            self.existing_channels = [c for c in self.existing_channels if c["id"] != cid]
            return _json({})
        if method == "GET" and path == "/api/xmltv.xml":
            return _Response(self.xmltv().encode())
        raise AssertionError(f"unexpected Tunarr request: {method} {path}")


class _Response(io.BytesIO):
    status = 200

    def __init__(self, data, content_type="application/json"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def getcode(self):
        return self.status


def _json(obj):
    return _Response(json.dumps(obj).encode())


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "fake", {}, io.BytesIO(b""))


def programming_payload(program_ids):
    """What Tunarr's GET /api/channels/{id}/programming returns for a channel airing
    these programs — read by the live-channel diff. Seed World(programming=...) with it."""
    return {"programs": {pid: {"id": pid} for pid in program_ids},
            "lineup": [{"type": "content", "id": pid} for pid in program_ids],
            "totalPrograms": len(program_ids)}
