"""test_ambiguous.py — a plain title that names more than one thing gets flagged, not guessed.

Real library: 40 entries across 12 channels ("Beauty and the Beast", "The Lion King", "Aladdin"…)
match two movies. A plain title plays ONE of them, chosen arbitrarily, and never the other.
Nothing is broken — it just isn't a choice anyone made. The engine now says so, describes the
options (each with the entry that would pin it) and which one plays right now; the editor
offers [year] [year] [Both]. Flagging must never change what plays.
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
    """The 2019 Aladdin is listed FIRST, so a bare "Aladdin" plays the 2019 one today."""
    return _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                _movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Wonder Woman", 2017, tmdb="297762", plex="116400", tunarr="ww-movie"),
                _movie("2 Guns", 2013, tmdb="11548", plex="7", tunarr="tid")],
         "tv": [_episode("Wonder Woman", "ww-show", 1975, tmdb="1924", plex="7000", n=i) for i in range(60)] +
               [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})


def _report(content, lib, **kw):
    report = {}
    channel_engine.resolve_content(content, lib[0], lib[1], id_index=lib[2], report=report, **kw)
    return report


# ── what gets flagged ────────────────────────────────────────────────────────────

def test_a_plain_remake_title_is_flagged_with_both_options_oldest_first(lib):
    (a,) = _report(["Aladdin"], lib)["ambiguous"]
    assert a["label"] == "Aladdin" and a["kind"] is None
    assert [o["year"] for o in a["options"]] == [1992, 2019]


def test_it_says_which_one_plays_right_now(lib):
    (a,) = _report(["Aladdin"], lib)["ambiguous"]
    assert a["options"][a["current"]]["year"] == 2019  # the one listed first — arbitrary, and now visible


def test_each_option_carries_the_entry_that_pins_it(lib):
    (a,) = _report(["Aladdin"], lib)["ambiguous"]
    assert a["options"][0]["entry"] == {"movie": "Aladdin", "year": 1992,
                                        "ids": {"tmdb": "812", "plex": "srcA:501", "tunarr": "a92"}}
    resolved, missing = channel_engine.resolve_content([a["options"][0]["entry"]], lib[0], lib[1], id_index=lib[2])
    assert not missing and resolved[0]["year"] == 1992  # choosing it really plays that one


def test_a_movie_and_a_show_sharing_a_title_is_flagged_and_the_show_currently_wins(lib):
    (a,) = _report(["Wonder Woman"], lib)["ambiguous"]
    assert [(o["kind"], o["year"]) for o in a["options"]] == [("movie", 2017), ("show", 1975)]
    assert a["options"][a["current"]]["kind"] == "show"


def test_a_typed_ref_without_numbers_is_flagged_only_when_its_own_kind_is_still_ambiguous(lib):
    assert len(_report([{"movie": "Aladdin"}], lib)["ambiguous"]) == 1   # two Aladdin movies
    assert "ambiguous" not in _report([{"movie": "Wonder Woman"}], lib)  # one movie: the pin already settles it


def test_a_unique_title_a_pinned_entry_and_a_collection_are_never_flagged(lib):
    pinned = {"movie": "Aladdin", "year": 1992, "ids": {"tmdb": "812"}}
    assert "ambiguous" not in _report(["2 Guns", "Cheers", pinned], lib)


def test_the_same_title_twice_is_flagged_once(lib):
    assert len(_report(["Aladdin", "aladdin", "  Aladdin "], lib)["ambiguous"]) == 1


def test_case_differences_still_find_it(lib):
    (a,) = _report(["ALADDIN"], lib)["ambiguous"]
    assert a["label"] == "ALADDIN" and len(a["options"]) == 2


def test_without_an_id_index_nothing_is_flagged_and_nothing_breaks(lib):
    report = {}
    channel_engine.resolve_content(["Aladdin"], lib[0], lib[1], report=report)
    assert "ambiguous" not in report


def test_collection_members_are_never_flagged_because_plex_ids_already_settle_them(lib, monkeypatch):
    from test_collection_members import _member, _plex
    _plex(monkeypatch, [_member("Aladdin", "movie", 502, 2019)])
    report = _report([{"collection": "3 Stars and Up"}], lib, plex_url="http://p", plex_token="t",
                     plex_sections=[{"key": "1"}], collection_cache={})
    assert "ambiguous" not in report


def test_flagging_never_changes_what_plays(lib):
    content = ["Aladdin", "Wonder Woman", "2 Guns", {"movie": "Aladdin"}]
    with_report, _ = channel_engine.resolve_content(content, lib[0], lib[1], id_index=lib[2], report={})
    without, _ = channel_engine.resolve_content(content, lib[0], lib[1], id_index=lib[2])
    assert [(r["type"], r["title"]) for r in with_report] == [(r["type"], r["title"]) for r in without]


def test_the_by_title_view_and_the_old_full_scan_agree(lib):
    with_view = channel_engine.find_by_title("Aladdin", lib[2])
    scan_only = {k: v for k, v in lib[2].items() if k != "by_title"}
    assert [f["year"] for f in channel_engine.find_by_title("Aladdin", scan_only)] == [f["year"] for f in with_view]
