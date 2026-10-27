# Online sources — collected with no export

Most sources reach this computer by themselves: a phone app's scheduled
backup lands in OneDrive, the browser keeps its own history file, the Mac
keeps Messages. A few live only on a service's servers — Google Calendar,
Gmail, Google Contacts, Last.fm. For those there is no export to make: you
**connect** each one once, and from then on the daily collect job (06:30,
`docs/collect.md`) downloads what is new before it reads anything.

```
python -m lifecrm connect                    which services there are, which are connected
python -m lifecrm connect SERVICE [ARGS]...  set one up (asks for what it needs)
python -m lifecrm fetch SERVICE              download its new data now, import nothing
python -m lifecrm disconnect SERVICE         forget its link / password / token
```

Where these four sit among all the others — what else arrives by itself,
what is still a download now and then — is
[automatic-collection.md](automatic-collection.md).

## How it works

1. **Fetch.** Each morning, for every connected service, the job downloads
   into `data\fetched\<service>\`, in a format an existing reader already
   understands: `.ics` files for the calendar connector, `.eml` files for
   email, a vCard for contacts, Last.fm's own pages for the `lastfm`
   connector.
2. **Read.** From there it is an ordinary source: the same plan, the same
   holds. A service's **first import waits for your yes** — `/morning` asks,
   or `python -m lifecrm collect approve <connector>` shows the preview and
   imports on yes. A large removal waits too, exactly as it would for an
   export.
3. **Settle.** A service that fetches only what is new (mail, scrobbles)
   keeps what it fetched in `pending\` until an import that read it has
   committed, then moves it to `archive\`. So a first import waiting for
   your yes still finds everything, and nothing is lost when a morning
   fails. A service that fetches its whole state each time (a calendar link,
   the address book) replaces its file instead.

A morning with nothing new is `unchanged`. A failed download (network down,
a link reset, a revoked password) is an **error** in `collect status` and a
line in `/morning` — never a silent skip — and the next run tries again.

## What is kept, and where

What a service needs — a private link, an app password, a refresh token —
is typed by you into your own terminal (passwords hidden as you type) and
kept in `data\secrets.json`: never in `config.toml`, never printed, never in
a collect result. `data\` is gitignored as a whole. `disconnect` forgets the
entry; what was fetched stays in `data\fetched\`, and what was imported
stays imported (`imports retract <uuid>` hides an import).

Fetching is the daily job's only network use. Every service is read-only:
nothing is ever sent to it but the request for your own data, and nothing on
the service is changed (mail is not marked read, nothing is moved).

## The services

### Google Calendar — `google_calendar` → the `calendar` connector

No sign-in: each calendar has a private iCal link.

1. calendar.google.com → ⚙ Settings → under *Settings for my calendars*
   click a calendar → *Integrate calendar* → copy **Secret address in iCal
   format**.
2. `python -m lifecrm connect google_calendar` and paste the link (or pass
   several links, one per calendar).

Each morning every link's current state is downloaded (one file per link);
repeats are expanded to today + 90 days, and an event deleted from the
calendar is gone from the next file (the calendar connector mirrors per
calendar). The link is a password in all but name; Google's *Reset* next to
it invalidates the old one — then connect again with the new link. Any other
https iCal link works the same way.

### Google Contacts — `google_contacts` → the `contacts` connector

Your Google address book every morning. Google has no way into contacts
without OAuth (its CardDAV server accepts nothing else), so this one takes a
ten-minute Google Cloud setup, once: your own project with the People API
on, a consent screen published to *In production* (in *Testing*, Google ends
the sign-in after 7 days), and a *Desktop app* OAuth client whose JSON you
download. Then `python -m lifecrm connect google_contacts "<client JSON>"`
opens Google's sign-in in your browser; Life CRM never sees your password
and keeps only a read-only refresh token. Step by step:
[docs/google_contacts.md](google_contacts.md).

Each morning the whole address book is written to
`data\fetched\google_contacts\contacts.vcf`, every card marked with its
account (`google:<email>`), so the contacts connector mirrors it with no
config: a contact deleted in Google is removed here, and a large removal is
held for your yes. When Google ends the sign-in, collect says *Google
Contacts needs connecting again: python -m lifecrm connect google_contacts*
(the kept client is reused; no new download). While Google Contacts feeds
the contacts connector, the daily job reads only it — a Mac's Contacts can
still be imported by hand.

### Last.fm — `lastfm` → the `lastfm` connector (your Spotify listening)

Spotify's own API returns only the last 50 plays, and its full history is a
download you request by hand. Scrobbling is the automatic route: Spotify
reports every play to Last.fm, and Last.fm's API hands it back.

1. last.fm → Settings › Applications › *Spotify Scrobbling* › Connect; under
   Settings › Privacy leave *Hide recent listening information* unticked.
2. Create a free API account at <https://www.last.fm/api/account/create> and
   copy its **API key** (not the shared secret).
3. `python -m lifecrm connect lastfm <your username>` — the key is asked
   for, hidden.

Each morning the new scrobbles, plus a newest-first batch of the history
(10,000 a morning, cut at day boundaries, until the first scrobble), are
fetched as Last.fm's own pages into `data\fetched\lastfm\pending\`; the
`lastfm` connector reads them into `music_play`, one record per track per
day — plays, but no minutes (Last.fm never says how long a track played).
Pages move to `archive\` once imported. Keep one source per stretch of time:
Last.fm from now on, a Spotify download only for the years before scrobbling
(both for the same days count each track-day twice). [docs/lastfm.md](lastfm.md).

### Gmail — `gmail` → the `email` connector

1. Turn on 2-Step Verification for the Google account.
2. Make an app password at <https://myaccount.google.com/apppasswords>.
3. `python -m lifecrm connect gmail you@gmail.com` and paste the 16 letters
   when asked (hidden; spaces are fine).

Each morning, over TLS to imap.gmail.com and strictly read-only (the mailbox
is examined, never selected; nothing is marked read, moved or flagged): all
new mail, and — once you said yes to the first import — up to 2,000 older
messages (250 MB) a morning, newest first, until the oldest. Mail lands as
`.eml` files in `data\fetched\gmail\pending\`, Gmail's own copy with its
labels written the way Takeout writes them (so *Sent* reads as yours); a
message over 1 MB comes without its attachments' bytes (their names are
kept). Imported files move to `archive\`. Spam and Trash are not in All Mail
and are not fetched. Send-as aliases: list them in `[connectors.email]
addresses`. [docs/gmail.md](gmail.md).

Bank and card alerts arrive as ordinary mail here, in full text — the record
a later step can read transactions from.

### GPS location — not a service, a phone folder

Location needs no connecting: GPSLogger on the phone writes one track file
a day, and Autosync uploads it to OneDrive › GPSLogger; the `gps_track`
connector finds that folder by itself and turns the points into visits.
Phone setup: [docs/gps_track.md](gps_track.md).

## What is not online, and why

- **Texts and calls** stay on the phone: SMS Backup & Restore's daily
  backup to OneDrive (Android), Messages in iCloud on a Mac, an Apple
  Devices backup on Windows + iPhone.
- **Health**: Health Connect's scheduled export (Android); Apple Health has
  no scheduled export at all.
- **Spotify**: its API returns only the last 50 plays, so listening comes
  through Last.fm scrobbling instead.
- **Bank and card transactions**: wallet apps have no export and banks no
  personal API here; the alert and receipt emails Gmail brings in are the
  automatic record (parsing them into transactions is a later step).
- **Instagram, WhatsApp, LinkedIn, Takeout-only data**: no personal API —
  request the export by hand; the morning after you download it into the
  place its connector looks, it is a question.
