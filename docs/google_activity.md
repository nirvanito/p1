# Google activity (Search, YouTube, Maps — via Google Takeout)

What you looked up is a record of what you were planning and worrying about:
"how to renew passport usps" the evening it went on the list, "knee surgery
recovery time" after a call with Mom, "uchi austin reservations" before a
birthday, "lake bled pletna boat" on the trip four years ago. Google keeps
every search and every YouTube watch for as long as its auto-delete setting
lets it, and Google Takeout hands them over as files. The `google_activity`
connector reads those files. It never talks to Google.

Records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. Nothing
here can touch `entries`, `measurements`, `mentions`, `entity_aliases` or
`entity_links`. See [docs/observed-tier.md](observed-tier.md).

## Getting the export

The export is the same Takeout whatever phone you use — searches in the
Google app, Chrome or Maps, and YouTube watches on the Samsung (combo 1) or
the iPhone (combo 2), all land in the one Google account. Make it from the
laptop's browser; it is easiest there.

1. Open **takeout.google.com** (signed in to the account you search with).
2. **Deselect all**, then tick:
   - **My Activity**. Click **Multiple formats** and set *Activity records*
     to **JSON** (recommended — exact UTC times; the default HTML works too,
     see *Times* below). *All activity data included* lets you narrow it to
     Search, Image Search, Maps, YouTube and so on; leaving it all in is fine,
     the connector skips what it does not read.
   - **YouTube and YouTube Music**. Click *All YouTube data included*,
     **Deselect** everything, tick **history** only; under *Multiple formats*
     set *history* to **JSON** too.
3. **Next step** → *Send download link via email*, *Export once*, file type
   **.zip**, size 2 GB (larger is fine) → **Create export**.
4. When the email arrives (minutes to hours), download the
   `takeout-<date>T<time>Z-001.zip` into **Downloads**. Leave it zipped.

From the phone instead: **Android** — Settings → Google → *Manage your
Google Account* → *Data & privacy* → *Download your data*. **iPhone** — the
Google app → your profile picture → *Manage your Google Account* → *Data &
privacy* → *Download your data*. Both open the same Takeout pages; save the
zip somewhere the laptop sees (OneDrive / iCloud Drive) or download it on
the laptop from the email.

*Not verified on a 2026 device:* the exact button names. Takeout renames
them from time to time; what matters is My Activity plus YouTube's
*history*, in JSON or HTML, as a .zip.

**How far back it goes** is your auto-delete setting
(myactivity.google.com → *Web & App Activity* / *YouTube History* →
*Auto-delete*: off, or after 3, 18 or 36 months). Accounts created since
2020 start at 18 months (YouTube history at 36); older ones often have it
off. An archive built from regular imports keeps what auto-delete later
removes — see *Nothing is mirrored*.

## Importing

Dry-run first — it is the review step:

```bash
.venv/Scripts/python -m lifecrm observe google_activity ~/Downloads/takeout-20261021T190000Z-001.zip
```

```
google activity: takeout-20261021T190000Z-001.zip (Google Takeout, HTML; 6 file(s) read)
  read: My Activity: Discover, My Activity: Image Search, My Activity: Maps, My Activity: Search, YouTube search history, YouTube watch history
  activity spans 2022-09-26..2026-10-21 — Google keeps what auto-delete (3, 18 or 36 months, or off) has not removed
  products: Search 13, YouTube 8, Maps 2, Image Search 1, YouTube Music 1
  web_search: 19 new, 0 changed | 2022-09-26..2026-10-21 | e.g. 2026-10-21 Search: "climbing gym austin new"
  video_watch: 6 new, 0 changed | 2022-09-27..2026-10-16 | e.g. 2026-10-16 "Be Sweet" — Japanese Breakfast - Topic
  times: HTML dates read in the zone each one names (CDT 25)
  skipped: 2 × Discover: feed activity — not imported
  skipped: 1 × Maps: Directions to — neither a search nor a YouTube watch
  skipped: 2 × Search: Visited a page — browsing is the browser connector's (docs/browser.md)
  skipped: 1 × YouTube: ad (From Google Ads)
  skipped: 1 × YouTube: watch of a video since removed (no link left)
  not read: 1 × archive_browser.html — not My Activity or YouTube history
  not read: 1 × file(s) in YouTube and YouTube Music/subscriptions/ — not My Activity or YouTube history
dry run — nothing written. Re-run with --apply to write.
```

Read the span (the oldest day should be about where your auto-delete
setting says), the products and the sample. Then add `--apply`. The command
prints an import uuid; `lifecrm imports retract <uuid>` undoes the whole run.

With no path, `observe google_activity` uses `[connectors.google_activity]
path` from config.toml, else the **newest** `takeout-*.zip` in Downloads
that holds My Activity or YouTube history (a Takeout of only Photos is
passed over):

```toml
[connectors.google_activity]
path = "~/Downloads/takeout-20261021T190000Z-001.zip"
```

Accepted targets: the Takeout `.zip`, the unzipped folder (the `Takeout`
folder or anything inside it), or one file — `MyActivity.json` /
`MyActivity.html`, `watch-history.json|html`, `search-history.json|html`.
Inside a zip or a folder only those files are read; every other file is
counted per Takeout product on a `not read:` line.

**A Takeout split into several zips** (`-001.zip`, `-002.zip`, ...) holds
different files in each part. Import each part that has activity in it; with
no path, the newest part with activity is the one read.

## What gets imported: one record per event

Two streams. Neither has participants or handles — nobody else is in a
search — and `direction` is empty.

**`web_search`** — every "Searched for" entry, whatever the product
(Search, Image Search, Maps, YouTube, Play Store, ...):

| field | what |
|---|---|
| title | the query, as typed |
| started_at | the moment of the search, UTC |
| journal_date | its day in your home timezone, rollover applied (a 00:40 search belongs to the evening before) |
| value_json | `product` (Search, Image Search, Maps, YouTube, ...), `url` (the results page), `time_source` (only when the time was read in the home timezone — see *Times*) |
| origin_app | `google_search`, `google_image_search`, `google_maps`, `youtube`, `google_play_store`, ... |

**`video_watch`** — every "Watched" entry of YouTube or YouTube Music:

| field | what |
|---|---|
| title | the video's title |
| body | the channel's name |
| started_at / journal_date | as above |
| value_json | `product` (YouTube or YouTube Music), `url` (the watch link), `channel`, `channel_url`, `time_source` when needed |
| origin_app | `youtube` / `youtube_music` |

`value`, `unit`, `ended_at` and `utc_offset_sec` stay empty.

**Volume.** One row per event, not a daily count: "what did I google about
the passport" needs the query and its moment. Heavy use is tens of thousands
of searches and watches a year, a few hundred bytes each; a decade is a few
hundred thousand rows, which the archive holds without trouble. The files
are read streaming — the JSON array entry by entry, the HTML through
html.parser a chunk at a time — so a Takeout of hundreds of MB is never held
in memory as one document. Speed, measured on generated files: JSON about
15,000 entries a second, HTML about 3,000 (the standard library's HTML
parser is the cost) — another reason to export JSON.

### Skipped and counted, per product — never imported

- **"Visited"** entries — pages opened from Search or Chrome. Browsing is
  the [browser](browser.md) connector's, which reads it from the browser
  itself, with visit counts.
- **Ads** — anything marked *From Google Ads* (YouTube ads you watched,
  the Ads product).
- **Discover** feed activity and **Assistant** voice activity.
- Every **other kind** of activity: Maps *Directions to* / *Viewed* /
  *Explored*, *Used*, *Liked*, *Subscribed to*, a non-YouTube *Watched*
  (Play Movies), ... — each counted under its product and verb.
- Searches and watches with **no time**, and **watches of a video since
  removed** (YouTube keeps only "Watched a video that has been removed", no
  title or link).
- An event listed **twice** — the same search in a JSON and an HTML file,
  or a watch in both My Activity and YouTube history — is kept once and
  counted.

Takeout's *Locations* ("At this general area: From your places (Home)") are
not imported: they are Google's rough guess of the area, not a place you
were; the [location](location.md) connector has the real visits.

## Times

**JSON** states UTC (`2026-10-12T02:41:07.123Z`); sub-seconds are dropped.

**HTML** writes each time in the account's display zone, with its
abbreviation, in the language of the Takeout:

- English dates are read: US `Oct 12, 2026, 9:41:07 PM CDT` (newer exports
  put a narrow no-break space before PM — that is handled) and UK
  `12 Oct 2026, 21:41:07 BST`.
- These zones are read exactly: CDT/CST, PDT/PST, EDT/EST, MDT/MST, BST,
  GMT, UTC, CET, CEST, and `GMT+02:00` / `UTC-5` style offsets.
- **Any other zone** (AEST, IST, ...) or none at all: the time is read as
  wall time in your home timezone (`[time] timezone`), the record carries
  `time_source: "home_timezone"`, and the report says how many and which
  zones. Re-export in JSON for exact times.
- **Dates in another form** (a German page's `12.10.2026, 21:41:07 MESZ`,
  Canada's `2026-10-12`) are **not guessed at**: those entries are counted
  on one `not read:` line with an example, and not imported. A misread date
  is worse than none. Export in JSON (its times do not depend on the
  language), or switch the Google account's language to English first.

The zone an HTML page shows is the account's setting when the export was
made, not where you were — a search made at the lake in Slovenia shows in
Austin time. So it is never stored as the record's own offset, and an HTML
entry and its JSON twin are the same record. Like every connector without a
per-record zone, a search made abroad is dated on the home-timezone day.

## Re-importing, and what wins

Identity is `sha1(product, kind, url, time)` — the title stands in for the
link when there is none. So one event is one record whether it came from
JSON or HTML, from My Activity or YouTube history, from this Takeout or the
next one.

The title is deliberately **not** part of the identity when there is a
link: creators retitle videos, and a retitled video would otherwise be
watched twice. An event never changes, so `last_modified_at` is its own
time:

- **identical** — skipped; re-importing the same Takeout writes nothing.
- A **renamed video or channel** shows as a difference that is not newer:
  the archive keeps the names it was imported with, and the report counts
  them on one line ("kept existing: N watch(es) whose video or channel has
  been renamed since"). Anything else that differs is a CONFLICT line,
  never applied.

## Nothing is mirrored

A search missing from a later Takeout is **kept**. My Activity's
auto-delete (3, 18 or 36 months) and YouTube's history expiry drop old
activity from every later export; a search that has expired at Google is not
a deleted fact, and the archive remembers it. Deleting activity by hand at
myactivity.google.com does not remove it here either — retract the import
(`lifecrm imports retract <uuid>`) if you want a run gone.

## Attribution

No handles: searches and watches involve nobody else, and a YouTube channel
is a publisher, not a person you know — its name is the record's `body`, not
a handle to link.

## Known limitations

- **English Takeouts only.** The file names (`MyActivity.html`,
  `watch-history.json`), the verbs ("Searched for", "Watched", "Visited")
  and the HTML dates are matched in English. A Takeout made in another
  language names its folders and files differently (*Meine Aktivitäten*,
  *Mi actividad*); the connector reads none of it and says so, naming what
  the zip holds. Switch the account's language to English before exporting.
- **Formats written from published descriptions and parsers, not a captured
  2026 Takeout.** The reader follows Takeout's JSON fields and HTML markup
  (`outer-cell`, `content-cell`, the caption's *Products / Details* labels)
  as they have been for years, defensively: unknown fields are ignored, and
  an entry it cannot classify is counted, never guessed. Check the first
  real dry run.
- **Whether My Activity also carries YouTube** depends on the export; when
  it does, the same watches in YouTube history dedupe (counted as "listed
  twice").
- **YouTube Music** watches are `video_watch` with product `YouTube Music`;
  "Listened to" entries (if a product writes them) are counted, not read.
- **Searches outside Google's own products** (Bing, DuckDuckGo, a site's
  own search box) are not here; the browser connector sees their results
  pages as visits.

## First real import: checklist

1. Export as above (JSON), download the zip into Downloads.
2. `observe google_activity` (dry run). Check:
   - `read:` lists My Activity: Search and the YouTube history files;
   - the span's oldest day matches your auto-delete setting (18 months by
     default);
   - the sample search is something you actually typed, on the right day;
   - no `not read: N × entries dated in a form ...` line (HTML only — if it
     is there, re-export as JSON);
   - the `skipped:` counts look plausible (Visited is usually the biggest).
3. `--apply`. Re-run the dry run: everything should be `identical`.
4. Ask something you remember googling, and check the cited row.
5. Import a fresh Takeout every few months — at least once within your
   auto-delete window, or the oldest activity expires before it is kept.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe google_activity [<takeout.zip | folder | MyActivity.json | watch-history.html>]
.venv/Scripts/python -m lifecrm observe google_activity <target> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```
