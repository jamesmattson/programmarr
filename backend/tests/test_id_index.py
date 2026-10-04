"""test_id_index.py — find a movie/show by its own unique numbers, never by title.

A title can be shared by several different things: the two Aladdins, The Office US and
UK, Wonder Woman the movie and the show. The by-title index (movie_map / show_map) can
only hold one of them; the id index holds every one, each reachable by its TMDB number,
its Plex id, or Tunarr's own id. Family videos ("Other Videos" libraries, no public
database numbers) are indexed too and found by their Plex / Tunarr ids.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine


def _idents(tmdb=None, plex=None, tvdb=None):
    out = []
    if tmdb:
        out.append({"type": "tmdb", "id": tmdb})
    if tvdb:
        out.append({"type": "tvdb", "id": tvdb})
    if plex:
        out.append({"type": "plex", "id": plex})
    out.append({"type": "plex-guid", "id": f"plex://x/{plex or tmdb or 'none'}"})
    return out


def _movie(title, year, tmdb=None, plex=None, tunarr="t", src="srcA", state="ok", idents=True):
    prog = {"title": title, "year": year, "state": state, "mediaSourceId": src}
    if idents:
        prog["identifiers"] = _idents(tmdb=tmdb, plex=plex)
    return {"id": tunarr, "program": prog}


def _episode(show_title, show_uuid, year, tmdb=None, plex=None, n=1, src="srcA", state="ok"):
    return {"id": f"{show_uuid}-e{n}", "program": {
        "title": f"{show_title} S1E{n}", "state": state,
        "show": {"uuid": show_uuid, "title": show_title, "year": year, "mediaSourceId": src,
                 "identifiers": _idents(tmdb=tmdb, plex=plex)}}}


def _library(monkeypatch, libs, programs_by_lib, extra_source=None):
    """Fake Tunarr: `libs` = [(id, mediaType), ...] in one Plex source."""
    sources = [{"name": "Plex", "libraries": [
        {"id": lid, "name": lid, "mediaType": mt, "enabled": True} for lid, mt in libs]}]
    if extra_source:
        sources.append(extra_source)
    monkeypatch.setattr(channel_engine, "get_plex_sources", lambda url: sources)

    def fake_api(url, method, path, body=None, timeout=60):
        for lid, items in programs_by_lib.items():
            if path.endswith(f"/{lid}/programs"):
                return items
        raise AssertionError(f"unexpected api call: {path}")
    monkeypatch.setattr(channel_engine, "api", fake_api)


def _index(monkeypatch, libs, programs_by_lib, **kw):
    _library(monkeypatch, libs, programs_by_lib, **kw)
    return channel_engine.build_library_index_with_ids("http://t")


def _find(id_index, kind, source, value):
    return id_index["by_id"][kind].get((source, value))


# ── other videos (family videos) are read at all ──────────────────────────────────

def test_other_videos_library_is_indexed(monkeypatch):
    movie_map, _, idx = _index(
        monkeypatch, [("fam", "other_videos")],
        {"fam": [_movie("Christmas 2019", 2019, plex="900", tunarr="fam-1", idents=True)]})
    assert "christmas 2019" in movie_map
    assert _find(idx, "movie", "tunarr", "fam-1")["title"] == "Christmas 2019"


def test_plain_build_library_index_reads_other_videos_too(monkeypatch):
    _library(monkeypatch, [("fam", "other_videos")], {"fam": [_movie("Beach Day", 2020, tunarr="f2")]})
    movie_map, _ = channel_engine.build_library_index("http://t")
    assert "beach day" in movie_map


def test_a_home_video_never_displaces_a_same_named_movie(monkeypatch):
    """Other Videos are read AFTER real movie libraries — even when listed first."""
    movie_map, _, _ = _index(
        monkeypatch, [("fam", "other_videos"), ("mv", "movies")],
        {"fam": [_movie("Frozen", 2020, tunarr="home")], "mv": [_movie("Frozen", 2013, tmdb="109445", tunarr="real")]})
    assert movie_map["frozen"]["id"] == "real"


# ── the collisions from a real library ────────────────────────────────────────────

def test_two_movies_sharing_a_title_are_both_reachable(monkeypatch):
    movie_map, _, idx = _index(
        monkeypatch, [("mv", "movies")],
        {"mv": [_movie("Aladdin", 1992, tmdb="812", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", tunarr="a19")]})
    assert len(movie_map) == 1  # the by-title view can only hold one
    assert _find(idx, "movie", "tmdb", "812")["year"] == 1992
    assert _find(idx, "movie", "tmdb", "420817")["year"] == 2019


def test_two_shows_sharing_a_title_are_both_reachable(monkeypatch):
    _, show_map, idx = _index(
        monkeypatch, [("tv", "shows")],
        {"tv": [_episode("The Office", "uk-uuid", 2001, tmdb="2996", n=1),
                _episode("The Office", "us-uuid", 2005, tmdb="2316", n=1),
                _episode("The Office", "us-uuid", 2005, tmdb="2316", n=2)]})
    assert len(show_map) == 1  # the by-title view can only hold one
    uk, us = _find(idx, "show", "tmdb", "2996"), _find(idx, "show", "tmdb", "2316")
    assert (uk["showId"], len(uk["programs"])) == ("uk-uuid", 1)
    assert (us["showId"], len(us["programs"])) == ("us-uuid", 2)


def test_wonder_woman_movie_and_show_never_collide(monkeypatch):
    """Even if TMDB used the same number for both, movie and show tables are separate."""
    _, _, idx = _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Wonder Woman", 2017, tmdb="297762", tunarr="ww-movie")],
         "tv": [_episode("Wonder Woman", "ww-show", 1975, tmdb="297762", n=1)]})
    assert _find(idx, "movie", "tmdb", "297762")["type"] == "Movie"
    assert _find(idx, "show", "tmdb", "297762")["type"] == "TV"


# ── the numbers themselves ────────────────────────────────────────────────────────

def test_movie_carries_tmdb_plex_and_tunarr_numbers(monkeypatch):
    _, _, idx = _index(monkeypatch, [("mv", "movies")],
                       {"mv": [_movie("2 Guns", 2013, tmdb="11548", plex="116400", tunarr="tid")]})
    item = _find(idx, "movie", "tmdb", "11548")
    assert item["ids"] == {"tmdb": "11548", "plex": "srcA:116400", "tunarr": "tid"}
    assert _find(idx, "movie", "plex", "srcA:116400") is item
    assert _find(idx, "movie", "tunarr", "tid") is item


def test_plex_ids_from_two_servers_do_not_collide(monkeypatch):
    _, _, idx = _index(
        monkeypatch, [("mv", "movies")],
        {"mv": [_movie("Alpha", 2000, plex="100", tunarr="a", src="srcA"),
                _movie("Beta", 2001, plex="100", tunarr="b", src="srcB")]})
    assert _find(idx, "movie", "plex", "srcA:100")["title"] == "Alpha"
    assert _find(idx, "movie", "plex", "srcB:100")["title"] == "Beta"


def test_family_video_with_no_public_numbers_is_found_by_its_own(monkeypatch):
    _, _, idx = _index(monkeypatch, [("fam", "other_videos")],
                       {"fam": [_movie("Birthday", 2018, plex="55", tunarr="bday")]})
    item = _find(idx, "movie", "tunarr", "bday")
    assert item["title"] == "Birthday" and "tmdb" not in item["ids"]


def test_older_tunarr_without_identifiers_still_indexes_by_tunarr_id(monkeypatch):
    _, _, idx = _index(monkeypatch, [("mv", "movies")],
                       {"mv": [_movie("Old Movie", 1990, tunarr="old", idents=False)]})
    assert _find(idx, "movie", "tunarr", "old")["ids"] == {"tunarr": "old"}


def test_same_movie_in_two_libraries_keeps_the_playable_copy(monkeypatch):
    _, _, idx = _index(
        monkeypatch, [("hd", "movies"), ("uhd", "movies")],
        {"hd": [_movie("Dune", 2021, tmdb="438631", tunarr="dead", state="missing")],
         "uhd": [_movie("Dune", 2021, tmdb="438631", tunarr="live")]})
    assert _find(idx, "movie", "tmdb", "438631")["ids"]["tunarr"] == "live"


# ── resolve_by_ids ───────────────────────────────────────────────────────────────

@pytest.fixture
def idx(monkeypatch):
    return _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("2 Guns", 2013, tmdb="11548", plex="116400", tunarr="tid"),
                _movie("Aladdin", 1992, tmdb="812", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", tunarr="a19")],
         "tv": [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})[2]


def test_first_saved_number_matches(idx):
    item, status, cur = channel_engine.resolve_by_ids(
        "movie", {"tmdb": "11548", "plex": "srcA:116400", "tunarr": "tid"}, "2 Guns", 2013, idx)
    assert status == "ok" and item["title"] == "2 Guns"


def test_a_spare_number_heals_when_the_main_one_stops_matching(idx):
    """A re-match changed the TMDB number; Plex's own id still finds it — re-save."""
    item, status, cur = channel_engine.resolve_by_ids(
        "movie", {"tmdb": "OLD-NUMBER", "plex": "srcA:116400"}, "2 Guns", 2013, idx)
    assert status == "healed"
    assert cur["tmdb"] == "11548"  # the fresh numbers to save back


def test_title_and_year_heal_when_every_number_is_stale(idx):
    item, status, cur = channel_engine.resolve_by_ids(
        "movie", {"tmdb": "gone", "plex": "srcA:gone", "tunarr": "gone"}, "2 Guns", 2013, idx)
    assert status == "healed" and item["ids"]["tmdb"] == "11548"


def test_title_and_year_pick_the_right_remake(idx):
    item, status, _ = channel_engine.resolve_by_ids("movie", {"tmdb": "gone"}, "Aladdin", 2019, idx)
    assert status == "healed" and item["year"] == 2019


def test_title_alone_is_ambiguous_between_remakes_so_it_asks_never_guesses(idx):
    item, status, cur = channel_engine.resolve_by_ids("movie", {"tmdb": "gone"}, "Aladdin", None, idx)
    assert (item, status, cur) == (None, "ambiguous", None)


def test_nothing_matches_is_missing(idx):
    assert channel_engine.resolve_by_ids("movie", {"tmdb": "nope"}, "Unknown", 1999, idx)[1] == "missing"


def test_a_movie_number_never_finds_a_show(idx):
    assert channel_engine.resolve_by_ids("show", {"tmdb": "11548"}, None, None, idx)[1] == "missing"


def test_two_copies_of_one_movie_are_not_ambiguous(monkeypatch):
    idx2 = _index(
        monkeypatch, [("hd", "movies"), ("uhd", "movies")],
        {"hd": [_movie("Dune", 2021, tmdb="438631", tunarr="hd-copy")],
         "uhd": [_movie("Dune", 2021, tmdb="438631", tunarr="uhd-copy")]})[2]
    item, status, _ = channel_engine.resolve_by_ids("movie", {"tmdb": "gone"}, "Dune", 2021, idx2)
    assert status == "healed" and item["ids"]["tmdb"] == "438631"
