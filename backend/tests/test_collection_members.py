"""test_collection_members.py — a Plex collection's members are matched by Plex's own id.

The real bug: a channel built from {"collection": "3 Stars and Up"} played the Wonder
Woman TV show. Plex's answer about the collection says the member is a MOVIE and gives
its Plex id — but the engine kept only the title "Wonder Woman", and a bare title
matches the show (it has far more episodes). Now the label and the id are kept.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
from test_id_index import _episode, _index, _movie


def _plex(monkeypatch, members):
    """Fake Plex: one library holding one collection, "3 Stars and Up"."""
    def fake_plex_get(base_url, token, path, timeout=60):
        if path == "/library/sections/1/collections":
            return {"MediaContainer": {"Metadata": [{"title": "3 Stars and Up", "ratingKey": "9000"}]}}
        if path == "/library/collections/9000/children":
            return {"MediaContainer": {"Metadata": members}}
        return None
    monkeypatch.setattr(channel_engine, "plex_get", fake_plex_get)


def _member(title, type_, rating_key, year=None):
    return {"title": title, "type": type_, "ratingKey": rating_key, "year": year}


def _resolve(content, movie_map, show_map, idx):
    return channel_engine.resolve_content(
        content, movie_map, show_map, plex_url="http://plex", plex_token="t",
        plex_sections=[{"key": "1"}], collection_cache={}, id_index=idx)


@pytest.fixture
def ww_library(monkeypatch):
    """A library where "Wonder Woman" is BOTH a movie and a 60-episode show."""
    movie_map, show_map, idx = _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Wonder Woman", 2017, tmdb="297762", plex="116400", tunarr="ww-movie")],
         "tv": [_episode("Wonder Woman", "ww-show", 1975, tmdb="1924", plex="7000", n=i) for i in range(60)]})
    return movie_map, show_map, idx


# ── the actual bug ───────────────────────────────────────────────────────────────

def test_collection_member_that_is_a_movie_stays_the_movie(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017)])
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert not missing
    assert [r["type"] for r in resolved] == ["Movie"]


def test_the_old_title_only_way_really_did_pick_the_show(ww_library):
    """Documents what used to happen, so the test above is known to mean something."""
    movie_map, show_map, _ = ww_library
    assert channel_engine.resolve_title("Wonder Woman", movie_map, show_map)["type"] == "TV"


def test_a_show_member_resolves_to_the_show(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Wonder Woman", "show", 7000, 1975)])
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert not missing
    assert resolved[0]["type"] == "TV" and len(resolved[0]["programs"]) == 60


def test_without_the_id_index_the_label_alone_still_prevents_the_mixup(monkeypatch, ww_library):
    """Older callers that don't pass an id index still get movie-vs-show right."""
    movie_map, show_map, _ = ww_library
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017)])
    resolved, _ = _resolve([{"collection": "3 Stars and Up"}], movie_map, show_map, None)
    assert resolved[0]["type"] == "Movie"


# ── remakes: same title, same type ───────────────────────────────────────────────

def test_the_right_remake_is_picked_by_plex_id(monkeypatch):
    movie_map, show_map, idx = _index(
        monkeypatch, [("mv", "movies")],
        {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19")]})
    _plex(monkeypatch, [_member("Aladdin", "movie", 502, 2019)])
    resolved, _ = _resolve([{"collection": "3 Stars and Up"}], movie_map, show_map, idx)
    assert resolved[0]["year"] == 2019


# ── when Plex and Tunarr disagree ────────────────────────────────────────────────

def test_tunarr_behind_plex_falls_back_to_a_unique_title_and_year(monkeypatch, ww_library):
    """Plex gave the movie a new id; Tunarr hasn't rescanned yet. Title + year names
    exactly one movie, so it is still found."""
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 999999, 2017)])
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert not missing and resolved[0]["type"] == "Movie"


def test_an_ambiguous_fallback_is_reported_not_guessed(monkeypatch):
    movie_map, show_map, idx = _index(
        monkeypatch, [("mv", "movies")],
        {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19")]})
    _plex(monkeypatch, [_member("Aladdin", "movie", 999999, None)])  # unknown id AND no year
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], movie_map, show_map, idx)
    assert resolved == [] and missing == ["Aladdin"]


def test_a_member_tunarr_has_never_heard_of_is_missing(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Not Synced Yet", "movie", 123, 2024)])
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert resolved == [] and missing == ["Not Synced Yet"]


def test_one_unfound_member_does_not_drop_the_others(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017),
                        _member("Not Synced Yet", "movie", 123, 2024)])
    resolved, missing = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert len(resolved) == 1 and missing == ["Not Synced Yet"]


# ── plumbing that must keep working ──────────────────────────────────────────────

def test_order_is_kept_between_typed_entries_and_collection_members(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017)])
    content = [{"show": "Wonder Woman"}, {"collection": "3 Stars and Up"}]
    resolved, _ = _resolve(content, *ww_library)
    assert [r["type"] for r in resolved] == ["TV", "Movie"]


def test_a_member_of_an_unexpected_type_still_falls_back_to_its_title(monkeypatch, ww_library):
    _plex(monkeypatch, [_member("Wonder Woman", "episode", 1, None)])
    resolved, _ = _resolve([{"collection": "3 Stars and Up"}], *ww_library)
    assert len(resolved) == 1  # the old behaviour, not a crash


def test_collection_members_keep_kind_id_and_year(monkeypatch):
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017)])
    members = channel_engine.resolve_collection_members(
        "http://plex", "t", "3 Stars and Up", [{"key": "1"}], {})
    assert members == [{"title": "Wonder Woman", "kind": "movie", "plex_id": "116400", "year": 2017}]


def test_resolve_collection_still_returns_plain_titles(monkeypatch):
    _plex(monkeypatch, [_member("Wonder Woman", "movie", 116400, 2017)])
    titles = channel_engine.resolve_collection("http://plex", "t", "3 Stars and Up", [{"key": "1"}], {})
    assert titles == ["Wonder Woman"]
