"""Shared pytest fixtures for the backend smoke suite.

Points the pipeline router at a temp data dir seeded with a synthetic
plex_library.csv, so the deterministic units (facets / compose / validate /
discover-prompt) can be exercised in isolation without a real Plex export.
"""

import csv
import sys
from pathlib import Path

import pytest

# The router imports `scheduler` (backend/) which imports `channel_engine` (repo
# root) — both must be importable the same way main.py wires them.
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for _p in (str(BACKEND), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Exact header export.py writes (see export.py fieldnames). Multi-valued columns
# (Genres / Director / Actors / Country / Mood / Style) are pipe-separated.
CSV_FIELDS = ["Title", "Year", "Type", "Rating", "Genres",
              "Director", "Studio", "Actors", "Seasons", "Episodes",
              "Country", "Mood", "Style"]


def movie(title, year=2000, genres="", studio="", director="", actors="", rating="PG",
          country="", mood="", style=""):
    return {"Title": title, "Year": str(year), "Type": "Movie", "Rating": rating,
            "Genres": genres, "Director": director, "Studio": studio, "Actors": actors,
            "Country": country, "Mood": mood, "Style": style}


def show(title, genres="", seasons=1, episodes=10, studio=""):
    return {"Title": title, "Type": "TV", "Genres": genres,
            "Studio": studio, "Seasons": str(seasons), "Episodes": str(episodes)}


@pytest.fixture
def pr(tmp_path, monkeypatch):
    """The pipeline_router module with DATA_DIR redirected to a temp dir."""
    from routers import pipeline_router as module
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "SCRIPTS_DIR", ROOT)
    monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setenv("PROGRAMMARR_DATA", str(tmp_path))
    module._test_data_dir = tmp_path  # convenience handle for assertions
    return module


@pytest.fixture
def seed(pr):
    """Write a synthetic plex_library.csv into the router's temp DATA_DIR."""
    def _seed(rows):
        path = pr._test_data_dir / "plex_library.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
        return path
    return _seed


# ── Snapshots ──────────────────────────────────────────────────────────────────
# Golden files for the Plex snapshot tests (test_plex_snapshots.py). A mismatch
# fails with a diff; `pytest --update-snapshots` rewrites them. Only do that when a
# change to Plex output is intended — and say so in the PR, since the Plex rule on
# issue #36 is that Plex output does not change.

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def pytest_addoption(parser):
    parser.addoption("--update-snapshots", action="store_true", default=False,
                     help="Rewrite snapshot files instead of comparing against them.")


@pytest.fixture
def snapshot(request):
    import difflib
    import json as _json

    update = request.config.getoption("--update-snapshots")

    def _check(name, value):
        path = SNAPSHOT_DIR / name
        text = value if isinstance(value, str) else (
            _json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        if update or not path.exists():
            if not update:
                pytest.fail(f"snapshot {name} does not exist — run pytest --update-snapshots")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
            return
        saved = path.read_text(encoding="utf-8")
        if saved != text:
            diff = "".join(difflib.unified_diff(
                saved.splitlines(keepends=True), text.splitlines(keepends=True),
                fromfile=f"snapshots/{name} (saved)", tofile="this run", n=2))
            lines = diff.splitlines()
            shown = "\n".join(lines[:120]) + (f"\n... {len(lines) - 120} more diff lines"
                                               if len(lines) > 120 else "")
            pytest.fail(f"snapshot {name} changed:\n{shown}", pytrace=False)

    return _check


# ── Plex snapshot environment ──────────────────────────────────────────────────
# Shared by test_plex_snapshots.py (CLI scripts) and test_plex_snapshots_app.py
# (web routes + scheduler). Every run is deterministic: uuid4 counts up from 1,
# "now" is frozen in create.py and the scheduler, and sleeps are skipped. The uuid4
# and sleep patches are process-wide (`create.time` IS the time module), so for the
# length of a test nothing else sleeps or gets a random uuid either.

def _frozen_datetime():
    from datetime import datetime, timezone

    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz else frozen.replace(tzinfo=None)

    return FrozenDatetime


@pytest.fixture
def deterministic(monkeypatch):
    import itertools
    import uuid

    import channel_engine
    import create
    import scheduler

    counter = itertools.count(1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))
    frozen = _frozen_datetime()
    monkeypatch.setattr(create, "datetime", frozen)
    monkeypatch.setattr(scheduler, "datetime", frozen)
    monkeypatch.setattr(create.time, "sleep", lambda s: None)
    channel_engine.set_tunarr_auth()   # module state — clear anything an earlier test set
    yield
    channel_engine.set_tunarr_auth()


def _write_config(directory, extra=None):
    import json as _json

    from plex_world import PLEX_TOKEN, PLEX_URL, TUNARR_URL
    cfg = {"tunarr_url": TUNARR_URL, "plex_url": PLEX_URL, "plex_token": PLEX_TOKEN}
    cfg.update(extra or {})
    (directory / "config.json").write_text(_json.dumps(cfg))


@pytest.fixture
def run(tmp_path, monkeypatch, deterministic):
    """Run a CLI script's main() in a temp cwd, against a plex_world.World."""
    import urllib.request

    def _run(module, argv, world, cwd=None, config=None):
        cwd = cwd or tmp_path
        monkeypatch.chdir(cwd)
        _write_config(cwd, config)
        monkeypatch.setattr(urllib.request, "urlopen", world.urlopen)
        monkeypatch.setattr(sys, "argv", [f"{module.__name__}.py", *argv])
        module.main()
        return cwd

    return _run


@pytest.fixture
def app(pr, monkeypatch, deterministic):
    """The web routers and the live-channel scheduler, pointed at one temp DATA_DIR
    and a plex_world.World (call app.use(world) first).

    The routes launch scripts (create.py) as subprocesses through
    pipeline_router._stream, which an in-process fake can't see. Here _stream runs
    the same script's main() in-process instead, so its Tunarr writes reach the
    World too. Like the real subprocess, a script that exits or crashes still ends
    in a done event (a crash with returncode 1 and its traceback as output lines).
    The subprocess plumbing itself is covered by test_stream_failures.py.
    """
    import asyncio
    import contextlib
    import importlib
    import io
    import json as _json
    import traceback
    import types
    import urllib.request

    import scheduler
    from routers import channels_router, status_router

    data = pr._test_data_dir
    for module in (channels_router, status_router, scheduler):
        monkeypatch.setattr(module, "DATA_DIR", data)
    monkeypatch.setattr(scheduler, "LOGS_DIR", data / "logs")
    _write_config(data)

    async def in_process_stream(script, args, tag):
        def event(**kw):
            return f"data: {_json.dumps(kw)}\n\n"

        yield event(type="start", cmd=" ".join([script, *args]), log=f"{tag}.log")
        out, code = io.StringIO(), 0
        with monkeypatch.context() as m:
            m.chdir(data)
            m.setattr(sys, "argv", [script, *args])
            with contextlib.redirect_stdout(out):
                try:
                    importlib.import_module(script[:-3]).main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                except Exception:
                    traceback.print_exc(file=out)   # what Python prints when a script dies
                    code = 1
        for line in out.getvalue().splitlines():
            yield event(type="line", text=line)
        yield event(type="done", returncode=code, log=f"{tag}.log")

    monkeypatch.setattr(pr, "_stream", in_process_stream)

    def use(world):
        monkeypatch.setattr(urllib.request, "urlopen", world.urlopen)
        return world

    def write(name, obj):
        (data / name).write_text(_json.dumps(obj, indent=2), encoding="utf-8")

    def read(name):
        p = data / name
        return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def drain(response):
        """Run an SSE route to the end; return its events."""
        async def _all():
            return [chunk async for chunk in response.body_iterator]
        chunks = asyncio.run(_all())
        return [_json.loads(c[6:]) for c in chunks if c.startswith("data: ")]

    return types.SimpleNamespace(pr=pr, data=data, use=use, write=write, read=read,
                                 drain=drain, config=lambda extra: _write_config(data, extra))
