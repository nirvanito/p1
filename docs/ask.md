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

window: last week (2026-07-22 to 2026-07-28) · answer: deterministic · no local model at http://localhost:11434 — evidence answers only (see docs/ask.md to add one)
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
[O3] 2026-10-12 21:00 call (ios_call) you → Linda Rivera "phone call" 12 min (observed, not confirmed)
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
  to that kind of contact. A journal entry then counts only when it says it
  was one ("Called Mom on the drive home"); "Tacos at Veracruz with Jake" is
  not when Jake last called. If only Jake ever called, "when did I last call
  Jake" still finds that call, and says who called whom.

  A text counts as contact with someone only when it passed between you and
  them: one they sent you, or one you sent them (a group message you sent
  counts for everyone in the group). A group message Jake sent, which Maya
  merely received, is not contact with Maya. "When did Maya last text me"
  and "when was the last time Maya texted me" keep only what Maya sent.

  A question about a call or a text that asks *when* or *whether* is
  answered the same way, with or without "last": **"When did Jake call?"**
  leads with the call, and **"Did Jake call me this week?"** says "Nothing
  recorded shows a call with Jake Morales for this week." when there was
  none, never with his contact card. So does "what did Jake text me over the
  weekend" when he texted nothing then. "Ring", "rang", "FaceTime" and
  "phone" used as a verb ("phone me", "did I phone Jake", "on the phone")
  are calls. "Phone number" and "Jake's phone" are still about his card.
- **"Where is Jake from?", "What's Jake's favorite restaurant?"** Their
  contact card (matched by a linked handle or by their exact name) comes
  first. Then the entries that mention them, with facts before everything
  else.
- **"What's Maya's phone number?", "What's Jake's email?"** The card's phone
  numbers and email addresses are part of its evidence line, as written on
  the card (the handles themselves when the export kept nothing else), so
  the answer can quote them and a model's answer that does is not set aside.
- **"What did Jake text me about the lake?"** Searches only the records Jake
  took part in. "Jake texted" means messages Jake *sent*; "what did I text
  Jake" means yours.

### Who you are

You take part in every text, call and meeting, so a record has to know which
participant is you. You are recognised by:

- the handles listed under `[owner]` in `config.toml`;
- any handle that sent an outgoing record (a text you sent);
- any import's own account (the number an SMS backup belongs to, the
  address an AI-chat export was made under);
- and every confirmed handle of the entity any of those is linked to, or of
  one whose handle the source itself labels "you (this phone)".

The rule lives in one place, `lifecrm/owner.py`, and `ask`, the web UI's
People page and the weekly review pack all read it. Your handles show as
"you", and you are never listed as one of your own contacts.

Texts and AI-chat exports usually say who you are on their own. Some imports
never do: an iPhone calendar export's account is `calendar:Home`, not your
address, and its events carry no direction, so with only that calendar your
own address read as a third party ("with Alex Rivera, Dr. Rebecca Ellis"),
even after you linked it to yourself. Say it once in `config.toml`:

```toml
[owner]
name = "Alex Rivera"           # optional; who you are is decided by handles only
handles = ["email:alex.rivera@example.com", "phone:+15125550100"]
```

Each handle is `kind:value`, with the kinds `handles list` shows (`phone`,
`email`, `calendar`, `app_account`, `card`, `place_id`). Emails are matched
without case and phone numbers are normalised the way the importers store
them, so `phone:(512) 555-0100` is `+15125550100`. A listed handle counts
even before you link it to an entity. A table in the wrong shape (a handle
without a kind, an unknown kind, a key other than `handles` and `name`) is a
one-line error naming the fix, never a setting silently ignored. Leaving the
table out is fine.

You are told when it is needed. `observe calendar` prints the exact
`[owner]` lines to add when its guess at your address is not yet covered
(docs/calendar.md), and `lifecrm doctor` warns `owner_unknown` while people
appear in your imports but no handle anywhere is known to be yours.

## Questions about places

A place you linked a handle to (`handles link` on a Timeline `place_id`, or
on a calendar location such as `place_id:calendar:uchi, ...`) has visits:
every place visit and every calendar event held there. **"When was I last at
Zilker Park?"** takes the newest of those and the newest journal entry
naming the place, and the latest leads:

```
Last visit to Zilker Park on record: 2026-10-18, place visit at Zilker Park
(30.2669, -97.7729) [O1]. Before that: 2026-09-25 journal entry "5k run at
Zilker Park" [E1].
```

Only those two streams count. A restaurant's phone number linked to the
restaurant does not make a call from it a visit. A question that asks for
another kind of record ("what did I ask Claude about Bled") is not given the
place's visits.

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

"Across N transaction(s)" counts money out only. Money in is stated with its
own count ("and received $8,400.00 in 2 transaction(s)").

A place or organisation you know is the merchant when you name it, under any
of its names: "at HEB" and "at H-E-B" both find "H-E-B #742 AUSTIN TX",
because H-E-B Mueller has both as aliases. A hyphenated or ampersanded word
stays one word, so "H-E-B" is searched as the phrase h e b and "AT&T" as the
phrase at t, the way the bank's text was indexed. In a money question a
two-letter word stays a search term ("at BP"), and so do the words after
"at" or "from" ("spend at 76", "what did I buy from H&M", "at Best Buy"),
up to a word that ends the name ("at Uchi in October"), even when they are
a number or a stopword. The merchant can be named without "at" too: the
words right after "pay", "paid" or "charge" ("how much did I pay 76"), a
number after "on" ("spend on 76", "on 7-11"), and the words right before
"spending", "purchases" or "charges" ("76 spending", "Whole Foods
purchases"). A number in a money question is the merchant ("76", "7-11")
unless something else reads it: a window ("the 20th", "2025", "last 3
days"), an amount ("$50", "20 dollars", "more than 100", "3.50", "10%") or a
count ("3 items"). A stream's name is a merchant here as well: "how much did
I spend on ChatGPT" finds "OPENAI *CHATGPT SUBSCR". When nothing else says
what was bought, a person you name is the payee and searched the same way
("how much did I pay Jake" finds "ZELLE TO JAKE MORALES", and journal
spending that mentions Jake); beside a merchant ("Jake and I at Uchi") a
person is company, not a filter. You are never the payee.

A question that names a merchant is never answered with everything you
spent: when nothing matches, the answer says there is no data for that
merchant. That holds whatever the question names, by one rule rather than
one per phrasing: every word of a money question is accounted for — the
question's frame ("how much did I spend"), the time, an amount, a name the
evidence uses, or a term searched for. A word left over ("how much did I
spend on X") was something the question named that nothing could use, and
the answer is `No data for: transactions matching "X"` — never the
window's total. The words are listed under `intent.unused` in `--json`.

## Open loops

**"What are my open tasks?", "What's on my list?", "What do I have to do?",
"What tasks are open?", "Any tasks this week?", "What plans do I have this
week?"** list every task and plan
that is not closed yet (`closed_at` is empty). The soonest due come first.
Each one is cited as an entry. Name a person ("open tasks with Mom") to list
only the ones that mention them.

Name a time ("what do I need to do this week", "my plans this weekend") and
you get what is still open then: a loop due by the end of that time (overdue
ones included), or one with no due date that already existed by then. A task
written down before the window began still counts. "This week" and "this
month" run to the end of the week or month here, and what the calendar holds
in that time is listed too.

**"What tasks are due this week?", "What's due this week?", "What's due by
Saturday?"** ask for due dates: only open loops that have one, by the end of
that time (overdue ones included), soonest first — "Open tasks and plans due
by 2026-10-25: ...". A loop with no due date is left out, and "due" is never
a word to search for. With no time named ("what tasks are due") every open
loop with a due date is listed. A bare weekday in a due question is the
coming one, today included: "what's due by Saturday" asked on a Thursday is
this Saturday, never last.

**"What's overdue?", "Anything past due?", "Which tasks are overdue this
week?"** list only loops due **before today** — the rule the review pack and
the web UI use — "Overdue tasks and plans: ...". A task due later this week
is not overdue.

When the person or the time leaves nothing, the answer says so and gives the
real total: "No open tasks or plans mention Priya Natarajan (4 open in
total)", "No open tasks or plans are due for this week (4 open in total)",
"No open tasks or plans are overdue (4 open in total)." It says "You have no
open tasks or plans" only when nothing is open.

## A day, a weekend, a week

**"What did I do yesterday?", "What happened on Tuesday?", "Give me a recap
of last week"** name a time and nothing else: no topic, person, metric, kind
of record, task or money. The answer lists that time's journal entries, with
up to `max_observations` of its calendar events, place visits, photos, calls
and transactions, all cited.

Words that only mean "what my journal holds" are not a topic: "what did I
*log* yesterday", "what's in my *journal* for yesterday", "my *entries* for
Monday", "what did I *record* / *write*", "my *diary*" are the same recap.
So is any way of naming the day: "the day before yesterday", "on 10/20",
"the 20th", "20 October", "over the weekend", "yesterday morning".

## The calendar, looking ahead

"Calendar", "meeting(s)", "appointment(s)", "invite(s)", "schedule(d)",
"event(s)", "agenda", "upcoming" and "coming up" ask for calendar events.
With "this week", "this month" or "this year" the window then runs to the
period's end, so **"what's on my schedule this week"** and **"what's coming
up this week"** include Friday's and Saturday's events, soonest first. **"What's on my calendar for the rest of the week"** is today through
Sunday. **"What's upcoming?", "What's next on my calendar?", "When is my
next appointment?"** name no time but look forward: from today as far ahead
as the calendar import expands repeating events (`[connectors.calendar]
horizon_days`, 90 days by default), soonest first.

## Time windows

The window is printed with every answer. Rolling definitions:

| you say | window |
|---|---|
| today / this morning / tonight / yesterday | that journal date (05:00 rollover applies) |
| the day before yesterday / the day after tomorrow | that day |
| last night | today — sleep is filed under the day you woke up |
| this week | Monday through today; through Sunday for the calendar and open loops |
| the rest of the week / month / year | today through the period's last day |
| last week | the 7 days before today |
| the coming week | today and the 6 days after it |
| past week / past fortnight | the 7 / 14 days up to and including today |
| past month / past year / past 12 months / last 3 months | rolling back by calendar months from today: "past 12 months" is "past year", both starting the day after today's date a year ago |
| last 10 days / past six weeks | rolling back from today |
| this weekend / last weekend / this past weekend | that Saturday and Sunday |
| the weekend / over the weekend | the one you are in on a Saturday or Sunday; otherwise the last one, or the coming one when the question looks forward ("what's on my calendar for the weekend", "what are my plans") |
| the coming weekend / the weekend before last / the weekend after next | that Saturday and Sunday |
| the week before last | the 7 days before "last week" (8 to 14 days ago) |
| the week after next | the Monday-to-Sunday week after next week |
| the night before last | yesterday (last night is today) |
| the month / year before last, the month / year after next | that calendar month / year |
| the week of October 5 / the weekend of October 10 | that Monday-to-Sunday week / that week's Saturday and Sunday |
| the other day | the past week, not today |
| so far this week / month / quarter / year, this month so far, month to date, YTD | the period's first day through today |
| three days ago / a week ago / a fortnight ago | that day / that Monday-to-Sunday week |
| the past few days / the last couple of weeks / the last several months | rolling back from today: **a couple is 2, a few 3, several 7** |
| a few days ago / a couple of weeks ago | from twice that many units back to one unit back (a few days ago: 6 days ago to yesterday) |
| the next two weeks / the next few days / over the next 3 days | today and what follows (the next two weeks: today and the 13 days after it) |
| this month / last month / previous month | calendar months (this month runs to its end for the calendar and open loops) |
| this year / last year / in 2025 | calendar years |
| this quarter / last quarter / Q3 / Q1 2025 / the third quarter | calendar quarters (Q1 is January-March); this quarter runs to today (to its end for the calendar and open loops), a bare "Q3" is the last one begun; "the past quarter" is the last 3 months |
| in the fall / last summer / this winter / spring 2025 | meteorological seasons, northern hemisphere: spring March-May, summer June-August, fall (autumn) September-November, winter December-February (winter 2025 runs into 2026). "Last summer" is the latest one that is over. A season needs a word that makes it a time — "summer camp" and "did I fall" have none |
| in July / June 2025 / July last year | that calendar month (a bare month means the most recent one — or the nearest one when the question looks forward: "what's on my calendar for November" asked in October is next month, "for September" last month) |
| the first / second / third / fourth / last week of September | days 1-7, 8-14, 15-21, 22-28, or the month's last 7 days |
| the first / second half of September | days 1-15, or 16 to the month's end |
| the first few days / last 5 days of October | that many days at the month's start or end |
| the start / middle / end of October (or of last month); towards / near the end of it | days 1-10, 11-20, or 21 to the month's end |
| the first half / the end of the year (or of 2025) | January-June or July-December; the start, middle and end of a year are January-March, May-August and October-December |
| March 3 / 20 October / the 20th of October / 2026-07-04 / last Monday | that day |
| Saturday / on Friday | the most recent one, today included — or the coming one when the question looks forward ("what's due by Saturday", "what's on my calendar Friday") |
| 10/20 / 10/20/2026 / 10/20/26 | that day — numeric dates are **month first** (US order). Without a year it is the most recent one, and it needs a word that makes it a time ("on 10/20", "since 9/1"): "1/2 cup" is not January 2 |
| the 20th | the most recent 20th of a month, up to today |
| between 2026-07-01 and 2026-07-10 / between June and August | that range |
| September and October / September or October / September, October / from September to October / Oct 1 - 15 | one range across both |
| since 2026-07-01 / since July | from then through today |
| what's upcoming / what's next on my calendar | from today through the calendar's horizon (90 days by default) |
| *(nothing)* | last 30 days, and text search is **not** limited to it |

That last row matters: "when did I last see Jake" searches all of history,
because defaulting to 30 days would answer "never" for something that
happened in March.

The words a window was read from are the window, not the topic. "How much did
I spend in September" sums every transaction in September. It does not look
for ones whose text says "September". The same goes for "weekend", "next",
"since", a weekday, the "two" of "past two weeks", and a word such as
"during" or "early" in front of them. Only the words the window used are
taken out, so "may" that is not the month, or a person called June, is
still searched for.

Time vocabulary is never a search term even when no window was read from it:
month and weekday names, "weekend", "fortnight", "next", "upcoming",
"coming", "earlier", "later", "morning", "afternoon", "evening", "night",
"rest", "start", "end", "beginning", "far", "few", "couple", "several",
"half", "second", "before", "after", "date", "quarter", "Q1"-"Q4", "YTD",
the seasons, "other", and a number counting time units (the "two" of "the
past two weekends", the "3" of "3 nights"). "What did I do yesterday
morning" is yesterday, not entries that say "morning", and a phrasing with
no rule of its own still never searches for its words: at worst the window
is the default one, and the answer prints which. The exception is a name: a
place you know as "Sunday Market" is still Sunday Market, and its "Sunday"
is not read as a day; a person called Summer is still Summer. ("May" and
"March" stay words unless the window read them, for the reason above.)

A window that reaches past today ("next week", or the rest of this week for a
calendar question) lists what is coming first, soonest first, then what has
already passed.

Words that only look like dates are not dates. "What *may* have happened" and
"did I *march*" have no month in them. A month name counts only next to
something that makes it one: "in May", "March 3", "May 2025". Someone named
June is never read as the month, but "in June" still is. Something that is
not a real date ("2026-13", "Feb 30", "the last 0 days") is ignored and the
answer says so. It never causes an error. A very long span like "last 100000
years" is capped at 100 years.

## What it knows about

**Sensor data** (`health_records`, from Health Connect or an Apple Health export): sleep, steps, weight,
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
   Sentences end at `.`, `!` or `?` outside quotes, but not at the period of
   an abbreviation ("Dr.", "Mr.", "Mrs.", "Jr.", "St.", "e.g.", "p.m.",
   "Ave.", "Blvd.", "Mt.", "Co.", "Inc.", "Ltd.", "Dept.", "Univ.", "Apt.",
   "No.", "approx.", "est."), of an initial in any alphabet ("Laura J. Kim",
   "Dr. R. Ellis", "José Á. García", "Zoë Ö. Müller") or of a dotted
   abbreviation ("D.D.S.", "Ph.D.", "U.S."). Nor at any period followed by a
   lowercase word ("Ellis Dental Co. in Austin", "eggs etc. at H-E-B"): a
   sentence starts with a capital, so an abbreviation not on the list still
   holds. "On 2026-10-10 you saw Dr. Ellis [O3]." and "On 2026-10-09 José
   Á. García texted you [O1]." are each one sentence citing their record.
   Names reach answers verbatim from contact cards and `handles link --new`,
   so a period inside one never cuts a date off from its citation.
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
    uuid, source file, value and unit. A contact card also has `card`: its
    `phones` and `emails`.
  - `entities`: each with `last_contact`, `cards` and `facts` (all as ids),
    and for a place, `visits`. Then `ambiguous`, `no_data`, `loops` and the
    parsed `intent` (among its fields: `contact` for a when-or-whether call
    or text question, `due` for a due-date question, `overdue` for one
    after what was due before today, `forward` for one that looks ahead,
    `merchant`, the name a money question's merchant slot held ("at 76",
    "pay 76", "76 spending"), and `unused`, the words of a money question
    nothing could use). `loops_total` counts the open loops that match the
    question and `loops_open` every open one. `recap` is true when the
    question named only a time. `today` is the date the question was
    answered on.
  - `window`: its `start`, `end` and `label`. An explicit window also has
    `spans`, the parts of the question it was read from, and a "this week /
    month / quarter / year" (or current season) window has `through`, the
    period's last day.
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
