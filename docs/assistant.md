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
   (an observed row, or a count over observed rows) — or, for an import the
   daily job held, `C#` (that run's outcome, docs/collect.md). `index[id]`
   holds the uuids, dates and import ids behind each one.
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
| `follow_up` | the newest incoming 1:1 text or email from a linked person is at least `follow_up_days` (3) old, within `follow_up_window` (30), and the imported records hold no later contact from you to them: no text or email you sent them, no answered call, no past meeting that was not cancelled, no confirmed entry naming them as a participant. Not judged, with a note, when the channel's newest import predates the end of the reply window | `O#` the message | `followup:<observation uuid>` |
| `goal` | an open plan carries a `data.goal` (below): how it stands this period, the last period, the streak | `E#` the plan, plus the rows behind the value | `goal:<entry uuid>` |
| `drift` | the last `drift_recent` (7) days differ from the `drift_baseline` (30) days before by more than the threshold: sleep 0.5 h, mood and energy 1.0, steps 1,500 a day, workouts 1 a week, spending 25 % and at least 50. A level needs 3 recent and 8 baseline known days; a weekly rate needs 80 % of both windows known. Below that it is a note, not a nudge | `M#`/`H#`/`O#` the rows of each window | `drift:<series>` |
| `birthday` | a contact card linked to a person carries a birthday within `birthday_days` (default 14); one nudge per person, the card that knows the year preferred; never your own | `O#` the card | `birthday:<entity id>:<year>` |
| `nutrient` | the food diary's average over `nutrient_days` (default 14) is below 70 % of the reference for a nutrient (`lifecrm nutrients`); one nudge per nutrient, the disclaimer first | `O#` the rows averaged | `nutrient:<key>` |
| `collect` | a source whose newest daily run held it — a source new on this computer (the title is the question: "New on this computer: browser history. Import it?"), a large removal, records the reader cannot validate — or that failed (or, when you named it, found nothing); never one you declined; gone once a run is routine (docs/collect.md) | `C#` that run's outcome | `collect:<source>:<why>` (`first_import`, `mass_removal`, `invalid`, `missing`, `error`) |
| `stale` | a connector's newest record is older than its own `stale_after_days` (the rule `doctor` and the Sources page share) | `O#` the newest row | `stale:<connector>` |
| `lapsed` | a person with at least 6 contact days in `lapsed_days` (180) whose days since the last one exceed twice their own median gap (and a week); skipped when they already have a follow-up | `O#`/`E#` the last contact | `lapsed:<entity id>` |
| `experiment` | an open plan with `data.experiment` that has started: its outcome during the change against the days before, over known days ([features.md](features.md)) | `E#` the plan, plus the rows of each side | `experiment:<entry uuid>` |
| `anniversary` | an entry with `data.anniversary` whose next occurrence is within `birthday_days` | `E#` the entry | `anniversary:<entry uuid>:<year>` |
| `price_change` | a recurring charge's latest is more than max(1.99, 5 %) above the one before, within 45 days | `O#` both charges | `price:<merchant>:<date>` |
| `new_recurring` | a recurring charge became visible recently (its newest within 45 days) | `O#` its charges | `recurring:<merchant>` |
| `job_change` | a linked person's LinkedIn position or company changed in an export imported within 30 days | `O#` the connection row | `job:<entity id>:<audit id>` |

The last five come from the read-side features and leave the list while
their feature is unchecked (`lifecrm features`, [features.md](features.md));
so does the gift-idea line a birthday nudge gains (`gift_ideas`).

The **agenda** is a separate list, not a nudge: the calendar events of today
and tomorrow (local time, who is on them, cancelled ones marked), each an
`O#`. A multi-day event is listed on the day it starts. Under each event that
is not cancelled, **meeting prep** (`meeting_prep`) gives one `A#` per linked
person on it, never you: the last message from them and from you, open tasks
and plans naming them, the newest journal entry you were in together,
relationships you confirmed, their LinkedIn position, a birthday within 30
days and gift ideas — each line ending in the ids it rests on, so a brief
citing the `A#` can state any of it.

Three more sections ride the pack, none of them a nudge: `on_this_day` (the
same date in earlier years: `D#` the journal day, `E#` its entries, `O#` its
records), `prompts` (yesterday's moments worth a word in the journal — a
prompt, never an entry) and `highlights` (three book highlights in a fixed
rotation). Each is `{"enabled": false}` while its feature is unchecked.

## Coverage: what a missing row means

A day with no workout row is not a day without a workout; the export may not
have covered it. Activity dates cannot establish completeness, and an
observation remembers only the import that created it, so a span can never
be rebuilt from rows. What can establish it is a guarantee in the source
format, and a connector that has one **declares** it with the import
(`import_coverage`, schema v7: stream, account, interval, and the basis):

| source | declares | basis |
|---|---|---|
| finance, OFX/QFX/QBO | `txn`, per account | the statement's own `DTSTART`/`DTEND` (a CSV says nothing) |
| android_sms | `android_sms`, `android_mms` | a backup marked `type="full"` whose count matches, from its oldest text to the day it was taken; calls never (the log ages out) |
| ios_messages, mac_messages | `imessage`, `sms`, `rcs` | the device's or the Mac's whole Messages store, from its oldest text to the backup's date (or its newest text) |
| fitness | `workout` | Strong, Hevy and Strava export every workout: from the first to the export's newest moment |

Everything else declares nothing, and its missing days stay unknown. A
declaration counts only while its import is applied. Every daily series
(`lifecrm/series.py`: mood, energy, sleep, steps, resting heart rate, weight,
workouts, spending, texts, people in touch, a person, a nutrient, a journal
category) carries the days it knows: a measured quantity knows the days with
a record; a count knows its positives and the declared zeros; a journal
category has positives only, since a logged day does not establish that
every category was recorded. People in touch take positives from every
channel but zeros from complete texts alone, so a zero reads "no recorded
texts". Drift, goals and follow-ups say "insufficient coverage" or "not
judged" where the days are not known, instead of inventing inactivity.

## Goals

A goal is a `plan` entry whose `data.goal` names a measurable target
([contract.md](contract.md), "Goals"):

```json
{"kind": "plan", "title": "Get back to three gym sessions a week",
 "data": {"goal": {"metric": "workouts", "target": 3, "per": "week"}}}
```

`metric` is one of `workouts`, `exercise_minutes`, `spending`, `texts`,
`contact` (with `entity`, the person's name), `steps`, `sleep`,
`sleep_hours`, `mood`, `energy`, or `cat:<category>`; `per` is `day`,
`week` or `month`; `op` is `>=` (default) or `<=`; optional `start`,
`currency` (spending) and `min_known`. `/log` proposes one when you state a
measurable intention; `lifecrm entry edit <uuid> --data '{"goal": ...}'`
attaches one to an existing plan. The shape is checked by the domain layer
on every write, and a goal needs kind `plan`: a kind-only edit of a plan
carrying a goal is refused until the goal is removed.

How a goal is decided depends on how its metric adds up:

- a **count or sum** (workouts, spending, texts, contact days) is met as soon
  as the period's value reaches a `>=` target, even with unknown days; it is
  missed only when the period is over and every day was known; otherwise
  undecided. `projected` = value / elapsed fraction, for the current period.
  A `<=` budget fails as soon as it is exceeded and is met only at the end
  with full coverage;
- a **level** (sleep, mood, energy; steps per week) reports its observed
  mean, its coverage and whether it is currently above target, and is
  decided only when the period is over with at least `min_known` days known
  (5 of a week, 20 of a month, 1 of a day);
- the goal starts at `goal.start` or the plan's journal date; periods are
  calendar-aligned; the first, partial period never enters a streak; the
  streak counts consecutive complete periods met and says what ended it, a
  miss or coverage; a closed plan ends the goal; an edited target applies
  to every period.

The review pack's `goals` section (every lens) lists each goal's periods
that overlap the pack, decided as of the day after it.

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
year and one for next — and, while `gift_ideas` is checked, every anniversary
you recorded (`data.anniversary`, [features.md](features.md)) the same way,
under the anniversary nudge's key. Never a yearly rule: a snooze moves one occurrence and
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
follow_up_days = 3    # an incoming message this old with no later contact is a follow-up
follow_up_window = 30 # how far back follow-ups look
lapsed_days = 180     # the history a person's own contact rhythm is learned from
drift_recent = 7      # drift: the last N days ...
drift_baseline = 30   # ... against the N days before them
patterns = "off"      # "on": the EXPERIMENTAL pattern finder in nudges, review and /morning
pattern_series = []   # extra series keys for it; pattern_days = 90 its window
reminders_dir = "~/OneDrive/LifeCRM reminders"   # unset = no calendar
reminder_hour = 9     # local hour of a due item's alarm
auto_publish = true   # republish after every command that changes the database
```

`lifecrm configure` writes the section, commented out, into a new
config.toml.

## Patterns (experimental)

`lifecrm patterns` tests every pair of daily series for an association the
same day and the next day and reports what survives a false-discovery
control ([patterns.md](patterns.md)): statistics over your own records, no
model, co-occurrence never cause. It is experimental and **off everywhere
else by default**: with `[assistant] patterns = "on"`, `nudges --json`
carries a `patterns` block whose items are `P#` ids in the same index (so a
brief can cite them and `--check --pack` holds them to the saved pack), the
review pack adds a `patterns` section, and `/morning` adds an optional
paragraph. `nudges --patterns` turns it on for one run.

## Not yet

Coverage declarations exist only for the sources whose formats carry a
guarantee (the table above); the others stay unknown. The pattern finder's
full-strength validation gate has been run and recorded in
[patterns.md](patterns.md) (2026-10-25, every suite passing); patterns stay
off by default until you turn them on. An anniversary now has a home — an
entry's `data.anniversary` ([features.md](features.md)) — but a birthday
without a contact card still has none in the schema.
