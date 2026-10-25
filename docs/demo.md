# The demo: a whole second brain, on synthetic data

`lifecrm demo` builds a complete instance you can click through, ask
questions of, and break, without risking anything real. It is one
fictional life rendered as the real export files of each app, then run
through the real importers. The commands it runs are the same ones you
would type, and they run in the same order.

Three setups are supported end to end, and the demo builds any of them:

| combo | computer | phone | what it imports |
|---|---|---|---|
| `android` | Windows laptop | Samsung Galaxy | Health Connect export, SMS Backup & Restore (texts + calls), Google Contacts `.vcf`, Google Calendar Takeout, Google Keep Takeout, Chase CSVs, Samsung Gallery photos (EXIF), Google Maps Timeline, Chrome history, WhatsApp (Android format), Claude + ChatGPT exports, a Gmail Takeout (`Mail/*.mbox`), Google Takeout My Activity + YouTube history (JSON), Venmo statements |
| `iphone` | MacBook | iPhone | Apple Health `export.zip`, the Mac's Messages `chat.db`, Call History, the Mac's Contacts store, Apple Calendar `.ics`, Apple Notes `NoteStore.sqlite`, Apple Card CSVs + a credit union `.qfx`, Mac Photos library, Safari history, WhatsApp (iOS format), Claude + ChatGPT exports, an Apple Mail "Export Mailbox" folder, Google Takeout My Activity + YouTube history (HTML), Venmo statements + a PayPal activity download |
| `windows-iphone` | Windows laptop | iPhone | Apple Health `export.zip`, an unencrypted Apple Devices **iPhone backup** (`Manifest.db`, `sms.db` and the phone's `AddressBook.sqlitedb`, read by `ios_messages`), an iCloud.com vCard, iCloud's `Home.ics`, Apple Card CSVs + a credit union `.qfx`, an iCloud for Windows photos folder (JPEGs with the iPhone's EXIF), Edge history, WhatsApp (iOS format), Claude + ChatGPT exports, a Gmail Takeout, Google Takeout My Activity + YouTube history (HTML), Venmo statements |

All three also import the sources that are the same file whichever
phone you carry: Instagram and Facebook "Download your information" zips
(JSON), a Telegram Desktop export, Strong's `strong.csv` and Strava's
`export_<id>.zip`, both Spotify downloads (`my_spotify_data.zip` and
`my_spotify_data (1).zip`), a Kindle's `My Clippings.txt` and Goodreads'
library CSV, Cronometer's `servings.csv` and `dailysummary.csv`, a LinkedIn
data export, and Uber's `Uber Data.zip` (rides and an Eats order). Each has
its own guide: [email](email.md), [meta_messages](meta_messages.md),
[telegram](telegram.md), [google_activity](google_activity.md),
[fitness](fitness.md), [spotify](spotify.md), [reading](reading.md),
[nutrition](nutrition.md), [linkedin](linkedin.md),
[payments](payments.md) and [rides](rides.md).

Neither iPhone combo has a location sample, because Apple's Significant
Locations cannot be exported ([location.md](location.md)). The
`windows-iphone` combo also has no calls and no notes, and its build says
why in its `skipped` lines: an unencrypted iPhone backup carries no call
history (Apple keeps it for encrypted backups, which cannot be read —
[calls.md](calls.md)), and Apple Notes has no export on Windows. The setup
it stands for is [windows-iphone.md](windows-iphone.md).

All start from the same journal: thirteen logged days spread over the six
weeks. Each day is an evening note, sometimes with quick notes, dropped into
the text inbox under the file name that combo's phone would give it (an
iPhone's Shortcut names, with the journal date and the UTC offset, on both
iPhone combos — in OneDrive on the Windows laptop). The
notes go through intake, one draft per day, ingest, review and apply. They
arrive as text, so whisper is not needed unless you pass `--with-audio`.

## Run it

**Windows (laptop + Samsung):**

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1
```

**Windows (laptop + iPhone):**

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -Combo windows-iphone
```

**Mac (MacBook + iPhone):**

```bash
/bin/bash scripts/setup.sh
/bin/bash scripts/demo.sh
```

Each script builds `demo/<combo>/` the first time (about two minutes), then
opens the web UI at `http://127.0.0.1:8765/` (`serve --open`: the browser
opens once the server is listening), or with `-Gui obsidian` /
`--gui obsidian` the demo's Obsidian vault. Either runs from any folder, Explorer's
*Run with PowerShell* included. `demo.ps1` builds the android
combo by default and `demo.sh` the iphone one. Pass `-Combo iphone` /
`-Combo windows-iphone` / `--combo android` / `--combo windows-iphone` to
build another setup on the same machine. Every combo works on either OS,
because every input is a file. To rebuild from scratch, use
`-Rebuild` / `--rebuild`. `-NoServe` / `--no-serve` builds without serving,
and `-Port N` / `--port N` serves on another port.

You can also call the command directly. Here `python` is the project's venv
interpreter (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on a
Mac):

```bash
python -m lifecrm demo --combo android          # or iphone, windows-iphone, both, all
python -m lifecrm demo --combo iphone --force   # rebuild
python -m lifecrm demo --dir ~/lifecrm-demo     # build somewhere else (one combo)
python -m lifecrm demo --anchor 2026-10-22      # pin the demo's "today" (default: today's journal date)
python -m lifecrm demo --with-audio             # also speak + transcribe one note
python -m lifecrm demo --no-ask --json          # skip the sample questions; print the summary as JSON
```

`both` builds `android` and `iphone`, as it always has; `all` builds all
three combos, each into its own `demo/<combo>/`.

The default "today" is the journal date in the persona's timezone
(America/Chicago) with the demo's 05:00 rollover — the same day the demo's
`ask`, `status` and `review` call today. A build at 01:00 is anchored on the
day before, so nothing in it is dated tomorrow.

`--with-audio` uses the OS voice (Windows System.Speech or macOS `say`) to
record one evening note into the inbox. Local whisper (`small`) then
transcribes it, which proves the audio path on a fresh machine. The demo
runs its commands offline (`HF_HUB_OFFLINE=1`), so the `small` model must
already be in the Hugging Face cache. To let the first run download it, set
`HF_HUB_OFFLINE=0` in that terminal.

## What is in it

The persona is **Alex Rivera**, a product manager in Austin. The data covers
six weeks:

- a **board deck** that Priya assigns. Alex works on it and takes her advice
  to lead with the customer story, and then it is presented. The task
  closes across days.
- a **passport** renewed, which closes another task from weeks earlier
- **Jake**, Alex's best friend. He grew up in El Paso and his favorite
  restaurant is Veracruz All Natural. This shows up in texts, the journal
  and his contact card.
- **Maya**'s birthday dinner at Uchi, plus calls with **Mom** while her knee
  heals
- a trip to **Lake Bled, Slovenia four years ago**, with photos, card
  charges and a packing list, and a return trip being planned now in texts,
  an AI chat and a task
- a **health** series: sleep, steps, weight, workouts and heart rate. It
  agrees with the journal ("slept badly, maybe five and a half hours"), and
  it dips in the last week as launches pile up.
- the rest of a life's exports, telling the same story: Priya's advice on
  the deck by email and her congratulations on LinkedIn, where she is now VP
  Product; the "Bled crew" planning the trip on Instagram and Sam's climbing
  group on Telegram; the searches and YouTube videos behind each plan; bench
  with Sam at 185 lb in Strong and the Zilker 5k in Strava; the Slovenia
  podcast on Spotify; highlights from *The Overstory*; a Cronometer diary
  that leaves vitamin D and magnesium low and sodium over on the taco
  nights; Alex's share of the Bled place, sent to Jake on Venmo; and the
  Uber to Uchi.

People, places, phone numbers and emails are all fictional (`example.com`,
`555-01xx`).

The demo writes its own `config.toml` in the demo root: the persona's
timezone and the 05:00 rollover, the synthetic sync folders (iCloud Drive
with the MacBook, OneDrive with either Windows laptop), each connector's
account, `[ask] enabled = false` (answers deterministic and cited), an
`[owner]` table that says who the owner is, and a `[nutrition]` table that
says whose reference intakes `nutrients` uses:

```toml
[owner]
name = "Alex Rivera"
handles = ["email:alex.rivera@example.com", "phone:+15125550100"]

[nutrition]
sex = "male"
age = 32          # from the persona's birthday, on the demo's "today"
```

`handles` uses the `kind:value` shape that `handles link` takes. The newer
connectors need nothing else: email, payments and LinkedIn find the owner
through `[owner]`, and the Meta and Telegram exports name their own
account.

## Things to try

Replace `<demo>` with the path the build printed, for example `demo/android`.
The build ends by printing the answers to the sample questions
(`persona.QUESTIONS`) and several of these commands with the root filled in.

```bash
python -m lifecrm --root <demo> serve                     # web UI: Ask, Today, People, Loops
python -m lifecrm --root <demo> ask "where is Jake from"
python -m lifecrm --root <demo> ask "what's Jake's favorite restaurant"
python -m lifecrm --root <demo> ask "how did I sleep last week"
python -m lifecrm --root <demo> ask "how much did I spend at Veracruz"
python -m lifecrm --root <demo> ask "when did I last talk to Mom"
python -m lifecrm --root <demo> ask "what are my open tasks"
python -m lifecrm --root <demo> status                    # streak, open loops, today's pending note
python -m lifecrm --root <demo> dashboard                 # charts (Journal, Health, Observed, People)
python -m lifecrm --root <demo> handles list --suggest    # identifiers still waiting for a decision
python -m lifecrm --root <demo> review --lens health      # the weekly evidence pack
python -m lifecrm --root <demo> voice --text "how did I sleep last week"
python -m lifecrm --root <demo> doctor
```

From the newer sources, on any combo:

```bash
python -m lifecrm --root <demo> ask "what advice did Priya give me over email"
python -m lifecrm --root <demo> ask "what's my bench PR"
python -m lifecrm --root <demo> ask "what am I low on"
python -m lifecrm --root <demo> ask "how much did I pay Jake"
python -m lifecrm --root <demo> ask "what did I highlight in The Overstory"
python -m lifecrm --root <demo> ask "what did I search for about Bled"
python -m lifecrm --root <demo> ask "when did I last take an Uber"
python -m lifecrm --root <demo> ask "where does Priya work"
python -m lifecrm --root <demo> nutrients                 # the cited nutrient report behind "what am I low on"
python -m lifecrm --root <demo> connectors                # each connector's own rows per stream
```

`nutrients` reads the Cronometer diary for the last 30 days against the
reference intakes for a 32-year-old man: vitamin D and magnesium come out
low, fiber borderline, and sodium over its limit on the two taco nights at
Veracruz, each line citing its rows. It is a comparison with reference
values, not medical advice ([nutrition.md](nutrition.md)).

The build already wrote an Obsidian vault to `<demo>/vault`, start pages
included (Home, People, Health, Sources). The demo's `config.toml` names it
under `[obsidian] vault`, so anything you change in the demo through the
CLI (`handles link`, `entry close`, `/log` against `--root <demo>`)
refreshes it by itself. To look at it in Obsidian instead of the browser:

```bash
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -Gui obsidian    # Windows
/bin/bash scripts/demo.sh --gui obsidian                                   # Mac
python -m lifecrm --root <demo> obsidian open                              # either, once built
```

The first time, Obsidian does not know the folder: `obsidian open` shows its
vault manager, where you choose *Open folder as vault* and pick
`<demo>/vault`. After that it opens straight on Home
([obsidian.md](obsidian.md#obsidian-as-your-main-gui)).

`doctor` reports 0 failures and one warning, `unresolved_handles`: the
review queue the demo leaves on purpose. On the iPhone combos the Watch and
the iPhone both write sleep records, as a real pair does. The iPhone's nights
are in-bed time only and carry no value, so `health_multi_origin` stays quiet
and no night is counted twice ([apple-health.md](apple-health.md), "iPhone +
Apple Watch: two recorders").

Every answer cites its evidence. `[E3]` is a journal entry you confirmed,
`[M1]` is a metric series, `[P2]` is a transcript passage, and `[O4]` is an
imported record: a text, a call, a calendar event, a contact card or a
transaction. Imported records are cited, never asserted as fact. Click a
citation in the web UI to see the row behind it.

**The note left for you:** today's quick note is still sitting in the
text inbox, the way a real one would be (`status` lists it). In Claude Code,
run `/log` against the demo root to process it: ask Claude to "log today's
note in the demo, with `--root demo/android` on every lifecrm command". It
goes through the same intake, draft, ingest, review and apply loop as real
data.

**The read-side features** ([features.md](features.md)): the demo writes a
small gazetteer of the six real towns its persona was in
(`data/geo/places.tsv`, in place of `places fetch`), so places work offline:
`python -m lifecrm --root <demo> places trips` finds the 2022 trip to
Ljubljana and Bled, `ask "when was I in Slovenia"` answers from it, and the
web UI's Places page and the vault's `Trips.md` show it. `features`,
`onthisday`, `highlights`, `search bled`, `conversation Jake`, `weekdays` and
`review --year 2022` all run against the demo too.

## What the demo plays itself

Three jobs belong to a person in real use. The demo does them itself and
prints each one:

- **Extraction.** Claude writes the drafts via `/log`. Here they come from
  `lifecrm/demo/journal.py`, and every entry cites the exact character span
  of its sentence.
- **Review.** Here the demo accepts its own diffs' decision items (the two
  cross-day closures). This is only acceptable because the data is
  synthetic. Real diffs are never applied unreviewed.
- **Handle decisions.** "This phone number is Jake" is one decision per
  identifier. The demo makes those decisions from the persona: phone
  numbers and email addresses by value, Instagram, Messenger, Telegram,
  LinkedIn and Venmo accounts by the name the export gives them. It leaves
  the strangers unresolved, so the review queue has something in it: Tía
  Rosa's number on WhatsApp and her Facebook account (Rosa Treviño), the
  airline's booking address, Acme's product-team list and the office's
  calendar location — five on the iPhone combos. The Android combo adds five
  Timeline places no entity stands for (Home, Work and three unnamed
  stops).

## Safety

- The demo refuses to build into the project root or into any folder that
  already holds something it didn't create. It marks its own folders with
  `.lifecrm-demo`, and `--force` only deletes a folder carrying that marker.
- It also refuses the project's own `data/` and `backups/` and every folder
  the real `config.toml` names (the journal inboxes, the archive, the cloud
  backups), anything inside them, and anything that holds one — after
  following symlinks and junctions. `--dir data/staging` would otherwise put
  the demo among real drafts, and a later `--force` would delete them all.
  Folders are compared by file identity, not by how the path is spelled, so
  every other name for one is refused too: `\\?\C:\...`,
  `\\?\Volume{...}\...`, `\\localhost\C$\...`, a junction. A folder whose
  `data` is the real data folder (a junction named `data`) is refused as
  well. The check runs again right before `--force` deletes.
- `--dir` itself refuses a `\\?\` device path or a `\\server\share` path,
  with a one-line error, before anything else happens. Use a drive-letter,
  `~` or relative path (a mapped network drive's letter is fine).
- `demo/` is gitignored.
- No network is needed. Answers are deterministic and cited; an answer in
  prose comes from `/ask` in Claude Code. The demo's config turns the
  optional local model off (`[ask] enabled = false`, the default —
  [ask.md](ask.md)).
