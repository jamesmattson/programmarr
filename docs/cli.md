# CLI guide (no Docker required)

Programmarr started as a handful of Python scripts, and they're all still here. If you'd
rather not run another server on your network, you can do **everything the web app does** from
the terminal. The scripts have **zero dependencies beyond the Python 3 standard library** and
run standalone.

> **Where config lives:** in the CLI, `config.json` sits in the **project root** (next to
> `programmarr.py`). In Docker it lives in `data/`. Same file shape either way — see
> [`config.json.example`](../config.json.example) and the **Advanced Configuration** table in
> the [README](../README.md#advanced-configuration) for every key (including `tunarr_stream_mode`
> and `tunarr_channel_group`).

## Requirements

- Python 3 (no `pip install` needed — stdlib only)
- A running [Tunarr](https://github.com/chrisbenincasa/tunarr) instance and a Plex server
- Optionally a [TMDB API key](https://www.themoviedb.org/settings/api) for channel logos

```bash
git clone https://github.com/jamesmattson/programmarr.git
cd programmarr
python programmarr.py
```

---

## Path A — the interactive menu

`python programmarr.py` is the guided front door. On first run (no `config.json` yet) it walks
you through a short setup wizard, then drops you at the main menu:

```
  1) AI path         — export → LLM → deploy
  2) No-AI path      — auto-generate → deploy
  3) Collections     — sync Plex collections → deploy

  i) Fetch channel images from TMDB
  s) Sync channels to Plex DVR

  q) Quit
```

- **AI path** — exports your library, builds a prompt you paste into an LLM (Claude/Gemini/ChatGPT),
  then deploys the `channels.json` the LLM returns. Always probes before deploying and asks
  whether to wipe-and-rebuild or preserve channels below a number.
- **No-AI path** — generates a starter `channels.json` straight from your library metadata
  (decade + genre movie channels, 50+ episode TV marathons) and deploys it. No LLM involved.
- **Collections** — turns your Plex collections (managed by Kometa/Trakt/Letterboxd) into
  channels, one per collection.
- **i / s** — run the image fetch or Plex-DVR sync on their own.

> **The wizard only writes the required keys** (`tunarr_url`, `plex_url`, `plex_token`, and TMDB
> if given). Advanced keys are **not** prompted — see [Setting advanced config](#setting-advanced-config)
> below to add them by hand.

---

## Path B — raw scripts (for cron and scripting)

The menu just orchestrates these. Call them directly for automation. **Every script takes
`--help`** — that's the authoritative flag reference; this guide only shows the chain and the
gotchas worth knowing.

```
export.py  →  generate_no_ai.py   →  create.py  →  fetch_images.py  →  sync_plex.py
              (or hand off to an LLM)
```

| Script | What it does |
|--------|--------------|
| `export.py` | Pulls full metadata from Plex → `plex_library.csv` + `export_summary.json`. Auto-detects movie + TV sections. |
| `generate_no_ai.py` | Builds a starter `channels.json` from the CSV (no AI). `--order KEY,KEY,…` overrides category order; `--start N` sets the first channel number. |
| `generate_from_collections.py` | One channel per Plex collection. `--apply` to write; manages the collection block from `--base` up. |
| `create.py` | Reads `channels.json` and deploys to Tunarr (delete-then-create). `--from N` scopes; `--protect N1,N2` preserves specific channels. |
| `fetch_images.py` | Sets every channel's Tunarr icon (verified TMDB logos + generated badges). **Dry-run by default; `--apply` to commit.** |
| `sync_plex.py` | Reconciles Tunarr's channel list into Plex's DVR mapping. Never deletes the DVR. |

### Example: nightly no-AI rebuild via cron

Save this as `rebuild.sh` in your install dir and `chmod +x rebuild.sh`:

```bash
#!/usr/bin/env bash
cd /opt/programmarr
python export.py
python generate_no_ai.py
python create.py
python fetch_images.py --apply
python sync_plex.py
```

```cron
# 4am daily
0 4 * * *  /opt/programmarr/rebuild.sh >> /var/log/programmarr.log 2>&1
```

> Live (auto-updating) channels are a feature of the **web app's** in-process scheduler. From the
> CLI, a cron rebuild like the above is the equivalent — re-run the chain on whatever schedule you like.

---

## Setting advanced config

Keys like the channel stream mode and the Tunarr group aren't in the setup wizard. Add them to
`config.json` by hand — they sit right alongside the connection settings:

```json
{
    "tunarr_url": "http://192.168.1.10:8000",
    "plex_url": "http://192.168.1.10:32400",
    "plex_token": "your-plex-token",

    "tunarr_stream_mode": "hls_direct_v2",
    "tunarr_channel_group": "Saturday Morning"
}
```

- **`tunarr_stream_mode`** — one of `hls`, `hls_slower`, `mpegts`, `hls_direct`, `hls_direct_v2`
  (default `hls`). Applied by `create.py` at channel creation.
- **`tunarr_channel_group`** — the Tunarr group/folder all created channels land in (default
  `tunarr`).
- **`channel_order`** — array of category keys controlling numbering order, e.g.
  `["marathon","tv_block","movie","franchise","specialty"]`. Omit for the default order. See
  [Channel Numbering](../README.md#channel-numbering).

> **One Plex server or several?** The example above uses the single-server `plex_url` + `plex_token`
> form (what the setup wizard writes). For multiple Plex servers, use a `plex_servers` array instead —
> see [`config.json.example`](../config.json.example). Either form works.

The full key list is in [`config.json.example`](../config.json.example) and the README's
[Advanced Configuration](../README.md#advanced-configuration) table.
