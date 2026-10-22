"""The Samsung's texts and calls as SMS Backup & Restore writes them.

Two files, as the app's nightly scheduled backup leaves them in its OneDrive
folder: `sms-<time>.xml` (SMS and MMS) and `calls-<time>.xml`. The persona's
one-to-one threads are SMS; the 'Bled crew' group is group MMS — Android has
no group SMS — with the phone's own number as the 'from' of what Alex sent,
which is how the connector learns whose phone this is. Calls are
persona.CALLS.

Rendered the way the app writes, not the way a tidy exporter would: epoch
milliseconds (and an MMS's date_sent in seconds), the literal "null" for
absent values, `readable_date` in the phone's locale, `contact_name` as the
phone shows it ("(Unknown)" for a stranger), numbers as the network or the
keyboard spelled them, and every emoji as two UTF-16 surrogate-pair
character references — which no XML parser accepts, so the connector's
repair is exercised by every demo run.

A few texts the persona does not list are added, consistent with it: two
from the Bled trip four years ago (a phone's backup reaches back years, and
"the lake in Slovenia" should be findable in texts too), Maya's photo from
the climbing gym as a one-to-one MMS with an image part, and Jake saving a
table at Veracruz.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

APP_VERSION = "10.22.004"
FOLDER = "SMSBackupRestore"
CREATOR = "com.samsung.android.messaging"
SERVICE_CENTER = "+12063130004"
OWNER = persona.OWNER.phone

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")

# (when, the other person, sender key, text, attachment) beyond
# persona.THREADS. `when` is (anchor offset, local time), or ('trip', day,
# time) on the Bled trip, in Slovenia's zone.
EXTRA: tuple[tuple, ...] = (
    (("trip", 2, "14:40"), "jake", "jake", "Rowboat out to the island at 3? \U0001F6A3", None),
    (("trip", 2, "14:42"), "jake", "me", "yes!! meet at the pier", None),
    ((-30, "18:42"), "maya", "maya", "sent the purple V3!! \U0001F9D7",
     ("image/jpeg", "movement_climb.jpg")),
    ((-1, "18:58"), "jake", "jake", "grabbed a table on the patio \U0001F32E", None),
)


# ---------------------------------------------------------------- rendering

def _esc(value) -> str:
    """An attribute value as the app writes it: XML-escaped, newlines as
    references, and anything outside the BMP as a UTF-16 surrogate pair."""
    out = []
    for ch in str(value):
        cp = ord(ch)
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == '"':
            out.append("&quot;")
        elif ch == "\n":
            out.append("&#10;")
        elif cp > 0xFFFF:
            cp -= 0x10000
            out.append(f"&#{0xD800 + (cp >> 10)};&#{0xDC00 + (cp & 0x3FF)};")
        else:
            out.append(ch)
    return "".join(out)


def _attrs(pairs) -> str:
    return " ".join(f'{k}="{_esc(v)}"' for k, v in pairs)


def _readable(dt: datetime) -> str:
    """Java's medium date format in a US locale, 'Oct 8, 2026 7:30:05 PM', in
    the phone's zone at backup time — so the Bled texts read in Austin time."""
    local = dt.astimezone(ZoneInfo(persona.TIMEZONE))
    hour = local.hour % 12 or 12
    return (f"{_MONTHS[local.month - 1]} {local.day}, {local.year} "
            f"{hour}:{local:%M:%S} {'AM' if local.hour < 12 else 'PM'}")


def _ms(dt: datetime, salt: str, whole_seconds: bool = False) -> int:
    """Epoch ms; SMS rows carry sub-second noise (deterministic), MMS rows
    are whole seconds — Android stores an MMS's date in seconds."""
    base_ms = int(dt.astimezone(timezone.utc).timestamp()) * 1000
    if whole_seconds:
        return base_ms
    return base_ms + int(hashlib.sha1(salt.encode()).hexdigest()[:4], 16) % 1000


def _hex(salt: str, n: int) -> str:
    return hashlib.sha1(salt.encode()).hexdigest()[:n]


def _when(anchor: date, when) -> datetime:
    if when[0] == "trip":
        return persona.trip_dt(when[1], when[2], anchor)
    return persona.local_dt(anchor, when[0], when[1])


def _spelled(p: persona.Person, outgoing: bool) -> str:
    """Numbers as Android stores them: the network's +E.164 on what arrives,
    whatever was typed on what was sent — Mom's number was typed by hand."""
    if outgoing and p.key == "mom":
        d = p.phone[2:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return p.phone


# ---------------------------------------------------------------- elements

def _sms(when: datetime, other: persona.Person, sender: str,
         text: str) -> tuple[int, str]:
    out = sender == "me"
    ms = _ms(when, f"sms|{other.key}|{when.isoformat()}|{text}")
    pairs = [("protocol", 0), ("address", _spelled(other, out)), ("date", ms),
             ("type", 2 if out else 1), ("subject", "null"), ("body", text),
             ("toa", "null"), ("sc_toa", "null"),
             ("service_center", "null" if out else SERVICE_CENTER),
             ("read", 1), ("status", -1), ("locked", 0),
             ("date_sent", 0 if out else ms - 1800), ("sub_id", 1),
             ("readable_date", _readable(when)), ("contact_name", other.name)]
    return ms, f"  <sms {_attrs(pairs)} />"


def _mms(when: datetime, members: list[persona.Person], sender: str, text: str,
         attachment: tuple[str, str] | None) -> tuple[int, str]:
    out = sender == "me"
    ms = _ms(when, "", whole_seconds=True)
    salt = f"mms|{'~'.join(m.key for m in members)}|{when.isoformat()}|{text}"
    image = None
    if attachment:
        # Stand-in bytes for the photo: the connector must never decode them.
        image = base64.b64encode(b"\xff\xd8\xff\xe0" + hashlib.sha256(
            salt.encode()).digest() * 8 + b"\xff\xd9").decode("ascii")
    pairs = [("date", ms), ("rr", 129), ("sub", "null"),
             ("ct_t", "application/vnd.wap.multipart.related"),
             ("read_status", "null"), ("seen", 1), ("msg_box", 2 if out else 1),
             ("address", "~".join(m.phone for m in members)), ("sub_cs", "null"),
             ("resp_st", 128 if out else "null"), ("retr_st", "null" if out else 128),
             ("d_tm", "null"), ("text_only", 0 if attachment else 1), ("exp", 604800),
             ("locked", 0), ("m_id", f"mavodi-1-{_hex(salt, 2)}-{_hex(salt + 'm', 16)}"),
             ("st", "null"), ("retr_txt_cs", "null"), ("retr_txt", "null"),
             ("creator", CREATOR), ("date_sent", ms // 1000 if not out else 0),
             ("read", 1), ("m_size", len(text.encode()) + (len(image) * 3 // 4 if image else 0)),
             ("rpt_a", "null"), ("ct_cls", "null"), ("pri", 129), ("sub_id", 1),
             ("tr_id", "T" + _hex(salt + "t", 11)), ("resp_txt", "null"),
             ("ct_l", "null"), ("m_cls", "personal"), ("d_rpt", 129), ("v", 18),
             ("m_type", 128 if out else 132), ("readable_date", _readable(when)),
             ("contact_name", ", ".join(m.name for m in members))]
    parts = [[("seq", -1), ("ct", "application/smil"), ("name", "null"),
              ("chset", "null"), ("cd", "null"), ("fn", "null"), ("cid", "<smil>"),
              ("cl", "smil.xml"), ("ctt_s", "null"), ("ctt_t", "null"),
              ("text", '<smil><head><layout><root-layout/><region id="Text" top="0" '
                       'left="0" height="100%" width="100%"/></layout></head><body>'
                       '<par dur="5000ms"><text src="text_0.txt" region="Text"/></par>'
                       '</body></smil>')]]
    if attachment:
        mime, fname = attachment
        parts.append([("seq", 0), ("ct", mime), ("name", fname), ("chset", "null"),
                      ("cd", "null"), ("fn", "null"), ("cid", f"<{fname}>"),
                      ("cl", fname), ("ctt_s", "null"), ("ctt_t", "null"),
                      ("text", "null"), ("data", image)])
    parts.append([("seq", 0), ("ct", "text/plain"), ("name", "null"), ("chset", 106),
                  ("cd", "null"), ("fn", "null"), ("cid", "<text_0>"),
                  ("cl", "text_0.txt"), ("ctt_s", "null"), ("ctt_t", "null"),
                  ("text", text)])
    by_key = {m.key: m for m in members}
    if out:
        addrs = [(OWNER, 137)] + [(m.phone, 151) for m in members]
    else:
        addrs = [(by_key[sender].phone, 137), (OWNER, 151)] + \
                [(m.phone, 151) for m in members if m.key != sender]
    lines = [f"  <mms {_attrs(pairs)}>", "    <parts>"]
    lines += [f"      <part {_attrs(p)} />" for p in parts]
    lines += ["    </parts>", "    <addrs>"]
    lines += [f"      <addr {_attrs([('address', a), ('type', t), ('charset', 106)])} />"
              for a, t in addrs]
    lines += ["    </addrs>", "  </mms>"]
    return ms, "\n".join(lines)


_CALL_TYPES = {"in": 1, "out": 2, "missed": 3}


def _call(when: datetime, who: persona.Person, seconds: int, kind: str) -> tuple[int, str]:
    ms = _ms(when, f"call|{who.key}|{when.isoformat()}")
    pairs = [("number", who.phone), ("duration", seconds), ("date", ms),
             ("type", _CALL_TYPES[kind]), ("presentation", 1), ("subscription_id", 1),
             ("post_dial_digits", ""),
             ("subscription_component_name",
              "com.android.phone/com.android.services.telephony.TelephonyConnectionService"),
             ("readable_date", _readable(when)), ("contact_name", who.name)]
    return ms, f"  <call {_attrs(pairs)} />"


# ---------------------------------------------------------------- files

def _file(root: str, stylesheet: str, taken: datetime, anchor: date, kind: str,
          rows: list[tuple[int, str]]) -> str:
    rows = [line for _, line in sorted(rows, key=lambda r: (r[0], r[1]))]
    backup_set = uuid.uuid5(uuid.NAMESPACE_URL, f"lifecrm-demo/{kind}/{anchor.isoformat()}")
    head = [
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>",
        f"<!--File Created By SMS Backup & Restore v{APP_VERSION} on "
        f"{taken:%d/%m/%Y %H:%M:%S}-->",
        f'<?xml-stylesheet type="text/xsl" href="{stylesheet}"?>',
        f'<{root} count="{len(rows)}" backup_set="{backup_set}" '
        f'backup_date="{int(taken.astimezone(timezone.utc).timestamp()) * 1000}" '
        f'type="full">',
    ]
    return "\n".join(head + rows + [f"</{root}>", ""])


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """Write sms-<time>.xml and calls-<time>.xml under dest; return their
    folder (what `lifecrm observe android_sms` is pointed at)."""
    if combo != "android":
        raise ValueError(f"android_sms has no '{combo}' sample — SMS Backup & Restore "
                         f"is an Android app; the iPhone's texts come from its backup "
                         f"or the Mac's Messages database")
    taken = persona.local_dt(anchor, 0, "02:00:03")
    texts: list[tuple[int, str]] = []
    for thread in persona.THREADS:
        members = [persona.person(k) for k in thread.members]
        for offset, hhmm, sender, text in thread.messages:
            when = persona.local_dt(anchor, offset, hhmm)
            if len(members) == 1:
                texts.append(_sms(when, members[0], sender, text))
            else:
                texts.append(_mms(when, members, sender, text, None))
    for when, other, sender, text, attachment in EXTRA:
        dt = _when(anchor, when)
        who = persona.person(other)
        texts.append(_mms(dt, [who], sender, text, attachment) if attachment
                     else _sms(dt, who, sender, text))
    calls = [_call(persona.local_dt(anchor, offset, hhmm), persona.person(key),
                   seconds, kind)
             for offset, hhmm, key, seconds, kind in persona.CALLS]

    folder = Path(dest) / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"{taken:%Y%m%d%H%M%S}"
    (folder / f"sms-{stamp}.xml").write_text(
        _file("smses", "sms.xsl", taken, anchor, "sms", texts),
        encoding="utf-8", newline="\n")
    (folder / f"calls-{stamp}.xml").write_text(
        _file("calls", "calls.xsl", taken, anchor, "calls", calls),
        encoding="utf-8", newline="\n")
    return folder
