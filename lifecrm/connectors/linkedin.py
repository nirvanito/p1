"""LinkedIn connector: the "Get a copy of your data" export — your
connections, and your messages.

Who you know professionally, where they work now and what they wrote to you
is the part of a life the address book does not hold. LinkedIn has no API
for it that an individual can use, but it will hand the whole thing over:
Settings & Privacy → Data privacy → Get a copy of your data. This connector
reads that export. It never logs in, never scrapes, and never sends anything.

Accepted targets
----------------
- the export .zip ('Basic_LinkedInDataExport_<MM-DD-YYYY>.zip', or the
  'Complete_…' one that follows a day later), or the folder it unzips to
- one of its CSVs on its own (Connections.csv or messages.csv), recognised
  by its header when it has been renamed
- nothing at all: [connectors.linkedin] path, else the newest
  *LinkedInDataExport* zip or folder in ~/Downloads

Of the export's dozens of files four are read: Connections.csv and
messages.csv are imported; Invitations.csv and Profile.csv are read only to
tell which profile is yours. Everything else (Positions, Skills, Reactions,
ad targeting, ...) is counted by name in the report and left alone.

What comes in
-------------
Two streams.

`linkedin_connection`, one per connection — the row Connections.csv holds:
  title       the connection's name as LinkedIn shows it
  body        "Position at Company" — what search should find
  started_at  the Connected On date, at local noon (the file has a date, no
              time; noon survives every rollover and zone shift — finance's
              rule), so journal_date is the day you connected
  value_json  {company, position, url, email} as the file holds them
  participants  app_account 'linkedin:<profile path>' and, when the person
              lets connections see it, their email — the handle that joins
              them to texts, calendar invites and contact cards once linked

`linkedin_message`, one per message in messages.csv:
  title       the conversation's title, else its subject, else the names of
              the people in it besides you (a one-to-one thread is named
              after the other person, as WhatsApp's export is)
  body        the content, HTML taken off (LinkedIn stores rich messages as
              HTML: <p>, <br>, links, &amp;)
  direction   'out' when the sender is your profile, else 'in'
  value_json  {conversation_id, folder, subject (when not the title), group,
              attachments}
  participants  the sender ('sender') and every recipient ('recipient'), by
              profile path

Handles, not names
------------------
A person is their profile URL's path, lower-cased and URL-decoded:
https://www.linkedin.com/in/Priya-Natarajan-Acme becomes app_account
'linkedin:in/priya-natarajan-acme'; a company page stays 'company/<slug>'.
The name is only the handle's display_name, which is what `handles list
--suggest` matches against your entities. A sender with no profile URL
("LinkedIn Member", a deleted account) has nothing stable to name them by:
the message is kept, its sender is not attributed, and the report counts it.

Which profile is yours: [connectors.linkedin] profile_url, else Invitations
(the inviter of an OUTGOING invitation, the invitee of an INCOMING one),
else the message sender whose name is Profile.csv's (or [owner] name), else
the one profile in every message. The report says which rule decided — and
when none did, direction stays unknown and nothing is mirrored.

Identity
--------
- a connection: its profile path ('linkedin:in/<slug>'), so a re-export is a
  no-op and a new job is an update of the same row. A row with no URL is
  keyed on the name and Connected On date instead.
- a message: messages.csv has no message ids, so one is derived — 'li:' +
  SHA-1 of the conversation id, the instant, the sender, the raw content and
  an ordinal among messages identical in all four.

Connections carry the export's own time as their last_modified_at (the
newest member of the zip, or the files' modification time): that is when
LinkedIn last said what the row holds, so a later export's "VP Product"
replaces an earlier "Director of Product" as an update instead of standing
as a conflict forever. A message's last_modified_at is its sent time: a
message does not change.

Deletion semantics — connections mirror, messages remember
------------------------------------------------------------
Connections.csv is the complete current list, so a connection you removed
(or who removed you) is removed here by the next import: `present_ids`
holds every connection the file lists, `mirror_streams` is
['linkedin_connection'] only, and the account that scopes it is your own
profile handle. Removal goes through the shared write layer — snapshot
first, the mass-removal guard, cited rows hidden rather than deleted. An
export whose owner cannot be told removes nothing, and says so.

Messages are remembered. LinkedIn's export holds what the inbox holds, but
a conversation deleted on one side, a thread archived away, or a Basic
export cut short is indistinguishable from deletion — and a message is
evidence of something said, which the owner chose to keep (2026-09-24,
"remember" is the default when unsure). `imports retract` undoes a run.

Known limitations, by design
----------------------------
- Emails appear only for connections who allow it (LinkedIn's own note,
  the preamble of Connections.csv, says so). Most rows have none.
- Some exports write a profile as its member id (/in/ACoAAB…) in
  messages.csv and as its vanity URL in Connections.csv; the two do not
  meet as one handle. Link both to the same entity.
- A changed vanity URL is a new handle — and, for a connection, a removal
  plus an addition.
- Connected On is a date in English ('15 Mar 2021'); a localised export's
  month names are counted as unreadable, not guessed.
- Message times are UTC as written; journal dates use the home timezone.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from .. import owner as owner_mod
from .. import platforms
from ..config import Config, ConfigError
from ..domain.util import DomainError, canonical_json
from . import base

name = "linkedin"
streams = ("linkedin_connection", "linkedin_message")
# No stale_after_days: an export is made by hand now and then, and a quiet
# LinkedIn is not a broken feed.

CONNECTIONS = "connections.csv"
MESSAGES = "messages.csv"
INVITATIONS = "invitations.csv"
PROFILE = "profile.csv"
_READ = (CONNECTIONS, MESSAGES, INVITATIONS, PROFILE)
_HEADER_SCAN = 15          # rows searched for a header (Connections has a preamble)

_CONN_HEADER = ("first name", "last name", "connected on")
_MSG_HEADER = ("conversation id", "from", "date", "content")
_DRAFT = ("yes", "true", "1")


# ---------------------------------------------------------------- reading the export

def _norm(cell: str) -> str:
    return " ".join(str(cell).replace("﻿", "").strip().strip('"').lower().split())


def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts)


def _decode(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DomainError(f"{label} is not valid UTF-8: {e}") from e


def _rows(raw: bytes, label: str) -> list[list[str]]:
    try:
        return list(csv.reader(io.StringIO(_decode(raw, label), newline="")))
    except csv.Error as e:
        raise DomainError(f"{label}: not readable as CSV: {e}") from e


def _header(rows: list[list[str]], required: tuple[str, ...]):
    """(index of the first data row, {column: position}) or None."""
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        names = [_norm(c) for c in row]
        if set(required) <= set(names):
            index: dict[str, int] = {}
            for j, n in enumerate(names):
                index.setdefault(n, j)
            return i + 1, index
    return None


def _sniff(raw: bytes) -> str | None:
    """Which export file a renamed CSV is, by its header."""
    try:
        rows = _rows(raw[:65536], "file")
    except DomainError:
        return None
    if _header(rows, _CONN_HEADER):
        return CONNECTIONS
    if _header(rows, _MSG_HEADER):
        return MESSAGES
    return None


def _local_iso(dt: tuple, cfg: Config) -> str:
    """A zip member's naive local time as UTC ISO, in the home timezone."""
    naive = datetime(*dt)
    return naive.replace(tzinfo=ZoneInfo(cfg.timezone)).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _files(target: Path, cfg: Config) -> dict:
    """{'found': {file key: (shown name, bytes)}, 'others': [names], 'kind',
    'exported_at'} — the files this reader uses, the ones it leaves alone,
    and when the export was made (see the module docstring)."""
    found: dict[str, tuple[str, bytes]] = {}
    others: list[str] = []
    stamps: list[str] = []
    try:
        if target.is_dir():
            for p in sorted(target.rglob("*"), key=lambda q: q.as_posix().lower()):
                rel = p.relative_to(target).as_posix()
                if not p.is_file() or _junk(rel):
                    continue
                key = p.name.lower()
                if key in _READ and key not in found:
                    found[key] = (rel, p.read_bytes())
                    stamps.append(base.iso_utc(p.stat().st_mtime))
                else:
                    others.append(rel)
            kind = "folder"
        elif zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename.lower()):
                    if info.is_dir() or _junk(info.filename):
                        continue
                    key = PurePosixPath(info.filename).name.lower()
                    if key in _READ and key not in found:
                        found[key] = (info.filename, zf.read(info))
                        stamps.append(_local_iso(info.date_time, cfg))
                    else:
                        others.append(info.filename)
            kind = "zip"
        elif target.is_file():
            raw = target.read_bytes()
            key = target.name.lower()
            if key not in _READ:
                key = _sniff(raw)
                if key is None:
                    raise DomainError(
                        f"{target.name} is not a LinkedIn export — expected the "
                        f"export .zip or folder, Connections.csv or messages.csv "
                        f"(first line reads: {_first_line(raw)!r})")
            found[key] = (target.name, raw)
            stamps.append(base.iso_utc(target.stat().st_mtime))
            kind = "file"
        else:
            raise DomainError(f"not found: {target}")
    except (OSError, zipfile.BadZipFile) as e:
        why = getattr(e, "strerror", None) or e
        raise DomainError(f"cannot read {target}: {why}") from e
    if CONNECTIONS not in found and MESSAGES not in found:
        listing = ", ".join(others[:10]) or "(empty)"
        raise DomainError(f"{target.name} holds neither Connections.csv nor "
                          f"messages.csv — is this a LinkedIn data export? "
                          f"(has: {listing})")
    return {"found": found, "others": others, "kind": kind,
            "exported_at": max((s for s in stamps if s), default=None)}


def _first_line(raw: bytes) -> str:
    text = raw[:400].decode("utf-8-sig", errors="replace")
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:80]


def _source_hash(target: Path, found: dict) -> str:
    if target.is_file():
        return base.file_hash(target)
    h = hashlib.sha256()
    for key in sorted(found):
        shown, raw = found[key]
        h.update(f"{shown}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def default_target(cfg: Config) -> Path:
    """[connectors.linkedin] path, else the newest LinkedIn export in
    ~/Downloads — where the browser puts the zip LinkedIn emails a link to."""
    value = cfg.connector_settings(name).get("path")
    if value:
        p = cfg.resolve(value)
        if not p.exists():
            raise DomainError(f"[connectors.linkedin] path {p} does not exist — fix it "
                              f"in config.toml, or pass the export's path")
        return p
    downloads = platforms.home() / "Downloads"
    try:
        found = [p for p in downloads.iterdir()
                 if "linkedindataexport" in p.name.lower().replace("_", "")
                 and (p.is_dir() or zipfile.is_zipfile(p))] if downloads.is_dir() else []
    except PermissionError as e:
        raise DomainError(f"cannot list {downloads}: permission denied — on a Mac, "
                          f"allow the terminal to access Downloads (System Settings > "
                          f"Privacy & Security > Files and Folders)") from e
    if not found:
        raise DomainError(
            f"no LinkedIn export found in {downloads} (looked for "
            f"*LinkedInDataExport*.zip) — request one (docs/linkedin.md) and pass "
            f"its path, or set [connectors.linkedin] path")
    return max(found, key=lambda p: (p.stat().st_mtime, p.name))


# ---------------------------------------------------------------- profiles

def profile_path(raw) -> str | None:
    """A profile URL as its path: 'https://www.linkedin.com/in/Priya-N/' ->
    'in/priya-n'. A bare slug is taken as a member's ('priya-n' ->
    'in/priya-n'). Lower-cased (LinkedIn's vanity URLs are not case
    sensitive, and handles compare without case anyway) and URL-decoded, so
    the same person reads the same from every file."""
    s = str(raw or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = s.lstrip("/")
        if "linkedin.com" not in s.lower():
            if "/" not in s:
                s = "in/" + s
            s = "www.linkedin.com/" + s
        s = "https://" + s
    path = unquote(urlsplit(s).path).strip("/").lower()
    return path or None


def _handle(path: str) -> str:
    return f"linkedin:{path}"


def _split_urls(raw: str) -> list[str]:
    return [u for u in re.split(r"[\s,;]+", raw or "") if u.strip()]


# ---------------------------------------------------------------- text

class _Text(HTMLParser):
    # A list item starts its own line ('- '), so its end adds none.
    _BLOCK = {"p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


_LOOKS_HTML = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(\s[^<>]*)?/?>"
                         r"|&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def plain_text(content: str) -> str:
    """A message's content as plain text: tags off, entities decoded, one
    line per paragraph. Text that holds no markup is only trimmed — '<3' and
    'a < b' are what someone wrote."""
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    if _LOOKS_HTML.search(text):
        p = _Text()
        p.feed(text)
        p.close()
        text = "".join(p.parts)
    text = text.replace("\xa0", " ")
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


# ---------------------------------------------------------------- dates

_CONNECTED_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
                      "%m/%d/%Y", "%m/%d/%y")
_MSG_TIME = re.compile(
    r"(?P<d>\d{4}-\d{2}-\d{2})[ T](?P<t>\d{1,2}:\d{2}(?::\d{2})?)(?:\.\d+)?"
    r"\s*(?P<z>UTC|GMT|Z|[+-]\d{2}:?\d{2})?(?:\s*(?:UTC|GMT))?", re.I)


def connected_on(text: str) -> date | None:
    s = " ".join(str(text or "").split())
    for fmt in _CONNECTED_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def message_time(text: str) -> str | None:
    """'2026-10-02 15:04:12 UTC' -> '2026-10-02T15:04:12Z'. LinkedIn writes
    UTC; a value with an offset is honoured, one with none is read as UTC."""
    m = _MSG_TIME.fullmatch(" ".join(str(text or "").split()))
    if not m:
        return None
    zone = (m["z"] or "+00:00").upper()
    if zone in ("UTC", "GMT", "Z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    clock = m["t"] if m["t"].count(":") == 2 else m["t"] + ":00"
    if len(clock.split(":")[0]) == 1:
        clock = "0" + clock
    return base.iso_utc(f"{m['d']}T{clock}{zone}")


def _noon(d: date, cfg: Config) -> str:
    return datetime.combine(d, time(12), ZoneInfo(cfg.timezone)).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- connections

def _read_connections(shown: str, raw: bytes, cfg: Config, exported_at: str | None,
                      unmapped: Counter, examples: dict) -> dict:
    rows = _rows(raw, shown)
    found = _header(rows, _CONN_HEADER)
    if found is None:
        raise DomainError(f"{shown}: no 'First Name,Last Name,…,Connected On' header "
                          f"in its first {_HEADER_SCAN} rows (first line reads: "
                          f"{_first_line(raw)!r})")
    start, index = found
    preamble = sum(1 for r in rows[:start - 1] if any(c.strip() for c in r))

    def cell(row: list[str], col: str) -> str:
        j = index.get(col)
        return row[j].strip() if j is not None and j < len(row) else ""

    people, present = [], set()
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in row):
            continue
        first, last = cell(row, "first name"), cell(row, "last name")
        full = " ".join(f"{first} {last}".split())
        url = cell(row, "url")
        path = profile_path(url)
        when_text = cell(row, "connected on")
        when = connected_on(when_text)
        if path:
            ext = _handle(path)
        elif full:
            digest = hashlib.sha1(f"{full.casefold()}\x1f{when_text}".encode("utf-8"))
            ext = f"linkedin-name:{digest.hexdigest()[:20]}"
        else:
            unmapped["connection row with neither a name nor a profile URL"] += 1
            examples.setdefault("connection row with neither a name nor a profile URL",
                                f"{shown}:{lineno}")
            continue
        present.add(ext)              # in the file, whether imported or not
        if when is None:
            what = "connection whose Connected On date does not read"
            unmapped[what] += 1
            examples.setdefault(what, f"{shown}:{lineno} ({when_text!r})")
            continue
        if not path:
            what = "connection with no profile URL (no handle; keyed on name and date)"
            unmapped[what] += 1
            examples.setdefault(what, f"{shown}:{lineno}")
        email = cell(row, "email address")
        email_h = base.handle_for(email) if "@" in email else None
        company, position = cell(row, "company"), cell(row, "position")
        body = " at ".join(x for x in (position, company) if x) or None
        detail = {"company": company, "position": position, "url": url,
                  "email": email_h[1] if email_h else ""}
        people.append({"external_id": ext, "path": path, "name": full or None,
                       "email": email_h[1] if email_h else None, "date": when,
                       "body": body, "detail": {k: v for k, v in detail.items() if v}})
    records = []
    for p in people:
        participants = []
        if p["path"]:
            participants.append({"kind": "app_account", "value": _handle(p["path"]),
                                 "role": "participant"})
        if p["email"]:
            participants.append({"kind": "email", "value": p["email"],
                                 "role": "participant"})
        records.append({
            "stream": "linkedin_connection",
            "external_id": p["external_id"],
            "journal_date": p["date"].isoformat(),
            "started_at": _noon(p["date"], cfg),
            "ended_at": None,
            "utc_offset_sec": None,
            "direction": None,
            "title": p["name"],
            "body": p["body"],
            "value": None,
            "unit": None,
            "value_json": canonical_json(p["detail"]),
            "media_path": None,
            "origin_app": "linkedin",
            "last_modified_at": exported_at,
            "participants": participants,
        })
    return {"records": records, "people": people, "present": present,
            "rows": len(people), "preamble": preamble}


# ---------------------------------------------------------------- messages

def _read_messages(shown: str, raw: bytes, skipped: Counter, unmapped: Counter,
                   examples: dict) -> list[dict]:
    rows = _rows(raw, shown)
    found = _header(rows, _MSG_HEADER)
    if found is None:
        raise DomainError(f"{shown}: no 'CONVERSATION ID,…,FROM,…,DATE,…,CONTENT' "
                          f"header (first line reads: {_first_line(raw)!r})")
    start, index = found

    def cell(row: list[str], col: str, raw_text: bool = False) -> str:
        j = index.get(col)
        if j is None or j >= len(row):
            return ""
        return row[j] if raw_text else row[j].strip()

    out = []
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in row):
            continue
        if cell(row, "is message draft").lower() in _DRAFT:
            skipped["draft message (never sent)"] += 1
            continue
        folder = cell(row, "folder")
        if folder.upper() == "SPAM":
            skipped["message in the spam folder"] += 1
            continue
        when = message_time(cell(row, "date"))
        if when is None:
            what = "message whose DATE does not read"
            unmapped[what] += 1
            examples.setdefault(what, f"{shown}:{lineno} ({cell(row, 'date')!r})")
            continue
        content = cell(row, "content", raw_text=True)
        body = plain_text(content)
        attachments = _split_urls(cell(row, "attachments"))
        if not body and not attachments:
            skipped["message with no text"] += 1
            continue
        urls = _split_urls(cell(row, "recipient profile urls"))
        to_names = [n.strip() for n in cell(row, "to").split(",") if n.strip()]
        if len(to_names) != len(urls):
            to_names = [cell(row, "to")] if len(urls) == 1 and cell(row, "to") else \
                [None] * len(urls)
        recipients = []
        for u, n in zip(urls, to_names):
            p = profile_path(u)
            if p:
                recipients.append((p, n))
        out.append({
            "line": lineno, "conversation": cell(row, "conversation id"),
            "conv_title": cell(row, "conversation title"),
            "subject": cell(row, "subject"), "folder": folder, "when": when,
            "sender": profile_path(cell(row, "sender profile url")),
            "sender_name": cell(row, "from") or None,
            "recipients": recipients, "content": content.strip(), "body": body or None,
            "attachments": len(attachments)})
    return out


# ---------------------------------------------------------------- whose export

def _setting_profile(cfg: Config) -> str | None:
    value = cfg.connector_settings(name).get("profile_url")
    if value is None or value == "":
        return None
    path = profile_path(value) if isinstance(value, str) else None
    if not path:
        raise DomainError(f"[connectors.linkedin] profile_url must be your profile's "
                          f"URL (https://www.linkedin.com/in/<you>) — got {value!r}")
    return path


def _owner_name(cfg: Config) -> str | None:
    try:
        return owner_mod.configured(cfg)[0]
    except ConfigError as e:
        raise DomainError(str(e)) from e


def _profile_name(found: dict) -> str | None:
    if PROFILE not in found:
        return None
    shown, raw = found[PROFILE]
    rows = _rows(raw, shown)
    head = _header(rows, ("first name", "last name"))
    if head is None:
        return None
    start, index = head
    for row in rows[start:]:
        first = row[index["first name"]].strip() if index["first name"] < len(row) else ""
        last = row[index["last name"]].strip() if index["last name"] < len(row) else ""
        full = " ".join(f"{first} {last}".split())
        if full:
            return full
    return None


def _invitation_owner(found: dict) -> str | None:
    """You are the inviter of every OUTGOING invitation and the invitee of
    every INCOMING one."""
    if INVITATIONS not in found:
        return None
    shown, raw = found[INVITATIONS]
    rows = _rows(raw, shown)
    head = _header(rows, ("direction",))
    if head is None:
        return None
    start, index = head
    votes: Counter = Counter()
    for row in rows[start:]:

        def cell(col: str) -> str:
            j = index.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        direction = cell("direction").upper()
        url = cell("inviterprofileurl") if direction == "OUTGOING" else \
            cell("inviteeprofileurl") if direction == "INCOMING" else ""
        path = profile_path(url)
        if path:
            votes[path] += 1
    if not votes:
        return None
    return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _owner(cfg: Config, found: dict, messages: list[dict]) -> tuple[str | None, str | None,
                                                                    str | None]:
    """(your profile path, which rule said so, your name) — see the module
    docstring for the order."""
    me_name = _profile_name(found)
    path = _setting_profile(cfg)
    if path:
        return path, "[connectors.linkedin] profile_url", me_name
    path = _invitation_owner(found)
    if path:
        return path, "Invitations.csv (the profile on your side of every invitation)", \
            me_name
    for label, who in (("Profile.csv", me_name), ("[owner] name", _owner_name(cfg))):
        if not who:
            continue
        hits = Counter(m["sender"] for m in messages if m["sender"]
                       and (m["sender_name"] or "").casefold() == who.casefold())
        if len(hits) == 1:
            return next(iter(hits)), f"the message sender named as {label} says you are", \
                me_name or who
    convs: dict[str, set] = {}
    for m in messages:
        people = {m["sender"]} | {p for p, _ in m["recipients"]}
        convs.setdefault(m["conversation"], set()).update(p for p in people if p)
    if len(convs) >= 2:
        common = set.intersection(*convs.values())
        if len(common) == 1:
            return next(iter(common)), "the one profile in every conversation", me_name
    return None, None, me_name


# ---------------------------------------------------------------- scan

def _message_id(m: dict, ordinal: int) -> str:
    raw = "\x1f".join((m["conversation"], m["when"], m["sender"] or m["sender_name"] or "",
                       m["content"], str(ordinal)))
    return "li:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def scan(target: Path, cfg: Config) -> base.Scan:
    files = _files(target, cfg)
    found = files["found"]
    skipped: Counter = Counter()
    unmapped: Counter = Counter()
    examples: dict[str, str] = {}

    conns = None
    if CONNECTIONS in found:
        conns = _read_connections(*found[CONNECTIONS], cfg, files["exported_at"],
                                  unmapped, examples)
    messages = _read_messages(*found[MESSAGES], skipped, unmapped, examples) \
        if MESSAGES in found else []
    me, me_from, me_name = _owner(cfg, found, messages)

    # One spelling per handle: a connection's own name first (it is what
    # LinkedIn shows today), then what a message called them.
    handles: dict[tuple[str, str], dict] = {}

    def seen(kind: str, value: str, display: str | None) -> dict:
        key = (kind, value.casefold())
        if key not in handles:
            handles[key] = {"kind": kind, "value": value, "display_name": display or None}
        elif not handles[key]["display_name"] and display:
            handles[key]["display_name"] = display
        return {"kind": kind, "value": handles[key]["value"]}

    if me:
        seen("app_account", _handle(me), me_name or "you (LinkedIn)")
    records = []
    if conns:
        for p in conns["people"]:
            if p["path"]:
                seen("app_account", _handle(p["path"]), p["name"])
            if p["email"]:
                seen("email", p["email"], p["name"])
        records += conns["records"]

    # ---- messages, conversation by conversation
    names: dict[str, str] = {}
    for m in messages:
        if m["sender"] and m["sender_name"]:
            names.setdefault(m["sender"], m["sender_name"])
        for p, n in m["recipients"]:
            if n:
                names.setdefault(p, n)
    for h in handles.values():
        if h["kind"] == "app_account" and h["display_name"]:
            names[h["value"][len("linkedin:"):]] = h["display_name"]
    by_conv: dict[str, list[dict]] = {}
    for m in messages:
        by_conv.setdefault(m["conversation"], []).append(m)
    unattributed = 0
    conv_facts = []
    for conv_id in sorted(by_conv):
        msgs = sorted(by_conv[conv_id], key=lambda m: (m["when"], m["line"]))
        people: list[str] = []
        for m in msgs:
            for p in [m["sender"], *(p for p, _ in m["recipients"])]:
                if p and p not in people:
                    people.append(p)
        others = [p for p in people if p != me]
        title = next((m["conv_title"] for m in msgs if m["conv_title"]), "") or \
            next((m["subject"] for m in msgs if m["subject"]), "") or \
            ", ".join(names.get(p) or p for p in others) or "(LinkedIn message)"
        group = len(people) > 2
        ordinals: Counter = Counter()
        for m in msgs:
            key = (m["when"], m["sender"] or m["sender_name"] or "", m["content"])
            ordinal = ordinals[key]
            ordinals[key] += 1
            participants = []
            if m["sender"]:
                participants.append({**seen("app_account", _handle(m["sender"]),
                                            names.get(m["sender"])), "role": "sender"})
            else:
                unattributed += 1
            for p, _n in m["recipients"]:
                if p == m["sender"]:
                    continue
                participants.append({**seen("app_account", _handle(p), names.get(p)),
                                     "role": "recipient"})
            detail = {"conversation_id": m["conversation"], "folder": m["folder"]}
            if m["subject"] and m["subject"] != title:
                detail["subject"] = m["subject"]
            if group:
                detail["group"] = True
            if m["attachments"]:
                detail["attachments"] = m["attachments"]
            if not m["sender"] and m["sender_name"]:
                detail["sender_shown_as"] = m["sender_name"]
            direction = None
            if me and m["sender"]:
                direction = "out" if m["sender"] == me else "in"
            elif me:
                direction = "in"
            records.append({
                "stream": "linkedin_message",
                "external_id": _message_id(m, ordinal),
                "journal_date": base.journal_date_for_iso(m["when"], cfg),
                "started_at": m["when"],
                "ended_at": None,
                "utc_offset_sec": None,
                "direction": direction,
                "title": title,
                "body": m["body"],
                "value": None,
                "unit": None,
                "value_json": canonical_json({k: v for k, v in detail.items()
                                              if v not in (None, "")}),
                "media_path": None,
                "origin_app": "linkedin",
                "last_modified_at": m["when"],
                "participants": participants,
            })
        days = [base.journal_date_for_iso(m["when"], cfg) for m in msgs]
        conv_facts.append({"conversation": title, "messages": len(msgs), "group": group,
                           "first": min(days), "last": max(days)})
    if unattributed:
        what = "message whose sender has no profile URL (kept; sender not attributed)"
        unmapped[what] += unattributed

    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    if conns and conns["preamble"]:
        skipped["line of LinkedIn's note above the Connections.csv header"] += \
            conns["preamble"]
    mirror = {}
    if conns is not None:
        # The file is the whole current list — but only its own stream mirrors:
        # messages are remembered (module docstring).
        mirror = {"present_ids": conns["present"],
                  "mirror_streams": ["linkedin_connection"]}
    used = sorted(shown for shown, _ in found.values())
    return base.empty_scan(
        name,
        account=_handle(me) if me else None,
        source_file=target.name,
        source_hash=_source_hash(target, found),
        records=records,
        handles=[handles[k] for k in sorted(handles)],
        unmapped=[{"what": k, "count": v, "example": examples.get(k)}
                  for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        facts={"files": used, "not_read": len(files["others"]),
               "read_messages": MESSAGES in found,
               "connections": conns["rows"] if conns else None,
               "messages": len(messages), "conversations": conv_facts,
               "you": _handle(me) if me else None, "you_from": me_from,
               "your_name": me_name, "exported_at": files["exported_at"]},
        target_kind=files["kind"],
        others=files["others"],
        **mirror)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    if rec["stream"] == "linkedin_connection":
        what = f"{rec['title'] or '(no name)'}" + (f" — {rec['body']}" if rec["body"] else "")
        return f"{rec['journal_date']} {what}"
    who = {"out": "you", "in": "them"}.get(rec["direction"], "?")
    body = " ".join((rec["body"] or "[attachment]").split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    parts = []
    if facts["connections"] is not None:
        parts.append(f"{facts['connections']} connection(s)")
    if facts["read_messages"]:
        parts.append(f"{facts['messages']} message(s) in "
                     f"{len(facts['conversations'])} conversation(s)")
    lines = [f"linkedin: {scan['source_file']} ({', '.join(parts)}; read: "
             f"{', '.join(PurePosixPath(f).name for f in facts['files'])})"]
    if facts["you"]:
        lines.append(f"  you: {facts['you']}"
                     + (f" ({facts['your_name']})" if facts["your_name"] else "")
                     + f" — from {facts['you_from']}")
    else:
        lines.append("  WARNING which profile is yours is not in this export — message "
                     "direction stays unknown (a later change is a conflict, not an "
                     "update) and removed connections are not mirrored; set "
                     "[connectors.linkedin] profile_url = \"https://www.linkedin.com/in/"
                     "<you>\" first")
    for c in facts["conversations"][:8]:
        lines.append(f"  conversation: {c['conversation']} — "
                     f"{'group' if c['group'] else 'one-to-one'}, {c['messages']} "
                     f"message(s) {c['first']}..{c['last']}")
    if len(facts["conversations"]) > 8:
        lines.append(f"  … and {len(facts['conversations']) - 8} more conversation(s)")

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["external_id"]))
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(latest)}")
    if not by_stream and not plan.get("removed"):
        lines.append("  nothing new — the export matches the database")
    lines += base.removal_lines(plan, "connection(s) no longer in Connections.csv",
                                lambda row: row["title"])

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} connection(s) re-dated by this newer "
                    f"export, otherwise unchanged")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate record(s) in the export")
    if tail:
        lines.append("  " + ", ".join(tail))

    people = [h for h in scan["handles"] if h["value"] != scan["account"]]
    if people:
        emails = sum(1 for h in people if h["kind"] == "email")
        lines.append(f"  handles: {len(people)} ({len(people) - emails} LinkedIn "
                     f"profile(s), {emails} email address(es)) — review with "
                     f"'lifecrm handles list --suggest'")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:24]}…: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. {u['example']})" if u.get("example") else ""))
    if facts["not_read"]:
        sample = ", ".join(PurePosixPath(o).name for o in scan["others"][:4])
        lines.append(f"  not imported: {facts['not_read']} other file(s) in the export "
                     f"(e.g. {sample})")
    return lines
