# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

> **Keep this lean and current.** Point at code and docs; don't restate them — paraphrased
> code is the #1 source of drift. Behavior changes update this file in the **same commit**.
> History → `CHANGELOG.md`; full designs → `docs/`; exhaustive endpoint/flag reference →
> [`docs/api.md`](docs/api.md) & each script's `--help`. If a section outgrows its job,
> relocate the detail and leave a pointer.

## What This Is

A Python 3 pipeline + web app that exports a Plex library, **composes curated themed
virtual TV channels** in a deterministic Planner (with an optional AI layer on top),
and deploys them to [Tunarr](https://github.com/chrisbenincasa/tunarr). Channels can
be marked **live** to auto-update as the library grows.

The web app's channel-creation experience is a single **Planner** (`Run.tsx`): pick
genres/decades "in play," then check exact curated candidates — per-show marathons,
genre×decade cuts, named sub-genres, studio/director/actor channels, **TV network channels**
(from the `Studio` CSV column for TV rows), **classic programming blocks** (matched from
`programming_blocks.json`), and **franchise channels** (detected from TMDB
`belongs_to_collection` + Wikidata series/franchise membership, on-demand + cached, with per-member checkboxes) — built
deterministically via `/pipeline/compose`. An optional "✨ Bring in AI" layer adds
*discovery* (themed channels filters miss) and *tonal curation* (split a broad pool by
vibe), merged on top.

Two entry points: a **Docker web app** (primary — FastAPI + React on port 7979) and
an interactive **CLI** (`python programmarr.py`, for power users — first-run config
setup, always probes before deploying, offers Plex sync at the end).

> **Audience:** user-facing docs (install, quick start, screenshots) live in
> [`README.md`](README.md). **This file is the developer/agent reference** —
> architecture, conventions, the rules an agent must not break. It describes **what
> exists today**; planned/unbuilt ideas go in [`docs/ideas.md`](docs/ideas.md).

## Web UI Architecture

**Stack:** FastAPI (Python) + React + Mantine v7 — served as a single Docker container on port 7979.

**Directory layout:**
```
backend/          FastAPI app + routers
  main.py         Entry point — auth middleware, SPA fallback, lifespan, scheduler start
  scheduler.py    In-process asyncio loop for live channels (see Live Channels)
  routers/        config / status / channels / pipeline / recipes / logs routers
frontend/         React + Mantine SPA (built to backend/static/)
  src/pages/      Onboarding, Dashboard, Run (the Planner stepper), Channels, Settings, Logs
data/             Bind-mounted volume — config.json, channels.json, plex_library.csv, logs/
```

**Environment variables (Docker):**
- `PROGRAMMARR_DATA` — path where data files live (default: `/data`)
- `PROGRAMMARR_SCRIPTS` — path where Python scripts live (default: `/app`)

**Key design decisions (non-obvious — don't undo these):**
- Pipeline scripts (`export.py`, `create.py`, etc.) run as subprocesses with `cwd=DATA_DIR` so their relative file opens work unmodified.
- SSE (Server-Sent Events) streams subprocess stdout line-by-line to the browser inline terminal. `_stream` **must always end in a `done` event** — the UI spins until it sees one, so a failed subprocess launch, a dead pipe, or an unwritable log each yield `done` (with `returncode: -1` for the first two) rather than killing the generator mid-response.
- Auth middleware reads `config.json` on every request — no restart needed to enable/disable auth.
- Onboarding shows automatically when `config_status.configured` is false (no Tunarr/Plex/token set). It offers **Test connections** (`POST /api/test-connection`), which validates the typed-but-unsaved values — saving alone never contacts Tunarr or Plex, so without this a typo'd URL produced a green "Setup complete". A failed test warns but still lets the user continue ("Continue anyway") — never trap someone behind a probe.
- **Dashboard** shows an EPG guide grid (fetched via `GET /api/guide` → Tunarr XMLTV). Clicking a channel navigates to its editor.
- **Channels page** lists channels from the live Tunarr API (`GET /api/tunarr/channels`); clicking a row fetches the full `channels.json` entry and opens the editor. Channels in Tunarr with no `channels.json` entry show as **"Not managed by Programmarr"** (read-only orphans).
- **Save and Apply** (`POST /api/channels/{number}/apply`) saves a channel edit to `channels.json` and pushes it to Tunarr in place — preserving the Tunarr id and Plex DVR mapping. This is the Channels-page equivalent of the scheduler's per-channel update, but available for any channel (not just live ones).
- `asyncio.WindowsProactorEventLoopPolicy` is set at startup in `main.py` — **required** on Windows for `asyncio.create_subprocess_exec`; no-op on Linux/Docker. (This is the one place it's stated; don't duplicate it.)
- **Deferred (Tier 3):** drag-to-reorder channels, autocomplete from plex_library.csv, inline Plex validation.

## Local Development

Two loops: the **fast loop** for iterating, the **parity loop** (Docker) for the final
check before shipping. Always run the parity loop before a release.

**One-time setup (Linux/WSL — fresh machine):**
```bash
# Python venv (requires python3-venv: sudo apt install python3.14-venv)
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
# Frontend — must reinstall on Linux even if node_modules exists from Windows
# (Windows-built native binaries don't work cross-platform)
cd frontend && npm install && cd ..
```

**Fast loop — hot reload:**
```bash
# Linux/WSL:
./dev.sh           # Vite (:5173) + uvicorn --reload (:7979) in one terminal; Ctrl+C stops both
# Windows:
.\dev.ps1          # opens two PowerShell windows (uses watchfiles; required on Windows)
```
Open **http://localhost:5173** (not 7979). Vite serves the SPA with HMR and proxies `/api`
→ the reload backend. Both read/write the real `./data` files, so behavior matches Docker.

**Parity loop — Docker (run before shipping):**
```powershell
docker build -t programmarr:local .
docker run --rm -p 7979:7979 -v ${PWD}/data:/data programmarr:local   # localhost:7979
```
**Not `docker compose build`** — the committed `docker-compose.yml` is the *user-facing* file
(it pulls `ghcr.io/.../programmarr:latest` and has no `build:` section), so `compose build`
reports "No services to build" and silently tests nothing. It matches what README tells users
to paste; keep it that way. `./data` is mounted as a volume so config/channels/csv persist.
Rebuild to pick up code changes. `backend/static/` is **gitignored** — the Dockerfile builds the frontend
inside the image (`npm run build` during `docker build`); **never commit files under `backend/static/`.**

**Tests:**
```powershell
pip install -r backend/requirements-dev.txt   # one-time (pytest; dev-only, not in the image)
pytest                                         # reads pytest.ini -> backend/tests
```
Each test seeds a temp `DATA_DIR` with a synthetic `plex_library.csv` (the `seed` fixture in
`conftest.py`) — nothing touches a real Plex/Tunarr. Covers `library_facets`, `compose_channels`,
`validate(append=True)`, `discover_prompt`, and `generate_no_ai`.

**Environments:** **localhost:7979** = local Docker before pushing. **TrueNAS** = production,
runs `ghcr.io/jamesmattson/programmarr:latest` with Watchtower (`:latest` moves only when
a GitHub Release is cut — not on a master push; Watchtower picks it up shortly after).

**Demo dataset:** `python scripts/make_demo_data.py` (re)generates the committed `demo/` dir
(synthetic `plex_library.csv` + `channels.json` + safe `config.json`) used for deterministic doc
screenshots. See the script's header for usage and which pages render offline.

## Workflow

```
export.py  ->  LLM (Gemini/Claude/ChatGPT)  ->  create.py
               or
export.py  ->  generate_no_ai.py             ->  create.py
```

Plex collections (managed by Kometa/Trakt/Letterboxd) can become channels directly,
skipping the export/LLM step:

```
generate_from_collections.py --apply  ->  create.py
```

For direct CLI use of any script (flags, dry-run/probe, scoping), see its `--help`.

## Configuration

All config lives in `config.json` (gitignored — in `data/` for Docker, project root for CLI).
See `config.json.example` for the full shape. Keys:

- `tunarr_url`, `plex_url`, `plex_token` — required connection settings.
- `tmdb_api_key` — optional; used by `fetch_images.py` for verified TMDB logo lookups.
  Without it, every channel gets a generated badge instead (icons still work). Free key at
  https://www.themoviedb.org/settings/api
- `auth_username` / `auth_password` — optional HTTP Basic Auth **for Programmarr itself**. **Both blank = auth disabled.** When set, every backend request requires them.
- `tunarr_username` / `tunarr_password` — optional credentials for **Tunarr's own** basic auth (Tunarr #1865). Blank = Tunarr is open. Editable in Settings → Connections; the password masks like other secrets. Every Tunarr call in every module goes through `channel_engine.tunarr_headers()` — **never hand-roll headers for a Tunarr request**, or auth works in some code paths and not others (`test_tunarr_auth.py` guards this).
- `recipes_enabled` (bool, default `false`), `recipe_interval_hours` (number, default `12`) — live-channel scheduler (see Live Channels).
- `tunarr_channel_group` (string, optional) — Tunarr `groupTitle` for all created channels (default `"tunarr"`).
- `tunarr_stream_mode` (string, optional) — Tunarr `streamMode`, lowercase enum: `hls`|`hls_slower`|`mpegts`|`hls_direct`|`hls_direct_v2` (default `"hls"`). Applied by `create.py` at channel creation; **not** exposed in the UI.
- `channel_order` (array, optional) — ordered list of category keys controlling channel numbering, e.g. `["marathon","tv_block","movie","franchise","specialty"]`. Omit for the canonical default order. **Editable in Settings → Channel Numbering.** See Channel Numbering Scheme. (Old `channel_blocks` size key is silently ignored.)

> `config_router.save_config` **merge-writes** `config.json`, so editing these (or the `recipes_*`) keys
> by hand survives a Settings save — the UI form only overwrites the keys it manages. (`channel_order`
> is preserved on an empty save, never wiped — see `save_config`.)

## Architecture

Each script's role and the gotcha worth knowing. Flags and exact behavior live in `--help`
and the code — don't restate them here.

- **`programmarr.py`** — CLI entry point. Flat menu (AI / No-AI / Collections / images / sync / quit). Walks first-run config setup, **always probes before deploying**, and pre-deploy asks whether to wipe-and-rebuild or preserve channels below a number (so manual/lower channels and their custom images survive). Accepts JSONL or bare-array LLM output and normalizes to the internal `{"channels":[...]}` dict.
- **`export.py`** — pulls full metadata from the Plex API. Includes **studio** + top-3 billed **actors** plus **Country / Mood / Style** tags (all from the `/all` response, via `_join_tags`) which power the Planner's entity/country/mood/style channels. Auto-detects all movie+TV sections (or scope with `--movie-sections`/`--tv-sections`); cross-references Tunarr to flag unsynced content. Output: `plex_library.csv` + `export_summary.json`.
- **`generate_no_ai.py`** — builds a starter `channels.json` from CSV metadata (decade + genre movie channels, 50+ episode TV marathons; placeholders for franchise/specialty). Numbers channels sequentially using `channel_blocks.assign_numbers` + `channel_blocks.resolve_order`; `--order KEY,KEY,…` overrides category order; `--start N` sets the first number.
- **`channel_blocks.py`** — shared, **pure, importable** channel-numbering logic (no `config.json`/argv). `assign_numbers(order, counts, start)` packs categories tight sequentially; `resolve_order(configured)` validates/fills the configured order against `CANONICAL_ORDER`. Single source of truth for compose, the LLM prompt, and `generate_no_ai`. **Must stay in the Dockerfile `COPY` line.**
- **`generate_from_collections.py`** — one channel per Plex collection via `{"collection":"Name"}`. Manages the collection block (default ch 80+): keeps everything below `--base`, regenerates from `--base` up. Re-run any time Kometa changes collections.
- **`channel_engine.py`** — shared, **pure, importable** resolution engine (no `config.json`/argv/`sys.exit`), so it's safe to import into the long-lived FastAPI process. Holds the resolution helpers, franchise `match_titles` (word-boundary), and the in-place live-channel updaters (`read_channel_programming`, `update_channel_in_place`). Imported by `create.py` at runtime and in-process by `recipes_router.py` — **must stay in the Dockerfile `COPY` line**. `build_library_index` indexes **all** enabled movie and shows libraries (not just the first — a Plex server can expose several, e.g. `TV Shows` + `Cartoons`), and indexes a show that appears in more than one library **once**, preferring the copy with the most playable (non-`missing`) episodes so a dead duplicate can't shadow the real one or inflate the live-diff into churn. The same scan also builds an **id index** keyed by each item's own ids — TMDB, Plex (namespaced by media source, since two servers can reuse a ratingKey) and Tunarr — so two things sharing a title (the two *Aladdin*s, *The Office* US/UK) never collapse into one. The result still unpacks as `movie_map, show_map`; read the id index with `library_id_index(result)`, which returns `None` (title-only matching) for a plain 2-tuple, e.g. a test double. `resolve_by_ids` finds a saved entry by its ids in priority order, heals from a spare id when the first went stale, falls back to title + year only when that names exactly one item, and otherwise reports `missing`/`ambiguous` — it never guesses. "Other Videos" libraries are not read. Tunarr auth lives here as module state (`set_tunarr_auth` / `set_tunarr_auth_from_config` / `tunarr_headers`) rather than a parameter on ~20 functions — callers pass the values in, so the no-`config.json` rule still holds; **every** caller that loads a config must call `set_tunarr_auth_from_config(cfg)` before hitting Tunarr. `build_library_index` distinguishes a **failed** library fetch from an **empty** one: all libraries failing raises rather than returning an empty index (which would otherwise deploy channels with no content); a partial failure warns and continues. Library reads go through `get_library_programs`, which handles both Tunarr's old bare-list response and the paged `{total, result}` one (Tunarr #2423, 2026.11) and returns `None` rather than a partial library if any page fails — **never call `/media-libraries/{id}/programs` directly**.
- **`create.py`** — thin CLI wrapper around `channel_engine`. Reads `channels.json`, indexes the Tunarr library (case-insensitive exact title match), and deploys (delete-then-create; `--from N` scopes, `--protect N1,N2` preserves specific channels). Builds 30-day rolling random schedules (no dead air). The delete/recreate path is **initial-deploy only** — never for live channels. Before any destructive delete it writes a timestamped **gzipped** `tunarr_backup_*.json.gz` (channel + raw `/programming` payload, last **3** kept) — the only way back from a wipe of a lineup Programmarr didn't create. Probe runs never write one. **Measured on a real 156-channel Tunarr: 151 MB raw, 12 MB gzipped, ~20s** — that measurement is why it's compressed and why retention is 3, not 10; don't raise either without re-measuring.
- **`fetch_images.py`** — sets every channel's Tunarr icon. Verified TMDB logos for
  solo-title/marathon/franchise/network/studio channels (the result's name must match the
  query after normalization — never `results[0]`); generated badge art for every other kind
  and any TMDB miss. Badges upload via Tunarr `POST /api/upload/image`. Channels pinned from
  the Channels editor (`"icon": {"pinned": true}` in channels.json) are skipped; the script
  never writes channels.json. `tmdb_api_key` is optional — without it everything badges.
  Dry-run by default; `--apply` to commit.
- **`icon_engine.py`** — shared, **pure, importable** icon policy + verified TMDB searches +
  Tunarr upload/icon helpers (no `config.json`/argv/`sys.exit`). Imported by `fetch_images.py`
  and in-process by `channels_router.py`. **Must stay in the Dockerfile `COPY` line.**
- **`badge_renderer.py`** — shared, **pure** Pillow badge rendering from committed
  `badge_assets/` (Tabler glyphs MIT, Anton font OFL; regenerate via
  `scripts/make_badge_assets.py`). Badges carry the channel name because Plex hides text
  labels once an icon is set. **Module + `badge_assets/` must stay in the Dockerfile `COPY` lines.**
- **`sync_plex.py`** — reconciles Tunarr's XMLTV channel list into Plex's DVR mapping (read-then-update; **never deletes** the DVR). Falls back to printing the XMLTV URL + manual steps.

## Channel Numbering Scheme

Channels are numbered **sequentially from 1, tight-packed in category order** — no fixed block
sizes, no gaps. 15 marathons → channels 1–15; next category starts at 16. Empty categories
consume no numbers. The only configurable knob is **the order of categories**, stored as
`channel_order` (list of category keys) in `config.json`.

Canonical category order and labels are defined in `channel_blocks.py` (`CANONICAL_ORDER`,
`BLOCK_LABELS`). The full set (in default order):

| Category key | Label | Content |
|---|---|---|
| `marathon` | TV Marathons | 24/7 single-show loops (50+ episodes) |
| `tv_block` | TV Blocks | Themed multi-show rotations |
| `tv_movie_mix` | TV & Movie Mix | Mixed-genre channels spanning shows + films |
| `movie` | Movie Channels | Genre and decade channels |
| `entity` | Studios / Directors / Actors | Curated by creator or studio |
| `network` | Networks | All shows from a single network |
| `programming_block` | Classic TV Blocks | Historical lineups (TGIF, Must See TV…) |
| `franchise` | Franchise & Series | Ordered collections (MCU, Star Wars, etc.) |
| `specialty` | Specialty | Single-movie loops, holiday, niche themes |

**`channel_order` is configurable** via Settings → Channel Numbering (drag up/down) or directly
in `config.json`. An absent or empty `channel_order` key falls back to the canonical order.
Old configs with `channel_blocks` (sizes) are silently ignored — no crash.

**Fresh deploys** start at channel 1; keeping existing channels rounds the start up above the
highest kept one. All three generators (`/pipeline/compose`, the LLM prompt, `generate_no_ai`)
call `channel_blocks.resolve_order` + `channel_blocks.assign_numbers` — single source of truth.

## channels.json Schema

```json
{
  "channels": [
    {
      "number": 10,
      "name": "Channel Name",
      "shuffle": "ordered",
      "content": ["Exact Title From Plex"]
    }
  ],
  "orphaned": [],
  "suggested_channels": []
}
```

**shuffle values:** `ordered` | `shuffle` | `block`

Content items can be plain title strings **or** Plex collection references
(`{"collection": "Name"}`), freely mixed. Collection refs are expanded to member titles at
deploy time via the Plex API; a not-found collection is warned and skipped. Plain titles must
match Plex names exactly (case-insensitive). A title may appear on multiple channels —
intentional. A title that exists as **both a movie and a show** resolves to whichever has
more playable programs (a series beats a lone film) — pin it with a typed ref
`{"movie": "Title"}` / `{"show": "Title"}` (editor syntax `{movie: Title}`). `/pipeline/compose`
emits typed refs automatically for exactly those colliding titles (#39). Live channels add one more content-ref type (`{"match": "title_contains", …}`),
documented under Live Channels.

**Write-only-on-deploy invariant.** `channels.json` is the record of **deployed channels**
and must stay in sync with Tunarr. Two rules:

1. **Planner-flow builders** (`compose`, `validate`, `discover-prompt`, `apply_collections`)
   read/write **`channels.draft.json`** only — never the deployed record. Abandoning a creation
   can at worst leave a stale draft.
2. **`channels.json` is written in exactly three ways:**
   - `deploy-selective` (`pipeline_router.py`: `_reconcile_channels_json`) — on a successful
     `create.py` exit (Nuke mode), writes the deployed set then clears `channels.draft.json`
     and `deploy_temp.json`.
   - `POST /api/pipeline/surgical-deploy` — Add/Edit mode: diffs draft vs deployed using
     `channel_engine.classify_channels`, executes the minimum Tunarr ops (create/delete/
     update-in-place/skip), then writes the merged managed set to `channels.json` and clears
     the draft. The create step passes `--no-delete` to `create.py` so it never wipes existing
     Tunarr channels — it only adds the new ones. Never touches orphan channels; never
     delete-recreates live channels.
   - `POST /api/channels/{number}/apply` — saves one entry and immediately patches Tunarr in
     place; they are always written together.

**Surgical deploy invariants (Add/Edit mode — never relax these):**
- `classify_channels(desired, deployed, prior_managed)` in `channel_engine.py` is the pure,
  testable diff function. Signature:
  `(desired: list[dict], deployed: list[dict], prior_managed: set[str] | None) -> dict`
  with keys `create | delete | update | unchanged | foreign`.
- **Provenance (`prior_managed`):** only channels whose lowercased name appears in
  `prior_managed` (the set of names the planner deployed last time, persisted in
  `planner_state.json["managed_names"]`) are eligible for deletion. Channels NOT in
  `prior_managed` (hand-authored on the Channels page, never built by the planner) go to
  the `foreign` bucket and are **never auto-deleted, created, or updated** by a surgical
  deploy. `channels.json` always includes foreign channels in its output.
- `managed_names` is written into `planner_state.json` on every successful deploy — both the
  surgical path and `_reconcile_channels_json` (the nuke/deploy-selective path). Bootstrapping:
  if `managed_names` is absent, `prior_managed` is empty → nothing is deleted (safe default).
- A changed **live** channel always lands in `update` (update-in-place) — its Tunarr id and
  Plex DVR mapping are preserved. A planner-managed live channel the user **removes** from the
  planner (name in `prior_managed`, absent from `desired`) IS deleted — intent wins. This is
  distinct from delete-RECREATE (which is always forbidden for live channels).
- **Orphan** channels (in Tunarr but absent from channels.json) are never passed into
  `classify_channels` and therefore cannot appear in any bucket.
- The route holds `scheduler.deploy_lock` for the full surgical operation.

Channels in Tunarr without a `channels.json` entry are "orphans" — visible on the Channels
page as read-only ("Not managed by Programmarr"). We deliberately do not reconstruct intent
(shuffle/live/franchise rules) from a deployed lineup.

**Commercials (optional).** A channel may carry
`"commercials": {"filler_list_id": "…", "filler_list_name": "…", "pad_minutes": 5}`. At deploy,
`create.py` attaches that Tunarr filler list to the channel (`fillerCollections`) and pads each
show up to the next `pad_minutes` boundary (`build_schedule(pad_ms=…)`), opening a gap that
Tunarr's FillerPicker fills with the clips **between shows** at playback. Absent = off. Applies to
**every** channel type (TV and movie) — density self-adjusts since the gap is per-program (a break
between movies vs. between episodes). The filler list itself is created/managed in Tunarr; the
picker is fed by `GET /api/tunarr/filler-lists`. The field is **per-channel by design**: the
Planner toggle is a blanket convenience that writes the same list onto every channel in a batch,
but each channel can point at a different filler list (the Channels editor already allows this) —
the basis for future era-matched pooling (90s ads → 90s channel; see `docs/ideas.md`).
**Mid-roll (ads inside a show) is deliberately not used — it doesn't stream on hardware-accelerated
(QSV) Tunarr; see [`docs/tunarr-commercials-findings.md`](docs/tunarr-commercials-findings.md).**

**Icon pin (optional).** A channel may carry `"icon": {"mode": "badge"|"tmdb"|"custom",
"url": "…", "pinned": true}` — written only by `POST /api/channels/{number}/icon` (the
Channels-editor icon control). `fetch_images.py` skips pinned channels and never writes
this field; removing it (the editor's "Reset to automatic") returns the channel to the
automatic art pass.

**Playback structure (optional).** `"playback": {"structure": "interleaved"|"timeline",
"episodes_per_block": 4}` controls cross-media scheduling: *interleaved* keeps movies in
watch order with ~N-episode blocks between (random-slot weights); *timeline* posts a manual
Tunarr lineup in strict release order (show runs air at their premiere position; commercials
padding is a no-op there). Absent = today's shuffle behavior. Live franchise channels compose
with interleaved/4 by default; editable per channel in the Channels editor.

## API Endpoints

All endpoint tables — **Pipeline**, **Recipe**, **Tunarr**, **TMDB**, **Plex** — live in
[`docs/api.md`](docs/api.md). The router source (`backend/routers/`) is the source of truth.

## Run.tsx — Pipeline Stepper UI

`frontend/src/pages/Run.tsx` is a **single unified stepper** (no tabs). The generation
method is a question on the first screen; the step list is built from the user's choices.

**Flow:** `Setup → Export → Planner → [AI Extras] → [Collections] → Deploy`. Export/Planner
are skipped for *Collections-only*; **AI Extras** appears only when the Planner's "✨ Bring in
AI" toggle is on; Collections only if opted in.

**Durable rules (these outlive any refactor of the step components):**
- **Deploy mode is a binary chosen on the Setup screen:**
  - **🧨 Nuke** — wipe all managed channels, numbers from 1. Uses the `deploy-selective` path
    (create.py wipe+rebuild). **Nuke only affects deploy behavior — it does NOT reset Planner picks.**
  - **✏️ Add/Edit** — keep existing channels, numbers continue above the highest existing managed
    channel + 1 (no rounding). Uses the surgical diff deploy path. Defaults to Edit when channels
    exist.
  - An **Advanced** disclosure under Edit mode lets a power user force-wipe specific channels.
- **Planner picks are always sticky** (`data/planner_state.json`):
  - **Saved on every change** via a debounced (~500ms) PUT in `PlannerStep` (guarded by
    `restoredRef` so the first-mount restore never overwrites the file).
  - **Restored on every Planner mount** (both Nuke and Edit modes) — `isEdit` gate removed.
  - **Nuke clears candidate selections** — `handleNuke` resets `selected`, `curate`, and
    `aiExtras`; genres/decades/comm/autoUpdate are preserved. The debounced save persists the
    blank selections to `planner_state.json` automatically.
  - **"Clear all"** in the Planner build bar does a full reset: clears `selected`, `curate`,
    active genre/decade toggles and all batch toggles to defaults, then calls
    `DELETE /pipeline/planner-state`.
  Contains: `activeGenres, activeDecades, selected, curate, aiExtras, commEnabled, commListId,
  commPad, autoUpdate`. API: `GET/PUT/DELETE /api/pipeline/planner-state`.
- **The Planner is deterministic:** selected candidates post as `CandidateSpec[]` to
  `POST /pipeline/compose`, which writes `channels.draft.json` (the AI and collections steps
  append to the same draft; the Deploy step's probe and `deploy-selective`/surgical-deploy both
  read it). Candidates are unchecked by default.
- **The AI layer merges on top** via `POST /pipeline/validate` with `append=true` (collisions
  renumbered, name-duplicates skipped) — it never overwrites the deterministic lineup.
- **Deploy runs a cascade that always completes:**
  - Nuke: `deploy-selective` → (art) → `sync`.
  - Edit: `surgical-deploy` → (art) → `sync`.
  Both stream inline, ending in a per-stage summary. In Edit mode no probe is run — the
  surgical deploy handles all diff logic.
- **The Planner body is a three-section accordion** (`AccordionSection` component, single-open
  at a time, `openSection` index state, Section 0 open initially):
  - **Section 0 — TV:** Marathons + Genre-blocks + **Networks** (from TV `Studio` values above `NETWORK_MIN=3`, via `EntitySection`) + **Classic TV Blocks** (from `programming_blocks.json` matched against library, `BLOCK_MIN=3`; spec carries `titles` field with present shows).
  - **Section 1 — Movies:** genre×decade, sub-genres, broad genres, Studios/Directors/Actors.
  - **Section 2 — TV + Movies:** mixed-genre candidates from `tv_movie_genres` facet (genres
    present in both libraries above `TV_MOVIE_MIX_MIN`) + **Franchises** (expandable cards with
    per-member checkboxes; **detected from TMDB + Wikidata** (P179 "part of the series" / P8345
    "media franchise", conservative label+year match, keyless-friendly) — Plex collections live
    in the separate Collections feature, not here. `data/wikidata_cache.json` alongside the TMDB cache). A background TMDB enrichment scan (`POST /pipeline/tmdb-scan`,
    `GET /pipeline/tmdb-scan/status`) runs each library movie through TMDB once with
    `append_to_response=keywords` (bounded concurrency), caching `belongs_to_collection` **and**
    `keywords` to `data/tmdb_enrichment.json` (keyed by library signature; shared with the themed
    channels). The Planner kicks the scan on mount and shows a progress bar; `GET /pipeline/franchises`
    reads the cache.
  Each section header opens/closes it (collapsing the other). A "Done — continue" footer
  button collapses the current and opens the next. The genres/decades chips and toggle cards
  (AI/commercials/auto-update) sit above the sections; the build bar sits below.

The blow-by-blow of each step's components and props is the code's job — read the `.tsx`.

## Known Limitations

**Plex guide shows channel icons, not text names.** When Plex receives a channel with any icon
in the XMLTV feed, it renders only the icon and suppresses the text label — a Plex design
decision, not a Programmarr bug. Tunarr injects a default icon for every channel, so without
custom icons the guide is a wall of identical icons. `fetch_images.py` now gives **every**
channel an icon: verified TMDB logos where trustworthy, generated name-stamped badges
everywhere else — so the guide is readable even though Plex hides the text labels.
Refreshing/restarting Plex does not change the icon-suppression behavior itself.

## Git Workflow

**`master` is the development trunk.** Pushing to it ships **nothing** — a master push runs
a CI **build-check only** (`docker build` with `push: false`). The public `:latest` image —
which end users run via the `docker compose` in the README — publishes **only when a GitHub
Release is cut**. So users receive a new image exactly once per version, never on day-to-day
commits. This is the whole point: accumulate many changes on trunk, release them as one version.

**Two operations:**

1. **`/ship` — daily work.** Commit + push to the current branch. Small/low-risk work can go
   straight to master; use a short-lived `feature/…`/`fix/…`/`chore/…` branch for big or
   abandon-able changes, then merge to master when done. Nothing here deploys.

2. **`/release` — going live.** The single gate that publishes an image. Docker-verifies the
   current trunk, asks for the new semantic version, bumps `frontend/package.json` +
   `CHANGELOG.md`, tags `vX.Y.Z`, and cuts the GitHub Release — which fires the versioned GHCR
   build (`:latest`, `X.Y.Z`, `vX.Y`, `sha-…`). End users then see an in-app **update banner**
   (via `GET /api/update-check`) and pull on their own schedule.

**The release-readiness gate lives at TAG time, not commit time.** Don't cut a release while
trunk has half-finished work — but committing in-progress work to trunk is fine and expected.

**SemVer:** patch = fixes/tweaks; minor = new features/UI/flags/endpoints; major = breaking
pipeline/schema/API changes. `/release` suggests the bump and always confirms.

**Updates are opt-in for users.** The app polls GitHub for newer releases (toggle in Settings,
default on) and shows a banner. Watchtower is documented as an *optional* auto-pull; because
images publish only on releases, even Watchtower users only ever get released versions.

**Always:** commit in small focused chunks with verbose what+why messages; **never commit
secrets or personal data** (`config*.json`, `channels*.json`, `*.csv`, `PROMPT.personal.md`
stay gitignored); **keep this file in sync in the same commit** as any behavior change.

## Live Channels (Auto-Updating Channels)

A **live channel** (`"live": true` in `channels.json`) is re-resolved against the Tunarr library
on a schedule and patched **in place**, so it stays fresh as the library grows. Ships **off** by
default (`recipes_enabled: false`).

**Two rules that must never be broken:**
1. **Update in place.** Look the channel up by number and `set_programming` on the existing
   Tunarr id. **Never** delete-and-recreate a live channel — that changes the Tunarr id and
   breaks the Plex DVR mapping. (`create.py`'s delete/recreate path is for *initial* deploy
   only; the scheduler must never use it.) **Name-match guard:** `update_channel_in_place`
   takes `expected_name` and refuses to patch (raises) if the Tunarr channel at that number
   carries a different name — so a `channels.json` that has drifted out of sync with Tunarr's
   numbering (two writers on one Tunarr; an orphan shifting numbers) can never silently
   overwrite the wrong channel. The scheduler skips + logs such mismatches instead of scrambling.
2. **Tunarr is the source of truth.** Each cycle diffs freshly-resolved program ids against the
   channel's *current* Tunarr programming and patches **only on a difference**. No state file
   drives correctness — `data/recipe_state.json` is cosmetic UI-only metadata (last-synced
   badges), never read by the diff.

**Title-match content-ref** — word-boundary name matching:
```json
{"match": "title_contains", "value": "Bad Boys", "order": "release_date", "exclude": []}
```
Word-boundary match (so "It" does not match "Little Women"); `order: "release_date"` sorts by the
Tunarr program's `releaseDate`; `exclude` drops false positives. Author-time preview
(`POST /api/recipes/preview`) requires human confirmation before saving — the LLM never auto-authors these.

**Franchise content-ref** — identity-based, for franchises whose members don't share a name:
```json
{"match": "franchise", "name": "Die Hard Collection", "order": "release_date", "exclude": []}
```
Members come from the Planner's cached TMDB (`belongs_to_collection`) + Wikidata franchise
data (`channel_engine.load_franchise_index` / `match_franchise`) — never name matching, so
new sequels join by membership once the scans have seen them (cache refresh stays on the
Planner's scan triggers). Authored by the Planner's per-franchise **"Keep updated"** switch
(`/pipeline/compose` computes `exclude` from unchecked members — the card's member list is
the author-time preview). Cache-miss at compose time falls back to a static channel; at
resolve time it counts as matched-nothing, and the refuse-to-wipe guards keep the channel
intact. Live franchise channels carry `playback: interleaved` by default (see channels.json schema).

**Moving parts** (scheduler loop, `channel_engine` updaters, `recipes_router`, the `Channels.tsx`
authoring UI and status cards), full rationale, rejected alternatives, and history:
[`docs/live-channels-design.md`](docs/live-channels-design.md).

## Issues

Tracked in GitHub Issues (`jamesmattson/programmarr`) via the `gh` CLI. Label
vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.
Bug reports come in through `.github/ISSUE_TEMPLATE/`, which requires the Programmarr and
Tunarr versions and the media-source type — the three fields that explain most reports.
