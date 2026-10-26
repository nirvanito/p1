"""Email connector: Gmail's Takeout mbox, Apple Mail and Thunderbird
mailboxes, .eml files, and Apple Mail's own store on a Mac.

Mail is where advice, plans and confirmations get written down at length:
"what exactly did Priya tell me about the deck" is answered by an email more
often than by a text. This connector reads mail archives the owner already
has. It never logs in to a mail server, never calls an API, and never sends
anything; the Gmail online source (lifecrm/online/gmail.py) does the
fetching, into .eml files read here like any others.

Accepted targets
----------------
- an mbox file: Google Takeout's 'All mail Including Spam and Trash.mbox', a
  Thunderbird folder file (the extensionless 'Inbox' beside its 'Inbox.msf'),
  or the 'mbox' inside an Apple Mail "Export Mailbox…" folder
- a .eml file, or an .emlx / .partial.emlx (Apple Mail's own message file)
- a folder of any of those, searched recursively: an unzipped Takeout, an
  Apple Mail export (INBOX.mbox/, Sent Messages.mbox/, ...), a Thunderbird
  profile's Mail folder, saved .eml files, ~/Library/Mail itself
- a .zip holding them — the Takeout zip as downloaded, read without unzipping
- nothing at all: `default_target` uses [connectors.email] path, else the
  newest takeout-*.zip in ~/Downloads that holds Mail/*.mbox, else on a Mac
  the Apple Mail store (~/Library/Mail)

Volume
------
A decade of Gmail is several GB in one mbox. It is streamed: a From_-line
splitter reads it line by line — from disk, or straight out of the zip — and
only one message is ever held as raw bytes. What is kept per message is its
trimmed text, the headers that matter and its attachments' names, never their
bytes. A message skipped for its label or as bulk mail is dropped once its
headers are read, before its body is parsed. One row per message is the
right grain here (the point is the exact words), so nothing is aggregated;
`[connectors.email] since` imports a recent window when the whole archive
is more than is wanted.

What comes in
-------------
One observation per message, stream 'email':

  title       the subject, RFC 2047-decoded
  body        the text/plain part (else the text/html part as text) with the
              quoted reply ("On … wrote:" and the '>' lines under it, Outlook's
              From:/Sent: block) and the signature ('-- ', "Sent from my
              iPhone") trimmed off — what this person wrote in this message.
              Forwarded content is kept: there it is the message.
  started_at  the Date header, its own offset kept in utc_offset_sec; else
              the newest Received header; else the mbox From_ line
  direction   'out' from one of your addresses, 'self' from you to only
              yourself, 'in' otherwise — None while nothing says which
              address is yours
  value_json  labels (Gmail) or mailbox (other stores), thread, in_reply_to,
              attachments [{name, mime, size}] (never the bytes; a part the
              Gmail source left on the server has the size its
              X-Lifecrm-Omitted line gives), cc_count,
              list_id, text_length (the whole text, before trimming),
              trimmed, invite (a calendar invitation, kept as the email it is)

Skipped and counted, never silently dropped: Spam, Trash, Junk, Drafts and
the other skip_labels (Gmail labels or mailbox names); mailing-list and bulk
mail (List-Unsubscribe or Precedence: bulk/list/junk) unless include_lists
is true — never your own; delivery reports (bounces, read receipts); a
second copy of a message already read; messages before `since`.

Identity
--------
The Message-ID, angle brackets stripped: the same message from a Takeout,
from Apple Mail and from a saved .eml is one record. A message without one
gets 'sha1:' + SHA-1 of its Date, From, Subject and the start of its text.

`last_modified_at` is when the source was made — a Takeout's own timestamp,
else the file's mtime — so a newer export's view of a message (labels
changed since) wins as an update, and an older export read after it is a
conflict, kept as stored. Gmail's read-state labels (Opened, Unread) are
left out of labels: they say what was clicked, not what the message is, and
would churn every import.

Addresses are handles, never names
----------------------------------
From is the 'sender'; To, Cc and Bcc (Bcc appears only on your own sent
copies) are 'recipient's — each an `email` handle, the same one a contact
card or calendar invite produces, with the name the headers give as its
display_name (the most frequent spelling). Which addresses are yours:
[connectors.email] addresses, else the email handles under [owner] in
config.toml, else the most frequent sender of the archive's Sent mail
(Gmail's 'Sent' label, a Sent mailbox), else the address mail was delivered
to (Delivered-To). The report says which, and warns when none is known.
Mail the Gmail source fetched opens with an X-Lifecrm-Account line naming
the account it came from: that address is the account and always one of
yours, ahead of any inference.

Deletion semantics — the archive remembers
------------------------------------------
Texts mirror the phone; mail does not mirror its source, and declares no
`present_ids`. A Takeout does hold all mail, but Gmail purges Spam and Trash
after 30 days and labels move, so "not in this export" reads as a deletion
when it is housekeeping; an Apple Mail store holds only what the account
syncs to this Mac; an exported mailbox is one folder. Nothing in any of them
tells a deletion from a window, so nothing is ever removed because a later
export lacks it. `imports retract` undoes an import.

Known limitations, by design
----------------------------
- A signature without a '-- ' line (just a name and a title under the text)
  stays in the body: there is no reliable way to tell it from the text.
- Journal dates use the configured home timezone, like every connector; the
  Date header's own offset is kept in utc_offset_sec.
- macOS guards ~/Library/Mail: without Full Disk Access every read fails,
  and that is reported as the missing permission, in one line.
"""

from __future__ import annotations

import base64
import binascii
import csv
import email.errors
import email.header
import email.quoprimime
import email.utils
import hashlib
import os
import plistlib
import re
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email import policy as email_policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.parsers.expat import ExpatError

from .. import platforms
from ..config import Config, ConfigError
from ..domain.util import DomainError, canonical_json
from . import base

name = "email"
streams = ("email",)

# Gmail labels or mailbox names whose messages are not kept. Matched without
# case against each Gmail label and the last part of a mailbox's name.
DEFAULT_SKIP_LABELS = ("Spam", "Trash", "Junk", "Bin", "Deleted Messages",
                       "Deleted Items", "Junk E-mail", "Junk Email", "Drafts",
                       "Draft", "Chat")
_SENT_MAILBOXES = frozenset(("sent", "sent mail", "sent messages", "sent items"))
# Gmail's read state: what was clicked, not what the message is.
_READ_STATE = frozenset(("opened", "unread"))
_BULK_PRECEDENCE = frozenset(("bulk", "list", "junk"))
BODY_CAP = 50_000          # characters of one message's text kept as its body

FULL_DISK_ACCESS = (
    "give the app you run this from Full Disk Access (System Settings > Privacy "
    "& Security > Full Disk Access: add Terminal, iTerm or your editor, then quit "
    "and reopen it) and run it again — see docs/email.md")

_PARSER = BytesParser(policy=email_policy.compat32)
_HEADER_END = re.compile(rb"\r?\n\r?\n")
_FOLD = re.compile(r"\r?\n(?=[ \t])")
_ADDRESS = re.compile(r"[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+")
_MSGID = re.compile(r"<([^<>\s]+)>")
_TAKEOUT_NAME = re.compile(r"takeout-(\d{8}T\d{6}Z)-\d+\.zip", re.I)

# Files that sit beside mail in an export or a store and are not messages.
_COMPANIONS = frozenset(("table_of_contents", "info.plist", ".ds_store",
                         "archive_browser.html"))
_COMPANION_SUFFIXES = (".msf", ".plist", ".dat", ".sqlite", ".sqlite-wal",
                       ".sqlite-shm", ".mboxcache")
# Folders inside Apple Mail's store that hold no messages.
_STORE_SKIP_DIRS = frozenset(("maildata", "attachments"))


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> dict:
    s = cfg.connector_settings(name)
    where = "[connectors.email]"
    raw = s.get("addresses")
    if raw is None or raw == "":
        listed: list[str] = []
    elif isinstance(raw, str):
        listed = [raw]
    elif isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        listed = raw
    else:
        raise DomainError(f'{where} addresses must be your address or a list of them, '
                          f'e.g. ["you@example.com"] — got {raw!r}')
    addresses: list[str] = []
    for a in listed:
        a = a.strip().lower()
        if a.startswith("email:"):
            a = a[len("email:"):].strip()
        if not _ADDRESS.fullmatch(a):
            raise DomainError(f"{where} addresses: {a!r} is not an email address")
        if a not in addresses:
            addresses.append(a)
    source = f"{where} addresses" if addresses else None
    if not addresses:
        from .. import owner as owner_mod
        try:
            _, handles = owner_mod.configured(cfg)
        except ConfigError as e:
            raise DomainError(str(e)) from e
        addresses = sorted(v for k, v in handles if k == "email")
        if addresses:
            source = "[owner] handles in config.toml"

    skip = s.get("skip_labels", list(DEFAULT_SKIP_LABELS))
    if isinstance(skip, str):
        skip = [skip]
    if not isinstance(skip, list) or not all(isinstance(x, str) for x in skip):
        raise DomainError(f'{where} skip_labels must be a list of labels or mailbox '
                          f'names, e.g. ["Spam", "Trash"] — got {skip!r}')
    include_lists = s.get("include_lists", False)
    if not isinstance(include_lists, bool):
        raise DomainError(f"{where} include_lists must be true or false — "
                          f"got {include_lists!r}")
    since = s.get("since")
    if isinstance(since, datetime):
        since = since.date()
    elif isinstance(since, str) and since.strip():
        try:
            since = date.fromisoformat(since.strip())
        except ValueError as e:
            raise DomainError(f"{where} since must be a date like 2020-01-01 — "
                              f"got {since!r}") from e
    elif since in (None, ""):
        since = None
    elif not isinstance(since, date):
        raise DomainError(f"{where} since must be a date like 2020-01-01 — got {since!r}")
    return {"addresses": addresses, "addresses_from": source,
            "skip": {x.strip().casefold() for x in skip if x.strip()},
            "include_lists": include_lists,
            "since": since.isoformat() if since else None}


# ---------------------------------------------------------------- headers

def _unfold(value: str) -> str:
    return _FOLD.sub("", value) if "\n" in value else value


def _undo_8bit(value: str) -> str:
    """Raw 8-bit header bytes, which the parser keeps as surrogate escapes,
    read as UTF-8 (as nearly every mailer writes them), else as cp1252."""
    if not any("\udc80" <= ch <= "\udcff" for ch in value):
        return value
    raw = value.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


_WORD = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=")


def _decode_with(raw: bytes, charset: str | None) -> str:
    for cs in (charset, "utf-8"):
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _decode_words(value: str) -> str:
    """RFC 2047 encoded words decoded. A mislabelled charset or a broken word
    is decoded word by word, leniently, rather than failing the message."""
    if "=?" not in value:
        return value
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, UnicodeError, ValueError, binascii.Error,
            email.errors.HeaderParseError):
        pass

    def one(m: re.Match) -> str:
        charset, enc, text = m.groups()
        try:
            if enc in "bB":
                raw = base64.b64decode(text + "=" * (-len(text) % 4))
            else:
                raw = email.quoprimime.header_decode(text).encode("latin-1")
        except (ValueError, binascii.Error, UnicodeError):
            return m.group(0)
        return _decode_with(raw, charset.split("*")[0])

    return _WORD.sub(one, re.sub(r"(\?=)\s+(?==\?)", r"\1", value))


def _text(raw: str | None) -> str:
    """A header as text: unfolded, decoded, whitespace runs collapsed."""
    if not raw:
        return ""
    return " ".join(_decode_words(_unfold(_undo_8bit(str(raw)))).split())


def _headers(msg: Message) -> dict[str, list[str]]:
    """Every header's raw values by lower-cased name, in message order."""
    out: dict[str, list[str]] = {}
    for k, v in msg.raw_items():
        out.setdefault(k.lower(), []).append(v if isinstance(v, str) else str(v))
    return out


def _first(heads: dict, field: str) -> str | None:
    vals = heads.get(field)
    return vals[0] if vals else None


def _addresses(values: list[str]) -> tuple[list[tuple[str, str | None]], int]:
    """[(address, display name)] from address headers, and how many entries
    were not email addresses ('MAILER-DAEMON', a broken '<<>>')."""
    out: list[tuple[str, str | None]] = []
    bad = 0
    for v in values:
        v = _unfold(_undo_8bit(v))
        try:
            pairs = email.utils.getaddresses([v], strict=False)
        except TypeError:                     # a Python without the switch
            pairs = email.utils.getaddresses([v])
        for display, addr in pairs:
            addr = addr.strip().strip("<>").strip().lower()
            if not addr:
                if display.strip():
                    bad += 1
                continue
            if not _ADDRESS.fullmatch(addr):
                bad += 1
                continue
            label = _text(display).strip(" '\"") or None
            if label and label.lower() == addr:
                label = None
            out.append((addr, label))
    return out, bad


def _msgid(value: str | None) -> str | None:
    if not value:
        return None
    v = _unfold(_undo_8bit(value)).strip()
    m = _MSGID.search(v)
    if m:
        return m.group(1)
    v = v.strip("<> \t\"'")
    return v if v and not re.search(r"\s", v) else None


def _labels(heads: dict) -> list[str]:
    """Gmail's X-Gmail-Labels: comma-separated, quoted when a label holds a
    comma, RFC 2047-encoded when it is not ASCII."""
    raw = _first(heads, "x-gmail-labels")
    if not raw:
        return []
    text = _text(raw)
    try:
        cells = next(csv.reader([text], skipinitialspace=True))
    except (csv.Error, StopIteration):
        cells = text.split(",")
    return [c.strip() for c in cells if c.strip()]


# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
     "dec"), 1)}
_FROM_DATE = re.compile(r"(\w{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
                        r"(?:\s+([+-]\d{4}))?\s+(\d{4})\s*$")


def _date(value: str | None) -> tuple[str, int | None] | None:
    """(UTC ISO, the date's own offset in seconds or None) from an RFC 5322
    date. '-0000' means UTC with the sender's zone unknown: no offset."""
    if not value:
        return None
    v = " ".join(_unfold(_undo_8bit(value)).split())
    try:
        dt = email.utils.parsedate_to_datetime(v)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt, offset = dt.replace(tzinfo=timezone.utc), None
        else:
            offset = int(dt.utcoffset().total_seconds())
        if dt.year < 1971:
            return None
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), offset
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _received_date(values: list[str]) -> tuple[str, int | None] | None:
    """The topmost Received header's date: the last hop, at delivery."""
    for v in values:
        _, sep, when = _unfold(v).rpartition(";")
        if sep:
            got = _date(when)
            if got:
                return got
    return None


def _from_line_date(line: bytes) -> tuple[str, int | None] | None:
    """The date an mbox writes on its separator line: 'From x Mon Oct 20
    14:47:00 +0000 2026' (Gmail) or without the zone (Thunderbird, read as
    UTC)."""
    m = _FROM_DATE.search(line.decode("latin-1").rstrip("\r\n"))
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    mon, dd, hh, mi, ss, zone, yyyy = m.groups()
    try:
        tz = timezone.utc
        if zone:
            sign = -1 if zone[0] == "-" else 1
            tz = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[3:])))
        dt = datetime(int(yyyy), _MONTHS[mon.lower()], int(dd), int(hh), int(mi),
                      int(ss or 0), tzinfo=tz)
    except ValueError:
        return None
    return (dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            int(dt.utcoffset().total_seconds()) if zone else None)


# ---------------------------------------------------------------- bodies

class _HtmlText(HTMLParser):
    """text/html as plain text: block elements break lines, scripts and
    styles are dropped, and a <blockquote> — how Gmail, Apple Mail and
    Outlook on the web quote a reply — is '>'-prefixed like a quoted line,
    so the same trimming applies to both kinds of body."""

    _BLOCK = frozenset((
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "tbody", "thead", "tfoot", "tr", "ul"))
    _SKIP = frozenset(("script", "style", "head", "title", "noscript", "template"))

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[list] = []
        self.depth = 0
        self.skipping = 0
        self._break()

    def _break(self) -> None:
        if self.lines and not "".join(self.lines[-1][1]).strip():
            self.lines[-1][0] = self.depth        # reuse an empty line
            return
        self.lines.append([self.depth, []])

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skipping += 1
        elif tag == "br":
            self._break()
        elif tag in ("td", "th"):
            self.lines[-1][1].append(" ")
        elif tag in self._BLOCK:
            if tag == "blockquote":
                self.depth += 1
            self._break()
            if tag == "li":
                self.lines[-1][1].append("- ")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self._BLOCK:
            if tag == "blockquote":
                self.depth = max(0, self.depth - 1)
            self._break()

    def handle_data(self, data):
        if not self.skipping:
            self.lines[-1][1].append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        out = []
        for depth, chunks in self.lines:
            line = " ".join("".join(chunks).split())
            out.append(("> " * depth + line).rstrip() if line or not depth else ">" * depth)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def html_to_text(markup: str) -> str:
    p = _HtmlText()
    try:
        p.feed(markup)
        p.close()
    except (AssertionError, ValueError):          # malformed markup: keep what was read
        pass
    return p.text()


_ATTRIBUTION = re.compile(
    r"^\s*(?:On|Am|Le|El|Op|Em|Il)\s.{0,400}\b(?:wrote|schrieb|a\s+écrit|escribió|"
    r"schreef|escreveu|ha\s+scritto)\b.{0,200}:\s*$", re.I)
_ATTRIBUTION_START = re.compile(r"^\s*(?:On|Am|Le|El|Op|Em|Il)\s", re.I)
_WROTE = re.compile(r"^.{1,200}\bwrote:\s*$", re.I)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(?:Original Message|Ursprüngliche Nachricht|"
                       r"Message d'origine|Mensaje original)\s*-{2,}\s*$", re.I)
_UNDERSCORES = re.compile(r"^\s*_{20,}\s*$")
_HDR_FROM = re.compile(r"^\s*\*?(?:From|Von|De)\s*:\*?\s+\S", re.I)
_HDR_NEXT = re.compile(r"^\s*\*?(?:Sent|Date|Gesendet|Datum|Envoyé|Enviado)\s*:\*?\s+\S",
                       re.I)
_FORWARD = re.compile(r"^\s*(?:-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:"
                      r"|-{2,}\s*Weitergeleitete Nachricht\s*-{2,})\s*$", re.I)
_FWD_SUBJECT = re.compile(r"^\s*(?:fwd?|wg|tr|rv|enc)\s*:", re.I)
_MOBILE_SIG = re.compile(
    r"^\s*(?:Sent from my \S.{0,40}|Sent from (?:Outlook|Mail|Yahoo Mail|Gmail)\b.{0,40}"
    r"|Get Outlook for \S.{0,30}|Sent with \S.{0,40}|Von meinem \S.{0,40} gesendet"
    r"|Enviado desde mi \S.{0,40}|Envoyé de mon \S.{0,40})\s*$", re.I)


def trim(text: str, subject: str = "") -> str:
    """What the writer wrote in this message: the text without the quoted
    reply below it and without the signature.

    - "On <date>, <name> wrote:" (and its translations), when '>' lines
      follow, ends the message — unless writing continues between or after
      the quoted lines (an inline or bottom-posted reply): then only the
      attribution and the '>' lines go;
    - Outlook's "-----Original Message-----", or its From:/Sent: header
      block, ends the message — except in a forward, where it is the start
      of what was forwarded;
    - a forwarded message ("---------- Forwarded message ---------", "Begin
      forwarded message:") is kept whole; only what comes before it is
      trimmed;
    - the signature: from a '-- ' line on, and a trailing "Sent from my
      iPhone".
    """
    lines = text.split("\n")
    forward = bool(_FWD_SUBJECT.match(subject or ""))

    # Cheap tests first: most lines of a reply are quoted ('>') or plain
    # prose, and a decade of mail is millions of lines.
    def header_block(rows: list[str], i: int) -> bool:
        return ":" in rows[i][:8] and bool(_HDR_FROM.match(rows[i])) and any(
            _HDR_NEXT.match(x) for x in rows[i + 1:i + 4])

    def separator(ln: str) -> bool:
        return ("--" in ln and bool(_ORIGINAL.match(ln))) or \
            (ln.lstrip().startswith("__") and bool(_UNDERSCORES.match(ln)))

    def forward_marker(ln: str) -> bool:
        return ("orward" in ln or "eitergeleitet" in ln) and bool(_FORWARD.match(ln))

    # In a forward, Outlook's separator or header block starts what was
    # forwarded; anywhere, Gmail's and Apple's forward markers do.
    fwd_at = next((i for i, ln in enumerate(lines)
                   if forward_marker(ln) or (forward and (separator(ln)
                                                          or header_block(lines, i)))),
                  None)
    head, tail = (lines, []) if fwd_at is None else (lines[:fwd_at], lines[fwd_at:])

    def quoted_follows(start: int) -> bool:
        nxt = next((ln for ln in head[start:] if ln.strip()), None)
        return nxt is not None and nxt.lstrip().startswith(">")

    cut = None
    drop: set[int] = set()
    i = 0
    while i < len(head):
        line = head[i]
        bare = line.lstrip()
        if not bare or bare.startswith(">"):
            i += 1
            continue
        if separator(line) and (_ORIGINAL.match(line) or any(
                _HDR_FROM.match(x) for x in head[i + 1:i + 3])):
            cut = i
            break
        if i > 0 and header_block(head, i):
            cut = i
            break
        span = 0
        if _ATTRIBUTION_START.match(line):
            if _ATTRIBUTION.match(line):
                span = 1
            elif i + 1 < len(head) and _ATTRIBUTION.match(
                    line.rstrip() + " " + head[i + 1].strip()):
                span = 2
        if not span and "wrote:" in line.lower() and _WROTE.match(line) \
                and quoted_follows(i + 1):
            span = 1
        if span and quoted_follows(i + span):
            rest = head[i + span:]
            seen_quote = False
            inline = False
            for ln in rest:
                if ln.lstrip().startswith(">"):
                    seen_quote = True
                elif ln.strip() and seen_quote:
                    inline = True
                    break
            if not inline:
                cut = i
                break
            drop.update(range(i, i + span))
            i += span
            continue
        i += 1

    kept = [ln for n, ln in enumerate(head[:cut] if cut is not None else head)
            if n not in drop and not ln.lstrip().startswith(">")]
    sig = next((n for n, ln in enumerate(kept) if ln.rstrip("\r") in ("-- ", "--")), None)
    if sig is not None:
        kept = kept[:sig]
    while kept and not kept[-1].strip():
        kept.pop()
    while kept and _MOBILE_SIG.match(kept[-1]):
        kept.pop()
        while kept and not kept[-1].strip():
            kept.pop()
    out = "\n".join(ln.rstrip() for ln in kept + tail)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _payload(part: Message) -> bytes:
    try:
        data = part.get_payload(decode=True)
    except (ValueError, LookupError, binascii.Error, UnicodeError):
        data = None
    return data if isinstance(data, bytes) else b""


def _part_text(part: Message) -> str:
    try:
        charset = part.get_content_charset()
    except (ValueError, LookupError):
        charset = None
    text = _decode_with(_payload(part), charset)
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _filename(part: Message) -> str | None:
    try:
        fn = part.get_filename()
    except (ValueError, LookupError, UnicodeError):
        fn = None
    return (_text(fn) or None) if fn else None


def _omitted(part: Message) -> int | None:
    """The size of a part whose bytes lifecrm's Gmail fetch left on the
    server (X-Lifecrm-Omitted, lifecrm/online/gmail.py): its name and type
    are there, its bytes never were."""
    value = (part.get("X-Lifecrm-Omitted") or "").strip()
    return int(value) if value.isdigit() else None


def _content(msg: Message, partial: bool) -> dict:
    """{plain, html, attachments, invite} of a parsed message. An attached
    message (message/rfc822) is described as an attachment, never read for
    the body."""
    found = {"plain": None, "html": None, "attachments": [], "invite": False}

    def visit(part: Message) -> None:
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else None
            subject = _text(inner.get("Subject")) if isinstance(inner, Message) else ""
            size = _omitted(part)
            if size is None:
                try:
                    size = len(inner.as_bytes()) if isinstance(inner, Message) else None
                except (ValueError, LookupError, UnicodeError, TypeError, KeyError):
                    size = None
            found["attachments"].append({
                "name": _filename(part) or (f"{subject}.eml" if subject else None),
                "mime": ctype, "size": size})
            return
        if part.is_multipart():
            for sub in part.get_payload() or ():
                if isinstance(sub, Message):
                    visit(sub)
            return
        disposition = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        filename = _filename(part)
        if ctype == "text/calendar":
            found["invite"] = True
        if disposition != "attachment" and not filename and ctype in ("text/plain",
                                                                       "text/html"):
            key = "plain" if ctype == "text/plain" else "html"
            if found[key] is None:
                text = _part_text(part)
                if text.strip():
                    found[key] = text
            return
        if disposition == "attachment" or filename:
            size = _omitted(part)
            if size is None:
                size = len(_payload(part))
            found["attachments"].append({"name": filename, "mime": ctype,
                                         "size": None if partial and not size else size})

    visit(msg)
    return found


# ---------------------------------------------------------------- where it was found

def _is_sent_mailbox(mailbox: str | None) -> bool:
    if not mailbox:
        return False
    return mailbox.rsplit("/", 1)[-1].strip().casefold() in _SENT_MAILBOXES


def _mailbox_for(rel: PurePosixPath, kind: str) -> str | None:
    """The mailbox a message file belongs to, from where it sits: the nearest
    '<name>.mbox' folder (an Apple Mail export, or the store), else an mbox
    file's own name under Thunderbird's '.sbd' folders, else an .eml's
    folder."""
    parts = rel.parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i].lower().endswith(".mbox"):
            return "/".join(p[:-5] for p in parts[:i + 1] if p.lower().endswith(".mbox"))
    if kind == "mbox":
        last = parts[-1]
        stem = last[:-5] if last.lower().endswith(".mbox") else last
        return "/".join([p[:-4] for p in parts[:-1] if p.lower().endswith(".sbd")] + [stem])
    return parts[-2] if len(parts) > 1 else None


def _kind_of(file_name: str, siblings: set[str] | None = None) -> str | None:
    low = file_name.lower()
    if low.endswith(".emlx"):
        return "emlx"
    if low.endswith(".eml"):
        return "eml"
    if low.endswith(".mbox") or low == "mbox":
        return "mbox"
    if siblings is not None and "." not in file_name and f"{low}.msf" in siblings:
        return "mbox"                          # Thunderbird: 'Inbox' beside 'Inbox.msf'
    return None


def _is_companion(file_name: str) -> bool:
    low = file_name.lower()
    return low in _COMPANIONS or low.startswith(".") or low.endswith(_COMPANION_SUFFIXES)


def _iso_mtime(st_mtime: float) -> str | None:
    return base.iso_utc(int(st_mtime)) if st_mtime > 0 else None


def _zip_made(path: Path, info: zipfile.ZipInfo) -> str | None:
    """When a zip's export was made: a Takeout's name says it (UTC);
    otherwise the member's own stamp."""
    m = _TAKEOUT_NAME.fullmatch(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    try:
        return datetime(*info.date_time).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _in_mail_store(path: Path) -> bool:
    parts = Path(path).parts
    return "Library" in parts and "Mail" in parts


def access_denied(path: Path, detail: str) -> DomainError:
    if _in_mail_store(path) or platforms.is_mac():
        return DomainError(f"macOS did not allow reading {path} ({detail}) — "
                           f"{FULL_DISK_ACCESS}")
    return DomainError(f"permission denied reading {path} ({detail})")


@contextmanager
def privacy_guard(target: Path):
    """A PermissionError anywhere inside becomes one DomainError line."""
    try:
        yield
    except PermissionError as e:
        where = Path(e.filename) if e.filename else target
        raise access_denied(where, e.strerror or "permission denied") from e


# ---------------------------------------------------------------- mbox

_FROM_SEPARATOR = re.compile(rb"^From \S+.*\d{1,2}:\d{2}")
# The whole asctime-shaped separator line: 'From x Tue Sep 22 13:47:00 [+0000 ]2026'.
_FROM_STRICT = re.compile(
    rb"^From \S+ +(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) +(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
    rb"Sep|Oct|Nov|Dec) +\d{1,2} +\d{1,2}:\d{2}(?::\d{2})?(?: +[+-]\d{4})? +\d{4}\s*$")
_ESCAPED_FROM = re.compile(rb"^>+From ")


def mbox_messages(stream, label: str):
    """(From_ line, message bytes) for each message of an mbox, read line by
    line: a multi-GB mailbox is never held whole.

    A message starts at a line beginning 'From ' that carries a time and
    follows a blank line (or opens the file), as every writer's separator
    does — or, without the blank line, at a line that is a separator from
    end to end ('From x Tue Sep 22 13:47:00 2026'), since not every writer
    leaves one. A body line a writer escaped as '>From ' is unescaped
    (mboxrd)."""
    first = True
    prev_blank = True
    sep: bytes | None = None
    buf: list[bytes] = []

    def done() -> bytes:
        while buf and buf[-1].strip() == b"":
            buf.pop()
        return b"".join(buf)

    for line in stream:
        if first:
            first = False
            if line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
            if not line.startswith(b"From "):
                raise DomainError(
                    f"{label} is not an mbox mailbox — it should start with a 'From ' "
                    f"separator line, but starts {line[:60]!r}")
            sep = line
            prev_blank = False
            continue
        if line.startswith(b"From ") and (
                (prev_blank and _FROM_SEPARATOR.match(line)) or _FROM_STRICT.match(line)):
            yield sep, done()
            buf = []
            sep = line
            prev_blank = False
            continue
        if line.startswith(b">") and _ESCAPED_FROM.match(line):
            line = line[1:]
        buf.append(line)
        prev_blank = line.strip() == b""
    if sep is not None:
        yield sep, done()


# ---------------------------------------------------------------- emlx

def read_emlx(data: bytes, label: str) -> tuple[bytes, dict]:
    """(the RFC 822 message, its Apple plist) from an .emlx: a line holding
    the message's byte count, the message, then an XML plist of flags."""
    data = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    nl = data.find(b"\n")
    head = data[:nl].strip() if nl >= 0 else b""
    if not head.isdigit():
        raise DomainError(f"{label} is not an .emlx message — it should start with "
                          f"the message's length, but starts {data[:40]!r}")
    count = int(head)
    message = data[nl + 1:nl + 1 + count]
    meta: dict = {}
    tail = data[nl + 1 + count:].strip()
    if tail:
        try:
            parsed = plistlib.loads(tail)
            meta = parsed if isinstance(parsed, dict) else {}
        except (plistlib.InvalidFileException, ExpatError, ValueError, TypeError):
            meta = {}
    return message, meta


# Apple Mail's emlx flags word (bit positions, as documented by those who
# have read the format; not published by Apple).
_EMLX_DELETED = 1 << 1
_EMLX_DRAFT = 1 << 6
_EMLX_JUNK = 1 << 24


# ---------------------------------------------------------------- reading

class _Reader:
    """Every message of every file, parsed once, kept compact."""

    def __init__(self, cfg: Config, settings: dict):
        self.cfg = cfg
        self.s = settings
        self.configured = set(settings["addresses"])
        self.hash = hashlib.sha256()
        self.messages: list[dict] = []
        self.ids: set[str] = set()
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.examples: dict[str, str] = {}
        self.mailboxes: dict[str, dict] = {}
        self.mbox_files: list[dict] = []          # one entry per mbox file
        self.kinds: Counter = Counter()           # mail files read, by kind
        self.ignored: list[str] = []
        self.read = 0
        self.bad_addresses = 0
        self.fetched_for: Counter = Counter()     # X-Lifecrm-Account, by address

    def _box(self, mailbox: str | None) -> dict:
        return self.mailboxes.setdefault(mailbox or "(none)", {"kept": 0, "skipped": 0})

    def skip(self, reason: str, mailbox: str | None) -> None:
        self.skipped[reason] += 1
        self._box(mailbox)["skipped"] += 1

    def unmap(self, what: str, where: str, mailbox: str | None) -> None:
        self.unmapped[what] += 1
        self.examples.setdefault(what, where)
        self._box(mailbox)["skipped"] += 1

    def _skip_label(self, labels: list[str], mailbox: str | None) -> str | None:
        for label in labels:
            if label.casefold() in self.s["skip"]:
                return f"message labelled {label} (skip_labels)"
        if mailbox:
            last = mailbox.rsplit("/", 1)[-1].strip()
            if last.casefold() in self.s["skip"]:
                return f"message in the {last} mailbox (skip_labels)"
        return None

    def message(self, raw: bytes, where: dict) -> None:
        self.read += 1
        mailbox = where.get("mailbox")
        try:
            self._message(raw, where)
        except (ValueError, LookupError, TypeError, IndexError, UnicodeError,
                binascii.Error, email.errors.MessageError) as e:
            self.unmap(f"message that could not be read ({type(e).__name__})",
                       where["label"], mailbox)

    def _message(self, raw: bytes, where: dict) -> None:
        mailbox = where.get("mailbox")
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if not raw.strip():
            self.unmap("empty message", where["label"], mailbox)
            return
        # The header block alone decides whether the message is kept; the
        # body is parsed only for a message that is.
        end = _HEADER_END.search(raw)
        heads = _headers(_PARSER.parsebytes(raw[:end.start()] if end else raw,
                                            headersonly=True))
        if not any(k in heads for k in ("from", "date", "subject", "message-id",
                                        "to", "received")):
            self.unmap("text with no mail headers", where["label"], mailbox)
            return
        if next(iter(heads)) == "x-lifecrm-account":
            # The account lifecrm's Gmail fetch took it from, written as the
            # first line — only there: one further down came with the message.
            fetched, _ = _addresses(heads["x-lifecrm-account"][:1])
            if fetched:
                self.fetched_for[fetched[0][0]] += 1
        labels = _labels(heads)
        reason = self._skip_label(labels, mailbox)
        if reason:
            self.skip(reason, mailbox)
            return
        flags = where.get("flags")
        if isinstance(flags, int):
            for bit, why in ((_EMLX_DELETED, "message marked deleted in Apple Mail"),
                             (_EMLX_DRAFT, "draft (Apple Mail's draft flag)"),
                             (_EMLX_JUNK, "message marked junk in Apple Mail")):
                if flags & bit:
                    self.skip(why, mailbox)
                    return
        when = _date(_first(heads, "date")) or _received_date(heads.get("received", []))
        if when is None and where.get("separator"):
            when = _from_line_date(where["separator"])
        if when is None:
            when = where.get("fallback_date")
        if when is None:
            self.unmap("message with no readable date (no Date, Received or From_ line)",
                       where["label"], mailbox)
            return
        journal = base.journal_date_for_iso(when[0], self.cfg)
        if self.s["since"] and journal < self.s["since"]:
            self.skip(f"message before [connectors.email] since ({self.s['since']})",
                      mailbox)
            return
        senders, bad = _addresses(heads.get("from", [])[:1])
        sender = senders[0] if senders else None
        sent_copy = any(lb.casefold() == "sent" for lb in labels) or _is_sent_mailbox(mailbox)
        mine = sent_copy or (sender is not None and sender[0] in self.configured)
        ctype = (_first(heads, "content-type") or "").split(";")[0].strip().lower()
        if ctype == "multipart/report":
            self.skip("delivery report (a bounce or a read receipt)", mailbox)
            return
        precedence = (_text(_first(heads, "precedence")) or "").strip().lower()
        if not self.s["include_lists"] and not mine and (
                "list-unsubscribe" in heads or precedence in _BULK_PRECEDENCE):
            self.skip("mailing-list or bulk mail (List-Unsubscribe or Precedence: "
                      "bulk; include_lists = true keeps it)", mailbox)
            return
        msgid = _msgid(_first(heads, "message-id"))
        if msgid is not None and msgid in self.ids:
            self.skip("second copy of a message already read (same Message-ID)", mailbox)
            return

        full = _PARSER.parsebytes(raw)
        content = _content(full, where.get("partial", False))
        subject = _text(_first(heads, "subject"))
        if content["plain"] is not None:
            whole = content["plain"]
        elif content["html"] is not None:
            whole = html_to_text(content["html"])
        else:
            whole = ""
        whole = whole.strip()
        body = trim(whole, subject) if whole else ""
        if msgid is None:
            seed = "\x1f".join((" ".join((_first(heads, "date") or "").split()),
                                " ".join((_first(heads, "from") or "").split()),
                                subject, whole[:1000]))
            msgid = "sha1:" + hashlib.sha1(seed.encode("utf-8", "surrogatepass")).hexdigest()
            if msgid in self.ids:
                self.skip("second copy of a message already read (same Message-ID)",
                          mailbox)
                return
        self.ids.add(msgid)

        recipients = {}
        for field in ("to", "cc", "bcc"):
            recipients[field], n = _addresses(heads.get(field, []))
            bad += n
        self.bad_addresses += bad
        refs = _MSGID.findall(_unfold(" ".join(heads.get("references", []))))
        in_reply_to = _msgid(_first(heads, "in-reply-to"))
        thrid = (_text(_first(heads, "x-gm-thrid")) or "").strip()
        detail: dict = {}
        shown_labels = sorted({lb for lb in labels if lb.casefold() not in _READ_STATE},
                              key=str.casefold)
        if shown_labels:
            detail["labels"] = shown_labels
        elif mailbox and not labels:
            detail["mailbox"] = mailbox
        detail["thread"] = thrid or (refs[0] if refs else None) or in_reply_to or msgid
        if in_reply_to:
            detail["in_reply_to"] = in_reply_to
        if content["attachments"]:
            detail["attachments"] = content["attachments"]
        if recipients["cc"]:
            detail["cc_count"] = len(recipients["cc"])
        list_id = _text(_first(heads, "list-id"))
        if list_id:
            m = re.search(r"<([^<>]+)>", list_id)
            detail["list_id"] = m.group(1) if m else list_id
        detail["text_length"] = len(whole)
        if body != whole:
            detail["trimmed"] = True
        if content["invite"]:
            detail["invite"] = True
        if len(body) > BODY_CAP:
            body = body[:BODY_CAP]
            detail["body_truncated"] = True

        if "x-gmail-labels" in heads or thrid:
            origin = "gmail"
        elif where.get("origin"):
            origin = where["origin"]
        elif "x-mozilla-status" in heads:
            origin = "thunderbird"
        else:
            origin = "email"
        delivered, _ = _addresses(heads.get("delivered-to", [])
                                  + heads.get("x-original-to", []))
        self.messages.append({
            "external_id": msgid, "when": when[0], "offset": when[1], "journal": journal,
            "subject": subject or None, "body": body or None, "detail": detail,
            "from": sender, "to": recipients["to"], "cc": recipients["cc"],
            "bcc": recipients["bcc"], "sent_copy": sent_copy,
            "delivered_to": [a for a, _ in delivered], "origin": origin,
            "modified": where.get("modified")})
        self._box(mailbox)["kept"] += 1

    # ------------------------------------------------------------ sources

    def mbox(self, stream, label: str, mailbox: str | None, modified: str | None,
             origin: str | None) -> None:
        n = 0
        start = self.read
        for sep, raw in mbox_messages(stream, label):
            n += 1
            self.hash.update(b"\0mbox\0" + label.encode("utf-8", "surrogatepass")
                             + b"\0" + sep + raw)
            self.message(raw, {"label": f"{label} (message {n})", "mailbox": mailbox,
                               "modified": modified, "origin": origin, "separator": sep})
        self.kinds["mbox"] += 1
        self.mbox_files.append({"file": label, "mailbox": mailbox,
                                "messages": self.read - start})

    def single(self, data: bytes, label: str, kind: str, mailbox: str | None,
               modified: str | None, origin: str | None) -> None:
        where = {"label": label, "mailbox": mailbox, "modified": modified,
                 "origin": origin}
        if kind == "emlx":
            raw, meta = read_emlx(data, label)
            where["origin"] = "apple_mail"
            where["partial"] = label.lower().endswith(".partial.emlx")
            where["flags"] = meta.get("flags") if isinstance(meta.get("flags"), int) else None
            received = meta.get("date-received")
            if isinstance(received, (int, float)) and received > 0:
                where["fallback_date"] = (base.iso_utc(int(received)), None)
        else:
            raw = data
        self.hash.update(b"\0" + kind.encode() + b"\0"
                         + label.encode("utf-8", "surrogatepass") + b"\0" + data)
        self.kinds[kind] += 1
        self.message(raw, where)

    def unreadable(self, label: str, error: DomainError, mailbox: str | None) -> None:
        """A file in a folder or zip that is not what its name says: counted
        with the reason, never fatal to the rest of the archive."""
        what = "mail file that could not be read"
        self.unmapped[what] += 1
        self.examples.setdefault(what, str(error))
        self._box(mailbox)


def _walk(root: Path) -> list[Path]:
    """Every file under root, sorted, without dot-folders, __MACOSX and the
    store's MailData/Attachments folders. A folder that cannot be listed
    raises (os.walk would otherwise skip it silently — a macOS privacy
    denial would read as an empty mail store)."""
    def fail(err: OSError) -> None:
        raise err

    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__MACOSX"
                             and d.lower() not in _STORE_SKIP_DIRS)
        out.extend(Path(dirpath) / f for f in sorted(filenames))
    return out


def _read_folder(target: Path, reader: _Reader) -> None:
    files = _walk(target)
    by_dir: dict[Path, set[str]] = {}
    for p in files:
        by_dir.setdefault(p.parent, set()).add(p.name.lower())
    for p in files:
        rel = PurePosixPath(p.relative_to(target).as_posix())
        kind = _kind_of(p.name, by_dir[p.parent])
        if kind is None:
            if not _is_companion(p.name):
                reader.ignored.append(rel.as_posix())
            continue
        mailbox = _mailbox_for(rel, kind)
        modified = _iso_mtime(p.stat().st_mtime)
        apple = any(part.lower().endswith(".mbox") for part in rel.parts[:-1])
        thunderbird = kind == "mbox" and p.name.lower() != "mbox" \
            and not p.name.lower().endswith(".mbox")
        origin = "apple_mail" if apple else ("thunderbird" if thunderbird else None)
        try:
            if kind == "mbox":
                with open(p, "rb") as f:
                    reader.mbox(f, rel.as_posix(), mailbox, modified, origin)
            else:
                reader.single(p.read_bytes(), rel.as_posix(), kind, mailbox, modified,
                              origin)
        except DomainError as e:
            reader.unreadable(rel.as_posix(), e, mailbox)


def _read_zip(target: Path, reader: _Reader) -> None:
    try:
        zf = zipfile.ZipFile(target)
    except zipfile.BadZipFile as e:
        raise DomainError(f"cannot open {target.name} as a zip: {e}") from e
    with zf:
        infos = sorted((i for i in zf.infolist() if not i.is_dir()),
                       key=lambda i: i.filename)
        for info in infos:
            rel = PurePosixPath(info.filename)
            if any(part.startswith(".") or part == "__MACOSX" for part in rel.parts):
                continue
            kind = _kind_of(rel.name)
            if kind is None:
                if not _is_companion(rel.name):
                    reader.ignored.append(info.filename)
                continue
            mailbox = _mailbox_for(rel, kind)
            modified = _zip_made(target, info)
            apple = any(part.lower().endswith(".mbox") for part in rel.parts[:-1])
            origin = "apple_mail" if apple else None
            try:
                if kind == "mbox":
                    with zf.open(info) as f:
                        reader.mbox(f, info.filename, mailbox, modified, origin)
                else:
                    reader.single(zf.read(info), info.filename, kind, mailbox, modified,
                                  origin)
            except DomainError as e:
                reader.unreadable(info.filename, e, mailbox)
            except (zipfile.BadZipFile, EOFError) as e:
                raise DomainError(f"{target.name}: {info.filename} cannot be read from "
                                  f"the zip ({e}) — is the download complete?") from e


def _sniff(head: bytes) -> str | None:
    text = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    if text.startswith(b"From "):
        return "mbox"
    first = text.split(b"\n", 1)[0].strip()
    if first.isdigit():
        return "emlx"
    if re.match(rb"^[A-Za-z][A-Za-z0-9-]*:", text):
        return "eml"
    return None


def _read_file(target: Path, reader: _Reader) -> str:
    with open(target, "rb") as f:
        head = f.read(512)
    if not head.strip():
        raise DomainError(f"{target.name} is empty")
    kind = _kind_of(target.name) or _sniff(head)
    if kind is None:
        raise DomainError(
            f"{target.name} is not a mail file — expected an mbox (first line 'From …'), "
            f"a .eml, an .emlx, a folder of them or a .zip; it starts {head[:40]!r}")
    # A file inside an Apple Mail '.mbox' folder belongs to that mailbox.
    parents = [p.name for p in target.parents][:6]
    at = next((i for i, n in enumerate(parents) if n.lower().endswith(".mbox")), None)
    rel = PurePosixPath(*reversed(parents[:at + 1]), target.name) if at is not None \
        else PurePosixPath(target.name)
    mailbox = _mailbox_for(rel, kind)
    modified = _iso_mtime(target.stat().st_mtime)
    origin = "apple_mail" if at is not None else None
    if kind == "mbox":
        with open(target, "rb") as f:
            reader.mbox(f, target.name, mailbox, modified, origin)
    else:
        reader.single(target.read_bytes(), target.name, kind, mailbox, modified, origin)
    return kind


# ---------------------------------------------------------------- who is you

def _owner(reader: _Reader, settings: dict) -> dict:
    """{'addresses', 'account', 'source', 'also', 'warning'} — see the module
    docstring for the order the answer is looked for in."""
    froms = Counter(m["from"][0] for m in reader.messages if m["from"])
    configured = settings["addresses"]
    # Mail lifecrm fetched from Gmail names its account, so the account is
    # the same every morning whatever that morning's mail says.
    fetched = (sorted(reader.fetched_for.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
               if reader.fetched_for else None)
    if configured:
        present = [a for a in configured if froms.get(a)]
        account = fetched or (max(present, key=lambda a: (froms[a], -configured.index(a)))
                              if present else configured[0])
        return {"addresses": set(configured) | ({fetched} if fetched else set()),
                "account": account, "source": settings["addresses_from"], "also": [],
                "warning": None}
    sent = Counter(m["from"][0] for m in reader.messages if m["sent_copy"] and m["from"])
    if fetched:
        others = sorted(((a, n) for a, n in sent.items() if a != fetched),
                        key=lambda kv: (-kv[1], kv[0]))
        return {"addresses": {fetched}, "account": fetched,
                "source": "the Gmail account it was fetched from (lifecrm connect gmail)",
                "also": [{"address": a, "messages": n} for a, n in others],
                "warning": None}
    if sent:
        ranked = sorted(sent.items(), key=lambda kv: (-kv[1], kv[0]))
        account = ranked[0][0]
        return {"addresses": {account}, "account": account,
                "source": (f"inferred: the most frequent sender of the {sum(sent.values())} "
                           f"sent message(s); set [connectors.email] addresses to be sure"),
                "also": [{"address": a, "messages": n} for a, n in ranked[1:]],
                "warning": None}
    delivered = Counter(a for m in reader.messages for a in m["delivered_to"])
    if delivered:
        account = sorted(delivered.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return {"addresses": {account}, "account": account,
                "source": ("inferred: the address mail was delivered to (Delivered-To); "
                           "set [connectors.email] addresses to be sure"),
                "also": [], "warning": None}
    return {"addresses": set(), "account": None, "source": None, "also": [],
            "warning": ("nothing in this archive says which address is yours (no Sent "
                        "mail, no Delivered-To) — direction stays unknown, and setting it "
                        "after --apply is a conflict, not an update; set [connectors.email] "
                        "addresses = [\"you@example.com\"] first")}


# ---------------------------------------------------------------- scan

def _record(m: dict, owner: set[str]) -> dict:
    sender = m["from"]
    everyone = [a for a, _ in m["to"] + m["cc"] + m["bcc"]]
    if not owner or sender is None:
        direction = None
    elif sender[0] in owner:
        direction = "self" if everyone and all(a in owner for a in everyone) else "out"
    else:
        direction = "in"
    participants: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for role, pairs in (("sender", [sender] if sender else []),
                        ("recipient", m["to"] + m["cc"] + m["bcc"])):
        for addr, _ in pairs:
            if (addr, role) not in seen:
                seen.add((addr, role))
                participants.append({"kind": "email", "value": addr, "role": role})
    return {
        "stream": "email",
        "external_id": m["external_id"],
        "journal_date": m["journal"],
        "started_at": m["when"],
        "ended_at": None,
        "utc_offset_sec": m["offset"],
        "direction": direction,
        "title": m["subject"],
        "body": m["body"],
        "value": None,
        "unit": None,
        "value_json": canonical_json(m["detail"]),
        "media_path": None,
        "origin_app": m["origin"],
        "last_modified_at": m["modified"],
        "participants": participants,
    }


def scan(target: Path, cfg: Config) -> base.Scan:
    settings = _settings(cfg)
    reader = _Reader(cfg, settings)
    with privacy_guard(target):
        try:
            if target.is_dir():
                _read_folder(target, reader)
                kind = "apple_mail_store" if reader.kinds["emlx"] else "folder"
            elif zipfile.is_zipfile(target):
                _read_zip(target, reader)
                kind = "zip"
            elif target.is_file():
                kind = _read_file(target, reader)
            else:
                raise DomainError(f"not found: {target}")
        except PermissionError:
            raise
        except OSError as e:
            raise DomainError(f"cannot read {target.name}: {e.strerror or e}") from e
    if not reader.kinds and not reader.unmapped:
        listing = ", ".join(reader.ignored[:8]) or "(empty)"
        raise DomainError(f"{target.name} holds no mail — looked for .mbox files, "
                          f"Apple Mail '<name>.mbox' folders, .eml and .emlx files "
                          f"(has: {listing})")
    if reader.read == 0:
        first = next(iter(reader.examples.values()), None)
        raise DomainError(f"{target.name} holds no messages"
                          + (f" — {first}" if first else ""))

    owner = _owner(reader, settings)
    records = sorted((_record(m, owner["addresses"]) for m in reader.messages),
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))

    names: dict[str, Counter] = {}
    for m in reader.messages:
        for addr, label in ([m["from"]] if m["from"] else []) + m["to"] + m["cc"] + m["bcc"]:
            c = names.setdefault(addr, Counter())
            if label:
                c[label] += 1
    handles = [{"kind": "email", "value": addr,
                "display_name": (sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                                 if c else None)}
               for addr, c in sorted(names.items())]

    skipped = [f"{n} × {why}" for why, n in sorted(reader.skipped.items())]
    if reader.ignored:
        skipped.append(f"{len(reader.ignored)} × file that is not a mail message "
                       f"(e.g. {reader.ignored[0]})")
    unmapped = [{"what": k, "count": v, "example": reader.examples.get(k)}
                for k, v in sorted(reader.unmapped.items())]
    if reader.bad_addresses:
        unmapped.append({"what": "address that is not an email address (dropped; its "
                                 "message is kept)", "count": reader.bad_addresses,
                         "example": None})
    directions = Counter(r["direction"] or "unknown" for r in records)
    dates = [r["journal_date"] for r in records]
    warnings = [owner["warning"]] if owner["warning"] else []
    facts = {
        "source_kind": kind,
        "messages_read": reader.read,
        "mbox_files": reader.mbox_files[:50],
        "file_count": sum(reader.kinds.values()),
        "file_kinds": dict(sorted(reader.kinds.items())),
        "mailboxes": [{"mailbox": k, **v} for k, v in sorted(reader.mailboxes.items())],
        "you": owner["account"],
        "you_from": owner["source"],
        "also_sent_as": owner["also"],
        "addresses": sorted(owner["addresses"]),
        "directions": dict(sorted(directions.items())),
        "attachments": sum(len(m["detail"].get("attachments", ()))
                           for m in reader.messages),
        "invites": sum(1 for m in reader.messages if m["detail"].get("invite")),
        "trimmed": sum(1 for m in reader.messages if m["detail"].get("trimmed")),
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "warnings": warnings,
    }
    # No present_ids: the archive remembers (see the module docstring).
    return base.empty_scan(
        name,
        account=owner["account"],
        source_file=target.name,
        source_hash=reader.hash.hexdigest(),
        records=records,
        handles=handles,
        unmapped=unmapped,
        skipped=skipped,
        facts=facts,
        target_kind=kind)


# ---------------------------------------------------------------- default target

def downloads_dir() -> Path:
    return platforms.home() / "Downloads"


def mail_store() -> Path:
    return platforms.home() / "Library" / "Mail"


def _holds_takeout_mail(path: Path) -> bool:
    """A Takeout zip or folder with Mail/<label>.mbox in it."""
    try:
        if path.is_dir():
            return any(p.is_file() for p in (path / "Mail").glob("*.mbox")) or \
                any(p.is_file() for p in (path / "Takeout" / "Mail").glob("*.mbox"))
        with zipfile.ZipFile(path) as zf:
            return any(re.search(r"(?:^|/)Mail/[^/]+\.mbox$", n) for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.email] path; else the newest Takeout in ~/Downloads that
    holds Mail/*.mbox (a takeout-*.zip, or the folder one unzips to); else,
    on a Mac, the Apple Mail store. Never a guess beyond those."""
    configured = cfg.connector_settings(name).get("path")
    if isinstance(configured, str) and configured.strip():
        p = cfg.resolve(configured.strip())
        if not p.exists():
            raise DomainError(f"[connectors.email] path {p} does not exist — fix it in "
                              f"config.toml, or pass the archive's path")
        return p
    looked = []
    downloads = downloads_dir()
    looked.append(f"{downloads}{os.sep}takeout-*.zip")
    found: list[Path] = []
    try:
        if downloads.is_dir():
            for p in downloads.iterdir():
                low = p.name.lower()
                if low.startswith("takeout") and (
                        (p.is_file() and low.endswith(".zip")) or p.is_dir()) \
                        and _holds_takeout_mail(p):
                    found.append(p)
    except PermissionError as e:
        raise DomainError(f"cannot list {downloads}: permission denied — on a Mac, allow "
                          f"the terminal to access Downloads (System Settings > Privacy & "
                          f"Security > Files and Folders)") from e
    if found:
        return max(found, key=lambda p: (p.stat().st_mtime, p.name))
    if platforms.is_mac():
        store = mail_store()
        looked.append(str(store))
        with privacy_guard(store):
            if store.is_dir() and any(re.fullmatch(r"V\d+", n) for n in os.listdir(store)):
                return store
    raise DomainError(
        f"no mail archive found (looked in: {'; '.join(looked)}) — pass an .mbox, a "
        f"folder of mailboxes or .eml files, or a Google Takeout zip, or set "
        f"[connectors.email] path (docs/email.md)")


# ---------------------------------------------------------------- report

_KIND_LABEL = {"zip": "zip", "folder": "folder", "apple_mail_store": "Apple Mail store",
               "mbox": "mbox", "eml": ".eml file", "emlx": ".emlx file"}


def _sample(rec: dict) -> str:
    parts = rec.get("participants") or []
    sender = next((p["value"] for p in parts if p["role"] == "sender"), None)
    to = next((p["value"] for p in parts if p["role"] == "recipient"), None)
    who = {"out": f"you to {to or '?'}", "self": "you to yourself",
           "in": f"from {sender or '?'}"}.get(rec["direction"], f"from {sender or '?'}")
    title = " ".join((rec["title"] or "(no subject)").split())
    if len(title) > 60:
        title = title[:59] + "…"
    return f"{rec['journal_date']} {who}: {title}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    lines = [f"email: {scan['source_file']} ({_KIND_LABEL.get(scan['target_kind'], 'mail')}, "
             f"{facts['messages_read']} message(s) read from {facts['file_count']} "
             f"mail file(s))"]
    boxes = sorted(facts["mailboxes"], key=lambda b: (-(b["kept"] + b["skipped"]),
                                                      b["mailbox"]))
    for b in boxes[:8]:
        lines.append(f"  mailbox {b['mailbox']}: {b['kept']} kept, {b['skipped']} skipped")
    if len(boxes) > 8:
        lines.append(f"  … and {len(boxes) - 8} more mailbox(es)")
    if facts["you"]:
        lines.append(f"  you: {facts['you']} ({facts['you_from']})")
    for a in facts["also_sent_as"][:4]:
        lines.append(f"  also sent from: {a['address']} ({a['messages']}) — if it is "
                     f"yours, list it in [connectors.email] addresses")
    for w in facts["warnings"]:
        lines.append(f"  WARNING {w}")

    new = plan["new"]
    changed = [rec for _, rec, _ in plan["updates"]]
    recs = new or changed
    if recs:
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["external_id"]))
        lines.append(f"  email: {len(new)} new, {len(changed)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    else:
        lines.append("  nothing new — the archive matches the database")
    d = facts["directions"]
    if d:
        lines.append("  direction: " + ", ".join(
            f"{d[k]} {label}" for k, label in (("in", "in"), ("out", "out"),
                                                ("self", "to yourself"),
                                                ("unknown", "unknown")) if d.get(k)))

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es) — a newer export "
                    f"of the same messages")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate message(s) in the archive")
    if tail:
        lines.append("  " + ", ".join(tail))

    you = set(facts["addresses"])
    people = [h for h in scan["handles"] if h["value"] not in you]
    if people:
        lines.append(f"  handles: {len(people)} email address(es) — review with "
                     f"'lifecrm handles list --suggest'")
    if facts["attachments"] or facts["invites"]:
        lines.append(f"  attachments: {facts['attachments']} described (name, type, size — "
                     f"never the bytes); {facts['invites']} calendar invitation(s) kept "
                     f"as email")
    if facts["trimmed"]:
        lines.append(f"  bodies: {facts['trimmed']} trimmed of a quoted reply or a "
                     f"signature (value_json text_length keeps the whole text's length)")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:28]}…: differs from "
                     f"the stored record but is not newer — kept existing "
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
    return lines
