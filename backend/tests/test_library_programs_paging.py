"""test_library_programs_paging.py — Tunarr 2026.11 pages /media-libraries/{id}/programs (#43).

Tunarr #2423 changed the endpoint from a bare list to {total, result, size, offset}
(default limit 100). Iterating the dict crashed build_library_index; reading only the
first page would silently deploy channels with 100 items. Both shapes must work, and
a failed page must fail the whole library rather than return a partial one.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine


def _movie(i):
    return {"type": "content", "id": f"id-{i}", "program": {"title": f"Movie {i}", "state": "ok"}}


def _paged_api(items, calls, fail_offset=None):
    def fake_api(url, method, path, body=None, timeout=60):
        calls.append(path)
        offset = int(path.split("offset=")[1].split("&")[0]) if "offset=" in path else 0
        if offset == fail_offset:
            return None
        limit = 1000 if "limit=" in path else 100   # Tunarr's default page is 100
        page = items[offset:offset + limit]
        return {"total": len(items), "result": page, "size": len(page), "offset": offset}
    return fake_api


def test_old_bare_list_returned_as_is(monkeypatch):
    items = [_movie(i) for i in range(3)]
    monkeypatch.setattr(channel_engine, "api", lambda *a, **k: items)
    assert channel_engine.get_library_programs("http://t", "lib") == items


def test_paged_result_fetches_every_page(monkeypatch):
    items = [_movie(i) for i in range(2500)]
    calls = []
    monkeypatch.setattr(channel_engine, "api", _paged_api(items, calls))
    assert channel_engine.get_library_programs("http://t", "lib") == items
    assert len(calls) == 4   # default 100, then 1000 + 1000 + 400


def test_failed_page_fails_whole_library(monkeypatch):
    items = [_movie(i) for i in range(2500)]
    monkeypatch.setattr(channel_engine, "api", _paged_api(items, [], fail_offset=1100))
    assert channel_engine.get_library_programs("http://t", "lib") is None


def test_build_library_index_on_paged_tunarr(monkeypatch):
    """The exact #43 crash: build_library_index against the new paged shape."""
    items = [_movie(i) for i in range(250)]
    source = {"libraries": [{"id": "mv1", "mediaType": "movies", "enabled": True}]}
    monkeypatch.setattr(channel_engine, "get_plex_sources", lambda url: [source])
    monkeypatch.setattr(channel_engine, "api", _paged_api(items, []))
    movie_map, _ = channel_engine.build_library_index("http://t")
    assert len(movie_map) == 250
