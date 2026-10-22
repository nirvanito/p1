"""Android texts and calls: SMS Backup & Restore (SyncTech) XML backups.

Android has no export of its own for texts, and Samsung's Smart Switch writes
an encrypted, proprietary archive. The free *SMS Backup & Restore* app is the
standard way out: on a schedule it writes the phone's SMS, MMS and call log
as two XML files — `sms-<time>.xml` and `calls-<time>.xml` — to the phone or
to a cloud drive (OneDrive, on the Windows + Samsung setup; see
docs/android-texts.md). This connector reads those files. It never talks to
the phone and never sends anything.

Accepted targets
----------------
- nothing at all: `default_target` looks in [connectors.android_sms] folder,
  then where the app uploads to OneDrive, then ~/Downloads, and says where it
  looked when it finds nothing.
- a folder: the newest sms-*.xml and the newest calls-*.xml in it.
- a .zip holding them, by the same rule.
- one file. A file is recognised by its root element (<smses> or <calls>),
  not by its name.

What comes in
-------------
  android_sms    a text message
  android_mms    an MMS, group messages included: who sent it, everyone it
                 went to, its text, and its attachments *described* — name and
                 type. The backup carries media inline as base64; it is never
                 decoded, and media never enters the database
  android_call   a call: value = duration in seconds (unit 's'), and
                 value_json.call = in / out / missed / voicemail / rejected /
                 answered_elsewhere

Participants are **handles, never names**: phone numbers (normalized), email
addresses, and — for alphanumeric senders like 'AMAZON' — an app_account
handle. The backup's `contact_name` is what the phone called that number on
the day of the backup; it becomes the handle's `display_name` and nothing
more. `readable_date` is the phone's locale rendering of `date` and is never
parsed.

Identity
--------
The backup has no message id: the phone's row ids are not exported, and would
not survive a restore or a new phone anyway. A message's id is therefore its
content — 'android:' + SHA-1 of the thread's normalized address(es), the time
in milliseconds, the direction and the text — or, for an MMS, the carrier's
message id (`m_id`) with its time, which also keeps MMS ids independent of
how this reader assembles text parts. Re-importing a later backup of the same
phone is a no-op for everything already stored. A draft, or a message still
in the outbox or failed, is not imported: it was never delivered.

Deletion semantics — texts mirror the phone (owner's decision, 2026-09-24)
-------------------------------------------------------------------------
A message deleted on the phone is removed by the next import, but only from a
backup that is the phone's whole state. The app can also write archive
("only new since last time") backups, and those must never remove anything:

- the file's root says type="full"            -> mirrors
- it says anything else (an incremental file) -> never mirrors
- it says nothing (older app versions)        -> mirrors only with
                                                 [connectors.android_sms]
                                                 mirror = true
- mirror = false                              -> never mirrors
- a file whose `count` disagrees with what it holds is incomplete, and
  never mirrors.

Mirroring is scoped to the texts file and to the phone's own number (the
import's account), from [connectors.android_sms] account or, failing that,
the `from` address of the phone's sent MMS. Without an account nothing is
removed. Every id in a mirroring file counts as present, including the
drafts and messages this reader skips, and the shared write layer's guards
apply: a removal large enough to look like an incomplete backup is refused
without --allow-mass-removal, a snapshot is taken first, and a row a
confirmed entry cites is hidden rather than deleted.

Calls are remembered, never removed. A calls file, even a full one, is not
the phone's whole call history: Android's call log keeps only its newest
calls (500 on stock Android; Samsung's limit is its own) and drops older ones
by itself, a few a day. Mirroring it would remove every call the phone aged
out — never enough at once to trip the mass-removal guard — until the archive
held no more than the phone does, which is what archiving calls is for. A
call deleted from the phone's Recents therefore stays archived; `imports
retract` undoes an import.

Known limitations, by design
----------------------------
- Journal dates use the configured home timezone; the backup records no zone
  per message.
- RCS: Samsung Messages keeps its own RCS chats out of the shared SMS/MMS
  store, so SMS Backup & Restore may not see them at all. Google Messages is
  reported to file RCS into the MMS store; such rows import as android_mms,
  and the report counts the ones that look like it (unverified).
- Android group MMS have no names; the thread is its member list.
- Reactions from an iPhone arrive as ordinary text ('Loved "..."') and are
  imported as the messages they are.
- Blocked calls are not imported (they are almost all spam, and every one
  would add a number to the handle review queue).
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain import days
from ..domain.util import DomainError, canonical_json
from . import base

name = "android_sms"
streams = ("android_sms", "android_mms", "android_call")
# Texts flow daily; a newest record this old means scheduled backups stopped.
stale_after_days = 14

# The same label ios_messages gives the phone's own handle, so review tools
# treat both phones' owner handles alike.
OWNER_LABEL = "you (this phone)"
# A sent MMS often names its sender with this placeholder instead of a number.
OWNER_TOKEN = "insert-address-token"

_MESSAGE_STREAMS = ("android_mms", "android_sms")

# sms.type (Telephony.TextBasedSmsColumns): 1 inbox, 2 sent; the rest never
# reached anyone. Undelivered rows are outgoing too, and hash as 'out', so a
# message still in the outbox at one backup keeps its id once it is sent.
_SMS_DIRECTIONS = {1: "in", 2: "out"}
_SMS_UNDELIVERED = {3: "draft", 4: "message still in the outbox",
                    5: "message that failed to send", 6: "message queued, never sent"}
# mms.msg_box: 1 inbox, 2 sent, 3 drafts, 4 outbox, 5 failed.
_MMS_DIRECTIONS = {1: "in", 2: "out"}
_MMS_UNDELIVERED = {3: "draft", 4: "message still in the outbox",
                    5: "message that failed to send"}
# PDU address types (PduHeaders): 137 FROM, 151 TO, 130 CC, 129 BCC.
_FROM, _TO, _CC, _BCC = 137, 151, 130, 129
_MMS_NOTIFICATION = 130          # m_type: a notice that an MMS awaits download
# CallLog.Calls.TYPE -> (direction, value_json.call); 6 (blocked) is skipped.
_CALL_TYPES = {1: ("in", "in"), 2: ("out", "out"), 3: ("in", "missed"),
               4: ("in", "voicemail"), 5: ("in", "rejected"),
               7: ("in", "answered_elsewhere")}
_CALL_BLOCKED = 6
# CallLog presentation: 1 allowed; otherwise the network withheld the number.
_PRESENTATION = {2: "private", 3: "unknown", 4: "payphone"}
# Old Android call logs spell a withheld number as one of these.
_NO_NUMBER = {"", "null", "-1", "-2", "-3", "unknown", "private"}

_ROOTS = {"smses": "texts", "calls": "calls"}
# expat errors that mean the file stops before it ends: no element found,
# unclosed token, partial character. A half-synced OneDrive file looks like this.
_CUT_OFF = {3, 5, 6}
_FILE_GLOBS = {"texts": "sms-*.xml", "calls": "calls-*.xml"}
_YEAR_3000_MS = 32503680000 * 1000
_STAMP = re.compile(r"(\d{14})")
_APP_VERSION = re.compile(rb"SMS Backup &(?:amp;)? Restore v?([0-9][0-9.]*)", re.I)

# Where the app's cloud upload lands under a OneDrive root. The app names its
# folder itself; these are the spellings seen across versions.
ONEDRIVE_FOLDERS = (
    Path("Apps") / "SMS Backup & Restore",
    Path("Apps") / "SMS Backup and Restore",
    Path("Apps") / "SMSBackupRestore",
    Path("SMSBackupRestore"),
    Path("SMS Backup & Restore"),
)


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> dict:
    """[connectors.android_sms]: account (your number), folder, mirror."""
    s = cfg.connector_settings(name)
    out: dict = {"account": None, "folder": None, "mirror": None}
    acct = s.get("account")
    if acct not in (None, ""):
        h = base.handle_for(str(acct))
        if not h or h[0] != "phone":
            raise DomainError(f"[connectors.android_sms] account = {acct!r} is not a "
                              f"phone number — it should be this phone's own number, "
                              f"e.g. '+15125550100'")
        out["account"] = h[1]
    folder = s.get("folder")
    if folder not in (None, ""):
        if not isinstance(folder, str):
            raise DomainError(f"[connectors.android_sms] folder = {folder!r} must be "
                              f"a path in quotes")
        out["folder"] = Path(folder).expanduser()
    mirror = s.get("mirror")
    if mirror is not None and not isinstance(mirror, bool):
        raise DomainError(f"[connectors.android_sms] mirror = {mirror!r} must be "
                          f"true or false")
    out["mirror"] = mirror
    return out


# ---------------------------------------------------------------- time

def ms_epoch(value) -> int | None:
    """A backup timestamp as epoch milliseconds, or None.

    `date` is milliseconds; an MMS's `date_sent` (and `date`, in some app
    versions) is seconds, straight from Android's MMS table. Anything below
    1e11 is seconds: as milliseconds it would be 1973. A value past the year
    3000 is a corrupt row, not a time — and would overflow the clock.
    """
    if value is None:
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    if n <= 0:
        return None
    n = n * 1000 if n < 10**11 else n
    return n if n < _YEAR_3000_MS else None


def _iso(ms: int) -> str:
    return base.iso_utc(ms // 1000)


# ---------------------------------------------------------------- reading the XML

_REF = re.compile(rb"&#(x[0-9A-Fa-f]+|[0-9]+);")
# Bytes a numeric character reference can contain. A chunk is only ever cut
# after a byte outside this set, so no reference — and no surrogate pair of
# references — is ever split between two chunks.
_REF_BYTES = frozenset(b"&#;x0123456789abcdefABCDEF")


def _is_xml_char(cp: int) -> bool:
    return (cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF)


class _Repaired:
    """The backup's bytes, with character references expat rejects rewritten.

    SMS Backup & Restore writes an emoji as two UTF-16 surrogate-pair
    references (`&#55357;&#56832;` for 😀). XML forbids surrogate code points,
    so expat refuses the whole file. Each pair is combined into the one
    reference it stands for; a lone surrogate becomes U+FFFD, and a reference
    to a character XML cannot hold (a control character) is dropped. Every
    repair is counted for the report. The source file is never modified, and
    the hash is of the bytes as they are on disk.
    """

    def __init__(self, raw):
        self._raw = raw
        self._hold = b""
        self._eof = False
        self.sha = hashlib.sha256()
        self.head = b""
        self.pairs = self.lone = self.dropped = 0

    def read(self, size: int = -1) -> bytes:
        while True:
            if self._eof:
                out, self._hold = self._hold, b""
                return self._fix(out)
            data = self._raw.read(1 << 16)
            if not data:
                self._eof = True
                continue
            self.sha.update(data)
            if len(self.head) < 4096:
                self.head += data[:4096 - len(self.head)]
            buf = self._hold + data
            cut = len(buf)
            while cut and buf[cut - 1] in _REF_BYTES:
                cut -= 1
            self._hold = buf[cut:]
            if cut:
                return self._fix(buf[:cut])

    def _fix(self, data: bytes) -> bytes:
        if b"&#" not in data:
            return data
        refs = list(_REF.finditer(data))
        out, pos, i = [], 0, 0
        while i < len(refs):
            m = refs[i]
            out.append(data[pos:m.start()])
            pos = m.end()
            cp = _codepoint(m.group(1))
            if 0xD800 <= cp <= 0xDBFF and i + 1 < len(refs) \
                    and refs[i + 1].start() == m.end():
                low = _codepoint(refs[i + 1].group(1))
                if 0xDC00 <= low <= 0xDFFF:
                    out.append(b"&#%d;" % (0x10000 + ((cp - 0xD800) << 10)
                                           + (low - 0xDC00)))
                    self.pairs += 1
                    pos = refs[i + 1].end()
                    i += 2
                    continue
            if _is_xml_char(cp):
                out.append(m.group(0))
            elif 0xD800 <= cp <= 0xDFFF:
                out.append(b"&#65533;")
                self.lone += 1
            else:
                self.dropped += 1
            i += 1
        out.append(data[pos:])
        return b"".join(out)

    def drain(self) -> None:
        """Read to the end, so the hash covers the whole file."""
        while self.read():
            pass


def _codepoint(group: bytes) -> int:
    return int(group[1:], 16) if group[:1] == b"x" else int(group)


def _attr(elem_or_attrs, key: str) -> str | None:
    """An attribute, with the app's literal "null" read as absent."""
    v = elem_or_attrs.get(key)
    if v is None:
        return None
    v = v.strip() if key not in ("body", "text") else v
    return None if v in ("", "null") else v


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _name(value: str | None) -> str | None:
    """contact_name, with the app's placeholder for a stranger read as none."""
    if value is None:
        return None
    v = value.strip()
    return None if v in ("", "null", "(Unknown)") else v


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.replace("\r\n", "\n").strip()
    return v or None


def _hid(*parts) -> str:
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return "android:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()


class _Reader:
    """Accumulates one scan across the texts file and the calls file. The
    owner's handle is attached afterwards (`finish`), once both files have
    had their say on which number is the phone's own."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.records: list[dict] = []
        self.names: dict[tuple[str, str], dict[str, int]] = {}
        self.examples: dict[str, tuple[str, str]] = {}
        self.skipped: dict[str, int] = {}
        self.unmapped: dict[str, int] = {}
        self.present: dict[str, set[str]] = {"texts": set(), "calls": set()}
        self.sent_from: dict[str, int] = {}          # owner candidates: sent MMS 'from'
        self.received_to: set[str] | None = None     # numbers on every received MMS
        self.mms_units = {"ms": 0, "s": 0}
        self.mms_apps: dict[str, int] = {}
        self.rcs_like = 0
        self.unaligned = 0
        self.read = {"texts": 0, "calls": 0}

    # --- bookkeeping ------------------------------------------------------

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def odd(self, what: str) -> None:
        self.unmapped[what] = self.unmapped.get(what, 0) + 1

    def handle(self, raw: str | None, label: str | None = None) -> tuple[str, str] | None:
        if raw is None:
            return None
        raw = raw.strip()
        if not raw or raw.lower() == OWNER_TOKEN:
            return None
        h = base.handle_for(raw)
        if h is None:
            return None
        if label:
            names = self.names.setdefault(h, {})
            names[label] = names.get(label, 0) + 1
        if raw != h[1] and len(self.examples) < 50:
            self.examples.setdefault(raw, h)
        return h

    def record(self, *, stream: str, external_id: str, ms: int, direction: str,
               body: str | None, detail: dict, participants: list[dict],
               owner_role: str, value=None, unit=None, ended_ms=None,
               origin_app: str) -> None:
        # The same rule as base.journal_date_for_iso, without the round trip
        # through a string: a phone's backup is tens of thousands of rows.
        at = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
        started = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.records.append({
            "stream": stream, "external_id": external_id,
            "journal_date": days.journal_date_for(at, self.cfg.timezone,
                                                  self.cfg.day_rollover_hour),
            "started_at": started,
            "ended_at": _iso(ended_ms) if ended_ms and ended_ms // 1000 > ms // 1000
            else None,
            "utc_offset_sec": None, "direction": direction,
            "title": None, "body": body, "value": value, "unit": unit,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None, "origin_app": origin_app,
            "last_modified_at": started,
            "participants": participants, "_owner_role": owner_role})

    @staticmethod
    def _add(participants: list[dict], h, role: str) -> None:
        if h:
            p = {"kind": h[0], "value": h[1], "role": role}
            if p not in participants:
                participants.append(p)

    # --- texts ------------------------------------------------------------

    def sms(self, el) -> None:
        self.read["texts"] += 1
        raw_addr = _attr(el, "address")
        ms = ms_epoch(el.get("date"))
        kind = _int(el.get("type"))
        body = _text(_attr(el, "body"))
        h = self.handle(raw_addr, _name(el.get("contact_name")))
        thread = f"{h[0]}:{h[1]}" if h else None
        direction = _SMS_DIRECTIONS.get(kind)
        # Identity before any skip: a draft or an unreadable row is still on
        # the phone, and counts as present for mirroring.
        ext = _hid("sms", thread or raw_addr, ms,
                   "out" if kind in _SMS_UNDELIVERED else direction or kind, body)
        self.present["texts"].add(ext)
        if kind in _SMS_UNDELIVERED:
            self.skip(f"{_SMS_UNDELIVERED[kind]} (never delivered)")
            return
        if direction is None:
            self.odd(f"sms type {el.get('type')!r}")
            return
        if ms is None:
            self.skip("message with no usable timestamp")
            return
        if body is None:
            self.skip("text message with no body")
            return
        participants: list[dict] = []
        self._add(participants, h, "sender" if direction == "in" else "recipient")
        self.record(stream="android_sms", external_id=ext, ms=ms, direction=direction,
                    body=body, detail={"thread": thread}, participants=participants,
                    owner_role="recipient" if direction == "in" else "sender",
                    origin_app="messages")

    def mms(self, el) -> None:
        self.read["texts"] += 1
        raw_date = _int(el.get("date"))
        ms = ms_epoch(raw_date)
        if raw_date and raw_date > 0:
            self.mms_units["s" if raw_date < 10**11 else "ms"] += 1
        box = _int(el.get("msg_box"))
        direction = _MMS_DIRECTIONS.get(box)

        # The thread: Android's own member list for it, '~'-joined, with the
        # names the phone showed in the same order.
        members_raw = [a.strip() for a in (_attr(el, "address") or "").split("~")
                       if a.strip()]
        contact = el.get("contact_name") or ""
        names = [contact] if len(members_raw) == 1 \
            else [n.strip() for n in contact.split(",")]
        aligned = len(names) == len(members_raw)
        if len(members_raw) > 1 and not aligned:
            self.unaligned += 1      # a name with a comma in it; guessing would misname
        member_names = {raw: _name(names[i]) if aligned else None
                        for i, raw in enumerate(members_raw)}
        thread_handles = []
        for raw in members_raw:
            h = self.handle(raw, member_names.get(raw))
            if h and h not in thread_handles:
                thread_handles.append(h)
        thread = ",".join(sorted(f"{k}:{v}" for k, v in thread_handles)) or None

        texts, attachments = [], []
        for part in el.iter("part"):
            ct = (_attr(part, "ct") or "").lower()
            if ct == "application/smil" or part.get("seq") == "-1":
                continue                          # slide layout, not content
            if ct == "text/plain":
                t = _text(_attr(part, "text"))
                if t:
                    texts.append(t)
                continue
            # The data attribute (base64 media) is never touched.
            attachments.append({k: v for k, v in {
                "name": _attr(part, "cl") or _attr(part, "name") or _attr(part, "fn"),
                "mime": ct or None}.items() if v})
        body = "\n".join(texts) or None

        addrs = [(_int(a.get("type")), (a.get("address") or "").strip())
                 for a in el.iter("addr")]
        m_id = _attr(el, "m_id")
        hash_dir = "out" if box in _MMS_UNDELIVERED else direction or box
        ext = (_hid("mms-id", m_id, ms, hash_dir) if m_id else
               _hid("mms", thread or "~".join(members_raw), ms, hash_dir,
                    body, "|".join(a.get("name", "") for a in attachments)))
        self.present["texts"].add(ext)

        if box in _MMS_UNDELIVERED:
            self.skip(f"{_MMS_UNDELIVERED[box]} (never delivered)")
            return
        if direction is None:
            self.odd(f"mms msg_box {el.get('msg_box')!r}")
            return
        if _int(el.get("m_type")) == _MMS_NOTIFICATION:
            self.skip("MMS never downloaded (only its notification is on the phone)")
            return
        if ms is None:
            self.skip("message with no usable timestamp")
            return
        if body is None and not attachments:
            self.skip("MMS with no text or attachment")
            return

        participants: list[dict] = []
        if direction == "out":
            mine = set()                 # the phone's own number, as this MMS names it
            for kind, raw in addrs:
                if kind == _FROM:
                    h = base.handle_for(raw) if raw.lower() != OWNER_TOKEN else None
                    if h and h[0] == "phone":
                        mine.add(h)
                        self.sent_from[h[1]] = self.sent_from.get(h[1], 0) + 1
            for kind, raw in addrs:
                if kind in (_TO, _CC, _BCC):
                    h = self.handle(raw, member_names.get(raw))
                    if h not in mine:
                        self._add(participants, h, "recipient")
            if not participants:          # no usable 'to': the thread is who it went to
                for h in thread_handles:
                    self._add(participants, h, "recipient")
        else:
            sender = next((self.handle(raw, member_names.get(raw))
                           for kind, raw in addrs if kind == _FROM), None)
            self._add(participants, sender, "sender")
            to_numbers = set()
            for kind, raw in addrs:
                if kind in (_TO, _CC, _BCC):
                    h = self.handle(raw, member_names.get(raw))
                    if h and h != sender:
                        self._add(participants, h, "recipient")
                        if h[0] == "phone":
                            to_numbers.add(h[1])
            if addrs:
                self.received_to = to_numbers if self.received_to is None \
                    else self.received_to & to_numbers
            if sender is None and len(thread_handles) == 1:
                # No usable 'from': in a one-to-one thread it can only be them.
                self._add(participants, thread_handles[0], "sender")

        creator = _attr(el, "creator")
        if creator:
            self.mms_apps[creator] = self.mms_apps.get(creator, 0) + 1
        if (_attr(el, "tr_id") or "").startswith("proto:"):
            self.rcs_like += 1
        detail = {"thread": thread, "group": True if len(thread_handles) > 1 else None,
                  "attachments": attachments or None, "subject": _attr(el, "sub"),
                  "app": creator}
        self.record(stream="android_mms", external_id=ext, ms=ms, direction=direction,
                    body=body, detail=detail, participants=participants,
                    owner_role="sender" if direction == "out" else "recipient",
                    origin_app="messages")

    # --- calls ------------------------------------------------------------

    def call(self, el) -> None:
        self.read["calls"] += 1
        raw = _attr(el, "number")
        ms = ms_epoch(el.get("date"))
        kind = _int(el.get("type"))
        withheld = _PRESENTATION.get(_int(el.get("presentation")) or 1)
        if raw is None or raw.lower() in _NO_NUMBER:
            raw = None
            withheld = withheld or "unknown"
        h = None if withheld else self.handle(raw, _name(el.get("contact_name")))
        ext = _hid("call", f"{h[0]}:{h[1]}" if h else (raw or withheld), ms)
        self.present["calls"].add(ext)
        if kind == _CALL_BLOCKED:
            self.skip("blocked call (not kept)")
            return
        if kind not in _CALL_TYPES:
            self.odd(f"call type {el.get('type')!r}")
            return
        if ms is None:
            self.skip("call with no usable timestamp")
            return
        direction, label = _CALL_TYPES[kind]
        duration = max(_int(el.get("duration")) or 0, 0)
        participants: list[dict] = []
        self._add(participants, h, "sender" if direction == "in" else "recipient")
        self.record(stream="android_call", external_id=ext, ms=ms, direction=direction,
                    body=None, detail={"call": label, "number": withheld},
                    participants=participants, value=duration, unit="s",
                    ended_ms=ms + duration * 1000 if duration else None,
                    owner_role="recipient" if direction == "in" else "sender",
                    origin_app="phone")

    # --- the owner ----------------------------------------------------------

    def owner_from_backup(self) -> tuple[str | None, str | None]:
        """The phone's own number, as the backup itself reveals it: the
        sender of its sent MMS, or else the one number every received MMS was
        addressed to. (number, how it was found)."""
        if self.sent_from:
            return (max(sorted(self.sent_from), key=self.sent_from.get),
                    "the sender of this phone's sent MMS")
        if self.received_to and len(self.received_to) == 1:
            return next(iter(self.received_to)), "the one number every received MMS went to"
        return None, None

    def finish(self, owner: str | None) -> tuple[list[dict], list[dict]]:
        """(records, handles), with the owner's handle attached."""
        handles: dict[tuple[str, str], str | None] = {}
        for rec in self.records:
            role = rec.pop("_owner_role")
            if owner:
                self._add(rec["participants"], ("phone", owner), role)
            for p in rec["participants"]:
                h = (p["kind"], p["value"])
                if h not in handles:
                    handles[h] = OWNER_LABEL if owner and h == ("phone", owner) \
                        else self._label(h)
        records = sorted(self.records,
                         key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
        return records, [{"kind": k, "value": v, "display_name": label}
                         for (k, v), label in sorted(handles.items())]

    def _label(self, h: tuple[str, str]) -> str | None:
        """The name the phone showed most often for this handle."""
        names = self.names.get(h)
        if not names:
            return None
        return max(sorted(names), key=names.get)


# ---------------------------------------------------------------- one file

def _parse(stream, label: str, reader: _Reader) -> dict:
    """Read one backup file into `reader`; return what its root says."""
    src = _Repaired(stream)
    root = None
    info: dict = {"file": label}
    depth = 0
    handlers = {}
    try:
        for event, el in ET.iterparse(src, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    root = el
                    kind = _ROOTS.get(el.tag)
                    if kind is None:
                        raise DomainError(
                            f"{label} is not an SMS Backup & Restore file — its root "
                            f"element is <{el.tag}>, expected <smses> or <calls>")
                    info.update({"kind": kind, "type": _attr(el, "type"),
                                 "count": _int(el.get("count")),
                                 "backup_date": ms_epoch(el.get("backup_date")),
                                 "backup_set": _attr(el, "backup_set")})
                    handlers = ({"sms": reader.sms, "mms": reader.mms}
                                if kind == "texts" else {"call": reader.call})
                continue
            depth -= 1
            if depth != 1:
                continue
            fn = handlers.get(el.tag)
            if fn is None:
                reader.odd(f"<{el.tag}> element in {info['kind']} backup")
            else:
                fn(el)
            # Media makes these files huge: drop each element once read, so
            # memory holds one message at a time, not the backup.
            root.clear()
        src.drain()
    except ET.ParseError as e:
        line, col = getattr(e, "position", (0, 0))
        msg = str(e).split(":")[0]
        if getattr(e, "code", None) in _CUT_OFF:
            raise DomainError(f"{label} ends early (line {line}) — still uploading, or "
                              f"an interrupted backup? Nothing was imported") from e
        raise DomainError(f"{label} is not well-formed XML at line {line} column "
                          f"{col} ({msg}) — is it an SMS Backup & Restore file?") from e
    m = _APP_VERSION.search(src.head)
    info.update({"sha256": src.sha.hexdigest(),
                 "app_version": m.group(1).decode("ascii", "replace") if m else None,
                 "repaired": {"surrogate_pairs": src.pairs, "lone_surrogates": src.lone,
                              "invalid_refs_dropped": src.dropped}})
    return info


# ---------------------------------------------------------------- finding files

def _stamp(name: str, fallback: float) -> str:
    m = _STAMP.search(name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(fallback, tz=timezone.utc).strftime("%Y%m%d%H%M%S")


def _newest_in_folder(folder: Path) -> dict[str, Path]:
    """{'texts': newest sms-*.xml, 'calls': newest calls-*.xml}, either absent."""
    found: dict[str, Path] = {}
    for kind, pattern in _FILE_GLOBS.items():
        hits = [p for p in folder.iterdir()
                if p.is_file() and _glob_match(p.name, pattern)]
        if hits:
            found[kind] = max(hits, key=lambda p: (_stamp(p.name, p.stat().st_mtime),
                                                   p.name))
    return found


def _glob_match(file_name: str, pattern: str) -> bool:
    prefix = pattern.split("*")[0]
    low = file_name.lower()
    return low.startswith(prefix) and low.endswith(".xml")


def _newest_in_zip(zf: zipfile.ZipFile) -> dict[str, str]:
    found: dict[str, str] = {}
    for kind, pattern in _FILE_GLOBS.items():
        hits = [i for i in zf.infolist()
                if not i.is_dir() and _glob_match(Path(i.filename).name, pattern)]
        if hits:
            found[kind] = max(hits, key=lambda i: (
                _stamp(Path(i.filename).name,
                       datetime(*i.date_time, tzinfo=timezone.utc).timestamp()),
                i.filename)).filename
    return found


def search_places(cfg: Config) -> list[Path]:
    """Where the backups are looked for, in order: the configured folder, the
    app's OneDrive upload folders, then Downloads."""
    places = []
    folder = _settings(cfg)["folder"]
    if folder is not None:
        places.append(folder)
    for root in platforms.onedrive_candidates():
        places.extend(root / sub for sub in ONEDRIVE_FOLDERS)
    places.append(platforms.home() / "Downloads")
    return list(dict.fromkeys(places))


def default_target(cfg: Config) -> Path:
    """The folder holding the newest backup, looked for where it usually is."""
    folder = _settings(cfg)["folder"]
    if folder is not None and not folder.exists():
        raise DomainError(f"[connectors.android_sms] folder {folder} does not exist — "
                          f"fix the path, or pass the backup file or folder")
    looked, blocked = [], []
    for place in search_places(cfg):
        looked.append(str(place))
        try:
            if place.is_file():
                return place
            if place.is_dir() and _newest_in_folder(place):
                return place
        except OSError:          # EPERM: a Mac folder the terminal may not read
            blocked.append(str(place))
    hint = (f" ({', '.join(blocked)} could not be read — on a Mac, allow the "
            f"terminal access to that folder)") if blocked else ""
    raise DomainError(
        f"no SMS Backup & Restore file (sms-*.xml or calls-*.xml) found in "
        f"{'; '.join(looked)}{hint} — set [connectors.android_sms] folder, or pass "
        f"the file or folder (docs/android-texts.md)")


# ---------------------------------------------------------------- scan

def _read_target(target: Path, reader: _Reader) -> tuple[str, list[dict]]:
    """(target kind, per-file info) after reading every file the target holds."""
    try:
        if target.is_dir():
            files = _newest_in_folder(target)
            if not files:
                listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) \
                    or "(empty)"
                raise DomainError(f"{target.name} holds no sms-*.xml or calls-*.xml "
                                  f"(has: {listing})")
            infos = []
            for kind in ("texts", "calls"):
                if kind in files:
                    with open(files[kind], "rb") as f:
                        infos.append(_parse(f, files[kind].name, reader))
            return "folder", infos
        if zipfile.is_zipfile(target):
            with zipfile.ZipFile(target) as zf:
                members = _newest_in_zip(zf)
                if not members:
                    listing = ", ".join(zf.namelist()[:10]) or "(empty)"
                    raise DomainError(f"{target.name} holds no sms-*.xml or "
                                      f"calls-*.xml (members: {listing})")
                infos = []
                for kind in ("texts", "calls"):
                    if kind in members:
                        with zf.open(members[kind]) as f:
                            infos.append(_parse(f, Path(members[kind]).name, reader))
            return "zip", infos
        with open(target, "rb") as f:
            return "file", [_parse(f, target.name, reader)]
    except PermissionError as e:
        raise DomainError(f"cannot read {e.filename or target}: permission denied — "
                          f"on a Mac, allow the terminal access to that folder") from e
    except (OSError, zipfile.BadZipFile) as e:
        raise DomainError(f"cannot read {target.name}: {e}") from e


def _mirroring(info: dict, setting: bool | None, read: int) -> tuple[bool, str | None]:
    """Whether this file is the phone's whole state, and if not, why not."""
    what = "texts" if info["kind"] == "texts" else "calls"
    if setting is False:
        return False, f"{what}: mirror = false in [connectors.android_sms]"
    if info["count"] is not None and info["count"] != read:
        return False, (f"{what}: {info['file']} says it holds {info['count']} but "
                       f"has {read} — an incomplete file is not the phone's whole state")
    kind = (info["type"] or "").lower()
    if kind == "full":
        return True, None
    if kind:
        return False, (f"{what}: {info['file']} is marked type=\"{info['type']}\", not "
                       f"full — only a full backup is the phone's whole state")
    if setting is True:
        return True, None
    return False, (f"{what}: {info['file']} does not say whether it is a full backup — "
                   f"set mirror = true in [connectors.android_sms] once you know "
                   f"your scheduled backups are full (docs/android-texts.md)")


def _local_date(ms: int | None, cfg: Config) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).astimezone(
        ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")


def scan(target: Path, cfg: Config) -> base.Scan:
    settings = _settings(cfg)
    reader = _Reader(cfg)
    target_kind, infos = _read_target(target, reader)

    derived, how = reader.owner_from_backup()
    account, account_source = (settings["account"], "config.toml") if settings["account"] \
        else (derived, how)
    records, handles = reader.finish(account)

    present: set[str] = set()
    mirror_streams: list[str] = []
    not_mirroring: list[str] = []
    for info in infos:
        if info["kind"] == "calls":
            # The call log is not the phone's whole call history: Android keeps
            # only its newest calls (500 on stock Android) and drops older ones
            # by itself. A full backup missing an old call says nothing about
            # whether anyone deleted it, so mirroring would shrink the archive
            # to whatever the phone still lists. Calls are remembered.
            info["mirrored"] = False
            continue
        mirrors, why = _mirroring(info, settings["mirror"], reader.read[info["kind"]])
        info["mirrored"] = mirrors
        if mirrors:
            present |= reader.present[info["kind"]]
            mirror_streams += list(_MESSAGE_STREAMS)
        else:
            not_mirroring.append(why)

    texts = [r["journal_date"] for r in records if r["stream"] in _MESSAGE_STREAMS]
    calls = [r["journal_date"] for r in records if r["stream"] == "android_call"]
    names = {(h["kind"], h["value"]): h["display_name"] for h in handles}
    repaired = {k: sum(i["repaired"][k] for i in infos)
                for k in ("surrogate_pairs", "lone_surrogates", "invalid_refs_dropped")}
    facts = {
        "read_from": str(target.resolve()),
        "files": {i["kind"]: i["file"] for i in infos},
        "app_version": next((i["app_version"] for i in infos if i["app_version"]), None),
        "backups": {i["kind"]: {"type": i["type"], "count": i["count"],
                                "read": reader.read[i["kind"]],
                                "taken": _local_date(i["backup_date"], cfg),
                                # without an account nothing can be removed safely
                                "mirrored": i["mirrored"] and bool(account)}
                    for i in infos},
        "account_source": account_source,
        "oldest_message": min(texts) if texts else None,
        "newest_message": max(texts) if texts else None,
        "oldest_call": min(calls) if calls else None,
        "newest_call": max(calls) if calls else None,
        "normalized": [{"raw": raw, "handle": f"{h[0]}:{h[1]}", "name": names.get(h)}
                       for raw, h in sorted(reader.examples.items())[:5]],
        "repaired": repaired,
        "mms_date_units": reader.mms_units,
        "mms_apps": dict(sorted(reader.mms_apps.items())),
        "rcs_like": reader.rcs_like,
        "group_names_unaligned": reader.unaligned,
    }
    source_hash = hashlib.sha256("".join(
        f"{i['kind']}:{i['sha256']};" for i in infos).encode()).hexdigest()
    extra = {"present_ids": present, "mirror_streams": sorted(mirror_streams)} \
        if mirror_streams else {}
    return base.empty_scan(
        name,
        account=account,
        source_file=" + ".join(i["file"] for i in infos),
        source_hash=source_hash,
        records=records,
        handles=handles,
        unmapped=[{"what": k, "count": v} for k, v in sorted(reader.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(reader.skipped.items())],
        facts=facts,
        target_kind=target_kind,
        not_mirroring=not_mirroring,
        named_handles=sum(1 for h in handles if h["display_name"]
                          and h["display_name"] != OWNER_LABEL),
        **extra)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    who = "you" if rec["direction"] == "out" else "them"
    if rec["stream"] == "android_call":
        d = json.loads(rec["value_json"])
        secs = int(rec["value"] or 0)
        length = f", {secs // 60}m{secs % 60:02d}s" if secs else ""
        what = {"in": "call", "out": "call"}.get(d.get("call"), f"{d.get('call')} call")
        return f"{rec['journal_date']} {who}: {what}{length}"
    body = rec["body"]
    if body is None:
        d = json.loads(rec["value_json"]) if rec["value_json"] else {}
        body = "[" + ", ".join(a.get("mime") or "attachment"
                               for a in d.get("attachments") or []) + "]"
    body = " ".join(body.split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def _thread_label(row) -> str | None:
    if not row["value_json"]:
        return None
    d = json.loads(row["value_json"])
    if d.get("thread"):
        return d["thread"]
    return d.get("call") and f"{d['call']} call"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    head = f"android-sms: {scan['source_file']} ({scan['target_kind']}"
    if facts.get("app_version"):
        head += f"; SMS Backup & Restore v{facts['app_version']}"
    lines = [head + ")"]
    if facts.get("read_from"):
        lines.append(f"  read from: {facts['read_from']}")
    for kind, b in (facts.get("backups") or {}).items():
        what = "message(s)" if kind == "texts" else "call(s)"
        lines.append(f"  {kind}: {b['type'] or 'type not stated'} backup"
                     + (f" taken {b['taken']}" if b.get("taken") else "")
                     + f", {b['read']} {what}"
                     + (f" (file says {b['count']})" if b["count"] not in (None, b["read"])
                        else "")
                     + (" — deletions on the phone are mirrored" if b["mirrored"] else "")
                     + (" — kept even once the phone's call log drops them"
                        if kind == "calls" else ""))
    if scan["account"]:
        source = facts.get("account_source")
        lines.append(f"  account: {scan['account']}"
                     + (f" (from {source})" if source else ""))
    else:
        lines.append("  account: unknown — no sent MMS names this phone's number; set "
                     "account = \"+1…\" under [connectors.android_sms] in config.toml")
    if facts.get("oldest_message"):
        lines.append(f"  messages span {facts['oldest_message']}..{facts['newest_message']} "
                     f"— check the oldest against what the phone shows")
    if facts.get("normalized"):
        lines.append("  normalized, e.g.: " + "; ".join(
            f"{n['raw']} -> {n['handle'].split(':', 1)[1]}"
            + (f" ({n['name']})" if n.get("name") else "")
            for n in facts["normalized"]))
    rep = facts.get("repaired") or {}
    fixes = []
    if rep.get("surrogate_pairs"):
        fixes.append(f"{rep['surrogate_pairs']} emoji written as UTF-16 surrogate pairs")
    if rep.get("lone_surrogates"):
        fixes.append(f"{rep['lone_surrogates']} broken half-emoji (now U+FFFD)")
    if rep.get("invalid_refs_dropped"):
        fixes.append(f"{rep['invalid_refs_dropped']} control-character reference(s) "
                     f"dropped")
    if fixes:
        lines.append("  repaired: " + ", ".join(fixes))

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(latest)}")
    if not by_stream and not plan.get("removed"):
        lines.append("  nothing new — the backup matches the database")
    for why in scan.get("not_mirroring") or ():
        lines.append(f"  not mirroring deletions: {why}")
    lines += base.removal_lines(plan, "no longer on the phone (deleted)", _thread_label)

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate(s) inside the backup")
    if tail:
        lines.append("  " + ", ".join(tail))

    people = [h for h in scan["handles"] if h["display_name"] != OWNER_LABEL]
    if people:
        lines.append(f"  handles: {len(people)} ({scan['named_handles']} named by the "
                     f"phone's contacts) — review with 'lifecrm handles list --suggest'")
    if facts.get("rcs_like"):
        lines.append(f"  rcs: {facts['rcs_like']} MMS carry a 'proto:' transaction id, "
                     f"which Google Messages is reported to give RCS chats it files as "
                     f"MMS — imported as android_mms (unverified; docs/android-texts.md)")
    if facts.get("group_names_unaligned"):
        lines.append(f"  {facts['group_names_unaligned']} group MMS whose contact names "
                     f"did not line up with their numbers — those numbers are unnamed")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:20]}…: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}")
    return lines
