"""test_live_update_guard.py — the name-match guard on in-place channel updates.

Regression test for the by-number scramble bug: when channels.json drifts out of
sync with Tunarr (e.g. two Programmarr instances writing one Tunarr, or an orphan
channel shifting numbers), update_channel_in_place must NOT blindly overwrite
whatever channel happens to sit at that number. It looks the channel up by number,
and if the Tunarr channel's name doesn't match the name we intended to patch, it
refuses (raises ChannelEngineError) instead of scrambling the lineup.

See docs/live-channels-design.md (In-Place Update Requirement) and CLAUDE.md
(Live Channels rule 1).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine

# Minimal real resolved-content shape build_schedule accepts (one movie program).
RESOLVED = [{"type": "Movie", "programs": [{"id": "p1"}]}]


@pytest.fixture
def fake_tunarr(monkeypatch):
    """Stub the Tunarr HTTP boundary. Returns a recorder you can configure/inspect.

    - set .channels to the channel list GET /api/channels returns.
    - .programmed records (channel_id, schedule) if set_programming is reached.
    """
    rec = type("Rec", (), {"channels": [], "programmed": None})()

    def fake_api(tunarr_url, method, path, body=None, timeout=60):
        if method == "GET" and path == "/api/channels":
            return rec.channels
        raise AssertionError(f"unexpected api call: {method} {path}")

    def fake_set_programming(tunarr_url, channel_id, schedule_payload):
        rec.programmed = (channel_id, schedule_payload)
        return {"ok": True}

    monkeypatch.setattr(channel_engine, "api", fake_api)
    monkeypatch.setattr(channel_engine, "set_programming", fake_set_programming)
    return rec


def test_refuses_to_patch_when_tunarr_name_differs(fake_tunarr):
    """The scramble case: Tunarr #11 is 'Jackass Marathon' but we meant 'Comedy TV'."""
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Jackass Marathon"}]

    with pytest.raises(channel_engine.ChannelEngineError) as exc:
        channel_engine.update_channel_in_place(
            "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV")

    assert "mismatch" in str(exc.value).lower()
    assert fake_tunarr.programmed is None  # never overwrote the wrong channel


def test_patches_when_name_matches(fake_tunarr):
    """Happy path: Tunarr #11 really is 'Comedy TV' → patch proceeds in place."""
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Comedy TV"}]

    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV")

    assert fake_tunarr.programmed is not None
    assert fake_tunarr.programmed[0] == "abc"  # patched the right Tunarr id


def test_name_match_is_case_and_space_insensitive(fake_tunarr):
    fake_tunarr.channels = [{"number": 5, "id": "x", "name": "  comedy TV "}]

    channel_engine.update_channel_in_place(
        "http://t", 5, "ordered", RESOLVED, expected_name="Comedy TV")

    assert fake_tunarr.programmed is not None


def test_no_expected_name_keeps_legacy_behavior(fake_tunarr):
    """Backward compat: callers that don't pass expected_name patch by number as before."""
    fake_tunarr.channels = [{"number": 7, "id": "y", "name": "Anything"}]

    channel_engine.update_channel_in_place("http://t", 7, "ordered", RESOLVED)

    assert fake_tunarr.programmed is not None


def test_summary_order_is_stable_across_hash_seeds():
    """#45: ties must sort by title, not by the set's (hash-seed) order."""
    import subprocess, sys
    code = ("import sys; sys.path[:0]=['backend','.']; import scheduler; "
            "print(scheduler._summarize({'a','b','c','d','e'}, "
            "{'a':'Zulu','b':'Alpha','c':'Mike','d':'Show','e':'Show'}))")
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**__import__('os').environ, "PYTHONHASHSEED": str(s)}).stdout
            for s in range(6)}
    assert outs == {"['Show x2', 'Alpha', 'Mike', 'Zulu']\n"}
