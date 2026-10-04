"""Franchise content-ref machinery — load_franchise_index + match_franchise.

Pure logic against seeded temp caches and fabricated library maps. No network."""

import json
import sys
from pathlib import Path

import channel_engine


# ── fixtures ──────────────────────────────────────────────────────────────────

def _seed_caches(tmp_path, tmdb_collections=None, wikidata_franchises=None):
    """Write minimal cache files in the shapes the Planner scans produce."""
    enrichment = {}
    for coll_id, coll_name, titles in (tmdb_collections or []):
        for t in titles:
            enrichment[t] = {"title": t, "year": 2000,
                             "collection": {"id": coll_id, "name": coll_name},
                             "keywords": []}
    (tmp_path / "tmdb_enrichment.json").write_text(
        json.dumps({"sig": "x", "enrichment": enrichment}))
    (tmp_path / "wikidata_cache.json").write_text(
        json.dumps({"sig": "x", "franchises": wikidata_franchises or []}))
    return tmp_path


def _movie(title, release_ms=None, year=None, pid=None):
    return {"id": pid or f"p-{title.lower().replace(' ', '-')}",
            "program": {"title": title, "releaseDate": release_ms, "year": year}}


def _maps():
    movie_map = {
        "die hard": _movie("Die Hard", 600000000000, 1988),
        "die hard 2": _movie("Die Hard 2", 650000000000, 1990),
        "unrelated": _movie("Unrelated", 1, 1970),
    }
    show_map = {
        "star trek": {"title": "Star Trek", "showId": "st-1",
                      "programs": [{"id": "p-st-1", "program": {"year": 1966}}]},
    }
    return movie_map, show_map


# ── load_franchise_index ──────────────────────────────────────────────────────

def test_index_groups_tmdb_collections(tmp_path):
    _seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard", "Die Hard 2"]),
    ])
    idx = channel_engine.load_franchise_index(tmp_path)
    assert idx["die hard collection"]["name"] == "Die Hard Collection"
    assert sorted(idx["die hard collection"]["titles"]) == ["Die Hard", "Die Hard 2"]


def test_index_includes_wikidata_and_tmdb_wins_collisions(tmp_path):
    _seed_caches(
        tmp_path,
        tmdb_collections=[(1, "Star Trek", ["Star Trek: First Contact"])],
        wikidata_franchises=[
            {"name": "Star Trek", "source": "wikidata",
             "members": [{"title": "Star Trek", "year": 1966, "type": "TV"}]},
            {"name": "Tremors", "source": "wikidata",
             "members": [{"title": "Tremors", "year": 1990, "type": "Movie"}]},
        ])
    idx = channel_engine.load_franchise_index(tmp_path)
    assert idx["star trek"]["titles"] == ["Star Trek: First Contact"]  # TMDB wins
    assert idx["tremors"]["titles"] == ["Tremors"]                     # wikidata-only kept


def test_index_empty_when_caches_absent(tmp_path):
    assert channel_engine.load_franchise_index(tmp_path) == {}


# ── match_franchise ───────────────────────────────────────────────────────────

def test_match_franchise_resolves_members_by_identity(tmp_path):
    idx = channel_engine.load_franchise_index(_seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard", "Die Hard 2", "Not In Library"]),
    ]))
    movie_map, show_map = _maps()
    resolved, preview = channel_engine.match_franchise(
        "Die Hard Collection", idx, movie_map, show_map, order="release_date")
    assert [r["title"] for r in resolved] == ["Die Hard", "Die Hard 2"]
    assert preview == [{"title": "Die Hard", "year": 1988},
                       {"title": "Die Hard 2", "year": 1990}]


def test_match_franchise_is_cross_media(tmp_path):
    idx = channel_engine.load_franchise_index(_seed_caches(
        tmp_path,
        wikidata_franchises=[{"name": "Star Trek", "source": "wikidata", "members": [
            {"title": "Star Trek", "year": 1966, "type": "TV"},
            {"title": "Die Hard", "year": 1988, "type": "Movie"},  # contrived: proves both maps
        ]}]))
    movie_map, show_map = _maps()
    resolved, _ = channel_engine.match_franchise("Star Trek", idx, movie_map, show_map,
                                                 order="release_date")
    types = {r["type"] for r in resolved}
    assert types == {"Movie", "TV"}
    assert resolved[-1]["type"] == "TV"  # shows sort to the end in release order


def test_match_franchise_exclude_and_unknown(tmp_path):
    idx = channel_engine.load_franchise_index(_seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard", "Die Hard 2"]),
    ]))
    movie_map, show_map = _maps()
    resolved, _ = channel_engine.match_franchise(
        "Die Hard Collection", idx, movie_map, show_map, exclude=["die hard 2"])
    assert [r["title"] for r in resolved] == ["Die Hard"]
    assert channel_engine.match_franchise("Nope", idx, movie_map, show_map) == ([], [])
    assert channel_engine.match_franchise("Nope", None, movie_map, show_map) == ([], [])


# ── resolve_content franchise refs ────────────────────────────────────────────

def test_resolve_content_franchise_ref(tmp_path):
    idx = channel_engine.load_franchise_index(_seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard", "Die Hard 2"]),
    ]))
    movie_map, show_map = _maps()
    content = [{"match": "franchise", "name": "Die Hard Collection",
                "order": "release_date", "exclude": []}]
    resolved, missing = channel_engine.resolve_content(
        content, movie_map, show_map, franchise_index=idx)
    assert [r["title"] for r in resolved] == ["Die Hard", "Die Hard 2"]
    assert missing == []


def test_resolve_content_franchise_ref_unknown_is_missing(tmp_path):
    movie_map, show_map = _maps()
    content = [{"match": "franchise", "name": "Nope"}]
    resolved, missing = channel_engine.resolve_content(
        content, movie_map, show_map, franchise_index={})
    assert resolved == []
    assert missing == ["[franchise:Nope]"]


def test_resolve_content_franchise_ref_without_index_is_missing():
    movie_map, show_map = _maps()
    content = [{"match": "franchise", "name": "Die Hard Collection"}]
    resolved, missing = channel_engine.resolve_content(content, movie_map, show_map)
    assert resolved == []
    assert missing == ["[franchise:Die Hard Collection]"]


def test_resolve_content_mixes_franchise_ref_with_plain_titles(tmp_path):
    idx = channel_engine.load_franchise_index(_seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard"]),
    ]))
    movie_map, show_map = _maps()
    content = ["Unrelated", {"match": "franchise", "name": "Die Hard Collection"}]
    resolved, missing = channel_engine.resolve_content(
        content, movie_map, show_map, franchise_index=idx)
    assert sorted(r["title"] for r in resolved) == ["Die Hard", "Unrelated"]


# ── callers build and pass the index ──────────────────────────────────────────

def test_scheduler_cycle_resolves_franchise_refs(tmp_path, monkeypatch):
    """The scheduler's resolve path must hand the franchise index to
    resolve_content — proven by resolving a live franchise channel end-to-end
    through scheduler._run_cycle_blocking with all I/O stubbed."""
    # scheduler is in backend/, which is already on sys.path via conftest
    import scheduler

    _seed_caches(tmp_path, tmdb_collections=[
        (1, "Die Hard Collection", ["Die Hard", "Die Hard 2"]),
    ])
    movie_map, show_map = _maps()

    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [{
        "number": 7, "name": "Die Hard 24/7", "shuffle": "ordered", "live": True,
        "content": [{"match": "franchise", "name": "Die Hard Collection",
                     "order": "release_date", "exclude": []}],
    }]}))

    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids",
                        lambda url: (movie_map, show_map, None))
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid-7", "number": n, "name": "Die Hard 24/7"})
    # Current programming is empty → the fresh ids must register as a change.
    monkeypatch.setattr(channel_engine, "read_channel_programming",
                        lambda url, cid: set())

    summary = scheduler._run_cycle_blocking(apply=False)
    assert summary["error"] is None
    assert summary["changed"] == 1
    assert summary["changes"][0]["number"] == 7
    assert summary["changes"][0]["added_count"] == 2  # both Die Hard movies
