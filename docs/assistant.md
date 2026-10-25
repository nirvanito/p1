# The assistant: nudges, the reminders calendar, /morning

```bash
.venv\Scripts\python.exe -m lifecrm nudges                 # what matters today, cited
.venv\Scripts\python.exe -m lifecrm nudges --json --out "%TEMP%\pack.json"
.venv\Scripts\python.exe -m lifecrm nudges --check brief.md --pack "%TEMP%\pack.json"
.venv\Scripts\python.exe -m lifecrm nudges snooze due:7e975b84-... --days 7
.venv\Scripts\python.exe -m lifecrm nudges dismiss stale:browser
.venv\Scripts\python.exe -m lifecrm nudges restore stale:browser
.venv\Scripts\python.exe -m lifecrm nudges states
.venv\Scripts\python.exe -m lifecrm reminders publish      # the .ics for the phone
```

```
Nudges for 2026-10-21: 5 shown, 0 hidden (snoozed or dismissed); database as of 2026-10-21
  [N1] Overdue by 3 days: Renew passport
       task recorded 2026-10-01, due 2026-10-18
       key due:7e975b84-2ccc-4466-aec6-9b55a2ee9aaf · rests on E1
  [N2] Due in 3 days (2026-10-24): Book flights to Ljubljana
       task recorded 2026-10-19, due 2026-10-24
       key due:7008a0ba-9dfa-4405-a1bf-917282731365 · rests on E2
  [N3] Sam Okafor's birthday is in 4 days (2026-10-25), turning 34
       from a contact card dated 2026-10-01 (observed, not confirmed)
       key birthday:2:2026 · rests on O1
  [N4] Vitamin D averaged 2.0 µg a day over 3 days, 13.3% of the 15 µg reference
       This compares logged intake with ... not medical advice ... (observed, not confirmed).
       key nutrient:vitamin_d · rests on O2
  [N5] android_sms: newest record is 2026-09-01, nothing newer in over 14 days
       the export or backup feeding it may have stopped; ...
       key stale:android_sms · rests on O3
Agenda (calendar, observed):
  [O4] 2026-10-21   10:00  1:1 Alex / Priya with Priya Natarajan
  [O5] 2026-10-22 all day  Launch week
Sources:
  E1: entry 7e975b84-... · 2026-10-01 · task · Renew passport
  ...
```

The database already knows what is due, whose birthday is near, what the
food diary averages and which import went quiet. `lifecrm nudges` says so,
every line resting on ids you can check, and the only thing you tell it is
"not now" or "never". Six rules shape everything here:

1. **Deterministic, read-only.** A nudge is recomputed from the records on
   every run; the same database and the same date give the same pack, byte
   for byte. Building a pack writes nothing.
2. **Cited.** `N#` is a nudge; it rests on `E#` (a confirmed entry) or `O#`
   (an observed row, or a count over observed rows). `index[id]` holds the
   uuids, dates and import ids behind each one.
3. **State attaches to a key, never to a number.** `N1` is this run's first
   line; `due:<entry uuid>` is the task. An import or a closed task between
   two runs renumbers everything, so snooze and dismiss take the key, and
   an `N#` only together with the saved pack it came from.
4. **The clock for you, the database's last change for the vault.** The
   CLI and `/morning` use today's journal date (timezone, 05:00 rollover);
   so does the web UI, like its loops page. The Obsidian Home note's Today
   section uses the as-of date, so the vault stays a pure function of the
   database.
5. **Wording states what the records show.** "No reply found in imported
   records" is defensible; "you forgot" is not. Everything from an import is
   marked *(observed, not confirmed)*.
6. **Not advice.** A nutrient below reference is a comparison with the
   reference intakes and carries the report's own line that it is not
   medical advice; nothing here diagnoses or recommends.

Nothing here runs a model, and the only writes are the state rows below,
through `lifecrm/domain/` like every other write.

## The kinds

| kind | fires when | rests on | key |
|---|---|---|---|
| `reminder` | an open task or plan is due within `due_days` (default 7), overdue first | `E#` the entry | `due:<entry uuid>` |
| `birthday` | a contact card linked to a person carries a birthday within `birthday_days` (default 14); one nudge per person, the card that knows the year preferred; never your own | `O#` the card | `birthday:<entity id>:<year>` |
| `nutrient` | the food diary's average over `nutrient_days` (default 14) is below 70 % of the reference for a nutrient (`lifecrm nutrients`); one nudge per nutrient, the disclaimer first | `O#` the rows averaged | `nutrient:<key>` |
| `stale` | a connector's newest record is older than its own `stale_after_days` (the rule `doctor` and the Sources page share) | `O#` the newest row | `stale:<connector>` |

The **agenda** is a separate list, not a nudge: the calendar events of today
and tomorrow (local time, who is on them, cancelled ones marked), each an
`O#`. A multi-day event is listed on the day it starts.

More kinds arrive with declared coverage: goals, drift against your own
baseline, follow-ups and lapsed contact (see "Not yet" below).

## The pack, saving it, checking a brief

`--json` prints the whole pack: `today`, `as_of` (the database's last
content change), `nudges`, `agenda`, `hidden`, `notes` (what could not be
computed and why), `index`, and `pack_id`, a sha256 of the pack's content —
a changed title, due date, value or state is a different pack.

`--out FILE` saves it. A brief written from a pack is checked against
**that file**, never against a rebuild:

```
lifecrm nudges --check brief.md --pack pack.json
```

The check verifies that every cited id exists in the pack, that the brief
cites something, and that every number, date and time a sentence states
appears in the lines it cites (today's date and the nutrition disclaimer are
free context). It does not judge whether a sentence's claim follows from
those lines: a correctly cited number in a wrong conclusion passes. That
judgement stays with the reader. Exit 1 lists what failed.

## Snooze and dismiss

```
lifecrm nudges snooze due:<uuid> --until 2026-10-30      # or --days 7
lifecrm nudges dismiss nutrient:vitamin_d --note "talked to the GP"
lifecrm nudges restore nutrient:vitamin_d
lifecrm nudges states
lifecrm nudges --all                                     # the hidden ones too
```

A snoozed nudge comes back on its day; a dismissed one never, until
`restore`. Both are rows in `nudge_states` (schema v6), written through
`lifecrm/domain/nudges.py` with an audit row each; `restore` deletes the row
and the audit log keeps what it was. A birthday's key carries the year, so a
dismissal is for this occurrence only. `doctor` warns about a snooze whose
task has since closed (harmless) and fails on a malformed key or date.

An `N#` is accepted for a write only with `--pack FILE`: `nudges snooze N2
--days 7 --pack pack.json` resolves `N2` in that file. Without it, pass the
key the list prints beside every line.

## Today, in the fronts

- The **Obsidian Home** note opens with `## Today`, built for the as-of date
  with the same code; each line ends with the key, and the vault is one way,
  so the "not now" goes through the CLI ([obsidian.md](obsidian.md)).
- The **web UI's** Today page opens with a Today card from `GET /api/nudges`,
  live on the clock; read-only, like the rest of the page ([web.md](web.md)).
- **/morning** in Claude Code writes the brief (below).

## The reminders calendar

```
lifecrm reminders publish                       # writes data/reminders/lifecrm-reminders.ics and the copy in [assistant] reminders_dir
lifecrm reminders publish --dry-run             # what would change, nothing written
lifecrm reminders publish --out ~/Desktop/r.ics # also write it there
lifecrm reminders publish --if-configured       # the scheduled job's form: one line and exit 0 when no folder is set
```

What is in it: every open task or plan with a due date, as a 30-minute
event at `reminder_hour` (default 09:00) local on its due day with an alarm
— an overdue one sits on **today**, so the phone nags each morning until it
is closed, snoozed or dismissed; and every birthday from a linked contact
card, as an all-day event with a morning alarm, one dated occurrence for this
year and one for next. Never a yearly rule: a snooze moves one occurrence and
a dismissal drops one. A snoozed item sits on its snooze day; a dismissed
one is left out. The nudge keys and the event UIDs line up one to one
(`lifecrm-due-<uuid>@lifecrm`, `lifecrm-bday-<entity id>-<year>@lifecrm`).

**One publisher.** A calendar client re-reads an event only when its
`SEQUENCE` moved and otherwise keeps what it fetched last, so every change —
an overdue task re-dated to today, a snooze, a title edit, an item dismissed
and later restored — must be a recorded revision. `publish` is the only code
that produces the calendar: inside one write transaction (the lock between
two publishers) it computes the events, asks `lifecrm/domain/reminders.py`
which changed since the last publication (`reminder_revisions`, schema v6:
a fingerprint per UID, its sequence, its last revision; a left-out item keeps
a tombstone so it can never come back at a sequence a client has seen),
writes the file atomically, commits, then copies it to the synced folder.
`DTSTAMP` is each event's own last revision, so an unchanged calendar is
byte-identical and nothing is rewritten. A publication's audit row does not
move "the database's last change": it is bookkeeping, not content.

**When it advances.** After every CLI command that changed the database
(the same hook that refreshes the vault, when `reminders_dir` is set), and
every morning at 07:15 by the `reminders` job (`install-tasks`), which is
what moves an overdue item to the new day. `lifecrm serve` answers
`/reminders.ics` with the published bytes and computes nothing.

Two things to never do: do not put the file inside the Obsidian vault (the
vault's manifest only manages `.md` files), and do not point the calendar
connector at it — the reminders would come back as observations.

## Getting it onto the phone

- **Android.** Install ICSx⁵ (F-Droid or Play). Add a calendar from a *local
  file* and pick `lifecrm-reminders.ics` inside the synced folder (OneDrive's
  local copy); set its sync interval and turn alarms on. The phone re-reads
  the file as the sync app updates it.
- **iPhone.** An iPhone subscribes to a URL, not a file. Start the web UI with
  a key (`lifecrm serve --host 0.0.0.0 --token random`); its banner prints
  the subscription line, `http://<laptop>:8765/reminders.ics?k=<key>`. On the
  phone: Settings › Calendar › Accounts › Add Account › Other › Add
  Subscribed Calendar, paste it, and in the calendar's settings turn on
  alerts and pick the refresh frequency. It updates while the laptop is on
  and on the same network; otherwise the phone keeps the last fetch.
- **Mac.** Calendar › File › New Calendar Subscription with the same URL; or
  import the file once (a one-shot import duplicates on re-import).

### Device checklist (verify on first real use)

Like each source's first-real-import checklist, these are the owner's checks;
the tests cover the bytes, not the phone.

- [ ] The subscription (Android: local file; iPhone: URL) appears as a
      calendar and shows a due task on its day and a birthday on its day.
- [ ] A due task's alarm fires at `reminder_hour`; a birthday's alarm fires
      that morning, not at midnight.
- [ ] `lifecrm nudges snooze <key> --days 3`, then the next sync: the event
      moved to the snooze day (one event, not two).
- [ ] `lifecrm nudges dismiss <key>`, then the next sync: the event is gone
      (a subscription drops it; a one-shot import would not).
- [ ] `lifecrm nudges restore <key>`: it is back.
- [ ] An overdue task shows on today after the 07:15 job (or any write).
- [ ] Laptop off, or off the LAN (iPhone): the calendar keeps the last fetch,
      updates nothing, and catches up when the laptop is back.

## /morning

In Claude Code, `/morning` runs `lifecrm nudges --json --out <pack file>`,
writes at most 200 words under **What matters today** (optionally **One
small experiment**, two weeks, measurable, framed as a question), cites an
`N#` in every factual sentence, copies numbers exactly, treats observed items
as what the records show, never diagnoses, then runs `nudges --check <brief>
--pack <pack file>` until it passes and shows the brief with its Sources. If
you want it kept, it goes through the normal gate as a `reflection` entry
with `data.derived_from = "lifecrm morning"` — `import --type manual`, a
one-entry draft, `ingest`, the diff, and `apply` only after you say so. A
kept brief is never re-read as evidence: every entry whose `derived_from`
starts with `lifecrm ` is left out of later packs.

## Settings

```toml
[assistant]
due_days = 7          # a task or plan due within this many days is a nudge
birthday_days = 14    # a linked contact card's birthday within this many days
nutrient_days = 14    # the food-diary window for "below reference"
reminders_dir = "~/OneDrive/LifeCRM reminders"   # unset = no calendar
reminder_hour = 9     # local hour of a due item's alarm
auto_publish = true   # republish after every command that changes the database
```

`lifecrm configure` writes the section, commented out, into a new
config.toml.

## Not yet

Declared coverage, goals (`data.goal` on a plan), drift against your own
baseline, follow-ups and lapsed contact arrive in the next increment;
patterns (co-occurrence between daily series) after that, experimental until
their validation suite passes. Anniversaries have no source field yet, and a
birthday without a contact card has no home in the schema.
