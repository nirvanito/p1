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
   a time window, metrics, the people and places it names, and the kind of
   record it asks about. It is then answered from SQL over a read-only
   connection. No model is involved in deciding what is true.
2. **A local model is optional, and only phrases the result.** It receives the
   evidence block and is instructed to cite it. Its answer is then checked
   (see [Citation rules](#citation-rules)): if it cites something that does
   not exist, or states a number the evidence it cited does not contain, the
   answer built from the evidence alone is shown instead, with a warning.

This is the opposite of the usual "chat with your database" design, where a
model writes SQL and you hope it got it right. Two things fall out of it:

- **Small local models are good enough.** They never have to write SQL or do
  arithmetic — only sentence-building — so a 7-8B model on CPU is fine.
- **It works with no model at all**, which is the default state. The numbers,
  the sources, and the honest gaps are all produced without one.

Nothing about this costs money or leaves the machine.

## The four kinds of evidence

| id | what it is | tier |
|---|---|---|
| `M#` | a statistic: a sensor metric, a journal measurement, or a transaction total | sensor and transactions: observed; journal: confirmed |
| `E#` | a journal entry you reviewed and applied | confirmed |
| `P#` | a passage of your own transcribed words | confirmed |
| `O#` | an **observed** record: a text, call, calendar event, AI chat turn, transaction, contact card, note, photo | observed |

`O#` items come from the bulk importers (`lifecrm observe ...`). Nobody
reviewed them, so the evidence marks every one `(observed, not confirmed)`,
and an answer reports what the record *says* — "Jake texted you ...", "a
$29.75 charge at ..." — never that it happened. Each one carries its full
provenance: the observation uuid, the stream, the connector, the import uuid
(so `lifecrm imports retract <uuid>` can undo it) and the export file.

Who took part is resolved through your handle links (`lifecrm handles link`).
A linked phone number or email shows as the person's name. An unlinked one
shows as `phone:+15125559999`. Your own handle shows as "you". A handle left
on an entity that was later merged still resolves to the survivor.

An observed item in the evidence looks like this:

```
[O3] 2026-10-12 21:00 call (ios_call) you → Linda Rivera 12 min (observed, not confirmed)
```

Streams are grouped so the answer can say what a record is:

| label | streams |
|---|---|
| text | `imessage`, `sms`, `rcs`, `android_sms`, `android_mms`, `whatsapp` |
| call | `ios_call`, `android_call` (value = seconds; 0 = missed) |
| calendar | `calendar_event` (location from the record's details) |
| contact card | `contact_card` (title = the person, body = notes, address, birthday) |
| note | `note` |
| transaction | `txn` (value = amount, negative = money out, unit = currency) |
| photo | `photo` (latitude/longitude from the record's details) |
| place visit / web visit | `place_visit`, `browser_visit` |
| AI chat | `claude_chat`, `chatgpt_chat` |

Reactions and thread events (`text_reaction`, `text_event`) are never search
results, because they carry no words of their own. A text matches on what was
said, not on its group's name. Otherwise a thread called "Bled crew" would
make every message in it a hit for "Bled". An AI chat's title does count,
weighted below the turn itself, because that title is your own topic. A
ChatGPT branch you abandoned by editing a message stays searchable, but ranks
after the answer you kept.

A database from before the observed tier (schema v3) is answered without it:
nothing fails, and the observed lists are simply empty.

## Questions about people

When the question names someone you know, the evidence is gathered across
both tiers. Names are matched by exact name or alias, ignoring case and
accents ("jake", "José", "Dr. Ellis", "Mom"). A first name counts when it
belongs to exactly one person. A name that fits several people is listed as
ambiguous and never used to narrow the search: retrieval does not guess.

- **"When did I last talk to Mom?"** The newest text, the newest call (a
  missed call is an attempt, not contact), the newest calendar event with
  them, and the newest journal entry they took part in. Each one is cited, and
  the latest of them leads:

  ```
  Last contact with Linda Rivera: 2026-10-12, you called Linda Rivera (12 min) [O3].
  Before that: 2026-10-12 journal entry "Called Mom on the drive home" [E1];
  2026-10-12 Linda Rivera texted you: "Tell Maya happy birthday from me" [O2];
  2026-10-08 calendar "Family dinner at Tía Rosa's" with Linda Rivera [O4].
  ```

  "When did I last *call* Jake" and "when did I last *text* Jake" narrow it
  to that kind of contact.
- **"Where is Jake from?", "What's Jake's favorite restaurant?"** Their
  contact card (matched by a linked handle or by their exact name) comes
  first. Then the entries that mention them, with facts before everything
  else.
- **"What did Jake text me about the lake?"** Searches only the records Jake
  took part in. "Jake texted" means messages Jake *sent*; "what did I text
  Jake" means yours.

## Questions about money

**"How much did I spend at Veracruz [this month]?"** sums the `txn`
observations matching the merchant words. With no time named it covers every
record, the same rule as text search, and the span it covered is printed.
With a time named it covers that window only. Money out and money in are kept
apart, so a paycheck never shrinks the total. Each currency gets its own
total. The transactions themselves are cited too:

```
You spent $150.45 across 3 transaction(s) matching "Veracruz" (all records,
2026-09-13 to 2026-10-20) [M1]: 2026-10-20 VERACRUZ ALL NATURAL -$29.75 [O1];
2026-10-01 VERACRUZ ALL NATURAL -$86.20 [O2]; 2026-09-13 VERACRUZ ALL NATURAL
-$34.50 [O3].
```

What the journal says you spent (`money` measurements, including ones that
belong to an entry like "dinner, $40") is a separate, confirmed line.

## Open loops

**"What are my open tasks?", "What's on my list?", "What do I have to do?"**
list every task and plan that is not closed yet (`closed_at` is empty). The
soonest due come first. Each one is cited as an entry. Name a person ("open
tasks with Mom") to list only the ones that mention them.

## Time windows

The window is printed with every answer. Rolling definitions:

| you say | window |
|---|---|
| today / this morning / tonight / yesterday | that journal date (05:00 rollover applies) |
| last night | today — sleep is filed under the day you woke up |
| this week | Monday through today |
| last week | the 7 days before today |
| this weekend / last weekend | that Saturday and Sunday |
| last 10 days / past three months | rolling back from today |
| three days ago / a week ago | that day / that Monday-to-Sunday week |
| this month / last month | calendar months |
| this year / last year / in 2025 | calendar years |
| in July / June 2025 / July last year | that calendar month (a bare month means the most recent one) |
| March 3 / 2026-07-04 / last Monday | that day |
| between 2026-07-01 and 2026-07-10 / between June and August | that range |
| since 2026-07-01 / since July | from then through today |
| *(nothing)* | last 30 days, and text search is **not** limited to it |

That last row matters: "when did I last see Jake" searches all of history,
because defaulting to 30 days would answer "never" for something that
happened in March.

Words that only look like dates are not dates. "What *may* have happened" and
"did I *march*" have no month in them. A month name counts only next to
something that makes it one: "in May", "March 3", "May 2025". Someone named
June is never read as the month, but "in June" still is. Something that is
not a real date ("2026-13", "Feb 30", "the last 0 days") is ignored and the
answer says so. It never causes an error. A very long span like "last 100000
years" is capped at 100 years.

## What it knows about

**Sensor data** (`health_records`, from Health Connect): sleep, steps, weight,
heart rate, resting heart rate, workouts, distance, calories burned, food
energy, body fat, hydration. When two apps record the same thing on the same
day (Samsung Health and Google Fit can both count one walk), a daily total
takes one app's figure, the larger, and never adds them together. The
evidence says on how many days this happened. Averages pool across apps.

**Journal data** (`measurements`, from what you said in notes): mood, energy,
sleep hours and quality, rating, weight, steps, calories, water, reps, sets,
duration, distance, servings, money. Some measurements belong to a day ("mood
7") and some to an entry ("ran 5 km", "dinner, $40"). Both count, and an
entry-owned one is dated by its entry. A measurement whose entry was
retracted goes with it.

The two are kept distinct in the evidence — "sleep (sensor import)" and
"sleep (journal)" are different lines with different ids, because when they
disagree, that disagreement is information.

**Observed records**, as above.

Anything else in the question becomes a full-text search over entry titles,
bodies, transcripts and the observed tier, plus entity resolution for names.
Words that say *how* something was communicated ("text", "said", "asked")
are dropped from the search, so "what did I ask Claude about Bled" searches
for Bled, in Claude chats, in turns you wrote.

## Citation rules

These apply to every answer, the model's and the deterministic one alike.
The deterministic answer must pass them too, and the tests hold it to that.

1. **Every id must exist.** Anything bracketed that looks like an id counts:
   `[M1]`, `[m1]`, `[M1, E2]`, and invented ones like `[X2]` or `[O9]`. An id
   that is not in the evidence is listed under `unknown`.
2. **Every sentence that states a number or a date must cite the evidence
   it came from, and that evidence must contain it.** One valid citation
   elsewhere in the answer does not vouch for the rest. Rounding is allowed:
   7.19 h can be said as 7.2 or 7, but not as 9. The window line and the
   no-data lines are shared, so any sentence may use those dates. A summary
   line such as "last contact O3 (2026-10-12)" counts only for a sentence
   that cites O3. A sentence that fails is listed under `unsupported`.
3. **An answer that cites nothing** is flagged `uncited`.

When a model's answer breaks rule 1 or 2, it is set aside. You get the
deterministic answer, a warning on stderr, and in `--json`, `model_rejected`
holds what the model said and why it was rejected. The `sources:` list only
ever shows what the printed answer actually cited.

The evidence handed to a model is also defended against what is quoted in
it. Once your texts are evidence, other people's words are in the prompt,
and any of them could be written as an instruction:

- Evidence goes between `BEGIN EVIDENCE` and `END EVIDENCE` lines. The system
  prompt says that everything between them is data, not instructions. A fence
  line that appears inside the evidence is defused, so quoted text cannot
  close the block early.
- Square brackets inside quoted text become parentheses, so a message
  containing "[M1]" can never look like a citation. Excerpts are flattened to
  one line and capped.

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

The `?` marks questions nothing could answer. A question that asked for
numbers counts as answered only when it got some, not just loosely matching
text. `--no-log` skips recording. The question is logged after the answer is
printed. If the database is locked or its schema is behind the code, you
still get the answer, with a warning that it was not logged.

## Adding a local model (optional)

Prose answers need a local model server. On Windows:

```bash
winget install Ollama.Ollama
```

On a Mac, `brew install ollama` or the app from ollama.com. Then:

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
num_ctx = 8192          # context window sent to the model
timeout_sec = 120
max_entries = 8         # evidence limits per question
max_passages = 4
max_observations = 6
max_transactions = 10
max_loops = 10
```

`enabled = false` turns the model off permanently. `--no-llm` turns it off for
one question. `--model NAME` uses another pulled model for one question. If
the model is unreachable or times out, you get the deterministic answer
instead. The command never fails because a model is having a bad day. A typo
in a number above falls back to its default.

`num_ctx` matters. Ollama's own default context is small, and when a prompt
overflows it, Ollama silently drops the oldest text, which is the rules and
the evidence.

**Keeping it local.** Requests to the model never go through a proxy. A
system or `HTTP_PROXY` proxy would otherwise carry the evidence, your texts
included, off the machine. An `endpoint` that is not this machine (anything
but `localhost`, `127.x.x.x` or `::1`; a LAN address counts as not this
machine) works, but `ask` warns about it every time, because the evidence is
sent there with every question.

## Using it from other tools

`--json` is the tool interface, and it is what a chat UI or voice loop should
call rather than reimplementing retrieval (in Python, `recall.answer()`
returns the same dictionary):

```bash
.venv\Scripts\python.exe -m lifecrm ask "how did I sleep last week" --json
```

It returns:

- `question`, `answer`, and `answer_source` (`deterministic` or `model:...`).
- `citations`: `cited`, `unknown`, `uncited` and `unsupported`.
- `model`: whether a model was available and why not. `model_rejected` is set
  when a model's answer was set aside.
- `evidence`: the full bundle.
  - `metrics`: each with its per-day series and source files.
  - `entries`: each with its UUID and `tier`.
  - `passages`: each with its source id and UUID.
  - `observations`: each with uuid, stream, participants, connector, import
    uuid, source file, value and unit.
  - `entities`: each with `last_contact`, `cards` and `facts` (all as ids),
    plus `ambiguous`, `no_data`, `loops` and the parsed `intent`.
- `evidence_text`: the evidence as the model saw it.

Wrapping that as an MCP tool or an OpenAPI tool server for Open WebUI is the
natural next step, and needs no changes here.

## Limits worth knowing

- Question parsing is rules, not comprehension. "Was I happier on days I
  worked out?" retrieves mood and workouts for the window but will not
  correlate them for you — the numbers are there, the analysis is not.
- Comparisons between two periods ("this month vs last") are not parsed yet;
  ask twice.
- Observed records are what the export said, not what happened. Texts are
  other people's words, and a transaction's merchant name is the bank's.
  Answers attribute them for that reason.
- People are found through handles you linked. A person whose number you
  never linked (`lifecrm handles list --suggest`) is found only by name, in
  your journal and on their contact card.
- Retrieval is read-only by construction. `ask` can never modify data, which
  is why it is safe to point a model at.
