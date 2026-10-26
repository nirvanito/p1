# Location from the phone's GPS (GPSLogger)

Google Maps Timeline now lives on the phone and leaves it only when you
export it by hand ([location.md](location.md)). To get places every morning
with nothing to remember, the phone logs its own position to a file, the
file syncs to OneDrive, and the `gps_track` connector turns the points into
**visits**: a place, an arrival, a departure. These are the same
`place_visit` records Timeline produces, so trips, home, countries
([places.md](places.md)), on this day, prompts and `ask` read them with no
changes.

They land in the **observed tier**. They are bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. See
[observed-tier.md](observed-tier.md).

Steps marked **(verify)** come from the apps' documentation and source code,
not from a test on a real Galaxy. The checklist at the end collects them.

## The route, and why this one

```
GPSLogger ──one file a day──> Internal storage/GPSLogger
          ──Autosync for OneDrive (Upload only)──> OneDrive/GPSLogger
          ──OneDrive on the laptop──> lifecrm collect (06:30) ──> place_visit
```

- **GPSLogger for Android** (mendhak, open source, no account, no known
  trackers) writes a new file each day by default (`20261024.gpx`). It runs
  for weeks without attention once Samsung is told to leave it alone.
- **OneDrive is not one of GPSLogger's upload targets.** It can upload to
  Google Drive, Dropbox, FTP/SFTP, ownCloud/Nextcloud, OpenStreetMap, email
  or a URL. Any of those needs a second sync client and account on the
  laptop, and its uploads come zipped by default.
- **Autosync for OneDrive is already on the phone** for the voice inbox
  ([android.md](android.md)). It is signed in to the right Microsoft account
  and Samsung already leaves it running. The track folder is just a second
  folder pair in an app that already works. The laptop changes nothing: the
  OneDrive client it already runs brings the folder down.

The catch: Autosync's free tier has **one** folder pair, and the voice inbox
uses it. The second pair needs Autosync's paid upgrade (android.md already
lists it for call recordings and typed notes). The free alternative is
*FolderSync* (Tacit Dynamics). Its free tier has several folder pairs and
supports OneDrive **(verify)**, but it is another app to install, sign in
and exempt from battery saving.

## 1. GPSLogger on the phone

1. Install **GPSLogger** (`com.mendhak.gpslogger`). Use F-Droid or the
   app's GitHub releases page (version 137, August 2026); the Play Store
   listing may be older **(verify)**.
2. Open it and grant location **Allow all the time**, with **Use precise
   location** on. When it asks to **Ignore battery optimization**, allow it.
3. ≡ › **Logging details**:
   - **Log to GPX**: on (the default). Leave *Use GPX 1.1* as it is. Both
     versions are read.
   - **Log to CSV**: optional. The CSV has two things GPX lacks: each fix's
     accuracy in metres, and the zone the phone was in. With both on, the
     same fix in two files counts once.
   - **New file creation**: **Once a day** (the default).
   - **Save to folder**: tap the path and pick **Internal storage ›
     GPSLogger** (create it). When it asks, allow it to **manage all files**.
     The default folder (`Android/data/com.mendhak.gpslogger/files`) is
     hidden from other apps on Android 11 and later, so the sync app could
     not see it **(verify)**.
   - **Log time with timezone offset**: on. GPX then carries the local time,
     and a visit abroad is dated by the local day there, not your home day.
4. ≡ › **Performance**:
   - **Logging interval**: 60 seconds (the default; GPSLogger's own advice
     is 60 s or more for battery).
   - **Distance filter**: 0. A still phone must keep logging, or a long
     visit becomes a gap.
   - **Accuracy Filter**: 40 m (the default). Poor fixes are never written.
   - **Keep GPS on between fixes**: off (the default).
   - **Log GPS/GNSS locations** and **Log network locations**: on. Network
     fixes keep a visit going indoors, where GPS has no view of the sky.
   - **Start on bootup**: on, so a restart does not stop logging.
5. Press **Start logging**. Its notification stays while it logs. That is
   Android's rule for background location; leave the notification on.

**Battery.** If a day costs too much, GPSLogger has *Only log if there is
significant motion* with an *Extended no motion heartbeat interval*: a still
phone logs once per heartbeat instead of once a minute. Visits survive that.
A quiet stretch is part of a visit when the fixes on either side agree. But
an arrival can then read up to one heartbeat late, so keep the heartbeat at
15 minutes or less **(verify)** the battery difference on the Galaxy.

## 2. The sync: a second Autosync folder pair

Not needed when the phone is shown in the laptop's File Explorer (Windows
11, [android.md](android.md), "No sync app at all"): the connector reads
`GPSLogger` on the phone directly. Otherwise:

1. Autosync › **Create folder pair** **(verify)** the menu names:
   - Local folder: `GPSLogger` (Internal storage).
   - Remote folder: `GPSLogger` at the **top** of OneDrive. Create it with
     the folder-plus icon. The connector looks there, and one folder down
     (`OneDrive/Apps/GPSLogger` works too).
   - Sync method: **Upload Only**. *Never* use *Upload Then Delete*: it
     deletes today's file while GPSLogger is still writing to it, and the
     next upload replaces OneDrive's copy with the restarted file. Do not
     use *Upload Mirror* or *Two-Way* either. Both copy deletions across,
     so tidying the phone would empty the laptop's history.
2. Autosync re-uploads a file that changed, so today's growing file reaches
   OneDrive through the day. Wi-Fi only is fine. The morning job reads
   whatever arrived by 06:30.
3. The free tier's per-file size cap is no problem. A day at one fix a minute
   is under 400 KB of GPX.

## 3. Keep Samsung from stopping it

One UI stops background apps without saying so. A stopped GPSLogger looks
like a phone that went nowhere. Do this for **GPSLogger** (Autosync should
already be done):

- Settings › Apps › GPSLogger › Battery: **Unrestricted** **(verify)**.
- Settings › Battery › Background usage limits › **Never sleeping apps** ›
  add GPSLogger; also check it is not in *Sleeping* or *Deep sleeping apps*
  **(verify)** the path on your One UI version.
- Settings › Apps › GPSLogger: turn off **Pause app activity if unused** /
  *Remove permissions if app is unused* **(verify)**.
- Do not swipe GPSLogger away in Recents. Its lock option (*Keep open*)
  helps on some versions **(verify)**.

## 4. The laptop

Nothing to configure. In File Explorer, right-click **OneDrive › GPSLogger**
› **Always keep on this device** **(verify)**. The 06:30 job then never waits
for a download or finds a file it cannot fetch offline. If a file cannot be
read on a given morning (still downloading, or held open by OneDrive), it is
named in the report and read the next day.

The first morning the folder holds tracks, `lifecrm collect` asks once
(`/morning`, or `collect status`):

```
new on this computer: gps_track, 5 record(s) from 2026-10-24 to 2026-10-24, in C:\Users\you\OneDrive\GPSLogger — yes imports them: python -m lifecrm collect approve gps_track ...
```

`collect approve gps_track` shows the dry run and imports on yes. After
that, every morning brings the new visits in by itself.

By hand, dry-run first:

```bash
.venv/Scripts/python -m lifecrm observe gps_track
```

```
gps track: GPSLogger (folder, 2 file(s) read: 2 GPX; 1,113 point(s); newest fix 2026-10-25)
  visits span 2026-10-24..2026-10-24 — check the oldest against the day logging began
  place_visit: 5 new, 0 changed | 2026-10-24..2026-10-24 | e.g. 2026-10-24 03:05 Place at 30.2667, -97.7433, 235 min
  still open: the newest visit (since 2026-10-25T08:05Z) has no departure yet — its end moves forward as the file grows
  places: 3 grid cell(s) of about 150 m — link one to a place once with 'lifecrm handles link place_id:gps:<cell> --entity <id>'
  skipped: 1 × point with HDOP above 5 (a poor fix)
dry run — nothing written. Re-run with --apply to write.
```

(The sample is the 3 a.m. return home after midnight. It starts on the
25th by the clock, but before the 05:00 rollover, so it belongs to the
24th's journal day.)

With no path, `observe gps_track` and `collect` use `[connectors.gps_track]
path` from config.toml, else the one folder named `GPSLogger` (or
`GPSLogger for Android`) at the top of a OneDrive or one folder down. They
refuse to guess between two such folders: two phones, or an old copy,
would mix.

```toml
[connectors.gps_track]
path = "~/OneDrive/GPSLogger"
```

## What it reads

Every `.gpx`, `.csv` and `.geojson` file in the folder (subfolders too), and
the same inside a `.zip` (GPSLogger zips what it uploads itself):

| format | what is used |
|---|---|
| GPX 1.0 / 1.1 | `<trkpt lat lon>`, `<time>` (UTC `Z`, or local with its offset), `<hdop>` when the phone supplied one |
| CSV | `lat`, `lon`, `timestamp_ms` / `time`, `time_offset` (the phone's zone), `accuracy`, `hdop`. Comma, semicolon or tab delimiter; a decimal comma is fine |
| GeoJSON | Point `[lon, lat]`, `time_long` / `time`, `accuracy` |

KML and NMEA files are named in the report and not read. Files with the same
name (`20261024.gpx` and `20261024.csv`) are one day logged twice. A fix at
the same second is one fix, with its accuracy and zone taken from whichever
file has them.

## How visits are found

All points are taken in time order across every file. A night at home that
starts in yesterday's file and ends in today's is one visit.

| rule | value |
|---|---|
| a fix less accurate than this is dropped (CSV/GeoJSON `accuracy`) | 75 m |
| with no accuracy, a fix with HDOP above this is dropped | 5 |
| a visit: consecutive fixes all within this of their centre... | 100 m |
| ...spanning at least | 10 min |
| a silent gap is part of a visit when the fixes either side agree, up to | 12 h |
| fixes that wander off and come back within this are an excursion, dropped | 10 min |

- **A gap counts only when both sides agree.** Fixes stop (the phone in a
  drawer, Android's deep sleep, a heartbeat) and resume within 100 m of the
  visit, within 12 hours: still there. Resume somewhere else, or after more
  than 12 hours: the visit ends at the last fix before the gap. What
  happened inside the gap is unknown, and nothing is invented for it.
- **A GPS jump or stepping out does not split a visit.** An absence shorter
  than the shortest visit is dropped.
- **Decided as the points arrive, never by looking back.** Re-reading
  today's file after it has grown finds the same visits with the same
  starts. Only the last visit can still change.

## What gets imported

**One `place_visit` observation per visit.**

| field | what |
|---|---|
| title | `Place at <lat>, <lon>`, the words location.py uses for a visit with no name. GPS knows no names; `lifecrm places` names the nearest town from the gazetteer |
| started_at / ended_at | the first and last fix of the visit, UTC |
| utc_offset_sec | the offset the file wrote with the first fix (CSV always; GPX with *Log time with timezone offset*) |
| journal_date | the local date in that zone, rollover applied there; without an offset, your home timezone. A visit starting at 01:05 belongs to the evening before |
| value / unit | minutes at the place |
| value_json | `lat`, `lon` (the centre of the visit's fixes), `place_id` (its grid cell). These are a subset of location.py's keys, with the same meaning |
| participants | the cell as a `place_id` handle `gps:<geohash>`, role `location` |
| external_id | `gps@<start>` |

**Not imported:** the points themselves. They stay in your OneDrive and on
the phone, and only the visits come in.

## Re-reading, and what wins

A visit's identity is its start, which never moves as a file grows. Every
record's `last_modified_at` is the newest fix the reading saw, as location.py
does with an export's as-of:

- **The visit still open** when the file ends (you are still there) is
  imported with its end so far. The next morning's longer file extends it.
  That is one audited update; nothing is added twice.
- **Finished visits** compare identical, so the as-of only moves forward
  ("re-dated to the newest fix").
- **An older reading** (a stale copy of the folder) overwrites nothing. A
  difference shows as a conflict and the stored visit is kept.

## Nothing is mirrored, and no coverage

A track file deleted from the folder is not a visit that never happened.
The archive remembers, as with location.py. And a day with no visits is a
day the phone did not log, not a day you went nowhere. No day is ever
declared complete: a missing row is unknown.

## Attribution: link a place once

GPS has no place ids, so each visit carries its **grid cell**: the geohash
of its centre, a cell about 150 m tall (less wide away from the equator).
The same place gives the same cell every day. Link it to a place entity once
and every visit there, past and future, is attributed to it:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link place_id:gps:9v6kpvc --entity 12
```

Paste the `lat,lon` from `value_json` into a map to see which place a cell
is. A place near a cell's edge can show up as two cells over time; link
both.

## Freshness

`lifecrm doctor` warns (`gps_track_data_age`), and the morning nudges say so,
when the newest visit started more than 7 days ago. That is the sign
GPSLogger or the sync stopped; Samsung's battery saver is the usual cause.
The check starts once gps_track has an import of its own, and then measures
the newest `place_visit` from any source, Timeline imports included — a
Timeline export on its own never trips it.

## Known limitations

- **Arrival and departure are fixes, not doorways.** A visit starts at the
  first fix within 100 m and ends at the last one. With a one-minute
  interval that is within a minute or two. With the significant-motion
  option it can be a heartbeat late.
- **Two places closer than 100 m** (a café next door to the gym) can read as
  one visit.
- **Timeline and GPS for the same days** are two sources: a visit appears
  once from each. Trips, home and countries count days, so they are
  unaffected. For the days both cover, prefer one: GPS from the day it
  starts, Timeline exports for the years before.
- **A file that reaches the folder days late** (a sync that stalled) can
  change how the days around it read. A visit the later reading splits or
  drops stays as first imported. Retract that import if it matters.
- **Every morning reads every file.** A year at one fix a minute is about
  half a million points in 70 MB of GPX. On the development laptop that
  reads in about 4 seconds, one day's file at a time, never all held at
  once.

## First-real-import checklist (unproven until a real phone run)

Everything above is tested against files written in GPSLogger's formats as
its source code writes them, not against a real phone's files. The first
real dry run settles the rest:

| check | where to look |
|---|---|
| [ ] GPSLogger can save to Internal storage/GPSLogger after *manage all files* | the files in My Files |
| [ ] Autosync (or FolderSync) can see that folder and re-uploads the growing file | OneDrive on the web: today's file size grows |
| [ ] The folder is found with no config | `observe gps_track` prints `gps track: GPSLogger (folder, …` |
| [ ] GPX times: `Z`, or an offset with *Log time with timezone offset* | the sample time in the report matches your clock |
| [ ] Visits look right for a day you remember: arrival, departure, the centre on a map | `place_visit: … e.g.` and `--json` |
| [ ] Indoors: does a visit hold, or break into pieces? | several visits at one cell on one day means fixes went missing |
| [ ] Battery cost of 60 s logging on the Galaxy, and of the significant-motion option | Settings › Battery |
| [ ] GPSLogger survives a night and a restart with the settings above | no gap in the next day's file |
| [ ] Samsung menu paths above, on your One UI version | |
| [ ] Autosync's sync method names and the paid upgrade for a second pair | the app |

## Commands

```bash
.venv/Scripts/python -m lifecrm observe gps_track [<folder | file.gpx | file.csv | file.geojson | file.zip>]
.venv/Scripts/python -m lifecrm observe gps_track <target> --apply
.venv/Scripts/python -m lifecrm collect approve gps_track
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles link place_id:gps:<cell> --entity <id>
```
