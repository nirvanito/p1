# Google Contacts (online source)

Connect Google Contacts once and the daily `collect` job fetches your whole
Google address book every morning. You never export a `.vcf` by hand again.
The fetched cards go through the same `contacts` connector as a hand-made
export ([docs/contacts.md](contacts.md)): one `contact_card` per contact,
every number and address a handle, observed and never asserted, and nothing
linked automatically. Birthdays on the cards feed /morning's birthday nudges
once a card's handle is linked to a person.

**Why it needs a Google sign-in.** Google offers no way into contacts
without OAuth. Its CardDAV server says "Google's CardDAV interface requires
OAuth 2.0" and that it supports no other method, and app passwords stopped
working for CardDAV in June 2023. So you sign in once, in your own browser,
on Google's own page. Life CRM never sees your password: it gets a
*refresh token* that can only read contacts, and keeps it in
`data/secrets.json`.

## One-time setup (about ten minutes)

The setup creates your own small Google Cloud "app" that only you use. Do
it signed in as the Google account whose contacts these are, at
[console.cloud.google.com](https://console.cloud.google.com).

1. **Create a project.** Open the project menu at the top, choose
   *New project*, and give it any name (e.g. `Life CRM`) → *Create*. Select
   it.
2. **Enable the People API.** *APIs & Services* → *Library* → search
   `People API` → *Enable*.
3. **Set up the consent screen.** *Google Auth Platform* → *Get started*
   (later it shows *Branding*, *Audience*, *Data Access* and *Clients*).
   - App information: app name `Life CRM`, your address as the user support
     email → *Next*;
   - Audience: **External** → *Next* (Internal needs a Workspace
     organization);
   - Contact information: your address → *Next*;
   - agree to the Google API Services User Data Policy → *Continue* →
     *Create*.
4. **Add yourself, then publish.** *Audience* → *Test users* → *Add users* →
   your address → *Save*. Then, on the same *Audience* page, **Publish app**
   → confirm, so the publishing status reads **In production**.
   *Do not skip publishing.* While an app with External users is in
   *Testing*, Google ends its sign-ins after **7 days** (that limit spares
   only apps that ask for nothing beyond name, email and profile), and the
   daily job would start saying "needs connecting again" a week later.
   Google will list the app as unverified. That is expected for an app only
   you use: verification is for apps offered to other people. An unverified
   app is capped at 100 users, and you are one.
5. **Declare the scope.** *Data Access* → *Add or remove scopes* → tick
   `.../auth/contacts.readonly` ("See and download your contacts") →
   *Update* → *Save*. (`openid` and `email` are basic sign-in scopes and need
   nothing here.)
6. **Create the client.** *Clients* → *Create client* → Application type
   **Desktop app** → any name → *Create*. In the dialog that follows,
   **Download JSON right away.** Google shows a client secret only once,
   when the client is created. Later the console shows its last four
   characters, and the fix is *rotating* the secret. The file is named like
   `client_secret_<numbers>-<letters>.apps.googleusercontent.com.json`.
7. **Connect.** In a terminal at the project root:

   ```
   .venv\Scripts\python.exe -m lifecrm connect google_contacts "%USERPROFILE%\Downloads\client_secret_….json"
   .venv/bin/python -m lifecrm connect google_contacts ~/Downloads/client_secret_….json
   ```

   Your browser opens Google's page (the terminal prints the link too, for
   when no browser opens; open it on *this* computer). Pick the account.
   Google then shows **"Google hasn't verified this app"**. Choose
   *Advanced* → *Go to Life CRM (unsafe)*: "unsafe" is Google's word for an
   app it has not reviewed, and this one is yours. Then allow **See and
   download your contacts**. If that box is left unticked, nothing is
   connected and the terminal says so. The tab then says *Signed in*. The
   terminal prints `signed in as you@gmail.com` and
   `connected: Google Contacts`.

   Connecting reads one contact straight away, so a missed step (the People
   API not enabled, say) shows up now and not at 06:30.
8. **The first import is yours to approve.** The next morning `collect`
   fetches the address book and *holds* it as a new source: "new on this
   computer: contacts for google:you@gmail.com, N record(s)…". /morning asks
   about it. Or preview it and say yes now:

   ```
   .venv\Scripts\python.exe -m lifecrm collect approve contacts
   ```

   Then link the people: `lifecrm handles list --suggest` (docs/contacts.md).

You can keep the downloaded JSON or delete it. Its contents are kept in
`data/secrets.json`, and connecting again does not need it.

## What is fetched

Every morning, with one fresh access token (an hour long; never stored):

- **Contacts.** Every contact in *Contacts* (People API
  `people.connections.list`, 1,000 per page, every page) with the fields
  `names, nicknames, emailAddresses, phoneNumbers, birthdays, organizations,
  addresses, urls, biographies, memberships, metadata`. Only what you
  keep on the contact is read (`READ_SOURCE_TYPE_CONTACT`), not the other
  person's own public Google profile. That data is not yours to archive, and
  it changes without you.
- **Contact labels** (`contactGroups.list`): the names of your labels, so a
  card's memberships are written as Google's export writes them
  (`CATEGORIES:myContacts,Climbing`). The connector does not import groups
  yet. They stay in the file.

Not fetched: *Other contacts* (the addresses Gmail saves by itself; a
separate scope that is not asked for), your organization's directory,
photos, and the People API fields not listed (events such as anniversaries,
relations, chat handles, custom fields).

The result is **one file**, `data/fetched/google_contacts/contacts.vcf`, in
vCard 3.0. It is written atomically and holds the whole address book each
time. It is rewritten only when something in it changed, so an unchanged
address book is an unchanged import. It reads like Google's own export,
with additions that make it better than one:

| on the card | from Google | so that |
|---|---|---|
| `UID:people/c…` | the contact's resourceName | a renamed card is the same card (an export's cards have no id and are keyed by name) |
| `REV:` | the contact's last update time | the card is dated by your last edit, not by the morning it was fetched |
| `X-LIFECRM-ACCOUNT:google:<email>` | the account that signed in | deletions mirror per account with no config (below) |
| `TEL:+15125550142` | Google's E.164 form of the number, where it has one | a number typed in national format (`020 7946 0958`) still matches the texts and calls from it, since Google completed it with your account's country |
| `item1.X-ABLabel:Work cell` | a custom or Google-only label ("Work Mobile", "Google Voice") | the label reads as you see it in Google Contacts |

Names, nicknames, the first (primary) organization and its title, emails,
addresses (a one-line address stays one line), web sites, the birthday
(with or without a year: `BDAY:--02-29`) and the note (*biography*) map as
in [docs/contacts.md](contacts.md#what-a-card-becomes).

## Every morning

The daily job (`collect`, 06:30) fetches, then decides as it does for any
source:

- **unchanged:** nothing differs from what is imported;
- **applied:** a new contact is added, an edited one updated (newer wins),
  and a contact **you deleted in Google is removed here**. The address book
  is mirrored, scoped to `google:<email>`, so it can never remove another
  account's cards, a Mac's or an iPhone backup's. A snapshot is taken
  before anything is removed;
- **held:** a removal of half the address book or more, or five or more
  cards that are over a tenth of it, at once. That looks more like a
  wrong account or a broken answer than deletions. `collect approve
  contacts` shows what would go and removes it on your yes;
- **error:** Google could not be reached ("nothing fetched; the next run
  tries again"), or the sign-in ended (below). An answer with no contacts
  at all is also an error, never "every card deleted".

`python -m lifecrm fetch google_contacts` downloads now without importing.
`python -m lifecrm observe contacts` (no path) fetches and shows the dry
run.

While Google Contacts is connected, it *is* the daily job's contacts
source. A Mac's Contacts folder or a `.vcf` in Downloads is then not
collected by the job, but can still be imported by hand
(`observe contacts <path> --apply`). Use one route per address book anyway:
the same person from two routes shows up twice.

## When the sign-in ends: connecting again

A refresh token stops working when:

- the app is still in **Testing** (7 days; publish it, step 4). If Google
  says so when you connect, the terminal tells you then;
- you remove the app's access in your Google Account (*Security* → *Your
  connections to third-party apps & services*,
  [myaccount.google.com/connections](https://myaccount.google.com/connections));
- it goes unused for six months, or you chose time-limited access on the
  consent page;
- you connect more than 100 times with one client. Each connect makes a
  new token, and the oldest is dropped.

Collect then records one line, and /morning shows it as a failed daily
import:

```
Google Contacts needs connecting again: python -m lifecrm connect google_contacts
```

Run exactly that. With no file it reuses the client kept from last time,
opens the browser, and replaces the token. If Google says the **client**
itself is gone (`invalid_client`: deleted, or its secret rotated), make or
rotate a Desktop app client (step 6) and pass its new JSON. Google deletes
OAuth clients that are unused for six months. The daily fetch keeps yours
in use.

Signing in as a **different Google account** keeps that account's cards
apart (`google:<other email>`). Its first import is asked about, and the
first account's cards stay as they are. Retract them with
`lifecrm imports retract <uuid>` if they should go.

## Disconnecting

```
.venv\Scripts\python.exe -m lifecrm disconnect google_contacts
```

This forgets the client and the token on this computer. The fetched file
stays in `data/fetched/google_contacts/`, and what was imported stays
imported. To withdraw the access on Google's side too, remove *Life CRM* at
[myaccount.google.com/connections](https://myaccount.google.com/connections).
You can also delete the project in the Cloud console.

## Moving from a hand-made export

- If you imported Google's `.vcf` export with `[connectors.contacts] account
  = "google:you@gmail.com"` (the label docs/contacts.md suggests), it is the
  same account. The first fetch replaces those name-keyed cards with
  id-keyed ones, which is held as a large removal. One `collect approve
  contacts` does it.
- If no account was set, the hand-imported cards are not scoped to any
  account and nothing removes them. Retract that import
  (`lifecrm imports list`, then `imports retract <uuid>`) to avoid each
  person showing twice.

## What is kept, and where

- `data/secrets.json` (private, never committed): `client_id`,
  `client_secret`, `refresh_token`, `account`. These are never in
  config.toml and never printed. Commands say *that* Google Contacts is
  connected, never with what. Google treats a Desktop app's client secret as
  not truly secret, but it is kept the same way.
- The scope is read-only (`contacts.readonly`), plus `openid email` to learn
  which account signed in. Life CRM cannot change your contacts.
- Network: `oauth2.googleapis.com` and `people.googleapis.com`, once each
  morning, about three requests for an address book under a thousand
  contacts. Signing in also uses a listener on `127.0.0.1` at a random
  port, for the one redirect, while `connect` runs.

## Not verified yet

- **Tested only against a faked Google.** The tests cover the sign-in
  (loopback, state, PKCE), refresh, paging, the vCard, mirroring and
  collect, but no real account has been used. The first real `connect` and
  `collect approve contacts` preview are the check: compare the card count
  with contacts.google.com.
- The console wording (*Google Auth Platform* → *Branding / Audience / Data
  Access / Clients*, *Publish app*) is from Google's documentation as of
  October 2026. The unverified-app screen's *Advanced* → *Go to … (unsafe)*
  is from user reports.
- Publishing an unverified app that asks for `contacts.readonly` (a
  sensitive scope) is believed to be allowed for your own use, with the
  warning screen and the 100-user cap. Google could ask for verification
  instead. If it does, keep the app in Testing and reconnect weekly.
- Whether a Testing app's token answer carries `refresh_token_expires_in`.
  The 7-day warning at connect relies on it. Without it, the limit shows up
  as the "needs connecting again" line a week later.
- How Google fills `canonicalForm` for numbers with an extension, and
  whether every contact carries an update time. A card without one is dated
  by the file, which is rewritten only on a change.
