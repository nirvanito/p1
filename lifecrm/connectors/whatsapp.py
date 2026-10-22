"""WhatsApp connector: the "Export chat" text file, from Android or iPhone.

WhatsApp has no API for its history, and its own backups are encrypted. What
both phones do have is *Export chat*: one chat at a time, as plain text. The
iPhone shares a 'WhatsApp Chat - <name>.zip' holding '_chat.txt'; Android
shares 'WhatsApp Chat with <name>.txt', zipped together with the media when
media is included. This connector reads those files. It never opens
WhatsApp's databases, never talks to the phone, and never sends anything.

Accepted targets
----------------
- one export: the .zip, the .txt, or the folder an unzipped export leaves
- a folder holding several exports (zips, text files, unzipped folders),
  each read as its own chat; files that are not WhatsApp exports are ignored
- nothing at all: `default_target` uses [connectors.whatsapp] exports_dir,
  else ~/Downloads (where AirDrop, Quick Share and browsers put things) when
  it holds an export

The text format
---------------
Every message starts with a timestamp in the phone's own locale, and nothing
in the file says which locale:

  iPhone    [27/07/2026, 21:30:05] Jake Morales: see you at 7
            [7/27/26, 9:30:05 PM] Jake Morales: see you at 7
  Android   27/07/2026, 21:30 - Jake Morales: see you at 7
            7/27/26, 9:30 PM - Jake Morales: see you at 7

A line that does not start with a timestamp continues the message above it.
Day-first vs month-first is decided per file from all of its dates: a first
field over 12 can only be a day, a second field over 12 can only be a day
written month-first. A file whose every date fits both ways is read by
[connectors.whatsapp] date_order, else day-first — and the report says it
had to guess, before anything is written.

The iPhone marks attachments and system lines with an invisible LEFT-TO-RIGHT
MARK, current phones put a narrow no-break space before AM/PM, and an iPhone
wraps an unsaved contact's number in direction marks and non-breaking
hyphens. All of that is undone here, so a sender reads the same from either
phone.

What comes in
-------------
One observation per message, stream 'whatsapp', titled with the chat name so
every row is self-describing and search finds a chat by its name.

- System lines — the encryption notice, 'X created group', 'X added Y', a
  security-code change, call notices — are not messages. They are counted in
  the report and never imported.
- 'This message was deleted' is counted, not imported: nothing is left of it.
- A media message is kept. When the file is in the export, media_path is its
  path relative to the export (never the bytes); an export made "without
  media" keeps the message, marked media_omitted.
- '<This message was edited>' is taken off the text and recorded as edited.

Identity
--------
The export carries no message ids, so one is derived: 'wa:' + SHA-1 of the
chat name, the instant (UTC), the sender's handle, the text, and the ordinal
among messages identical in all four — two "ok"s sent in the same Android
minute stay two messages. It is stable across re-exports of the same chat,
so re-importing is a no-op. Two consequences, documented in docs/whatsapp.md:
renaming a chat (or the contact a one-to-one chat is named after) before the
next export makes its messages new again, and an edit made after an import
arrives as a new row beside the old wording.

Senders are handles, never names
--------------------------------
The export names a sender only as the phone displays them: the contact's
name, or the number of someone not in the contacts. A phone-shaped sender
becomes a phone handle, so it meets the same number from texts and calls.
Anything else becomes app_account 'whatsapp:<name as shown>' with that name
as its display_name — the export's only identifier for that person. Linking
it to an entity stays the owner's decision (`handles list --suggest`).

Which sender is you: [connectors.whatsapp] me = "Alex Rivera" (a name, or a
list of them, as your own lines show it). Without it, a one-to-one chat gives
it away — the sender who is not the chat's name — as does the one sender
common to every chat in a folder. Otherwise direction stays unknown, and the
report says so loudly: direction is recorded once, and a later change to it
is a conflict, never applied silently.

Deletion semantics — the archive remembers
------------------------------------------
Texts and AI chats mirror their source (owner's decision, 2026-09-24),
because each of those scans is the account's complete current state. A
WhatsApp export is not: it is one chat, capped by WhatsApp's export limits,
so an older export can hold messages a newer one has dropped without anyone
deleting them, and a message deleted for everyone stays in place as 'This
message was deleted'. Nothing in an export tells a deletion from truncation,
so this connector declares no `present_ids` and removes nothing. A message
deleted in WhatsApp stays archived; `imports retract` undoes an import.

Known limitations, by design
----------------------------
- Times carry no zone. WhatsApp writes them in the phone's zone at the
  moment of export; they are read in cfg.timezone, or in
  [connectors.whatsapp] timezone for an export made elsewhere.
- Android timestamps have no seconds; such messages land on the minute.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "whatsapp"
streams = ("whatsapp",)

IOS_TEXT = "_chat.txt"
LRM = "\N{LEFT-TO-RIGHT MARK}"
# Invisible marks WhatsApp writes around names and markers: LRM/RLM, the bidi
# embeddings an iPhone wraps a phone number in, isolates, a stray BOM.
_MARKS = "".join(map(chr, (0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                           0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF)))
# Hyphens an iPhone writes inside a phone number (U+2011 non-breaking, ...).
_DASHES = dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-")
_ORDER_WORDS = {"dmy": "day-first", "mdy": "month-first", "ymd": "year-first"}
_ORDER_SETTINGS = {"dmy": "dmy", "day-first": "dmy", "mdy": "mdy",
                   "month-first": "mdy", "ymd": "ymd", "year-first": "ymd"}

# 'WhatsApp Chat with Jake.txt' (Android), 'WhatsApp Chat - Jake.zip' (iPhone),
# the folder an unzipped export leaves, and the ' (1)' a browser appends to a
# second download of the same name.
_EXPORT_NAME = re.compile(
    r"WhatsApp Chat (?:with |- )(?P<chat>.+?)(?: \(\d+\))?(?P<ext>\.txt|\.zip)?", re.I)


# ---------------------------------------------------------------- finding exports

def _chat_name(file_or_folder: str) -> str:
    """The chat's name from an export's own file or folder name."""
    m = _EXPORT_NAME.fullmatch(file_or_folder)
    if m:
        return m["chat"].strip()
    stem = re.sub(r"\.(txt|zip)$", "", file_or_folder, flags=re.I)
    return re.sub(r" \(\d+\)$", "", stem).strip() or file_or_folder


def _is_chat_text(file_name: str) -> bool:
    if file_name == IOS_TEXT:
        return True
    m = _EXPORT_NAME.fullmatch(file_name)
    return bool(m and (m["ext"] or "").lower() == ".txt")


def _media_beside(names: list[str], text_member: str) -> dict[str, str]:
    """basename (casefolded) -> path relative to the chat text's folder, for
    every other file in the export. WhatsApp writes media flat beside the
    text; a path, not a name, is kept so a nested layout still resolves."""
    parent = PurePosixPath(text_member).parent
    out: dict[str, str] = {}
    for n in sorted(names):
        p = PurePosixPath(n)
        if n == text_member or _is_chat_text(p.name):
            continue
        try:
            rel = p.relative_to(parent)
        except ValueError:
            continue
        out.setdefault(p.name.casefold(), rel.as_posix())
    return out


def _from_zip(path: Path, label: str) -> list[dict]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DomainError(f"cannot open {path.name} as a zip: {e}") from e
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        texts = [n for n in names if _is_chat_text(PurePosixPath(n).name)]
        if not texts:
            listing = ", ".join(names[:10]) or "(empty)"
            raise DomainError(
                f"{path.name} holds no WhatsApp chat text — expected {IOS_TEXT} "
                f"or 'WhatsApp Chat with <name>.txt' (members: {listing})")
        out = []
        for member in sorted(texts):
            p = PurePosixPath(member)
            if p.name == IOS_TEXT:
                folder = p.parent.name
                chat = _chat_name(folder) if folder else _chat_name(path.name)
            else:
                chat = _chat_name(p.name)
            out.append({"chat": chat, "raw": zf.read(member),
                        "label": label if len(texts) == 1 else f"{label}/{member}",
                        "media": _media_beside(names, member)})
    return out


def _from_text(path: Path, label: str) -> dict:
    folder = path.parent
    chat = _chat_name(folder.name) if path.name == IOS_TEXT else _chat_name(path.name)
    names = sorted(p.name for p in folder.iterdir() if p.is_file())
    return {"chat": chat, "raw": path.read_bytes(), "label": label,
            "media": _media_beside(names, path.name)}


def _find_exports(target: Path) -> tuple[list[dict], str]:
    """[{chat, raw, label, media}] and what kind of target it was."""
    if target.is_file():
        if zipfile.is_zipfile(target):
            return _from_zip(target, target.name), "zip"
        if target.suffix.lower() == ".txt":
            return [_from_text(target, target.name)], "text"
        raise DomainError(
            f"{target.name} is not a WhatsApp chat export — expected the .zip or "
            f".txt that 'Export chat' made, or a folder of them")
    if not target.is_dir():
        raise DomainError(f"not found: {target}")
    if (target / IOS_TEXT).is_file():
        return [_from_text(target / IOS_TEXT, target.name)], "folder"
    found: list[dict] = []
    entries = sorted(target.iterdir(), key=lambda p: p.name)
    for p in entries:
        if p.is_file() and _EXPORT_NAME.fullmatch(p.name):
            if p.suffix.lower() == ".zip" and zipfile.is_zipfile(p):
                found += _from_zip(p, p.name)
            elif p.suffix.lower() == ".txt":
                found.append(_from_text(p, p.name))
        elif p.is_dir():
            if (p / IOS_TEXT).is_file():
                found.append(_from_text(p / IOS_TEXT, f"{p.name}/{IOS_TEXT}"))
            elif _EXPORT_NAME.fullmatch(p.name):
                for q in sorted(p.iterdir(), key=lambda q: q.name):
                    if q.is_file() and _is_chat_text(q.name):
                        found.append(_from_text(q, f"{p.name}/{q.name}"))
    if not found:
        listing = ", ".join(p.name for p in entries[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no WhatsApp chat export — looked for "
            f"'WhatsApp Chat …' .zip/.txt files and folders with {IOS_TEXT} "
            f"(has: {listing})")
    return found, "folder"


def _source_hash(target: Path, exports: list[dict]) -> str:
    """The file's own hash; for a folder, one hash over every chat text and
    the names of the media beside it — media arriving later changes what
    media_path can say, so it must not look like an unchanged folder."""
    if target.is_file():
        return base.file_hash(target)
    h = hashlib.sha256()
    for e in sorted(exports, key=lambda e: e["label"]):
        h.update(e["label"].encode() + b"\0" + hashlib.sha256(e["raw"]).digest())
        h.update("\0".join(sorted(e["media"].values())).encode())
    return h.hexdigest()


def _has_exports(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(
            _EXPORT_NAME.fullmatch(p.name) or (p / IOS_TEXT).is_file()
            for p in folder.iterdir())
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.whatsapp] exports_dir, else ~/Downloads when it holds a
    chat export: AirDrop on a Mac, Quick Share on Windows and every browser
    put a shared export there."""
    configured = cfg.connector_settings(name).get("exports_dir")
    if configured:
        p = Path(str(configured)).expanduser()
        p = p if p.is_absolute() else cfg.root / p
        if not p.is_dir():
            raise DomainError(f"[connectors.whatsapp] exports_dir {p} is not a folder")
        return p
    downloads = Path.home() / "Downloads"
    if _has_exports(downloads):
        return downloads
    raise DomainError(
        f"no WhatsApp chat export found in {downloads} — export a chat "
        f"(docs/whatsapp.md) and pass the .zip or .txt, or set "
        f"[connectors.whatsapp] exports_dir")


# ---------------------------------------------------------------- lines and dates

_DATE = r"(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4})"
# \s covers the no-break and narrow no-break spaces phones put before AM/PM.
_TIME = r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?)"
_HEADERS = {
    "iphone": re.compile(r"\[" + _DATE + r",?\s" + _TIME + r"\]\s?(?P<rest>.*)"),
    "android": re.compile(_DATE + r",?\s" + _TIME + r"\s[-\N{EN DASH}]\s(?P<rest>.*)"),
}
_CLOCK = re.compile(r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s?(?:([AaPp])\.?\s?[Mm]\.?)?")


def _decode(raw: bytes, label: str) -> str:
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DomainError(f"{label} is not valid UTF-8: {e}") from e


def _messages(text: str, label: str) -> tuple[list[dict], str, int]:
    """Header lines with their continuation lines, the format (iphone or
    android), and how many lines came before the first timestamp.

    The format is fixed by the first header line: a continuation line that
    happens to look like the *other* phone's header stays continuation."""
    msgs: list[dict] = []
    flavor = None
    preamble = 0
    for n, line in enumerate(re.split(r"\r\n|\r|\n", text), 1):
        bare = line.lstrip(_MARKS)
        m = None
        if flavor is None:
            for f, rx in _HEADERS.items():
                m = rx.fullmatch(bare)
                if m:
                    flavor = f
                    break
        else:
            m = _HEADERS[flavor].fullmatch(bare)
        if m:
            msgs.append({"line": n, "date": m["date"], "time": m["time"],
                         "rest": m["rest"], "more": [],
                         "marked": bare != line})
        elif msgs:
            msgs[-1]["more"].append(line)
        elif line.strip(_MARKS + " \t"):
            preamble += 1
    if not msgs:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")[:60]
        raise DomainError(
            f"{label} has no WhatsApp message lines — expected lines like "
            f"'[27/07/2026, 21:30:05] Name: text' or '27/07/2026, 21:30 - Name: "
            f"text'; first line reads: {first!r}")
    return msgs, flavor, preamble


def _date_order(msgs: list[dict], setting: str | None,
                label: str) -> tuple[str, str, str | None]:
    """(order, how it was decided, warning or None) for one file."""
    parts = [re.split(r"[./-]", m["date"]) for m in msgs]
    if any(len(p[0]) == 4 for p in parts):
        return "ymd", "the year comes first", None
    day_first = next((p for p in parts if int(p[0]) > 12), None)
    month_first = next((p for p in parts if int(p[1]) > 12), None)
    if day_first and month_first:
        raise DomainError(
            f"{label}: dates fit neither order — {'/'.join(day_first)} is "
            f"day-first but {'/'.join(month_first)} is month-first; is this one "
            f"chat export?")
    evidence = "dmy" if day_first else "mdy" if month_first else None
    if evidence:
        warning = None
        if setting and setting != evidence:
            warning = (f"{label}: [connectors.whatsapp] date_order ignored — its "
                       f"dates are unambiguously {_ORDER_WORDS[evidence]}")
        return evidence, f"e.g. {'/'.join(day_first or month_first)}", warning
    if setting:
        return setting, "[connectors.whatsapp] date_order", None
    return "dmy", "guessed", (
        f"{label}: every date fits both day-first and month-first — read as "
        f"day-first; if the phone writes the month first, set "
        f"[connectors.whatsapp] date_order = \"mdy\" before --apply")


def _local(msg: dict, order: str, label: str) -> datetime:
    """Naive local wall-clock time of a header line."""
    a, b, c = (int(x) for x in re.split(r"[./-]", msg["date"]))
    year, month, day = {"dmy": (c, b, a), "mdy": (c, a, b), "ymd": (a, b, c)}[order]
    if year < 100:
        year += 2000
    clock = _CLOCK.fullmatch(msg["time"])
    try:
        hour, minute = int(clock[1]), int(clock[2])
        second = int(clock[3]) if clock[3] else 0
        if clock[4]:
            if not 1 <= hour <= 12:
                raise ValueError("hour out of range for AM/PM")
            hour = hour % 12 + (12 if clock[4] in "Pp" else 0)
        return datetime(year, month, day, hour, minute, second)
    except (TypeError, ValueError) as e:
        raise DomainError(
            f"{label} line {msg['line']}: '{msg['date']}, {msg['time']}' is not a "
            f"valid {_ORDER_WORDS[order]} date and time ({e})") from e


# ---------------------------------------------------------------- senders

def clean_label(raw: str) -> str:
    """A sender as the phone displayed it, without the invisible marks — and
    without the '~' WhatsApp puts before a name someone chose for themself."""
    s = "".join(ch for ch in raw if ch not in _MARKS)
    s = " ".join(s.replace("\N{NO-BREAK SPACE}", " ")
                 .replace("\N{NARROW NO-BREAK SPACE}", " ").split())
    return s.lstrip("~").strip()


def sender_handle(label: str) -> tuple[str, str]:
    """A phone-shaped sender is a phone handle (it meets the same number from
    texts and calls); anything else is the name the export shows, which is
    the only identifier it has."""
    phoneish = label.translate(_DASHES)
    digits = re.sub(r"\D", "", phoneish)
    if re.fullmatch(r"\+?[\d\s().\-]+", phoneish) and len(digits) >= 7:
        return ("phone", base.normalize_phone(phoneish))
    return ("app_account", f"whatsapp:{label}")


def _key(h: tuple[str, str]) -> tuple[str, str]:
    return (h[0], h[1].casefold())


# A sender candidate holding quotes or a group verb is a system line whose
# own text contains ': ' — 'X changed the subject from "Plans: June" to ...'.
_NOT_A_SENDER = re.compile(r"[\"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}]"
                           r"| (?:created|changed|added|removed) ")
_SENDER = re.compile(r"(?P<sender>[^:]{1,120}?):(?: (?P<text>.*))?")


def _split(rest: str) -> tuple[str | None, str]:
    m = _SENDER.fullmatch(rest)
    if not m or _NOT_A_SENDER.search(m["sender"]):
        return None, rest
    label = clean_label(m["sender"])
    return (label or None), (m["text"] or "")


# ---------------------------------------------------------------- what a line is

_ENCRYPTION = re.compile(
    r"(?:Messages and calls are end-to-end encrypted|Messages (?:to|you send to) "
    r"this (?:group|chat) are now secured with end-to-end encryption)", re.I)
_SYSTEM_KINDS = (
    ("encryption notice", re.compile(r"end-to-end encrypt", re.I)),
    ("security code change", re.compile(r"security code", re.I)),
    ("number change", re.compile(r"changed (?:their|his|her|your) (?:phone )?number"
                                 r"|changed to \+?\d|new phone number", re.I)),
    ("disappearing messages", re.compile(r"disappearing messages|message timer", re.I)),
    ("business notice", re.compile(r"business account|official business"
                                   r"|secure service from (?:Meta|Facebook)", re.I)),
    ("call notice", re.compile(r"^(?:missed )?(?:group )?(?:voice|video) call\b", re.I)),
    ("group change", re.compile(
        r"\bcreated (?:group|this group)\b| added | added$| removed | removed$"
        r"| left$|\bjoined using\b|\bchanged the (?:subject|group|settings)"
        r"|\bchanged this group|\bdeleted this group|\bnow an admin\b"
        r"|\bno longer an admin\b|\bpinned a message\b|\breset this group", re.I)),
)
_DELETED = re.compile(r"(?:This message was deleted|You deleted this message)\.?", re.I)
_WAITING = re.compile(r"Waiting for this message\b.*", re.I | re.S)
_EDITED = re.compile(r"\s*<This message was edited>\s*$")
_ATTACHED = re.compile(r"<attached: (?P<file>[^>\n]+)>")
_FILE_ATTACHED = re.compile(r"(?P<file>[^\n]+?) \(file attached\)(?:\n|$)")
_OMITTED_IOS = re.compile(r"(?:.*?\s)?(?P<what>image|video note|video|audio|sticker|"
                          r"GIF|document|Contact card|voice message) omitted", re.I | re.S)
_OMITTED_ANDROID = re.compile(r"<(?P<what>[^<>]*?) ?omitted>", re.I)


def _system_kind(text: str) -> str:
    for kind, rx in _SYSTEM_KINDS:
        if rx.search(text):
            return kind
    return "other"


def _is_media_line(t: str) -> bool:
    return bool(_ATTACHED.search(t) or _OMITTED_IOS.fullmatch(t)
                or _OMITTED_ANDROID.fullmatch(t) or _DELETED.fullmatch(t))


def _media_kind(file_name: str | None, what: str | None = None) -> str | None:
    """image / video / audio / voice / sticker / gif / document / contact,
    from an attachment's name or an 'omitted' marker's wording."""
    if what is not None:
        w = what.lower()
        for word, kind in (("gif", "gif"), ("sticker", "sticker"), ("voice", "voice"),
                           ("video", "video"), ("audio", "audio"), ("image", "image"),
                           ("photo", "image"), ("document", "document"),
                           ("contact", "contact")):
            if word in w:
                return kind
        return None
    u = (file_name or "").upper()
    for tag, kind in (("-PHOTO-", "image"), ("-VIDEO-", "video"), ("-AUDIO-", "audio"),
                      ("-STICKER-", "sticker"), ("-GIF-", "gif"), ("-CONTACT-", "contact")):
        if tag in u:
            return kind
    for prefix, kind in (("IMG-", "image"), ("VID-", "video"), ("PTT-", "voice"),
                         ("AUD-", "audio"), ("STK-", "sticker"), ("DOC-", "document")):
        if u.startswith(prefix):
            return kind
    ext = u.rsplit(".", 1)[-1] if "." in u else ""
    if ext in ("JPG", "JPEG", "PNG", "HEIC", "WEBP"):
        return "image"
    if ext in ("MP4", "MOV", "3GP"):
        return "video"
    if ext in ("OPUS", "M4A", "MP3", "AAC", "OGG"):
        return "audio"
    if ext == "VCF":
        return "contact"
    return "document"


def _read_chat(export: dict, order_setting: str | None, zone: ZoneInfo) -> dict:
    """One export -> its messages, system lines and skips. Nothing here
    depends on which sender is you; that is decided across every chat."""
    label = export["label"]
    msgs, flavor, preamble = _messages(_decode(export["raw"], label), label)
    order, how, warning = _date_order(msgs, order_setting, label)
    chat = {"name": export["chat"], "key": _key(sender_handle(export["chat"])),
            "label": label, "flavor": flavor, "order": order, "order_how": how,
            "warnings": [warning] if warning else [], "lines": len(msgs),
            "preamble": preamble, "items": [], "system": {}, "skipped": {},
            "senders": {}, "plain_senders": set(), "group_change": False,
            "media": export["media"]}

    def system(kind: str) -> None:
        chat["system"][kind] = chat["system"].get(kind, 0) + 1
        if kind == "group change":
            chat["group_change"] = True

    def skip(reason: str) -> None:
        chat["skipped"][reason] = chat["skipped"].get(reason, 0) + 1

    for msg in msgs:
        when = _local(msg, order, label).replace(tzinfo=zone).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sender, text = _split(msg["rest"])
        if msg["more"]:
            text = "\n".join([text, *msg["more"]])
        if sender is None:
            # Android writes system lines with no sender at all.
            system(_system_kind("\n".join([msg["rest"], *msg["more"]]).lstrip(_MARKS)))
            continue
        # The iPhone's mark on media and system lines: before the '[', before
        # the text, or both.
        lead = msg["marked"] or (bool(text) and text[0] in _MARKS)
        t = text.replace(LRM, "").strip()
        if _ENCRYPTION.match(t):
            system("encryption notice")
            continue
        if lead and not _is_media_line(t):
            kind = _system_kind(t)
            if kind != "other":
                system(kind)
                continue
        if _DELETED.fullmatch(t):
            skip("deleted message ('This message was deleted')")
            continue
        if _WAITING.fullmatch(t):
            skip("message the phone never received ('Waiting for this message')")
            continue
        if t == "null" and flavor == "android":
            skip("view-once media ('null')")
            continue
        edited = bool(_EDITED.search(t))
        t = _EDITED.sub("", t).strip()
        files = [f.strip() for f in _ATTACHED.findall(t)]
        body = _ATTACHED.sub("", t).strip()
        if not files:
            m = _FILE_ATTACHED.match(t)
            if m:
                files = [m["file"].strip()]
                body = t[m.end():].strip()
        omitted = None
        if not files:
            m = _OMITTED_ANDROID.fullmatch(t) or _OMITTED_IOS.fullmatch(t)
            # A document's name and page count may precede 'document omitted',
            # but only on a marked line: unmarked, "...the audio omitted" is
            # something a person wrote.
            if m and (lead or m.start("what") <= 1):
                omitted, body = m["what"], ""
        if not body and not files and omitted is None:
            skip("empty message")
            continue
        h = sender_handle(sender)
        chat["senders"].setdefault(_key(h), (h, sender))
        if not lead:
            chat["plain_senders"].add(_key(h))
        chat["items"].append({
            "line": msg["line"], "when": when, "sender": _key(h), "lead": lead,
            "key_text": t, "body": body or None, "edited": edited,
            "files": files, "omitted": omitted})
    return chat


def _voices(chat: dict) -> set:
    """The chat's senders, less the chat itself: an iPhone writes a group's
    system lines as sent by the group's own name, always with its mark."""
    keys = set(chat["senders"])
    if chat["key"] in keys and chat["key"] not in chat["plain_senders"]:
        keys.discard(chat["key"])
    return keys


# ---------------------------------------------------------------- who is you

def _settings(cfg: Config) -> tuple[list[str], str | None, str]:
    s = cfg.connector_settings(name)
    me = s.get("me")
    if me is None or me == "":
        names: list[str] = []
    elif isinstance(me, str):
        names = [me]
    elif isinstance(me, list) and all(isinstance(x, str) for x in me):
        names = [x for x in me if x.strip()]
    else:
        raise DomainError(f"[connectors.whatsapp] me must be your name as your own "
                          f"lines show it, or a list of names — got {me!r}")
    order = s.get("date_order")
    if order is not None:
        if str(order).lower() not in _ORDER_SETTINGS:
            raise DomainError(f"[connectors.whatsapp] date_order must be \"dmy\" "
                              f"(day-first) or \"mdy\" (month-first) — got {order!r}")
        order = _ORDER_SETTINGS[str(order).lower()]
    zone = s.get("timezone") or cfg.timezone
    try:
        ZoneInfo(str(zone))
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise DomainError(f"[connectors.whatsapp] timezone '{zone}' is not a known "
                          f"IANA zone (e.g. America/Chicago)") from e
    return names, order, str(zone)


def _owner(chats: list[dict], me: list[str]) -> dict:
    """{'keys': handle keys that are you, 'handle', 'label', 'source', 'warning'}."""
    seen: dict = {}
    for c in chats:
        for k, v in c["senders"].items():
            seen.setdefault(k, v)
    you = {k for k in seen if k == ("app_account", "whatsapp:you")}
    if me:
        labels = [clean_label(x) for x in me]
        keys = {_key(sender_handle(x)) for x in labels} | you
        hit = next((k for k in sorted(keys) if k in seen), None)
        h, label = seen[hit] if hit else (sender_handle(labels[0]), labels[0])
        warning = None if hit else (
            f"no sender in this export is [connectors.whatsapp] me "
            f"({', '.join(labels)}) — check it matches your own lines exactly")
        return {"keys": keys, "handle": h, "label": label,
                "source": "[connectors.whatsapp] me", "warning": warning}

    candidates: dict = {}
    for c in chats:
        voices = _voices(c)
        if not c["group_change"] and len(voices) == 2 and c["key"] in voices:
            for k in voices - {c["key"]}:
                candidates.setdefault(k, c["name"])
    found = source = None
    if len(candidates) == 1:
        found = next(iter(candidates))
        source = (f"inferred: the other sender in the one-to-one chat with "
                  f"{candidates[found]}; set [connectors.whatsapp] me to be sure")
    elif not candidates and len(chats) >= 2:
        common = set.intersection(*(_voices(c) for c in chats))
        if len(common) == 1:
            found = next(iter(common))
            source = ("inferred: the one sender in every chat; set "
                      "[connectors.whatsapp] me to be sure")
    if found is None and you and len(candidates) <= 1:
        found, source = next(iter(you)), "the export labels your lines 'You'"
    if found is not None:
        h, label = seen[found]
        return {"keys": {found} | you, "handle": h, "label": label,
                "source": source, "warning": None}
    why = ("one-to-one chats disagree about which sender is you"
           if len(candidates) > 1 else "nothing in the export says which sender is you")
    return {"keys": set(), "handle": None, "label": None, "source": None,
            "warning": (f"{why} — direction stays unknown, and changing it after "
                        f"--apply is a conflict, not an update; set "
                        f"[connectors.whatsapp] me = \"<your name as your lines "
                        f"show it>\" first")}


def _is_group(chat: dict, owner_keys: set) -> bool:
    """A one-to-one chat is named after the other person, so their lines
    carry the chat's name; any other sender besides you means a group. With
    you unknown, a lone sender who is not the chat's name is taken as a
    group member — that never invents a handle named after the chat."""
    if chat["group_change"]:
        return True
    others = _voices(chat) - owner_keys
    if owner_keys:
        return len(others) > 1 or (len(others) == 1 and chat["key"] not in others)
    if chat["key"] in others:
        return len(others) > 2
    return bool(others)


# ---------------------------------------------------------------- scan

def _external_id(chat: str, when: str, sender: tuple[str, str], text: str,
                 ordinal: int) -> str:
    raw = "\x1f".join((chat.casefold(), when, f"{sender[0]}:{sender[1]}", text,
                       str(ordinal)))
    return "wa:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def scan(target: Path, cfg: Config) -> base.Scan:
    me, order_setting, zone_name = _settings(cfg)
    try:
        exports, target_kind = _find_exports(target)
        source_hash = _source_hash(target, exports)
    except OSError as e:
        raise DomainError(f"cannot read {target.name}: {e}") from e
    zone = ZoneInfo(zone_name)
    chats = [_read_chat(e, order_setting, zone) for e in exports]
    owner = _owner(chats, me)
    owner_keys = owner["keys"]

    # One spelling per handle across every chat in the scan: the first seen.
    handles: dict[tuple[str, str], dict] = {}

    def seen(h: tuple[str, str], label: str | None) -> dict:
        k = _key(h)
        if k not in handles:
            handles[k] = {"kind": h[0], "value": h[1],
                          "display_name": label if h[0] == "app_account" else None}
        return {"kind": handles[k]["kind"], "value": handles[k]["value"]}

    owner_part = seen(owner["handle"], owner["label"]) if owner["handle"] else None
    records: list[dict] = []
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    media = {"present": 0, "missing": 0, "omitted": 0}
    chat_facts = []
    warnings = [w for c in chats for w in c["warnings"]]
    if owner["warning"]:
        warnings.append(owner["warning"])

    for c in chats:
        group = _is_group(c, owner_keys)
        voices = _voices(c) if group else set(c["senders"])
        members = {k: seen(*c["senders"][k]) for k in sorted(voices)}
        if group or not c["items"]:
            counterpart_key, counterpart = None, None
        elif c["key"] in members:
            counterpart_key, counterpart = c["key"], members[c["key"]]
        elif len(set(members) - owner_keys) == 1:
            counterpart_key = next(iter(set(members) - owner_keys))
            counterpart = members[counterpart_key]
        else:
            # The other person never wrote: the chat's name is all there is.
            counterpart_key = c["key"]
            counterpart = seen(sender_handle(c["name"]), c["name"])
        ordinals: dict[tuple, int] = {}
        kept = []
        for it in c["items"]:
            if it["sender"] not in members:
                # A group's own name as sender: a system line this connector
                # has no pattern for. Still a system line, never a message.
                c["system"]["other"] = c["system"].get("other", 0) + 1
                continue
            sender = members[it["sender"]]
            mine = it["sender"] in owner_keys
            if group:
                recipients = [m for k, m in members.items() if k != it["sender"]]
                if owner_part and not mine and owner_part not in recipients:
                    recipients.append(owner_part)
            else:
                other = owner_part if it["sender"] == counterpart_key else counterpart
                recipients = [other] if other and other != sender else []
            participants = [{**sender, "role": "sender"}] + \
                [{**r, "role": "recipient"} for r in recipients]

            tup = (it["when"], it["sender"], it["key_text"])
            ordinal = ordinals.get(tup, 0)
            ordinals[tup] = ordinal + 1
            detail: dict = {}
            if group:
                detail["group"] = True
            if it["edited"]:
                detail["edited"] = True
            media_path = None
            if it["files"]:
                detail["media"] = _media_kind(it["files"][0])
                if len(it["files"]) == 1:
                    detail["attachment"] = it["files"][0]
                else:
                    detail["attachments"] = it["files"]
                media_path = next((c["media"][f.casefold()] for f in it["files"]
                                   if f.casefold() in c["media"]), None)
                media["present" if media_path else "missing"] += 1
            elif it["omitted"] is not None:
                kind = _media_kind(None, it["omitted"])
                if kind:
                    detail["media"] = kind
                detail["media_omitted"] = True
                media["omitted"] += 1
            records.append({
                "stream": "whatsapp",
                "external_id": _external_id(c["name"], it["when"], it["sender"],
                                            it["key_text"], ordinal),
                "journal_date": base.journal_date_for_iso(it["when"], cfg),
                "started_at": it["when"],
                "ended_at": None,
                "utc_offset_sec": None,
                "direction": ("out" if mine else "in") if owner_keys else None,
                "title": c["name"],
                "body": it["body"],
                "value": None,
                "unit": None,
                "value_json": canonical_json(detail),
                "media_path": media_path,
                "origin_app": "whatsapp",
                "last_modified_at": it["when"],
                "participants": participants,
            })
            kept.append(it["when"])

        for reason, n in c["skipped"].items():
            skipped[reason] = skipped.get(reason, 0) + n
        for kind, n in c["system"].items():
            if kind == "other":
                what = "system line of a kind not recognised"
                unmapped[what] = unmapped.get(what, 0) + n
            else:
                skipped[f"system line: {kind}"] = skipped.get(f"system line: {kind}", 0) + n
        if c["preamble"]:
            what = "line before the first timestamp"
            unmapped[what] = unmapped.get(what, 0) + c["preamble"]
        dates = sorted(base.journal_date_for_iso(w, cfg) for w in kept)
        chat_facts.append({
            "chat": c["name"], "export": c["label"], "phone": c["flavor"],
            "group": group, "messages": len(kept),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "date_order": _ORDER_WORDS[c["order"]], "date_order_from": c["order_how"]})

    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    return base.empty_scan(
        name,
        account=owner["handle"][1] if owner["handle"] else None,
        source_file=target.name,
        source_hash=source_hash,
        records=records,
        handles=[handles[k] for k in sorted(handles)],
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        # No present_ids: an export is one chat, possibly truncated, so what
        # it lacks is not evidence of a deletion. The archive remembers.
        facts={"chats": chat_facts, "you": owner["label"],
               "you_from": owner["source"], "timezone": zone_name,
               "media": media, "warnings": warnings},
        target_kind=target_kind,
        lines=sum(c["lines"] for c in chats))


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    who = {"out": "you", "in": "them"}.get(rec["direction"], "?")
    body = rec["body"]
    if body is None:
        detail = json.loads(rec["value_json"]) if rec["value_json"] else {}
        body = f"[{detail.get('media') or 'media'}]" if (
            "media" in detail or detail.get("media_omitted")) else "[message]"
    body = " ".join(body.split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    chats = facts["chats"]
    lines = [f"whatsapp: {scan['source_file']} ({len(chats)} chat export(s), "
             f"{scan['lines']} timestamped line(s))"]
    for c in chats:
        span = f" {c['first']}..{c['last']}" if c["first"] else ""
        lines.append(f"  chat: {c['chat']} — {'group' if c['group'] else 'one-to-one'}, "
                     f"{c['messages']} message(s){span}, {c['phone']} format, dates "
                     f"{c['date_order']} ({c['date_order_from']}) | {c['export']}")
    if facts["you"]:
        lines.append(f"  you: {facts['you']} ({facts['you_from']})")
    lines.append(f"  times read as {facts['timezone']} — the export records no zone")
    for w in facts["warnings"]:
        lines.append(f"  WARNING {w}")

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
    if not by_stream:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate message(s) across the exports")
    if tail:
        lines.append("  " + ", ".join(tail))

    you = facts["you"]
    people = [h for h in scan["handles"]
              if not (you and h["value"] == scan["account"])]
    if people:
        phones = sum(1 for h in people if h["kind"] == "phone")
        lines.append(f"  handles: {len(people)} ({len(people) - phones} by the name the "
                     f"export shows, {phones} by phone number) — review with "
                     f"'lifecrm handles list --suggest'")
    m = facts["media"]
    if any(m.values()):
        lines.append(f"  media: {m['present']} file(s) in the export (path kept, never "
                     f"the bytes), {m['missing']} named but not in the export, "
                     f"{m['omitted']} exported without media")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:12]}…: differs "
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
