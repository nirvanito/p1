# Last.fm: your listening, collected every morning

What you listened to is a diary kept without trying (docs/spotify.md says
why it is worth keeping). Spotify will not hand it over by itself: its API
returns only the last 50 plays, and the whole history is a download you
request by hand and get days later. The automatic route is **Spotify
scrobbling to Last.fm**: connected once on last.fm, Spotify sends every
song you play there, on any device, and each morning `lifecrm collect`
fetches the new scrobbles from Last.fm's API (`lifecrm/online/lastfm.py`)
and imports them with the `lastfm` connector (`lifecrm/connectors/lastfm.py`)
into the same `music_play` records Spotify's download makes.

Records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. See
[docs/observed-tier.md](observed-tier.md). The first import waits for your
yes, like every source's (docs/collect.md).

## Setting it up (once)

1. **A Last.fm account** — free, at last.fm (sign up if you have none).
2. **Spotify → Last.fm.** On last.fm: **Settings › Applications › Spotify
   Scrobbling › Connect**, and allow access to your Spotify account. From
   then on Spotify scrobbles what you play in any Spotify app; there is no
   setting for this on Spotify's side any more. A free Spotify account
   works too. Check it: play a song, and it shows on your Last.fm profile
   within a few minutes.
3. **Recent listening visible.** **Settings › Privacy**: leave *Hide recent
   listening information* unticked. With it ticked, Last.fm's API answers
   error 17 and nothing can be fetched.
4. **An API key.** Signed in, open **last.fm/api/account/create**, give
   the application any name (`lifecrm`), leave the callback and homepage
   empty, and submit. Copy the **API key** it shows — not the *shared
   secret*, which is never needed here. Your keys are listed again at
   last.fm/api/accounts.
5. **Connect** in a terminal at the project root:

   ```bash
   .venv/Scripts/python -m lifecrm connect lastfm <your username>
   ```

   The username is the end of your profile's address,
   `last.fm/user/<username>` (pasting that address works too). The key is
   asked for, hidden as you paste it, and never taken on the command line
   — it would stay in the shell's history. Both are checked now (an unknown
   user, a wrong key or hidden listening is said here, not at 06:30), and
   kept in `data/secrets.json`, never in config.toml, never printed.

The next morning collect fetches it and `/morning` asks before the first
import (`python -m lifecrm collect approve lastfm` shows the dry run and
imports on yes). `python -m lifecrm collect setup` lists it under
*Online* until it is connected.

## What a morning fetches

Reading a user's scrobbles needs only the API key — no password, no
sign-in, nothing that can write to the account. Each morning:

- **New scrobbles** — everything from the start of the day that was in
  progress at the last fetch (or seven days back, if that is earlier: a
  phone that was offline scrobbles late, stamped with when it played) up
  to now, every page of it. Yesterday's evening is therefore fetched again
  whole. Nothing is saved unless a day in that span differs from what was
  fetched before (a short digest per day is kept with the cursor), and then
  only from the first day that changed. **A quiet morning saves nothing**:
  collect records `unchanged` and writes nothing.
- **History (the backfill)** — newest first, 50 pages of 200 a morning
  (10,000 scrobbles) before the oldest day fetched so far, until the pages
  run out at your first scrobble. A 100,000-scrobble history arrives over
  ten mornings; no morning's job runs for an hour. Each batch is **cut at a
  day boundary**: the oldest day a batch only reached into is fetched whole
  the next morning, so no day is ever imported short (a single day longer
  than the batch is read to its start first). The dry run says how far it
  got: `history: fetched back to 2019-03-02; 31,200 older scrobble(s) still
  to come`, and later `history: complete`.

One request at a time, half a second between pages, an identifying
User-Agent (Last.fm asks for one). A page that fails is tried once more
after ten seconds when the failure is worth retrying (HTTP 5xx, a network
error, or Last.fm's errors 8, 11, 16 and 29 — "try again", offline,
temporary, rate limit); then it is one line:

| said | what to do |
|---|---|
| `Last.fm refused the API key (error 10 …)` | copy the key again from last.fm/api/accounts, then `connect lastfm <username>` again |
| `Last.fm has no such user (error 6 …)` | the username from your profile address; connect again |
| `Last.fm hides this user's listening (error 17 …)` | untick *Hide recent listening information* (Settings › Privacy) |
| `Last.fm says too many requests (error 29 …)` | nothing; the next run carries on |
| `could not reach Last.fm (…)` | nothing; the next run tries again |

A failure in the **new** part saves nothing new that morning (the morning
is an `error` in `collect status`, never a silent skip). A failure in the
**history** part keeps the whole days it got, says the failure in the dry
run (`this morning's batch stopped: …`), and the next morning carries on
from there. Reconnecting the same user (a new key) keeps how far the
fetches got.

## Where the pages are

```
data/fetched/lastfm/
  pending/    pages fetched and not yet imported — what collect reads
  archive/    pages a committed import has read: kept, never deleted
```

A page is one `user.getRecentTracks` answer, kept whole under `response`,
in a small wrapper naming when it was fetched and its **window** — the
whole days it promises to hold (the connector's docstring has the format;
the API key is never in a page). Pages wait in `pending/` until an import
that read them is committed — through a held first import, too, so the yes
still finds everything — then move to `archive/`. Either folder, or one
page, can be read by hand:

```bash
.venv/Scripts/python -m lifecrm observe lastfm data/fetched/lastfm/archive
```

`observe lastfm` with no path fetches first, like the morning does.
`observe lastfm --apply` imports without moving the pages to `archive/`;
the next morning reads them again (identical, nothing written) and moves
them then. A bare API answer saved by hand (`format=json`) reads too.

## What gets imported: one record per track per day

Exactly the shape of Spotify's download (docs/spotify.md), so the
dashboards, `ask`, the review and the web pages read both the same way:

| field | what |
|---|---|
| title | the track |
| body | the artist |
| started_at / ended_at | the day's first and last scrobble, UTC (each the moment a play **began**) |
| value / unit | none — see below |
| value_json | `plays` (scrobbles that day), `first_play`, `last_play`, `album` (when Last.fm has one), `kind` (`music`) |
| origin_app | `lastfm` |
| external_id | sha1 of `lastfm`, artist, track and day — case- and space-insensitive |
| participants | none — artists are not people |

**Dating.** Last.fm stamps a scrobble with the time the track **started**
(its `track.scrobble` docs), and that dates it — home timezone, the
configured rollover applied, so a 1 a.m. song counts toward the evening
before, as Spotify's plays do. Travel is dated in the home timezone.

**A day fetched twice** (yesterday's evening, a late scrobble): in one
read, each day is taken from the newest fetch that covers it — never a
union of two fetches, so a scrobble deleted on Last.fm in between is gone
from that day's count. A record's `last_modified_at` is the fetch it came
from, so a later fetch of a day is always the newer record (an update, with
the old values audited), and an unchanged day only moves that stamp.

**Remembers.** Nothing is mirrored: a scrobble deleted on Last.fm, or a
whole day emptied, stays here. No coverage is declared: a day with no
scrobbles is unknown, not a known zero (a play Spotify failed to scrobble
is still a play).

## What a Last.fm record cannot know (and Spotify's download does)

- **Minutes.** Last.fm records *that* a track was played, never for how
  long. `value` and `unit` stay empty — nothing is estimated from track
  lengths. Spotify's download fills them with minutes played.
- **Skips.** Never scrobbled at all: a track counts once it has played half
  its length or four minutes (and is over 30 seconds long), so `plays` is
  close to Spotify's own 30-second count but not the same. `skipped` is
  absent, as it is in Spotify's Account data.
- **The play's end.** `last_play` is when the day's last play began;
  Spotify's is when it ended.
- **URIs, podcasts, audiobooks, video.** No Spotify URI (`uri`), and every
  record is `kind: music` — a scrobble is a song to Last.fm, and Spotify's
  scrobbler has been reported to send music only, not podcast episodes.
  No `chapter`, no `video`.
- **Which app.** A scrobble does not say which player sent it: anything
  else scrobbling to the account (another player, a record-player app) is
  imported as listening too.

## With a Spotify download as well

Importing your Spotify download (docs/spotify.md) for days Last.fm also
covers gives **two records for the same track-day** — ids never shared, so
neither overwrites the other: Spotify's with minutes, Last.fm's with plays.
What that means for each reader of `music_play`:

- **`ask`** (`activity_total`) totals per stream *and unit*: Spotify's rows
  (`min`) and Last.fm's (no unit) are two separate "listening records"
  lines, each right on its own; the same listening is in both, so the two
  lines must not be added. A search lists both rows of a track-day.
- **Dashboards** (*Listening per week*): minutes are Spotify's only
  (Last.fm adds 0), but `tracks` counts both rows on weeks both cover. With
  Last.fm alone the minutes bars are empty — the chart is minutes.
- **The review** counts observed records per stream and day: overlapping
  days count twice.

So the cleanest archive takes one source per stretch of time: Last.fm from
now on, and a Spotify download only for the years before scrobbling was
connected. `lifecrm imports retract <uuid>` undoes a run either way.

## When scrobbling stops

Spotify's link to Last.fm can end without a word (a password change, a
revoked app); the mornings would then just be quiet. So the connector is
expected to stay fresh: once it has an import of its own, `lifecrm doctor`
warns (`lastfm_data_age`) and `/morning` says so when the newest play is over
14 days old. A fortnight without music is a false alarm — `nudges dismiss
stale:lastfm`.

## Disconnecting

```bash
.venv/Scripts/python -m lifecrm disconnect lastfm
```

forgets the username, key and cursor; what was fetched stays in
`data/fetched/lastfm/`, and what was imported stays imported. Disconnect
Spotify on last.fm (Settings › Applications) to stop the scrobbling itself,
and delete the API account at last.fm/api/accounts if you made it only for
this.

## First real import checklist

- [ ] `collect approve lastfm` (no `--yes`): the user is yours, the span
      starts about a week back (the history then comes a batch a morning).
- [ ] The top artists look like your listening.
- [ ] A late-night song is on the evening before (rollover) — `ask "what
      did I listen to yesterday"` after the import.
- [ ] `skipped:` lines are only now-playing tracks and edge days; `not
      understood:` is empty.
- [ ] The morning after: `collect status` says `lastfm applied` or
      `unchanged`, never an error.

## Not verified

Built from Last.fm's API pages (user.getRecentTracks, user.getInfo,
track.scrobble, the error codes) and tested against a fake API, never
against Last.fm itself:

- whether `from` and `to` include their own second (the fetcher asks one
  second early and clips each window exactly, so either way is safe);
- that the now-playing track is flagged `@attr.nowplaying` in JSON as the
  page describes for XML, and that a lone track comes as an object rather
  than a list (both are handled);
- the menu paths (Settings › Applications › Spotify Scrobbling; Settings ›
  Privacy › *Hide recent listening information*), the API-account form, and
  that error 17 is what hidden listening returns;
- that Spotify scrobbles music only, how late it sends offline plays (seven
  days are looked back over each morning), and whether a private session
  is scrobbled — reports differ;
- Last.fm's rate limit: no figure is published; half a second between
  pages has not been tested at the limit.
