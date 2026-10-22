# The demo: a whole second brain, on synthetic data

`lifecrm demo` builds a complete instance you can click through, ask
questions of, and break, without risking anything real. It is one
fictional life rendered as the real export files of each app, then run
through the real importers. The commands it runs are the same ones you
would type, and they run in the same order.

Two setups are supported end to end, and the demo builds either one:

| combo | computer | phone | what it imports |
|---|---|---|---|
| `android` | Windows laptop | Samsung Galaxy | Health Connect export, SMS Backup & Restore (texts + calls), Google Contacts `.vcf`, Google Calendar Takeout, Google Keep Takeout, Chase CSVs, Samsung Gallery photos (EXIF), Google Maps Timeline, Chrome history, WhatsApp (Android format), Claude + ChatGPT exports |
| `iphone` | MacBook | iPhone | Apple Health `export.zip`, the Mac's Messages `chat.db`, Call History, the Mac's Contacts store, Apple Calendar `.ics`, Apple Notes `NoteStore.sqlite`, Apple Card CSVs + a credit union `.qfx`, Mac Photos library, Safari history, WhatsApp (iOS format), Claude + ChatGPT exports |

The iPhone combo has no location sample, because Apple's Significant
Locations cannot be exported ([location.md](location.md)).

Both start from the same journal: thirteen logged days spread over the six
weeks. Each day is an evening note, sometimes with quick notes, dropped into
the text inbox under the file name that combo's phone would give it. The
notes go through intake, one draft per day, ingest, review and apply. They
arrive as text, so whisper is not needed unless you pass `--with-audio`.

## Run it

**Windows (laptop + Samsung):**

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1
```

**Mac (MacBook + iPhone):**

```bash
/bin/bash scripts/setup.sh
/bin/bash scripts/demo.sh
```

Each script builds `demo/<combo>/` the first time (about two minutes), then
opens the web UI at `http://127.0.0.1:8765/` (`serve --open`: the browser
opens once the server is listening). Either runs from any folder, Explorer's
*Run with PowerShell* included. `demo.ps1` builds the android
combo by default and `demo.sh` the iphone one. Pass `-Combo iphone` /
`--combo android` to build the other setup on the same machine. Both work on
either OS, because every input is a file. To rebuild from scratch, use
`-Rebuild` / `--rebuild`. `-NoServe` / `--no-serve` builds without serving,
and `-Port N` / `--port N` serves on another port.

You can also call the command directly. Here `python` is the project's venv
interpreter (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on a
Mac):

```bash
python -m lifecrm demo --combo android          # or iphone, or both
python -m lifecrm demo --combo iphone --force   # rebuild
python -m lifecrm demo --dir ~/lifecrm-demo     # build somewhere else (one combo)
python -m lifecrm demo --anchor 2026-10-22      # pin the demo's "today" (default: today's journal date)
python -m lifecrm demo --with-audio             # also speak + transcribe one note
python -m lifecrm demo --no-ask --json          # skip the sample questions; print the summary as JSON
```

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

People, places, phone numbers and emails are all fictional (`example.com`,
`555-01xx`).

The demo writes its own `config.toml` in the demo root: the persona's
timezone and the 05:00 rollover, the synthetic sync folders, each
connector's account, and an `[owner]` table that says who the owner is:

```toml
[owner]
name = "Alex Rivera"
handles = ["email:alex.rivera@example.com", "phone:+15125550100"]
```

`handles` uses the `kind:value` shape that `handles link` takes.

## Things to try

Replace `<demo>` with the path the build printed, for example `demo/android`.
The build ends by printing the answers to ten sample questions and several
of these commands with the root filled in.

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

The build already wrote an Obsidian vault to `<demo>/vault` (open that
folder as a vault in Obsidian). Running
`export --format obsidian --out <demo>/vault` again rewrites only the notes
that changed.

`doctor` reports 0 failures and one warning, `unresolved_handles`: the
review queue the demo leaves on purpose. On the iPhone combo the Watch and
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
  identifier. The demo makes those decisions from the persona and leaves a
  few unresolved, such as WhatsApp's "Tía Rosa", so the review queue has
  something in it.

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
- No network is needed. A local model (Ollama) is used only if one is
  running, and only to phrase answers. Without one, answers are
  deterministic.
