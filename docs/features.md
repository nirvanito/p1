# Features: check and uncheck what the assistant shows

Fourteen read-side features sit on top of the records. Each one is a reading
of what is already in the database, recomputed every time it is shown: none
of them stores anything, none of them calls a model, and every line any of
them prints cites the row it rests on. Unchecking one hides what it shows and
changes no record; checking it again brings it back exactly as it was.

## The switches

```
python -m lifecrm features                    # the checklist: [x] checked, [ ] unchecked
python -m lifecrm features check places 5     # keys or their numbers from the list
python -m lifecrm features uncheck subscriptions
python -m lifecrm features uncheck --all      # everything off
python -m lifecrm features check --all        # everything on
python -m lifecrm features pick               # the list in the terminal: type numbers to toggle,
                                              # a = all, n = none, Enter saves, q quits
```

They live in `[features]` in `config.toml`, beside every other setting:

```toml
[features]
subscriptions = false
places = true
```

A feature not listed is on. `check` and `uncheck` rewrite only the
`[features]` lines — a key's line in place (its comment kept), a missing key
after the table's last setting, a missing table at the end of the file — then read
the whole file back and refuse, changing nothing, unless everything outside
the table reads exactly as before. A BOM and CRLF line ends are kept. A
`features = { ... }` inline table cannot be edited line by line and is
refused with "edit it by hand". A key or value the table cannot hold
(`subscriptons = false`, `places = "maybe"`) is a one-line error from every
command that reads the switches, the way an unknown `[assistant]` setting is,
and `lifecrm doctor` warns about it (`features_config`; `places_config` for
`[places]`).

The web UI shows the same list on the Sources page. On a server started
with a key (`lifecrm serve --token random`) the boxes check and uncheck, with
*Check all* and *Uncheck all* beside them: the page sends `POST /api/features`
and the server saves through the same `features.save`, so the same rules
hold — only `[features]` changes, and a write that would touch anything else
is refused. Without a key the list is read-only and says how to change it.
The server reads `[features]` again whenever `config.toml` changes, so a
switch flipped in the terminal shows on the page's next request; the Search
link leaves the nav while search is unchecked. The vault and the reminders
calendar follow the switches on their next refresh.

What a switch never turns off is a rule on the write path: the shapes of
`data.experiment`, `data.gift` and `data.anniversary` are checked on every
write (create, edit, the draft contract) whether or not their feature is
shown, so a plan written while a feature was unchecked still reads correctly
once it is checked again.

Four switches are older than this list and keep their own settings and
defaults; the checklist names them at the end so every switch is findable
in one place: `[assistant] patterns` (the experimental pattern finder, off
by default — [patterns.md](patterns.md)), `[ask] enabled` (a local model
phrasing `ask`'s answers, off), `[obsidian] auto_refresh` (re-render the vault
after each change, on once a vault is set) and `[assistant] auto_publish`
(republish the reminders calendar after each change, on).

## The fourteen

| # | key | what it shows | where |
|---|---|---|---|
| 1 | `on_this_day` | the same month and day in every earlier year: the journal day's summary, its entries, and a few of its records — photos (a count), place visits, meetings, workouts, highlights, notes, a ride, and how many texts and with whom. February 29 comes back on a common year's 28th | nudges pack `on_this_day` (`/morning`), web Today (any date), `lifecrm onthisday`, vault day notes ("On this day:" links) and Home |
| 2 | `meeting_prep` | under each agenda event, for each linked person on it (never you): the last message from them and from you, open tasks and plans naming them, the newest journal entry you were in together, relationships you confirmed, their LinkedIn position, a birthday within 30 days, gift ideas — one `A#` per person, every line cited | nudges agenda (`/morning`), web Today, vault Home |
| 3 | `places` | town and country names for place visits and photos from a local GeoNames file; home; trips; countries ([places.md](places.md)) | `ask`, review pack `trips`, web Places (`#/places`) and the day view, vault `Trips.md` and day notes, `lifecrm places` |
| 4 | `journal_prompts` | a day's moments worth a word in the journal: a first recorded visit to a place, a meeting with someone, a call of 20 minutes or more, 8 or more photos, a workout, a spending day at least twice your median (and 50). A moment the day's journal already covers is left out | nudges pack `prompts` (yesterday's), web Today, vault Home, `lifecrm prompts`, an optional step of `/log` |
| 5 | `subscriptions` | charges that come back weekly, monthly, quarterly or yearly at a steady amount; a price rise over max(1.99, 5 %); a new series; "stopped" only where the statements declared the days complete | nudge kinds `price_change`, `new_recurring`; review `subscriptions` (finance lens); `ask` ("what subscriptions do I pay for"); `lifecrm subscriptions` |
| 6 | `experiments` | a plan with `data.experiment`: the outcome over the change's days against the days before, over known days only — co-occurrence, never cause | nudge kind `experiment`, review `experiments` (every lens), `lifecrm experiments` |
| 7 | `search` | every record a few words match across journal entries, transcripts and imports, newest first, with facets by tier, kind, year and person | web Search (`#/search`), `lifecrm search` |
| 8 | `job_changes` | a linked person's position or company changed between two of your LinkedIn exports (the audit log keeps what the row held before) | nudge kind `job_change`, review `job_changes` (career lens), web person page |
| 9 | `year_review` | the review pack's `year` lens: the overview sections over a calendar year, plus new people, towns and countries first on record, books read, highlights, most-listened artists, mood by month and per day | `lifecrm review --year YYYY`, `/review-week` |
| 10 | `link_memory` | where a link or a site turned up — texts, chats, email, notes, browser history, the journal — and who sent it; the links a person sent you | `ask` ("who sent me nytimes.com"), web person page, `lifecrm links` |
| 11 | `gift_ideas` | gift ideas you noted for a person beside their birthday nudge; anniversaries from `data.anniversary` as nudges | birthday nudge detail, nudge kind `anniversary`, the reminders calendar (.ics), meeting prep, web person page |
| 12 | `conversations` | one thread with a person across every app: the one-to-one texts, chats, emails and calls, group threads left out, oldest first | web person page → Conversation, `lifecrm conversation` |
| 13 | `weekday_profile` | each series averaged by day of the week over the days it knows; a weekday with fewer than 3 known days is not compared | review `weekdays` (health and year lenses), `ask` ("which day of the week do I sleep least"), `lifecrm weekdays` |
| 14 | `highlights` | a few Kindle highlights a day in a fixed rotation: every highlight sorted by a hash of its uuid, day N takes the next three, so each comes round once per cycle and nothing records what was shown | nudges pack `highlights` (`/morning`), web Today, vault Home, `lifecrm highlights` |

## The shapes three of them read

All three are ordinary `data` on an entry you confirmed, proposed by `/log`
when you say them and checked by the domain layer
([contract.md](contract.md)).

```json
{"kind": "plan", "title": "Morning walks",
 "data": {"experiment": {"change": "a 20-minute walk before work", "outcome": "mood",
                         "days": 14, "hope": "up"}}}
```

`outcome` is a series key (`mood`, `energy`, `sleep`, `sleep_hours`,
`steps`, `workouts`, `spending`, `texts`, `contacts`, ..., `cat:<category>`,
`nutrient:<key>`); `days` and `baseline_days` are 7-90 (default 14, the
baseline as long as the change); `start` defaults to the plan's journal
date; `min_known` (default half the days, at least 4) is how many known days
each side needs to be compared.

```json
{"kind": "idea", "title": "A Kindle Paperwhite for Jake", "data": {"gift": true}}
{"kind": "fact", "title": "Mom and Dad's wedding anniversary",
 "data": {"anniversary": {"date": "1988-10-24", "what": "wedding"}}}
```

A gift entry mentions the person it is for; `{"status": "given"}` marks one
you gave (shown on the person page, not beside the birthday), `"dropped"`
leaves it out. An `idea` whose title says gift or present counts without the
marker. An anniversary mentions the people it belongs to; `--MM-DD` when the
year is not known.

## Checking a brief

Everything the features add to the nudges pack is in its index, so
`nudges --check BRIEF --pack FILE` verifies a `/morning` brief that uses
them: `A#` for a person's prep (every line of it, with the dates and numbers),
`D#` for an earlier year's journal day, `O#` and `E#` for the rest. "Three
years ago today" checks against the pack's own `ON THIS DAY` context line.
The review pack's new sections cite with its usual `E`/`M`/`H`/`O` ids.
