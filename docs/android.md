# Android capture (Samsung Galaxy) — Life CRM voice journal

Self-contained guide for getting voice notes, quick notes, and the phone's
own data (health, texts, contacts, calendar, photos) from a **Samsung Galaxy
on One UI** into a Life CRM install on a computer. Written for Samsung; any
Android phone works the same way except where a step names a Samsung app.

The computer side for a Windows laptop is in
[windows-android.md](windows-android.md). This page is only the phone.

Steps marked **(verify)** come from documentation and other people's phones,
not from a test on a real Galaxy. The checklist at the end collects them;
running it once replaces the guesses with facts.

## How it fits together

```
Voice Recorder ──Autosync──> OneDrive/VoiceJournal/inbox        (audio)
Automate flow  ──Autosync──> OneDrive/VoiceJournal/inbox-text   (typed/dictated text, optional)
Samsung Health ──> Health Connect ──scheduled export──> zip ──> lifecrm health import
SMS Backup & Restore ──scheduled──> OneDrive ──> lifecrm observe android_sms
Contacts export (.vcf), Google Takeout (Calendar, Keep, Photos) ──> lifecrm observe ...
Timeline export (Timeline.json) ──> lifecrm observe location
WhatsApp "Export chat" (.zip) ──Quick Share──> lifecrm observe whatsapp
```

To read it all back on the phone, open the laptop's web UI in the phone's
browser ([web.md, "On your phone"](web.md#on-your-phone)).

Voice and text notes are *journal sources*: they go through `/log` and your
review. Everything else is the *observed tier*: bulk imports you check with a
dry run, never reviewed one by one, always cited back to the row they came
from.

## The time in the filename (read this once)

A recording's file name is how the pipeline knows **when** you spoke. The
computer sees the file only after a sync round trip, and that can turn the
file's own timestamp into the *upload* time.

Android apps write the **calendar date** the phone's clock showed. The
pipeline applies the day rollover itself: a note recorded before 05:00
belongs to the previous journal day, so a 00:30 debrief after a long night
files under the evening it describes, with its real recording time. (The
iPhone Shortcut instead writes the already-rolled-back journal date plus a UTC
offset; the pipeline tells the two apart by the name's shape.)

Names it reads, all as "27 July 2026, 21:30:05, phone's local time":

```
Recording_20260727_213005.m4a     Voice 20260727_213005.m4a
20260727_213005.m4a               20260727 213005.m4a
2026-07-27_21-30-05.m4a           Rec 2026-07-27 21-30-05.m4a
2026-07-27 21.30.05.m4a           Call recording Mom_260727_213005.m4a
```

A name with no date (`Voice 001.m4a`) falls back to the file's modified time,
with the same rollover. A name with a date but no time keeps the date and
takes the time from the file.

Check any name before you rely on it — no file needed, nothing is written
(shown with `timezone = "America/Chicago"`):

```
python -m lifecrm filedate "Voice 20260728_003000.m4a"
Voice 20260728_003000.m4a: journal day 2026-07-27, recorded 2026-07-28T05:30:00Z — calendar date + clock time in the name
```

Pass the path of a real file instead and, when its name has no date, it
shows what the file's modified time gives.

If a phone app ever writes the journal date itself (an automation that
subtracts five hours first) without an offset, tell the pipeline in
`config.toml`:

```toml
[time]
filename_date = "journal"     # "auto" (default) | "journal" | "calendar"
```

The setting applies to every file in both inboxes, so leave it at `auto`
unless one convention is all your phones write. (`calendar` is the opposite
case: names that carry an offset but a calendar date.)

## 1. Voice notes

### The recorder: Samsung Voice Recorder

It is already on the phone, records good AAC `.m4a`, and works offline.

- **Its names carry no time:** files are `Voice 001.m4a`, `Voice 002.m4a`, …
  (If your version writes a date and time instead, the pipeline reads those
  too; check with `lifecrm filedate`.) **(verify)**
- Files land in **Internal storage › Recordings › Voice Recorder** on One UI 3
  and later; older phones used `Internal storage/Voice Recorder` or saved
  `.3ga` files (also read). **(verify)** the folder with the *My Files* app.
- Settings (⋮ › Settings) › **Recording quality**: Medium or Low. Speech does
  not need more, whisper downsamples to 16 kHz anyway, and smaller files sync
  faster and stay under the sync app's size cap. **(verify)** the names.

So with the stock recorder, **when you spoke comes from the file's modified
time.** That is correct as long as the sync keeps the phone's timestamp — the
first checklist item proves it once. If it turns out the computer sees the
upload time instead, either:

- live with it for notes uploaded within minutes (Autosync's instant upload
  usually is), and rename the rare note recorded offline before it syncs:
  in Voice Recorder, long-press it › **Rename** › `Voice 20260727_213005`
  **(verify)**; or
- switch to a recorder that puts the date and time in the file name. Several
  Play Store recorders have a file-name setting; any of the shapes above
  works. Confirm the result with `lifecrm filedate` before trusting it.

### One press to record

- **Side button**: Settings › Advanced features › **Side button** ›
  Double press › **Open app** › Voice Recorder. Double-press, tap the red
  button, talk.
- Home screen: long-press Voice Recorder › add it to the dock.
- Nightly nudge: a Clock alarm at 21:00 labelled *Log my day*, or a
  *Modes and Routines* routine (If: Time 21:00 › Then: Open an app › Voice
  Recorder) **(verify)**. Like the iPhone, nothing records unattended: the
  nudge opens the recorder; you still tap and talk.

### Sync: Autosync for OneDrive

*Autosync for OneDrive* (MetaCtrl, Play Store) copies a phone folder to a
OneDrive folder in the background. OneDrive is usually preinstalled on a
Galaxy; sign it and Autosync into the **same** Microsoft account the computer
uses.

1. Autosync › **Create folder pair**:
   - Local folder: `Recordings/Voice Recorder`
   - Remote folder: `VoiceJournal/inbox`
   - Sync method: **Upload only**. *Upload then delete* also works if you
     do not want a copy left on the phone. **(verify)** the method names.
2. Settings: turn on instant sync; Wi-Fi only is the safe default, allow
   mobile data if you want notes synced away from home.
3. The free tier allows **one folder pair** and caps file size **(verify)**
   the current cap in the app. Record a 20-minute test note: if it does not
   upload, lower the recording quality or upgrade.

Uploading into **someone else's** journal (they run the pipeline, you only
record): they share `VoiceJournal` from OneDrive with edit access; in the
OneDrive app open Shared › the folder › **Add shortcut to My files**, then
pick that shortcut folder as Autosync's remote folder.

If a note you already logged ever reappears in the inbox (a sync app
re-uploading it), intake recognizes the identical bytes and archives it as a
duplicate. Nothing is counted twice.

### Keep Samsung from killing the sync

One UI puts background apps to sleep aggressively, and a sleeping Autosync
simply stops uploading — no error, nothing arrives.

- Settings › Battery › **Background usage limits** › **Never sleeping apps** ›
  add **Autosync** (and OneDrive, Automate and SMS Backup & Restore if you
  use them). On older One UI this is under Settings › Battery and device care
  › Battery. **(verify)** the path on your version.
- Make sure the same apps are not in *Sleeping apps* or *Deep sleeping apps*.
- Settings › Apps › Autosync › Battery: **Unrestricted**; Mobile data: allow
  background data use.

### Call recordings (optional)

Where the Samsung Phone app offers call recording (it depends on region and
carrier; recording a call may need everyone's consent where you live), its
files are named like `Call recording Mom_261020_200512.m4a` — a two-digit
year, read correctly by the pipeline — and saved in
`Recordings/Call` **(verify)**. To journal them, add a folder pair for that
folder to the same inbox (a paid Autosync feature) or move chosen ones into
`Recordings/Voice Recorder`. A call recording becomes a source like any
other note, through `/log` and your review.

## 2. Quick notes during the day

**Simplest: just record.** A ten-second voice note through the same recorder
and the same sync is the quick note on a Samsung. It is the route with no
extra setup and it works offline.

**Typed or dictated text (optional).** Samsung Notes and Keep have no way to
drop one plain-text file per note into a folder, so use a small automation
that writes the file itself. With **Automate** (LlamaLab, free for small
flows) **(verify)** the block names and the expression syntax in the current
version:

1. In My Files, create the folder `VoiceJournal-text` in Internal storage.
2. New flow: *Flow beginning* › *Dialog input* (title "Quick note"; dictate
   with the keyboard's microphone key) › *File write*:
   - File: `"/storage/emulated/0/VoiceJournal-text/" ++ dateFormat(Now, "yyyy-MM-dd HH.mm.ss") ++ ".txt"`
   - Content: the dialog's output variable; **Append off**.
3. In *Flow beginning*, **Install home screen shortcut**.
4. Give Automate storage access when it asks, and add it to Never sleeping
   apps.
5. Sync `VoiceJournal-text` → OneDrive `VoiceJournal/inbox-text` with a
   second folder pair (paid Autosync), or with *FolderSync*, which allows
   several pairs on its free tier **(verify)**.

The name it writes, `2026-07-27 14.05.33.txt`, is the calendar date, which is
exactly what the pipeline expects from an Android phone. **One file per note**
— never append to a shared daily file.

Any other app that saves a `.txt` per note into a synced folder works, as
long as the name carries the date and time (`lifecrm filedate` tells you).

## 3. Health: Samsung Health → Health Connect

The pipeline imports Android's **Health Connect** export, not Samsung Health
directly; Samsung Health writes into Health Connect.

1. Samsung Health › ≡ › Settings › **Health Connect** › allow Samsung Health
   to read and write every category you want (sleep, steps, weight,
   exercise, heart rate, nutrition). **(verify)** the menu path.
2. Health Connect itself: on Android 14 and later it is part of the system
   (Settings › search "Health Connect"). Under *App permissions*, let **only
   one app write each metric** — with Samsung Health *and* Google Fit both
   writing steps, each day's total is whichever app counted more (never the
   two added up), so one app's partial day can stand in for the other's.
3. Health Connect › **Export / Scheduled export** **(verify)**: daily, to a
   folder the computer can reach (Google Drive, or OneDrive if it is offered
   as a destination).
4. On the computer, dry run first, then apply — the full walkthrough and the
   first-import checklist are in [health-connect.md](health-connect.md):

```
python -m lifecrm health import "<path to the export .zip>"
python -m lifecrm health import "<path to the export .zip>" --apply
```

Samsung Health's own *Download personal data* (a folder of CSVs) is not
read by anything here; Health Connect is the supported route.

## 4. Texts and calls: SMS Backup & Restore

*SMS Backup & Restore* (SyncTech, free) writes `sms-<timestamp>.xml` and
`calls-<timestamp>.xml` and can upload them to OneDrive on a schedule.

1. **Set up a backup** › tick **Messages** and **Call logs**.
2. Back up **all conversations**, not selected ones. Texts mirror the phone
   (a message deleted there leaves the archive at the next full backup), and
   only a backup of everything can say what the phone no longer holds.
3. Where: **OneDrive** (sign in) › a folder such as `SMSBackupRestore`
   **(verify)** the default.
4. **Schedule recurring backups**: daily, at night.
5. MMS media (photos sent in texts) makes the files large and is not needed
   for the text; leave it out unless you want it.

On the computer (dry run, then `--apply`):

```
python -m lifecrm observe android_sms "<OneDrive>\SMSBackupRestore\sms-20261020213000.xml"
python -m lifecrm observe android_sms "<…>\sms-20261020213000.xml" --apply
python -m lifecrm handles list --suggest
```

RCS chats (Samsung Messages "Chat", Google Messages) may be missing from or
incomplete in the backup: they live in the messaging app's own database,
which SMS Backup & Restore cannot always see. **(verify)** by checking that a
known RCS conversation appears in the dry run's sample lines.

## 5. Contacts (.vcf)

Contact cards give phone numbers and email addresses their names, so texts,
calls and calendar attendees can be linked to people.

1. **Contacts** › ≡ › **Manage contacts** › **Import or export contacts** ›
   **Export** › Internal storage. **(verify)** — it writes a `.vcf` file
   (often `Contacts.vcf`) and says where.
2. Share that file to OneDrive (My Files › long-press › Share › OneDrive).
3. On the computer: `python -m lifecrm observe contacts "<path>\Contacts.vcf"`,
   read the report, then `--apply`. Names appear as suggestions in
   `lifecrm handles list --suggest`; linking a handle to a person is always
   your decision.

## 6. Calendar, notes, photos: Google Takeout

1. On any browser: **takeout.google.com** › *Deselect all* › tick
   **Calendar**, **Keep** and (optionally) **Google Photos**.
2. Delivery: **Add to OneDrive** (or a download link), frequency **Export
   once** for the history; "every 2 months for 1 year" keeps it topped up.
   **(verify)** where OneDrive deliveries land (an `Apps` subfolder).
3. Extract the zip on the computer, then dry-run each connector on its
   folder and `--apply` when the samples look right:

```
python -m lifecrm observe calendar "<…>\Takeout\Calendar"
python -m lifecrm observe notes    "<…>\Takeout\Keep"
python -m lifecrm observe photos   "<…>\Takeout\Google Photos"
```

- **Samsung Calendar:** only events in your *Google* calendar reach
  Takeout. Events saved to the phone's local "My calendar" or a Samsung
  account calendar do not **(verify)**; in Samsung Calendar › Settings, make
  the Google account the default for new events.
- **Samsung Notes** has no bulk export (per note: Share › *Text file*). For
  notes you want in the archive, use Keep, or save them as text into a synced
  folder.
- **Photos** are read for metadata only (time, place, camera) — never the
  pixels. Google Photos Takeout carries that metadata in sidecar JSON files.

### Photos via Samsung Gallery → OneDrive

Gallery › ≡ › Settings › **Sync with OneDrive** **(verify)** puts new photos
in your OneDrive (under *Pictures*). That keeps the photo archive current
without Takeout: point `observe photos` at that OneDrive folder. Note that a
folder of online-only OneDrive files is downloaded file by file when read —
mark it *Always keep on this device* first, or use Takeout for the history.

### Location and browser history

- **Where you were:** Settings › Location › Location services › Timeline ›
  **Export Timeline data** writes `Timeline.json` **(verify)**. Save it to
  OneDrive and run `python -m lifecrm observe location`. See
  [location.md](location.md).
- **What you read:** with Chrome sync on, the phone's Chrome history arrives
  in the laptop's Chrome, and `python -m lifecrm observe browser` reads it
  there. See
  [browser.md](browser.md).

## 7. Moving a recording to another day

If a note landed on the wrong day, fix it at intake for that one file
(`--date` requires `--file`):

```
python -m lifecrm transcribe --file "<inbox>\Voice 014.m4a" --date 2026-07-27
```

Or just tell `/log` ("log this as yesterday") — you review every extraction
anyway.

## Verify it on the real phone (once)

The pipeline side is tested with synthetic files named as above. What only a
real Galaxy can show:

- [ ] Record a note, wait **ten minutes**, then let it sync. On the computer,
      `python -m lifecrm filedate "<OneDrive>\VoiceJournal\inbox\Voice 0NN.m4a"`
      — does "recorded" match when you spoke, or when it uploaded? (This
      decides whether the stock recorder's names are enough.)
- [ ] Voice Recorder: the folder it saves to, the file names it writes, the
      quality setting names, and whether Rename exists in the list.
- [ ] Autosync: sync method names, the free tier's file-size cap (record 20
      minutes), and upload latency on Wi-Fi.
- [ ] With Autosync in **Never sleeping apps**, a note recorded with the
      screen off still uploads within the hour.
- [ ] Side button › Double press › Open app works with Voice Recorder.
- [ ] Automate (if used): block names, the `dateFormat` file name, and that
      the `.txt` reaches `inbox-text`.
- [ ] Health Connect: the scheduled-export menu path, its destinations, and
      that only one app writes steps.
- [ ] SMS Backup & Restore: the OneDrive folder it uses, that a scheduled
      backup fires overnight without opening the app, and whether RCS chats
      are included.
- [ ] Contacts export: the menu path and the `.vcf` name it writes.
- [ ] Takeout to OneDrive: where the zip lands; Samsung Calendar events made
      on the phone appear in it.
- [ ] Gallery › Sync with OneDrive: the OneDrive folder photos land in.

Write what you find back into this page.
