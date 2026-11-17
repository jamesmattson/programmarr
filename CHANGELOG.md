# Changelog

All notable changes to Programmarr are documented here. This project follows
[Semantic Versioning](https://semver.org/) and the spirit of
[Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- **Channel entries can pin the exact item** (#40). An entry can now carry the item's own
  ids and year — `{"movie": "Aladdin", "year": 2019, "ids": {"tmdb": "…", "plex": "…",
  "tunarr": "…"}}` — so two things that share a title (the 1992 and 2019 *Aladdin*) can't be
  mixed up. If the first id goes stale (e.g. after a Plex re-match) a spare id still finds it;
  nothing is ever guessed. Plain titles and older entries resolve exactly as before.
- **`GET /api/library/lookup`** lists every library item with an exact title, each with its
  ready-to-save pinned entry, and **`GET /api/channels/{n}/review`** checks one channel against
  the library without changing anything (skipped items, and titles that match more than one
  item). Both read a 5-minute library cache that is cleared after every deploy, Apply, and
  live-channel update that changed something (also when it is interrupted or fails part-way).
  Only one library scan runs at a time: a request that arrives while one is running waits for
  it instead of starting a second scan of Tunarr.
- **Save and Apply refreshes a stale id** when a spare id confirms it is the same item, so the
  entry matches on its first id again. An entry found only by title and year is never
  rewritten.
- **The Channels editor's Add box pins the exact item.** Adding a title looks it up in the
  library first: one match is saved with its ids, several (the two *Aladdin*s) show a picker,
  and none is added as typed, as before. `{collection: …}` is never looked up. Editing and
  saving a channel no longer drops the extra keys on an entry; pinned rows show "· pinned".

### Fixed

- **Plex collection members are matched by Plex id, not by title** (#40). A channel built
  from a collection could play the *Wonder Woman* TV show instead of the movie in the
  collection, because only each member's title was kept. Members are now matched by their
  Plex id and type; one that can't be found is skipped and the rest still play. When Tunarr
  has no id for a member (older Tunarr) or a different year, it is still found by its title
  within its type, so every member v0.8.1 played still plays.

## [0.8.2] — 2026-11-21

### Fixed

- **Deploy and export work again on Tunarr 2026.11+** (#43). Tunarr 2026.11 changed its
  library programs endpoint to return results in pages, and Build Channels crashed with
  `AttributeError: 'str' object has no attribute 'get'`. Programmarr now reads every page,
  still works with older Tunarr versions, and treats a failed page as a failed library
  instead of deploying channels from a partial library.

### Changed

- **The Docker image moved to `ghcr.io/jamesmattson/programmarr`.** The GitHub account was
  renamed, and the old `ghcr.io/alpinearchitecture/programmarr` path no longer serves
  updates. Change the `image:` line in your `docker-compose.yml` to
  `ghcr.io/jamesmattson/programmarr:latest`, then `docker compose pull && docker compose up -d`.
  Your `data/` folder is untouched.

## [0.8.1] — 2026-09-10

### Fixed

- **A movie and a show with the same title resolve to the right one** (#39). With both the
  *Wonder Woman* film and the 1975 series in the library, a channel listing "Wonder Woman"
  always got the series. Channel content can now pin a media type — `{"movie": "Title"}` or
  `{"show": "Title"}` (type `{movie: Title}` in the Channels editor) — and the Planner writes
  these automatically for any title that is both a movie and a show. Plain titles behave as
  before.

## [0.8.0] — 2026-08-19

Hardening release ahead of going public. Programmarr had only ever run against its
author's Plex and Tunarr; this release is the result of auditing it for everything that
breaks, misleads, or silently does nothing on *somebody else's* setup.

### Added

- **Tunarr basic auth support.** Tunarr gained optional HTTP basic auth on its API;
  Programmarr sent no credentials at all, so a locked-down Tunarr failed everywhere.
  New `tunarr_username` / `tunarr_password` config keys, editable in
  Settings → Connections (and in onboarding). Every Tunarr call in every module now
  goes through one shared header builder, so enabling auth can't half-work.
- **Test connections in onboarding** (`POST /api/test-connection`). Saving config never
  contacted Tunarr or Plex, so a typo'd URL produced a green "Setup complete" and a
  broken install. The wizard now validates what you typed *before* writing it. A failed
  test warns but still lets you continue — it informs, it doesn't gatekeep.
- **Pre-delete lineup backups.** Deploy can delete Tunarr channels, which is
  irreversible. Programmarr now writes a gzipped snapshot of everything it's about to
  remove — each channel plus its full programming — to `tunarr_backup_*.json.gz` in your
  data directory before the first delete. The 3 most recent are kept; dry runs write
  nothing. See the new **Backups** section in the README.
- **Tunarr version reporting.** The Dashboard's connection status now includes Tunarr's
  self-reported version, so bug reports can state it without digging.
- **Issue templates and Discussions**, so bug reports arrive with the version and log
  information needed to actually act on them.

### Fixed

- **Channel numbering order was ignored by the AI path.** `PROMPT.md` had been rewritten
  to a numbered list while the code still matched the old wording, so the function that
  rewrites the numbering scheme was silently doing nothing — the LLM always received the
  *default* category order regardless of Settings → Channel Numbering. A non-match now
  warns loudly instead of passing the content through unchanged.
- **Pre-1970 films were invisible to the decade facet.** Decade buckets started at 1970,
  so a classic-film collection contributed nothing at all. Buckets now run from the
  1920s. (On the author's library this made 132 previously-unreachable films eligible for
  decade channels.) New labels are spelled in full — "1950s", not "50s", which would be
  ambiguous beside "2020s". Existing 70s/80s/90s labels are unchanged, so current channel
  names and saved planner state keep matching.
- **A failed library fetch looked like an empty library.** Every API error collapsed to
  "no results", so an outage reported "Indexed 0 movies" and would deploy channels with
  nothing on them. Failures are now distinguished from genuinely empty libraries: all
  libraries failing raises, a partial failure warns and continues.
- **Misleading errors for unsupported setups.** "No Plex source found in Tunarr" covered
  four unrelated situations and sent Jellyfin users off debugging Plex. Now each is named
  specifically: an unreachable Tunarr, a Tunarr with no media sources, a non-Plex source
  ("Found: jellyfin"), a Tunarr too old to have the required endpoint, and — separately —
  credentials that are missing versus credentials that are wrong.
- **A broken pipeline run hung the UI forever.** The progress stream ends on a completion
  event; anything failing after the response started (a subprocess that won't launch, a
  dead pipe, an unwritable log) killed the stream with no error and no exit code, leaving
  a spinner. Every failure path now terminates properly.
- **An empty library picker with no explanation.** A Plex failure while listing libraries
  was swallowed by a comment claiming the UI showed the error elsewhere. It didn't.
- **A wrong Plex token could pass the connection test.** Many self-hosted Plex servers
  allow unauthenticated access on the local network, so *every* local request succeeds —
  even with no token. That result is now flagged as unverified rather than reported as a
  validated token.
- **Empty Planner sections now say why.** "No TV channels available — run Export" was
  shown for three different situations, including to users who had already exported. A
  movies-only library is now told plainly there are no TV channels to build; a library
  with shows that don't qualify gets the actual thresholds named.

### Changed

- Deploy-time backups are gzipped and retention is 3 rather than 10. Measured on a real
  156-channel server: 151 MB raw, 12 MB compressed — at the old retention that was 1.5 GB
  quietly accumulating in a data directory.
- README quick start now includes the compose file inline; previously only the TrueNAS
  section had one, so anyone who hadn't cloned the repo had nothing to run. Requirements
  now state Tunarr 1.x + Plex explicitly, and that Jellyfin/Emby are not supported.

### Notes

Plex remains required. If you'd like Jellyfin or Emby support, add a reaction to
[#36](https://github.com/AlpineArchitecture/programmarr/issues/36) — demand decides
priority.

## [0.7.1] — 2026-06-20

### Fixed

- **Title-collision resolution.** When a library holds a movie and a TV series with the
  exact same title (e.g. the 2017 *Baywatch* film and the 1989 series), `resolve_title`
  picked the movie first and returned unconditionally — so a marathon channel could loop
  a single film instead of the series. Resolution now prefers whichever copy has more
  *playable* programs (the same tie-break `build_library_index` uses for duplicate shows):
  a real series beats a lone movie; an all-missing series still yields to the movie.
  Applies to every path that resolves titles (initial deploy, live re-resolve,
  surgical/apply).

### Changed

- **Dependency cleanup.** Dropped unused `@mantine/code-highlight` (frontend) and
  `aiofiles` (backend) — neither was imported anywhere.

## [0.7.0] — 2026-06-11

### Added

- **Playback structure (per-channel).** Channels can carry
  `"playback": {"structure": "interleaved"|"timeline", "episodes_per_block": N}`.
  *Interleaved* keeps movies in watch order with ~N-episode blocks between them;
  *timeline* posts a manual Tunarr lineup in strict release order (show runs air at
  their premiere position). The field is plumbed through every deploy path (compose,
  surgical deploy, nuke, apply) and editable per channel in the Channels editor.
  Live franchise channels default to interleaved/4.
- **Franchise live content-refs.** A new identity-based ref
  `{"match": "franchise", "name": "…", "order": "release_date", "exclude": []}`
  lets live channels auto-add franchise members that don't share a name (resolved from
  the cached TMDB `belongs_to_collection` + Wikidata franchise index, never name
  matching). Authored by the Planner's per-franchise **"Keep updated"** switch; the
  scheduler, surgical deploy, and `apply` all resolve it; recipes preview supports it.
- **Channel icon overhaul.** Every channel now gets an icon: verified TMDB logos where
  trustworthy (kind-gated, name-must-match-query) and generated name-stamped badges
  everywhere else (`badge_renderer` + committed `badge_assets/`). New Channels-editor
  icon control (badge / TMDB / custom upload / reset-to-automatic) via
  `POST /api/channels/{number}/icon`, with pin support so manual icons survive the
  automatic art pass. `tmdb_api_key` is now optional — without it everything badges.
- **Multi-source / multi-server library support.** Export auto-supplements from all
  Tunarr media sources; the Export-step library picker shows every source grouped by
  server. Multi-Plex-server support across export, deploy, and Settings.
- **Planner refinements.** Deselect-all on entity dropdowns, a reset confirmation,
  Nuke now clears candidate selections, and auto search+scroll for large candidate
  lists.

### Fixed

- **Icons never use SVG logos.** Plex guide icons must be raster — SVG TMDB logos are
  now rejected so the guide renders.
- **Export-step library labels.** Divider labels group library checkboxes by source
  server; redundant per-label server suffix removed.

### Changed

- **Self-healing dev loop.** `dev.ps1` kills stale instances before starting.

## [0.6.0] — 2026-06-10

### Added

- **Multi-title channel logos.** `fetch_images.py` now fetches real TMDB logos for
  network, franchise, entity (director/actor), and generic multi-title channels — not
  just solo-title channels. Kind-aware search strategies: network channels try
  company/network search first; franchise channels try the cleaned name then the first
  content item; director/actor channels use the cleaned name; everything else falls
  back through TV → movie → company. Kind hints are read from `planner_state.json`;
  missing file is a safe no-op. Solo-title path is unchanged.
- **In-app update banner.** When a new GitHub release is available, a dismissible
  banner appears in the app linking to the release notes. Backed by
  `GET /api/update-check`, which polls the GitHub Releases API and caches the result.
- **"Check for updates" toggle in Settings.** Update polling is on by default; users
  can opt out without rebuilding the container. Persisted to `config.json` as
  `update_check_enabled`.

### Fixed

- **Live-channel name-match guard.** `update_channel_in_place` now takes an
  `expected_name` and refuses to patch (raises, then skips + logs) if the Tunarr
  channel at that number carries a different name. Prevents silent content scrambling
  when two Programmarr instances share one Tunarr server — or when `channels.json`
  drifts out of sync with Tunarr's numbering. Wired into the scheduler, the
  Channels-page "Save and Apply," and the surgical deploy.
- **Live-channel indexing now covers all Plex libraries.** `build_library_index`
  previously indexed only the first enabled movie and first enabled TV library, silently
  dropping entire secondary libraries (e.g. a "Cartoons" section alongside "TV Shows").
  It now aggregates every enabled library of each kind.
- **Live-channel dedup across libraries.** A show that appears in more than one Plex
  library is indexed once, preferring the copy with the most playable (non-`missing`)
  episodes. Without this, a dead duplicate could shadow the real copy, or doubled
  episode counts caused a permanent diff mismatch that churned the channel every cycle.
- **Light mode theming.** Introduced semantic CSS surface/border tokens
  (`--app-bg`, `--surface-panel`, `--surface-sunken`, `--border-subtle`) defined
  per color scheme, replacing 40+ hardcoded dark values across 7 components that never
  flipped in light mode. Warm off-white page background replaces blinding gray-0.
  Terminal surfaces (SSE output, Logs, AI prompt) stay dark in both modes by design.
  Dark mode is visually unchanged.
- **Logs page readable in light mode.** The log code block forces a dark terminal
  background but text was inheriting Mantine's default dark color — dark-on-dark,
  unreadable. Text is now pinned to the same light terminal value used by the SSE
  terminal and AI prompt preview.

## [0.5.0] — 2026-06-09

A large Planner overhaul: smarter channel discovery from new data sources, a guided
three-section Planner, sticky picks, and a safe surgical add/edit deploy.

### Added

- **Three-section Planner (TV / Movies / TV+Movies).** A single-open accordion replaces the
  long candidate list. Categories open by default with a per-category **Done** collapse; a new
  **TV+Movies** section builds mixed-genre channels that interleave episodes and films of a
  shared genre.
- **Sticky Planner picks.** Your selections persist to `planner_state.json` (saved on every
  change, restored every run) — reopen the Planner and your last lineup is already checked.
  **Nuke** keeps your picks (it only wipes Tunarr); **Clear all** is the sole reset.
- **Add / Edit vs Nuke.** A top-of-Run choice. **Add/Edit** does a *surgical* deploy — creates
  new channels, deletes unchecked ones, updates changed ones **in place** (preserving the Tunarr
  id + Plex DVR mapping), and never touches live or hand-authored channels. **Nuke** wipes and
  rebuilds from channel 1.
- **Deploy diff preview.** Before anything touches Tunarr, see exactly what will change —
  + new / ~ changed / − removed / = unchanged / · not-managed — then **Confirm**.
- **Real networks from TVmaze** (HBO, NBC, Apple TV+…) — replaces the unreliable Plex "studio"
  field, no API key needed.
- **Franchise detection** spanning TV + film, from **TMDB + Wikidata** (per-member checkboxes —
  pick your favorite entries). Runs as a cached background scan with a progress bar.
- **Themed channels** from a curated TMDB-keyword catalog (Heist, Time Travel, Christmas,
  Dystopian, Superhero, …) — deterministic, no AI step needed.
- **Classic TV programming blocks** (TGIF, Must See TV, Saturday Morning Cartoons, …) matched
  against your library from a built-in catalog.
- **Country / Mood / Style channels** from Plex metadata (e.g. French Cinema, Film Noir).
- **Dashboard:** open-in-new-tab buttons for Tunarr and Plex.
- **Settings:** drag-and-drop reordering of the channel-numbering categories.

### Changed

- **Channel numbering is now sequential from 1, grouped by category.** No fixed block sizes or
  caps — the lineup is exactly as long as what you pick, and the category order is configurable
  (Settings + first-run onboarding). Replaces the old `channel_blocks` sizes with `channel_order`.
- Collections stay in their own dedicated feature — they're no longer pulled into the Franchise step.

### Fixed

- **Surgical deploy now records the right channel numbers.** Editing an existing channel updates
  it in place at its real Tunarr number instead of mislabeling it in `channels.json`.
- **Dev loop export works on Windows.** The dev backend reloads at the process level (watchfiles)
  instead of `uvicorn --reload`, which forced the Selector event loop and broke pipeline
  subprocesses (`export.py` etc.). No-op on Docker.
- Dashboard guide no longer repeats the channel number; the auto-update card is trimmed to status
  + last-run.

## [0.4.4] — 2026-06-08

### Fixed

- **Planner now deploys what you built — AI channels and collections no longer vanish.**
  The Planner assembles its lineup in `channels.draft.json` (compose + the AI "Bring in AI"
  step + the Collections step all write it), but the Deploy step was reviewing/deploying the
  wrong file. Two bugs, both fixed:
  - The Deploy review was built from `create.py --probe`, which read the **deployed**
    `channels.json` instead of the draft — so AI-discovered channels and added collections
    never showed up and never deployed. The probe now targets the draft (and forwards kept
    channels as `--protect` for an accurate delete preview).
  - The Collections step sized its channel numbers from the deployed record, not the draft,
    so collections landed on top of the AI channels and `apply_collections` deleted them.
    A new `GET /pipeline/draft` lets the Collections step place itself **above** the full
    built lineup, preserving every channel.
- **Export no longer crashes on a Windows host.** Pipeline subprocess output is now forced to
  UTF-8 (`PYTHONIOENCODING`), so a title containing a non-cp1252 character (e.g. `⧸`) in the
  unsynced-titles list no longer kills `export.py` with `UnicodeEncodeError`. No-op on Docker.

## [0.4.3] — 2026-06-08

### Fixed

- **Dashboard guide now has a single unified vertical scroll.** The EPG guide grid previously
  showed two independent vertical scrollbars — one for the channel rail (number/name) and one
  for the programme grid — so scrolling one left the other behind. The rail's scroll is now
  driven by the programme grid via the existing scroll-sync mechanism, so the two move together
  as one. The mousewheel still works while hovering the channel names.
- **AI Extras "Copy" button works over plain HTTP.** Copying the AI prompt silently did nothing
  on non-secure origins (e.g. the production box over HTTP), because `navigator.clipboard` is
  undefined outside a secure context and the call threw with no fallback. Added a clipboard
  helper with a `textarea` + `execCommand('copy')` fallback and success/failure notifications,
  used by both the prompt copy and the XMLTV-URL copy.

## [0.4.2] — 2026-06-08

### Added

- **EPG Guide grid on the Dashboard.** The Dashboard now shows a real TV-guide grid
  (scrollable, 3-hour window, now-line) sourced from Tunarr's XMLTV feed. Clicking any
  channel in the left rail navigates to its editor on the Channels page.
- **Channels page sourced from live Tunarr.** The Channels list now pulls from the live
  Tunarr API so the list always reflects what's actually deployed — no more stale data.
  Tunarr channels with no `channels.json` entry show as **"Not managed by Programmarr"**
  (read-only orphans).
- **Save and Apply** (`POST /api/channels/{number}/apply`). Edit any managed channel and
  push the change to Tunarr in a single click — updates in place, preserving the Tunarr
  channel id and Plex DVR mapping. Acquires the same lock as the live-channel scheduler
  to avoid races.
- **Configurable block sizes.** Each channel category (Marathons, TV Blocks, Movies,
  Franchise, Specialty) now has a configurable size in Settings, so large libraries can
  scale a block without colliding into the next. Channel numbering now defaults to starting
  at 1 (previously 10). `channel_blocks.py` is the new single source of truth, shared by
  the Planner and `generate_no_ai.py`.

### Changed

- **Anti-drift deploy model.** Planner-flow operations (Compose, Validate, AI Extras,
  Collections) now write to `channels.draft.json` instead of `channels.json`. The deployed
  record (`channels.json`) is written only on a successful `deploy-selective` run, keeping
  it in sync with what's actually in Tunarr. Abandoning a creation can at worst leave a
  stale draft, never corrupt the deployed record.

## [0.4.0] — 2026-06-07

### Added

- **Commercials.** Any channel can play commercials in the gap between shows — real-TV
  style. Turn it on with the new **📺 Add commercials** toggle in the Planner (applies to
  every channel you build) or per-channel in the channel editor, and pick which Tunarr
  **filler list** to pull clips from. A short pad after each program opens the gap; Tunarr's
  filler picker fills it (varied selection, no back-to-back repeats). Density self-adjusts —
  a break between movies on a movie channel, between episodes on a TV channel. `channels.json`
  gains an optional per-channel `commercials` object. (Mid-roll ads *inside* a show were
  investigated and deliberately left out — they don't stream on hardware-accelerated Tunarr;
  see [`docs/tunarr-commercials-findings.md`](docs/tunarr-commercials-findings.md).)
- **Auto-update toggle in the Planner.** A **🔄 Keep channels fresh** switch marks the
  channels you build as live, so new episodes and matching films appear on their own as your
  library grows (runs on the live-channel schedule).
- **Advanced config (optional, `config.json`).** `tunarr_channel_group` and
  `tunarr_stream_mode` set the Tunarr group/folder and streaming mode for generated channels.
  See README → Advanced Configuration.
- **Filler-list picker endpoint** — `GET /api/tunarr/filler-lists`.

### Fixed

- **Settings saves no longer clobber config-file-only keys.** `config.json` is now
  merge-written, so hand-edited keys (the advanced ones above, and the live-channel
  `recipes_*` keys) survive a save from the Settings form instead of being dropped.

### Changed

- **Export step warns about filler libraries.** A clearer, more visible note to leave
  commercials / trailers / bumper libraries out of the content scan — Plex labels them as
  "movies," so they'd otherwise sneak into your channel candidates.

## [0.3.1] — 2026-06-05

### Fixed

- **Multi-library exports no longer silently drop content.** Two related bugs in
  `export.py` where content vanished from `plex_library.csv` without any error:
  - **Tunarr cross-reference checked only the first library of each type.** With more
    than one enabled Tunarr movie (or TV) library — e.g. a main "Movies HD" plus a
    "Crossovers HD" — only the first was loaded, so everything in the rest failed the
    cross-ref check and was skipped. It now unions **all** enabled libraries of each
    type, and prints per-library counts plus a combined total.
  - **CLI auto-detect picked only the first Plex section of each type.** Running
    `export.py` without `--movie-sections`/`--tv-sections` and with multiple Plex
    libraries (e.g. "TV Shows" + "Cartoons") silently ignored the extras. Auto-detect
    now collects **every** matching section. The explicit `--*-sections` flags and the
    UI (which always passes keys) were unaffected.

  No CSV schema or downstream changes — a single-library export is byte-identical.

## [0.3.0] — 2026-06-05

### The Planner — a ground-up rebuild of how you create channels

The biggest release since the web app shipped. The old three-tab Run pipeline
(AI / No-AI / Collections) is **gone**, replaced by a single unified **Planner**
that composes curated, hand-programmed-feeling channels from your library —
**deterministically** — with AI demoted from "the engine" to an optional layer on
top. The result feels like a TV lineup a person with taste assembled, not a wall
of `Comedy Movies (140)` blocks.

### Added

- **The Planner: ingredients → curated candidates.** Pick which genres and decades
  are "in play," then check the exact channels you want from a live, **counted**
  candidate list. Everything is built by precise filters — no AI required, no
  guesswork:
  - **Per-show TV Marathons** — one 24/7 channel per show; every show in your
    library is offered, sorted by episode count.
  - **TV genre Blocks** — themed multi-show channels (Comedy TV, Crime TV…).
  - **Genre × Decade** — tight era cuts like "90s Comedy" and "80s Horror," nested
    under each decade.
  - **Named Sub-genres** — a curated, recognizable set (Rom-Coms, Dark Comedies,
    Horror Comedies, Crime Thrillers…) instead of arbitrary genre pairs.
  - **Studio / Director / Actor channels** — A24, "Directed by Tarantino," "Adam
    Sandler Movies" — the most curated-TV channels there are.
  - **Bulk picking** — "Top 10" and "Add all" on every section, and sections that
    fold away once handled so the list never becomes a wall of checkboxes.
- **Optional AI layer (✨ Bring in AI).** Two things filters can't do, both seeded
  with your built lineup and **merged on top** via a copy-prompt / paste-back
  hand-off:
  - **Discovery** — the AI suggests themed channels your filters miss (Heist Films,
    Courtroom Dramas, Time Travel, Sports Underdogs…), and is told what you already
    have so it never duplicates it.
  - **Tonal curation** — flag a broad pool (a ✨ on any genre or decade pick) and
    the AI splits it by *tone* (Feel-Good vs Raunchy vs Dark Comedies) — the "one
    comedy doesn't blend into the next" fix that genre tags alone can't make.
- **New library metadata for entity channels.** `export.py` now captures **Studio**
  and the top-3 billed **actors** per title (both already present in Plex's
  response), so studio/director/actor channels work out of the box.
- **Facets API.** One pass over your library powers the entire candidate list with
  live counts — genres, decades, the genre×decade matrix, sub-genre blends, and
  the studio/director/actor/marathon lists.

### Changed

- **One unified flow:** Setup → Export → Planner → [AI Extras] → [Collections] →
  Deploy. The generation "method" is no longer three separate tabs; it's a single
  Planner, with **Collections-only** kept as a quick path.
- **Every decision is up front.** Keep-vs-wipe of existing channels, whether to add
  collections, and whether to fetch art are all answered on the first screen — the
  rest of the flow is review-and-go.
- **Deploy is one cascade.** It auto-probes on entry, you review/renumber, hit
  **Deploy** once, and channel art + Plex sync run automatically — with an
  always-completes status summary.
- **Soft-block channel numbering.** Marathons ~10s, TV blocks ~20s, movie channels
  ~30s+, entity channels ~50s+ — assigned sequentially and spilling gracefully
  instead of overflowing fixed 20-slot blocks.
- **Clearer AI hand-off.** The walkthrough now spells out "paste the JSON only,"
  and the prompt is generated from your exact picks.

### Fixed

- **Merge dedup.** Pasting AI suggestions skips any channel whose name already
  exists, so re-running the AI step can no longer stack duplicate channels.
- **Collections numbering.** Collections now append *above* your composed lineup
  rather than at a fixed ~80 block, so they never overwrite the channels you just
  built.
- **Prominent Plex-sync fallback.** When Plex can't auto-add the DVR (common in
  many setups), the end-of-run summary now shows exactly what to do — with a
  one-click-copy XMLTV URL — instead of a quiet status line.

### Notes

- The deterministic Planner is a strict superset of the old No-AI path. The
  standalone scripts (`generate_no_ai.py`, `generate_from_collections.py`) and the
  `programmarr.py` CLI remain for power users.
- Live Channels (v0.2.x) is unchanged and fully compatible — channels built in the
  Planner can still be marked "live" to auto-update as your library grows.

## [0.2.3] — 2026-06-04

Follow-up to the Live Channels release: per-channel visibility and control that the
original plan called for but v0.2.2 only partly delivered.

### Added

- **Per-channel "last synced" badge.** Each live channel row on the Channels page
  shows when it was last checked ("synced 2h ago" / "never"). Backed by a new
  cosmetic `data/recipe_state.json` — written by the scheduler, read by the UI. It
  is **not** correctness state (the change-detection diff still reads live Tunarr);
  it lives in its own file so it never races with `channels.json` edits or deploys.
- **Per-channel "Save & Sync now".** The channel editor (for live channels) gains a
  button that saves the channel and immediately runs a scheduler cycle scoped to
  just that channel (`/api/recipes/run?only=N`), applying the recipe to Tunarr in
  place without leaving the editor.
- **"Next run" on the Dashboard.** The Auto-Updates card now shows when the next
  automatic check will run (e.g. "next ~in 11h"), alongside the last check.

### Changed

- `/api/recipes/status` now returns `next_run_seconds` and a per-channel `channels`
  map. The scheduler's last-run timestamp moved to wall-clock time so the status
  endpoint can compute the next run without touching the event loop.

### Notes

- Documentation reconciled against the full original plan: source/target
  agnosticism (Jellyfin source, ErsatzTV target) is captured as an explicit
  **future** goal with the in-place-update contract noted, but intentionally not
  built yet — there's no second source/target to validate an adapter layer against.

## [0.2.2] — 2026-06-04

### Added — Live Channels (auto-updating channels)

The headline feature of this release. Channels can now be **self-maintaining**:
mark one "live" and Programmarr re-checks it against your library on a schedule
and patches it **in place** as the library grows — no redeploy, no manual edits.

- **Live toggle per channel.** In the channel editor, an "Auto-update on a
  schedule" switch marks a channel live. Live channels show an orange badge in
  the list. A channel without the flag behaves exactly as before.
- **Franchise auto-match.** A new content rule, `title_contains`, pulls in every
  library title matching a phrase (e.g. `Bad Boys` → all four films, in release
  order) and auto-adds new sequels as they appear. Matching is **word-boundary**,
  not raw substring, so `It` matches "It Follows" but never "Little Women". The
  editor shows a **live preview** of exactly what matches before you save, with
  per-title checkboxes to exclude false positives.
- **In-place updates — the critical guarantee.** Updates reuse the existing
  Tunarr channel via its id (same channel number, same internal id), so Plex's
  Live TV / HDHomeRun DVR mapping is never disturbed. Channels are **never**
  deleted and recreated for an update. Verified end-to-end: a live Batman channel
  grew from 162 to 254 programs with its channel id byte-for-byte unchanged.
- **Change-detection, not blind re-posting.** Each cycle diffs the freshly
  resolved program set against the channel's *current* Tunarr programming and
  patches only on a real difference. An unchanged channel is a cheap no-op —
  idempotent, with no needless Plex guide churn. There is no state file; Tunarr
  is the source of truth, so the scheduler survives container restarts for free.
- **Background scheduler.** A lightweight asyncio loop runs inside the existing
  FastAPI app (no extra process). It re-reads config every minute, so enabling,
  pausing, or changing the interval takes effect without a restart. Ships **off**.
- **Controls & visibility.**
  - *Settings → Live Channels*: master enable switch + check interval (hours).
  - *Dashboard → Auto-Updates* card: on/paused state, live count, last-cycle
    summary with per-channel changes, a **Pause/Resume** toggle, and a
    **"Check now"** button that runs a cycle on demand.
  - A rolling diff log is written to `data/logs/recipes.log`.
- **New API endpoints** (`/api/recipes/*`): `preview` (author-time match preview),
  `status`, `run` (`apply=false` for a safe dry run), `pause`, and `config`
  (merge-writes the enable flag + interval without clobbering connection settings).

### Changed

- **`create.py` refactor.** The Tunarr resolution logic (library indexing, title/
  collection/franchise resolution, schedule building, in-place programming
  updates) was extracted into a new, dependency-free, importable
  `channel_engine.py`. `create.py` is now a thin CLI wrapper around it, and the
  scheduler and preview endpoint share the exact same resolver. No behavior change
  to existing deploys.
- **Deploy/scheduler safety.** Manual deploys and the scheduler now share a lock
  so a deploy and an auto-update can never touch Tunarr at the same time.

### Fixed

- **Channel editor "bounce" on Save.** Saving a channel could re-open the modal
  instead of closing it (and fire the "saved" notification twice) because the
  list reload re-triggered the deep-link open. The deep-link logic is now guarded
  so a reload never re-opens a modal you just closed.

### Misc

- Added a favicon (orange broadcast icon matching the navbar logo).

## [0.2.1] — 2025

### Added

- **Channel protection** — choose which existing Tunarr channels to keep before a
  deploy; protected channels (and their custom logos) are preserved while the rest
  are rebuilt.
- **Channel offset** — set a starting channel number so new channels don't collide
  with ones you keep; auto-calculated from the highest protected channel.

## [0.2.0] — 2025

### Added

- **Library picker** on the Export step — choose which Plex libraries to scan, and
  mix multiple libraries of the same type.
- **Run pipeline UI rewrite** — a premium stepper with export stats, probe review,
  and selective deploy across the AI / No-AI / Collections paths.
- UI screenshots in the README.

## [0.1.x] — 2025

### Added

- Initial **web app**: FastAPI + React + Mantine, served as a single Docker
  container on port 7979, with a first-run onboarding wizard.
- AI / No-AI / Collections pipeline paths with live streamed terminal output.
- TMDB channel-logo fetching, Plex DVR sync, optional HTTP basic auth.
- Dark / light / auto theme switching.
- GitHub Actions CI publishing to GHCR; Watchtower auto-update support
  (including the `DOCKER_API_VERSION` fix for newer Docker engines).

[0.6.0]: https://github.com/AlpineArchitecture/programmarr/releases/tag/v0.6.0
[0.2.3]: https://github.com/AlpineArchitecture/programmarr/releases/tag/v0.2.3
[0.2.2]: https://github.com/AlpineArchitecture/programmarr/releases/tag/v0.2.2
[0.2.1]: https://github.com/AlpineArchitecture/programmarr/releases/tag/v0.2.1
[0.2.0]: https://github.com/AlpineArchitecture/programmarr/releases/tag/v0.2.0
