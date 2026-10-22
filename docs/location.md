# Location history (Google Maps Timeline)

Where a day happened is the cheapest strong evidence there is: "the lake in
Slovenia four years ago", "how often did I actually make it to the gym",
"when was I last at Veracruz". Google Maps Timeline has already turned the
phone's raw positions into **visits** — a place, an arrival, a departure —
and the `location` connector imports exactly those.

They land in the **observed tier**: bulk records that never pass the `/log`
review gate, are never asserted as fact, and are always cited. Nothing here
can touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md).

## Getting the export

**Android (the Samsung, combo 1).** Timeline lives on the phone now, not in
Google's cloud, so the export is made on the phone:

1. Settings → Location → Location services → **Timeline** →
   **Export Timeline data** (on some phones: Google Maps → your profile
   picture → Your Timeline → ⋮ → Location & privacy settings → Export
   Timeline data).
2. Save `Timeline.json` somewhere the laptop can see — the OneDrive folder,
   or Downloads and then Quick Share / USB.

**iPhone (combo 2).** Apple's own Significant Locations are encrypted on the
phone and **cannot be exported at all** — there is nothing for this connector
to read, and the demo's iPhone combo has no location sample for that reason.
The one exception: if Google Maps on the iPhone has Timeline turned on, its
export (Google Maps → profile → Your Timeline → ⋯ → Location & privacy
settings → Export Timeline data) is a `location-history.json` that this
connector reads the same way.

**Older history (any phone).** Before Timeline moved onto the phone, Google
Takeout's *Location History (Timeline)* held it as
`Semantic Location History/<year>/<YEAR>_<MONTH>.json`. If you have an old
Takeout, import the `.zip` or the unzipped folder; it is often the only
copy of years the phone no longer holds.

## Importing

Dry-run first — it is the review step:

```bash
.venv/Scripts/python -m lifecrm observe location Timeline.json
```

```
location history: Timeline.json (Google Maps Timeline, on-device export, 1 file(s) read; as of 2026-10-21)
  visits span 2022-09-26..2026-10-20 — check the oldest against what Timeline shows
  place_visit: 44 new, 0 changed | 2022-09-26..2026-10-20 | e.g. 2026-10-20 20:34 Home, 176 min
  places: 13 place id(s), 2 named by the export — link one to a place once with 'lifecrm handles link place_id:<id> --entity <id>'
  skipped: 40 × activity segment (travel between places)
  skipped: 3 × raw signal (position, wifi or activity reading)
  skipped: 40 × timelinePath segment (route points)
dry run — nothing written. Re-run with --apply to write.
```

Read the span and the sample. A wrong year or a visit at 3 a.m. that should
be at 9 p.m. means stop and look. Then add `--apply`. The command prints an
import uuid; `lifecrm imports retract <uuid>` undoes the whole run.

With no path, `observe location` uses `[connectors.location] path` from
config.toml, else the one `Timeline.json` / `location-history.json` it finds
in Downloads, a OneDrive folder or (on a Mac) iCloud Drive. If it finds
several it lists them and asks — each export is the whole timeline as of its
own day, and guessing which one you meant is worse than asking.

```toml
[connectors.location]
path = "~/OneDrive/Timeline.json"
```

Accepted targets: `Timeline.json` / `location-history.json` (any name, if
passed directly), a Takeout `.zip`, or a folder. Inside a folder or a zip
only the timeline files are parsed; `Records.json` (raw pings — it can run to
gigabytes), `Settings.json` and other files are named in the report as not
read.

## What gets imported

**One `place_visit` observation per visit.**

| field | what |
|---|---|
| title | the place name (Takeout only); otherwise *Home* / *Work* where Google labelled the place, else the address, else `Place at <lat>, <lon>` |
| body | the address, when the export has one (Takeout) |
| started_at / ended_at | the visit, UTC |
| utc_offset_sec | the zone the export wrote with the visit |
| value / unit | minutes at the place |
| value_json | `lat`, `lon`, `place_id`, `semantic_type` (HOME, WORK, INFERRED_HOME, …), `address`, `probability` (0..1), `hierarchy_level` (a visit nested inside another), `confirmed` (you confirmed it in Timeline) |
| participants | the place as a `place_id` handle, role `location` |

**Not imported, counted in the report:** activity segments (travel between
places), timelinePath route points, raw signals, trip summaries, and
Takeout's `Records.json`. They are positions, not places; storing every GPS
fix would bury the visits they add up to.

**The day a visit belongs to** is its local date where the export says the
zone — a morning in Slovenia is that Slovenian morning, with the configured
rollover hour applied in that zone. The on-device exports always say;
Takeout writes UTC only, so a Takeout visit is dated in your home timezone,
as every other connector without a zone is.

## Re-importing, and what wins

Identity is `<placeId>@<start>` — the same in both formats, so a visit read
from an old Takeout and again from today's device export is one record.

The exports carry no per-visit edit time, so every visit's
`last_modified_at` is the export's own **as-of** moment (the latest instant
it describes). That makes the newer export win:

- **identical** — skipped. A newer export moves an unchanged visit's as-of
  forward silently ("re-dated to this export" in the report).
- **update** — the visit changed and this export is newer: a visit still in
  progress when the last export was taken gets its real end; a visit Google
  re-inferred gets the new reading. Audit-logged with the old values.
- **conflict** — the visit changed but this export is *older* than the one
  already imported. Never applied; the report counts it.

## Nothing is mirrored

A visit missing from a later export is **kept**. Timeline's auto-delete, or
an export that reaches back less far, is not you deleting a visit — the
archive remembers. (Texts and AI chats mirror their source by the owner's
decision of 2026-09-24; location history deliberately does not.)

## Attribution: link a place once

The connector emits **handles, never names**: each Google place id is a
`place_id` handle. Link it to a place entity once and every visit there —
past and future — is attributed to it, with no backfill pass:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link place_id:ChIJ... --entity 12
```

Takeout handles carry the place's name as their `display_name`, so
`--suggest` can offer the matching entity; device-export handles are named
only *Home* / *Work*, so for those use the position in `value_json` (paste
`lat,lon` into a map) to see which place it is.

## Known limitations

- **The on-device export names no place.** Google removed names and
  addresses from it; only the place id, position and HOME / WORK labels
  remain. Reverse geocoding is a derived layer, deliberately not done at
  import time.
- **A visit you correct in Timeline after importing it** arrives as a new
  visit (the place is part of its identity) and the old one stays. Retract
  the import or ignore the old place's handle.
- **Format details are from published descriptions, not a captured Samsung
  export.** The reader accepts every spelling seen in the wild (`latLng`
  strings with degree signs, `geo:` strings, `placeId`/`placeID`, numbers as
  strings, `latitudeE7`, `startTimestampMs`). Check the first real dry run:
  the span, the sample time, and the skipped counts.
- **Memory.** A device export is parsed whole; a decade of Timeline is a
  few hundred MB of RAM for the run. Takeout months are read one at a time.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe location [<Timeline.json | takeout.zip | folder>]
.venv/Scripts/python -m lifecrm observe location <target> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link place_id:<id> --entity <id>
```
