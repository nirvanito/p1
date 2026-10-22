# The web UI: `lifecrm serve`

```bash
.venv\Scripts\python.exe -m lifecrm serve            # Windows
.venv/bin/python -m lifecrm serve                    # macOS
```

```
serving a read-only snapshot of ...\data\lifecrm.db — refreshed when the database changes; the live file is never held open
open: http://127.0.0.1:8765/
questions are logged to recall_log (--no-log to stop)
Ctrl+C to stop
```

One page in the browser, on this computer or on your phone: ask a question
and get a cited answer, see a day, a person, your open loops, where the data
came from, and your health numbers. It works offline — no CDN, no fonts, no
analytics; the page, its icons and its charts are all served by the command
itself, and the browser is told (Content-Security-Policy) to fetch nothing
from anywhere else.

It is a **viewer**. There is no capture box and no edit button: logging a day
stays the reviewed `/log` path, and changes stay CLI commands, so the review
gate is never bypassed from a phone.

## Pages

| page | what it shows |
|---|---|
| **Ask** | A question box. The answer is exactly `lifecrm ask --json`'s (`recall.answer()`): every claim carries a citation chip — `M1` a metric series, `E2` a journal entry, `P3` a transcript passage, `O4` an observed record. Tap a chip to expand the evidence: the series as a small chart with mean/min/max, the entry with the transcript passage it came from, the observed row with its stream, time, who, text and the import that brought it in. Under each answer: the time window it used, and *answered deterministically* or the local model that phrased it (its citations are checked). Example questions come from the demo persona on the demo database, generic ones otherwise. |
| **Today** | Pick a day (date picker, prev/next, or tap a square on the five-week heat map). The day's summary, mood / energy / slept, health numbers from sensor imports (the same per-day numbers `ask` and the Health page give — see below), journal entries grouped by category with kind badges, and observed records (texts, calls, chats, ...) grouped by stream — styled as *observed*, each citing its import. |
| **People** | Everyone of type person, most recent contact first — across both tiers (the latest journal mention vs. the latest text, connected call or calendar event naming one of their handles: the rule `ask` uses for "when did I last talk to X", so a missed call, a tapback, a contact card or next week's meeting is never "last contact"). You are not on the list: the person linked to your own number or account (a handle that sends your outgoing texts, that an import names as its account, or that the source calls "you (this phone)") opens as *you*, with no last contact. A person page adds aliases, linked handles, journal mentions, recent observed records, upcoming calendar events apart from them, contact-card fields, and *you → them* / *them → you* — a message or call they sent you (a group text someone else sent does not count) and one you sent them. |
| **Loops** | Open tasks and plans, overdue first, then by due date; the last few closed. Closing one is `lifecrm entry close <uuid> --closure done` (or `dropped` / `obsolete`) — or say it in tonight's note. |
| **Health** | The key sensor metrics (sleep, steps, heart rate, weight, workouts, ...) and the journal's mood / energy / sleep over the last 14 or 30 days. The series are the same ones `ask` cites. When `lifecrm dashboard` is running and answers at the address the page was opened on, a link opens the full Datasette dashboards; on a phone, with the dashboards started the default way (this computer only), the page says so instead — start them with `lifecrm dashboard --host 0.0.0.0` to open them from the phone (no key, so trusted networks only). |

**Two apps, one walk.** Samsung Health and Google Fit can both write the same
walk into Health Connect, and an iPhone and a Watch both count the same
steps. For a summed metric (sleep, steps, workouts, distance, energy) each
day therefore takes the one app with the larger total, never both added up —
in the day view, the Health page and `ask` alike (and in the Obsidian
export and the Health dashboards). A day tile names that app (`origin_app`)
when more than one recorded the day. Averages (weight, heart rate) pool every
app.
| **Sources** | Import runs with counts, the unresolved-handle queue, each connector's newest record and whether it has gone stale, health export files, and the journal's own sources. |

Confirmed rows (entries, measurements, mentions) and observed rows
(imported in bulk, never reviewed) are kept visibly apart on every page —
observed rows are shown as what a source recorded, with the row's own id, not
as fact.

## On your phone

The computer runs the server; the phone is just a browser on the same Wi-Fi.

```bash
python -m lifecrm serve --host 0.0.0.0 --token random
```

```
open: http://127.0.0.1:8765/?k=Xb3...
on your phone (same Wi-Fi): http://192.168.1.23:8765/?k=Xb3...
warning: no login and plain http — anyone on this network can read it; trusted networks only
```

Type the phone line into the phone's browser (or send it to yourself). The
`?k=` key is needed once; a cookie remembers it.

**Windows laptop + Samsung (Android).** The first time, Windows Defender
Firewall asks whether Python may communicate on networks: allow **Private**
networks (your home Wi-Fi must be set to *Private* in Windows network
settings — on a *Public* network the phone cannot connect). On the phone,
open the URL in Samsung Internet or Chrome, then menu → **Add page to → Home
screen** (Samsung Internet) or ⋮ → **Add to Home screen / Install app**
(Chrome). It opens full-screen like an app.

**MacBook + iPhone.** macOS asks whether Python may accept incoming network
connections: click **Allow** (System Settings → Network → Firewall if you
missed it). On the iPhone, open the URL in Safari, then Share → **Add to Home
Screen** while the address still ends in `?k=...` — home-screen apps keep
their own cookies, so the icon has to carry the key itself (the page and its
manifest keep it in the start address for that reason).

Both: the laptop must be awake and `lifecrm serve` running. The URL changes
if the laptop's IP address changes; a DHCP reservation on the router keeps it
stable.

## Voice

- **On the computer itself** (`http://127.0.0.1` or `localhost`), the mic
  button records your question; the server transcribes it with the same
  faster-whisper used for voice notes (`[web] model`, default `small`, loaded
  on the first voice question and kept) and asks it. The recording is deleted
  as soon as it is transcribed — question audio is never kept. If
  faster-whisper or the model is unavailable, the page says so (HTTP 503).
- **On the phone**, browsers only allow the microphone on https or
  localhost, and this is plain http on the LAN — so the page hides the mic
  button and suggests the keyboard's own dictation instead (the mic key on
  Samsung Keyboard / Gboard, or iOS dictation). It works just as well:
  dictate into the box and send.
- **Read answers aloud** reads each answer with the browser's own speech
  synthesis, citations removed. Nothing is sent anywhere for that.

## What it reads, and the one thing it writes

- **The live database is never held open.** At start the database is copied
  through SQLite's backup API to `data\web\lifecrm.db`; every few seconds at
  most, a request checks whether the live file (or its WAL) changed and, if
  so, takes a fresh copy and swaps it in once no request is reading the old
  one. So `lifecrm restore`, `migrate` and `/log` work normally while the
  server runs, and the page catches up a few seconds after an apply.
- **Every request reads the snapshot** through its own read-only connection.
- **The only write** is the question log: each question asked is recorded in
  `recall_log`, the same row `lifecrm ask` writes. Both apply one rule for
  `answered`: a question counts as answered when evidence came back, and a
  question that asked for numbers only when it got some. `--no-log` (or
  `[web] log_questions = false`) turns it off.

`data\web\` is inside `data\`, so it is private and gitignored like the rest.

## Access

- Default: `127.0.0.1` — this computer only.
- `--host 0.0.0.0`: anyone on the same network can open it. There is no login
  and no https; use it on networks you trust, and prefer `--token`.
- `--token random` (or `--token <yours>`, or `[web] token`) makes every
  request carry the key; without it the answer is 401. A key of your own may
  hold any characters: the printed links, the home-screen address and the
  cookie carry it percent-encoded (`a+b` prints as `?k=a%2Bb`). The manifest
  and icons stay public (they hold nothing private) so "Add to Home Screen"
  works.
- Requests from another website (`Sec-Fetch-Site: cross-site`) are refused for
  the API, and so are requests naming a Host that is not this machine (DNS
  rebinding). Reaching it through another name? Add it to `[web]
  allowed_hosts`.

## Settings

All optional, in `config.toml`:

```toml
[web]
host = "127.0.0.1"         # 0.0.0.0 for the phone
port = 8765
token = ""                 # "random" makes one up at each start
log_questions = true       # record questions in recall_log
use_llm = true             # let a running local model phrase answers
model = "small"            # whisper model for voice questions
refresh_seconds = 3        # how often to check the live DB for changes
dashboard_port = 8001      # where `lifecrm dashboard` listens, for the link
allowed_hosts = []         # extra Host names to accept
```

Command options override them: `--host`, `--port`, `--token`, `--no-log`,
`--no-llm`, `--open` (open this computer's browser), `--verbose` (log request
paths — never questions or the key).

## JSON API

Everything the page shows is plain JSON, for scripts or another front end. Errors are JSON too: `{"error": "...", "status": 400}`.

| endpoint | returns |
|---|---|
| `GET /api/ask?q=...[&days=N][&llm=0]` | `lifecrm ask --json`'s shape, plus `index` (id → one-line description), `speech` (the answer without citations), `logged`, `warnings` |
| `GET /api/day?date=YYYY-MM-DD` | summary, mood/energy/sleep, `by_category` entries, `health` (per metric; `origin_app` when two apps recorded the day), `observed` by stream, `prev`/`next` days with data, `sources` |
| `GET /api/days?start=&end=` | per-day counts (entries, observations, health metrics, mood) for a heat map; at most 400 days |
| `GET /api/people` | persons (not you) with aliases, mention and observation counts, `last_contact` across tiers |
| `GET /api/person/<id>` | one person: aliases, handles, mentions, observations (up to today), `upcoming` (dated after today), streams, contact cards, `last_contact`, `last_in`/`last_out`, `is_owner` |
| `GET /api/entry/<uuid>` | the entry with provenance (transcript passage), revisions, links, cited observations |
| `GET /api/observation/<uuid>` | one observed row in full, its import, and the rows around it in the same thread |
| `GET /api/loops` | open tasks/plans (overdue first) and the recently closed |
| `GET /api/imports` | import runs, unresolved handles, connector freshness, health files, journal sources |
| `GET /api/health?days=14[&end=YYYY-MM-DD]` | daily series per metric (the ones `ask` cites) and the journal's own |
| `GET /api/status` | today, counts, demo or not, example questions, snapshot time, model and voice state, `dashboards` (`running` at this page's host name, `local_only` when only this computer can open them) |
| `POST /api/transcribe` | body = recorded audio → `{"text", "language", "duration_sec", "model"}` |

## Troubleshooting

- **The phone cannot connect.** Same Wi-Fi (not a guest network)? Server
  started with `--host 0.0.0.0`? Firewall allowed (Windows: Private network)?
  Try the URL on the laptop itself first.
- **401 "this server needs its key".** Open the printed link with `?k=` once.
- **"cannot listen on ...".** Another program (or another `lifecrm serve`)
  has the port: `--port 8766`.
- **The page shows old data.** It refreshes within a few seconds of a change;
  Sources shows when the snapshot was taken and any refresh problem.
- **Voice says faster-whisper is missing / the model cannot load.** The first
  load downloads the model, so it needs the network once; or set
  `[web] model` to one you already have. Keyboard dictation always works.
