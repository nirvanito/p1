"""Google Contacts through the People API — the address book as Google holds
it, fetched each morning as one vCard the contacts connector reads
(docs/google_contacts.md).

Google has no way into contacts without OAuth (its CardDAV server refuses
passwords and app passwords alike), so the owner signs in once in their own
browser (`_google_oauth`) and the refresh token is kept in data/secrets.json.
Each morning it buys an hour's access token, and every contact in "Contacts"
(people.connections.list, what the owner keeps — not "Other contacts", not
the other person's public profile) is written to
`data/fetched/google_contacts/contacts.vcf`: the whole address book every
time, so nothing needs settling.

The vCard is written for the contacts connector and reads like Google's own
export, with three additions that make it better than one:

- UID is the contact's resourceName (people/c…): a renamed card is the same
  card, where an export's cards (no UID) are keyed by name;
- REV is the contact's last change, so a card is dated by its edit, not by
  the morning it was fetched;
- X-LIFECRM-ACCOUNT names whose address book it is (google:<email>), which
  is what lets the shared mirroring remove a contact deleted in Google — and
  only that account's — with no config.

A phone number is written in Google's own E.164 form when it has one: Google
completed it with the account's country, which a national-format number on
its own cannot be (connectors/base.normalize_phone), so texts and calls from
that number find the card. The file is rewritten only when its content
changes; an unchanged address book is an unchanged import.
"""

from __future__ import annotations

import itertools
import sys
import urllib.parse
from pathlib import Path

from ..config import Config
from ..connectors import _vcard
from ..domain.util import DomainError
from . import _google_oauth as oauth
from . import secrets as kept

SERVICE = "google_contacts"
CONNECTOR = "contacts"
TITLE = "Google Contacts"
DOC = "docs/google_contacts.md"
API = "People API"

CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
# openid + email: which account signed in (the ID token's email), and the
# account the cards are mirrored under. Both are basic sign-in scopes.
SCOPES = ["openid", "email", CONTACTS_SCOPE]
PEOPLE_URL = "https://people.googleapis.com/v1/people/me/connections"
GROUPS_URL = "https://people.googleapis.com/v1/contactGroups"
PERSON_FIELDS = ("names,nicknames,emailAddresses,phoneNumbers,birthdays,organizations,"
                 "addresses,urls,biographies,memberships,metadata")
SOURCES = "READ_SOURCE_TYPE_CONTACT"
PAGE_SIZE = 1000
FILE = "contacts.vcf"
PRODID = "-//Life CRM//Google Contacts via the People API//EN"

_PHONE_TYPES = {"mobile": "CELL", "home": "HOME", "work": "WORK", "main": "MAIN",
                "pager": "PAGER", "homeFax": "HOME,FAX", "workFax": "WORK,FAX",
                "otherFax": "FAX", "other": "OTHER"}
_PLAIN_TYPES = {"home": "HOME", "work": "WORK", "other": "OTHER"}


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)     # stdout stays the command's own


def steps() -> list[str]:
    return [
        "On console.cloud.google.com, signed in as the account whose contacts these "
        "are: the project menu › New project (any name, e.g. Life CRM).",
        "APIs & Services › Library › search 'People API' › Enable.",
        "Google Auth Platform › Get started: app name Life CRM, your address as "
        "support and contact email, Audience: External, agree, Create.",
        "Audience › Test users › Add users › your address; then Audience › Publish "
        "app › Confirm, so it reads 'In production' — in Testing Google ends the "
        "sign-in after 7 days. Unverified is expected for an app only you use.",
        "Data Access › Add or remove scopes › tick .../auth/contacts.readonly (See and "
        "download your contacts) › Update › Save.",
        "Clients › Create client › Application type: Desktop app › Create, then "
        "Download JSON right away — Google shows the client secret only on that screen.",
        "Here: python -m lifecrm connect google_contacts \"<the downloaded .json>\" — "
        "the browser opens; on 'Google hasn't verified this app' choose Advanced › Go "
        "to Life CRM (unsafe), and allow 'See and download your contacts'.",
        "The next morning collect fetches it; /morning asks before the first import "
        "(or now: python -m lifecrm collect approve contacts).",
    ]


# ---------------------------------------------------------------- connect

def connect(cfg: Config, args: list[str], ask) -> dict:
    """Sign in once. The client JSON is args[0], or asked for; connecting
    again with no file reuses the client kept from last time (Google shows
    a client secret only once, when it is made)."""
    before = kept.load(cfg).get(SERVICE) or {}
    if args:
        client = oauth.load_client(args[0], service=SERVICE)
    elif before.get("client_id"):
        client = {"client_id": before["client_id"],
                  "client_secret": before.get("client_secret") or ""}
        _say("using the OAuth client kept from the last connect")
    elif ask is not None:
        client = oauth.load_client(ask("Path to the OAuth client JSON you downloaded "
                                       "(Desktop app)", False), service=SERVICE)
    else:
        raise DomainError(f"pass the client JSON: python -m lifecrm connect {SERVICE} "
                          f"<client_secret_….json> (docs/{SERVICE}.md)")
    try:
        return _sign_in(client)
    except OSError as e:                      # offline, DNS, a refused connection
        raise DomainError(f"could not reach Google ({e}) — nothing connected; check "
                          f"the connection and run: python -m lifecrm connect "
                          f"{SERVICE}") from e


def _sign_in(client: dict) -> dict:
    tokens = oauth.authorize(client, SCOPES, title=TITLE, service=SERVICE, say=_say)
    if CONTACTS_SCOPE not in oauth.granted(tokens, SCOPES):
        raise DomainError(f"{TITLE} not connected — 'See and download your contacts' "
                          f"was left unticked on Google's page; run connect again and "
                          f"allow it")
    email = str(oauth.id_claims(tokens.get("id_token")).get("email") or "").strip().lower()
    if not email:
        raise DomainError(f"{TITLE} not connected — Google did not say which account "
                          f"signed in (no email in its answer); run connect again")
    _first_page(tokens["access_token"])        # a missing step is said now, not at 06:30
    note = oauth.lifetime_note(tokens, service=SERVICE)
    if note:
        _say(note)
    account = f"google:{email}"
    _say(f"signed in as {email} — its contacts are kept as account {account}")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": tokens["refresh_token"], "account": account}


# ---------------------------------------------------------------- fetch

def _get(url: str, params: dict, access: str) -> dict:
    return oauth.get_json(url + "?" + urllib.parse.urlencode(params), access,
                          title=TITLE, service=SERVICE, api=API)


def _first_page(access: str) -> dict:
    return _get(PEOPLE_URL, {"pageSize": 1, "personFields": "names",
                             "sources": SOURCES}, access)


def _all(url: str, params: dict, key: str, access: str) -> list[dict]:
    """Every page; the parameters stay the same on each, as the API asks."""
    out, token, seen = [], None, set()
    while True:
        data = _get(url, {**params, **({"pageToken": token} if token else {})}, access)
        out += [x for x in data.get(key) or [] if isinstance(x, dict)]
        token = data.get("nextPageToken")
        if not token:
            return out
        if token in seen:
            raise DomainError(f"{TITLE} sent the same page twice — nothing fetched; the "
                              f"next run tries again")
        seen.add(token)


def fetch(cfg: Config, secret: dict, dest: Path) -> Path:
    client_id, refresh_token, account = (secret.get(k) for k in
                                         ("client_id", "refresh_token", "account"))
    if not (client_id and refresh_token and account):
        raise DomainError(oauth.reconnect(TITLE, SERVICE))
    tokens = oauth.refresh({"client_id": client_id,
                            "client_secret": secret.get("client_secret") or ""},
                           refresh_token, title=TITLE, service=SERVICE)
    if tokens.get("refresh_token") and tokens["refresh_token"] != refresh_token:
        kept.update(cfg, SERVICE, refresh_token=tokens["refresh_token"])   # rotated
    access = tokens["access_token"]
    people = _all(PEOPLE_URL, {"pageSize": PAGE_SIZE, "personFields": PERSON_FIELDS,
                               "sources": SOURCES}, "connections", access)
    if not people:
        # A vCard of no cards cannot be written, and an empty answer is more
        # likely the wrong account than every contact deleted.
        raise DomainError(f"{TITLE} has no contacts for {account} — nothing fetched; "
                          f"if that is the wrong account: python -m lifecrm connect "
                          f"{SERVICE}")
    groups = {g.get("resourceName"): g.get("name") or g.get("formattedName")
              for g in _all(GROUPS_URL, {"pageSize": PAGE_SIZE,
                                         "groupFields": "name,groupType"},
                            "contactGroups", access)}
    out = dest / FILE
    _write(out, vcard(people, groups, account))
    for old in dest.glob("*.vcf"):          # the folder holds this one file only
        if old.name != FILE:
            old.unlink()
    return out


def _write(out: Path, data: bytes) -> None:
    """Atomically, and only when something changed: an unchanged file keeps
    its date, which dates any card without a REV of its own."""
    try:
        if out.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(out)


# ---------------------------------------------------------------- the vCard

def _s(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;"))


def _fold(line: str) -> str:
    """75 octets a line, continued after one space, never inside a character."""
    parts, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            parts.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    parts.append(cur)
    return "\r\n".join(parts) + "\r\n"


def _primary(items) -> dict:
    items = [i for i in items or [] if isinstance(i, dict)]
    return next((i for i in items if (i.get("metadata") or {}).get("primary")),
                items[0] if items else {})


def _typed(prop: str, value: str, field: dict, known: dict, items, base=()) -> list[str]:
    """A property with Google's type: a vCard TYPE where one exists, else the
    label as Google shows it ('Work Mobile', a custom 'Climbing gym') in an
    X-ABLabel group, as Google's own export writes custom labels."""
    kind = _s(field.get("type"))
    if not kind or kind in known:
        types = [*base, *([known[kind]] if kind else [])]
        return [prop + "".join(f";TYPE={t}" for t in types) + ":" + value]
    group = f"item{next(items)}"
    label = _s(field.get("formattedType")) or kind
    return [f"{group}.{prop}" + "".join(f";TYPE={t}" for t in base) + ":" + value,
            f"{group}.X-ABLabel:{_esc(label)}"]


def _birthday(person: dict) -> str | None:
    found = [b for b in person.get("birthdays") or [] if isinstance(b, dict)]
    dated = [b for b in found if (b.get("date") or {}).get("month")
             and (b.get("date") or {}).get("day")]
    pick = _primary(dated) if dated else _primary(found)
    if not pick:
        return None
    d = pick.get("date") or {}
    if d.get("month") and d.get("day"):
        md = f"{int(d['month']):02d}-{int(d['day']):02d}"
        return f"BDAY:{int(d['year']):04d}-{md}" if d.get("year") else f"BDAY:--{md}"
    text = _s(pick.get("text"))
    return f"BDAY;VALUE=text:{_esc(text)}" if text else None


def _updated(person: dict) -> str | None:
    sources = [s for s in (person.get("metadata") or {}).get("sources") or []
               if isinstance(s, dict) and _s(s.get("updateTime"))]
    pick = next((s for s in sources if s.get("type") == "CONTACT"),
                sources[0] if sources else None)
    return _s(pick.get("updateTime")) if pick else None


def _card(person: dict, groups: dict, account: str) -> str:
    items = itertools.count(1)
    name = _primary(person.get("names"))
    org = _primary(person.get("organizations"))
    emails = [e for e in person.get("emailAddresses") or []
              if isinstance(e, dict) and _s(e.get("value"))]
    phones = [(p, _s(p.get("canonicalForm")) or _s(p.get("value")))
              for p in person.get("phoneNumbers") or [] if isinstance(p, dict)]
    phones = [(p, number) for p, number in phones if number]
    parts = [_s(name.get(k)) for k in ("familyName", "givenName", "middleName",
                                       "honorificPrefix", "honorificSuffix")]
    fn = (_s(name.get("displayName")) or _s(name.get("unstructuredName"))
          or " ".join(x for x in (parts[1], parts[2], parts[0]) if x)
          or _s(org.get("name")) or (_s(emails[0]["value"]) if emails else "")
          or (phones[0][1] if phones else ""))
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"PRODID:{PRODID}", f"FN:{_esc(fn)}",
             "N:" + ";".join(_esc(p) for p in parts)]
    nicks = [_s(n.get("value")) for n in person.get("nicknames") or []
             if isinstance(n, dict) and _s(n.get("value"))]
    if nicks:
        lines.append("NICKNAME:" + ",".join(_esc(n) for n in nicks))
    if _s(org.get("name")) or _s(org.get("department")):
        lines.append("ORG:" + _esc(_s(org.get("name")))
                     + (";" + _esc(_s(org.get("department"))) if _s(org.get("department"))
                        else ""))
    if _s(org.get("title")):
        lines.append(f"TITLE:{_esc(_s(org['title']))}")
    for e in emails:
        lines += _typed("EMAIL", _esc(_s(e["value"])), e, _PLAIN_TYPES, items,
                        base=("INTERNET",))
    for p, number in phones:
        lines += _typed("TEL", _esc(number), p, _PHONE_TYPES, items)
    for a in person.get("addresses") or []:
        if not isinstance(a, dict):
            continue
        fields = [_s(a.get(k)) for k in ("poBox", "extendedAddress", "streetAddress",
                                         "city", "region", "postalCode")]
        fields.append(_s(a.get("country")) or _s(a.get("countryCode")))
        if not any(fields):
            if not _s(a.get("formattedValue")):
                continue
            fields[2] = _s(a["formattedValue"])     # an address typed as one line
        lines += _typed("ADR", ";".join(_esc(f) for f in fields), a, _PLAIN_TYPES, items)
    for u in person.get("urls") or []:
        if isinstance(u, dict) and _s(u.get("value")):
            lines += _typed("URL", _esc(_s(u["value"])), u, _PLAIN_TYPES, items)
    bday = _birthday(person)
    if bday:
        lines.append(bday)
    bio = _s(_primary(person.get("biographies")).get("value"))
    if bio:
        lines.append(f"NOTE:{_esc(bio)}")
    member = [(m.get("contactGroupMembership") or {}).get("contactGroupResourceName")
              for m in person.get("memberships") or [] if isinstance(m, dict)]
    labels = [groups.get(r) or str(r).rpartition("/")[2] for r in member if r]
    if labels:
        lines.append("CATEGORIES:" + ",".join(_esc(x) for x in labels))
    if _s(person.get("resourceName")):
        lines.append(f"UID:{_esc(_s(person['resourceName']))}")
    rev = _updated(person)
    if rev:
        lines.append(f"REV:{rev}")
    lines += [f"{_vcard.ACCOUNT_PROP}:{_esc(account)}", "END:VCARD"]
    return "".join(_fold(line) for line in lines)


def vcard(people: list[dict], groups: dict, account: str) -> bytes:
    """Every contact as one vCard 3.0 file, in resourceName order, so the
    same address book is the same bytes."""
    ordered = sorted(people, key=lambda p: _s(p.get("resourceName")))
    return "".join(_card(p, groups, account) for p in ordered).encode("utf-8")
