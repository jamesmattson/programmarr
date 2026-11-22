# API Reference

Exhaustive endpoint and external-API tables, relocated out of `CLAUDE.md` (which
keeps only a pointer here). The **router source** is the real source of truth — if a
table here disagrees with the code, the code wins. Keep this in sync when endpoints
change; don't restate it back into `CLAUDE.md`.

## Status / Guide API Endpoints (`backend/routers/status_router.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Connection status: `{tunarr:{ok,url,error?}, plex:{ok,url,error?}}` for the **saved** config. An HTTP error is `ok:false` — a 401 from an auth-protected Tunarr is a failure, not a healthy server |
| POST | `/api/test-connection` | Body `{tunarr_url, tunarr_username, tunarr_password, plex_url, plex_token}`. Tests credentials the user has typed but has **not saved** — this is what lets Onboarding refuse to green-light a typo. Only the sides supplied are tested. A secret sent as the mask falls back to the stored value. Never leaves the tested credential applied to the process. Plex results may carry `token_unverified: true` + a `note` when the server allows unauthenticated local access — the library is readable but the token itself was not proven |
| GET | `/api/guide` | Fetch and parse Tunarr's XMLTV feed. Returns `{channels:[{number,name,icon?}], programmes:[{number,start,stop,title,episode?}], error?}`. Channels sorted by number; timestamps as ISO 8601. Never throws — returns `error` field on failure. |
| GET | `/api/tunarr/channels` | Live channel list from Tunarr: `[{number,name,id?}]` |
| GET | `/api/tunarr/filler-lists` | Filler lists in Tunarr: `[{id,name,contentCount}]` — powers the Commercials picker |
| GET | `/api/update-check?current=<semver>` | Compares the newest published GitHub release against the running version (passed as `current`). Cached server-side ~6 h. Honors the `update_check_enabled` config flag — returns `{"enabled":false}` when off. Returns `{enabled, update_available, current, latest, name, url}`. |

## Channels API Endpoints (`backend/routers/channels_router.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/channels` | Full `channels.json`: `{channels, orphaned, suggested_channels}` |
| PUT | `/api/channels` | Overwrite entire `channels.json` |
| GET | `/api/channels/{number}` | Single channel by number; 404 if not found |
| PUT | `/api/channels/{number}` | In-place update of one channel; 404 if not found |
| DELETE | `/api/channels/{number}` | Remove one channel; 404 if not found |
| POST | `/api/channels/{number}/apply` | **Save-and-Apply**: push one channel to Tunarr **in place** (preserves Tunarr id/Plex mapping). The channel must already exist in Tunarr and in `channels.json`. Acquires `deploy_lock` to avoid racing the scheduler. Returns `{ok,number,program_count}`; 404 if not in `channels.json`, 400 if Tunarr not configured, 409 on engine error. |
| POST | `/api/channels/{number}/icon` | Set/pin the channel's Tunarr icon. Body `{"mode": "badge"\|"tmdb"\|"custom"\|"clear", "url"?}`. badge/tmdb/custom pin the choice in channels.json (skipped by automatic art passes); clear resets to the Tunarr default and unpins. 409 if the channel isn't deployed or no verified TMDB logo exists. |
| GET | `/api/library/titles` | Titles from `plex_library.csv` (for autocomplete) |

## Pipeline API Endpoints (`backend/routers/pipeline_router.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pipeline/libraries` | List Plex library sections filtered to `movie` and `show` types (`{key, title, type}`) |
| POST | `/api/pipeline/export` | SSE-stream `export.py`; JSON body `{"no_crossref": bool, "movie_sections": ["1","2"], "tv_sections": ["3"]}` — sections are Plex section keys; `null` = auto-detect, `[]` = skip that type |
| GET | `/api/pipeline/csv` | Download `plex_library.csv` |
| GET | `/api/pipeline/csv/info` | Stats: rows, movies, tv_shows, skipped counts, preview lines |
| GET | `/api/pipeline/facets` | **Facets v2** — one CSV pass returning everything the Planner candidate list needs: `genres:{canonical,more}` (canonical always present; `more` ≥ `min_items`), `decades`, `genre_decade` matrix (≥6), `blends` genre-pairs (≥6), entity lists `studios`(≥4)/`directors`(≥3)/`actors`(≥4, capped 60), `tv_genres`(≥3), `marathons` (every show with ≥2 episodes, `{title,episodes,seasons}` sorted desc — drives the per-show marathon candidates), `networks` (TV Studio values ≥ `NETWORK_MIN=3`, same `{value,count}` shape as studios), `tv_movie_genres`, plus `movies`/`tv_shows`/`marathon_count`. Thresholds are module constants in `pipeline_router.py`. |
| GET | `/api/pipeline/programming-blocks` | **Classic TV Blocks catalog.** Reads `programming_blocks.json` (repo root / `/app` in Docker), intersects each block's `shows` against the library TV titles (case-insensitive), returns blocks with `present_count ≥ BLOCK_MIN=3`. Shape: `[{name, era, network, shows, present_shows, present_count}]`. |
| GET | `/api/pipeline/franchises` | **Franchise candidates** — on-demand, cached. Returns `[{name, source, members:[{title,year,type}]}]` where members are filtered to titles present in `plex_library.csv`. Source 1 (`source="plex"`): fetches all Plex collection children via `/library/metadata/{ratingKey}/children`. Source 2 (`source="tmdb"`, only if `tmdb_api_key` configured): looks up `belongs_to_collection` per movie, groups by collection, keeps collections with ≥ `FRANCHISE_MIN=2` library members not already covered by a Plex collection. Results cached to `data/franchise_cache.json` keyed by CSV mtime+size. Pass `?refresh=1` to force re-scan. Network/TMDB failures return Plex-only results — never 500s. |
| POST | `/api/pipeline/compose` | **Planner v2 deterministic resolver.** Body `ComposeRequest{specs:[CandidateSpec], start}`. Each `CandidateSpec{kind, …}` (`kind` ∈ genre / genre_decade / blend / studio / director / actor / tv_genre / marathon / tv_movie_mix / **network** / **programming_block** / **franchise**) is resolved against `plex_library.csv` into a title list; empties skipped + reported. `network` matches TV show `Studio` values; `programming_block` intersects spec's `titles` field with library TV show titles; `franchise` intersects spec's `titles` against both movies + TV, sorted by year ascending. Writes **`channels.draft.json`** with sequential tight-packed numbering. Returns `{count, channels:[{number,name,items}], skipped}`. |
| GET | `/api/pipeline/prompt` | **Legacy** — fetch full `PROMPT.md` (meta header included) with `{TARGET}`, preferences, and `start` (block offset) injected; query params: `target`, `preferences`, `start`. |
| POST | `/api/pipeline/prompt` | Body `PromptOptions{target, preferences, start, include_genres, exclude_genres, include_decades, exclude_decades, include_types, exclude_types}`. Strips the meta header above the first `---` and injects a `## What To Build` section. |
| POST | `/api/pipeline/validate` | Parse/validate LLM output (file upload or raw text), write **`channels.draft.json`**. With form field `append=true`, **merges** on top of the existing draft — colliding numbers bumped to next free slot, name dupes skipped; returns `added` + `skipped_dupes`. |
| POST | `/api/pipeline/discover-prompt` | Build the AI-extras prompt, seeded with **`channels.draft.json`** lineup and numbering from `max+1`. Body `DiscoverOptions{discover, curate_pools}`. Returns `{content, start, existing_count}`. |
| POST | `/api/pipeline/no-ai` | SSE-stream `generate_no_ai.py`; query params `start`, `genres`, `decades`, `types`, `min_items` passed through as the matching `--` flags |
| GET | `/api/pipeline/collections` | Fetch all Plex collections (id, name, count, section, summary, has_poster) |
| GET | `/api/pipeline/collections/{id}/poster` | Proxy Plex collection poster image |
| POST | `/api/pipeline/collections/apply` | Append selected collections into **`channels.draft.json`** |
| POST | `/api/pipeline/probe` | SSE-stream `create.py --probe` |
| POST | `/api/pipeline/deploy` | SSE-stream `create.py`; query params: `protected` (comma-separated channel numbers to preserve), `no_delete` (bool) |
| POST | `/api/pipeline/deploy-selective` | JSON body `DeployRequest{selections, protected_numbers, no_delete}`; reads **`channels.draft.json`**, filters to selected entries, writes `deploy_temp.json`, SSE-streams `create.py`. On `returncode=0`, runs `_reconcile_channels_json` to write `channels.json` from the deployed set and clean up staging files. |
| POST | `/api/pipeline/images` | SSE-stream `fetch_images.py --apply` |
| POST | `/api/pipeline/sync` | SSE-stream `sync_plex.py` |

## Recipe API Endpoints (`backend/routers/recipes_router.py`)

Live-channel endpoints. Unlike the pipeline router (which spawns scripts as subprocesses), this
router imports `channel_engine` **in-process** — it adds `PROGRAMMARR_SCRIPTS` to `sys.path` so the
engine module (which lives at `/app`, not `/app/backend`) is importable. This is the same wiring the
scheduler reuses.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/recipes/preview` | JSON body `{value, exclude?, order?, match?}`; previews the resolved titles for a live ref before saving. `match` is `"title_contains"` (default, word-boundary name match via `match_titles`) or `"franchise"` (cached-membership identity resolution via `match_franchise`, backed by the same TMDB/Wikidata data the Planner uses — `value` is the collection/franchise name). Returns `{value, match, order, count, matches:[{title, year}]}` |
| GET | `/api/recipes/status` | Scheduler state: `{enabled, paused, running, interval_hours, next_run_seconds, live_count, last_cycle, channels}`. `next_run_seconds` = seconds to next auto cycle (null if disabled/paused); `channels` = per-channel sync metadata from `recipe_state.json` |
| POST | `/api/recipes/run` | Run one cycle on demand. `apply=true` (default) patches; `apply=false` is a dry run (detect + log, no Tunarr writes). `only=<number>` scopes the cycle to one live channel. Same code path the background loop uses |
| POST | `/api/recipes/pause` | `paused=true\|false` — runtime kill switch; halts/resumes the loop without a restart |
| POST | `/api/recipes/config` | JSON body `{enabled, interval_hours}`; **merge-writes** config.json (`recipes_enabled`, `recipe_interval_hours`) so it never clobbers connection/auth keys. Returns the new status |

## Tunarr API Endpoints Used

- `GET /api/media-sources` — discover Plex source and library IDs
- `GET /api/media-libraries/{id}/programs` — all episodes/movies in a library (paged since Tunarr 2026.11; read via `channel_engine.get_library_programs`)
- `GET /api/transcode_configs` — fetch transcode config ID at runtime
- `GET /api/channels` — list existing channels
- `POST /api/channels` — create channel
- `DELETE /api/channels/{id}` — delete channel
- `POST /api/channels/{id}/programming` — set rolling schedule (body: `{"type":"random","programs":[...],"schedule":{...}}`; schedule requires `padStyle` and `randomDistribution` as of current Tunarr version)
- `PUT /api/channels/{id}` — update channel settings (used by `fetch_images.py` to set `icon.path`)

## TMDB API Endpoints Used

- `GET /3/search/tv?query=...` — search for TV show by title
- `GET /3/search/movie?query=...` — search for movie by title
- `GET /3/tv/{id}/images?include_image_language=en,null` — fetch logo images for a TV show
- `GET /3/movie/{id}/images?include_image_language=en,null` — fetch logo images for a movie
- Images served from `https://image.tmdb.org/t/p/original/{file_path}`

## Plex API Endpoints Used

- `GET /library/sections` — discover library section keys
- `GET /library/sections/{key}/all?type=1` — all movies with full metadata
- `GET /library/sections/{key}/all?type=2` — all TV shows with full metadata

No dependencies beyond the Python standard library.
