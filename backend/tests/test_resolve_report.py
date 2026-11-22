"""test_resolve_report.py — resolve_content can say WHY it skipped something.

Before: a title that no longer matched simply vanished from its channel; the "missing" list
was thrown away. resolve_content(report=...) now records the reason for every skipped item,
and every saved entry that was found again through a backup id, so it can be shown and the
fresh ids written back. Callers that pass no report behave exactly as before.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
from test_id_index import _episode, _index, _movie


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


def _report(content, lib, **kw):
    report = {}
    resolved, missing = channel_engine.resolve_content(content, *lib[:2], id_index=lib[2], report=report, **kw)
    return resolved, missing, report


# ── the engine says why ─────────────────────────────────────────────────────────────

def test_a_title_that_isnt_in_the_library_is_reported_with_a_reason(lib):
    resolved, missing, report = _report(["2 Guns", "Not A Real Movie"], lib)
    assert len(resolved) == 1 and missing == ["Not A Real Movie"]
    assert report["missing"] == [{"label": "Not A Real Movie", "why": "not found in your library"}]
    assert "healed" not in report


def test_an_ambiguous_saved_entry_says_to_pick_again(lib):
    _, _, report = _report([{"movie": "Aladdin", "ids": {"tmdb": "x"}}], lib)
    (m,) = report["missing"]
    assert m["label"] == "Aladdin" and "more than one" in m["why"]


def test_a_saved_entry_that_matches_nothing_says_not_found(lib):
    _, _, report = _report([_entry("movie", "Gone", 2001, tmdb="nope")], lib)
    assert report["missing"] == [{"label": "Gone", "why": "not found in your library"}]


def test_a_missing_collection_is_reported(lib, monkeypatch):
    monkeypatch.setattr(channel_engine, "plex_get", lambda *a, **k: None)
    _, _, report = _report([{"collection": "Nope"}], lib, plex_url="http://p", plex_token="t",
                           plex_sections=[{"key": "1"}], collection_cache={})
    assert report["missing"] == [{"label": "[collection:Nope]", "why": "collection not found in Plex"}]


def test_an_entry_found_through_a_backup_id_is_reported_as_healed(lib):
    entry = _entry("movie", "Aladdin", 2019, tmdb="STALE", plex="srcA:502")
    resolved, missing, report = _report([entry], lib)
    assert len(resolved) == 1 and not missing and "missing" not in report
    (h,) = report["healed"]
    assert h["entry"] == entry and h["ids"]["tmdb"] == "420817" and h["year"] == 2019 and h["kind"] == "movie"


def test_an_entry_found_only_by_title_and_year_plays_but_is_not_offered_for_write_back(lib):
    """Every saved id is stale: title + year still finds the one 2019 Aladdin, so it plays,
    but no id confirmed it — its saved ids must not be rewritten from a title match."""
    entry = _entry("movie", "Aladdin", 2019, tmdb="x", plex="srcA:x", tunarr="x")
    resolved, missing, report = _report([entry], lib)
    assert len(resolved) == 1 and resolved[0]["year"] == 2019 and not missing
    assert "healed" not in report


def test_a_clean_resolve_leaves_the_report_empty(lib):
    _, _, report = _report([_entry("movie", "Aladdin", 2019, tmdb="420817"), "2 Guns", "Cheers"], lib)
    assert "missing" not in report and "healed" not in report


def test_no_report_means_exactly_the_old_behaviour(lib):
    resolved, missing = channel_engine.resolve_content(["Not A Real Movie"], *lib[:2], id_index=lib[2])
    assert resolved == [] and missing == ["Not A Real Movie"]


# ── writing the re-found ids back ───────────────────────────────────────────────────

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


# ── summarize_report (what the review endpoint returns) ─────────────────────────────

def test_a_clean_report_summarizes_to_zeros():
    zeros = {"missing_count": 0, "missing": [], "healed_count": 0, "ambiguous_count": 0, "ambiguous": []}
    assert channel_engine.summarize_report({}) == zeros
    assert channel_engine.summarize_report(None) == zeros


def test_a_long_list_is_capped_but_the_real_count_is_kept():
    report = {"missing": [{"label": f"Movie {i}", "why": "not found in your library"} for i in range(300)],
              "ambiguous": [{"label": f"T{i}", "options": []} for i in range(120)]}
    s = channel_engine.summarize_report(report)
    assert s["missing_count"] == 300 and len(s["missing"]) == channel_engine.REVIEW_MAX_MISSING
    assert s["ambiguous_count"] == 120 and len(s["ambiguous"]) == channel_engine.REVIEW_MAX_AMBIGUOUS


def test_the_summary_has_no_timestamp():
    assert "at" not in channel_engine.summarize_report({"missing": [{"label": "A", "why": "w"}]})
