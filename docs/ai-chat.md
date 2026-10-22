# AI chat history import

Your conversations with Claude and ChatGPT are the densest record you have of
how you actually think — and they are the easiest of all the observed-tier
sources to get: one export request, one JSON file, no phone, no API key, no
recurring cost.

They land in the **observed tier**: bulk records that never pass the `/log`
review gate, are never asserted as fact, and are always cited. Nothing here can
touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

**Claude** — Settings → Privacy → *Export data*. A link arrives by email;
the download is a `.zip` containing `conversations.json`.

**ChatGPT** — Settings → Data controls → *Export data*. Same shape: an
emailed link, a `.zip` containing `conversations.json` and `user.json`.

Both exports are point-in-time snapshots. Re-export whenever you want to catch
up; re-importing is always safe.

## Importing

Dry-run first — always. It is the only place a wrong date or a misparsed body
is cheap to notice.

```bash
.venv/Scripts/python -m lifecrm observe ai_chat claude-export.zip
```

```
ai-chat export: claude-export.zip (claude, 412 conversation(s), member: conversations.json)
  account: a1b2c3
  claude_chat: 8134 new, 0 changed | 2024-02-11..2026-08-19 | e.g. 2026-08-19 you: …
  handle: app_account:claude:a1b2c3 — resolve it with 'lifecrm handles link' …
  skipped: 106 × message with no text
dry run — nothing written. Re-run with --apply to write.
```

Read the date span. If it does not match when you actually used the product,
stop and say so — that is the signal that something is being parsed wrong.

Then write:

```bash
.venv/Scripts/python -m lifecrm observe ai_chat claude-export.zip --apply
```

The command prints an import uuid. `lifecrm imports retract <uuid>` undoes the
whole run (reversible with `lifecrm imports restore <uuid>`).

Accepted targets: the `.zip`, an unzipped directory, or `conversations.json`
itself.

## What gets imported

**One observation per message**, not per conversation. Recall works at turn
granularity, and a confirmed entry citing one of these as provenance should
point at the sentence, not at a 200-turn thread. Every row carries the
conversation name in `title`, so full-text search finds a thread by its name
and each row is self-describing.

| kept | not kept |
|---|---|
| your turns and the assistant's | system prompts and hidden context |
| conversation title, timestamps, model | tool calls, code-interpreter output |
| ChatGPT's abandoned branches, marked | attachments and images (counted only) |

Deliberate choices worth knowing:

- **All ChatGPT branches are imported.** Regenerated answers and edited
  prompts leave every branch in the export, and all of them are imported;
  each row records `on_current_path` so recall can prefer the answer you kept.
- **Media stays out of the database.** An attachment is a count in the detail
  blob, never a blob in SQLite.
- **Sub-second timestamps are dropped**, so two exports of the same message
  compare equal instead of looking changed.
- **Empty messages are skipped and counted**, not imported as blank rows.

## Re-importing, and what wins

Identity is the vendor's own message id, so a later export of the same account
is a no-op for everything already stored:

- **identical** — skipped silently.
- **update** — the record changed *and* is strictly newer. Applied, audit-logged.
- **conflict** — the record changed but is *not* newer. **Never applied**,
  surfaced in the report so you decide.

## Deleted conversations: the archive mirrors the account

An export is the account's whole history, so a conversation you delete in
Claude or ChatGPT is gone from the next export — and the next import removes
it here too (decided 2026-09-24). The dry run shows what would go, by
conversation:

```
  remove: 12 no longer in the export (deleted) — 12 claude_chat | from Slovenia trip planning (12)
```

The guards are the same as for texts (docs/texts.md):

- **Only what the export no longer holds.** Every message id in the export
  counts as present — including system turns and anything else this importer
  skips — so a message it cannot read is never mistaken for a deleted one.
- **Only this vendor, only this account.** A Claude export never removes
  ChatGPT conversations, and one account's export never touches another's.
  An export that does not name its account — ChatGPT without `user.json` —
  removes nothing, and the report says so.
- **Never most of the archive at once.** Removing more than 100 messages and
  more than 10% of that account's archive in one import is refused without
  `--allow-mass-removal` — a truncated or wrong export looks like mass deletion.
- **Snapshot first.** An import that removes anything takes a standard
  database snapshot beforehand.

ChatGPT's abandoned branches are still in its export, so they stay: only what
the export itself drops is removed.

## Attribution: link the account once

The importer emits **handles, never names** — here, the account the export
belongs to. Resolving it is a join, so one decision attributes every message
you have ever sent, past and future, with no backfill pass:

```bash
.venv/Scripts/python -m lifecrm handles list
.venv/Scripts/python -m lifecrm handles link app_account:claude:a1b2c3 --new "Your Name"
```

`--entity <id>` links to an entity that already exists. `handles ignore` is
for handles never worth resolving; it is reversible.

`lifecrm doctor` warns while any handle seen in an import is still unlinked —
an observation attributed to nobody answers none of the questions this tier
exists to answer.

## Known limitations

- **Timezone.** Chat exports carry no per-message zone offset, so journal dates
  use your configured home timezone. A conversation held while travelling lands
  on the home-timezone day. The export does not say where you were, and
  guessing would be worse than the documented rule.
- **Memory.** A large export is parsed whole. A 100 MB `conversations.json`
  wants a few hundred MB of RAM for the duration of the run.

## Commands

```bash
.venv/Scripts/python -m lifecrm connectors                     # what's available, last run
.venv/Scripts/python -m lifecrm observe ai_chat <export>       # dry run
.venv/Scripts/python -m lifecrm observe ai_chat <export> --apply
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm imports restore <uuid>
.venv/Scripts/python -m lifecrm handles list [--all]
.venv/Scripts/python -m lifecrm handles link <kind:value> --entity <id>
.venv/Scripts/python -m lifecrm handles ignore <kind:value>
```
