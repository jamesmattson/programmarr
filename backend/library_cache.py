"""A short-lived, in-process cache of the Tunarr library index.

The channel editor's Add box asks "which items have this title?" on every add, and opening a
channel runs a review against the library. Each would otherwise scan every Tunarr library, so
the last build_library_index result (by-title maps + id index) is kept for a few minutes.

Locking: the lock is held only to READ the cache, to register or join a scan, and to STORE a
result — never during the scan itself, so a slow scan never blocks another request that can be
served from the cache.

One scan at a time (single-flight): a full scan is heavy on Tunarr, so a miss while a scan is
already running waits for that scan's result instead of starting its own. The running scan is
registered under the lock as a Future; the waiters block on it outside the lock. If the scan
raises, every waiter gets the same exception, nothing is stored, and the marker is cleared so
the next call scans again. refresh=True joins a scan that is already running (its data is
fresh) rather than starting a second one.

Clearing: invalidate() empties the cache and bumps a generation counter. A scan that was
already running when invalidate() was called stores its result only if the generation is
unchanged, so a clear is never undone by data read before it. Callers that were already
waiting on that scan still get its result (it was just read); a caller that arrives AFTER the
clear waits for that scan to finish and then starts a new one — never two scans at once, and
never data from before the clear. It is called after anything that changes what is deployed
(Apply, the surgical deploy, a create.py deploy, a scheduler cycle that applied changes).

Lives in its own module so the scheduler and the deploy paths can clear it without importing
a router.
"""

import os
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

SCRIPTS_DIR = Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import channel_engine  # noqa: E402

TTL_SECONDS = 300

_lock = threading.Lock()
_state = {"at": 0.0, "value": None, "generation": 0, "inflight": None}


def get(tunarr_url, refresh=False):
    """The cached build_library_index result for `tunarr_url`, scanning Tunarr when the
    cache is empty, older than TTL_SECONDS, or `refresh` is set. If a scan is already
    running, waits for it instead of starting another. Raises what build_library_index
    raises (ChannelEngineError) and caches nothing then."""
    while True:
        with _lock:
            value = _state["value"]
            if not refresh and value is not None and time.monotonic() - _state["at"] <= TTL_SECONDS:
                return value
            flight = _state.get("inflight")
            if flight is None:
                flight = Future()
                flight.generation = _state["generation"]
                _state["inflight"] = flight
                break  # this caller runs the scan
            stale = flight.generation != _state["generation"]
        if stale:
            # That scan started before a clear: let it finish (never two scans at once),
            # then go round again and start a fresh one.
            flight.exception()  # blocks until done; its result or error is not ours
            continue
        return flight.result()  # joined a scan in progress: its value, or its exception

    try:
        value = channel_engine.build_library_index(tunarr_url)  # no lock held while scanning
    except BaseException as e:
        with _lock:
            if _state.get("inflight") is flight:
                _state["inflight"] = None
        flight.set_exception(e)
        raise

    with _lock:
        if _state["generation"] == flight.generation:
            _state.update(at=time.monotonic(), value=value)
        if _state.get("inflight") is flight:
            _state["inflight"] = None
    flight.set_result(value)
    return value


def invalidate():
    """Drop the cached index; a scan already in flight will not store its result, and a
    caller arriving after this will not be handed it."""
    with _lock:
        _state["generation"] += 1
        _state.update(at=0.0, value=None)
