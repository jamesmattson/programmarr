"""test_library_lookup.py — "which items in the library have exactly this title?"

The Add-title box asks this on every add. One match: the entry is saved with the item's
own numbers. Several (the two Aladdins): the box asks which. None: the text is added as
typed. find_by_title is the matching; GET /library/lookup wraps it with a short cache so
typing doesn't rescan Tunarr every time.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
from routers import channels_router
from test_id_index import _episode, _index, _library, _movie


@pytest.fixture
def idx(monkeypatch):
    return _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                _movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Aladdin and the Lamp", 2000, tmdb="9", tunarr="lamp"),
                _movie("Wonder Woman", 2017, tmdb="297762", plex="116400", tunarr="ww-movie"),
                _movie("No Year", None, tmdb="5", tunarr="ny")],
         "tv": [_episode("Wonder Woman", "ww-show", 1975, tmdb="1924", plex="7000", n=1)]})[2]


# ── find_by_title ────────────────────────────────────────────────────────────────

def test_both_remakes_come_back_oldest_first(idx):
    found = channel_engine.find_by_title("Aladdin", idx)
    assert [(f["type"], f["year"]) for f in found] == [("Movie", 1992), ("Movie", 2019)]


def test_only_the_exact_title_matches_not_a_longer_one(idx):
    assert [f["title"] for f in channel_engine.find_by_title("aladdin", idx)] == ["Aladdin", "Aladdin"]


def test_a_movie_and_a_show_sharing_a_title_both_come_back(idx):
    found = channel_engine.find_by_title("Wonder Woman", idx)
    assert [f["type"] for f in found] == ["Movie", "TV"]


def test_kind_narrows_it(idx):
    assert [f["type"] for f in channel_engine.find_by_title("Wonder Woman", idx, kind="show")] == ["TV"]


def test_nothing_or_blank_finds_nothing(idx):
    assert channel_engine.find_by_title("Not In Library", idx) == []
    assert channel_engine.find_by_title("   ", idx) == []


def test_two_library_copies_of_one_movie_are_one_match(monkeypatch):
    idx2 = _index(
        monkeypatch, [("hd", "movies"), ("uhd", "movies")],
        {"hd": [_movie("Dune", 2021, tmdb="438631", tunarr="hd", state="missing")],
         "uhd": [_movie("Dune", 2021, tmdb="438631", tunarr="uhd")]})[2]
    found = channel_engine.find_by_title("Dune", idx2)
    assert len(found) == 1 and found[0]["ids"]["tunarr"] == "uhd"  # the playable copy


# ── GET /library/lookup ──────────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    monkeypatch.setattr(channels_router, "_index_cache", {"at": 0.0, "index": None})
    _library(monkeypatch, [("mv", "movies"), ("tv", "shows")],
             {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                     _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                     _movie("No Year", None, tmdb="5", tunarr="ny")],
              "tv": [_episode("The Office", "office-us", 2005, tmdb="2316", plex="31", n=1)]})
    builds = []
    real = channel_engine.build_library_index_with_ids
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids", lambda url: builds.append(1) or real(url))
    return builds


def _lookup(**kw):
    kw.setdefault("kind", "")
    kw.setdefault("refresh", False)
    return asyncio.run(channels_router.library_lookup(**kw))


def test_two_matches_each_with_a_ready_to_save_entry(server):
    matches = _lookup(title="Aladdin")["matches"]
    assert [m["year"] for m in matches] == [1992, 2019]
    assert matches[1]["entry"] == {"movie": "Aladdin", "year": 2019,
                                   "ids": {"tmdb": "420817", "plex": "srcA:502", "tunarr": "a19"}}


def test_one_match_is_a_show_entry(server):
    (m,) = _lookup(title="The Office")["matches"]
    assert m["kind"] == "show" and m["entry"]["show"] == "The Office" and m["entry"]["ids"]["tmdb"] == "2316"


def test_no_year_means_no_year_key_in_the_entry(server):
    (m,) = _lookup(title="No Year")["matches"]
    assert "year" not in m["entry"]


def test_no_match_is_an_empty_list_not_an_error(server):
    assert _lookup(title="Nothing Like It") == {"matches": []}


def test_the_index_is_reused_between_lookups_until_refreshed(server):
    _lookup(title="Aladdin"); _lookup(title="The Office")
    assert len(server) == 1
    _lookup(title="Aladdin", refresh=True)
    assert len(server) == 2


def test_a_bad_kind_is_rejected(server):
    with pytest.raises(HTTPException) as e:
        _lookup(title="Aladdin", kind="book")
    assert e.value.status_code == 400


def test_tunarr_not_configured_is_a_400(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(HTTPException) as e:
        _lookup(title="Aladdin")
    assert e.value.status_code == 400


def test_tunarr_down_is_a_502_so_the_box_can_fall_back_to_the_typed_text(server, monkeypatch):
    monkeypatch.setattr(channels_router, "_index_cache", {"at": 0.0, "index": None})
    def boom(url): raise channel_engine.ChannelEngineError("Tunarr unreachable")
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids", boom)
    with pytest.raises(HTTPException) as e:
        _lookup(title="Aladdin")
    assert e.value.status_code == 502
