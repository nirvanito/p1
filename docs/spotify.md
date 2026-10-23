# Spotify listening history

What you listened to is a diary kept without trying — the focus music on the
day the board deck was due, the album behind a concert-ticket gift idea, the
travel podcast the week a trip took shape. Spotify hands all of it over on
request. The `spotify` connector reads those downloads on the laptop. It never
logs in, never calls Spotify's API, and never sends anything.

Records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. See
[docs/observed-tier.md](observed-tier.md).

## Getting the downloads

The request is made on Spotify's **website**, the same for both combos (the
Samsung and the iPhone apps only link there):

1. Sign in at spotify.com → profile menu → **Account** → **Privacy settings**
   (spotify.com/account/privacy).
2. Under **Download your data**, tick what to request and press
   **Request data**, then confirm from the email Spotify sends:
   - **Account data** — ready in about **5 days**; listening history covers
     roughly the **last year**, to the minute.
   - **Extended streaming history** — ready in up to **30 days**; the
     account's **whole life**, to the second, with Spotify URIs, albums and
     skips.
3. Each arrives as an emailed link to `my_spotify_data.zip`. Save both into
   one folder — the second download becomes `my_spotify_data (1).zip`.

Inside, the files this connector reads:

| download | folder in the zip | files |
|---|---|---|
| Account data | `Spotify Account Data/` | `StreamingHistory_music_N.json`, `StreamingHistory_podcast_N.json` (older downloads: one `StreamingHistoryN.json`), `Userdata.json` (username only) |
| Extended streaming history | `Spotify Extended Streaming History/` | `Streaming_History_Audio_<years>_N.json`, `Streaming_History_Video_<years>.json` (downloads from before 2023: `endsong_N.json`) |

Everything else in the zips (library, playlists, searches, inferences,
payments) is listed as "not read" and never opened.

**Not verified against a real download:** the website's menu labels, the
waiting times, and the exact file names are as Spotify documented them;
the field names were checked against Spotify's published description of
the extended history. The podcast file's `podcastName`/`episodeName` and the
pre-2023 `endsong_N.json` names come from secondary sources. The first real
dry run will show whether every file was read (see the checklist below).

## Importing

Dry-run first, on the folder holding both zips (or one zip, or one `.json`):

```bash
.venv/Scripts/python -m lifecrm observe spotify Downloads/Spotify
```

```
spotify: Spotify (Account data: 2 file(s), 29 play(s); extended streaming history: 1 file(s), 11 play(s); account alexrivera94)
  plays span 2022-09-23..2026-10-20 — Account data reaches back about a year, the extended history to the account's first play
  listened: 2.3 h of music, 2.2 h of podcast | top: The Slow Traveler (2.2 h), Japanese Breakfast (22.6 min), Tycho (18.9 min), Bon Iver (16.8 min), Nils Frahm (16.6 min)
  music_play: 31 new, 0 changed | 2022-09-23..2026-10-20 | e.g. 2026-10-20 Ólafur Arnalds — saman, 1 play(s), 3.2 min
  skipped: 4 × Account-data play on a day the extended history also covers (the extended history is the fuller record of that day)
  skipped: 2 × play under 30 seconds (a skip or a preview — Spotify itself counts a stream from 30 s)
  not read: 1 other file(s) of the download (library, playlists, searches — not listening history), e.g. YourLibrary.json
  never read: the IP address, country, device and user agent of each play (docs/spotify.md, Privacy)
dry run — nothing written. Re-run with --apply to write.
```

Check the span and the top artists, then add `--apply`. `lifecrm imports
retract <uuid>` undoes a run. With no path, `observe spotify` takes
`[connectors.spotify] path`, else the one `my_spotify_data*.zip` in
Downloads (or OneDrive / iCloud Drive); several are refused — put them in one
folder and pass the folder.

```toml
[connectors.spotify]
path = "~/Downloads/Spotify"
```

## Privacy: what is never read

Every extended-history play also carries the **IP address** it was played
from, the **country**, the device's **user agent** and **platform**, and
offline timestamps. None of it is kept — not in a record, not in
`value_json`, not in the report. A play is read for its time, length, names,
URI, album, the skipped flag and the private-session flag (and, in older
downloads that carry it, the username), nothing else.
`Userdata.json` also holds the account's email, birth date, postal code and
phone number: only its `username` is read, to label the import.

## What gets imported: one record per track per day

A decade of listening is hundreds of thousands of plays; "what was I
listening to the week of the board meeting" needs the track and the day, not
every repeat. Each `music_play` observation is **one track (or podcast
episode, or audiobook) on one journal day** — home timezone, the configured
rollover applied, so a 1 a.m. play counts toward the evening before:

| field | what |
|---|---|
| title | the track, episode or audiobook |
| body | the artist or show (an audiobook's author is not in the export) |
| started_at / ended_at | first play's start, last play's end, UTC |
| value / unit | minutes played that day, `min` |
| value_json | `plays`, `skipped` (plays Spotify marks skipped — extended history only), `first_play`, `last_play`, `album`, `uri`, `kind` (`music` / `podcast` / `audiobook`; absent for the old mixed `StreamingHistoryN.json`, which cannot tell), `video` (from a `_Video_` file), `chapter` (an audiobook's last chapter that day) |
| external_id | sha1 of artist/show, track/episode and day — case- and space-insensitive |
| participants | none |

**Times.** Spotify records when a play *ended* (Account data to the minute,
the extended history to the second). A play's start is that minus the time
played, and **the start dates it** — a podcast begun at 04:50 and finished
after the 05:00 rollover belongs to the evening before.

**Plays under 30 seconds** are dropped and counted: skips and previews, and
Spotify itself only counts a stream from 30 s. A private session's plays
are imported like any other — it hides listening from followers, not from
you — and counted in the report.

**Identity is the names, not the URI.** Account data has no URIs, and both
downloads cover the last year: keying on the URI would count the same
listening twice. The URI is kept in `value_json`. One consequence: a song
re-released under the same artist and title is the same record on a day both
were played.

**No handles.** Artists and shows are not people you know; a handle is for
someone the owner might link to an entity. Full-text search finds an artist
in `body`.

## The two downloads together

- **In one folder (recommended):** the extended history wins. For every
  journal day it covers, that day's Account-data plays are dropped and
  counted ("Account-data play on a day the extended history also covers") —
  the extended history has the same plays with seconds, URIs and skips.
- **One after the other:** the same track-day is the same record. An
  extended play ends at the second and Account data's at the minute, so the
  extended record is almost always newer and replaces the Account-data one
  (audited). Account data imported *after* the extended history is never
  newer: the fuller record is kept, and the report counts those on one line
  ("kept existing: N track-day(s) the other Spotify download already
  stored").
- **Two overlapping downloads of the same kind** (Account data requested a
  month apart) in one folder: a play listed in both counts once.

## Re-importing, and what wins

A record's `last_modified_at` is its last play. Today's record grows while
you keep listening and the next import updates it; earlier days are
identical on every re-import and skipped.

## Nothing is mirrored

Account data only reaches back about a year. A play missing from a later
download has aged out of the window, not been deleted, so the archive
remembers what it imported. Re-request Account data every few months to keep
the whole of it; the extended history fills in everything older.

## Known limitations

- **Plays abroad land on the home-timezone day.** The export says when, in
  UTC, not where you were (the country it carries is deliberately never
  read). A morning play in Slovenia can be dated to the evening before.
- **One archive is one listener.** Ids do not name the account (Account
  data's history files do not either). A folder holding two accounts'
  downloads is refused; a second person's download imported on its own
  would merge into the same track-days.
- **Account data's start times are approximate** — to the minute, so a
  play's start can be up to 59 seconds early.
- **The old mixed `StreamingHistoryN.json`** carries no kind: podcasts there
  look like music, with the show as the artist.

## First real import — checklist

1. Request both downloads; save both zips into one folder.
2. Dry run the folder. Every history file should be counted in the first
   line; "not understood" should be empty — if it lists a play of an unknown
   shape, Spotify has changed its format: stop and report the keys it names.
3. Check the span: Account data alone should reach back about a year; with
   the extended history, to the account's first play.
4. Spot-check one day you remember (the top artists, a podcast) against
   Spotify's own Recently played.
5. `--apply`, then run it again: everything identical.
6. `lifecrm doctor` stays green.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe spotify [<my_spotify_data.zip | folder | history .json>]
.venv/Scripts/python -m lifecrm observe spotify <target> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```
