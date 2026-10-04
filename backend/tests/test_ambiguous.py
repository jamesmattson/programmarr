"""test_ambiguous.py — a plain title that names more than one thing gets flagged, not guessed.

Real library: 40 entries across 12 channels ("Beauty and the Beast", "The Lion King", "Aladdin"…)
match two movies. A plain title plays ONE of them, chosen arbitrarily, and never the other.
Nothing is broken — it just isn't a choice anyone made. The engine now says so, describes the
options (each with the entry that would pin it) and which one plays right now; the editor
offers [year] [year] [Both]. Flagging must never change what plays.
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
from routers import channels_router
from test_id_index import _episode, _index, _library, _movie


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


# ── the saved notice ─────────────────────────────────────────────────────────────

def test_a_channel_with_only_ambiguous_titles_still_gets_a_notice():
    n = notices.summarize({"ambiguous": [{"label": "A", "options": []}]})
    assert n["missing_count"] == 0 and n["healed_count"] == 0 and n["ambiguous_count"] == 1


def test_a_long_ambiguous_list_is_capped_but_counted():
    n = notices.summarize({"ambiguous": [{"label": f"T{i}", "options": []} for i in range(120)]})
    assert n["ambiguous_count"] == 120 and len(n["ambiguous"]) == notices.MAX_AMBIGUOUS


# ── GET /channels/{n}/review ─────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(channels_router, "_index_cache", {"at": 0.0, "index": None})
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    _library(monkeypatch, [("mv", "movies"), ("tv", "shows")],
             {"mv": [_movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                     _movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                     _movie("2 Guns", 2013, tmdb="11548", plex="7", tunarr="tid")],
              "tv": [_episode("Cheers", "cheers-uuid", 1982, tmdb="4471", n=1)]})
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    calls = {"builds": 0, "deploys": 0}
    real = channel_engine.build_library_index_with_ids
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids",
                        lambda url: calls.__setitem__("builds", calls["builds"] + 1) or real(url))
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda *a, **k: calls.__setitem__("deploys", calls["deploys"] + 1))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "shuffle": "shuffle",
         "content": ["Aladdin", "2 Guns", "Not A Real Movie", {"movie": "Cheers", "ids": {"tmdb": "nope"}}]}]}))
    return tmp_path, calls


def _review(number=1, **kw):
    return asyncio.run(channels_router.channel_review(number, **kw))


def test_review_lists_skipped_and_ambiguous_titles_together(server):
    r = _review()
    assert r["missing_count"] == 2 and {m["label"] for m in r["missing"]} == {"Not A Real Movie", "Cheers"}
    assert r["ambiguous_count"] == 1 and r["ambiguous"][0]["label"] == "Aladdin"


def test_review_writes_nothing_and_deploys_nothing(server):
    tmp_path, calls = server
    before = (tmp_path / "channels.json").read_text()
    _review()
    assert calls["deploys"] == 0
    assert not (tmp_path / notices.FILE).exists()
    assert (tmp_path / "channels.json").read_text() == before


def test_a_clean_channel_reviews_as_all_zeros(server):
    tmp_path, _ = server
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "content": ["2 Guns", "Cheers"]}]}))
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


def test_apply_records_the_ambiguous_titles_in_the_notice_too(server, monkeypatch):
    tmp_path, _ = server
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "t", "number": n, "name": "Test"})
    result = asyncio.run(channels_router.apply_channel(1))
    assert result["notice"]["ambiguous_count"] == 1
    assert notices.load(tmp_path)["1"]["ambiguous"][0]["label"] == "Aladdin"


# ── GET /channel-reviews — the channel LIST flags them too ───────────────────────────
# The row badge used to come only from the notice saved at the last Apply/auto-update, so a
# channel nobody had applied since (Heroes) showed nothing on the list even though opening it
# listed three ambiguous titles. The list now runs the same live check for every channel.

def _reviews(**kw):
    kw.setdefault("refresh", False)
    return asyncio.run(channels_router.channel_reviews(**kw))


def _two_channels(tmp_path, second=("2 Guns", "Cheers")):
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Needs a look", "content": ["Aladdin", "2 Guns", "Not A Real Movie"]},
        {"number": 4, "name": "Clean", "content": list(second)}]}))


def test_a_channel_nobody_has_applied_is_still_flagged_on_the_list(server):
    tmp_path, _ = server
    _two_channels(tmp_path)
    assert not (tmp_path / notices.FILE).exists()  # no saved notice anywhere — the Heroes situation
    r = _reviews()
    assert r[1]["ambiguous_count"] == 1 and r[1]["ambiguous"][0]["label"] == "Aladdin"
    assert r[1]["missing_count"] == 1 and r[1]["missing"][0]["label"] == "Not A Real Movie"


def test_a_clean_channel_reviews_as_all_zeros_on_the_list(server):
    tmp_path, _ = server
    _two_channels(tmp_path)
    assert _reviews()[4] == {"missing_count": 0, "missing": [], "healed_count": 0,
                             "ambiguous_count": 0, "ambiguous": []}


def test_every_channel_is_reviewed_from_one_library_scan_and_nothing_is_written(server):
    tmp_path, calls = server
    _two_channels(tmp_path)
    before = (tmp_path / "channels.json").read_text()
    assert set(_reviews()) == {1, 4}
    assert calls["builds"] == 1 and calls["deploys"] == 0
    assert not (tmp_path / notices.FILE).exists()
    assert (tmp_path / "channels.json").read_text() == before


def test_one_channel_that_cannot_be_checked_does_not_blank_the_rest(server, monkeypatch):
    tmp_path, _ = server
    _two_channels(tmp_path, second=("BOOM",))
    real = channel_engine.resolve_content
    def flaky(content, *a, **k):
        if "BOOM" in content:
            raise RuntimeError("an odd entry")
        return real(content, *a, **k)
    monkeypatch.setattr(channel_engine, "resolve_content", flaky)
    r = _reviews()
    assert set(r) == {1} and r[1]["ambiguous_count"] == 1


def test_no_channels_is_an_empty_answer_not_an_error(server):
    tmp_path, _ = server
    (tmp_path / "channels.json").write_text(json.dumps({"channels": []}))
    assert _reviews() == {}


def test_the_list_check_needs_tunarr_configured(server):
    tmp_path, _ = server
    _two_channels(tmp_path)
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(HTTPException) as e:
        _reviews()
    assert e.value.status_code == 400


def test_the_list_and_the_editor_agree(server):
    tmp_path, _ = server
    _two_channels(tmp_path)
    assert _reviews()[1] == {k: v for k, v in _review(1).items()}
