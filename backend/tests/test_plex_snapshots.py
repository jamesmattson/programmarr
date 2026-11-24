"""test_plex_snapshots.py — a Plex user's export and deploys, pinned byte for byte.

Issue #36 (Jellyfin/Emby support) has one hard rule: Plex output must not change.
These tests run the real CLI scripts against a fake Plex + Plex-backed Tunarr
(plex_world.py) and compare their OUTPUT with saved files under snapshots/plex/:

  export.py                    -> plex_library.csv, export_summary.json
  (that CSV) -> Planner        -> library_facets()
  create.py                    -> every write sent to Tunarr + the pre-delete backup
  sync_plex.py                 -> the Plex DVR update it sends
  generate_from_collections.py -> the channels.json it writes

and the same for a setup with two Plex servers. The web routes and the live-channel
scheduler are pinned the same way in test_plex_snapshots_app.py.

Each run also pins how many times each Plex request is made (`plex_requests`), so a
change can't quietly add Plex calls or skip a section it used to read. Tunarr reads
and the ORDER of any read are not pinned: moving Plex code behind a new interface may
legitimately change how it fetches.

If one of these fails, either the change altered Plex output (fix the change) or
the change is meant to (rerun with `pytest --update-snapshots` and call it out in
the PR). Snapshots are derived from demo/plex_library.csv, so regenerating the demo
dataset also means updating them.
"""

import gzip
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import plex_world
from plex_world import PLEX2_SOURCE_ID, PLEX2_TOKEN, PLEX2_URL, PLEX_SOURCE_ID, PLEX_TOKEN, \
    PLEX_URL, ROOT, World


def _read(path):
    return path.read_text(encoding="utf-8")


# Tunarr's optional basic auth (#1865). The `*_with_tunarr_auth` tests rerun a
# scenario against a Tunarr that demands it and compare with the SAME snapshots:
# turning auth on must change nothing a Plex user gets.
TUNARR_AUTH = ("snapshot-user", "snapshot-pass")
TUNARR_AUTH_CONFIG = {"tunarr_username": TUNARR_AUTH[0], "tunarr_password": TUNARR_AUTH[1]}


def test_fake_tunarr_rejects_a_request_without_auth():
    """Guards the guard: the auth-on tests only mean something if the fake refuses."""
    world = World(tunarr_auth=TUNARR_AUTH)
    req = urllib.request.Request(f"{plex_world.TUNARR_URL}/api/channels")
    with pytest.raises(urllib.error.HTTPError) as e:
        world.urlopen(req)
    assert e.value.code == 401


# ── Export ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("auth", [
    pytest.param(False, id="no_auth"),
    pytest.param(True, id="with_tunarr_auth"),
])
def test_export_output_is_unchanged(run, snapshot, auth):
    import export
    world = World(tunarr_auth=TUNARR_AUTH if auth else None)
    out = run(export, [], world, config=TUNARR_AUTH_CONFIG if auth else None)
    # Snapshots compare text with newlines normalized (so a Windows autocrlf checkout
    # can't break them) — so pin the CSV's own line endings separately: csv's \r\n.
    raw = (out / "plex_library.csv").read_bytes()
    assert raw.endswith(b"\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    snapshot("plex/export/plex_library.csv", _read(out / "plex_library.csv"))
    snapshot("plex/export/export_summary.json", json.loads(_read(out / "export_summary.json")))
    snapshot("plex/export/requests.json", world.outputs())


def test_exported_csv_gives_the_same_planner_facets(run, pr, snapshot):
    """The exported CSV is what the Planner builds every candidate from, so pin the
    facets it yields (genres, decades, entities, networks, country/mood/style...)."""
    import export
    run(export, [], World(), cwd=pr._test_data_dir)
    snapshot("plex/planner/library_facets.json", pr.library_facets())


def test_export_without_crossref_keeps_unsynced_titles(run, snapshot):
    """--no-crossref keeps Plex titles Tunarr hasn't synced."""
    import export
    out = run(export, ["--no-crossref"], World())
    snapshot("plex/export/plex_library.no_crossref.csv", _read(out / "plex_library.csv"))


# ── Deploy ─────────────────────────────────────────────────────────────────────

# Hand-built channels already in Tunarr before the deploy: a full deploy wipes them,
# after writing the gzipped backup.
EXISTING = [{"id": "old-hand-built", "number": 5, "name": "Hand Built"},
            {"id": "old-simpsons", "number": 10, "name": "Old Simpsons"}]


def deploy_channels(world):
    """The demo lineup plus one channel per kind of content ref a Plex user can have."""
    demo = json.loads(_read(ROOT / "demo" / "channels.json"))["channels"]
    die_hard = next(m for m in world.plex_movies if m["title"] == "Die Hard")
    rk = die_hard["ratingKey"]
    extra = [
        {"number": 90, "name": "Fargo (movie)", "shuffle": "ordered",
         "content": [{"movie": "Fargo"}]},
        {"number": 91, "name": "Fargo (show)", "shuffle": "ordered",
         "content": [{"show": "Fargo"}]},
        {"number": 92, "name": "Fargo (plain)", "shuffle": "ordered", "content": ["Fargo"]},
        {"number": 93, "name": "Action Classics", "shuffle": "shuffle",
         "content": [{"collection": "Snapshot Action Classics"}]},
        {"number": 94, "name": "Partly Synced", "shuffle": "shuffle",
         "content": [{"collection": "Snapshot Partly Synced"}, "Not A Real Title"]},
        {"number": 95, "name": "Missing Collection", "shuffle": "shuffle",
         "content": [{"collection": "No Such Collection"}]},
        {"number": 96, "name": "Mad Max Saga", "shuffle": "ordered",
         "content": [{"match": "title_contains", "value": "Mad Max", "order": "release_date",
                      "exclude": []}]},
        {"number": 97, "name": "Sitcoms With Ads", "shuffle": "shuffle",
         "content": ["Cheers", "Frasier"],
         "commercials": {"filler_list_id": "filler-90s", "filler_list_name": "90s Ads",
                         "pad_minutes": 5}},
        {"number": 98, "name": "Interleaved", "shuffle": "shuffle",
         "content": ["The Simpsons", "Die Hard", "Aliens"],
         "playback": {"structure": "interleaved", "episodes_per_block": 2}},
        {"number": 99, "name": "Timeline", "shuffle": "ordered", "content": ["Cheers", "Die Hard"],
         "playback": {"structure": "timeline"}},
        # Saved ids (#42): a stale tmdb id heals through the Plex id.
        {"number": 100, "name": "Pinned Die Hard", "shuffle": "ordered",
         "content": [{"movie": "Die Hard", "year": 1988,
                      "ids": {"tmdb": "999999999", "plex": f"{PLEX_SOURCE_ID}:{rk}"}}]},
        {"number": 101, "name": "Block", "shuffle": "block", "content": ["Cheers", "Frasier"]},
    ]
    return {"channels": demo + extra}


def _deploy(run, tmp_path, argv=(), auth=False):
    import create
    world = World(existing_channels=EXISTING, tunarr_auth=TUNARR_AUTH if auth else None)
    (tmp_path / "channels.json").write_text(json.dumps(deploy_channels(world)))
    run(create, list(argv), world, config=TUNARR_AUTH_CONFIG if auth else None)
    return world


@pytest.mark.parametrize("auth", [False, True], ids=["no_auth", "with_tunarr_auth"])
def test_deploy_writes_and_backup_are_unchanged(run, tmp_path, snapshot, auth):
    world = _deploy(run, tmp_path, auth=auth)
    snapshot("plex/deploy/requests.json", world.outputs())

    backups = sorted(tmp_path.glob("tunarr_backup_*.json.gz"))
    assert [b.name for b in backups] == ["tunarr_backup_20260101T120000Z.json.gz"]
    with gzip.open(backups[0], "rt", encoding="utf-8") as f:
        snapshot("plex/deploy/tunarr_backup.json", json.load(f))


def test_deploy_without_delete_creates_the_same_channels(run, tmp_path, snapshot):
    """The surgical (Add/Edit) path runs create.py --no-delete: nothing is deleted
    and no backup is written, but the channels created are the same."""
    world = _deploy(run, tmp_path, ["--no-delete", "--from", "90"])
    assert not [w for w in world.writes() if w["method"] == "DELETE"]
    assert not list(tmp_path.glob("tunarr_backup_*"))
    snapshot("plex/deploy/requests.no_delete_from_90.json", world.outputs())


def test_deploy_probe_writes_nothing(run, tmp_path):
    world = _deploy(run, tmp_path, ["--probe"])
    assert world.writes() == []
    assert not list(tmp_path.glob("tunarr_backup_*"))


# ── Guide sync ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("auth", [False, True], ids=["no_auth", "with_tunarr_auth"])
def test_sync_plex_maps_the_missing_channels(run, snapshot, auth):
    import sync_plex
    world = World(existing_channels=[{"id": f"c{n}", "number": n, "name": f"Channel {n}"}
                                     for n in (10, 11, 12, 20)],
                  dvr_mapped=["10", "11"], tunarr_auth=TUNARR_AUTH if auth else None)
    run(sync_plex, [], world, config=TUNARR_AUTH_CONFIG if auth else None)
    snapshot("plex/sync/requests.json", world.outputs())
    assert sorted(world.dvr_mapped, key=int) == ["10", "11", "12", "20"]


def test_sync_plex_probe_writes_nothing(run):
    import sync_plex
    world = World(existing_channels=[{"id": "c12", "number": 12, "name": "Channel 12"}],
                  dvr_mapped=["10"])
    run(sync_plex, ["--probe"], world)
    assert world.writes() == []


# ── Collections ────────────────────────────────────────────────────────────────

def test_collection_channels_are_unchanged(run, tmp_path, snapshot):
    import generate_from_collections
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 10, "name": "Kept", "shuffle": "ordered", "content": ["Cheers"]},
        {"number": 85, "name": "Old Collection Channel", "shuffle": "shuffle",
         "content": [{"collection": "Gone"}]},
    ]}))
    world = World()
    run(generate_from_collections, ["--apply"], world)
    snapshot("plex/collections/channels.json", json.loads(_read(tmp_path / "channels.json")))
    snapshot("plex/collections/requests.json", world.outputs())


# ── Two Plex servers ───────────────────────────────────────────────────────────
# The second server's Plex ids collide with the first's, it repeats "Die Hard", and
# its "Cheers" has more episodes than the first's (see plex_world.SECOND_SERVER).

TWO_SERVERS = {"plex_servers": [
    {"name": "Plex", "url": PLEX_URL, "token": PLEX_TOKEN},
    {"name": "Plex 2", "url": PLEX2_URL, "token": PLEX2_TOKEN},
]}


def test_export_from_two_plex_servers(run, snapshot):
    """Rows carry their server's name in Source; a title the first server already
    exported is not repeated from the second."""
    import export
    world = World(second_server=True)
    out = run(export, [], world, config=TWO_SERVERS)
    snapshot("plex/two_servers/plex_library.csv", _read(out / "plex_library.csv"))
    snapshot("plex/two_servers/export_summary.json",
             json.loads(_read(out / "export_summary.json")))
    snapshot("plex/two_servers/export_requests.json", world.outputs())


def test_deploy_from_two_plex_servers(run, tmp_path, snapshot):
    """Which server's copy plays: a plain title, a show both servers have (the copy
    with more episodes), and saved Plex ids that are the same number on both
    servers (told apart by the Tunarr source)."""
    import create
    world = World(second_server=True)
    rk1 = next(m for m in world.plex_movies if m["title"] == "Die Hard")["ratingKey"]
    rk2 = next(m for m in world.servers[1].movies if m["title"] == "Die Hard")["ratingKey"]
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "Cheers 24/7", "shuffle": "ordered", "content": ["Cheers"]},
        {"number": 2, "name": "Die Hard (plain)", "shuffle": "ordered", "content": ["Die Hard"]},
        {"number": 3, "name": "Die Hard (server 1)", "shuffle": "ordered",
         "content": [{"movie": "Die Hard", "year": 1988,
                      "ids": {"plex": f"{PLEX_SOURCE_ID}:{rk1}"}}]},
        {"number": 4, "name": "Die Hard (server 2)", "shuffle": "ordered",
         "content": [{"movie": "Die Hard", "year": 1988,
                      "ids": {"plex": f"{PLEX2_SOURCE_ID}:{rk2}"}}]},
        {"number": 5, "name": "Second Server", "shuffle": "shuffle",
         "content": ["Second Server Movie", "Second Server Show"]},
        {"number": 6, "name": "Action Classics", "shuffle": "shuffle",
         "content": [{"collection": "Snapshot Action Classics"}]},
    ]}))
    run(create, [], world, config=TWO_SERVERS)
    snapshot("plex/two_servers/deploy_requests.json", world.outputs())


# ── Housekeeping ───────────────────────────────────────────────────────────────

def test_every_snapshot_file_is_still_used():
    """--update-snapshots only writes the files a run touches, so a renamed test or
    snapshot path leaves its old file behind, never compared again but still looking
    like pinned output. Every file under snapshots/plex/ must be named by one of the
    snapshot tests; delete any this lists."""
    here = Path(__file__).resolve().parent
    SNAPSHOT_DIR = here / "snapshots"
    sources = "".join(p.read_text(encoding="utf-8") for p in here.glob("test_plex_snapshots*.py"))
    stale = [p.relative_to(SNAPSHOT_DIR).as_posix()
             for p in sorted((SNAPSHOT_DIR / "plex").rglob("*")) if p.is_file()
             and f'"{p.relative_to(SNAPSHOT_DIR).as_posix()}"' not in sources]
    assert stale == [], f"snapshot files no test uses any more: {stale}"
