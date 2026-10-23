"""Telegram sample: Telegram Desktop's "Export Telegram data", as
machine-readable JSON.

Telegram Desktop runs on the Windows laptop and on the MacBook alike, and
writes the same export on both, so both combos have this source and get the
same folder:

  DataExport_<anchor>/
      result.json                    personal_information, contacts,
                                     chats.list[] with every message
      chats/chat_001/photos/photo_1@<dd-mm-yyyy_HH-MM-SS>.jpg

Written the way Telegram Desktop writes it: one-space indents, real UTF-8,
"date" in local time beside "date_unixtime", text as a plain string or as a
list of runs when it has formatting, and "text_entities" beside it.

What the persona leaves to the export, defined here consistently with it:

- Sam texts Alex on Telegram about the new climbing gym on Saturday — the
  note waiting in today's inbox ("Sam wants to try the new climbing gym on
  Saturday") — after a call the week before; Alex's reply is edited once,
  Sam sends a photo of the wall and a sticker (left out by the export's
  size settings, as stickers usually are).
- "Saturday Climbers", the group Sam makes for it with Maya: her bold
  "I'm in", Alex's snacks with two reactions, a pinned message.
- Saved Messages: Alex's own note about the Bled trip.
- A climbing-news channel Alex follows, which is counted, not imported.
- The account's contact list, which is not messages and is not imported.

User ids are fictional: 7000001xx, matching each person's 555-01xx number.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import persona

FOLDER = "DataExport_{day}"
NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"
USER_IDS = {"me": 700000100, "sam": 700000166, "maya": 700000187}
GROUP_ID = 4000000007
CHANNEL_ID = 1500000042

# Smallest well-formed JPEG (8x8, mid-grey), so the sample photo opens.
_JPEG = (b"\xff\xd8"
         b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
         b"\xff\xdb\x00\x43\x00" + b"\x01" * 64 +
         b"\xff\xc0\x00\x0b\x08\x00\x08\x00\x08\x01\x01\x11\x00"
         b"\xff\xc4\x00\x14\x00\x01" + b"\x00" * 15 + b"\x00"
         b"\xff\xc4\x00\x14\x10\x01" + b"\x00" * 15 + b"\x00"
         b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
         b"\x3f\xff\xd9")

# ---------------------------------------------------------------- the story
#
# (offset, 'HH:MM', sender key, kind, payload, extras). kind:
#   text      payload: a string, or a list of strings and (entity type, text)
#   photo     payload: the caption (the file is written into the export)
#   sticker   payload: the sticker's emoji (the file is left out)
#   service   payload: the action; extras carry its fields
# extras: {'edited': 'HH:MM', 'reactions': ((emoji, (person keys)),), ...}

CHATS = (
    {"key": "sam", "type": "personal_chat", "messages": (
        (-6, "07:30", "sam", "service", "phone_call",
         {"duration_seconds": 180, "discard_reason": "hangup"}),
        (-1, "18:20", "sam", "text",
         "Did you see the new climbing gym opened on the east side?", {}),
        (-1, "18:24", "me", "text", "No way, bouldering or ropes?", {}),
        (-1, "18:25", "sam", "text",
         ["Both. Opening week is ", ("bold", "half price day passes")], {}),
        (0, "08:45", "sam", "text", "Want to try the new climbing gym on Saturday?", {}),
        (0, "08:47", "me", "text", "Yes! Morning works, I'm free till 1",
         {"edited": "08:49"}),
        (0, "08:50", "sam", "photo", "Their comp wall \N{FLUSHED FACE}", {}),
        (0, "08:51", "sam", "sticker", "\N{FLEXED BICEPS}", {}),
    )},
    {"key": "group", "type": "private_group", "name": "Saturday Climbers",
     "messages": (
        (0, "09:00", "sam", "service", "create_group",
         {"title": "Saturday Climbers", "members": ["Sam Okafor", "Maya Chen",
                                                    "Alex Rivera"]}),
        (0, "09:02", "sam", "text", "Saturday 10am at the new climbing gym, who's in?", {}),
        (0, "09:05", "maya", "text", [("bold", "I'm in"), ", I'll bring the extra chalk"], {}),
        (0, "09:06", "me", "text", "Bringing snacks \N{BANANA}",
         {"reactions": (("\N{THUMBS UP SIGN}", ("sam", "maya")),)}),
        (0, "09:10", "sam", "service", "pin_message", {"message_id": 0}),
    )},
    {"key": "saved", "type": "saved_messages", "messages": (
        (-16, "21:00", "me", "text",
         "Bled: book Vintgar Gorge tickets online, pack the type F adapter", {}),
    )},
    {"key": "channel", "type": "public_channel", "name": "Austin Climbing News",
     "messages": (
        (-3, "12:00", "channel", "text", "New east-side gym opens this week", {}),
    )},
)


def _who(key: str) -> tuple[str, str]:
    """(name as shown, from_id)."""
    if key == "channel":
        return "Austin Climbing News", f"channel{CHANNEL_ID}"
    p = persona.OWNER if key == "me" else persona.person(key)
    return p.name, f"user{USER_IDS[key]}"


def _text(payload) -> tuple[object, list[dict]]:
    """(text as Telegram writes it, text_entities)."""
    if isinstance(payload, str):
        return payload, ([{"type": "plain", "text": payload}] if payload else [])
    text, entities = [], []
    for part in payload:
        if isinstance(part, str):
            text.append(part)
            entities.append({"type": "plain", "text": part})
        else:
            kind, words = part
            text.append({"type": kind, "text": words})
            entities.append({"type": kind, "text": words})
    return text, entities


def _when(anchor: date, offset: int, hhmm: str, n: int) -> datetime:
    return persona.local_dt(anchor, offset, hhmm) + timedelta(seconds=(7 + 13 * n) % 60)


def _stamp(dt: datetime) -> dict:
    return {"date": dt.replace(tzinfo=None).isoformat(timespec="seconds"),
            "date_unixtime": str(int(dt.astimezone(timezone.utc).timestamp()))}


def _chat(anchor: date, c: dict, index: int, first_id: int
          ) -> tuple[dict, list[tuple[str, bytes]], int]:
    """(the chat as result.json lists it, [(file in the export, bytes)], next id)."""
    files: list[tuple[str, bytes]] = []
    if c["type"] == "personal_chat":
        peer = persona.person(c["key"])
        head = {"name": peer.name, "type": c["type"], "id": USER_IDS[c["key"]]}
    elif c["type"] == "saved_messages":
        head = {"type": c["type"], "id": USER_IDS["me"]}
    else:
        head = {"name": c["name"], "type": c["type"],
                "id": GROUP_ID if c["type"] == "private_group" else CHANNEL_ID}
    messages, mid = [], first_id
    photos = 0
    for n, (off, hhmm, who, kind, payload, extras) in enumerate(c["messages"]):
        dt = _when(anchor, off, hhmm, n)
        shown, from_id = _who(who)
        msg: dict = {"id": mid, "type": "service" if kind == "service" else "message",
                     **_stamp(dt)}
        if kind == "service":
            msg.update({"actor": shown, "actor_id": from_id, "action": payload})
            msg.update({k: v for k, v in extras.items()})
            if payload == "pin_message":
                msg["message_id"] = mid - 1
            msg.update({"text": "", "text_entities": []})
        else:
            if "edited" in extras:
                hh, mm = extras["edited"].split(":")
                ed = dt.replace(hour=int(hh), minute=int(mm))
                msg.update({"edited": ed.replace(tzinfo=None).isoformat(timespec="seconds"),
                            "edited_unixtime": str(int(ed.timestamp()))})
            msg.update({"from": shown, "from_id": from_id})
            if kind == "photo":
                photos += 1
                path = (f"chats/chat_{index:03d}/photos/photo_{photos}@"
                        f"{dt:%d-%m-%Y_%H-%M-%S}.jpg")
                files.append((path, _JPEG))
                msg.update({"photo": path, "photo_file_size": len(_JPEG),
                            "width": 1280, "height": 960})
                text, entities = _text(payload)
            elif kind == "sticker":
                msg.update({"file": NOT_INCLUDED, "file_size": 31412,
                            "thumbnail": NOT_INCLUDED, "thumbnail_file_size": 5120,
                            "media_type": "sticker", "sticker_emoji": payload,
                            "width": 512, "height": 512})
                text, entities = _text("")
            else:
                text, entities = _text(payload)
            msg.update({"text": text, "text_entities": entities})
            if "reactions" in extras:
                msg["reactions"] = [
                    {"type": "emoji", "count": len(people), "emoji": emoji,
                     "recent": [{"from": _who(p)[0], "from_id": _who(p)[1],
                                 **_stamp(dt + timedelta(minutes=2 + i))}
                                for i, p in enumerate(people)]}
                    for emoji, people in extras["reactions"]]
        messages.append(msg)
        mid += 1
    return {**head, "messages": messages}, files, mid


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The export folder for `combo`, under `dest`; returns its path."""
    if combo not in persona.COMBOS:
        raise ValueError(f"no Telegram sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    me = persona.OWNER
    chats, files, next_id = [], [], 1
    for i, c in enumerate(CHATS, 1):
        chat, more, next_id = _chat(anchor, c, i, next_id if c["type"] != "public_channel"
                                    else 9001)
        chats.append(chat)
        files += more
    contacts = []
    for key in ("sam", "maya"):
        p = persona.person(key)
        added = persona.local_dt(anchor, -400, "12:00")
        contacts.append({"first_name": p.first, "last_name": p.last,
                         "phone_number": p.phone, **_stamp(added)})
    data = {
        "about": "Here is the data you requested. Remember: Telegram is ad free, "
                 "it doesn't sell your data and doesn't use it for ad targeting.",
        "personal_information": {
            "about": "This section has the personal information you provided.",
            "user_id": USER_IDS["me"], "first_name": me.first, "last_name": me.last,
            "phone_number": "+1 512 555 0100", "username": "@alexrivera_atx", "bio": ""},
        "contacts": {"about": "This is your contact list.", "list": contacts},
        "chats": {"about": "This page lists all chats from this export.", "list": chats},
    }
    out = dest / FOLDER.format(day=anchor.isoformat())
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "result.json", "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    for rel, data_bytes in files:
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data_bytes)
    return out
