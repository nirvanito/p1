# Life CRM

Personal life database: voice notes + dictated quick notes -> local whisper /
direct text intake -> reviewed extraction (via /log) -> SQLite with full
provenance, closable loops, entry links and entity relationships; beside it,
an observed tier of bulk imports (texts, calls, email, Instagram/Messenger/
Telegram, contacts, LinkedIn, calendar, notes, photos, location, browser,
Google searches/YouTube, finance, Venmo/PayPal, Uber, workouts, food,
Spotify/Last.fm, GPS tracks, Kindle/Goodreads, health, AI chats) that is
cited, never asserted. Sources that live only online (Google Calendar,
Gmail, Google Contacts, Last.fm) are fetched each morning once connected.

## Rules

- **All writes go through `python -m lifecrm ...`** (which routes through
  `lifecrm/domain/`). Never modify `data\lifecrm.db` with direct SQL writes,
  never edit files under `data\` by hand.
- Ad-hoc reads: `python -m lifecrm sql "SELECT ..." --json` (read-only
  connection; writes are blocked at the connection level).
- The extraction contract is `docs/contract.md` (contract v2, schema v8). A
  day with several sources extracts as ONE draft. Drafts go to
  `data\staging\`, then `lifecrm ingest` -> review -> `lifecrm apply`. A
  draft that will never be applied is ended with `lifecrm admin discard-run`
  (audited; diff renamed .discarded; apply refuses it).
- Never apply a diff the user hasn't reviewed. Never guess on ambiguous
  entity matches. Cross-day closures (`entry_updates`) and retract proposals
  are always explicit user decisions. `entries[].uuid` re-extracts an active
  entry of the draft's own day only; any other cross-day change is
  `entry_updates` (closures) or `lifecrm entry edit`.
- **An importer may not write entries, measurements, mentions,
  entity_aliases or entity_links.** Relationships come only from a reviewed
  draft's `entity_links` (stated, never inferred) or `entities link add`.
- **The daily collect job never does a source's first import.** `lifecrm
  collect` finds every source on the computer by itself (no config), applies
  a routine plan, and HOLDS (writes nothing, records why) a first import
  from a connector + account, a mass removal, or invalid records. Only the
  owner's yes (`collect approve`, or /morning on their answer) imports a
  held one; `collect decline` is their never.
- **Online sources are connected by the owner, read-only, and held like any
  other.** `lifecrm connect <service>` is run by the owner in their own
  terminal: never type a password, app password, API key, private link or
  token yourself. What a service needs lives in `data\secrets.json` (never
  config.toml, never printed, never in a result); fetching only reads the
  owner's own data and never changes anything on the service; what is
  fetched lands in `data\fetched\<service>\` and goes through the same
  plan and holds (a first import waits for the yes). docs/online.md.
- **A missing row is unknown, never zero.** A connector declares a day
  complete (`scan["coverage"]` -> `import_coverage`, schema v7) only from a
  guarantee in its source format (an OFX statement's DTSTART/DTEND, a full
  SMS backup, a whole Messages store, a full-history fitness export), never
  from its rows' dates; `lifecrm/series.py` turns declarations into
  known-zero days and drift, goals and follow-ups say "insufficient coverage"
  or "not judged" otherwise. A goal (`data.goal` on a `plan`) is validated by
  `domain/goals.py` on every write path and decided by its metric's
  aggregation (a count is met when reached, a level only when the period is
  over with enough known days).
- Filename dates come in two conventions (`transcribe.FilenameStamp`). A
  dotted time + UTC offset is the iPhone Shortcut (and `voice` notes) and
  carries the JOURNAL date. Every other shape (Android recorders, Samsung
  call recordings `_yyMMdd_HHmmss`, hand-renamed files) carries the CALENDAR
  date, and the rollover is applied to it. `[time] filename_date =
  auto|journal|calendar` overrides; `lifecrm filedate <name>` shows the
  result.
- **Call recordings are transcribed only by the owner's pick.** Intake holds
  every inbox file named like one (`transcribe.is_call_recording`: "Call
  recording…", "Call with…", "Call_"/"Call-", Samsung's `_yyMMdd_HHmmss`,
  `[intake] call_patterns`). `calls take <n>` transcribes it into a normal
  source, and `calls skip <n>` archives it untranscribed. /log asks which
  to take and never reads one the owner did not pick. `[intake]
  call_recordings = "auto"` is the owner's opt-out.
- `data\`, `backups\`, and `config.toml` are private and gitignored — never
  commit them, never paste transcript contents into commit messages.
- Schema changes are numbered migrations in `lifecrm/migrations/`
  (`0002_*.sql`, consecutive, no gaps) applied atomically by the runner in
  `lifecrm/db.py`; `lifecrm migrate` runs them with a pre-migration snapshot.
- This project uses interactive Claude Code only — no Anthropic API calls,
  no headless metered workflows.

## Commands

- Run everything from the project root with `.venv\Scripts\python.exe`
  (Python 3.12 venv; system 3.14 lacks ctranslate2 wheels). Set `PYTHONUTF8=1`.
- On a Mac: `.venv/bin/python -m lifecrm ...`; scripts are
  `/bin/bash scripts/x.sh` (LF, enforced by .gitattributes); `LIFECRM_PYTHON`
  overrides the interpreter the .sh scripts use. Scripts (.ps1 / .sh): setup,
  transcribe-inbox, doctor, backup, verify-restore, restore, review,
  reminders, collect, demo, install-tasks (+ uninstall-tasks.sh).
  install-tasks registers collect (06:30, + logon on Windows), intake (07:00 + logon), doctor
  (08:00), backup (21:30), verify-restore (Sun 12:00), review (Mon 07:30),
  reminders (07:15) as Task Scheduler `LifeCRM *` / launchd
  `local.lifecrm.*`; logs in `data\*.log`.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (pass
  `--basetemp=<scratch dir>` if the default temp root is blocked). Every
  concurrent pytest run needs its own `--basetemp`: pytest deletes a basetemp
  at session start. `.github/workflows/ci.yml` (Windows + macOS) runs only
  once the repo is pushed.
- Full surface (`python -m lifecrm --help`): init, intake (pulls a linked
  phone's notes first; holds call recordings), transcribe (`--date` only with `--file`; a named file is
  transcribed even if it is a call), calls (list | take <n|name>... |
  skip <n|name>... [--all]), import, ingest, apply, status, filedate,
  doctor, sql, export (jsonl | markdown | obsidian; obsidian has --dry-run),
  backup, verify-backup (restore a snapshot into a scratch root, doctor it,
  compare every table with a lower bound; exit 1 on failure), restore,
  migrate, configure (writes config.toml for this machine; --force to
  replace), where (read-only report of what this machine can import),
  health (import [PATH] — Health Connect zip or Apple Health
  export.zip/folder/xml, auto-detected, path optional; dry-run by default;
  retract/restore --source-file; Apple keys look like
  `export.zip#2026-10-22T02:30:00Z`),
  dashboard (Datasette over a snapshot — never the live db),
  ask (cited answers; deterministic retrieval; `--check FILE` verifies a
  written answer's citations; optional local model, off by default),
  serve (web UI over a snapshot, read-only for the database; --host
  0.0.0.0 for the phone, --token random; writes recall_log (--no-log),
  and — only with --token — [features] in config.toml from the Sources
  page's checklist, POST /api/features through features.save),
  open (the main GUI: `[gui] main` = web | obsidian, --gui for once),
  obsidian (refresh [--dry-run] | open [--no-refresh]; the vault named by
  `[obsidian] vault`, which every command that changes the db refreshes —
  `auto_refresh = false` / LIFECRM_NO_VAULT_REFRESH=1 to skip),
  voice (--text/--file/--once/--wake/--no-speak/--no-llm/--json; never
  writes the db, never applies),
  review (evidence pack: --lens week|health|career|relationships|finance|all|year,
  --week/--start/--end/--days/--year YYYY (picks the year lens), --json |
  --write-only | --check BRIEF [--out FILE]; read-only, no model),
  nutrients (the food diary's daily averages vs adult reference intakes for
  [nutrition] sex/age: low/borderline/high, the foods behind each, every
  number cited; --days/--start/--end, --sex/--age, --json; read-only, no
  model, "not medical advice" first),
  nudges (what matters today, cited: reminder | follow_up | goal | drift |
  birthday | nutrient | collect | stale | lapsed nudges N# resting on
  E#/M#/H#/O#/C#, plus the agenda; --json, --all, --today,
  --out FILE saves the pack, --check BRIEF --pack FILE checks against that
  saved pack, never a rebuild; `nudges snooze|dismiss <key>` by durable
  key (an N# only with --pack), `restore <key>`, `states`; the clock's
  journal date for the CLI, the as-of date for the vault),
  reminders (publish [--today] [--now] [--out] [--dry-run]
  [--if-configured]: the one publisher of the .ics — revisions recorded
  in reminder_revisions, data/reminders/ + `[assistant] reminders_dir`,
  served by `serve` at /reminders.ics),
  connect [SERVICE] [ARGS]... [--json] (an online source, once: lists the
  services with their steps when no service is named; asks for what one
  needs, hidden) | disconnect SERVICE (forgets its secret; fetched files and
  imports stay) | fetch SERVICE (download now, import nothing; "nothing new"
  for a quiet incremental service),
  collect ([--dry-run] [--status] [--if-configured] [--json]: every source
  on this computer, or `[collect] sources`; each from where its connector
  looks with no path; routine plans applied through observe's own write
  path, a first import / mass removal / invalid records HELD as a question;
  `run SOURCE...`, `approve SOURCE [--yes]` (the dry run, then the import),
  `decline SOURCE`, `status`, `setup [--phone android|iphone-windows|
  iphone-mac]` (the phone's side as a checklist); every decided outcome a
  `collect_runs` row; exit 1 only when a source failed; docs/collect.md),
  patterns (EXPERIMENTAL: co-occurrence between daily series, --days,
  --today, --keys, --all, --json; off in nudges/review//morning unless
  `[assistant] patterns = "on"` or `nudges --patterns`; docs/patterns.md),
  features (the read-side features' checklist: list [--json] | check KEY|N...
  [--all] | uncheck ... [--all] | pick; `[features]` in config.toml, a key
  not listed is on; docs/features.md),
  places (fetch [--dataset] [--dry-run] — the one network call, GeoNames into
  data/geo/places.tsv | status | trips [--year] | countries | where DATE;
  docs/places.md), onthisday [--date], prompts [--date] (default yesterday),
  highlights [--date], subscriptions [--today], experiments [--today] [--all],
  search WORDS... [--tier --stream --person --year], links URL|SITE | --from
  PERSON, conversation PERSON, weekdays [--days] — all read-only, each
  refusing while its feature is unchecked,
  demo (--combo android|iphone|windows-iphone|both|all — both = android +
  iphone; --dir, --anchor, --force, --with-audio, --no-ask, --json),
  observe/connectors/imports/handles (observed tier — bulk imports that never
  pass the review gate; dry-run by default; connectors ai_chat, android_sms,
  browser, calendar, contacts, email, finance, fitness, google_activity,
  gps_track, ios_calls, ios_messages, lastfm, linkedin, location,
  mac_messages, meta_messages, notes, nutrition, payments, photos, reading,
  rides, spotify, telegram, whatsapp; `connectors` counts each stream's rows per connector (finance and
  payments share txn); `imports list/retract/restore` —
  restore puts back exactly what retract hid, never what mirroring removed;
  `handles list --suggest` offers entity candidates (splits 'A / B' names,
  skips owner placeholders, falls back to the name on a contact_card holding
  the handle), never links; `handles link/ignore`),
  entities (match/list/show/merge/alias-add; `entities link
  add/list/rm/restore/types/type-add` — relationships between entities),
  entry (show/edit/retract/restore/merge/close/reopen), link (add/list/rm),
  categories, kinds, admin (purge-day; discard-run <run uuid or 8-char
  prefix>), version. The global `--root DIR` (or `LIFECRM_ROOT`) runs any
  command against another project root (a demo, a restore target).
- Skills (`.claude/skills/`, mirrored identically in `.agents/skills/`):
  /log, /ask <question> (Claude writes a cited answer from `ask --json
  --no-llm`, verified by `ask "<q>" --check <file>` before it is shown),
  /review-week, /advise <lens>, /morning (a cited "what matters today"
  from a saved `nudges --json --out` pack, checked with `nudges --check
  --pack` against that file; a kept brief is a reflection with
  data.derived_from = "lifecrm morning").

## Layout

- `lifecrm/domain/` — the single validated write layer (audit + revisions).
- `lifecrm/contract.py` — deterministic draft validation, single-line errors.
- `lifecrm/domain/reconcile.py` — draft(s) vs existing day -> reviewable
  diff; confirmed records are never silently overwritten.
- `lifecrm/domain/entity_links.py` — entity <-> entity relationships (schema
  v5, confirmed tier): typed edges read as `FROM type TO` (Jake `from` El
  Paso, Linda `parent_of` Alex); types are rows of `entity_link_types`
  (`entities link type-add`); symmetric types stored once, lower id first;
  retract, never delete; entity merge repoints; `for_entity()` is the read
  path.
- `lifecrm/doctor.py` — invariant verification (`lifecrm doctor`).
- `lifecrm/export.py` — deterministic jsonl/markdown archives + the obsidian
  vault mirror (build_vault = pure render; export_obsidian =
  marker/manifest-guarded write: never writes or deletes a file without the
  `lifecrm: generated` front-matter line). See `docs/obsidian.md`.
- `lifecrm/obsidian.py` + `commands/obsidian.py` + `commands/gui.py` — the
  vault as a main GUI: the start pages (Home, People, Health, Sources) built
  from web/api.py's reads and dated as of the newest audit row, never the
  clock; `[obsidian] vault` / `[gui] main`; the change stamp (newest audit
  row + schema version) cli._arm_vault_refresh compares around every
  command to re-export after a write; the obsidian:// hand-off (Obsidian's
  own obsidian.json is read, never written). One way: nothing is read back.
- `lifecrm/health_connect.py` + `lifecrm/domain/health.py` — Health Connect
  export import into `health_records` (deterministic, external-id dedupe,
  newer-wins updates, conflicts surfaced; dry-run by default; sleep dated by
  the wake day).
- `lifecrm/apple_health.py` + `lifecrm/health_import.py` — Apple Health
  export import into `health_records` (streamed XML, 'ah1:' hash ids);
  `health_import` picks the reader from the file and finds the newest export
  in Downloads / iCloud Drive / OneDrive. Not a connector: it never enters
  the observe registry. See `docs/apple-health.md`.
- `lifecrm/domain/observations.py` + `lifecrm/connectors/` — the OBSERVED
  tier (schema v4). Two tiers, one rule: `entries`/`measurements`/`mentions`/
  `entity_links` are user-confirmed; `observations`/`health_records` are
  bulk-imported, never reviewed, and never asserted as fact without citing
  the row. Connectors are pure readers (`connectors/base.py` contract);
  dedupe, newer-wins classification, conflict surfacing, transactional
  apply, retraction/restore and mirroring live once in
  `domain/observations.py` (a connector that declares `present_ids` has what
  its source no longer holds removed, scoped to its own streams and a named
  account; a row an entry or a relationship cites is hidden, not deleted;
  the owner chose "mirror the source" over "remember", 2026-09-24; a store
  that ages out its oldest end by itself — the three text connectors, for a
  keep-messages limit — declares `mirror_ages_out`, and only what is missing
  from inside the span it still holds is removed: older rows aged out and
  stay, with their declared days). A `default_target` that finds several
  candidates raises `base.Ambiguous`; a snapshot copy goes through
  `base.sqlite_backup`, which gives up on a locked source instead of
  hanging.
  Importers emit *handles*, never names — linking one handle attributes
  every observation that referenced it, with no backfill pass. See
  `docs/observed-tier.md`. Connectors (each has a doc: `docs/<name>.md`, except
  ai_chat -> `docs/ai-chat.md`, ios_messages/mac_messages -> `docs/texts.md`,
  ios_calls -> `docs/calls.md`, android_sms -> `docs/android-texts.md`;
  `meta_messages` and `google_activity` keep the underscore):
  - `ai_chat` — Claude/ChatGPT exports; mirrors per vendor + account.
  - `ios_messages` — iPhone backup -> iMessage/SMS/RCS; mirrors;
    `[connectors.ios_messages] path` names the backup when there are several.
    `connectors/typedstream.py` decodes `attributedBody` (written from the
    format; crabstep/imessage-exporter are GPL-3.0 — never copy their code).
  - `mac_messages` — the Mac's `chat.db` via ios_messages' reader under its
    own mirroring scope (iMessage GUIDs dedupe with a backup). `docs/texts.md`.
  - `ios_calls` — CallHistory.storedata from a Mac or a backup; stream
    `ios_call`; remembers (iOS ages out old calls). `docs/calls.md`.
  - `android_sms` — SMS Backup & Restore XML; streams android_sms /
    android_mms / android_call; texts mirror only a type="full" file whose
    count matches, with a known account; calls are remembered (the phone's
    call log ages old calls out). `docs/android-texts.md`.
  - `whatsapp` — "Export chat" .txt/.zip, one chat per export; remembers.
  - `contacts` (+ `_vcard.py`, `_ios_addressbook.py`, `_abcddb.py`) — vCard,
    Mac Contacts, iPhone AddressBook as `contact_card`; handles carry the
    card's name (what `--suggest` matches); mirrors per account (a vCard's
    account: every card's `X-LIFECRM-ACCOUNT`, which the Google Contacts
    fetch writes, else `[connectors.contacts] account`).
  - `calendar` (+ `_ics.py`, stdlib RFC 5545) — .ics / Takeout ->
    `calendar_event`, repeats expanded to today + 90 days; mirrors per
    calendar or account.
  - `notes` (+ `_protobuf.py`, stdlib wire-format reader for Apple Notes) —
    .md/.txt folder, Keep Takeout, NoteStore.sqlite -> `note`; mirrors per
    account.
  - `finance` (+ `_ofx.py`) — bank CSV presets + [connectors.finance]
    mapping, OFX/QFX/QBO -> `txn` (signed value, ISO currency, no
    auto-categorisation, card handles only); remembers.
  - `photos` (+ `_exif.py`) — metadata only (never pixels) from a folder,
    Google Photos Takeout or Mac Photos library -> `photo`; only the library
    mirrors.
  - `location` — Google Maps Timeline visits -> `place_visit`, place_id
    handles; remembers.
  - `gps_track` — GPSLogger's daily GPX/CSV/GeoJSON tracks (OneDrive ›
    GPSLogger, or one folder down; `[connectors.gps_track] path`) ->
    `place_visit` with location.py's keys: stays of 100 m / 10 min, a gap
    bridged only when both sides agree (<= 12 h), excursions under 10 min
    dropped; `place_id` handle `gps:<geohash7>`; `gps@<start>` ids, so a
    growing file updates its open visit; remembers; stale after 7 days.
  - `browser` — Chrome/Edge/Brave/Safari history -> `browser_visit`, one row
    per (URL, day); remembers.
  - `email` — Gmail Takeout mbox (read inside the zip), Apple Mail export or
    `~/Library/Mail`, Thunderbird, .eml -> `email` (own words: quoted reply
    and signature trimmed); email handles sender/recipient; direction from
    [connectors.email] addresses, else [owner], else Sent; a first header
    line `X-Lifecrm-Account` (the Gmail fetch) is the scan's account;
    remembers.
  - `meta_messages` — Instagram/Facebook "Download your information" (JSON
    only) -> `instagram_dm` / `messenger`; Meta's Latin-1 mojibake repaired;
    `instagram:`/`messenger:` app_account handles; remembers.
  - `telegram` — Telegram Desktop's JSON export (ChatExport_/DataExport_)
    -> `telegram`; `telegram:user<id>` handles; channels/bots counted, left
    out; remembers.
  - `google_activity` — Takeout My Activity + YouTube history (JSON or
    HTML, streamed) -> `web_search`, `video_watch`; no handles; remembers.
  - `fitness` — Strong / Hevy CSV, Strava export_<id>.zip -> one `workout`
    per session, sets in value_json (top_set, weight_kg); remembers.
  - `spotify` — my_spotify_data.zip (account data + extended history) ->
    `music_play`, one per track per day; IPs/devices never read; remembers.
  - `lastfm` — Last.fm scrobbles (Spotify scrobbling) -> `music_play` in
    spotify's shape: one per track per day, plays but no minutes (value/unit
    empty), kind music, ids of its own; reads the pages `online/lastfm.py`
    fetches (per day the newest fetch wins); no handles; remembers; stale
    after 14 days. With a Spotify download for the same days a track-day
    counts twice: one source per stretch of time.
  - `reading` — Kindle My Clippings.txt -> `highlight`, Goodreads CSV ->
    `book`; finds a plugged-in Kindle; remembers.
  - `nutrition` — Cronometer servings/dailysummary (MyFitnessPal meal
    totals) -> `food_log`, `nutrition_day`; stale after 45 days;
    remembers. `lifecrm/nutrients.py` + `commands/nutrients.py` is the
    cited report (`lifecrm nutrients`); imports nothing from recall/review.
  - `linkedin` — the data export -> `linkedin_connection` (MIRRORS per
    owner profile: Connections.csv is the complete list) and
    `linkedin_message` (remembers); `linkedin:in/<slug>` handles.
  - `payments` — Venmo statements, PayPal activity CSV -> `txn` with a
    `counterparty` handle (venmo:<name> / PayPal email); transfers marked
    value_json.transfer; remembers.
  - `rides` — Uber's data export -> `ride` (fare, not a signed txn) and
    `food_order` (Eats); no handles; remembers.
  macOS-guarded folders need Full Disk Access; a denial is one DomainError
  line saying so (`ios_messages.privacy_guard`).
- `lifecrm/recall.py` + `lifecrm/llm.py` — `lifecrm ask`: rules parse the
  question (window + metrics + people), SQL answers it, every fact carries a
  citable id: M# statistics, E# confirmed entries, P# transcript passages,
  O# observed records (always rendered "(observed, not confirmed)").
  Relationships are cited through the entry that established them. The
  local model (Ollama over stdlib HTTP, optional and OFF unless `[ask]
  enabled = true` — /ask in Claude Code is the prose route; never via a
  proxy) may only
  PHRASE that evidence — never supply facts; evidence is fenced as data, and
  its citations are checked per sentence (a number or date not in the cited
  evidence -> the deterministic answer replaces it, `model_rejected`).
  Retrieval must stay read-only and model-free.
- `lifecrm/review.py` + `lifecrm/commands/review.py` — the weekly review /
  advisor evidence pack (D/E/M/H/O ids, prior-period deltas, lenses). The
  /review-week and /advise skills write the cited brief; a kept brief is a
  `reflection` entry (data.derived_from = "lifecrm review") through import ->
  ingest -> review -> apply, excluded from later packs. `docs/review.md`.
  Every generated interpretation's derived_from starts with "lifecrm "
  (`review.NOT_DERIVED` / `is_derived`); an entry without the mark is
  evidence.
- `lifecrm/nudges.py` + `lifecrm/commands/nudges.py` — the assistant's
  "what matters today" (schema v6). Read-only, deterministic: reminder
  (due within `due_days`), birthday (linked contact cards), nutrient
  (`nutrients.build_report` lows), collect (a held / missing / failed daily
  import), stale (connector freshness over its streams, only once the
  connector has an applied import of its own — doctor and the Sources page
  share the rule), the agenda;
  N# nudges rest on E#/O#/C# through `review._Ids`; `pack_id` hashes the pack;
  `check` = `recall.check_text` against a SAVED pack. State (snooze /
  dismiss / restore) is `lifecrm/domain/nudges.py` -> `nudge_states`, keyed
  by durable key, audited. `docs/assistant.md`.
- `lifecrm/series.py` — the daily-series registry (mood, energy, sleep,
  steps, resting heart rate, weight, exercise minutes, workouts, spending,
  texts, people in touch, `contact:<id>`, `cat:<name>`, `nutrient:<key>`):
  values per day, the days each series KNOWS (`known`), and the rows behind
  each value (`refs`, never pack ids). Known zeros only from
  `domain/observations.coverage_days` (declared coverage); workouts merge
  sessions across the fitness app, the sensor and the journal (overlap or
  30 min). `contact_days` is the per-person view follow-ups and lapsed use.
- `lifecrm/domain/goals.py` (the rule) + `lifecrm/goals.py` (the reading) —
  goals: `validate_entry_data` runs in `entries.create`,
  `corrections.edit_entry` (on the resulting entry) and `contract.py`;
  `progress`/`in_period` decide periods by aggregation (cumulative:
  reached -> met, missed only with full coverage, `projected`; level:
  `observed_mean`, `above_target`, decided only when over with `min_known`
  days), streaks say what ended them. Review pack section `goals` (every
  lens, never None).
- `lifecrm/patterns.py` + `lifecrm/commands/patterns.py` — the pattern
  finder, EXPERIMENTAL (docs/patterns.md): every pair of series at lags 0
  and 1 on CALENDAR days, linear detrend, Spearman, Fisher z on a
  CONSERVATIVE effective n (lag-1 autocorrelation + one SE), Benjamini-
  Hochberg over the whole family (`family_size`), floors |rho| >= 0.4 and
  n >= 30, a circular day-shift check printed beside reported pairs.
  `nudges.pattern_items` cites them as P# (shared by the nudges pack and
  review's `patterns` section); off unless `[assistant] patterns = "on"`.
  The validation gate is tests/test_patterns_validation.py (R 40 in the
  suite, LIFECRM_PATTERN_GATE=1 for R 200 on the reserved seeds); the
  reserved run was recorded in docs/patterns.md on 2026-10-25 (all suites
  pass). The default stays off: turning it on is the owner's decision, not
  a test result. A false pair in the gate is one involving an independent
  series (a planted pair's next-day echo is a true, dependent association).
  Wording: co-occurrence, never cause.
- `lifecrm/features.py` + `lifecrm/commands/features.py` — the read-side
  features (docs/features.md), fourteen readings recomputed from the records,
  none storing anything, each checked or unchecked in `[features]`
  (`features.save` rewrites only that table and refuses unless the rest of
  config.toml parses identically): `memories.py` (on this day), `prep.py`
  (meeting prep, `A#` per person under the agenda), `places.py` (GeoNames
  gazetteer on a CELL grid, home = most days within 90 either side, trips,
  countries; docs/places.md), `prompts.py` (journal prompts — a prompt, never
  an entry), `subscriptions.py` (cadence over txn, price rise over
  max(1.99, 5 %), "stopped" only with declared coverage), `experiments.py` +
  `domain/experiments.py` (data.experiment), `search.py` (faceted FTS, AND,
  newest first), `jobchanges.py` (audit-log updates on linkedin_connection),
  review's `year` lens, `links.py`, `gifts.py` + `domain/gifts.py`
  (data.gift, data.anniversary), `web/api.conversation`, `weekday.py`,
  `highlights.py` (hash rotation, no state). `ask_extra.py` answers their
  question shapes as metrics carrying their own line and sentences
  (recall `_EXTRA_KINDS`); nudges adds kinds experiment, anniversary,
  price_change, new_recurring, job_change and sections on_this_day, prompts,
  highlights; review adds experiments, trips, subscriptions, weekdays,
  job_changes, year (`SECTION_FEATURES`). The data shapes are validated on
  every write path whether or not their feature is checked.
- `lifecrm/reminders.py` + `lifecrm/commands/reminders.py` +
  `lifecrm/domain/reminders.py` + `lifecrm/ics.py` — the reminders calendar.
  ONE publisher (`publish`: BEGIN IMMEDIATE, components, revisions, atomic
  file, commit, copies); revisions in `reminder_revisions` (fingerprint,
  sequence, tombstones) through the domain module, whose audit rows the
  as-of stamp ignores (`domain.reminders.CONTENT_CHANGE`). Due items are
  timed events (overdue -> today), birthdays (and, with gift_ideas checked,
  anniversaries) dated occurrences this year and next, never RRULEs; snoozed moves, dismissed omits. Runs from the CLI, the
  after-write hook (`cli._arm_vault_refresh`, with the vault refresh) and the
  07:15 job; `serve` returns the published bytes at /reminders.ics.
  `ics.py` is the shared RFC 5545 writer (the demo's calendar uses it too).
- `lifecrm/online/` + `lifecrm/commands/connect.py` — online sources
  (docs/online.md): a module per service (SERVICE, CONNECTOR, TITLE, DOC,
  steps, connect, fetch -> Path | None, optional settle), discovered by
  `online.registry()`; `online.fetch` downloads into data/fetched/<service>/
  in a format the connector reads, wrapping OSError as one DomainError line;
  `secrets.py` keeps what connect returned in data/secrets.json (atomic,
  0600). collect fetches a connected service instead of the connector's
  `default_target` (a failed fetch is an error, never a skip; None = nothing
  new -> unchanged) and calls `settle` only after a committed import, so an
  incremental fetch stays in pending/ until the yes (observe --apply settles
  too). Services:
  - `google_calendar` — each secret iCal link's whole state, one .ics per
    link; no settle.
  - `gmail` — IMAP + app password into gmail/pending/ as .eml; read-only
    (EXAMINE, BODY.PEEK, certificate checked); all new mail every morning,
    older mail 2,000 (250 MB) a morning newest-first only after the first
    yes; UIDVALIDITY + UID cursor in secrets.json; over 1 MB fetched without
    attachment bytes (X-Lifecrm-Omitted); X-Gmail-Labels as Takeout spells
    them; settle moves what was imported to archive/. docs/gmail.md.
  - `google_contacts` (+ `_google_oauth.py`: stdlib OAuth for installed apps
    — loopback 127.0.0.1 on a random port, PKCE S256, state checked, refresh
    at oauth2.googleapis.com; HTTP behind `_open`, the browser behind
    `_open_browser`) — People API connections -> one vCard (UID =
    resourceName, REV, E.164 TEL, `X-LIFECRM-ACCOUNT google:<email>`);
    invalid_grant -> "needs connecting again". docs/google_contacts.md.
  - `lastfm` — user.getRecentTracks pages, whole journal days only, into
    lastfm/pending/; new days each morning plus a newest-first backfill of
    50 pages a morning cut at day boundaries; settle -> archive/.
    docs/lastfm.md.
- `lifecrm/collect.py` + `lifecrm/commands/collect.py` +
  `lifecrm/domain/collect.py` — the daily collection (schema v8,
  docs/collect.md). Sources: `[collect] sources` when set, else
  `collectable()` — every connector with `default_target`, and "health"
  (found one folder down in OneDrive too; `health_dir` adds a folder). Per
  source: the connector's own target, its scan and `build_plan`, then
  applied | unchanged | held (first_import per connector + account,
  mass_removal, invalid) | missing (named only) | error; with no list, a
  source not found, or found for the first time but unreadable or empty, is
  `skipped` and leaves no row — but a source macOS refuses (`blocked()`: a
  PermissionError in the chain or a connector's "Full Disk Access" line) is
  always an `error`, never skipped, and the nudges pack folds every such
  source into one `collect:macos:blocked` item. `approve` = the owner's yes (preview = the
  connector's report_lines; apply allows the held removal, records the run
  with actor user, clears a decline); `decline` = a dismissed nudge key
  `collect:<source>:declined` (the first-import key dismissed counts too).
  `setup_checklist` is per phone (PHONE: android, iphone-windows,
  iphone-mac; `guess_phone`): collecting | waiting | to do (with the
  guides' steps) | declined, from `default_target` lookups only.
  `apply_observed` is the one observed-tier write path, shared with
  `observe --apply` (refusal, pre-removal snapshot, apply_plan). A new file
  that only declares coverage applies (a quiet day is a known zero); the
  same file again is unchanged; a conflict holds nothing. Each source is its
  own transaction with its `collect_runs` row; one failing never stops the
  next. Rows are bookkeeping: `CONTENT_CHANGE` (the as-of date) ignores
  them, `REFRESH_CHANGE` (the after-write hook) does not. Read by `collect
  status`, doctor (`collect_waiting`, `collect_last_run`) and the nudges
  pack (kind `collect`, key `collect:<source>:<why>`, resting on C#; a
  first import's title is the question). The 06:30 job is
  scripts/collect.* with --if-configured. Tests isolate the home folder
  (tests/test_collect.py `home` fixture): with no list, a run looks
  everywhere a connector looks.
- `lifecrm/phone.py` — a phone Windows 11 shows in File Explorer (Link to
  Windows: a cloud-files sync root at `%USERPROFILE%\CrossDevice\<phone>\
  storage`, found through the registry's SyncRootManager, never by folder
  name) read directly: `pull` copies — never moves — the recorder's and the
  quick-note folder's new files into the inboxes once (`data/phone-
  pulled.json`; intake's content-hash check stands behind it), `folders()`
  is one more place android_sms and gps_track look; every call has a
  timeout, and a phone that is away is "not reachable" (collect: skipped
  or "missing", never an error). NEVER relied on: the sync-app route stays
  and is the only one on a Mac / with an iPhone; `[phone] read = false`
  opts out. The Windows collect task also runs 3 min after logon
  (modern standby keeps no wake timer). docs/android.md.
- `lifecrm/web/` + `lifecrm/commands/web.py` — `lifecrm serve`: stdlib HTTP
  over a backup-API snapshot in data/web/lifecrm.db, re-copied when the live
  db or its -wal changes; one read-only connection per request; `api.py`
  reads, `index.html` is the single offline page (CSP forbids external
  fetches). Two writes: recall_log, and — only on a server with a key,
  same-origin JSON — [features] via `features.save` (POST /api/features;
  the server re-reads [features] whenever config.toml changes). Never
  the database otherwise. No capture UI. `docs/web.md`.
- `lifecrm/voice.py` + `lifecrm/commands/voice.py` — the voice loop: a front
  end over recall.answer() and the quick-note inbox, never a write path; not
  logged to recall_log; TTS through a constant OS-engine script with the text
  on stdin, never a shell; optional deps only in requirements-voice.txt.
  `docs/voice.md`.
- `lifecrm/views.py` — read-only view assembly shared by CLI, web and voice.
- `lifecrm/platforms.py` — where things live on Windows vs macOS (cloud
  drives, Messages, backups); proposes paths, config.toml always wins.
- `lifecrm/commands/` — CLI plug-ins (`register(app)`), discovered by
  cli.py: calls, collect, configure, connect (connect, disconnect, fetch),
  demo, entity_links, features, filedate,
  gui (open), memory (onthisday, prompts, highlights), money (subscriptions,
  experiments), nudges, nutrients, obsidian, places, reminders, review,
  search (search, links, conversation, weekdays), verify (verify-backup),
  voice, web (serve), where.
- `lifecrm/demo/` — `lifecrm demo`: persona + journal drafts + one export
  generator per source in `demo/sources/`, run through the real CLI into a
  `.lifecrm-demo`-marked root (never the project's data). `docs/demo.md`.
  `persona.DEVICE` gives each combo its phone OS, computer OS and browser
  (read through has_iphone / on_mac / on_windows / browser); the
  windows-iphone combo's texts are `demo/sources/ios_messages.py`, an
  unencrypted Apple Devices backup (`sms.db` via
  `mac_messages.write_messages_db`, the phone's `AddressBook.sqlitedb`), and
  it has no calls (unencrypted backups leave them out).
- `dashboards/dashboards.yml` — dashboards-as-code (datasette-dashboards
  0.8.x: charts MUST be explicit vega-lite specs — the bar/line/pie
  shorthands in the plugin's master README don't exist in 0.8.x and fail
  silently). Four dashboards (journal, health, observed, people); `dashboard`
  passes `sql_time_limit_ms 5000`. Rules in the file header, enforced by
  tests/test_dashboard.py + tests/test_dashboards_served.py: metrics and
  tables return a row on an empty DB; date axes use utc timeUnits/scales;
  time series ORDER BY newest first; observed queries keep status='active';
  message joins walk observation_participants CROSS JOIN observations; the
  file stays ASCII.
- `docs/windows-android.md` (combo 1: Windows laptop + Samsung),
  `docs/mac.md` (combo 2: MacBook + iPhone) and `docs/windows-iphone.md`
  (combo 3: Windows laptop + iPhone) — the setups end to end;
  `docs/iphone.md` + `docs/android.md` — standalone phone capture guides.
  On GitHub (nirvanito/p1, private) each setup is a branch —
  windows-android, windows-iphone, mac-iphone — that differs from main only
  in its README quick start and demo defaults; fixes land on main and are
  merged into the three.
- Deferred (do not build unless asked): ActivityWatch screen time,
  comparison questions ("this month vs last"), Samsung `.qta` recordings.
  Bidirectional/live Obsidian sync is forbidden without an explicit decision
  (a second write path; the read-only mirror exists: export --format
  obsidian, re-rendered one way after each CLI write — nothing is read
  back). Never a capture UI; never live federation of app data.
