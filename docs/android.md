# Android capture — Life CRM voice journal

Self-contained guide for capturing voice notes and quick text notes from an
Android phone into a Life CRM inbox. Android needs one recorder app and one
sync app; both have free tiers.

Target folders (the pipeline watches these):

```
OneDrive/VoiceJournal/inbox         <- audio notes (.m4a)
OneDrive/VoiceJournal/inbox-text    <- dictated/typed text notes (.txt)
```

Uploading into **someone else's** journal: they share `VoiceJournal` from
OneDrive with edit access; in the OneDrive app open Shared -> the folder ->
**Add shortcut to My files**, then point the sync app (below) at that
shortcut folder.

## Audio notes

1. **Recorder app** — anything that can save m4a to a folder you choose with
   a filename template. *Easy Voice Recorder* works well:
   - Format: `.m4a` (AAC), mono is fine (~1 MB/min).
   - Save location: a phone folder named `VoiceJournal`.
   - Filename template: date **and time**, e.g. `2026-07-27 21.30.05.m4a`
     (dots in the time — colons are illegal in filenames). The time in the
     name is what makes `recorded_at` survive a OneDrive round trip, where
     the file's timestamp can become the upload time.
   - Put the app's record widget on your home screen: one tap to start.
2. **Sync app** — *Autosync for OneDrive (OneSync)*, free tier:
   - One folder pair: phone `VoiceJournal` -> OneDrive `VoiceJournal/inbox`.
   - Sync method: **Upload only**. Enable instant upload; Wi-Fi-only is the
     safe default, allow cellular if you want same-hour sync away from home.
   - Free-tier file size cap (~30 MB) fits notes up to roughly half an hour.
3. Fallback that always works: record with anything, then Share -> OneDrive
   -> `VoiceJournal/inbox`.

**The after-midnight caveat (honest limitation):** the pipeline files notes
recorded before 05:00 under the *previous* day — but only when it computes
the date itself. A filename date always wins, and Android recorder apps stamp
the *actual* calendar date. So a 00:30 note about yesterday gets tomorrow's...
strictly, today's date in the filename and files under the wrong day. Three
ways to handle it, pick one:

- Ignore it — you review every extraction anyway, and `/log` can be told the
  date ("log this as yesterday").
- Fix it at intake: `python -m lifecrm transcribe --date 2026-07-27`.
- Drop the date from the filename template (keep the time). Then the
  pipeline's rollover logic decides the day — at the cost of trusting file
  timestamps through OneDrive. (iPhone users don't have this trade-off; the
  Shortcuts recipe computes the rolled-back date on the phone.)

## Quick text notes (mid-day)

Any route that drops a `.txt` file into a synced folder works. Two that do:

- Gboard voice typing into a plain-text notes app that stores files
  (e.g. *Markor*), saving to a phone folder `VoiceJournal-text`, with a
  second Autosync folder pair -> OneDrive `VoiceJournal/inbox-text`.
- A "note to self" automation app (Tasker, Macrodroid) writing
  `<date> <time>.txt` files to that folder.

Name files `2026-07-27 14.05.33.txt` (same convention as audio). One file per
note — never append to a shared daily file.

## Verify it end to end

1. Record a short note; watch it appear in OneDrive (app) and then in the
   computer's OneDrive folder.
2. On the computer: `python -m lifecrm intake` — confirm the journal date and
   recorded time are right (`python -m lifecrm status --json`).
3. Run `/log` in Claude Code and apply the extraction.
