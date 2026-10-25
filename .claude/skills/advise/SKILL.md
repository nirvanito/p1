---
name: advise
description: An advisor's brief through one lens — health, career, relationships or finance — written from the deterministic review pack in an advisor's voice, every claim cited to a pack id, suggestions framed as questions or small experiments (never diagnoses, never medical, legal, tax or investment advice). Optionally saved as a reflection entry through the normal review gate. Use when the user asks for advice or a check-in on their health, work, relationships or money, or runs /advise <lens>.
---

# /advise — one lens, one advisor

`/review-week` generalized: the same evidence pack, narrowed to one part of
life, read by an advisor instead of a recap. The pack is deterministic and
model-free; the advice is yours, derived from it and cited to it. Advice is
never stored unless the user asks, and then only as a `reflection` entry
through the review gate. Contract: `docs/review.md`.

Run everything from the project root with the venv python and
`PYTHONUTF8=1` — Windows: `.venv\Scripts\python.exe -m lifecrm ...`,
macOS: `.venv/bin/python -m lifecrm ...`.

## Step 1 — pick the lens and period

`$ARGUMENTS` names the lens: `health`, `career`, `relationships` or
`finance` (ask if it names none). The default period is the last full week;
an advisor often sees more in a month — offer `--days 28` when a week is
thin. Use the same lens and period flags in every later command.

    python -m lifecrm review --lens <lens> --json   (plus any period flags)

`empty: true` -> nothing recorded for the period; say so, show `notes`, stop.

## Step 2 — read it as that advisor

The pack's ids, tiers and caveats are explained in `/review-week` Step 2:
`D/E/M/H/O` ids with details in `index`, `confirmed` vs `observed` tier,
`coverage` and `notes`, unresolved handles never guessed. What each lens
carries:

- **health** — `health.metrics` (sleep with nights under 6.5 h, steps,
  workout sessions, weight change, resting heart rate, each against the
  prior period), `journal_metrics` (mood, energy, sleep as said), the days,
  and health/exercise/sleep/food entries and loops.
- **career** — `career.work_entries`, `career.decisions`, calendar
  `meetings` by attendee and hours, work loops (overdue first), AI-chat and
  note titles.
- **relationships** — `relationships.people` (who you saw: entries; who you
  texted, called, met: observations), `relationships.quiet` (people not in
  contact for `quiet_weeks`+ weeks, each with a cited last contact), and
  stream counts.
- **finance** — `finance.totals` (spent and received per currency, against
  the prior period), `merchants` ranked by spend under the bank's own text,
  and amounts said in the journal. No categories exist; do not invent them.

## Step 3 — write the brief

Roughly 150-300 words, advisor's voice, second person:

- **What I see** — two to four observations, each cited.
- **What I'd look at** — one or two things worth attention, cited.
- **Try this week** — one to three suggestions, each a question or a small,
  reversible experiment with a way to tell if it worked ("for the next 7
  nights, lights out by 23:00 — next week's pack will show whether the short
  nights [H1] drop"). If the user takes one up, it can be measured: a plan
  carrying `data.experiment` (`{"change": "lights out by 23:00", "outcome":
  "sleep", "days": 14}`, docs/features.md), proposed through `/log` like any
  other plan — later packs' `experiments` section then compares the days of
  the change with the days before, co-occurrence and never cause.

The `/review-week` rules hold: every factual sentence cites its ids, numbers
are copied exactly from the pack, nothing outside the pack, co-occurrence is
never stated as cause. And for advisors specifically:

- **Health**: no diagnoses, no medication or supplement advice. If something
  looks medically worth raising (a sustained resting-heart-rate rise, very
  short sleep), say plainly that it is worth mentioning to a doctor — once,
  without alarm.
- **Finance**: no investment, tax or legal advice; questions about the
  user's own spending only ("three Veracruz visits came to 150.45 [O7] —
  is that the eating-out budget you want?").
- **Relationships**: suggestions are invitations, not obligations. Never
  speculate about why someone went quiet. An unresolved handle can be
  pointed out with `python -m lifecrm handles list --suggest`; linking it
  stays the user's decision.
- **Career**: about priorities, follow-ups owed and meeting load — not
  about other people's performance.

Write it to a temporary file outside `data\`, then check it:

    python -m lifecrm review --lens <lens> --check <brief file>   (same period flags)

Fix until it passes, then show the user.

## Step 4 — keep it? (only if the user says yes)

Follow `/review-week` Step 5 exactly, with `--lens <lens>` on the
`--check ... --out` command, the draft at
`data\staging\draft-review-<lens>-<period slug>.json`, the title
`"<Lens> advisor brief <period label>"`, and
`"data": {"derived_from": "lifecrm review", "lens": "<lens>", "period": "<period slug>"}`.
Ingest, show the diff (one `add`), and **never apply without the user's
confirmation**.
