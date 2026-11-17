"""test_library_cache.py — the editor's cached library view.

The scan runs with no lock held, so a request the cache can answer never waits behind a slow
scan. invalidate() bumps a generation, so a scan that started before a clear can't store
stale data afterwards. Everything that changes what is deployed clears the cache.
"""

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
import library_cache
import scheduler
from routers import channels_router
from routers import pipeline_router
from test_id_index import _library, _movie

# Captured at import: another test module rebinds pipeline_router._locked_stream to a fake.
REAL_LOCKED_STREAM = pipeline_router._locked_stream


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(library_cache, "_state", {"at": 0.0, "value": None, "generation": 0})


@pytest.fixture
def scans(monkeypatch):
    """Count library scans; each returns a distinct, recognisable stand-in index."""
    count = {"n": 0}

    def fake_build(url):
        count["n"] += 1
        return channel_engine.LibraryIndex({"scan": count["n"]}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", fake_build)
    return count


# ── locking and the generation counter ──────────────────────────────────────────────

def test_a_warm_cache_answers_while_another_scan_is_blocked(monkeypatch):
    first = channel_engine.LibraryIndex({"which": "first"}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", lambda url: first)
    assert library_cache.get("http://t") is first  # warm

    started, release = threading.Event(), threading.Event()

    def slow_build(url):
        started.set()
        assert release.wait(5), "test never released the blocked scan"
        return channel_engine.LibraryIndex({"which": "second"}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", slow_build)

    refresher = threading.Thread(target=library_cache.get, args=("http://t", True))
    refresher.start()
    try:
        assert started.wait(5)
        # The forced rescan is stuck inside build_library_index. A plain get() must not wait for it.
        answered = []
        reader = threading.Thread(target=lambda: answered.append(library_cache.get("http://t")))
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "get() blocked behind a scan in progress"
        assert answered == [first]
    finally:
        release.set()
        refresher.join(5)
    assert library_cache.get("http://t")[0] == {"which": "second"}


def test_an_invalidate_during_a_scan_leaves_nothing_stored(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_build(url):
        started.set()
        assert release.wait(5)
        return channel_engine.LibraryIndex({"stale": True}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", slow_build)

    result = []
    t = threading.Thread(target=lambda: result.append(library_cache.get("http://t")))
    t.start()
    assert started.wait(5)
    library_cache.invalidate()        # e.g. an Apply finished while the scan was running
    release.set()
    t.join(5)

    assert result[0][0] == {"stale": True}       # the caller still gets its answer...
    assert library_cache._state["value"] is None  # ...but it is not kept


def test_get_reuses_until_refresh_or_expiry(scans, monkeypatch):
    library_cache.get("u"); library_cache.get("u")
    assert scans["n"] == 1
    library_cache.get("u", refresh=True)
    assert scans["n"] == 2
    monkeypatch.setattr(library_cache, "TTL_SECONDS", -1)  # everything is stale
    library_cache.get("u")
    assert scans["n"] == 3


def test_a_failed_scan_caches_nothing(monkeypatch):
    def boom(url): raise channel_engine.ChannelEngineError("down")
    monkeypatch.setattr(channel_engine, "build_library_index", boom)
    with pytest.raises(channel_engine.ChannelEngineError):
        library_cache.get("u")
    assert library_cache._state["value"] is None


# ── every path that changes what is deployed clears it ─────────────────────────────

def _warm(scans):
    library_cache.get("http://tunarr:8000")
    assert scans["n"] == 1


def _rescans_next_time(scans):
    library_cache.get("http://tunarr:8000")
    return scans["n"] == 2


def test_apply_clears_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "shuffle": "shuffle", "content": ["Dune"]}]}))
    _library(monkeypatch, [("mv", "movies")], {"mv": [_movie("Dune", 2021, tmdb="438631", tunarr="d")]})
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "t", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "update_channel_in_place", lambda *a, **k: None)
    count = {"n": 0}
    real = channel_engine.build_library_index
    monkeypatch.setattr(channel_engine, "build_library_index",
                        lambda url: count.__setitem__("n", count["n"] + 1) or real(url))

    _warm(count)
    asyncio.run(channels_router.apply_channel(1))   # Apply builds its own index: n == 2
    library_cache.get("http://tunarr:8000")
    assert count["n"] == 3                           # the cached copy was dropped, so it rescans


def test_a_failed_apply_leaves_the_cache_alone(tmp_path, monkeypatch, scans):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "content": ["Nothing Here"]}]}))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})
    _warm(scans)
    with pytest.raises(Exception):
        asyncio.run(channels_router.apply_channel(1))  # resolves to empty: refused
    gen = library_cache._state["generation"]
    assert gen == 0


def _drain(agen):
    async def run():
        return [chunk async for chunk in agen]
    return asyncio.run(run())


def _fake_stream(monkeypatch, returncode=0):
    async def fake_stream(script, args, tag):
        yield f"data: {json.dumps({'type': 'line', 'text': 'working'})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'returncode': returncode})}\n\n"
    monkeypatch.setattr(pipeline_router, "_stream", fake_stream)


def test_a_create_py_deploy_clears_the_cache(monkeypatch, scans):
    _fake_stream(monkeypatch)
    _warm(scans)
    _drain(REAL_LOCKED_STREAM("create.py", [], "deploy"))
    assert _rescans_next_time(scans)


def test_a_failed_create_py_deploy_still_clears_it(monkeypatch, scans):
    """A failed run may still have deleted or created channels."""
    _fake_stream(monkeypatch, returncode=1)
    _warm(scans)
    _drain(REAL_LOCKED_STREAM("create.py", [], "deploy"))
    assert _rescans_next_time(scans)


def test_a_probe_changes_nothing_so_it_keeps_the_cache(monkeypatch, scans):
    _fake_stream(monkeypatch)
    _warm(scans)
    _drain(REAL_LOCKED_STREAM("create.py", [], "probe"))
    library_cache.get("http://tunarr:8000")
    assert scans["n"] == 1


def test_the_cache_is_cleared_before_the_client_sees_done(monkeypatch, scans):
    _fake_stream(monkeypatch)
    _warm(scans)

    async def run():
        async for chunk in REAL_LOCKED_STREAM("create.py", [], "deploy"):
            if '"done"' in chunk:
                return library_cache._state["value"]
    assert asyncio.run(run()) is None


def test_the_surgical_deploy_clears_the_cache(pr, monkeypatch, scans):
    ch = {"number": 1, "name": "Kept", "shuffle": "shuffle", "content": ["Dune"]}
    for name in ("channels.json", "channels.draft.json"):
        (pr._test_data_dir / name).write_text(json.dumps({"channels": [ch]}))
    (pr._test_data_dir / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    _warm(scans)

    async def run():
        resp = await pr.run_surgical_deploy()
        return [chunk async for chunk in resp.body_iterator]
    asyncio.run(run())
    assert _rescans_next_time(scans)


def _live_cycle(tmp_path, monkeypatch, current_ids):
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Live", "shuffle": "shuffle", "live": True, "content": ["Dune"]}]}))
    movie_map = {"dune": {"id": "d", "program": {"title": "Dune", "state": "ok"}}}
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "t", "number": n, "name": "Live"})
    monkeypatch.setattr(channel_engine, "read_channel_programming", lambda url, cid: set(current_ids))
    monkeypatch.setattr(channel_engine, "update_channel_in_place", lambda *a, **k: None)
    # The scheduler's own scan sees a library holding Dune; the cache keeps the counting stand-in.
    return lambda url: channel_engine.LibraryIndex(movie_map, {}, None)


def test_a_scheduler_cycle_that_applied_changes_clears_the_cache(tmp_path, monkeypatch, scans):
    _warm(scans)
    counting = channel_engine.build_library_index
    build = _live_cycle(tmp_path, monkeypatch, current_ids=[])  # Tunarr has nothing: a change
    monkeypatch.setattr(channel_engine, "build_library_index", build)
    summary = scheduler._run_cycle_blocking(apply=True)
    assert summary["error"] is None and summary["changes"][0]["applied"]
    monkeypatch.setattr(channel_engine, "build_library_index", counting)
    assert _rescans_next_time(scans)


def test_a_dry_run_or_unchanged_cycle_keeps_the_cache(tmp_path, monkeypatch, scans):
    _warm(scans)
    counting = channel_engine.build_library_index
    build = _live_cycle(tmp_path, monkeypatch, current_ids=[])
    monkeypatch.setattr(channel_engine, "build_library_index", build)
    scheduler._run_cycle_blocking(apply=False)                     # dry run: nothing applied
    _live_cycle(tmp_path, monkeypatch, current_ids=["d"])
    scheduler._run_cycle_blocking(apply=True)                      # already up to date
    monkeypatch.setattr(channel_engine, "build_library_index", counting)
    library_cache.get("http://tunarr:8000")
    assert scans["n"] == 1


# ── one scan at a time (single-flight) ──────────────────────────────────────────────

import time  # noqa: E402

def _blocked_scan(monkeypatch, result=None, error=None):
    """A build_library_index that counts calls and blocks until released."""
    state = {"n": 0, "started": threading.Event(), "release": threading.Event()}

    def slow_build(url):
        state["n"] += 1
        state["started"].set()
        assert state["release"].wait(5), "test never released the blocked scan"
        if error is not None:
            raise error
        return result if result is not None else channel_engine.LibraryIndex({"scan": state["n"]}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", slow_build)
    return state


def _call_in_thread(fn, *args):
    out = {}

    def run():
        try:
            out["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 — the test inspects it
            out["error"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, out


def _wait_for_waiter():
    """Give a joining thread time to reach the in-flight scan (the only place it can block)."""
    time.sleep(0.1)


def test_two_concurrent_misses_run_exactly_one_scan(monkeypatch):
    scan = _blocked_scan(monkeypatch)
    first, out1 = _call_in_thread(library_cache.get, "u")
    assert scan["started"].wait(5)
    second, out2 = _call_in_thread(library_cache.get, "u")
    _wait_for_waiter()
    scan["release"].set()
    first.join(5); second.join(5)
    assert scan["n"] == 1
    assert out1["value"] is out2["value"]
    assert out1["value"][0] == {"scan": 1}
    assert library_cache.get("u") is out1["value"]   # and it was stored
    assert library_cache._state["inflight"] is None


def test_a_refresh_joins_a_scan_already_running(monkeypatch):
    scan = _blocked_scan(monkeypatch)
    first, out1 = _call_in_thread(library_cache.get, "u")
    assert scan["started"].wait(5)
    second, out2 = _call_in_thread(library_cache.get, "u", True)
    _wait_for_waiter()
    scan["release"].set()
    first.join(5); second.join(5)
    assert scan["n"] == 1 and out1["value"] is out2["value"]


def test_a_failed_scan_fails_every_waiter_and_the_next_call_scans_again(monkeypatch):
    scan = _blocked_scan(monkeypatch, error=channel_engine.ChannelEngineError("down"))
    first, out1 = _call_in_thread(library_cache.get, "u")
    assert scan["started"].wait(5)
    second, out2 = _call_in_thread(library_cache.get, "u")
    _wait_for_waiter()
    scan["release"].set()
    first.join(5); second.join(5)
    assert scan["n"] == 1
    assert isinstance(out1["error"], channel_engine.ChannelEngineError)
    assert isinstance(out2["error"], channel_engine.ChannelEngineError)
    assert library_cache._state["value"] is None
    assert library_cache._state["inflight"] is None

    ok = channel_engine.LibraryIndex({"back": True}, {}, None)
    monkeypatch.setattr(channel_engine, "build_library_index", lambda url: ok)
    assert library_cache.get("u") is ok   # the marker was cleared: a fresh scan runs


def test_an_invalidate_during_a_shared_scan_stores_nothing(monkeypatch):
    """Callers already waiting get the just-read result; it is not kept."""
    scan = _blocked_scan(monkeypatch)
    first, out1 = _call_in_thread(library_cache.get, "u")
    assert scan["started"].wait(5)
    second, out2 = _call_in_thread(library_cache.get, "u")
    _wait_for_waiter()
    library_cache.invalidate()
    scan["release"].set()
    first.join(5); second.join(5)
    assert scan["n"] == 1
    assert out1["value"] is out2["value"]
    assert library_cache._state["value"] is None
    assert library_cache._state["inflight"] is None


def test_a_caller_after_an_invalidate_waits_then_scans_fresh(monkeypatch):
    """Never handed data read before the clear, and never a second scan alongside the first."""
    scan = _blocked_scan(monkeypatch)
    first, out1 = _call_in_thread(library_cache.get, "u")
    assert scan["started"].wait(5)
    library_cache.invalidate()
    late, out2 = _call_in_thread(library_cache.get, "u")
    _wait_for_waiter()
    assert scan["n"] == 1          # the late caller did not start a parallel scan
    scan["release"].set()          # (the slow build returns at once from now on)
    first.join(5); late.join(5)
    assert scan["n"] == 2
    assert out1["value"][0] == {"scan": 1}
    assert out2["value"][0] == {"scan": 2}
    assert library_cache._state["value"] is out2["value"]


# ── a clear can't be skipped by a disconnect or an error ───────────────────────────

def _cleared():
    return library_cache._state["generation"] > 0


def _close_after_first_chunk(agen):
    async def run():
        first = await agen.__anext__()
        await agen.aclose()          # what a browser disconnect does to the response stream
        return first
    return asyncio.run(run())


def test_a_deploy_stream_closed_before_done_still_clears(monkeypatch):
    _fake_stream(monkeypatch)
    first = _close_after_first_chunk(REAL_LOCKED_STREAM("create.py", [], "deploy"))
    assert '"done"' not in first
    assert _cleared()


def test_a_probe_stream_closed_early_does_not_clear(monkeypatch):
    _fake_stream(monkeypatch)
    _close_after_first_chunk(REAL_LOCKED_STREAM("create.py", [], "probe"))
    assert not _cleared()


def test_a_completed_deploy_clears_exactly_once(monkeypatch):
    _fake_stream(monkeypatch)
    _drain(REAL_LOCKED_STREAM("create.py", [], "deploy"))
    assert library_cache._state["generation"] == 1


def test_a_surgical_deploy_that_raises_mid_way_still_clears(pr, monkeypatch):
    ch = {"number": 1, "name": "Kept", "shuffle": "shuffle", "content": ["Dune"]}
    for name in ("channels.json", "channels.draft.json"):
        (pr._test_data_dir / name).write_text(json.dumps({"channels": [ch]}))
    (pr._test_data_dir / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))

    def boom(data_dir):
        raise RuntimeError("disk went away")
    monkeypatch.setattr(channel_engine, "load_franchise_index", boom)

    async def run():
        resp = await pr.run_surgical_deploy()
        return [chunk async for chunk in resp.body_iterator]
    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert _cleared()


def test_an_apply_that_fails_after_patching_tunarr_still_clears(tmp_path, monkeypatch):
    """Tunarr was patched, then writing the healed ids back failed."""
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "shuffle": "shuffle", "content": [{"movie": "Dune"}]}]}))
    monkeypatch.setattr(channel_engine, "build_library_index",
                        lambda url: channel_engine.LibraryIndex({}, {}, None))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})

    def fake_resolve(content, movie_map, show_map, report=None, **kw):
        report["healed"] = [{"entry": {"movie": "Dune"}, "kind": "movie", "title": "Dune",
                             "ids": {"tmdb": "438631"}, "year": 2021}]
        return [{"type": "Movie", "title": "Dune", "programs": [{"id": "d"}]}], []
    monkeypatch.setattr(channel_engine, "resolve_content", fake_resolve)
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "t", "number": n, "name": "Test"})
    patched = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place", lambda *a, **k: patched.append(a))

    def broken_save(data):
        raise OSError("channels.json is read-only")
    monkeypatch.setattr(channels_router, "save", broken_save)

    with pytest.raises(OSError):
        asyncio.run(channels_router.apply_channel(1))
    assert patched and _cleared()


def test_an_apply_refused_by_tunarr_does_not_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://tunarr:8000"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Test", "shuffle": "shuffle", "content": ["Dune"]}]}))
    _library(monkeypatch, [("mv", "movies")], {"mv": [_movie("Dune", 2021, tmdb="438631", tunarr="d")]})
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "t", "number": n, "name": "Test"})

    def refuse(*a, **k):
        raise channel_engine.ChannelEngineError("name mismatch")
    monkeypatch.setattr(channel_engine, "update_channel_in_place", refuse)
    with pytest.raises(Exception):
        asyncio.run(channels_router.apply_channel(1))
    assert not _cleared()


def test_a_scheduler_cycle_that_patched_one_channel_then_raised_still_clears(tmp_path, monkeypatch):
    build = _live_cycle(tmp_path, monkeypatch, current_ids=[])
    monkeypatch.setattr(channel_engine, "build_library_index", build)
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Live", "shuffle": "shuffle", "live": True, "content": ["Dune"]},
        {"number": 2, "name": "Live", "shuffle": "shuffle", "live": True, "content": ["Dune"]}]}))
    calls = []

    def patch_then_break(url, number, *a, **k):
        calls.append(number)
        if number == 2:
            raise RuntimeError("connection reset")
    monkeypatch.setattr(channel_engine, "update_channel_in_place", patch_then_break)

    with pytest.raises(RuntimeError):
        scheduler._run_cycle_blocking(apply=True)
    assert calls == [1, 2] and _cleared()
