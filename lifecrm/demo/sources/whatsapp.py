"""WhatsApp sample: the Rivera Family group, as each phone's "Export chat".

The family group lives in WhatsApp on both phones (persona.WHATSAPP_GROUP),
so every combo has this source; only the phone's export format differs
(windows-iphone writes the iPhone's, like iphone):

  android   'WhatsApp Chat with Rivera Family.zip' — the .txt and the media,
            lines like '10/8/26, 8:40 PM - Linda Rivera: text'
  iphone    'WhatsApp Chat - Rivera Family.zip' — '_chat.txt' and the media,
            lines like '[10/8/26, 8:40:07 PM] Linda Rivera: text', with the
            invisible LEFT-TO-RIGHT MARKs iOS writes on system and media
            lines, CRLF line ends, and an unsaved number wrapped in direction
            marks with a non-breaking hyphen

Alex's phones are set to US English, so both write the month first, and both
put the narrow no-break space current phones use before AM/PM.

What the persona leaves to the export, defined here consistently with it:

- Tía Rosa has no contact card, so the export shows her number, not her
  name — the importer gets a phone handle with no display name, as it would
  for any unsaved contact.
- Mom created the group about fourteen months before the anchor, on the
  20th. A day past 12 means the month-first order reads unambiguously from
  the dates themselves, whatever the anchor; otherwise the importer would
  have to guess, and say so.
- The dinner thread carries what a real chat carries: Mom's menu over several
  lines, a message Rosa deleted for everyone, Alex's reply edited once, and
  Rosa's photo from the dinner followed by a caption.
"""

from __future__ import annotations

import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import persona

LRM = "\N{LEFT-TO-RIGHT MARK}"
NNBSP = "\N{NARROW NO-BREAK SPACE}"
ROSA_PHONE = "+12105550131"           # 555-01xx: fictional, unused elsewhere
CREATED_DAY = 20
CHAT_FILE = {"android": "WhatsApp Chat with {name}.zip",
             "iphone": "WhatsApp Chat - {name}.zip"}

# (offset, 'HH:MM', sender, kind, text) added to the persona's own lines.
# kind: 'text', 'deleted' (deleted for everyone), 'photo'.
EXTRA = (
    (-15, "19:02", "mom", "text",
     "Menu so far:\ntamales\narroz con pollo\nRosa is making her flan"),
    (-15, "19:06", "Tía Rosa", "deleted", None),
    (-13, "20:40", "Tía Rosa", "photo", None),
    (-13, "20:40", "Tía Rosa", "text",
     "Qué bonita familia \N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}"),
)
# Persona lines shown as edited: Alex fixed a typo in the reply.
EDITED = {(-15, "19:10")}
UNSAVED = {"Tía Rosa": ROSA_PHONE}

# Smallest well-formed JPEG: an 8x8 mid-grey baseline image, so the photo in
# the sample opens in a viewer instead of being bytes that only look like one.
_JPEG = (b"\xff\xd8"
         b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
         b"\xff\xdb\x00\x43\x00" + b"\x01" * 64 +
         b"\xff\xc0\x00\x0b\x08\x00\x08\x00\x08\x01\x01\x11\x00"
         b"\xff\xc4\x00\x14\x00\x01" + b"\x00" * 15 + b"\x00"
         b"\xff\xc4\x00\x14\x10\x01" + b"\x00" * 15 + b"\x00"
         b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
         b"\x3f\xff\xd9")


# ---------------------------------------------------------------- the story

def created_on(anchor: date) -> date:
    """The day Mom made the group: the 20th, about fourteen months back."""
    month = anchor.month - 14
    year = anchor.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, CREATED_DAY)


def _events(anchor: date) -> list[tuple[datetime, str, str | None, str | None]]:
    """(local time, kind, sender, text) in the order the export lists them.

    kind: 'encryption', 'created', 'added_you', 'added', 'text', 'edited',
    'deleted', 'photo'. Seconds are filled in per minute, so lines sharing a
    minute keep their order on the iPhone, which writes seconds."""
    g = persona.WHATSAPP_GROUP
    creator = g["members"][0]
    start = (created_on(anchor) - anchor).days
    rows: list[tuple[int, str, int, str, str | None, str | None]] = [
        (start, "18:42", 0, "encryption", None, None),
        (start, "18:42", 1, "created", creator, None),
        (start, "18:42", 2, "added_you", creator, None),
        (start, "18:43", 3, "added", creator, "Tía Rosa"),
    ]
    for i, (off, hhmm, who, text) in enumerate(g["messages"]):
        kind = "edited" if (off, hhmm) in EDITED else "text"
        rows.append((off, hhmm, 10 + i, kind, who, text))
    for i, (off, hhmm, who, kind, text) in enumerate(EXTRA):
        rows.append((off, hhmm, 100 + i, kind, who, text))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    out, per_minute = [], {}
    for off, hhmm, _, kind, who, text in rows:
        k = per_minute.get((off, hhmm), 0)
        per_minute[(off, hhmm)] = k + 1
        when = persona.local_dt(anchor, off, hhmm) + timedelta(seconds=7 + 19 * k)
        out.append((when, kind, who, text))
    return out


def _name(who: str, combo: str) -> str:
    """How the phone shows a sender: the contact's name, or the number of
    someone with no contact card."""
    if who == "me":
        return persona.OWNER.name
    if who in UNSAVED:
        digits = UNSAVED[who][2:]
        if persona.has_iphone(combo):
            return (f"\N{LEFT-TO-RIGHT EMBEDDING}+1 ({digits[:3]}) {digits[3:6]}"
                    f"\N{NON-BREAKING HYPHEN}{digits[6:]}\N{POP DIRECTIONAL FORMATTING}")
        return f"+1 {digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return persona.person(who).name


def _clock(dt: datetime, seconds: bool) -> str:
    hour = dt.hour % 12 or 12
    tail = f":{dt:%S}" if seconds else ""
    return f"{dt.month}/{dt.day}/{dt:%y}, {hour}:{dt:%M}{tail}{NNBSP}{dt:%p}"


# ---------------------------------------------------------------- rendering

def _render(anchor: date, combo: str) -> tuple[str, list[tuple[str, bytes]]]:
    """(chat text, [(media file name, bytes)])."""
    g = persona.WHATSAPP_GROUP
    group = g["name"]
    ios = persona.has_iphone(combo)
    lines: list[str] = []
    media: list[tuple[str, bytes]] = []
    photos_per_day: dict[date, int] = {}
    quote = ("\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}") \
        if ios else ('"', '"')
    for when, kind, who, text in _events(anchor):
        name = _name(who, combo) if who else None
        if kind in ("encryption", "created", "added_you", "added"):
            what = {
                "encryption": "Messages and calls are end-to-end encrypted. Only "
                              "people in this chat can read, listen to, or share them."
                              + ("" if ios else " Learn more."),
                "created": f"{name} created group {quote[0]}{group}{quote[1]}",
                "added_you": f"{name} added you",
                "added": f"{name} added {_name(text, combo) if text else ''}",
            }[kind]
            lines.append(f"{LRM}[{_clock(when, True)}] {group}: {LRM}{what}" if ios
                         else f"{_clock(when, False)} - {what}")
            continue
        if kind == "photo":
            day = when.date()
            photos_per_day[day] = photos_per_day.get(day, 0) + 1
            if ios:
                file = (f"{len(lines) + 1:08d}-PHOTO-{when:%Y-%m-%d-%H-%M-%S}.jpg")
                body, mark = f"{LRM}<attached: {file}>", LRM
            else:
                file = f"IMG-{when:%Y%m%d}-WA{photos_per_day[day]:04d}.jpg"
                body, mark = f"{file} (file attached)", ""
            media.append((file, _JPEG))
        elif kind == "deleted":
            body = f"{LRM}This message was deleted." if ios else "This message was deleted"
            mark = LRM if ios else ""
        else:
            body = text or ""
            if kind == "edited":
                body += f" {LRM}<This message was edited>" if ios \
                    else " <This message was edited>"
            mark = ""
        if ios:
            lines.append(f"{mark}[{_clock(when, True)}] {name}: {body}")
        else:
            lines.append(f"{_clock(when, False)} - {name}: {body}")
    newline = "\r\n" if ios else "\n"
    text = newline.join(line.replace("\n", newline) for line in lines) + newline
    return text, media


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The group's export zip for `combo`, under `dest`; returns its path."""
    if combo not in persona.COMBOS:
        raise ValueError(f"no WhatsApp sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    group = persona.WHATSAPP_GROUP["name"]
    text, media = _render(anchor, combo)
    phone = "iphone" if persona.has_iphone(combo) else "android"
    member = "_chat.txt" if phone == "iphone" else f"WhatsApp Chat with {group}.txt"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / CHAT_FILE[phone].format(name=group)
    # A fixed timestamp per member: the same (anchor, combo) gives the same
    # bytes, so the importer's "already imported" check sees the same file.
    stamp = (anchor.year, anchor.month, anchor.day, 12, 0, 0)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in [(member, text.encode("utf-8")), *media]:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return out
