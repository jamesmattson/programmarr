"""test_notices.py — an item that can't be found is reported, not silently dropped.

Before: a title that no longer matched simply vanished from its channel and nothing said so
(the manual Apply and the auto-update both threw the "missing" list away). Now the engine
says WHY for every skipped item, an entry re-found through a backup number is written back
with its current numbers, and both Apply and the auto-update record a notice per channel
that the Channels screen shows. None of it can ever stop a deploy.
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
import notices
import scheduler
from routers import channels_router
from test_id_index import _episode, _index, _library, _movie


@pytest.fixture
def lib(monkeypatch):
    return _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                _movie("2 Guns", 2013, tmdb="11548", plex="116400", tunarr="tid")],
         "tv": [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})


def _entry(kind, title, year, **ids):
    return {kind: title, "year": year, "ids": ids}


# ── the engine says why ─────────────────────────────────────────────────────────────

def test_a_title_that_isnt_in_the_library_is_reported_with_a_reason(lib):
    report = {}
    resolved, missing = channel_engine.resolve_content(["2 Guns", "Not A Real Movie"], *lib[:2], id_index=lib[2], report=report)
    assert len(resolved) == 1 and missing == ["Not A Real Movie"]
    assert report["missing"] == [{"label": "Not A Real Movie", "why": "not found in your library"}]
    assert "healed" not in report


def test_an_ambiguous_saved_entry_says_to_pick_again(lib):
    report = {}
    channel_engine.resolve_content([{"movie": "Aladdin", "ids": {"tmdb": "x"}}], *lib[:2], id_index=lib[2], report=report)
    (m,) = report["missing"]
    assert m["label"] == "Aladdin" and "more than one" in m["why"]


def test_a_saved_entry_that_matches_nothing_says_not_found(lib):
    report = {}
    channel_engine.resolve_content([_entry("movie", "Gone", 2001, tmdb="nope")], *lib[:2], id_index=lib[2], report=report)
    assert report["missing"] == [{"label": "Gone", "why": "not found in your library"}]


def test_a_missing_collection_is_reported(lib, monkeypatch):
    monkeypatch.setattr(channel_engine, "plex_get", lambda *a, **k: None)
    report = {}
    channel_engine.resolve_content([{"collection": "Nope"}], *lib[:2], plex_url="http://p", plex_token="t",
                                   plex_sections=[{"key": "1"}], collection_cache={}, id_index=lib[2], report=report)
    assert report["missing"] == [{"label": "[collection:Nope]", "why": "collection not found in Plex"}]


def test_an_entry_found_through_a_backup_number_is_reported_as_healed(lib):
    report = {}
    entry = _entry("movie", "Aladdin", 2019, tmdb="STALE", plex="srcA:502")
    resolved, missing = channel_engine.resolve_content([entry], *lib[:2], id_index=lib[2], report=report)
    assert len(resolved) == 1 and not missing and "missing" not in report
    (h,) = report["healed"]
    assert h["entry"] == entry and h["ids"]["tmdb"] == "420817" and h["year"] == 2019 and h["kind"] == "movie"


def test_a_clean_resolve_leaves_the_report_empty(lib):
    report = {}
    channel_engine.resolve_content([_entry("movie", "Aladdin", 2019, tmdb="420817"), "2 Guns"], *lib[:2],
                                   id_index=lib[2], report=report)
    assert report == {}


def test_no_report_means_exactly_the_old_behaviour(lib):
    resolved, missing = channel_engine.resolve_content(["Not A Real Movie"], *lib[:2], id_index=lib[2])
    assert resolved == [] and missing == ["Not A Real Movie"]


# ── writing the re-found numbers back ────────────────────────────────────────────────

def _healed(entry, **fresh):
    return {"entry": entry, "kind": "movie", "title": "Aladdin", "year": 2019,
            "ids": {"tmdb": "420817", "plex": "srcA:502", "tunarr": "a19"}, **fresh}


def test_apply_healed_refreshes_only_the_entry_that_was_healed():
    stale = _entry("movie", "Aladdin", 2019, tmdb="STALE", plex="srcA:502")
    other = _entry("movie", "Aladdin", 1992, tmdb="812")
    out = channel_engine.apply_healed(["Plain", stale, other, {"collection": "X"}], [_healed(stale)])
    assert out[0] == "Plain" and out[2] == other and out[3] == {"collection": "X"}
    assert out[1] == {"movie": "Aladdin", "year": 2019,
                      "ids": {"tmdb": "420817", "plex": "srcA:502", "tunarr": "a19"}}


def test_apply_healed_leaves_an_entry_the_person_changed_in_the_meantime():
    stale = _entry("movie", "Aladdin", 2019, tmdb="STALE")
    edited = _entry("movie", "Aladdin", 2019, tmdb="SOMETHING-ELSE")
    assert channel_engine.apply_healed([edited], [_healed(stale)]) == [edited]


def test_apply_healed_keeps_any_other_keys_on_the_entry():
    stale = {**_entry("movie", "Aladdin", 2019, tmdb="STALE"), "note": "keep me"}
    (out,) = channel_engine.apply_healed([stale], [_healed(stale)])
    assert out["note"] == "keep me" and out["ids"]["tmdb"] == "420817"


# ── the notices file ────────────────────────────────────────────────────────────────

def test_a_clean_report_is_no_notice():
    assert notices.summarize({}) is None and notices.summarize(None) is None


def test_a_long_list_is_capped_but_the_real_count_is_kept():
    report = {"missing": [{"label": f"Movie {i}", "why": "not found in your library"} for i in range(300)]}
    n = notices.summarize(report)
    assert n["missing_count"] == 300 and len(n["missing"]) == notices.MAX_SHOWN


def test_record_replaces_then_clears(tmp_path):
    notices.record(tmp_path, 3, {"missing": [{"label": "A", "why": "w"}]})
    assert notices.load(tmp_path)["3"]["missing_count"] == 1
    notices.record(tmp_path, 3, {"missing": [{"label": "A", "why": "w"}, {"label": "B", "why": "w"}]})
    assert notices.load(tmp_path)["3"]["missing_count"] == 2
    assert notices.record(tmp_path, 3, {}) is None
    assert notices.load(tmp_path) == {}
    assert not list(tmp_path.glob("*.tmp"))  # the atomic write leaves nothing behind


def test_channels_keep_separate_notices(tmp_path):
    notices.record(tmp_path, 3, {"missing": [{"label": "A", "why": "w"}]})
    notices.record(tmp_path, 8, {"healed": [{}]})
    assert set(notices.load(tmp_path)) == {"3", "8"}


def test_a_broken_notices_file_is_ignored_not_fatal(tmp_path):
    (tmp_path / notices.FILE).write_text("{not json")
    assert notices.load(tmp_path) == {}
    notices.record(tmp_path, 1, {"missing": [{"label": "A", "why": "w"}]})  # and it is repaired
    assert "1" in notices.load(tmp_path)


def test_an_unwritable_folder_never_raises(tmp_path):
    assert notices.record(tmp_path / "does" / "not" / "exist", 1, {"missing": [{"label": "A", "why": "w"}]})


# ── manual Apply ────────────────────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    _library(monkeypatch, [("mv", "movies"), ("tv", "shows")],
             {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                     _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                     _movie("2 Guns", 2013, tmdb="11548", plex="116400", tunarr="tid")],
              "tv": [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid-1", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "update_channel_in_place", lambda *a, **k: None)
    monkeypatch.setattr(channel_engine, "read_channel_programming", lambda url, cid: set())
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    return tmp_path


def _write_channels(tmp_path, content, live=False):
    ch = {"number": 1, "name": "Test", "shuffle": "shuffle", "content": content}
    if live:
        ch["live"] = True
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [ch]}))


def _saved(tmp_path):
    return json.loads((tmp_path / "channels.json").read_text())["channels"][0]["content"]


def _apply():
    return asyncio.run(channels_router.apply_channel(1))


def test_apply_tells_you_what_was_skipped(server):
    _write_channels(server, ["2 Guns", "Not A Real Movie"])
    result = _apply()
    assert result["program_count"] == 1
    assert result["notice"]["missing"] == [{"label": "Not A Real Movie", "why": "not found in your library"}]
    assert notices.load(server)["1"]["missing_count"] == 1
    assert channels_router.channel_notices()["1"]["missing_count"] == 1


def test_a_clean_apply_clears_the_old_notice(server):
    notices.record(server, 1, {"missing": [{"label": "Old", "why": "w"}]})
    _write_channels(server, ["2 Guns"])
    assert _apply()["notice"] is None
    assert notices.load(server) == {}


def test_apply_writes_refound_numbers_back_into_the_channel(server):
    stale = _entry("movie", "Aladdin", 2019, tmdb="STALE", plex="srcA:502")
    _write_channels(server, [stale, "2 Guns"])
    result = _apply()
    assert result["notice"]["healed_count"] == 1
    assert _saved(server)[0]["ids"]["tmdb"] == "420817" and _saved(server)[1] == "2 Guns"
    assert _apply()["notice"] is None  # now matches on the first number: nothing left to report


def test_a_channel_that_resolves_to_nothing_still_explains_itself(server):
    _write_channels(server, ["Not A Real Movie"])
    with pytest.raises(HTTPException) as e:
        _apply()
    assert e.value.status_code == 409
    assert notices.load(server)["1"]["missing"][0]["label"] == "Not A Real Movie"


def test_apply_still_works_when_the_notices_cannot_be_saved(server, monkeypatch):
    """The notices are information: a full or read-only disk must never fail a deploy."""
    def disk_full(self, target):
        raise OSError("no space left on device")
    monkeypatch.setattr(Path, "replace", disk_full)  # the notices file's atomic swap
    _write_channels(server, ["2 Guns", "Not A Real Movie"])
    result = _apply()
    assert result["program_count"] == 1
    assert result["notice"]["missing_count"] == 1  # the caller is still told, just not persisted
    assert notices.load(server) == {}


# ── the auto-update cycle ───────────────────────────────────────────────────────────

def test_the_auto_update_records_notices_for_live_channels(server):
    _write_channels(server, ["2 Guns", "Not A Real Movie"], live=True)
    summary = scheduler._run_cycle_blocking(apply=True)
    assert summary["error"] is None
    assert notices.load(server)["1"]["missing"][0]["label"] == "Not A Real Movie"


def test_a_dry_run_never_touches_the_notices(server):
    _write_channels(server, ["2 Guns", "Not A Real Movie"], live=True)
    scheduler._run_cycle_blocking(apply=False)
    assert notices.load(server) == {}
