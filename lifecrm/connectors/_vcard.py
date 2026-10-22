"""vCard reader (.vcf): the export format every address book shares.

A helper, not a connector (the leading underscore keeps it out of the
registry); `contacts.py` turns its cards into observations.

Three versions are in the wild, and an export rarely says which quirks it
uses, so every one is read the same way:

  2.1   Android/Samsung device exports. Bare parameters (`TEL;CELL:`),
        QUOTED-PRINTABLE values with an explicit CHARSET and soft line
        breaks (a trailing '='), base64 photos indented over many lines,
        nicknames tucked into `X-ANDROID-CUSTOM:vnd.android.cursor.item/
        nickname;...`.
  3.0   Google Contacts ("vCard for iOS Contacts"), macOS Contacts, iCloud.
        `TYPE=CELL` parameters, `item1.TEL` + `item1.X-ABLabel` grouping
        for custom labels, backslash escapes (`\\n`, `\\,`, `\\;`, Google's
        `http\\://`), lines folded at 75 octets — which can split a UTF-8
        character in two, so folding is undone on bytes, before decoding.
  4.0   `TEL;VALUE=uri:tel:+1-...`, `TYPE="cell,voice"`, `BDAY:--0314`.

Parsing works on bytes until the last moment: a 2.1 file may declare a
different CHARSET per property, and decoding the whole file as one encoding
would mangle exactly those values. A UTF-8 BOM is dropped and a UTF-16 file
(Outlook writes them) is re-encoded first.

Photos are never decoded — media stays outside the database — but each card
that carried one is counted, so the report can say so. Every property this
reader does not use is counted by name, not silently dropped.
"""

from __future__ import annotations

import codecs
import quopri
import re
from datetime import date, datetime, timedelta, timezone

NO_YEAR = 1604                 # Apple's year for a birthday entered without one

# Properties read into the card, or structural; everything else is counted.
_USED = {"BEGIN", "END", "VERSION", "PRODID", "FN", "N", "NICKNAME", "ORG",
         "TITLE", "BDAY", "ADR", "NOTE", "URL", "EMAIL", "TEL", "UID",
         "X-ABUID", "REV", "X-ABLABEL", "X-ABSHOWAS", "X-ABADR"}
_MEDIA = {"PHOTO", "LOGO", "SOUND", "KEY"}
_IGNORED_TYPES = {"voice", "pref", "internet", "x400", "msg", "intl", "dom",
                  "postal", "parcel", "text"}
# Every TYPE the vCard RFCs (and Apple) define; any other TYPE value is a
# label someone typed — Android writes a custom label that way.
_REGISTERED_TYPES = _IGNORED_TYPES | {
    "cell", "mobile", "iphone", "main", "home", "work", "pager", "school",
    "other", "fax", "video", "textphone", "bbs", "modem", "car", "isdn", "pcs",
    "aol", "applelink", "attmail", "cis", "eworld", "ibmmail", "mcimail",
    "powershare", "prodigy", "tlx"}
# Samsung's custom label: TEL;X-CUSTOM(CHARSET=UTF-8,ENCODING=QUOTED-PRINTABLE,=4F=66...)
_X_CUSTOM = re.compile(r"(?i)^X-CUSTOM\((.*)\)$")
_X_CUSTOM_PARAMS = re.compile(r"(?i)^((?:\s*(?:CHARSET|ENCODING)=[^,]*,)*)(.*)$", re.DOTALL)
# Apple's built-in labels ('_$!<Mobile>!$_', or 'Mobile' once unwrapped) to
# one spelling, so a card reads the same from a vCard and from the Mac store.
_KNOWN_LABELS = {"mobile": "mobile", "cell": "mobile", "iphone": "iphone",
                 "main": "main", "home": "home", "work": "work",
                 "other": "other", "pager": "pager", "school": "school",
                 "homefax": "home fax", "workfax": "work fax",
                 "otherfax": "other fax", "fax": "fax", "homepage": "homepage",
                 "apple watch": "apple watch"}
_APPLE_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


class VCardError(ValueError):
    """The bytes are not a vCard file; the message says what they were."""


# ---------------------------------------------------------------- labels

def norm_label(raw) -> str | None:
    """One label vocabulary for vCard TYPEs, X-ABLabels and Mac store labels:
    built-ins lower-cased to a fixed spelling, custom labels kept as typed."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _APPLE_LABEL.match(s)
    if m:
        s = m.group(1).strip()
    if not s:
        return None
    return _KNOWN_LABELS.get(s.lower(), s)


def _type_label(types: set[str]) -> str | None:
    t = {x.lower() for x in types} - _IGNORED_TYPES
    if "fax" in t:
        return "home fax" if "home" in t else "work fax" if "work" in t else "fax"
    # 'iphone' before 'cell': Apple writes an iPhone number as
    # type=IPHONE;type=CELL, and its store calls the same number 'iPhone'.
    for key in ("iphone", "cell", "mobile", "main", "home", "work", "pager",
                "school", "other"):
        if key in t:
            return _KNOWN_LABELS[key]
    return None


def _custom_label(names: list[str]) -> str | None:
    """A typed label among a property's TYPEs: 2.1's bare `X-Office`, or
    3.0's `TYPE=Office` — whatever is not a type the format defines."""
    for n in names:
        label = n[2:] if n[:2].upper() == "X-" else n
        if n.lower() not in _REGISTERED_TYPES and label.strip():
            return label.strip()
    return None


def _x_custom(inner: str) -> str | None:
    params, label = _X_CUSTOM_PARAMS.match(inner).groups()
    found = {k.strip().upper(): v.strip() for k, _, v in
             (p.partition("=") for p in params.split(",") if p.strip())}
    if found.get("ENCODING", "").upper() == "QUOTED-PRINTABLE":
        raw = quopri.decodestring(label.encode("ascii", "replace"))
        try:
            label = raw.decode(found.get("CHARSET") or "utf-8")
        except (LookupError, UnicodeDecodeError):
            label = raw.decode("utf-8", "replace")
    return label.strip() or None


# ---------------------------------------------------------------- dates

def parse_birthday(value: str, omit_year: str | None = None) -> str | None:
    """'YYYY-MM-DD', or '--MM-DD' for a birthday with no year; None when the
    value is not a date at all (BDAY;VALUE=text:circa 1950)."""
    s = (value or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})(?:[T ].*)?$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if y == 0 or y == NO_YEAR or (omit_year and omit_year.strip() == m.group(1)):
            return _no_year(mo, d)
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.match(r"^--(\d{2})-?(\d{2})$", s)
    if m:
        return _no_year(int(m.group(1)), int(m.group(2)))
    return None


def _no_year(month: int, day: int) -> str | None:
    try:
        date(2000, month, day)          # a leap year: Feb 29 is a real birthday
    except ValueError:
        return None
    return f"--{month:02d}-{day:02d}"


def birthday_text(bday: str | None) -> str | None:
    """'1993-03-14' -> 'March 14, 1993'; '--03-14' -> 'March 14'."""
    if not bday:
        return None
    if bday.startswith("--"):
        month, day = int(bday[2:4]), int(bday[5:7])
        return f"{_MONTHS[month - 1]} {day}"
    y, month, day = (int(x) for x in bday.split("-"))
    return f"{_MONTHS[month - 1]} {day}, {y}"


def parse_instant(value: str) -> str | None:
    """A REV timestamp in any of the shapes exporters write ('2026-10-01T12:00:00Z',
    '20261001T120000Z', with fractions or an offset, or a bare date) ->
    'YYYY-MM-DDTHH:MM:SSZ'. No zone means UTC: that is what REV is defined in."""
    s = (value or "").strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})"
                 r"(?:[T ](\d{2}):?(\d{2})(?::?(\d{2}))?(?:[.,]\d+)?"
                 r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?$", s, re.IGNORECASE)
    if not m:
        return None
    y, mo, d, hh, mi, ss, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0),
                      int(ss or 0), tzinfo=timezone.utc)
    except ValueError:
        return None
    if tz and tz.upper() != "Z":
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        dt = dt - sign * offset
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- lines

def _to_utf8_bytes(raw: bytes) -> bytes:
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):]
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16").encode("utf-8")
    return raw


def _cards(raw: bytes) -> tuple[list[list[bytes]], int]:
    """Physical lines of each top-level BEGIN:VCARD ... END:VCARD block, and
    how many blocks ran out before their END (a truncated file). A card
    nested inside another (2.1's AGENT) stays inside its parent's lines."""
    out, current, depth, unterminated = [], None, 0, 0
    for line in re.split(rb"\r\n|\n|\r", raw):
        word = line.strip().upper()
        if word == b"BEGIN:VCARD":
            depth += 1
            if depth == 1:
                current = []
                continue
        elif word == b"END:VCARD" and depth:
            depth -= 1
            if depth == 0:
                out.append(current)
                current = None
                continue
        if current is not None:
            current.append(line)
    if current is not None:
        out.append(current)
        unterminated += 1
    return out, unterminated


def _logical(lines: list[bytes]) -> list[bytes]:
    """Undo folding, then quoted-printable soft line breaks.

    3.0/4.0 fold by inserting CRLF plus one space or tab, and unfolding
    removes both. 2.1 folds the RFC 822 way — at existing white space, which
    unfolding keeps. A QUOTED-PRINTABLE value continues past a line ending in
    '=' (a soft line break) onto the next line, which is not indented: its
    first character is already content.
    """
    v21 = any(re.match(rb"^\s*VERSION\s*:\s*2\.1\s*$", ln, re.IGNORECASE)
              for ln in lines if ln[:1] not in (b" ", b"\t"))
    # Each logical line is kept as its physical pieces and joined once at the
    # end: a 2.1 photo is a thousand indented base64 lines, and growing one
    # bytes object line by line would copy it a thousand times.
    out: list[list[bytes]] = []
    for ln in lines:
        if out and _qp_open(out[-1]):
            parts = out[-1]
            while parts and not parts[-1].strip(b" \t"):
                parts.pop()
            parts[-1] = parts[-1].rstrip(b" \t")[:-1]
            parts.append(ln)
        elif ln[:1] in (b" ", b"\t") and out:
            out[-1].append(ln if v21 else ln[1:])
        elif ln.strip():
            out.append([ln])
    return [b"".join(parts) for parts in out]


def _qp_open(parts: list[bytes]) -> bool:
    tail = next((p.rstrip(b" \t") for p in reversed(parts) if p.strip(b" \t")), b"")
    if not tail.endswith(b"="):
        return False
    head, _, _ = _split_head(parts[0])
    return b"QUOTED-PRINTABLE" in head.upper()


def _split_head(line: bytes) -> tuple[bytes, bytes, bool]:
    """(name and parameters, value, found) — split at the first ':' that is
    not inside a quoted parameter value."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == 0x22:
            quoted = not quoted
        elif ch == 0x3A and not quoted:
            return line[:i], line[i + 1:], True
    return line, b"", False


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_line(line: bytes) -> dict | None:
    head, value, found = _split_head(line)
    if not found:
        return None
    pieces = _split_unquoted(head.decode("utf-8", "replace").strip(), ";")
    group, _, prop = pieces[0].rpartition(".")
    params: dict[str, str] = {}
    types: set[str] = set()
    names: list[str] = []                # the TYPEs as written, for a typed label
    custom = None
    for p in pieces[1:]:
        m = _X_CUSTOM.match(p.strip())
        if m:
            custom = _x_custom(m.group(1))
            continue
        key, eq, val = p.partition("=")
        key, val = key.strip().upper(), val.strip()
        if not eq:                       # 2.1 bare parameter
            if key in ("QUOTED-PRINTABLE", "BASE64", "B", "8BIT", "7BIT"):
                params["ENCODING"] = key
            else:
                types.add(key.lower())
                names.append(p.strip())
            continue
        if key == "TYPE":
            for t in (t.strip() for t in val.split(",")):
                if t:
                    types.add(t.lower())
                    names.append(t)
        else:
            params[key] = val
    return {"group": group.strip().lower() or None, "prop": prop.strip().upper(),
            "params": params, "types": types,
            "custom": custom or _custom_label(names), "raw": value}


def _decode(entry: dict) -> str | None:
    """The property's text, or None for binary content (photos)."""
    enc = entry["params"].get("ENCODING", "").upper()
    raw = entry["raw"]
    if enc in ("B", "BASE64"):
        return None
    if enc == "QUOTED-PRINTABLE":
        raw = quopri.decodestring(raw)
    charset = entry["params"].get("CHARSET") or "utf-8"
    for cs in (charset, "utf-8"):
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("cp1252", "replace")


# ---------------------------------------------------------------- values

def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1),
                  s, flags=re.DOTALL)


def _components(s: str, sep: str = ";") -> list[str]:
    """Split a structured value on separators that are not backslash-escaped,
    then unescape each part."""
    parts, buf, i = [], [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            buf.append(s[i:i + 2])
            i += 2
            continue
        if ch == sep:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [_unescape(p).strip() for p in parts]


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def _one_line(s: str | None) -> str | None:
    if s is None:
        return None
    return _clean(", ".join(x.strip() for x in s.splitlines() if x.strip()))


# ---------------------------------------------------------------- cards

def parse(raw: bytes, name: str) -> dict:
    """Every card in one .vcf file.

    Returns {'cards': [card], 'unmapped': {property: count}, 'photos': n,
    'unterminated': n, 'versions': {version: count}, 'prodid', 'hints'}.
    A card carries the same keys as `_abcddb.read_contacts` produces (uid,
    first, last, middle, nickname, org, job_title, display_name, phones,
    emails, addresses, birthday, note, created_at, modified_at) plus urls,
    uid_kind ('UID' | 'X-ABUID' | None) and bday_raw for a BDAY that is not
    a date. Raises VCardError when the bytes hold no vCard at all.
    """
    data = _to_utf8_bytes(raw)
    blocks, unterminated = _cards(data)
    if not blocks:
        start = data[:40].decode("utf-8", "replace").strip().splitlines()
        seen = start[0][:40] if start else "(empty)"
        raise VCardError(f"{name} has no BEGIN:VCARD — is this a vCard export? "
                         f"(starts with: {seen!r})")
    cards, unmapped, versions = [], {}, {}
    photos, prodid, hints = 0, None, set()
    for lines in blocks:
        card, info = _card(_logical(lines))
        for prop, n in info["unmapped"].items():
            unmapped[prop] = unmapped.get(prop, 0) + n
        photos += 1 if info["photo"] else 0
        versions[info["version"] or "?"] = versions.get(info["version"] or "?", 0) + 1
        prodid = prodid or info["prodid"]
        hints |= info["hints"]
        cards.append(card)
    return {"cards": cards, "unmapped": unmapped, "photos": photos,
            "unterminated": unterminated, "versions": versions,
            "prodid": prodid, "hints": hints}


def _card(lines: list[bytes]) -> tuple[dict, dict]:
    entries = [e for e in (_parse_line(ln) for ln in lines) if e]
    labels = {}
    for e in entries:
        if e["prop"] == "X-ABLABEL" and e["group"]:
            labels[e["group"]] = norm_label(_unescape(_decode(e) or ""))

    def label_of(e: dict) -> str | None:
        if e["group"] and labels.get(e["group"]):
            return labels[e["group"]]
        return e["custom"] or _type_label(e["types"])

    card = {"uid": None, "uid_kind": None, "first": None, "last": None,
            "middle": None, "prefix": None, "suffix": None, "nickname": None,
            "org": None, "job_title": None, "display_name": None, "phones": [],
            "emails": [], "addresses": [], "urls": [], "birthday": None,
            "bday_raw": None, "note": None, "created_at": None,
            "modified_at": None}
    info = {"unmapped": {}, "photo": False, "version": None, "prodid": None,
            "hints": set()}
    fn = abuid = None
    nicknames: list[str] = []
    notes: list[str] = []
    company = False
    for e in entries:
        prop = e["prop"]
        if prop in _MEDIA:
            info["photo"] = info["photo"] or prop == "PHOTO"
            continue
        if prop == "X-ANDROID-CUSTOM":
            info["hints"].add("android")
            parts = _components(_decode(e) or "")
            if parts and parts[0] == "vnd.android.cursor.item/nickname" and len(parts) > 1:
                if parts[1]:
                    nicknames.append(parts[1])
                continue
            kind = parts[0].rsplit("/", 1)[-1] if parts else "?"
            key = f"X-ANDROID-CUSTOM ({kind})"
            info["unmapped"][key] = info["unmapped"].get(key, 0) + 1
            continue
        if prop not in _USED:
            if prop == "CATEGORIES" and "mycontacts" in (_decode(e) or "").lower():
                info["hints"].add("google")
            info["unmapped"][prop] = info["unmapped"].get(prop, 0) + 1
            continue
        text = _decode(e)
        if text is None:
            continue
        if prop == "VERSION":
            info["version"] = text.strip()
        elif prop == "PRODID":
            info["prodid"] = _unescape(text).strip()
        elif prop == "FN":
            fn = _clean(_unescape(text))
        elif prop == "N":
            parts = (_components(text) + [""] * 5)[:5]
            card["last"], card["first"], card["middle"], card["prefix"], \
                card["suffix"] = (_clean(" ".join(p.split(","))) for p in parts)
        elif prop == "NICKNAME":
            nicknames += [n for n in _components(text, ",") if n]
        elif prop == "ORG":
            card["org"] = _clean(_components(text)[0])
        elif prop == "TITLE":
            card["job_title"] = _clean(_unescape(text))
        elif prop == "BDAY":
            card["birthday"] = parse_birthday(_unescape(text),
                                              e["params"].get("X-APPLE-OMIT-YEAR"))
            if card["birthday"] is None and text.strip():
                card["bday_raw"] = _unescape(text).strip()
        elif prop == "ADR":
            parts = (_components(text) + [""] * 7)[:7]
            pobox, ext, street, city, region, code, country = parts
            addr = {"label": label_of(e),
                    "street": _one_line(", ".join(x for x in (street, ext) if x)
                                        or pobox),
                    "city": _clean(city), "state": _clean(region),
                    "zip": _clean(code), "country": _clean(country)}
            if any(addr[k] for k in ("street", "city", "state", "zip", "country")):
                card["addresses"].append(addr)
        elif prop == "NOTE":
            note = _clean(_unescape(text))
            if note:
                notes.append(note)
        elif prop == "URL":
            url = _clean(_unescape(text))
            if url:
                card["urls"].append((label_of(e), url))
        elif prop == "EMAIL":
            addr = _clean(_unescape(text))
            if addr:
                if addr.lower().startswith("mailto:"):
                    addr = addr[7:].strip()
                card["emails"].append((label_of(e), addr))
        elif prop == "TEL":
            number = _clean(_unescape(text))
            if number:
                if number.lower().startswith("tel:"):
                    number = number[4:].strip()
                card["phones"].append((label_of(e), number))
        elif prop == "UID":
            card["uid"] = _clean(_unescape(text))
            card["uid_kind"] = "UID" if card["uid"] else None
        elif prop == "X-ABUID":
            abuid = _clean(_unescape(text))
        elif prop == "REV":
            card["modified_at"] = parse_instant(_unescape(text))
        elif prop == "X-ABSHOWAS":
            company = text.strip().upper() == "COMPANY"
    if card["uid"] is None and abuid:
        card["uid"], card["uid_kind"] = abuid, "X-ABUID"
    card["nickname"] = ", ".join(dict.fromkeys(nicknames)) or None
    card["note"] = "\n".join(notes) or None
    joined = " ".join(x for x in (card["first"], card["middle"], card["last"]) if x)
    if company and card["org"]:
        display = card["org"]
    else:
        display = fn or joined or (nicknames[0] if nicknames else None) or card["org"]
    card["display_name"] = display
    return card, info
