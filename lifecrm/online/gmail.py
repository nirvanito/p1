"""Gmail over IMAP with an app password — read-only, incremental, no export
(docs/gmail.md).

Each morning the mail that arrived since the last run is downloaded from
imap.gmail.com into `data/fetched/gmail/pending/`, one .eml file per
message, and the email connector reads that folder like any saved mail: the
same trimming, the same skips (Spam, Trash, Drafts, bulk mail), the same
handles, the same Message-ID identity as a Takeout. What a committed import
took in is then moved to `archive/` beside it — kept, never deleted — so
tomorrow's read is tomorrow's mail. Before the owner's yes to the first
import nothing is moved: the first import reads all of it.

Read-only on the server, by construction: All Mail is opened with EXAMINE
(never SELECT), every body is fetched with BODY.PEEK (a message is never
marked read), and nothing is stored, copied, moved, flagged or expunged.
All Mail is found by its \\All flag, never by name: Gmail names it in the
account's language ("[Gmail]/Alle Nachrichten", "[Google Mail]/All Mail").

What a fetched file holds
-------------------------
Gmail's own copy of the message (BODY[]), byte for byte, under three header
lines the connector reads the way it reads a Takeout's:

  X-Lifecrm-Account  the account it was fetched from: whose mail it is (the
                     connector's "you", and the import's account, the same
                     every morning whatever that morning's mail says)
  X-GM-THRID         Gmail's thread id, as Takeout writes it
  X-Gmail-Labels     the labels in Takeout's spelling — Inbox, Sent,
                     Important, Starred, Drafts and the owner's own,
                     comma-separated, quoted when a label holds a comma,
                     RFC 2047 when it is not ASCII — so a Sent message reads
                     as outgoing

A message over MESSAGE_CAP (1 MB: nearly always the attachments) is fetched
in pieces instead (BODYSTRUCTURE first): its header and its text parts, each
up to TEXT_CAP, and every other part as its MIME header alone — its name,
type and disposition — with `X-Lifecrm-Omitted: <bytes>` where its bytes
were. The connector never keeps attachment bytes; this way the big ones are
never downloaded either, and the text — the part a later reader (bank
alerts) needs — is whole.

How much, each morning
----------------------
UIDs (ascending as mail arrives in the folder) are the cursor, kept with the
password in data/secrets.json: every morning brings everything newer than
the newest UID fetched, then a backfill of older mail, newest first, at most
BATCH messages and BACKFILL_BYTES a run, until it reaches the oldest. The
first run brings the newest BATCH; the backfill then waits for the owner's
yes to that first import — nothing more is downloaded for a question still
open, or for a no. A new UIDVALIDITY (Gmail renumbered the folder) starts
over; the connector's Message-ID identity makes a second copy of a message
the same record. A connection that breaks keeps what it fetched and where
it got to.
"""

from __future__ import annotations

import base64
import csv
import email.header
import imaplib
import io
import os
import re
import ssl
from pathlib import Path

from ..config import Config
from ..domain.util import DomainError
from . import secrets

SERVICE = "gmail"
CONNECTOR = "email"
TITLE = "Gmail"
DOC = "docs/gmail.md"
HOST = "imap.gmail.com"
PORT = 993
TIMEOUT = 60

BATCH = 2000                    # older messages a run, newest first
BACKFILL_BYTES = 250 * 2**20    # ... and no more than this of them
MESSAGE_CAP = 2**20             # larger: fetched without its attachments' bytes
TEXT_CAP = 2**20                # of one text part of such a message
CHUNK = 200                     # messages per round of FETCHes; the cursor moves after each
FETCH_BYTES = 16 * 2**20        # whole messages in one FETCH response (held in memory)
WINDOW = 25_000                 # UIDs one SEARCH covers: its answer stays far below
                                # imaplib's 1 MB line limit, however big the mailbox
PENDING = "pending"
ARCHIVE = "archive"
CURSOR = ("uidvalidity", "high", "low", "complete", "first_batch", "settled")

_ADDRESS = re.compile(r"[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+\.[^@\s<>()\[\],;:\"]+")
# Gmail's system labels, as Takeout spells them.
_SYSTEM = {"inbox": "Inbox", "sent": "Sent", "important": "Important",
           "starred": "Starred", "draft": "Drafts", "drafts": "Drafts", "trash": "Trash",
           "spam": "Spam", "chat": "Chat", "chats": "Chat"}

# What each fetch left in its folder, by folder. settle moves exactly that:
# a file another process fetched after this one read the folder stays for
# the next read, never filed away unread.
_HELD: dict[str, list[str]] = {}


def _imap(host: str):
    """The connection. imaplib's own default does not check the server's
    certificate; this one does — the app password goes over it."""
    return imaplib.IMAP4_SSL(host, PORT, ssl_context=ssl.create_default_context(),
                             timeout=TIMEOUT)


def steps() -> list[str]:
    return [
        "Turn on 2-Step Verification for the Google account, if it is not on: "
        "myaccount.google.com › Security › 2-Step Verification.",
        "Make an app password: myaccount.google.com/apppasswords › a name such as "
        "'Life CRM' › Create — copy the 16 letters.",
        "In a terminal here: python -m lifecrm connect gmail you@gmail.com — paste the "
        "app password when asked (it is not shown).",
        "The next morning collect fetches the newest mail; /morning asks before the "
        "first import (docs/gmail.md).",
    ]


# ---------------------------------------------------------------- connect

def _ask(ask, prompt: str, hide: bool) -> str:
    if ask is None:
        raise DomainError("Gmail's app password is typed when asked, in a terminal: "
                          "python -m lifecrm connect gmail you@gmail.com")
    return str(ask(prompt, hide) or "")


def connect(cfg: Config, args: list[str], ask) -> dict:
    if len(args) > 1:
        raise DomainError("connect gmail takes the address only — the app password is "
                          "asked for, never put on the command line: python -m lifecrm "
                          "connect gmail you@gmail.com")
    address = (args[0] if args else _ask(ask, "Gmail address", False)).strip().lower()
    if not (address.isascii() and _ADDRESS.fullmatch(address)):
        raise DomainError(f"{address!r} is not an email address — python -m lifecrm "
                          f"connect gmail you@gmail.com")
    password = "".join(_ask(ask, f"App password for {address} (16 letters from "
                                 f"myaccount.google.com/apppasswords; not shown)",
                            True).split())
    if not password:
        raise DomainError(f"no app password given — make one at "
                          f"https://myaccount.google.com/apppasswords (it needs 2-Step "
                          f"Verification), then: python -m lifecrm connect gmail {address}")
    try:
        imap = _imap(HOST)
    except (OSError, imaplib.IMAP4.error) as e:
        raise DomainError(f"could not reach {HOST} ({_why(e)}) — check the internet "
                          f"connection and try again; nothing kept") from e
    try:
        _login(imap, address, password, connecting=True)
        mailbox = _all_mail(imap)
    except (OSError, imaplib.IMAP4.abort) as e:
        raise DomainError(f"the connection to Gmail broke ({_why(e, password)}) — try "
                          f"again; nothing kept") from e
    except imaplib.IMAP4.error as e:
        raise DomainError(f"Gmail refused a request ({_why(e, password)}) — try again; "
                          f"nothing kept") from e
    finally:
        _close(imap)
    kept = {"address": address, "password": password, "mailbox": mailbox}
    before = secrets.load(cfg).get(SERVICE) or {}
    if before.get("address") == address:
        # a new app password for the same account carries on where the fetch got to
        kept.update({k: before[k] for k in CURSOR if k in before})
    return kept


def _login(imap, address: str, password: str, *, connecting: bool) -> None:
    try:
        imap.login(address, password)
    except imaplib.IMAP4.abort:
        raise                                 # a broken connection: the caller says it
    except imaplib.IMAP4.error as e:
        why = _why(e, password)
        if connecting:
            raise DomainError(
                f"Gmail did not accept the sign-in for {address} ({why}) — check the "
                f"address, and use an app password (16 letters from "
                f"https://myaccount.google.com/apppasswords, which needs 2-Step "
                f"Verification), not the Google password; nothing kept") from e
        raise DomainError(
            f"Gmail no longer accepts the app password for {address} ({why}) — it was "
            f"revoked, or the Google password changed; make a new one and connect "
            f"again: python -m lifecrm connect gmail {address}") from e


def _all_mail(imap) -> str:
    """The folder flagged \\All (RFC 6154), whatever the account's language
    calls it."""
    typ, data = imap.list()
    if typ != "OK":
        raise DomainError(f"Gmail would not list its folders ({_text(data)}) — try again")
    for values in _responses(data):
        if len(values) >= 3 and isinstance(values[0], list) and any(
                isinstance(f, str) and f.lower() == "\\all" for f in values[0]):
            name = values[2]
            return name.decode("ascii", "replace") if isinstance(name, bytes) else str(name)
    raise DomainError("Gmail shows no All Mail folder over IMAP — in Gmail on the web: "
                      "Settings › See all settings › Labels › All Mail › tick 'Show in "
                      "IMAP', then connect again")


def _close(imap) -> None:
    try:
        imap.logout()
    except (OSError, imaplib.IMAP4.error):
        try:
            imap.shutdown()
        except OSError:
            pass


# ---------------------------------------------------------------- fetch

def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def fetch(cfg: Config, secret: dict, dest: Path) -> Path | None:
    address, password = secret.get("address"), secret.get("password")
    if not address or not password:
        raise DomainError("Gmail is connected without an address or app password — "
                          "connect again: python -m lifecrm connect gmail you@gmail.com")
    pending = dest / PENDING
    pending.mkdir(parents=True, exist_ok=True)
    for half in pending.glob("*.part"):       # a write a broken run left unfinished
        half.unlink(missing_ok=True)
    imap = _imap(HOST)                        # unreachable: online.fetch says so
    run = _Fetch(imap, cfg, secret, pending)
    try:
        _login(imap, address, password, connecting=False)
        run.run()
    except (OSError, imaplib.IMAP4.abort) as e:
        kept = (f"the {run.count} message(s) fetched before it are kept, and the next "
                f"run carries on from there" if run.count else
                "nothing fetched; the next run tries again")
        raise DomainError(f"the connection to Gmail broke ({_why(e, password)}) — "
                          f"{kept}") from e
    except imaplib.IMAP4.error as e:
        raise DomainError(f"Gmail refused a request ({_why(e, password)}) — what was "
                          f"fetched is kept; the next run tries again") from e
    finally:
        _close(imap)
    names = sorted(p.name for p in pending.glob("*.eml"))
    _HELD[_key(pending)] = names
    return pending if names else None


def settle(cfg: Config, secret: dict, target: Path) -> None:
    """What a committed import read is in: those files go to archive/ (kept,
    never deleted), and the backfill — waiting for the first yes — may go
    on. A folder this process did not fetch is left as it is: the next read
    takes it again, deduped."""
    target = Path(target)
    names = _HELD.pop(_key(target), None)
    if names is None:
        return
    archive = target.parent / ARCHIVE
    archive.mkdir(parents=True, exist_ok=True)
    for name in names:
        try:
            os.replace(target / name, archive / name)
        except FileNotFoundError:
            continue
    if secrets.load(cfg).get(SERVICE):
        secrets.update(cfg, SERVICE, settled=True)


class _Fetch:
    """One morning: the session, the cursor, the files written."""

    def __init__(self, imap, cfg: Config, secret: dict, folder: Path):
        self.imap, self.cfg, self.secret, self.folder = imap, cfg, secret, folder
        self.address = secret["address"]
        self.validity = 0
        self.count = 0

    def save(self, **values) -> None:
        """The cursor, kept as it moves. A service disconnected meanwhile
        stays disconnected: nothing is written back for it."""
        self.secret.update(values)
        if secrets.load(self.cfg).get(SERVICE):
            secrets.update(self.cfg, SERVICE, **values)

    def run(self) -> None:
        validity, uidnext = self._examine()
        s = self.secret
        if s.get("uidvalidity") != validity or not all(
                isinstance(s.get(k), int) for k in ("high", "low")):
            # first run, or Gmail renumbered the folder: start over, an empty span
            self.save(uidvalidity=validity, high=uidnext - 1, low=uidnext, complete=False,
                      first_batch=False)
        self.validity = validity
        for chunk in _chunks(self._newer(s["high"], uidnext), CHUNK):
            self._messages(chunk)
            self.save(high=chunk[-1])
        if s["high"] < uidnext - 1:
            self.save(high=uidnext - 1)
        if s.get("complete") or (s.get("first_batch") and not s.get("settled")):
            return                 # after the first batch, older mail waits for the yes
        older, everything = self._older(s["low"], BATCH)
        spent = 0
        for chunk in _chunks(older, CHUNK):
            spent += self._messages(chunk)
            self.save(low=chunk[-1])
            if spent >= BACKFILL_BYTES:
                break
        else:
            if everything:
                self.save(complete=True)
        if not s.get("first_batch"):
            self.save(first_batch=True)

    # ------------------------------------------------------------ the folder

    def _examine(self) -> tuple[int, int]:
        """(UIDVALIDITY, UIDNEXT) of All Mail, opened read-only."""
        imap = self.imap
        name = self.secret.get("mailbox")
        if not name:
            name = _all_mail(imap)
            self.save(mailbox=name)
        typ, data = imap.select(_quote(name), readonly=True)
        if typ != "OK":
            found = _all_mail(imap)            # renamed: the account's language changed
            if found != name:
                typ, data = imap.select(_quote(found), readonly=True)
            if typ != "OK":
                raise DomainError(f"Gmail would not open {found} ({_text(data)}) — try "
                                  f"again later, or connect again: python -m lifecrm "
                                  f"connect gmail {self.address}")
            self.save(mailbox=found)
        validity = _number(imap.response("UIDVALIDITY"))
        if validity is None:
            raise DomainError("Gmail did not say All Mail's UIDVALIDITY — nothing fetched; "
                              "the next run tries again")
        uidnext = _number(imap.response("UIDNEXT"))
        if uidnext is None:
            top = self._search("UID *")
            uidnext = (max(top) + 1) if top else 1
        return validity, uidnext

    def _search(self, criteria: str) -> list[int]:
        typ, data = self.imap.uid("SEARCH", criteria)
        if typ != "OK":
            raise DomainError(f"Gmail refused a search ({_text(data)}) — the next run "
                              f"tries again")
        return sorted({int(x) for item in data if isinstance(item, bytes)
                       for x in item.split() if x.isdigit()})

    def _between(self, lo: int, hi: int) -> list[int]:
        # only the range asked counts (`UID n:*` answers the newest message
        # even when n is past it; a server may be as loose with n:m)
        return [u for u in self._search(f"UID {lo}:{hi}") if lo <= u <= hi]

    def _newer(self, high: int, uidnext: int) -> list[int]:
        """Every UID above `high`, oldest first."""
        out: list[int] = []
        lo = high + 1
        while lo < uidnext:
            hi = min(uidnext - 1, lo + WINDOW - 1)
            out += self._between(lo, hi)
            lo = hi + 1
        return out

    def _older(self, low: int, want: int) -> tuple[list[int], bool]:
        """Up to `want` UIDs below `low`, newest first, and whether they are
        all there is."""
        out: list[int] = []
        hi = low - 1
        while hi >= 1 and len(out) <= want:
            lo = max(1, hi - WINDOW + 1)
            out += sorted(self._between(lo, hi), reverse=True)
            hi = lo - 1
        return out[:want], hi < 1 and len(out) <= want

    # ------------------------------------------------------------ messages

    def _messages(self, uids: list[int]) -> int:
        """Fetch and write `uids`; the bytes downloaded."""
        meta = _by_uid(_fetch(self.imap, uids, "(UID RFC822.SIZE X-GM-LABELS X-GM-THRID)"),
                       uids)
        whole = [u for u in uids if u in meta
                 and (_int(meta[u].get("RFC822.SIZE")) or 0) <= MESSAGE_CAP]
        spent = 0
        for group in _groups(whole, meta):
            bodies = _by_uid(_fetch(self.imap, group, "(UID BODY.PEEK[])"), group)
            for uid in group:
                raw = bodies.get(uid, {}).get("BODY[]")
                if isinstance(raw, bytes):
                    spent += self._write(uid, meta[uid], raw)
        for uid in uids:
            if uid in meta and uid not in whole:
                spent += self._write(uid, meta[uid], _reduced(self.imap, uid))
        return spent

    def _write(self, uid: int, meta: dict, raw: bytes) -> int:
        head = [f"X-Lifecrm-Account: {self.address}"]
        thrid = meta.get("X-GM-THRID")
        if isinstance(thrid, str) and thrid.isdigit():
            head.append(f"X-GM-THRID: {thrid}")
        labels = _labels(meta.get("X-GM-LABELS"))
        if labels:
            head.append(f"X-Gmail-Labels: {_label_header(labels)}")
        out = self.folder / f"{self.validity}-{uid:010d}.eml"
        part = out.with_name(out.name + ".part")
        part.write_bytes(("\r\n".join(head) + "\r\n").encode("ascii") + raw)
        os.replace(part, out)
        self.count += 1
        return len(raw)


def _by_uid(responses: list[dict], uids: list[int]) -> dict[int, dict]:
    """FETCH responses by UID, only the UIDs asked for: a server may answer
    one message in several responses, and add flag updates nobody asked for."""
    wanted = set(uids)
    out: dict[int, dict] = {}
    for d in responses:
        uid = _int(d.get("UID"))
        if uid in wanted:
            out.setdefault(uid, {}).update(d)
    return out


def _chunks(seq: list[int], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _groups(uids: list[int], meta: dict):
    """Whole messages, as many to a FETCH as FETCH_BYTES allows."""
    group: list[int] = []
    size = 0
    for u in uids:
        n = _int(meta[u].get("RFC822.SIZE")) or 0
        if group and size + n > FETCH_BYTES:
            yield group
            group, size = [], 0
        group.append(u)
        size += n
    if group:
        yield group


# ---------------------------------------------------------------- big messages

def _reduced(imap, uid: int) -> bytes:
    """A message over MESSAGE_CAP rebuilt from its pieces: the header, the
    text parts whole (up to TEXT_CAP), every other part its MIME header and
    an X-Lifecrm-Omitted line saying how many bytes were left on the server.
    A structure that cannot be read falls back to the message's first
    MESSAGE_CAP bytes."""
    got = _by_uid(_fetch(imap, [uid], "(UID BODYSTRUCTURE)"), [uid]).get(uid, {})
    try:
        tree = _tree(got["BODYSTRUCTURE"], "")
    except (KeyError, IndexError, TypeError, ValueError):
        tree = None
    if tree is None:
        d = _by_uid(_fetch(imap, [uid], f"(UID BODY.PEEK[]<0.{MESSAGE_CAP}>)"),
                    [uid]).get(uid, {})
        raw = d.get("BODY[]") if isinstance(d.get("BODY[]"), bytes) else b""
        return raw[:raw.rfind(b"\n") + 1] if len(raw) >= MESSAGE_CAP else raw
    items = ["UID", "BODY.PEEK[HEADER]"]
    for node in _nodes(tree):
        s = node["section"]
        if s:
            items.append(f"BODY.PEEK[{s}.MIME]")
        if node["kids"] is None and node["keep"]:
            where = s or "TEXT"
            items.append(f"BODY.PEEK[{where}]" + (f"<0.{TEXT_CAP}>"
                                                  if node["size"] > TEXT_CAP else ""))
        elif node["kids"] is None and s and node["type"] == "message/rfc822":
            items.append(f"BODY.PEEK[{s}.HEADER]")
    parts = _by_uid(_fetch(imap, [uid], f"({' '.join(items)})"), [uid]).get(uid, {})
    return _assemble(tree, parts, root=True)


def _tree(bs, section: str) -> dict:
    """BODYSTRUCTURE (RFC 3501 §7.4.2) as {section, kids, boundary} for a
    multipart and {section, kids: None, type, size, encoding, keep} for a
    part. `keep`: a text body, as the connector picks one — text/plain or
    text/html, not an attachment, no file name."""
    if isinstance(bs, list) and bs and isinstance(bs[0], list):
        kids, i = [], 0
        while i < len(bs) and isinstance(bs[i], list):
            kids.append(_tree(bs[i], f"{section}.{i + 1}" if section else str(i + 1)))
            i += 1
        boundary = _params(bs[i + 1] if i + 1 < len(bs) else None).get("boundary")
        if isinstance(boundary, str):
            boundary = boundary.encode("ascii", "replace")
        if not isinstance(boundary, bytes) or not boundary:
            raise ValueError("a multipart part without a boundary")
        return {"section": section, "kids": kids, "boundary": boundary}
    mtype, sub = _str(bs[0]).lower(), _str(bs[1]).lower()
    params = _params(bs[2])
    at = 9 if mtype == "text" else 11 if (mtype, sub) == ("message", "rfc822") else 8
    disp = bs[at] if len(bs) > at else None
    dtype, dparams = "", {}
    if isinstance(disp, list) and disp:
        dtype = _str(disp[0]).lower()
        dparams = _params(disp[1] if len(disp) > 1 else None)
    named = any(k.startswith(("name", "filename")) for k in (*params, *dparams))
    return {"section": section, "kids": None, "type": f"{mtype}/{sub}",
            "size": _int(bs[6]) or 0, "encoding": _str(bs[5]).lower() or "7bit",
            "keep": (mtype == "text" and sub in ("plain", "html")
                     and dtype != "attachment" and not named)}


def _nodes(tree: dict):
    yield tree
    for kid in tree["kids"] or ():
        yield from _nodes(kid)


def _assemble(node: dict, got: dict, *, root: bool = False) -> bytes:
    s = node["section"]
    header = got.get("BODY[HEADER]" if root else f"BODY[{s}.MIME]")
    lines = [header.rstrip(b"\r\n")] if isinstance(header, bytes) and header.strip() else []
    if node["kids"] is not None:
        b = node["boundary"]
        out = [b"\r\n".join(lines), b"\r\n\r\n"]
        for kid in node["kids"]:
            out += [b"--" + b + b"\r\n", _assemble(kid, got), b"\r\n"]
        out.append(b"--" + b + b"--\r\n")
        return b"".join(out)
    if node["keep"]:
        body = got.get("BODY[TEXT]" if root else f"BODY[{s}]")
        body = body if isinstance(body, bytes) else b""
        if node["size"] > TEXT_CAP:
            body = body[:body.rfind(b"\n") + 1]     # cut where a line ends
        return b"\r\n".join(lines) + b"\r\n\r\n" + body
    lines.append(f"X-Lifecrm-Omitted: {_decoded_size(node)}".encode("ascii"))
    inner = got.get(f"BODY[{s}.HEADER]") if not root else None
    return b"\r\n".join(lines) + b"\r\n\r\n" + (inner if isinstance(inner, bytes) else b"")


def _decoded_size(node: dict) -> int:
    """The size the connector reports for a part it has: its decoded bytes.
    BODYSTRUCTURE gives the encoded size; base64 is undone by estimate
    (76-character lines)."""
    n = node["size"]
    if node["encoding"] == "base64":
        n = (n - 2 * -(-n // 78)) * 3 // 4
    return max(n, 0)


# ---------------------------------------------------------------- labels

def _utf7(text: str) -> str:
    """IMAP's modified UTF-7 (RFC 3501 §5.1.3), how a label that is not
    ASCII arrives: '&' opens base64 of UTF-16 with ',' for '/', '-' closes
    it, '&-' is '&'."""
    if "&" not in text:
        return text

    def one(m: re.Match) -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        b64 = chunk.replace(",", "/")
        try:
            return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", one, text)


def _labels(values) -> list[str]:
    """X-GM-LABELS as Takeout names them: \\Inbox is Inbox, \\Draft is
    Drafts, the owner's own as they are."""
    out: list[str] = []
    for v in values if isinstance(values, list) else ():
        if v is None:
            continue
        text = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        if text.startswith("\\"):
            text = _SYSTEM.get(text[1:].lower(), text[1:])
        else:
            text = _utf7(text)
        if text and text not in out:
            out.append(text)
    return out


def _label_header(labels: list[str]) -> str:
    """The X-Gmail-Labels value, as Takeout writes it and the connector
    reads it: comma-separated, a label holding a comma quoted, RFC 2047
    when it is not ASCII."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(labels)
    value = buf.getvalue()
    if value.isascii():
        return value
    return email.header.Header(value, "utf-8").encode(linesep="\r\n")


# ---------------------------------------------------------------- IMAP responses

_ATOM = re.compile(rb'(?:[^\s()"\[\]{]|\[[^\]]*\])+')
_QUOTED = re.compile(rb'"((?:[^"\\]|\\.)*)"', re.S)
_LITERAL = re.compile(rb"\{\d+\}\s*$")


def _responses(data) -> list[list]:
    """imaplib's untagged data as one parsed value list per response. A
    response holding a literal arrives as (text ending '{n}', the literal)
    tuples and ends with the bytes that follow the last one."""
    out, group = [], []
    for item in data or ():
        if item is None:
            continue
        group.append(item)
        if isinstance(item, bytes):
            out.append(_parse(group))
            group = []
    if group:
        out.append(_parse(group))
    return out


def _parse(group: list) -> list:
    stack: list[list] = [[]]
    for kind, value in _tokens(group):
        if kind == "(":
            stack.append([])
        elif kind == ")":
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        elif kind == "atom":
            stack[-1].append(None if value.upper() == "NIL" else value)
        else:
            stack[-1].append(value)
    while len(stack) > 1:
        done = stack.pop()
        stack[-1].append(done)
    return stack[0]


def _tokens(group: list):
    """('(' | ')' | 'atom' | 'str', value): atoms as str (NIL, numbers,
    BODY[1.MIME]), quoted strings and literals as bytes."""
    for item in group:
        if isinstance(item, tuple):
            text, literal = item[0], item[1]
            m = _LITERAL.search(text)
            yield from _lex(text[:m.start()] if m else text)
            yield "str", literal
        elif item:
            yield from _lex(item)


def _lex(text: bytes):
    pos, n = 0, len(text)
    while pos < n:
        c = text[pos]
        if c in b" \t\r\n":
            pos += 1
        elif c == 0x28:
            yield "(", None
            pos += 1
        elif c == 0x29:
            yield ")", None
            pos += 1
        elif c == 0x22:
            m = _QUOTED.match(text, pos)
            if m is None:                              # unterminated: the rest as text
                yield "str", text[pos + 1:]
                return
            yield "str", re.sub(rb"\\(.)", rb"\1", m.group(1))
            pos = m.end()
        else:
            m = _ATOM.match(text, pos)
            if m is None:
                pos += 1
                continue
            yield "atom", m.group(0).decode("ascii", "replace")
            pos = m.end()


def _fetch(imap, uids: list[int], items: str) -> list[dict]:
    """One UID FETCH: {ITEM: value} per message, keys upper-cased with a
    partial's '<0>' left off (BODY[1.2]<0> is BODY[1.2])."""
    typ, data = imap.uid("FETCH", _uidset(uids), items)
    if typ != "OK":
        raise DomainError(f"Gmail refused to send messages ({_text(data)}) — what was "
                          f"fetched is kept; the next run tries again")
    out = []
    for values in _responses(data):
        if len(values) >= 2 and isinstance(values[1], list):
            kv = values[1]
            out.append({re.sub(r"<\d+>$", "", kv[i].upper()): kv[i + 1]
                        for i in range(0, len(kv) - 1, 2) if isinstance(kv[i], str)})
    return out


def _uidset(uids: list[int]) -> str:
    """1,2,3,7 as '1:3,7'."""
    runs: list[list[int]] = []
    for u in sorted(set(uids)):
        if runs and u == runs[-1][1] + 1:
            runs[-1][1] = u
        else:
            runs.append([u, u])
    return ",".join(str(a) if a == b else f"{a}:{b}" for a, b in runs)


def _params(values) -> dict:
    """A BODYSTRUCTURE parameter list ("CHARSET" "utf-8" ...) as {name: value}."""
    if not isinstance(values, list):
        return {}
    return {_str(values[i]).lower(): values[i + 1] for i in range(0, len(values) - 1, 2)}


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _str(v) -> str:
    if isinstance(v, bytes):
        return v.decode("ascii", "replace")
    return "" if v is None else str(v)


def _int(v) -> int | None:
    s = _str(v)
    return int(s) if s.isdigit() else None


def _number(response) -> int | None:
    """The number in imaplib's response(CODE): ('UIDVALIDITY', [b'123'])."""
    _, data = response
    for item in data or ():
        if isinstance(item, bytes) and item.strip().isdigit():
            return int(item.strip())
    return None


def _text(data) -> str:
    parts = [d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
             for d in data or () if d is not None and not isinstance(d, tuple)]
    return " ".join(" ".join(parts).split())[:200] or "no reason given"


def _why(e: Exception, password: str | None = None) -> str:
    """An error's words in one line, the app password never among them."""
    # imaplib's own errors carry the server's words, often as bytes
    text = e.args[0] if isinstance(e, imaplib.IMAP4.error) and e.args else str(e)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text).split())
    if password:
        text = text.replace(password, "…")
    return text[:200] or type(e).__name__
