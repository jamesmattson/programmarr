"""test_pinned_entries.py — a saved channel entry that carries the item's own numbers.

{"movie": "Aladdin", "year": 2019, "ids": {...}} finds exactly that Aladdin. A title
alone can't: two movies (or two shows) can share one, and the by-title lookup keeps
only the first. The entry is the developer's own {"movie": ...} form plus year + ids, so
his code, and older versions, still read it as a plain typed ref.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
from test_id_index import _episode, _index, _movie


def _resolve(content, movie_map, show_map, idx):
    return channel_engine.resolve_content(content, movie_map, show_map, id_index=idx)


@pytest.fixture
def lib(monkeypatch):
    """Two Aladdins, two Offices (UK/US), a family video, and Wonder Woman both ways."""
    return _index(
        monkeypatch, [("mv", "movies"), ("fam", "other_videos"), ("tv", "shows")],
        {"mv": [_movie("Aladdin", 1992, tmdb="812", plex="501", tunarr="a92"),
                _movie("Aladdin", 2019, tmdb="420817", plex="502", tunarr="a19"),
                _movie("Wonder Woman", 2017, tmdb="297762", plex="116400", tunarr="ww-movie")],
         "fam": [_movie("Christmas 2019", 2019, plex="900", tunarr="xmas")],
         "tv": [_episode("The Office", "office-uk", 2001, tmdb="2996", plex="30", n=1),
                _episode("The Office", "office-us", 2005, tmdb="2316", plex="31", n=1),
                _episode("The Office", "office-us", 2005, tmdb="2316", plex="31", n=2)] +
               [_episode("Wonder Woman", "ww-show", 1975, tmdb="1924", plex="7000", n=i) for i in range(60)]})


def _entry(kind, title, year, **ids):
    return {kind: title, "year": year, "ids": ids}


# ── finding the exact one ────────────────────────────────────────────────────────

def test_the_right_remake_is_found_not_the_first_one_with_that_title(lib):
    resolved, missing = _resolve([_entry("movie", "Aladdin", 2019, tmdb="420817")], *lib)
    assert not missing and resolved[0]["year"] == 2019


def test_the_other_remake_too(lib):
    resolved, _ = _resolve([_entry("movie", "Aladdin", 1992, tmdb="812")], *lib)
    assert resolved[0]["year"] == 1992


def test_the_office_uk_and_us_are_told_apart(lib):
    uk, _ = _resolve([_entry("show", "The Office", 2001, tmdb="2996")], *lib)
    us, _ = _resolve([_entry("show", "The Office", 2005, tmdb="2316")], *lib)
    assert (uk[0]["showId"], len(uk[0]["programs"])) == ("office-uk", 1)
    assert (us[0]["showId"], len(us[0]["programs"])) == ("office-us", 2)


def test_a_family_video_is_pinned_by_its_own_number(lib):
    resolved, missing = _resolve([_entry("movie", "Christmas 2019", 2019, tunarr="xmas")], *lib)
    assert not missing and resolved[0]["title"] == "Christmas 2019"


def test_movie_and_show_sharing_a_title_stay_apart(lib):
    movie, _ = _resolve([_entry("movie", "Wonder Woman", 2017, tmdb="297762")], *lib)
    show, _ = _resolve([_entry("show", "Wonder Woman", 1975, tmdb="1924")], *lib)
    assert (movie[0]["type"], show[0]["type"]) == ("Movie", "TV")


# ── numbers that went stale ──────────────────────────────────────────────────────

def test_a_stale_main_number_heals_from_a_spare(lib):
    """A re-match changed the TMDB number; Plex's own number still finds it."""
    resolved, missing = _resolve([_entry("movie", "Aladdin", 2019, tmdb="OLD", plex="srcA:502")], *lib)
    assert not missing and resolved[0]["year"] == 2019


def test_every_number_stale_but_title_and_year_name_one_item(lib):
    resolved, missing = _resolve([_entry("movie", "Aladdin", 2019, tmdb="x", plex="x", tunarr="x")], *lib)
    assert not missing and resolved[0]["year"] == 2019


def test_ambiguous_is_reported_never_guessed(lib):
    """No year, every number stale: two Aladdins fit, so it must not pick one."""
    entry = {"movie": "Aladdin", "ids": {"tmdb": "x"}}
    resolved, missing = _resolve([entry], *lib)
    assert resolved == [] and missing == ["Aladdin"]


def test_a_missing_item_never_drops_its_neighbours(lib):
    content = [_entry("movie", "Aladdin", 2019, tmdb="420817"),
               _entry("movie", "Gone For Good", 2001, tmdb="nope"),
               _entry("movie", "Aladdin", 1992, tmdb="812")]
    resolved, missing = _resolve(content, *lib)
    assert [r["year"] for r in resolved] == [2019, 1992] and missing == ["Gone For Good"]


# ── compatibility ────────────────────────────────────────────────────────────────

def test_order_is_kept_among_mixed_entries(lib):
    content = [{"show": "The Office"}, _entry("movie", "Aladdin", 1992, tmdb="812"), "Wonder Woman"]
    resolved, _ = _resolve(content, *lib)
    assert [r["type"] for r in resolved] == ["TV", "Movie", "TV"]  # plain "Wonder Woman": show wins, as before


def test_a_typed_entry_without_numbers_behaves_exactly_as_before(lib):
    resolved, _ = _resolve([{"movie": "Wonder Woman"}], *lib)
    assert resolved[0]["type"] == "Movie"


def test_without_an_id_index_the_title_and_type_are_still_used(lib):
    movie_map, show_map, _ = lib
    resolved, missing = channel_engine.resolve_content(
        [_entry("movie", "Wonder Woman", 2017, tmdb="297762")], movie_map, show_map)
    assert not missing and resolved[0]["type"] == "Movie"


def test_the_extra_year_and_ids_are_harmless_to_the_developers_own_reader(lib):
    """What v0.8.1 does with the same dict: it reads only the kind and the title."""
    entry = _entry("movie", "Wonder Woman", 2017, tmdb="297762")
    kind = "movie" if "movie" in entry else "show"
    assert (kind, entry[kind]) == ("movie", "Wonder Woman")
