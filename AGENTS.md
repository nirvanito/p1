# Life CRM

Personal life database: voice notes + dictated quick notes -> local whisper /
direct text intake -> reviewed extraction (via /log) -> SQLite with full
provenance, closable loops, entry links and entity relationships; beside it,
an observed tier of bulk imports (texts, calls, email, Instagram/Messenger/
Telegram, contacts, LinkedIn, calendar, notes, photos, location, browser,
Google searches/YouTube, finance, Venmo/PayPal, Uber, workouts, food,
Spotify, Kindle/Goodreads, health, AI chats) that is cited, never asserted.

## Rules

- **All writes go through `python -m lifecrm ...`** (which routes through
  `lifecrm/domain/`). Never modify `data\lifecrm.db` with direct SQL writes,
  never edit files under `data\` by hand.
- Ad-hoc reads: `python -m lifecrm sql "SELECT ..." --json` (read-only
  connection; writes are blocked at the connection level).
- The extraction contract is `docs/contract.md` (contract v2, schema v5). A
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
- Filename dates come in two conventions (`transcribe.FilenameStamp`). A
  dotted time + UTC offset is the iPhone Shortcut (and `voice` notes) and
  carries the JOURNAL date. Every other shape (Android recorders, Samsung
  call recordings `_yyMMdd_HHmmss`, hand-renamed files) carries the CALENDAR
  date, and the rollover is applied to it. `[time] filename_date =
  auto|journal|calendar` overrides; `lifecrm filedate <name>` shows the
  result.
- `data\`, `backups\`, and `config.toml` are private and gitignored — never
  commit them, never paste transcript contents into commit messages.
- Schema changes are numbered migrations in `lifecrm/migrations/`
  (`0002_*.sql`, consecutive, no gaps) applied atomically by the runner in
  `lifecrm/db.py`; `lifecrm migrate` runs them with a pre-migration snapshot.
- This project uses interactive agent sessions only — no metered API calls,
  no headless metered workflows.

## Commands

- Run everything from the project root with `.venv\Scripts\python.exe`
  (Python 3.12 venv; system 3.14 lacks ctranslate2 wheels). Set `PYTHONUTF8=1`.
- On a Mac: `.venv/bin/python -m lifecrm ...`; scripts are
  `/bin/bash scripts/x.sh` (LF, enforced by .gitattributes); `LIFECRM_PYTHON`
  overrides the interpreter the .sh scripts use. Scripts (.ps1 / .sh): setup,
  transcribe-inbox, doctor, backup, verify-restore, restore, review, demo,
  install-tasks (+ uninstall-tasks.sh). install-tasks registers intake (07:00
  + logon), doctor (08:00), backup (21:30), verify-restore (Sun 12:00),
  review (Mon 07:30) as Task Scheduler `LifeCRM *` / launchd
  `local.lifecrm.*`; logs in `data\*.log`.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (pass
  `--basetemp=<scratch dir>` if the default temp root is blocked). Every
  concurrent pytest run needs its own `--basetemp`: pytest deletes a basetemp
  at session start. `.github/workflows/ci.yml` (Windows + macOS) runs only
  once the repo is pushed.
- Full surface (`python -m lifecrm --help`): init, intake, transcribe
  (`--date` only with `--file`), import, ingest, apply, status, filedate,
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
  ask (cited answers; deterministic retrieval, optional local model),
  serve (read-only web UI over a snapshot; --host 0.0.0.0 for the phone,
  --token random; writes only recall_log, --no-log),
  open (the main GUI: `[gui] main` = web | obsidian, --gui for once),
  obsidian (refresh [--dry-run] | open [--no-refresh]; the vault named by
  `[obsidian] vault`, which every command that changes the db refreshes —
  `auto_refresh = false` / LIFECRM_NO_VAULT_REFRESH=1 to skip),
  voice (--text/--file/--once/--wake/--no-speak/--no-llm/--json; never
  writes the db, never applies),
  review (evidence pack: --lens week|health|career|relationships|finance|all,
  --week/--start/--end/--days, --json | --write-only | --check BRIEF
  [--out FILE]; read-only, no model),
  nutrients (the food diary's daily averages vs adult reference intakes for
  [nutrition] sex/age: low/borderline/high, the foods behind each, every
  number cited; --days/--start/--end, --sex/--age, --json; read-only, no
  model, "not medical advice" first),
  demo (--combo android|iphone|both, --dir, --anchor, --force, --with-audio,
  --no-ask, --json),
  observe/connectors/imports/handles (observed tier — bulk imports that never
  pass the review gate; dry-run by default; connectors ai_chat, android_sms,
  browser, calendar, contacts, email, finance, fitness, google_activity,
  ios_calls, ios_messages, linkedin, location, mac_messages, meta_messages,
  notes, nutrition, payments, photos, reading, rides, spotify, telegram,
  whatsapp; `connectors` counts each stream's rows per connector (finance and
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
  /log, /review-week, /advise <lens>.

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
  the owner chose "mirror the source" over "remember", 2026-09-24).
  Importers emit *handles*, never names — linking one handle attributes
  every observation that referenced it, with no backfill pass. See
  `docs/observed-tier.md`. Connectors (each has a doc: `docs/<name>.md`, except
  ai_chat -> `docs/ai-chat.md`, ios_messages/mac_messages -> `docs/texts.md`,
  ios_calls -> `docs/calls.md`, android_sms -> `docs/android-texts.md`;
  `meta_messages` and `google_activity` keep the underscore):
  - `ai_chat` — Claude/ChatGPT exports; mirrors per vendor + account.
  - `ios_messages` — iPhone backup -> iMessage/SMS/RCS; mirrors.
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
    card's name (what `--suggest` matches); mirrors per account.
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
  - `browser` — Chrome/Edge/Brave/Safari history -> `browser_visit`, one row
    per (URL, day); remembers.
  - `email` — Gmail Takeout mbox (read inside the zip), Apple Mail export or
    `~/Library/Mail`, Thunderbird, .eml -> `email` (own words: quoted reply
    and signature trimmed); email handles sender/recipient; direction from
    [connectors.email] addresses, else [owner], else Sent; remembers.
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
  local model (Ollama over stdlib HTTP, optional, never via a proxy) may only
  PHRASE that evidence — never supply facts; evidence is fenced as data, and
  its citations are checked per sentence (a number or date not in the cited
  evidence -> the deterministic answer replaces it, `model_rejected`).
  Retrieval must stay read-only and model-free.
- `lifecrm/review.py` + `lifecrm/commands/review.py` — the weekly review /
  advisor evidence pack (D/E/M/H/O ids, prior-period deltas, lenses). The
  /review-week and /advise skills write the cited brief; a kept brief is a
  `reflection` entry (data.derived_from = "lifecrm review") through import ->
  ingest -> review -> apply, excluded from later packs. `docs/review.md`.
- `lifecrm/web/` + `lifecrm/commands/web.py` — `lifecrm serve`: stdlib HTTP
  over a backup-API snapshot in data/web/lifecrm.db, re-copied when the live
  db or its -wal changes; one read-only connection per request; `api.py`
  reads, `index.html` is the single offline page (CSP forbids external
  fetches). The only write is recall_log. No capture UI. `docs/web.md`.
- `lifecrm/voice.py` + `lifecrm/commands/voice.py` — the voice loop: a front
  end over recall.answer() and the quick-note inbox, never a write path; not
  logged to recall_log; TTS through a constant OS-engine script with the text
  on stdin, never a shell; optional deps only in requirements-voice.txt.
  `docs/voice.md`.
- `lifecrm/views.py` — read-only view assembly shared by CLI, web and voice.
- `lifecrm/platforms.py` — where things live on Windows vs macOS (cloud
  drives, Messages, backups); proposes paths, config.toml always wins.
- `lifecrm/commands/` — CLI plug-ins (`register(app)`), discovered by
  cli.py: configure, demo, entity_links, filedate, gui (open), nutrients,
  obsidian, review, verify (verify-backup), voice, web (serve), where.
- `lifecrm/demo/` — `lifecrm demo`: persona + journal drafts + one export
  generator per source in `demo/sources/`, run through the real CLI into a
  `.lifecrm-demo`-marked root (never the project's data). `docs/demo.md`.
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
- `docs/windows-android.md` (combo 1: Windows laptop + Samsung) and
  `docs/mac.md` (combo 2: MacBook + iPhone) — the setups end to end;
  `docs/iphone.md` + `docs/android.md` — standalone phone capture guides.
- Deferred (do not build unless asked): ActivityWatch screen time,
  comparison questions ("this month vs last"), Samsung `.qta` recordings.
  Bidirectional/live Obsidian sync is forbidden without an explicit decision
  (a second write path; the read-only mirror exists: export --format
  obsidian, re-rendered one way after each CLI write — nothing is read
  back). Never a capture UI; never live federation of app data.
