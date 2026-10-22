# Browser history (Chrome, Edge, Brave, Safari)

What you read is a record of what you were working on and planning — the
board deck template, the passport renewal page, the Lake Bled itinerary —
and every browser already keeps it in a local SQLite file. The `browser`
connector reads that file on the laptop. It never opens it for writing, never
talks to the browser, and needs no export step.

Records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited. See
[docs/observed-tier.md](observed-tier.md).

## Where the history is

| browser | Windows (combo 1) | macOS (combo 2) |
|---|---|---|
| Chrome | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\History` | `~/Library/Application Support/Google/Chrome/Default/History` |
| Brave | `%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default\History` | `~/Library/Application Support/BraveSoftware/Brave-Browser/Default/History` |
| Edge | `%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\History` | `~/Library/Application Support/Microsoft Edge/Default/History` |
| Safari | — | `~/Library/Safari/History.db` |

With no path, `observe browser` takes the default profile of the first of
Chrome, Brave, Edge (and Safari on a Mac) that exists. The browsers the OS
ships with come last — that Edge or Safari is installed says nothing about
whether you use it. Pick one, or a profile, in config.toml:

```toml
[connectors.browser]
browser = "safari"                    # chrome | brave | edge | safari | ...
# path = "~/Library/Application Support/Google/Chrome/Profile 2/History"
exclude_domains = ["mybank.example", "health-portal.example"]
```

**Phones.** Nothing to do separately: Chrome on the Samsung syncs its
history into desktop Chrome (when sync is on), and Safari on the iPhone syncs
into the Mac's Safari through iCloud. Those visits arrive in the laptop's
file, and each record counts them as `synced`.

**Safari needs Full Disk Access.** macOS guards `~/Library/Safari`. Without
it the read fails and says so: System Settings → Privacy & Security → Full
Disk Access → add Terminal (or whatever runs the command), then run it again.

## Importing

Dry-run first:

```bash
.venv/Scripts/python -m lifecrm observe browser
```

```
browser history: Chrome, profile Default — Default/History (41 visit(s) on 35 page-day(s); History schema v69)
  read from: C:\Users\alex\AppData\Local\Google\Chrome\User Data\Default\History via a byte copy — the browser held the file locked (it is probably running), so the file was copied first and the copy read
  history spans 2026-09-11..2026-10-20 — browsers keep about 90 days (Chrome, Edge, Brave) or a year (Safari) unless set otherwise
  top domains: docs.google.com (11), google.com (9), mail.google.com (6), en.wikipedia.org (2), usps.com (2)
  browser_visit: 35 new, 0 changed | 2026-09-11..2026-10-20 | e.g. 2026-10-20 ×1 en.wikipedia.org — The Overstory - Wikipedia
  skipped: 2 × chrome: URL (browser-internal or local, not a web page)
  skipped: 1 × file: URL (browser-internal or local, not a web page)
dry run — nothing written. Re-run with --apply to write.
```

Check the span and the top domains, then add `--apply`. `lifecrm imports
retract <uuid>` undoes a run. Accepted targets: the `History` / `History.db`
file, its profile folder, or Chromium's `User Data` folder (its `Default`
profile). Other profiles beside the one read are listed in the report;
import each by passing its folder.

**A running browser.** Chromium holds `History` locked while it runs (on
Windows, exclusively). The connector first asks SQLite's backup API for a
consistent snapshot; when the browser refuses, it copies the file's bytes —
with its `-journal` / `-wal` — and reads the copy, which SQLite recovers to
the last committed state. The report says which happened. Closing the browser
first is never required, but it is what to do if Windows refuses the copy
too (the error says so).

## What gets imported: one record per page per day

A visit-per-row archive would be tens of thousands of rows a month for no
gain — "what was I reading the week of the board meeting" needs the page and
the day, not every reload. So each `browser_visit` observation is **one URL
on one journal day** (home timezone; a 1 a.m. visit counts toward the
evening before, as everything else does):

| field | what |
|---|---|
| title | the page title — Safari: the last title it had that day; Chromium keeps only each URL's current title. Unread counters (`(3) `, Gmail's `Inbox (12)`) are dropped so a title does not change every time mail arrives |
| body | the URL |
| started_at / ended_at | first and last visit that day, UTC |
| value / unit | visits that day, `visits` |
| value_json | `domain`, `first_visit`, `last_visit`, `typed_count` (Chromium: typed into the address bar), `browser`, `profile`, `synced` (visits made on another device and synced in, when any) |
| external_id | sha1 of browser/profile, URL and day |

**Skipped and counted, never imported:** anything that is not an http(s) web
page (`chrome://`, `edge://`, `about:`, `file:`, extensions, `data:`),
frames a page loaded by itself (embedded videos, ads), loads that failed
(Safari), and everything under the domains in `exclude_domains` — a domain
covers its subdomains, so `example.com` also excludes `mail.example.com`.

Full URLs are kept, query strings included, as the browser keeps them; use
`exclude_domains` for sites whose URLs you do not want in the archive.

## Re-importing, and what wins

A record's `last_modified_at` is its last visit:

- **Today's record grows** as you keep browsing; the next import updates it
  (audit-logged).
- **Earlier days are identical** on every re-import and skipped.
- Two expected differences on earlier days are **kept as imported** and
  counted on one line each, not listed: the browser has since expired or
  deleted some of that day's visits ("now hold fewer visits — the archive
  keeps the fuller count"), or (Chromium) the page's current title changed.
  Anything else that differs is shown as a CONFLICT and never applied.

## Nothing is mirrored

Browsers expire history on their own — Chrome after about 90 days, Safari
after a year by default. Expiry is not you deleting anything, so the archive
remembers what it imported. Import regularly (weekly is plenty) to keep the
whole of it; a page-day that has already expired from the browser before the
first import is simply not there to read.

## Known limitations

- **No visit durations.** Chromium's `visit_duration` is unreliable and
  Safari has none; only counts and first/last times are kept.
- **A copied History file** outside its profile folder reads as the
  `chromium` browser's `Default` profile, so its records do not match the
  same history imported from the browser's own folder. Import from the
  browser's folder.
- **Formats verified against generated files only.** The demo writes Chrome
  and Safari databases with the tables and columns real ones have (schema
  read defensively, missing optional columns tolerated), but no real profile
  has been imported yet. Check the first real dry run's span and domains.
- **Safari profiles (Safari 17+)** keep separate histories; only
  `~/Library/Safari/History.db` is found by default — pass another profile's
  `History.db` explicitly.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe browser [<History | History.db | profile folder>]
.venv/Scripts/python -m lifecrm observe browser <target> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```
