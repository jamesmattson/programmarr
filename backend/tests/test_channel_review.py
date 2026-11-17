"""test_channel_review.py — GET /channels/{n}/review, and Apply writing re-found ids back.

The review checks ONE channel against the library on demand (skipped items, entries found
again through a spare id, plain titles that name more than one item) without writing anything
or touching Tunarr's channels. Apply refreshes an entry's saved ids only when a spare id
confirmed it is the same item — never from a title match.
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
import library_cache
from routers import channels_router
from test_id_index import _episode, _library, _movie


def _entry(kind, title, year, **ids):
    return {kind: title, "year": year, "ids": ids}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(library_cache, "_state", {"at": 0.0, "value": None, "generation": 0})
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    _library(monkeypatch, [("mv", "movies"), ("tv", "shows")],
             {"mv": [_movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                     _movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                     _movie("2 Guns", 2013, tmdb="11548", plex="7", tunarr="tid")],
              "tv": [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    calls = {"builds": 0, "deploys": 0}
    real = channel_engine.build_library_index
    monkeypatch.setattr(channel_engine, "build_library_index",
                        lambda url: calls.__setitem__("builds", calls["builds"] + 1) or real(url))
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda *a, **k: calls.__setitem__("deploys", calls["deploys"] + 1))
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid-1", "number": n, "name": "Test"})
    _write_channels(tmp_path, ["Aladdin", "2 Guns", "Not A Real Movie", {"movie": "Cheers", "ids": {"tmdb": "nope"}}])
    return tmp_path, calls


def _write_channels(tmp_path, content):
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "shuffle": "shuffle", "content": content}]}))


def _saved(tmp_path):
    return json.loads((tmp_path / "channels.json").read_text())["channels"][0]["content"]


def _review(number=1, **kw):
    kw.setdefault("refresh", False)
    return asyncio.run(channels_router.channel_review(number, **kw))


def _apply():
    return asyncio.run(channels_router.apply_channel(1))


# ── GET /channels/{n}/review ─────────────────────────────────────────────────────

def test_review_lists_skipped_and_ambiguous_titles_together(server):
    r = _review()
    assert r["missing_count"] == 2 and {m["label"] for m in r["missing"]} == {"Not A Real Movie", "Cheers"}
    assert r["ambiguous_count"] == 1 and r["ambiguous"][0]["label"] == "Aladdin"
    assert [o["year"] for o in r["ambiguous"][0]["options"]] == [1992, 2019]


def test_review_writes_nothing_and_deploys_nothing(server):
    tmp_path, calls = server
    before = sorted(p.name for p in tmp_path.iterdir())
    channels_before = (tmp_path / "channels.json").read_text()
    _review()
    assert calls["deploys"] == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert (tmp_path / "channels.json").read_text() == channels_before


def test_a_clean_channel_reviews_as_all_zeros(server):
    tmp_path, _ = server
    _write_channels(tmp_path, ["2 Guns", "Cheers"])
    assert _review() == {"missing_count": 0, "missing": [], "healed_count": 0,
                         "ambiguous_count": 0, "ambiguous": []}


def test_review_reuses_the_cached_library_until_refreshed(server):
    _, calls = server
    _review(); _review()
    assert calls["builds"] == 1
    _review(refresh=True)
    assert calls["builds"] == 2


def test_review_of_an_unknown_channel_is_a_404(server):
    with pytest.raises(HTTPException) as e:
        _review(number=99)
    assert e.value.status_code == 404


def test_review_when_tunarr_is_not_configured_is_a_400(server):
    tmp_path, _ = server
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(HTTPException) as e:
        _review()
    assert e.value.status_code == 400


def test_review_when_tunarr_is_down_is_a_502(server, monkeypatch):
    def boom(url): raise channel_engine.ChannelEngineError("Tunarr unreachable")
    monkeypatch.setattr(channel_engine, "build_library_index", boom)
    with pytest.raises(HTTPException) as e:
        _review()
    assert e.value.status_code == 502


# ── Apply writes re-found ids back ──────────────────────────────────────────────────

def test_apply_response_is_unchanged(server):
    tmp_path, _ = server
    _write_channels(tmp_path, ["2 Guns"])
    assert _apply() == {"ok": True, "number": 1, "program_count": 1}


def test_apply_writes_ids_confirmed_by_a_spare_id_back_into_the_channel(server):
    tmp_path, _ = server
    stale = _entry("movie", "Aladdin", 2019, tmdb="STALE", plex="srcA:502")
    _write_channels(tmp_path, [stale, "2 Guns"])
    _apply()
    saved = _saved(tmp_path)
    assert saved[0]["ids"] == {"tmdb": "420817", "plex": "srcA:502", "tunarr": "a19"}
    assert saved[1] == "2 Guns"
    assert _review()["healed_count"] == 0  # now matches on the first id


def test_apply_never_rewrites_ids_from_a_title_and_year_match(server):
    tmp_path, calls = server
    stale = _entry("movie", "Aladdin", 2019, tmdb="x", plex="srcA:x", tunarr="x")
    _write_channels(tmp_path, [stale])
    assert _apply()["program_count"] == 1  # it still plays the one 2019 Aladdin...
    assert _saved(tmp_path) == [stale]     # ...but its saved ids are left exactly as they were


def test_a_clean_apply_does_not_rewrite_the_file(server):
    tmp_path, _ = server
    _write_channels(tmp_path, [_entry("movie", "Aladdin", 1992, tmdb="812"), "2 Guns"])
    before = (tmp_path / "channels.json").read_text()
    _apply()
    assert (tmp_path / "channels.json").read_text() == before
