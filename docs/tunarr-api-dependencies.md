# Tunarr API surface Programmarr depends on

Every Tunarr endpoint Programmarr calls, what it uses each one for, and the specific
response fields it reads. Written for two audiences:

- **Tunarr maintainers**, as a concrete answer to "what would a breaking change break?"
  Anything on this list is load-bearing for at least one downstream tool.
- **Programmarr contributors**, as the list of places an upstream change can bite us.

Verified against **Tunarr 1.3.13**. Programmarr does not gate on a version number — it
probes for the endpoints it needs and reports what's missing, because a missing endpoint
is directly observable and a minimum-version guess isn't.

## Endpoints

| Method | Endpoint | Used for | Response fields read |
|---|---|---|---|
| GET | `/api/version` | Diagnostics; shown in the UI and asked for in bug reports | `tunarr` |
| GET | `/api/media-sources` | Find Plex sources and their libraries | `type`, `name`, `uri`, `libraries[].id`, `libraries[].name`, `libraries[].mediaType`, `libraries[].enabled` |
| GET | `/api/media-libraries/{id}/programs` | Index the whole library — the core read. Paged (`{total, result}`, max 1000/page) since Tunarr #2423 (2026.11); `channel_engine.get_library_programs` reads every page and still accepts the old bare list | `id`, `program.title`, `program.show.uuid`, `program.show.title`, `program.showId`, `program.state`, `program.releaseDate` |
| GET | `/api/transcode_configs` | Pick a transcode config when creating channels | `id`, `name` |
| GET | `/api/channels` | List channels; look up by number | `id`, `number`, `name` |
| GET | `/api/channels/{id}` | Read a full channel to edit its icon | whole object (round-tripped) |
| POST | `/api/channels` | Create a channel | `id` |
| PUT | `/api/channels/{id}` | Set/clear a channel icon | whole object (round-tripped) |
| DELETE | `/api/channels/{id}` | Remove channels on a wipe-and-rebuild deploy | — |
| GET | `/api/channels/{id}/programming` | Read the current lineup — drives change detection and pre-delete backups | `programs` (dict keyed by program id), `lineup[].type`, `lineup[].id` |
| POST | `/api/channels/{id}/programming` | Write a channel's schedule | — |
| POST | `/api/upload/image` | Upload generated channel badge art | `fileUrl` |
| GET | `/api/filler-lists` | Populate the commercials picker | `id`, `name`, `contentCount` |
| GET | `/api/xmltv.xml` | Plex DVR sync and the guide grid | channel ids of the form `C{number}.{...}.tunarr.com` |

## Request payload shapes we depend on

Two request bodies are effectively a contract. If either changes shape, Programmarr
writes a channel that looks fine and plays nothing.

**`POST /api/channels/{id}/programming`** — the schedule payload
(`channel_engine.build_schedule`):

```json
{
  "type": "random",
  "programs": ["<program id>", "..."],
  "schedule": {
    "type": "random",
    "flexPreference": "end",
    "maxDays": 30,
    "padMs": 0,
    "padStyle": "episode",
    "randomDistribution": "uniform",
    "slots": [{ "type": "show|movie", "order": "next|chronological", "weight": 1 }]
  }
}
```

`padMs` opens the gaps that `fillerCollections` fills at playback — that's how
Programmarr does commercials between shows.

**`POST /api/channels`** — the create body (`create.py:create_channel`) sets
`transcodeConfigId`, `streamMode`, `groupTitle`, `guideMinimumDuration`,
`fillerRepeatCooldown`, `fillerCollections`, `disableFillerOverlay`, `stealth`,
`subtitlesEnabled`, `icon`, `offline`, `watermark`, `onDemand`.

## Behaviours we rely on, beyond the shapes

These aren't endpoints, but a change to any of them would be just as breaking:

- **`program.state == "missing"` marks unplayable content** rather than removing it.
  Programmarr counts non-missing programs to pick between duplicate copies of a show
  across libraries, so a dead duplicate can't shadow the real one.
- **A channel's Tunarr `id` is stable across programming updates.** Auto-updating
  channels patch programming in place specifically so the id survives — recreating a
  channel breaks the Plex DVR mapping that points at it.
- **`programs` in the programming response is keyed by program id**, and those ids match
  the ones from `/api/media-libraries/{id}/programs`. That shared id-space is what makes
  change detection possible without a state file.
- **XMLTV channel ids encode the channel number** as the first dotted segment
  (`C10.97.tunarr.com` → channel 10). Plex DVR sync parses this.

## What would help most

Ranked by how much breakage it would prevent, not by effort:

1. **A stable id-space between `/programs` and `/channels/{id}/programming`.** More load-bearing than any single endpoint — it's what lets a tool diff desired against actual without keeping its own state.
2. **Advance notice on the programming payload shape.** A silent change here fails quietly: the channel is created, the write is accepted, nothing plays.
3. **`/api/media-sources` and `/api/media-libraries/{id}/programs` staying stable**, or changing behind a version. Everything else is recoverable; without these there's no library to read.
4. **Anything version-ish on responses** — a header, a field, `/api/version` semantics that are safe to compare — so downstream tools can adapt instead of guessing.

Not asking for a frozen API. Knowing which parts are intentionally stable versus
in-flux is worth more than stability itself.
