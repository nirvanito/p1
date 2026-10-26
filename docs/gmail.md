# Gmail, fetched every morning (no export)

A Google Takeout of Gmail ([email.md](email.md)) is a one-off: it is out of
date the day after. Connected once instead, Gmail is fetched by the daily
`collect` job by itself: each morning the mail that arrived since the last
run is downloaded over IMAP into `data/fetched/gmail/`, and the `email`
connector reads it exactly as it reads a Takeout — the same trimming of
quoted replies and signatures, the same skips (Spam, Trash, Drafts,
newsletters), the same handles, the same one row per message. Like every
source, the **first import waits for your yes**.

Nothing on the Gmail side is ever changed: see [Read-only](#read-only).

## One-time setup

1. **2-Step Verification.** App passwords need it.
   [myaccount.google.com](https://myaccount.google.com) → **Security** →
   *How you sign in to Google* → **2-Step Verification** → turn it on, if it
   is not on already.
2. **An app password.** Open
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
   type a name such as `Life CRM`, **Create**. Google shows 16 letters in
   four groups (`abcd efgh ijkl mnop`); copy them. This is not your Google
   password, and it is shown once — if you lose it, delete it there and
   make another.
3. **Connect.** In a terminal in the project folder:

   ```bash
   .venv/Scripts/python -m lifecrm connect gmail you@gmail.com
   ```

   Paste the app password when asked; it is not shown as you type, and the
   spaces do not matter. It signs in once to check, finds the All Mail
   folder, and downloads nothing yet. If something is wrong you get one
   line saying what to check, and nothing is kept.
4. **The next morning** the 06:30 `collect` fetches your newest 2,000
   messages and holds them as a question — /morning asks it, and so does
   `collect status`:

   ```
   new on this computer: email for you@gmail.com, 1,874 record(s) from 2026-06-02 to 2026-10-25 …
   ```

   To see it now instead of tomorrow: `python -m lifecrm fetch gmail`, then
   `python -m lifecrm collect approve email` (the dry run: who "you" is,
   the direction split, what was skipped). `--yes` imports it;
   `python -m lifecrm collect decline email` says never. From the yes on,
   every morning is routine.
5. **Aliases.** Mail you send *from another address* through Gmail ("Send
   mail as") reads as incoming until that address is listed:

   ```toml
   [connectors.email]
   addresses = ["you@gmail.com", "you@work.example"]
   ```

   The dry run lists any such address as "also sent from: …". The Gmail
   address itself never needs listing: every fetched message says which
   account it came from.

App passwords are not offered when 2-Step Verification uses only security
keys, under Advanced Protection, or on a work or school account whose
admin turned them off (as Google documents it; not checked here). Google
says IMAP is always on for personal Gmail accounts since January 2025; a
Google Workspace admin can still turn it off. And All Mail must be visible
over IMAP — Gmail on the web → Settings → *See all settings* → **Labels** →
All Mail → *Show in IMAP* (it is, unless it was unticked); `connect` says
so when it is not.

## What is fetched

- **All Mail**: every message except Spam and Trash, which Gmail keeps
  outside All Mail. Drafts are skipped by the connector (`skip_labels`), as
  are newsletters unless `include_lists = true` — all the
  [email.md](email.md) settings apply, `since` included.
- **Each message as Gmail stores it**, byte for byte, with three header
  lines on top, written the way a Takeout writes them so the connector
  reads them the same way:

  | header | holds |
  |---|---|
  | `X-Lifecrm-Account` | the account it was fetched from — "you", and the import's account |
  | `X-GM-THRID` | Gmail's thread id (the record's `thread`) |
  | `X-Gmail-Labels` | its labels then: `Inbox`, `Sent`, `Important`, `Starred`, `Drafts` and your own (`Work/Projects`, `Café`) — a `Sent` message is outgoing |

- **Big messages** (over 1 MB — nearly always the attachments) are fetched
  in pieces: the header and the text whole, and each attachment as its name,
  type and size only, with `X-Lifecrm-Omitted: <bytes>` where its bytes
  would be. The connector never keeps attachment bytes anyway; this way a
  20 MB deck is never downloaded either. Its size in the record is the
  server's, estimated (base64 undone), within a few bytes.
- **Labels as they were when the message was fetched.** A label you add
  later, or an archive, is not picked up (the archive remembers). Gmail's
  read state (`Opened`, `Unread`) and its tabs (`Category Promotions` and
  friends) are not labels over IMAP, so they never appear — the connector
  drops the read state from a Takeout too.

## Read-only

By construction, not by care: All Mail is opened with `EXAMINE` (the
read-only open; never `SELECT`), every message is fetched with
`BODY.PEEK` (which does not mark it read), and the job never sends a
command that stores flags, copies, moves, deletes or expunges anything.
Your unread mail stays unread. The connection checks Google's certificate
(Python's IMAP library does not by default; this one does).

An app password is a key to the whole mailbox for whatever holds it. It
is kept in `data/secrets.json` — under `data/`, private and never
committed, never in `config.toml`, never printed or logged. Gmail's own
*Last account activity* will show the IMAP sign-ins.

## How much each morning

| | |
|---|---|
| new mail | everything since the last run, every morning |
| older mail | after your yes: 2,000 messages a morning (and at most 250 MB), newest first, until it reaches your first email — about 50 mornings for 100,000 messages |
| the first fetch | the newest 2,000; until you answer, only new mail is added to what waits, so the question stays the same size and nothing more is downloaded for a no |
| a broken connection | what arrived is kept, the place it got to is kept, and the run is an error in `collect status` and /morning; the next run carries on |
| Gmail renumbers the folder (a new `UIDVALIDITY`, rare) | it starts over; every message has the same Message-ID, so nothing doubles — they are only downloaded again, 2,000 a morning |

Where it got to (the UIDs fetched, whether the backfill is done) is kept
beside the app password in `data/secrets.json`. Google documents a limit
of about 2,500 MB of IMAP downloads a day; one morning's backfill stays
well under it.

## Where the files go

```
data/fetched/gmail/
  pending/   what was fetched and is not imported yet — the folder collect reads
  archive/   what an import took in, moved here afterwards; kept, never deleted
```

Each message is one file, `<UIDVALIDITY>-<UID>.eml`. Until your first yes,
everything stays in `pending/`, and the first import reads all of it.
`archive/` grows by roughly the size of your mail without its big
attachments. To read everything again (a later reader, say):
`python -m lifecrm observe email data/fetched/gmail/archive`.

**With a Takeout as well.** The same account's messages have the same
Message-IDs, so a Takeout and the fetched mail are the same rows. If a
Takeout of the account was imported before, the account is already known
and the first fetch is collected as routine. After that a Takeout is not
needed; one imported later would rewrite labels back and forth (a Takeout
has `Category …` and `Archived`, IMAP does not).

## Disconnecting

```bash
.venv/Scripts/python -m lifecrm disconnect gmail
```

forgets the app password and where the fetch got to. What was fetched
stays in `data/fetched/gmail/`, and what was imported stays imported
(`lifecrm imports retract <uuid>` undoes an import). Delete the app
password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
too — that is what actually ends the access.

- A **new app password** for the same address (the old one revoked, or the
  Google password changed — both end every app password): run `connect
  gmail` again; it carries on where it got to. Until then each morning is
  an error saying so.
- **Stop importing without disconnecting**: `python -m lifecrm collect
  decline email` (the job then neither fetches nor imports;
  `collect approve email` takes it back).

## First-real-fetch checklist (unverified until a real account)

Written from the IMAP standards and Google's documentation of its
extensions, and tested against a stand-in server that speaks the protocol
to Python's own IMAP library — never against Gmail itself. On the first
real run:

| check | how |
|---|---|
| [ ] the sign-in with an app password works, and a wrong one says so in one line | `connect gmail` |
| [ ] All Mail is found in a non-English account | `connect gmail` succeeds; `fetch gmail` fetches |
| [ ] labels: `Inbox`, `Sent`, `Important`, your own, a non-ASCII one | a few records' `labels` (`sql "SELECT title, value_json FROM observations WHERE stream='email' LIMIT 20" --json`) |
| [ ] you are recognised and sent mail is `out` | the dry run's `you:` and `direction` lines |
| [ ] a big message with attachments: its text right, its attachments named, sizes near Gmail's | find one with a large attachment in `pending/` and read it |
| [ ] the first morning: how long 2,000 messages take, how big `pending/` is | `collect status`, the folder's size |
| [ ] nothing marked read | an unread message in Gmail stays unread after a fetch |
| [ ] no Gmail limit hit during the backfill | no "connection broke" errors on backfill mornings |

Answers so far: none.

## Commands

```bash
.venv/Scripts/python -m lifecrm connect gmail you@gmail.com   # once
.venv/Scripts/python -m lifecrm fetch gmail                   # fetch now, import nothing
.venv/Scripts/python -m lifecrm collect approve email          # the dry run; --yes imports
.venv/Scripts/python -m lifecrm collect status
.venv/Scripts/python -m lifecrm disconnect gmail
```

On a Mac the interpreter is `.venv/bin/python`.
