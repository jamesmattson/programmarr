"""test_plex_snapshots_app.py — the web app's Plex paths, pinned like the CLI ones.

test_plex_snapshots.py pins the CLI scripts. A Plex user mostly goes through the
web app, and its routes have logic of their own around those scripts: which
channels a deploy picks, what channels.json and planner_state.json end up holding,
in-place updates that must never recreate a channel. This file runs those routes
and the live-channel scheduler against the same fake Plex + Tunarr (plex_world.py)
and pins their output:

  POST /pipeline/deploy-selective  (Nuke)     -> Tunarr writes, channels.json, planner_state
  POST /pipeline/surgical-deploy   (Add/Edit) -> the same
  POST /channels/{n}/apply         (Save and Apply) -> Tunarr writes, healed ids
  scheduler cycle                  (live channels)  -> Tunarr writes, summary, recipe_state
  Planner reads from Plex: library picker, collections list + poster, connection test

Same rules as the CLI file: output and Plex request counts are pinned, Tunarr reads
and read order are not. `pytest --update-snapshots` only for an intended Plex change.
"""

import asyncio

import pytest
from fastapi import HTTPException

from plex_world import (PLEX2_URL, PLEX_TOKEN, PLEX_URL, TUNARR_URL, World,
                        programming_payload)
from test_plex_snapshots import (EXISTING, TUNARR_AUTH, TUNARR_AUTH_CONFIG, TWO_SERVERS,
                                 deploy_channels)

# Auth-on variants compare with the same snapshots: Tunarr auth must change nothing.
with_and_without_auth = pytest.mark.parametrize("auth", [False, True],
                                                ids=["no_auth", "with_tunarr_auth"])


def _tunarr_ids(world, *titles):
    """Tunarr program ids on the primary server for these movie titles / show titles."""
    srv = world.servers[0]
    ids = []
    for t in titles:
        ids += [p["id"] for p in srv.tunarr_movies if p["program"]["title"] == t]
        ids += [p["id"] for p in srv.tunarr_episodes if p["program"]["show"]["title"] == t]
    assert ids, titles
    return ids


def _done(events):
    """The return code of the stream's final done event. A stream must end in one."""
    assert events and events[-1]["type"] == "done", "an SSE stream must end in a done event"
    return events[-1]["returncode"]


# ── Nuke: deploy-selective ─────────────────────────────────────────────────────

def test_nuke_deploy_from_the_planner(app, snapshot):
    """The Deploy step's selection list: one channel left out, one renumbered."""
    world = app.use(World(existing_channels=EXISTING))
    draft = {**deploy_channels(world), "orphaned": [], "suggested_channels": []}
    app.write("channels.draft.json", draft)
    app.write("planner_state.json", {"activeGenres": ["Comedy"], "managed_names": ["Old"]})
    selections = [
        app.pr.DeploySelection(original_number=ch["number"],
                               deploy_number=120 if ch["number"] == 101 else ch["number"],
                               include=ch["number"] != 95)
        for ch in draft["channels"]
    ]
    events = app.drain(asyncio.run(app.pr.run_deploy_selective(
        app.pr.DeployRequest(selections=selections))))

    assert _done(events) == 0
    assert app.read("channels.draft.json") is None and app.read("deploy_temp.json") is None
    snapshot("plex/app/nuke/requests.json", world.outputs())
    snapshot("plex/app/nuke/channels.json", app.read("channels.json"))
    snapshot("plex/app/nuke/planner_state.json", app.read("planner_state.json"))


# ── Add/Edit: surgical-deploy ──────────────────────────────────────────────────

@with_and_without_auth
def test_surgical_deploy_from_the_planner(app, snapshot, auth):
    """One channel per bucket: update in place, unchanged, create (a collection and a
    live title match), delete (planner-managed, removed), and a foreign channel the
    planner never built, which must be kept untouched. An orphan in Tunarr (not in
    channels.json) is never touched either."""
    if auth:
        app.config(TUNARR_AUTH_CONFIG)
    world = app.use(World(tunarr_auth=TUNARR_AUTH if auth else None, existing_channels=[
        {"id": "t-cheers", "number": 11, "name": "Cheers 24/7"},
        {"id": "t-frasier", "number": 12, "name": "Frasier 24/7"},
        {"id": "t-gone", "number": 13, "name": "Gone Channel"},
        {"id": "t-hand", "number": 40, "name": "Hand Made"},
        {"id": "t-orphan", "number": 60, "name": "Orphan"},
    ]))
    app.write("channels.json", {"channels": [
        {"number": 11, "name": "Cheers 24/7", "shuffle": "ordered", "content": ["Cheers"]},
        {"number": 12, "name": "Frasier 24/7", "shuffle": "ordered", "content": ["Frasier"]},
        {"number": 13, "name": "Gone Channel", "shuffle": "ordered", "content": ["Friends"]},
        {"number": 40, "name": "Hand Made", "shuffle": "shuffle", "content": ["Die Hard"]},
    ], "orphaned": [], "suggested_channels": []})
    app.write("planner_state.json", {
        "activeGenres": ["Comedy"],
        "managed_names": ["Cheers 24/7", "Frasier 24/7", "Gone Channel"]})
    app.write("channels.draft.json", {"channels": [
        {"number": 50, "name": "Cheers 24/7", "shuffle": "ordered",
         "content": ["Cheers", "Frasier"]},                                   # update
        {"number": 51, "name": "Frasier 24/7", "shuffle": "ordered",
         "content": ["Frasier"]},                                             # unchanged
        {"number": 52, "name": "Action Classics", "shuffle": "shuffle",
         "content": [{"collection": "Snapshot Action Classics"}]},            # create
        {"number": 53, "name": "Mad Max Saga", "shuffle": "ordered", "live": True,
         "content": [{"match": "title_contains", "value": "Mad Max",
                      "order": "release_date", "exclude": []}]},              # create
    ], "orphaned": [], "suggested_channels": []})

    events = app.drain(asyncio.run(app.pr.run_surgical_deploy()))

    assert _done(events) == 0
    # Today the stream also forwards create.py's own done event mid-stream (log
    # "surgical_create.log") before its final one. Pinned as-is: not this PR's to change.
    assert [e["log"] for e in events if e["type"] == "done"] == ["surgical_create.log", "surgical"]
    assert app.read("channels.draft.json") is None
    touched = {w["url"] for w in world.writes()}
    assert not any("t-orphan" in u or "t-hand" in u or "t-frasier" in u for u in touched)
    snapshot("plex/app/surgical/requests.json", world.outputs())
    snapshot("plex/app/surgical/channels.json", app.read("channels.json"))
    snapshot("plex/app/surgical/planner_state.json", app.read("planner_state.json"))


# ── Save and Apply ─────────────────────────────────────────────────────────────

def _apply(app, number):
    from routers import channels_router
    return asyncio.run(channels_router.apply_channel(number))


@with_and_without_auth
def test_save_and_apply(app, snapshot, auth):
    """A collection channel and a saved entry whose tmdb id went stale: Apply patches
    both in place, and writes the healed ids back into channels.json."""
    if auth:
        app.config(TUNARR_AUTH_CONFIG)
    world = app.use(World(tunarr_auth=TUNARR_AUTH if auth else None, existing_channels=[
        {"id": "t-action", "number": 93, "name": "Action Classics"},
        {"id": "t-pinned", "number": 100, "name": "Pinned Die Hard"},
    ]))
    rk = next(m for m in world.plex_movies if m["title"] == "Die Hard")["ratingKey"]
    app.write("channels.json", {"channels": [
        {"number": 93, "name": "Action Classics", "shuffle": "shuffle",
         "content": [{"collection": "Snapshot Action Classics"}]},
        {"number": 100, "name": "Pinned Die Hard", "shuffle": "ordered",
         "content": [{"movie": "Die Hard", "year": 1988,
                      "ids": {"tmdb": "999999999", "plex": f"src-plex:{rk}"}}]},
    ]})
    results = [_apply(app, 93), _apply(app, 100)]
    snapshot("plex/app/apply/results.json", results)
    snapshot("plex/app/apply/requests.json", world.outputs())
    snapshot("plex/app/apply/channels.json", app.read("channels.json"))


def test_save_and_apply_refuses_a_renamed_channel(app):
    """channels.json out of sync with Tunarr's numbering: never overwrite the wrong
    channel."""
    world = app.use(World(existing_channels=[
        {"id": "t-other", "number": 12, "name": "Someone Else's Channel"}]))
    app.write("channels.json", {"channels": [
        {"number": 12, "name": "Frasier 24/7", "shuffle": "ordered", "content": ["Frasier"]}]})
    with pytest.raises(HTTPException) as e:
        _apply(app, 12)
    assert e.value.status_code == 409
    assert world.writes() == []


# ── Live channels: the scheduler cycle ─────────────────────────────────────────

@with_and_without_auth
def test_live_channel_cycle(app, snapshot, auth):
    """One live channel each: gained a title (patched), unchanged (no write), renamed in
    Tunarr (skipped, never overwritten), a collection with nothing on it yet (patched),
    and one missing from Tunarr (skipped). A non-live channel is never looked at. The
    patched ones carry commercials and a playback structure, which every cycle must
    re-apply or they silently revert."""
    import scheduler
    if auth:
        app.config(TUNARR_AUTH_CONFIG)
    world = World(
        tunarr_auth=TUNARR_AUTH if auth else None,
        existing_channels=[
            {"id": "t-madmax", "number": 96, "name": "Mad Max Saga"},
            {"id": "t-cheers", "number": 11, "name": "Cheers 24/7"},
            {"id": "t-frasier", "number": 12, "name": "Renamed In Tunarr"},
            {"id": "t-action", "number": 93, "name": "Action Classics"},
            {"id": "t-static", "number": 30, "name": "Comedy Movies"},
        ])
    world.programming.update({
        "t-madmax": programming_payload(_tunarr_ids(world, "Mad Max")),
        "t-cheers": programming_payload(_tunarr_ids(world, "Cheers")),
    })
    app.use(world)
    app.write("channels.json", {"channels": [
        {"number": 11, "name": "Cheers 24/7", "shuffle": "ordered", "live": True,
         "content": ["Cheers"]},
        {"number": 12, "name": "Frasier 24/7", "shuffle": "ordered", "live": True,
         "content": ["Frasier"]},
        {"number": 14, "name": "Not In Tunarr", "shuffle": "ordered", "live": True,
         "content": ["Friends"]},
        {"number": 30, "name": "Comedy Movies", "shuffle": "shuffle", "content": ["Airplane!"]},
        {"number": 93, "name": "Action Classics", "shuffle": "shuffle", "live": True,
         "content": [{"collection": "Snapshot Action Classics"}],
         "playback": {"structure": "interleaved", "episodes_per_block": 3}},
        {"number": 96, "name": "Mad Max Saga", "shuffle": "ordered", "live": True,
         "content": [{"match": "title_contains", "value": "Mad Max",
                      "order": "release_date", "exclude": []}],
         "commercials": {"filler_list_id": "filler-90s", "filler_list_name": "90s Ads",
                         "pad_minutes": 3}},
    ]})

    summary = scheduler._run_cycle_blocking(apply=True)

    assert summary["error"] is None
    assert not [w for w in world.writes() if w["method"] == "DELETE"]
    assert not any(w["url"].endswith("/api/channels") for w in world.writes()), \
        "a live update must never create a channel"
    # scheduler._summarize counts titles from a set of ids, so titles with the same
    # count come out in hash order, which changes from one Python process to the next.
    # Sort them so the snapshot is stable; the counts and titles are still pinned.
    # (jamesmattson/programmarr#45 — once fixed in the app, this sort can go.)
    for change in summary["changes"]:
        change["added"] = sorted(change["added"])
    snapshot("plex/app/live/summary.json", summary)
    snapshot("plex/app/live/requests.json", world.outputs())
    snapshot("plex/app/live/recipe_state.json", app.read("recipe_state.json"))


# ── Planner reads from Plex ────────────────────────────────────────────────────

def _planner_reads(app):
    from routers import status_router
    pr = app.pr
    poster = pr.collection_poster("9001")
    with pytest.raises(HTTPException) as no_poster:
        pr.collection_poster("9002")
    connection = status_router.test_connection(status_router.ConnectionTest(
        tunarr_url=TUNARR_URL, plex_url=PLEX_URL, plex_token=PLEX_TOKEN))
    bad_token = status_router.test_connection(status_router.ConnectionTest(
        plex_url=PLEX_URL, plex_token="wrong-token"))
    return {
        "libraries": pr.list_libraries(),
        "collections": pr.list_collections(),
        "poster": {"media_type": poster.media_type, "body": poster.body.decode()},
        "poster_missing": no_poster.value.status_code,
        "test_connection": connection,
        "test_connection_bad_token": bad_token,
    }


def test_planner_reads_from_plex(app, snapshot):
    world = app.use(World())
    results = _planner_reads(app)
    apply = app.pr.apply_collections([
        app.pr.CollectionSelection(name="Snapshot Action Classics", channel_number=80,
                                   include=True),
        app.pr.CollectionSelection(name="Snapshot Partly Synced", channel_number=81,
                                   include=False),
    ])
    snapshot("plex/app/planner/results.json", {**results, "apply_collections": apply})
    snapshot("plex/app/planner/channels.draft.json", app.read("channels.draft.json"))
    snapshot("plex/app/planner/requests.json", world.outputs())


def test_library_picker_with_two_plex_servers(app, snapshot):
    """The first server's sections come from Plex; the second shows up through Tunarr."""
    world = app.use(World(second_server=True))
    app.config(TWO_SERVERS)
    snapshot("plex/app/two_servers/libraries.json", app.pr.list_libraries())
    snapshot("plex/app/two_servers/requests.json", world.outputs())
    assert not any(r["url"].startswith(PLEX2_URL) for r in world.requests), \
        "the second server is listed through Tunarr, never called directly"
