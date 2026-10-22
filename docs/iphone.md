# iPhone capture — Life CRM voice journal

This guide is self-contained: everything needed to capture voice notes and
quick dictated notes from an iPhone into a Life CRM inbox, using Apple's
built-in **Shortcuts**.

How it works: you record (or dictate) on the phone; the file lands in a
cloud folder; the computer running the Life CRM pipeline picks it up from
there. Which cloud depends on that computer:

| the computer is | save to | install on the phone |
|---|---|---|
| **a Mac** (recommended pairing) | **iCloud Drive** | nothing — iCloud Drive is built in |
| a Windows PC | **OneDrive** | OneDrive (free, App Store) |

```
iCloud Drive/VoiceJournal/inbox         <- audio notes (.m4a)       (Mac)
iCloud Drive/VoiceJournal/inbox-text    <- dictated text notes (.txt)
OneDrive/VoiceJournal/inbox             <- the same, for a Windows PC
OneDrive/VoiceJournal/inbox-text
```

On a Mac, `lifecrm configure` creates the iCloud Drive folders for you (see
[mac.md](mac.md)); iCloud Drive is also the one place a Shortcut's **Save
File** can always pin a fixed folder, so the file-picker caveat below does
not apply. Everywhere this guide says `VoiceJournal/inbox`, use the folder
in the cloud that matches your computer.

If you're uploading into **someone else's** journal (they run the pipeline,
you capture): with OneDrive, they share their `VoiceJournal` folder (Share ->
invite you with edit access), and in your OneDrive app you open Shared -> the
folder -> **Add shortcut to My files**; it then appears inside your own
OneDrive in the Files app, and the shortcuts below can save into it. With
iCloud Drive, they share the folder from Finder (Share -> Share Folder,
*can make changes*) and it appears in your iCloud Drive.

## The two-timestamp rule (why the filename looks like that)

Every file is named:

```
<journal date> <clock time> <utc offset>.m4a
2026-07-27 21.30.05 -0500.m4a
```

- **Clock time** is the real, unmodified time of capture (dots, not colons —
  colons are illegal in filenames).
- **Journal date** is the day the note *belongs to*: notes recorded before
  **05:00** count as the previous day, so a 00:30 debrief after a long night
  files under the day it describes. That's why the shortcut subtracts 5 hours
  before formatting the date — and only for the date, never for the time.
  (If the pipeline's `day_rollover_hour` is configured to something other
  than 5, use that number instead.)

Do not "simplify" this by formatting both from the adjusted date: that stamps
a 00:30 note as 19:30.

## Shortcut 1 — "Log My Day" (audio)

Create in Shortcuts (exact action names as they appear in iOS):

1. **Record Audio** — Audio Quality: Normal, Start Recording: Immediately.
   (Recording stops when you tap Stop.)
2. **Current Date** — no changes.
3. **Format Date** — Date: `Current Date`, Date Format: **Custom**, Format
   String: `HH.mm.ss ZZZ` → produces `21.30.05 -0500`. Rename this action's
   result to *ClockTime* if you like tidy variable names.
4. **Adjust Date** — `Subtract` `5` `hours` from `Current Date`.
5. **Format Date** — Date: the adjusted date from step 4, Custom format
   `yyyy-MM-dd` → *JournalDate*.
6. **Text** — `[JournalDate] [ClockTime]` (insert the two Format Date results
   as magic variables, one space between).
7. **Set Name** — Input: `Recorded Audio`, Name: the Text from step 6.
   Leave "Don't Include File Extension" ON so iOS keeps `.m4a`.
8. **Save File** — Input: the renamed audio.
   - Turn **Ask Where to Save** OFF and set the destination to
     `VoiceJournal/inbox` — in **iCloud Drive** (Mac) or **OneDrive**
     (Windows PC).
   - Turn **Overwrite If File Exists** OFF.
   - **iCloud Drive (Mac):** a fixed destination always works. Some iOS
     versions only take a path *inside* iCloud Drive's Shortcuts folder
     rather than any folder; if yours does, use `VoiceJournal/inbox` there
     and point the Mac at it (mac.md, section 3, has the one command).
   - **OneDrive caveat (Windows PC):** on some iOS versions, Save File only
     allows a fixed destination path for iCloud Drive, and third-party
     providers (OneDrive) force the file picker. If you can't pin the OneDrive
     folder, leave *Ask Where to Save* ON — it costs two taps per note
     (OneDrive is remembered as the last location) — or use the Voice Memos
     fallback below.
   - Please report which behavior your iOS shows; it's the one step that can
     only be tested on a real phone.

## Shortcut 2 — "Quick Note" (dictated text, for mid-day moments)

One nightly note only captures what you still remember at 9pm. This one is
for the ten-second thought at 2pm. One file per note — never append to a
shared daily file.

1. **Dictate Text** — Language: yours, Stop Listening: After Pause.
2. **Current Date**.
3. **Format Date** — `Current Date`, Custom `HH.mm.ss ZZZ`.
4. **Adjust Date** — Subtract 5 hours from `Current Date`.
5. **Format Date** — adjusted date, Custom `yyyy-MM-dd`.
6. **Set Name** — Input: `Dictated Text`, Name:
   `[JournalDate] [ClockTime].txt`.
7. **Save File** — destination `VoiceJournal/inbox-text` (iCloud Drive or
   OneDrive, as above), same toggles and same caveats.

## Triggers — pick what fits your hardware

| Trigger | Where | Hardware |
|---|---|---|
| Action Button | Settings -> Action Button -> Shortcut | iPhone 15 Pro and later |
| Back Tap (double-tap the phone's back) | Settings -> Accessibility -> Touch -> Back Tap | any recent iPhone |
| Lock Screen control | Lock Screen customization / Control Center (iOS 18+) | iOS 18+ |
| Home Screen icon | Shortcuts -> (i) -> Add to Home Screen | any |
| Siri | "Hey Siri, Log My Day" (shortcut name is the phrase) | any |

A nightly nudge: Shortcuts -> Automation -> New Automation -> **Time of Day**
(e.g. 21:00, Daily) -> run "Log My Day". Be aware: **this opens the shortcut;
it does not record unattended.** iOS will not capture audio from a background
automation — you still tap and talk. There is no hands-free mode.

## Fallback that always works

Record in **Voice Memos** -> Share -> **Save to Files** ->
`VoiceJournal/inbox` (iCloud Drive or OneDrive). The filename will have no
date ("New Recording 7.m4a") — the pipeline then falls back to the file's
timestamp, which after a cloud round trip may be the *upload* time. Fine for
a note uploaded promptly; for anything delayed, rename the file in the Files
app to the `yyyy-MM-dd HH.mm.ss` form first.

## Verification checklist (run on the real phone)

The pipeline side is tested with synthetic files named exactly as above.
What is **not** tested — and cannot be, without your hands on the phone — is
Shortcuts saving into iCloud Drive or the OneDrive file provider. Please run:

1. Build "Quick Note". Run it, dictate one sentence, and confirm the `.txt`
   appears in `VoiceJournal/inbox-text` in the Files app (iCloud Drive) or
   the OneDrive app.
2. On the computer, wait for sync and confirm the file shows up in the local
   folder — Finder's iCloud Drive on a Mac, the OneDrive folder on Windows
   (note roughly how long sync took). On a Mac, `lifecrm where` shows the
   count of notes waiting.
3. Run `python -m lifecrm intake` there, then `python -m lifecrm status --json`
   — confirm the note's journal date and the recorded time match reality.
4. Build "Log My Day". Record a ~3 minute note; confirm the `.m4a` lands in
   `inbox`.
5. Run `python -m lifecrm intake` and note the whisper wall-clock time for
   that ~3 minute note.
6. Run `/log` in Claude Code end to end: review the extraction, apply it.
7. Report back: sync latency, whisper wall-clock, and whether Save File let
   you pin the folder (step 8: any iCloud Drive folder, only one under
   Shortcuts, or — for OneDrive — not at all) — those three facts replace
   the guesses in the docs.

## Beyond capture

- **Reading it on the phone:** open the computer's web UI in Safari and add
  it to the Home Screen ([web.md, "On your phone"](web.md#on-your-phone)).
- **The phone's other data** comes in on the computer. On a Mac that means
  Messages, calls, Contacts, Photos, Notes and Apple Health
  ([mac.md](mac.md)). Safari history syncs into the Mac's Safari through
  iCloud ([browser.md](browser.md)). Apple's own location history cannot be
  exported; only Google Maps Timeline on the iPhone can
  ([location.md](location.md)).
