# Calls from an iPhone (phone and FaceTime)

The iPhone's Recents list is a Core Data store, `CallHistory.storedata`. The
`ios_calls` connector reads it into the observed tier as one `ios_call`
observation per call — who (a handle, never a name), when, how long, which
way, answered or missed, phone or FaceTime. Like the texts connectors it is a
pure reader: the store is copied through SQLite's backup API and read from
the copy, and nothing is ever written back.

## Where the call history is

| source | path | notes |
|---|---|---|
| **A Mac** (the everyday path) | `~/Library/Application Support/CallHistoryDB/CallHistory.storedata` | holds the iPhone's calls when they sync to the Mac, plus the Mac's own FaceTime calls; current without a backup |
| **An iPhone backup** | `HomeDomain` / `Library/CallHistoryDB/CallHistory.storedata` — in the backup folder as `5a/5a4935c78a5255723f707230a451d79c540d2741` (SHA1 of `HomeDomain-Library/CallHistoryDB/CallHistory.storedata`) | Apple lists call history among what only an **encrypted** backup carries, so an ordinary unencrypted backup is expected not to have it |

For the Mac to hold the phone's calls: sign in to FaceTime on the Mac with
the iPhone's Apple ID, turn on **FaceTime > Settings > Calls from iPhone**,
and on the iPhone **Settings > Apps > Phone > Calls on Other Devices** with
the Mac allowed.

An encrypted backup cannot be read without its password, and the connector
refuses it (see `docs/texts.md`). An unencrypted backup without the file is
refused with a pointer here rather than an empty import. So in practice:
**MacBook + iPhone reads calls from the Mac.** On Windows with an iPhone
there is usually no call history to read.

## Importing

```
.venv/bin/python -m lifecrm observe ios_calls            # dry run
.venv/bin/python -m lifecrm observe ios_calls --apply
```

With no path it reads this Mac's call history when there is one, else the
single iPhone backup on this machine (the same search as `ios_messages`,
which never guesses between several phones). A path works too: the store
file, the `CallHistoryDB` folder, a backup folder, or the `Backup` folder
above one backup. Reading the Mac's store needs **Full Disk Access** — a
missing permission is reported as one line saying so (`docs/texts.md`,
*Full Disk Access*).

The report shows where it read from, the account, the span of calls ("check
the oldest against the Recents list"), how a few raw numbers were
normalized, new/changed counts with the number of missed calls and a sample,
anything skipped and any call type not understood.

## What comes in

| column | becomes |
|---|---|
| `ZUNIQUE_ID` | `external_id` (a record without one gets a stable id made from its number, time and direction) |
| `ZDATE` | `started_at` — Core Data time, seconds since 2001-01-01 UTC |
| `ZDURATION` | `value`, unit `s`; `ended_at` = start + duration |
| `ZORIGINATED` | `direction`: 1 `out`, 0 `in` |
| `ZANSWERED` | an incoming call never answered is **missed** (`value_json.missed`); an outgoing one nobody picked up is "not answered", not missed |
| `ZCALLTYPE` | 1 phone, 8 FaceTime video, 16 FaceTime audio; a call another app placed through CallKit (WhatsApp, say) names the app in `ZSERVICE_PROVIDER` and is kept as an app call, with the app as `origin_app` |
| `ZADDRESS` | the participant: a phone or email **handle**, normalized as for texts. Text on most iOS versions, a blob of the same bytes on some — both read |
| `ZNAME` | the name the phone showed: the handle's `display_name` when no contact card names it |
| `ZISO_COUNTRY_CODE`, `ZLOCATION` | `value_json.country`, `value_json.number_location` |

The title says what kind of call it was (`phone call`, `missed phone call`,
`FaceTime video call`, …) so search finds calls by kind; the person is the
handle, and the name comes from linking it (`handles list --suggest`).

Columns differ across iOS versions, so each is checked before it is read and
a missing one reads as empty. A call type not yet mapped is still imported
and counted under "not understood".

Names: the Mac's Contacts stores (beside the call history, in
`Application Support/AddressBook`) or the backup's own address book, by the
same rule as texts — and the phone's `ZNAME` only where no card names the
number. Nothing is linked to an entity automatically.

## Calls are remembered

Unlike texts, a call is never removed by a later import (decided
2026-10-22, the same rule as Android's call log). The call history is not the
phone's whole record: iOS keeps only its newest calls and drops older ones by
itself, a few at a time. Mirroring it would remove every call the phone aged
out — never enough at once to trip the mass-removal guard — until the archive
held no more than the phone does. A call you delete from Recents therefore
stays archived; `lifecrm imports retract <uuid>` undoes an import.

Each import still records its **account** — which device it read:

- for a backup: the `Phone Number` in its `Info.plist`;
- otherwise: the store's own Core Data identity, `callhistory:<Z_METADATA.Z_UUID>`
  — stable for the life of that device's store, different on every device.

The same call held by both a backup and the Mac is one record (its unique id
travels with the sync).

## Freshness

`lifecrm doctor` warns (`ios_calls_data_age`) when the newest imported call
is more than 14 days old — the sync or the backups have probably stopped.

## Known limitations, by design

- Group FaceTime participants are an archived blob
  (`ZREMOTE_PARTICIPANT_HANDLES`) and are not decoded; such a call carries
  the one handle in `ZADDRESS` when there is one.
- Journal dates use the configured home timezone; the store records none.
- Voicemail is a separate store and is not read.

## First-real-import checklist (unproven until a real store)

Everything above is tested against synthetic stores with the real table and
column names. What only a real Mac or phone can settle:

| check | how |
|---|---|
| [ ] The Mac store really holds the iPhone's calls | `calls span …` reaches back past the Mac's own FaceTime use |
| [ ] A call's `ZUNIQUE_ID` is the same on the Mac and in a backup | import both: the second should be all *identical* |
| [ ] Whether an unencrypted backup really lacks the store on current iOS | `observe ios_calls <backup>` — refused, or read |
| [ ] Call types and providers seen | `not understood: … call type N` |
| [ ] `ZADDRESS` spelling against contacts | `normalized, e.g.: …` |

Answers so far: none.
