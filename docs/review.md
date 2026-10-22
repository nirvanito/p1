# The weekly review and the advisors

```bash
.venv\Scripts\python.exe -m lifecrm review                 # last full week, Mon-Sun
.venv\Scripts\python.exe -m lifecrm review --lens health --days 28
```

```
review pack: week 2026-W42 (2026-10-12 to 2026-10-18), lens week
  14 entries on 4 of 7 day(s), 63 health records, 212 observations; loops: 2 opened, 1 closed, 5 open, 1 overdue
wrote data\staging\review-week-2026-W42.md
  and data\staging\review-week-2026-W42.json
turn it into a brief with /review-week (or /advise <lens>) in Claude Code
```

`lifecrm review` builds an **evidence pack** for a period: everything the
database can say about it, each item carrying a citable id. The
`/review-week` and `/advise` skills turn the pack into a short brief. The
split is the one `ask` uses, for the same reason:

1. **The pack is deterministic.** Plain SQL over a read-only connection, no
   model. The same database and the same flags give the same pack, byte for
   byte — so a brief's citations can be re-checked at any time.
2. **The brief is interpretation, and stays labeled as such.** It is written
   by Claude in an interactive session, cites the pack for every claim, is
   regenerable from the pack, and is stored only if you ask — as a
   `reflection` entry through the normal review gate, never as a fact.

Nothing here runs a metered model, and nothing here writes the database.

## The period

| flags | period | prior period |
|---|---|---|
| *(none)* | the last **full** week, Monday-Sunday, in `[time] timezone` with the rollover hour | the week before |
| `--week 2026-W42` | that ISO week | the week before |
| `--start 2026-10-01 --end 2026-10-14` | that range (`--end` defaults to today) | the 14 days before |
| `--days 28` | the 28 days ending today | the 28 days before |

On a Wednesday the default is the week that ended three days ago, never the
half-week in progress. Every comparison in the pack is against a prior
period of the same length.

## What the pack contains

Ids are pack-local, one per thing, numbered in reading order:

| id | points at | tier |
|---|---|---|
| `D#` | a journal day: summary, mood, energy and sleep as said, entry count | confirmed |
| `E#` | an entry (uuid, date, kind, categories, people, amounts, loop state) | confirmed |
| `M#` | a journal metric over the period (mood, energy, sleep hours, sleep quality) with the prior period's mean and the change | confirmed |
| `H#` | a sensor/app health metric over the period, likewise, naming the export files | observed |
| `O#` | one imported observation (uuid, import uuid), or a count over observations: a stream, a person's messages/calls/meetings, a merchant | observed |

`index` maps every id to its details; `review --check` and the sources
footer read it. Sections, by name:

- **days** — each day with a summary, day measurements or entries.
- **entries** — counts by primary category and by kind, then every entry
  (up to 200; the counts are always complete).
- **loops** — tasks and plans: opened in the period, closed in it (closed
  date in local time), still open at its end, and overdue. A task due on the
  period's last day is overdue once that day has passed.
- **journal_metrics** — mood, energy, sleep as said, each vs the prior
  period. Metrics with no data are listed under `no_data`.
- **health** — sleep (mean hours, nights under 6.5 h), steps (daily mean),
  workouts (sessions and minutes), weight (mean, and change first to last),
  resting heart rate — each vs the prior period, from `health_records`.
  A metric with no data names its last record date, so "no sleep data" reads
  as a stale import rather than a sleepless week.
- **observations** — imported records per stream (vs the prior period) and
  the most-contacted people.
- **titles** — AI chat conversations (grouped, with turn counts) and note
  titles of the period.
- **relationships** — per person: the entries that name them (and how many
  as a participant — "saw"), and the messages, calls and meetings with them;
  then who has not been in contact for `--quiet-weeks` (default 3) weeks
  before the period end, each with a cited last contact.
- **career** — work entries, decisions, and calendar meetings: total, timed
  hours (all-day blocks excluded), and by attendee.
- **finance** — spent and received per currency vs the prior period, then
  merchants ranked by spend **under the bank's own description** (no
  categorization), plus amounts said in the journal.
- **briefs** — briefs already saved for this period (see below). Listed, not
  counted.
- **coverage**, **notes**, **empty** — how complete the period is ("entries
  on 2 of 7 days"), what is missing and why, and whether anything was
  recorded at all. An empty week is a valid pack, not an error.

### People

A contact is counted from the other side's role: an incoming message counts
its sender (a group message from Jake is contact with Jake, not with
everyone else in the thread), an outgoing one its recipients, a meeting
everyone but the location. You are recognized the way connectors mark the
owner — the sender of outgoing records, and the account an import names —
and never appear as your own contact. A handle linked to an entity
(`handles link`) counts for that person, following entity merges. An
unresolved handle is shown under the name its source gave it, marked
unresolved — never guessed onto an entity.

## Lenses

| lens | sections | entries and loops kept |
|---|---|---|
| `week` (default) | days, entries, loops, journal_metrics, health, observations, titles | all |
| `health` | days, entries, loops, journal_metrics, health | health, exercise, sleep, food |
| `career` | entries, loops, career, titles | work, learning |
| `relationships` | entries, loops, relationships, observations | social, or naming a person |
| `finance` | entries, loops, finance | purchase, or carrying an amount |
| `all` | every section | all |

A section a lens does not include is absent from the pack.

## Output

- default — writes `data\staging\review-<lens>-<period>.md` (for reading)
  and `.json` (the exact pack), prints a summary and both paths.
- `--write-only` — the same files; prints only the two paths. For the
  scheduler.
- `--json` — prints the pack; writes nothing. What the skills read.
- `--check BRIEF` — verifies a brief's citations against the pack built
  with the same flags: exit 1 for an id the pack does not hold, or for a
  brief that cites nothing. `[E1]`, `[E1, H2]` and `[E1][H2]` all count.
- `--check BRIEF --out FILE` — also writes the brief plus a **sources
  footer** that spells every cited id out as uuids, dates and source files,
  so the saved text stays checkable after the pack is gone.

`<period>` is the ISO week (`2026-W42`) or `<start>_<end>`.

## The skills

**`/review-week`** builds the pack, writes a 150-300 word brief (what
happened, wins, open loops, patterns worth attention), checks its citations,
and shows it. **`/advise <lens>`** does the same through one lens in an
advisor's voice: suggestions are questions or small experiments with a way
to tell whether they worked — never diagnoses, never medical, legal, tax or
investment advice.

Rules both follow: every factual sentence cites pack ids; numbers are copied
from the pack, never recomputed; nothing outside the pack; co-occurrence is
never stated as cause; observed items are phrased as what the data shows.

### Keeping a brief

Only when you say so. Then the brief goes through exactly the path a voice
note does:

1. `review --check brief.md --out brief-sourced.txt` — the brief plus its
   sources footer.
2. `import brief-sourced.txt --type manual --date <period end>` — a source,
   immutable like every other.
3. A one-entry draft: `kind: reflection`, `categories.primary: reflection`,
   provenance covering the source, and
   `data: {"derived_from": "lifecrm review", "lens": ..., "period": ...}`.
   No day block, entities, measurements or closures — a brief records an
   interpretation, not new facts.
4. `ingest` -> you review the one-line diff -> `apply`.

The `derived_from` mark is what keeps a saved brief out of later packs'
evidence: it is listed under `briefs`, excluded from entries, counts and
coverage, so an interpretation is never re-read as a fact. A brief you no
longer want goes the usual way: `entry retract <uuid>`.

## Scheduling

The pack is cheap and read-only, so it can be waiting before you sit down
for the review. `scripts/install-tasks.ps1` (task "LifeCRM review") and
`scripts/install-tasks.sh` (`local.lifecrm.review`) register a weekly job,
Sundays at 19:00. It runs `scripts/review.ps1` / `review.sh`, which logs to
`data/review.log`:

```bash
python -m lifecrm review --lens all --write-only
```

That leaves `data\staging\review-all-<week>.md` (and `.json`) for the
default period, the last full Monday-to-Sunday week. The job never writes the database and never runs a model; turning
the pack into a brief stays an interactive `/review-week` session.

## Settings

```toml
[review]
quiet_weeks = 3      # relationships: "not in contact for N weeks"
```

`--quiet-weeks N` overrides it for one run.
