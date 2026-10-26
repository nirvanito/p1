# Calendar import

Your calendar is the one place other people appear with a stable address
attached. Every invite names its attendees by email, every event has a time,
and most have a place. One export bootstraps the people graph: every attendee
becomes an `email` handle you link to a person once, and every location a
candidate place.

Events land in the **observed tier**: bulk records that never pass the `/log`
review gate, are never asserted as fact, and are always cited. Nothing here can
touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

### Automatic — no export at all (Google Calendar)

Connect the calendar once and the daily collect job downloads it every
morning through its private iCal link: `python -m lifecrm connect
google_calendar`, then paste *Settings › (the calendar) › Integrate calendar ›
Secret address in iCal format*. See [docs/online.md](online.md). The exports
below are for a one-off import or for calendars without such a link.

### Windows laptop + Android (Samsung) — Google Calendar

A Samsung phone's events reach Google only if they are saved to the Google
account. In Samsung Calendar → ☰ → ⚙ Settings → *Calendars*, check that your
events are in the Google account's calendar, not in "My calendar" (phone-only)
or the Samsung account; move them if they are not. Then, on the laptop, either:

- **Google Takeout** — <https://takeout.google.com> → *Deselect all* → tick
  **Calendar** → *Next step* → *Export once* → *Create export*. The download is
  `takeout-<date>-001.zip` holding `Takeout/Calendar/<calendar name>.ics`, one
  file per calendar (your primary calendar is named after your address).
- **Google Calendar's own export** — calendar.google.com → ⚙ *Settings* →
  *Import & export* → **Export**. The download is `<you>@gmail.com.ical.zip`,
  one `.ics` per calendar you own.

Either file can be imported as downloaded — no unzipping.

### MacBook + iPhone — Apple Calendar (iCloud)

The iPhone has no calendar export of its own. Turn on iCloud Calendars on the
phone (Settings → your name → iCloud → *Calendars*) so its calendars appear in
Calendar.app on the Mac, then:

- **Calendar.app** → select one calendar in the sidebar → *File* → *Export* →
  **Export…** → `Home.ics`. One calendar per export; repeat for each calendar
  you want (Home, Work, …). Do *not* use *Calendar Archive…* — that `.icbu` is
  Apple's own backup format, not iCalendar. For events you delete to leave
  the archive too, list those names in `[connectors.calendar]
  mirror_calendars` (see *Deleted events* below).
- **iCloud.com** has no export button. A calendar shared as a *Public
  Calendar* has a `webcal://` link; change it to `https://`, open it in a
  browser to download the `.ics`, then turn public sharing off again.

Outlook (`.ics` from *File* → *Save Calendar*) and Thunderbird exports read the
same way; Outlook's Windows zone names ("Central Standard Time") are mapped to
real zones.

## Importing

Dry-run first — always. Read the date span and the calendar list: if they do
not match what your calendar shows, stop and say so.

```bash
# Windows
.venv\Scripts\python -m lifecrm observe calendar Downloads\takeout-20261021T120000Z-001.zip
# Mac
.venv/bin/python -m lifecrm observe calendar ~/Downloads/Home.ics
```

```
calendar: takeout-20261021T120000Z-001.zip (Google Takeout, 3 calendar(s): alex.rivera@example.com (16), Family (17), Holidays in United States (6))
  account: alex.rivera@example.com (from the calendar named by an email address)
  repeating events expanded through 2027-01-19 (today + 90 days): 5 series, 23 occurrence(s)
  calendar_event: 39 new, 0 changed | 2022-09-26..2027-01-18 | e.g. 2026-10-20 Launch week
  handles: 7 people (7 named by their invites), 6 place(s) — review with 'lifecrm handles list --suggest'
  your own address is likely alex.rivera@example.com — this import's account, so it already counts as you
  skipped: 1 × room, resource or calendar address among the attendees
dry run — nothing written. Re-run with --apply to write.
```

An iPhone `Home.ics` is not named by your address (its account is
`calendar:Home`), so nothing in it says which attendee is you. Until
`config.toml` says so, the report prints the lines to add:

```
  your own address is likely alex.rivera@example.com — say so once in config.toml, so you are never listed as someone you met (People, the review pack, ask):
      [owner]
      handles = ["email:alex.rivera@example.com"]
    and link it to yourself if you like: lifecrm handles link email:alex.rivera@example.com --entity <id>
```

(`[owner]` is described in [ask.md](ask.md), "Who you are"; `lifecrm doctor`
warns `owner_unknown` while nothing imported or configured says who you are.)

Then write it with `--apply`. The command prints an import uuid;
`lifecrm imports retract <uuid>` undoes what the run added. A run that also
removed events prints the snapshot that undoes the removal instead.

With no path, `observe calendar` uses `[connectors.calendar] path`, else the
newest Takeout zip or folder, or `.ical.zip`, in `~/Downloads` that holds a
calendar. Accepted targets: a `.ics` file, a folder of them (searched
recursively — an unzipped Takeout), or a zip holding them.

### Settings

All optional, in `config.toml`:

```toml
[connectors.calendar]
path = "~/Downloads/Home.ics"        # what to read when no path is given
account = "you@gmail.com"            # whose calendars a multi-calendar export holds
mirror_calendars = ["Home", "Work"]  # one-calendar exports whose name is yours alone
horizon_days = 90                    # repeating events expand to today + this
exclude_calendars = ["Holidays in United States"]
```

## What gets imported

**One observation per event occurrence**, stream `calendar_event`. A weekly
1:1 is a row per week: "when did I last meet Priya" is a question about
occurrences, not about a rule.

| field | from |
|---|---|
| `title` | SUMMARY (`(no title)` when empty) |
| `body` | DESCRIPTION, with Google's *Join with Google Meet* block and any HTML removed |
| `started_at` / `ended_at` | DTSTART / DTEND (or DURATION), in UTC |
| `utc_offset_sec` | the event's own offset, when it was written with a zone |
| `value_json` | `calendar`, `all_day`, `recurring`, `rrule`, `location`, `status`, `url` (or the Meet link), `geo` (Apple's structured location or GEO), `responses` (each attendee's RSVP), `original_start` (a moved occurrence) |
| participants | ORGANIZER → `organizer`, ATTENDEE → `participant` (`email` handles, the invite's CN as display name); a physical LOCATION → `location` (a `place_id` handle) |

Deliberate choices worth knowing:

- **Your own address is kept.** It is a handle like any other. What says "I
  was there" is `config.toml` listing it under `[owner]`:

  ```toml
  [owner]
  handles = ["email:alex.rivera@example.com"]
  ```

  That keeps you out of People, the review pack and `ask`'s participant
  lists ("with Dr. Rebecca Ellis", never "with Alex Rivera, Dr. Rebecca
  Ellis"). Linking the address to yourself (`lifecrm handles link
  email:<you> --entity <id>`) gives you a person page but does not, on its
  own, say that person is you: an iPhone Home calendar's account is
  `calendar:Home` and its events carry no direction, so nothing else in the
  import does. When the scan's guess at your address is not covered yet, the
  report prints the `[owner]` lines to add; when your address is the
  import's own account (a Google calendar named by it), it already counts
  as you and the report says so.
- **Rooms and resources are skipped**, counted. Google's boardroom calendars
  are not people.
- **Places are handles.** A location becomes `place_id:calendar:<location,
  lower-cased>`. Link it to a place entity once and every event held there is
  attributed. Meeting links and "Zoom" get no place handle.
- **Cancelled events are skipped**, counted — they did not happen.
- **Tasks are not events.** VTODO and VJOURNAL entries are counted only.

## Repeating events

Expanded by the importer (it is plain iCalendar: FREQ daily/weekly/monthly/
yearly with INTERVAL, COUNT, UNTIL, BYDAY — including "the last Friday" —,
BYMONTHDAY, BYMONTH, BYSETPOS):

- From the first occurrence **through today + 90 days**, at most the newest
  1000 occurrences of one series.
- Occurrences keep their wall-clock time across daylight-saving changes, in
  the event's own zone.
- **EXDATE** removes an occurrence. A **moved or edited occurrence** (a
  RECURRENCE-ID override) replaces its slot and keeps its id, so re-importing
  updates it instead of adding a second row. A cancelled occurrence is
  removed.
- A rule the importer cannot expand (hourly, by week number, several times a
  day) keeps its **first occurrence**, is listed under *not understood*, and
  the run removes nothing (below).

## Dates and time zones

- **Each event is dated in its own zone.** Breakfast at 08:00 in Ljubljana
  belongs to that day there, not to the night before at home. The day
  rollover hour applies, as everywhere: an event at 00:30 is the previous
  evening's.
- **All-day events keep the date written**, with no rollover. DTEND is
  exclusive: a one-day event ends at the next midnight.
- **Times written in UTC carry no zone of their own** — Google writes one-off
  events that way, Outlook writes `TZID=UTC` — and are dated in your home
  timezone, like every connector without one. Floating times are read in the
  home timezone too.
- A TZID that is not a real zone falls back to its VTIMEZONE block's standard
  offset, and the report says so.

## Re-importing, and what wins

Identity is `UID@<occurrence start>` (the UTC instant, or the date for all-day
events), so re-importing an export is always safe:

- **identical** — skipped silently.
- **update** — the event changed *and* its LAST-MODIFIED is strictly newer.
  Applied, audit-logged.
- **conflict** — changed but *not* newer. **Never applied**, surfaced in the
  report so you decide.

`last_modified_at` is LAST-MODIFIED, else DTSTAMP, else CREATED. Google's
DTSTAMP is the export time, so it is only the fallback.

## Deleted events: the archive mirrors the calendar

A calendar export is its calendars' whole current state, so — like texts and
AI chats (decided 2026-09-24) — an event you delete or cancel in the calendar
is removed here by the next import. The dry run shows what would go:

```
  remove: 2 no longer in the calendar (deleted or cancelled) — 2 calendar_event | from Coffee with Sam (1), 1:1 Alex / Priya (1)
```

What is mirrored is **one account**, decided like this:

- **One calendar** (Calendar.app's export, any single `.ics`) is account
  `calendar:<its name>` and mirrors only that calendar — importing `Work.ics`
  never removes anything from `Home.ics`. But the name is all such an export
  says, and **two calendars can share a name**: with iCloud and an Exchange
  account on the Mac, both default calendars are called *Calendar*. Under one
  account, importing either export would remove every event of the other,
  and the next import of the other would remove them back. So a one-calendar
  export **mirrors only when its name is yours alone**:
  - a calendar named by its owner's address (Google's primary calendar is
    `you@gmail.com`) mirrors as it is;
  - any other name mirrors once you list it in `[connectors.calendar]
    mirror_calendars` — you vouching that no other calendar you export is
    called that. Rename a duplicate in Calendar.app (*Get Info*) first if you
    have one.

  Until then the dry run says `not mirroring deletions: 'Home' is known only
  by its name …` and nothing is removed; events you delete stay archived.
- **Several calendars** (Takeout, Google's Export zip) are one Google account's
  whole set: `[connectors.calendar] account`, else the address in a
  `<you>@gmail.com.ical.zip` file name, else the one calendar named by an
  email address (Google names your primary calendar that way). If none of
  those says whose calendars they are, nothing is removed and the report says
  how to fix it. Importing each calendar's `.ics` on its own also works, as a
  one-calendar export (above).
- **Export every calendar.** Takeout and Google's Export include all of your
  calendars by default; one left out of a later export reads as all its
  events deleted. The dry run's `remove:` line names what would go, and a
  `WARNING:` line names any calendar whose events would go although the
  export does not hold that calendar at all — re-export with it included.

The guards:

- **Only what the calendar no longer holds.** Repeating events are expanded a
  further year past the import window just to check presence, so an
  occurrence an earlier run archived is never mistaken for a deletion because
  today's window ends sooner.
- **Unreadable is not deleted.** An export holding an event this importer
  cannot read, or a rule it cannot expand, removes nothing that run — and says
  why.
- **Excluded is not deleted.** A calendar in `exclude_calendars` is not
  imported, and nothing already archived from it is removed.
- **Never most of the archive at once.** Removing half or more of the
  account's archive, or five or more events that are over a tenth of it, in
  one import is refused without `--allow-mass-removal` — a truncated or wrong
  export looks like mass deletion.
  A file cut off mid-download is refused outright ("ends inside BEGIN:VEVENT").
- **Snapshot first** (`backups/pre-removal-<time>.db`, with the undo
  `lifecrm restore "<that snapshot>"` printed), and an event a confirmed
  journal entry cites is hidden, not deleted — and back, active again, once
  the calendar holds it again.

## Attribution: link people and places once

The importer emits **handles, never names**. Resolve them once:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link email:jake.morales@example.com --entity 12
.venv/Scripts/python -m lifecrm handles link "place_id:calendar:veracruz all natural, 1704 e cesar chavez st, austin, tx" --entity 31
.venv/Scripts/python -m lifecrm handles link email:you@gmail.com --new "Your Name"
```

`--suggest` offers entity candidates from each handle's display name (the
invite's CN, Apple's place title); nothing is ever linked automatically.

## Known limitations

- **One event, two exports.** A Google calendar synced to a Mac and exported
  from both apps has the same ids; the second import's differences (calendar
  name, description formatting) surface as conflicts, never applied.
- **Moving a one-off event** to another time changes its id: the mirror
  removes the old row and adds the new one.
- **A changing calendar list.** A Takeout that held one calendar is account
  `calendar:<name>` (mirrored when named by your address or listed in
  `mirror_calendars`); once it holds several it mirrors as the account. Rows
  imported under the old scope are kept, never wrongly removed — but no longer
  mirrored.
- **Places are matched by their text.** "Veracruz" and "Veracruz All Natural,
  1704 E Cesar Chavez St" are two handles; link both.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe calendar <export>          # dry run
.venv/Scripts/python -m lifecrm observe calendar <export> --apply
.venv/Scripts/python -m lifecrm observe calendar                   # [connectors.calendar] path / ~/Downloads
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list --suggest
```
