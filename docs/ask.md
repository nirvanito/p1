# Asking your own data questions

```bash
.venv\Scripts\python.exe -m lifecrm ask "how did I sleep last week"
```

```
Sleep totalled 7.09 h per day over 5 day(s) (min 6.47 h 2026-07-27,
max 7.75 h 2026-07-26) [M1]. Related entries: 2026-07-27 Gym: push day [E1].

sources:
  [M1] sleep: mean 7.09 h over 5 day(s), sensor import
  [E1] 2026-07-27 entry: Gym: push day

window: last week (2026-07-22 to 2026-07-28) · answer: deterministic
```

Every claim ends in an evidence id you can check. The window it decided to
use is always printed, so a misread question is visible rather than silent.

## How it works, and why it is built this way

Two layers, deliberately separated:

1. **Retrieval is deterministic.** The question is parsed by plain rules into
   a time window and a set of metrics, then answered from SQL over a
   read-only connection. No model is involved in deciding what is true.
2. **A local model is optional, and only phrases the result.** It receives the
   evidence block and is instructed to cite it. Its citations are then checked
   against the real evidence ids — if it cites something that does not exist,
   you get a warning instead of a confident sentence.

This is the opposite of the usual "chat with your database" design, where a
model writes SQL and you hope it got it right. Two things fall out of it:

- **Small local models are good enough.** They never have to write SQL or do
  arithmetic — only sentence-building — so a 7-8B model on CPU is fine.
- **It works with no model at all**, which is the default state. The numbers,
  the sources, and the honest gaps are all produced without one.

Nothing about this costs money or leaves the machine.

## Time windows

The window is printed with every answer. Rolling definitions:

| you say | window |
|---|---|
| today / yesterday | that journal date (05:00 rollover applies) |
| last night | today — sleep is filed under the day you woke up |
| this week | Monday through today |
| last week | the 7 days before today |
| last 10 days / past 3 months | rolling back from today |
| this month / last month | calendar months |
| in July / June 2025 | that calendar month (bare month = most recent one) |
| 2026-07-04 | that day |
| between 2026-07-01 and 2026-07-10 | that range |
| *(nothing)* | last 30 days, and text search is **not** limited to it |

That last row matters: "when did I last see Jake" searches all of history,
because defaulting to 30 days would answer "never" for something that
happened in March.

## What it knows about

**Sensor data** (`health_records`, from Health Connect): sleep, steps, weight,
heart rate, resting heart rate, workouts, distance, calories burned, food
energy, body fat, hydration.

**Journal data** (`measurements`, from what you said in notes): mood, energy,
sleep hours and quality, rating, weight, steps, calories, water, reps, sets,
duration, distance, servings, money.

The two are kept distinct in the evidence — "sleep (sensor import)" and
"sleep (journal)" are different lines with different ids, because when they
disagree, that disagreement is information.

Anything else in the question becomes a full-text search over entry titles,
bodies, and transcripts, plus entity resolution for names.

## When there is no data

```
No data for: heart rate (sensor import) in June 2026.
```

That is a feature. A question the database cannot answer is logged as
unanswered, and the accumulating list is the most honest possible guide to
what to import or start recording next:

```bash
.venv\Scripts\python.exe -m lifecrm ask --history
```

```
2026-07-29 14:02   how did I sleep in July
2026-07-29 13:58 ? how much did I spend on coffee in March
```

The `?` marks questions nothing could answer. (`--no-log` skips recording.)

## Adding a local model (optional)

Prose answers need a local model server. On Windows:

```bash
winget install Ollama.Ollama
```

```bash
ollama pull llama3.1:8b
```

That is all — `ask` detects it automatically. To use a different one, set it
in `config.toml`:

```toml
[ask]
enabled = true
endpoint = "http://localhost:11434"
model = "qwen2.5:7b"
```

`enabled = false` turns the model off permanently; `--no-llm` turns it off for
one question. If the model is unreachable or times out, you silently get the
deterministic answer instead — the command never fails because a model is
having a bad day.

## Using it from other tools

`--json` is the tool interface, and it is what a chat UI or voice loop should
call rather than reimplementing retrieval:

```bash
.venv\Scripts\python.exe -m lifecrm ask "how did I sleep last week" --json
```

It returns the answer, the answer's source (`deterministic` or `model:...`),
the citation check, and the full evidence bundle — every metric with its
per-day series and source files, every entry with its UUID, every transcript
passage with its source id. Wrapping that as an MCP tool or an OpenAPI tool
server for Open WebUI is the natural next step, and needs no changes here.

## Limits worth knowing

- Question parsing is rules, not comprehension. "Was I happier on days I
  worked out?" retrieves mood and workouts for the window but will not
  correlate them for you — the numbers are there, the analysis is not.
- Comparisons between two periods ("this month vs last") are not parsed yet;
  ask twice.
- Retrieval is read-only by construction. `ask` can never modify data, which
  is why it is safe to point a model at.
