"""test_paged_identifiers.py — the id index still works on Tunarr 2026.11's paged reads.

Tunarr #2423 pages /media-libraries/{id}/programs as {total, result} (100 per page by
default). test_library_programs_paging.py proves every page is read; test_id_index.py
proves the id index on the old bare list. This file joins the two: each item's
`identifiers` must survive paging, so an item on page 2 is found by its TMDB number,
its Plex id and Tunarr's id just like one on page 1 — and the index matches the one
built from the same data as a bare list.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine


def _movie(i, title=None, tmdb=None, plex=None):
    return {"id": f"m{i}", "program": {
        "title": title or f"Movie {i}", "year": 2000, "state": "ok", "mediaSourceId": "srcA",
        "identifiers": [{"type": "tmdb", "id": tmdb or f"tm{i}"},
                        {"type": "plex", "id": plex or f"px{i}"}]}}


def _episode(i, show_uuid, show_title, tmdb):
    return {"id": f"{show_uuid}-e{i}", "program": {
        "title": f"{show_title} E{i}", "state": "ok",
        "show": {"uuid": show_uuid, "title": show_title, "year": 1990, "mediaSourceId": "srcA",
                 "identifiers": [{"type": "tmdb", "id": tmdb}, {"type": "plex", "id": f"px-{show_uuid}"}]}}}


# 150 movies: two that share a title, one on each page (index 5 and 120).
MOVIES = [_movie(i) for i in range(150)]
MOVIES[5] = _movie(5, title="Aladdin", tmdb="812")
MOVIES[120] = _movie(120, title="Aladdin", tmdb="420817")
# 130 episodes: one show filling page 1, another starting on page 2.
EPISODES = ([_episode(i, "cheers", "Cheers", "4471") for i in range(110)]
            + [_episode(i, "frasier", "Frasier", "3452") for i in range(20)])
LIBS = {"mv": MOVIES, "tv": EPISODES}


def _fake_tunarr(monkeypatch, paged):
    """Fake Tunarr serving LIBS either paged (2026.11) or as bare lists (older)."""
    source = {"name": "Plex", "libraries": [
        {"id": "mv", "name": "mv", "mediaType": "movies", "enabled": True},
        {"id": "tv", "name": "tv", "mediaType": "shows", "enabled": True}]}
    monkeypatch.setattr(channel_engine, "get_plex_sources", lambda url: [source])
    calls = []

    def fake_api(url, method, path, body=None, timeout=60):
        calls.append(path)
        lid = path.split("/media-libraries/")[1].split("/")[0]
        items = LIBS[lid]
        if not paged:
            return items
        offset = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        limit = int(path.split("limit=")[1].split("&")[0]) if "limit=" in path else 100
        page = items[offset:offset + limit]
        return {"total": len(items), "result": page, "size": len(page), "offset": offset}
    monkeypatch.setattr(channel_engine, "api", fake_api)
    return calls


def _id_index(monkeypatch, paged):
    calls = _fake_tunarr(monkeypatch, paged)
    idx = channel_engine.library_id_index(channel_engine.build_library_index("http://t"))
    return idx, calls


def _find(idx, kind, source, value):
    return idx["by_id"][kind].get((source, value))


def test_items_from_every_page_keep_their_identifiers(monkeypatch):
    idx, calls = _id_index(monkeypatch, paged=True)
    assert any("offset=100" in c for c in calls)  # a second page really was read

    for i in (0, 140):  # page 1 and page 2
        item = _find(idx, "movie", "tmdb", f"tm{i}")
        assert item["title"] == f"Movie {i}"
        assert _find(idx, "movie", "plex", f"srcA:px{i}") is item
        assert _find(idx, "movie", "tunarr", f"m{i}") is item

    cheers, frasier = _find(idx, "show", "tmdb", "4471"), _find(idx, "show", "tmdb", "3452")
    assert (cheers["showId"], len(cheers["programs"])) == ("cheers", 110)   # spans both pages
    assert (frasier["showId"], len(frasier["programs"])) == ("frasier", 20)  # page 2 only
    assert _find(idx, "show", "plex", "srcA:px-frasier") is frasier
    assert _find(idx, "show", "tunarr", "frasier") is frasier


def test_same_title_on_different_pages_resolves_by_id(monkeypatch):
    idx, _ = _id_index(monkeypatch, paged=True)
    first, status1, _ = channel_engine.resolve_by_ids("movie", {"tmdb": "812"}, "Aladdin", None, idx)
    second, status2, _ = channel_engine.resolve_by_ids("movie", {"tmdb": "420817"}, "Aladdin", None, idx)
    assert (status1, first["ids"]["tunarr"]) == ("ok", "m5")
    assert (status2, second["ids"]["tunarr"]) == ("ok", "m120")


def test_paged_and_bare_list_build_the_same_id_index(monkeypatch):
    paged, _ = _id_index(monkeypatch, paged=True)
    bare, _ = _id_index(monkeypatch, paged=False)
    for kind in ("movie", "show"):
        assert paged["by_id"][kind].keys() == bare["by_id"][kind].keys()
        for key, item in paged["by_id"][kind].items():
            other = bare["by_id"][kind][key]
            assert (item["ids"], item["title"], len(item["programs"])) == \
                   (other["ids"], other["title"], len(other["programs"]))
