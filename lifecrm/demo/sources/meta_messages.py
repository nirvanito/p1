"""Meta sample: Alex's Instagram DMs and Facebook Messenger, as each app's
"Download your information" export in JSON format.

The export is the same whichever phone Alex uses — it is made on Meta's
servers and downloaded in a browser — so both combos have this source and
get the same two zips:

  instagram-alexrivera.atx-<anchor>-<id>.zip
      your_instagram_activity/messages/inbox/<thread>/message_1.json (+ photos/)
      your_instagram_activity/messages/message_requests/<thread>/message_1.json
      personal_information/personal_information/personal_information.json
  facebook-alexrivera-<anchor>-<id>.zip
      your_facebook_activity/messages/inbox/<thread>/message_1.json (+ photos/)
      your_facebook_activity/messages/e2ee_cutover/<thread>/message_1.json
      personal_information/profile_information/profile_information.json

Written the way Meta writes them: two-space indents, messages newest first,
and every string's UTF-8 bytes escaped as Latin-1 code points — "jalapeño"
is "jalape\\u00c3\\u00b1o" in the file — which is the quirk the connector
repairs.

What the persona leaves to the export, defined here consistently with it:

- Maya shares the Austin Clay Studio wheel-throwing reel on Instagram the day
  before the gift-ideas note lists a ceramics class voucher; the birthday
  dinner at Uchi is planned there the night before, and her photo from it
  arrives that evening.
- The "Bled crew" group (Jake, Maya, Sam) lives on Instagram too: a shared
  post of the lake when the trip idea starts, the castle and Vintgar Gorge
  plan, Jake booking the lakeside place.
- Tía Rosa is on Facebook as Rosa Treviño (the WhatsApp sample knows only her
  number): the folding chairs for the family dinner, a photo from it, a call.
- Mom's pre-encryption Messenger thread sits in e2ee_cutover/.
- A boat-tour business's message request, which is counted, not imported.
- Notices that are not messages: "Liked a message", an unsent message, the
  group being named, a call.
"""

from __future__ import annotations

import json
import zipfile
from datetime import date, timezone
from pathlib import Path

from .. import persona

PLATFORMS = ("instagram", "messenger")
IG_USERNAME = "alexrivera.atx"
ROSA_NAME = "Rosa Treviño"
ZIP_NAME = {"instagram": "instagram-{user}-{day}-dEm0iG7x.zip",
            "messenger": "facebook-alexrivera-{day}-dEm0fB3k.zip"}
AREA = {"instagram": "your_instagram_activity", "messenger": "your_facebook_activity"}

# Smallest well-formed JPEG (8x8, mid-grey), so a sample photo opens.
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
# (offset, 'HH:MM', sender key, kind, payload). kind:
#   text      payload: the text
#   share     payload: (link, share_text, owner)
#   photo     payload: None (a photo file is written beside the thread)
#   liked     Instagram's "Liked a message"
#   unsent    "<name> unsent a message."
#   named     "<name> named the group <payload>."
#   call      "<name> called you." with call_duration = payload seconds
# and an optional reactions tuple of (person key, emoji).

THREADS = {
    "instagram": (
        {"folder": "mayachen_17841400000000187", "box": "inbox", "title": None,
         "members": ("maya",), "messages": (
             (-26, "20:15", "maya", "share",
              ("https://www.instagram.com/reel/DEMOclay26/",
               "Wheel throwing for beginners: six Tuesday nights, all clay included",
               "austinclaystudio"), ()),
             (-26, "20:16", "maya", "text",
              "I neeeed to take this class \N{SMILING FACE WITH HEART-SHAPED EYES}", ()),
             (-26, "20:20", "me", "text", "Noted \N{EYES}", ()),
             (-26, "20:21", "maya", "liked", None, ()),
             (-10, "22:30", "me", "text",
              "Birthday dinner tomorrow: Uchi at 7. Dress up a little \N{BIRTHDAY CAKE}",
              (("maya", "\N{HEAVY BLACK HEART}"),)),
             (-10, "22:34", "maya", "text", "Uchi?! yes \N{FACE HOLDING BACK TEARS}", ()),
             (-9, "23:40", "maya", "photo", None, (("me", "\N{HEAVY BLACK HEART}"),)),
             (-9, "23:41", "maya", "text",
              "Best birthday. That jalapeño yellowtail was unreal, and the voucher "
              "is going straight to the wheel class", ()),
             (-8, "08:05", "me", "text", "Coffee at Café Medici before work?", ()),
         )},
        {"folder": "bledcrew_17842600000000999", "box": "inbox", "title": "Bled crew",
         "members": ("jake", "maya", "sam"), "messages": (
             (-16, "15:05", "me", "named", "Bled crew", ()),
             (-16, "15:10", "jake", "share",
              ("https://www.instagram.com/p/DEMObled16/",
               "Sunrise over Lake Bled and the island church", "lakebled.views"), ()),
             (-16, "15:12", "sam", "text", "That water is unreal. Count me in", ()),
             (-16, "15:14", "maya", "text", "Only if we do Bled Castle this time", ()),
             (-2, "20:05", "maya", "text",
              "Castle first, then Vintgar Gorge \N{PERSON WITH FOLDED HANDS}", ()),
             (-2, "20:06", "jake", "liked", None, ()),
             (-2, "20:10", "sam", "unsent", None, ()),
             (-2, "20:12", "jake", "text", "Lakeside place is booked for the four of us",
              (("maya", "\N{HEAVY BLACK HEART}"), ("sam", "\N{FIRE}"))),
         )},
        {"folder": "lakebledboattours_17841400000000555", "box": "message_requests",
         "title": None, "members": ("Lake Bled Boat Tours",), "messages": (
             (-15, "09:00", "Lake Bled Boat Tours", "text",
              "Hi! Planning a trip to Slovenia? 20% off pletna rides this summer", ()),
         )},
    ),
    "messenger": (
        {"folder": "rosatrevino_10158234567890123", "box": "inbox", "title": None,
         "members": (ROSA_NAME,), "messages": (
             (-14, "10:12", ROSA_NAME, "text",
              "Alex mijo, can you bring the folding chairs mañana? "
              "\N{PERSON WITH FOLDED HANDS}", ()),
             (-14, "10:40", "me", "text", "Claro, Tía. See you at 5", ()),
             (-12, "09:05", ROSA_NAME, "photo", None, ()),
             (-12, "09:06", ROSA_NAME, "text",
              "Qué bonita familia \N{HEAVY BLACK HEART}\N{VARIATION SELECTOR-16}",
              (("me", "\N{HEAVY BLACK HEART}"),)),
             (-11, "19:00", ROSA_NAME, "call", 420, ()),
             (-11, "19:08", "me", "text", "Thanks for the flan recipe, Tía!", ()),
         )},
        {"folder": "lindarivera_10155512345678901", "box": "e2ee_cutover", "title": None,
         "members": ("mom",), "messages": (
             (-40, "19:15", "mom", "text", "Are you eating enough vegetables, mijo?", ()),
             (-40, "19:20", "me", "text", "Yes Mom \N{BROCCOLI}", ()),
         )},
    ),
}


def _name(who: str) -> str:
    if who == "me":
        return persona.OWNER.name
    try:
        return persona.person(who).name
    except StopIteration:
        return who                    # someone the persona has no card for


def _mojibake(value):
    """Meta's escaping: each string's UTF-8 bytes as Latin-1 code points."""
    if isinstance(value, str):
        return value.encode("utf-8").decode("latin-1")
    if isinstance(value, list):
        return [_mojibake(v) for v in value]
    if isinstance(value, dict):
        return {k: _mojibake(v) for k, v in value.items()}
    return value


def _ms(anchor: date, offset: int, hhmm: str, n: int) -> int:
    dt = persona.local_dt(anchor, offset, hhmm).astimezone(timezone.utc)
    return int(dt.timestamp()) * 1000 + 137 + 211 * n % 800


# ---------------------------------------------------------------- rendering

def _thread(anchor: date, platform: str, t: dict) -> tuple[dict, list[tuple[str, bytes]]]:
    """(message_1.json content, [(path in the export, bytes)])."""
    area = AREA[platform]
    base = f"{area}/messages/{t['box']}/{t['folder']}"
    people = [_name(m) for m in t["members"]] + [persona.OWNER.name]
    messages, files = [], []
    for n, (off, hhmm, who, kind, payload, reactions) in enumerate(t["messages"]):
        sender = _name(who)
        ms = _ms(anchor, off, hhmm, n)
        msg: dict = {"sender_name": sender, "timestamp_ms": ms}
        if kind == "text":
            msg["content"] = payload
        elif kind == "share":
            link, text, owner = payload
            msg["content"] = f"{sender} sent an attachment."
            msg["share"] = {"link": link, "share_text": text,
                            "original_content_owner": owner}
        elif kind == "photo":
            uri = f"{base}/photos/{ms // 1000}_{t['folder'].split('_')[-1]}_{n}.jpg"
            msg["photos"] = [{"uri": uri, "creation_timestamp": ms // 1000}]
            files.append((uri, _JPEG))
        elif kind == "liked":
            msg["content"] = "Liked a message"
        elif kind == "unsent":
            msg["content"] = f"{sender} unsent a message."
        elif kind == "named":
            msg["content"] = f"{sender} named the group {payload}."
        elif kind == "call":
            msg["content"] = f"{sender} called you."
            msg["call_duration"] = payload
        if reactions:
            msg["reactions"] = [
                {"reaction": emoji, "actor": _name(by)}
                | ({"timestamp": ms // 1000 + 60} if platform == "instagram" else {})
                for by, emoji in reactions]
        msg["is_geoblocked_for_viewer"] = False
        msg["is_unsent_image_by_messenger_kid_parent"] = False
        messages.append(msg)
    messages.sort(key=lambda m: -m["timestamp_ms"])          # Meta: newest first
    data = {
        "participants": [{"name": p} for p in people],
        "messages": messages,
        "title": t["title"] or people[0],
        "is_still_participant": True,
        "thread_path": f"{t['box']}/{t['folder']}",
        "magic_words": [],
    }
    return data, files


def _profile(platform: str) -> tuple[str, dict]:
    me = persona.OWNER
    if platform == "instagram":
        def field(value):
            return {"href": "", "value": value, "timestamp": 0}
        return ("personal_information/personal_information/personal_information.json",
                {"profile_user": [{"media_map_data": {}, "string_map_data": {
                    "Email": field(me.email), "Phone Confirmed": field("True"),
                    "Username": field(IG_USERNAME), "Name": field(me.name),
                    "Bio": field("Austin \N{TACO} | climbing | Lake Bled again soon"),
                    "Private Account": field("True")}}]})
    return ("personal_information/profile_information/profile_information.json",
            {"profile_v2": {"name": {"full_name": me.name, "first_name": me.first,
                                     "middle_name": "", "last_name": me.last},
                            "emails": {"emails": [me.email], "previous_emails": [],
                                       "pending_emails": [], "ad_account_emails": []}}})


def _dump(obj) -> bytes:
    return json.dumps(_mojibake(obj), indent=2, ensure_ascii=True).encode("ascii")


def write_sample(dest: Path, anchor: date, combo: str,
                 platform: str = "instagram") -> Path:
    """One app's export zip for `combo`, under `dest`; returns its path."""
    if combo not in persona.COMBOS:
        raise ValueError(f"no Meta sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    if platform not in PLATFORMS:
        raise ValueError(f"no Meta sample for platform '{platform}' "
                         f"(expected one of {', '.join(PLATFORMS)})")
    members: list[tuple[str, bytes]] = []
    for t in THREADS[platform]:
        data, files = _thread(anchor, platform, t)
        members.append((f"{AREA[platform]}/messages/{t['box']}/{t['folder']}/message_1.json",
                        _dump(data)))
        members += files
    path, profile = _profile(platform)
    members.append((path, _dump(profile)))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / ZIP_NAME[platform].format(user=IG_USERNAME, day=anchor.isoformat())
    # A fixed timestamp per member: the same (anchor, combo) gives the same
    # bytes, so the importer's "already imported" check sees the same file.
    stamp = (anchor.year, anchor.month, anchor.day, 12, 0, 0)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(members):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return out


def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """Both exports: Instagram, then Facebook Messenger."""
    return [write_sample(dest, anchor, combo, platform) for platform in PLATFORMS]
