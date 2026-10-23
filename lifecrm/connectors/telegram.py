"""Telegram connector: Telegram Desktop's chat export, in machine-readable JSON.

Telegram keeps your history in its cloud and gives it back through Telegram
Desktop (Windows, macOS, Linux — the phone apps cannot export) in two ways,
both writing a `result.json` beside folders of media when the format is set
to "Machine-readable JSON":

  Export chat history   one chat: {name, type, id, messages: [...]}
                        (a chat's ⋮ menu, written to ChatExport_<date>/)
  Export Telegram data  the account: {personal_information, contacts,
                        chats: {list: [{name, type, id, messages}]},
                        left_chats: {list: [...]}, ...}
                        (Settings > Advanced, written to DataExport_<date>/)

This connector reads those files. It never logs in, never talks to Telegram,
and never reads a photo's bytes.

Accepted targets
----------------
- result.json itself, the export folder holding it, or a .zip of that folder
- a folder holding several exports (Telegram Desktop's own download folder,
  which keeps every ChatExport_/DataExport_ it made), each read in turn
- nothing at all: `default_target` uses [connectors.telegram] exports_dir,
  else ~/Downloads/Telegram Desktop when it holds an export

An export made in HTML (the default) holds messages.html or
export_results.html instead, and is refused in one line: export again as
JSON.

What comes in
-------------
One observation per message, stream 'telegram', titled with the chat's name.

- body: the text — a plain string, or Telegram's list of runs (bold, links,
  mentions) flattened to the text it shows; a poll's question when a poll
  is all there is.
- value_json: group (anything but a one-to-one chat), chat_type (when not
  personal_chat), reply_to (the message id answered), edited, forwarded,
  media {kind, file, mime_type, duration_seconds, sticker_emoji, omitted} —
  file is the path inside the export, never the bytes; `omitted` when the
  export settings left the file out —, location {lat, lon}, venue, poll
  {question, answers}, contact_shared, reactions [{reaction, count, by}]
  with `by` handle values.
- media_path: the file's path inside the export, when it is there.
- Service messages — calls, joins, pins, title and photo changes, group
  creation — are counted as 'service: <action>' and never imported.
- Channels, public supergroups and bots are counted and left out (a channel
  is a broadcast, not a conversation; a bot is a program).
  [connectors.telegram] include_channels = true imports channels and public
  supergroups; bot chats always stay out.
- Sections of a full export that are not chats (contacts, sessions, ...)
  are listed in the report and not imported: address books are the
  contacts connector's.

Identity
--------
Telegram has real ids: 'tg:<chat id>:<message id>', unique and stable, so a
later export of the same chat is a no-op for what is already stored. An edit
carries its own time (edited_unixtime), which becomes last_modified_at: a
message edited after an import *updates* on the next one, newer-wins, as do
reactions added since (their dates count too).

Handles: Telegram's user ids
----------------------------
Every sender has an id, 'from_id': 'user123456789'. It becomes app_account
'telegram:user123456789' with the name shown ('from') as display_name — an
id that survives renames and a deleted account alike. The other side of a
one-to-one chat is the chat's own id. Linking the handle once
(`handles link`) attributes every message, past and future.

Which sender is you: the full export's personal_information names your
user id. A single-chat export does not; then [connectors.telegram] me =
"user123456789" (or your name as shown), else the sender in a one-to-one
chat who is not the chat, or the sender of Saved Messages. Without any of
those, direction stays unknown and the report says so. Saved Messages are
direction 'self'.

Deletion semantics — the archive remembers
------------------------------------------
An export is what you asked for: one chat, a date range ("From"/"To" in the
export dialog), chosen chat types, media size limits. A message absent from
a later export may simply be outside it, and nothing in result.json says
which range was chosen. So this connector declares no `present_ids` and
removes nothing; `imports retract` undoes an import.

Known limitations, by design
----------------------------
- result.json is read whole. A full export of a busy account can be
  hundreds of MB; export chats separately, or a date range, if memory runs
  short.
- Times come from date_unixtime (UTC). An export old enough to lack it is
  read from 'date' in the configured home timezone.
- Calls are service messages and are counted, not imported.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "telegram"
streams = ("telegram",)

RESULT = "result.json"
_HTML = ("messages.html", "export_results.html")
_GROUPS = ("private_group", "private_supergroup")
_CHANNELS = ("public_channel", "private_channel", "public_supergroup")
_KNOWN_TYPES = ("personal_chat", "saved_messages", "bot_chat", *_GROUPS, *_CHANNELS)
_SECTIONS = ("about", "personal_information", "chats", "left_chats")
_MEDIA_KINDS = {"sticker": "sticker", "voice_message": "voice", "video_message": "video note",
                "animation": "gif", "video_file": "video", "audio_file": "audio"}
# Keys a message carries besides what it says.
_BOOKKEEPING = {"id", "type", "date", "date_unixtime", "edited", "edited_unixtime",
                "from", "from_id", "text", "text_entities", "reply_to_message_id",
                "reply_to_peer_id", "forwarded_from", "forwarded_from_id", "saved_from",
                "via_bot", "author", "signature", "reactions", "inline_bot_buttons",
                "width", "height", "thumbnail", "thumbnail_file_size", "photo_file_size",
                "file_size", "file_name", "performer", "title", "media_spoiler",
                "self_destruct_period_seconds"}


# ---------------------------------------------------------------- finding exports

def _load(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{label} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{label} is not valid JSON: line {e.lineno} "
                          f"column {e.colno}: {e.msg}") from e


def _html_refusal(label: str) -> DomainError:
    return DomainError(
        f"{label} is a Telegram export in HTML format — this reader needs JSON: "
        f"export again from Telegram Desktop with Format: Machine-readable JSON")


def _folder_names(folder: Path) -> list[str]:
    out = []
    for top, dirs, files in os.walk(folder):
        dirs.sort()
        rel = Path(top).relative_to(folder).as_posix()
        out += [f if rel == "." else f"{rel}/{f}" for f in sorted(files)]
    return out


def _from_folder(folder: Path, label: str) -> dict:
    if not (folder / RESULT).is_file():
        if any((folder / h).is_file() for h in _HTML):
            raise _html_refusal(label)
        listing = ", ".join(sorted(p.name for p in folder.iterdir())[:10]) or "(empty)"
        raise DomainError(f"{label} holds no {RESULT} (has: {listing})")
    raw = (folder / RESULT).read_bytes()
    return {"label": label, "raw": raw, "names": set(_folder_names(folder)), "base": ""}


def _from_zip(path: Path) -> list[dict]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DomainError(f"cannot open {path.name} as a zip: {e}") from e
    with zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        results = [n for n in names if PurePosixPath(n).name == RESULT]
        if not results:
            if any(PurePosixPath(n).name in _HTML for n in names):
                raise _html_refusal(path.name)
            listing = ", ".join(names[:10]) or "(empty)"
            raise DomainError(f"{path.name} holds no {RESULT} (members: {listing})")
        out = []
        for member in results:
            parent = PurePosixPath(member).parent.as_posix()
            prefix = "" if parent == "." else parent + "/"
            out.append({"label": path.name if len(results) == 1 else f"{path.name}/{member}",
                        "raw": zf.read(member), "base": prefix,
                        "names": {n[len(prefix):] for n in names if n.startswith(prefix)}})
    return out


def _is_export_folder(p: Path) -> bool:
    return p.is_dir() and ((p / RESULT).is_file() or any((p / h).is_file() for h in _HTML))


def _exports(target: Path) -> tuple[list[dict], str, list[str]]:
    """[{label, raw, names, base}], what kind of target it was, and the HTML
    exports a folder of exports held beside them (left out)."""
    if target.is_file():
        if zipfile.is_zipfile(target):
            return _from_zip(target), "zip", []
        if target.name.lower() == RESULT:
            return [_from_folder(target.parent, target.name)], "file", []
        if target.suffix.lower() in (".html", ".htm"):
            raise _html_refusal(target.name)
        raise DomainError(f"{target.name} is not a Telegram export — pass result.json, "
                          f"the export folder holding it, or a .zip of that folder")
    if not target.is_dir():
        raise DomainError(f"not found: {target}")
    if _is_export_folder(target):
        return [_from_folder(target, target.name)], "folder", []
    found, html = [], []
    entries = sorted(target.iterdir(), key=lambda p: p.name)
    for p in entries:
        if _is_export_folder(p):
            if (p / RESULT).is_file():
                found.append(_from_folder(p, p.name))
            else:
                html.append(p.name)
        elif p.is_file() and p.suffix.lower() == ".zip" and zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                holds = any(PurePosixPath(n).name == RESULT for n in zf.namelist())
            if holds:
                found += _from_zip(p)
    if not found and html:
        raise _html_refusal(html[0])
    if not found:
        listing = ", ".join(p.name for p in entries[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no Telegram export — looked for {RESULT} in it and "
            f"in the folders and zips inside it (has: {listing})")
    return found, "folder", html


def default_target(cfg: Config) -> Path:
    """[connectors.telegram] exports_dir, else Telegram Desktop's own export
    folder, ~/Downloads/Telegram Desktop, when it holds an export."""
    configured = cfg.connector_settings(name).get("exports_dir")
    if configured:
        p = cfg.resolve(configured)
        if not p.is_dir():
            raise DomainError(f"[connectors.telegram] exports_dir {p} is not a folder")
        return p
    folder = Path.home() / "Downloads" / "Telegram Desktop"
    try:
        if folder.is_dir() and (_is_export_folder(folder) or any(
                _is_export_folder(p) for p in folder.iterdir())):
            return folder
    except OSError:
        pass
    raise DomainError(
        f"no Telegram export found in {folder} — export from Telegram Desktop as "
        f"Machine-readable JSON (docs/telegram.md) and pass the folder, or set "
        f"[connectors.telegram] exports_dir")


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> tuple[list[str], bool]:
    s = cfg.connector_settings(name)
    me = s.get("me")
    if me is None or me == "":
        names: list[str] = []
    elif isinstance(me, str):
        names = [me.strip()]
    elif isinstance(me, list) and all(isinstance(x, str) for x in me):
        names = [x.strip() for x in me if x.strip()]
    else:
        raise DomainError(f"[connectors.telegram] me must be your user id (user123...) "
                          f"or your name as shown, or a list of them — got {me!r}")
    include = s.get("include_channels", False)
    if not isinstance(include, bool):
        raise DomainError(f"[connectors.telegram] include_channels must be true or "
                          f"false — got {include!r}")
    return names, include


# ---------------------------------------------------------------- one message

def flatten(text) -> str:
    """The text a message shows: Telegram writes it as a string, or as a list
    of plain strings and {type, text} runs (bold, link, mention, ...)."""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(p if isinstance(p, str)
                       else p.get("text") if isinstance(p, dict)
                       and isinstance(p.get("text"), str) else "" for p in text)
    return ""


def _peer(value) -> str | None:
    """'user123' as written; a bare number (older exports) as 'user<number>'."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return f"user{value}"
    if isinstance(value, str) and value.strip():
        v = value.strip()
        return f"user{v}" if v.isdigit() else v
    return None


def _instant(msg: dict, unix_key: str, local_key: str, zone: ZoneInfo) -> str | None:
    raw = msg.get(unix_key)
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return base.iso_utc(raw)
    local = msg.get(local_key)
    if isinstance(local, str) and local.strip():
        try:
            dt = datetime.fromisoformat(local.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _included(path) -> str | None:
    """A media path as written, or None when the export left the file out
    ('(File not included. Change data exporting settings to download.)')."""
    if isinstance(path, str) and path.strip() and not path.strip().startswith("("):
        return path.strip()
    return None


def _media(msg: dict) -> dict | None:
    if "photo" in msg:
        m = {"kind": "photo"}
        f = _included(msg["photo"])
    elif "file" in msg:
        m = {"kind": _MEDIA_KINDS.get(msg.get("media_type"), "file")}
        f = _included(msg["file"])
    else:
        return None
    if f:
        m["file"] = f
    else:
        m["omitted"] = True
    for key in ("mime_type", "sticker_emoji"):
        if isinstance(msg.get(key), str) and msg[key]:
            m[key] = msg[key]
    if isinstance(msg.get("duration_seconds"), int):
        m["duration_seconds"] = msg["duration_seconds"]
    return m


# ---------------------------------------------------------------- scan

def _chats(data, label: str) -> tuple[list[dict], dict | None, str, dict]:
    """(chats, personal_information, which kind of export, other sections)."""
    if not isinstance(data, dict):
        raise DomainError(f"{label}: {RESULT} is a {type(data).__name__}, expected an "
                          f"object — is this a Telegram export?")
    if "messages" in data:
        if not isinstance(data["messages"], list):
            raise DomainError(f"{label}: 'messages' in {RESULT} is not a list")
        return [data], None, "single chat", {}
    if "chats" in data or "personal_information" in data:
        chats = []
        for key in ("chats", "left_chats"):
            section = data.get(key)
            items = section.get("list") if isinstance(section, dict) else None
            if isinstance(items, list):
                chats += [dict(c, _left=key == "left_chats") for c in items
                          if isinstance(c, dict)]
        other = {}
        for key in sorted(data):
            if key in _SECTIONS:
                continue
            v = data[key]
            items = v.get("list") if isinstance(v, dict) else v
            other[key] = len(items) if isinstance(items, list) else 1
        info = data.get("personal_information")
        return chats, info if isinstance(info, dict) else None, "full export", other
    keys = ", ".join(sorted(data)[:8]) or "(none)"
    raise DomainError(f"{label}: {RESULT} has neither 'messages' (a chat export) nor "
                      f"'chats' (a full export) — has: {keys}")


def _owner(chats: list[dict], info: dict | None, me: list[str]) -> dict:
    """{'id', 'label', 'source', 'warning'} across every export in the scan."""
    names: dict[str, str] = {}
    for c in chats:
        for m in c["messages"]:
            if m.get("type") == "message" and _peer(m.get("from_id")):
                names.setdefault(_peer(m.get("from_id")), m.get("from") or None)
    if info and _peer(info.get("user_id")):
        uid = _peer(info["user_id"])
        shown = " ".join(x for x in (info.get("first_name"), info.get("last_name"))
                         if isinstance(x, str) and x.strip()) or names.get(uid)
        return {"id": uid, "label": shown, "source": "the export's personal_information",
                "warning": None}
    if me:
        want = {x.casefold() for x in me}
        hit = next((i for i in sorted(names) if i.casefold() in want
                    or (names[i] or "").casefold() in want), None)
        if hit:
            return {"id": hit, "label": names[hit], "source": "[connectors.telegram] me",
                    "warning": None}
        ids = [x for x in me if x.lower().startswith("user")]
        return {"id": ids[0] if ids else None, "label": None,
                "source": "[connectors.telegram] me",
                "warning": (f"no sender in this export is [connectors.telegram] me "
                            f"({', '.join(me)}) — check it is your user id or your "
                            f"name as shown")}
    candidates: dict[str, str] = {}
    for c in chats:
        senders = {_peer(m.get("from_id")) for m in c["messages"]
                   if m.get("type") == "message"} - {None}
        if c.get("type") == "saved_messages":
            for s in senders:
                candidates.setdefault(s, "the sender of Saved Messages")
        elif c.get("type") == "personal_chat" and _peer(c.get("id")):
            for s in senders - {_peer(c["id"])}:
                candidates.setdefault(s, f"the other sender in the chat with {c.get('name')}")
    if len(candidates) == 1:
        uid, why = next(iter(candidates.items()))
        return {"id": uid, "label": names.get(uid),
                "source": f"inferred: {why}; set [connectors.telegram] me to be sure",
                "warning": None}
    why = ("one-to-one chats disagree about which sender is you" if candidates
           else "nothing in the export says which sender is you")
    return {"id": None, "label": None, "source": None,
            "warning": (f"{why} — direction stays unknown, and changing it after "
                        f"--apply is a conflict, not an update; set "
                        f"[connectors.telegram] me = \"user<your id>\" first")}


def _chat_title(c: dict) -> str:
    title = c.get("name")
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())
    if c.get("type") == "saved_messages":
        return "Saved Messages"
    return "Deleted Account" if c.get("type") == "personal_chat" else "(unnamed chat)"


def scan(target: Path, cfg: Config) -> base.Scan:
    me, include_channels = _settings(cfg)
    zone = ZoneInfo(cfg.timezone)
    try:
        exports, target_kind, html = _exports(target)
    except OSError as e:
        raise DomainError(f"cannot read {target.name}: {e}") from e
    exports.sort(key=lambda e: e["label"])
    h = hashlib.sha256()
    loaded = []
    for e in exports:
        # Content, not the label: the folder, its result.json and a zip of it
        # are the same export, and the dry run should say "already imported".
        h.update(hashlib.sha256(e["raw"]).digest())
        h.update("\0".join(sorted(e["names"])).encode())
        chats, info, kind, other = _chats(_load(e["raw"], f"{e['label']}: {RESULT}"),
                                          e["label"])
        for c in chats:
            if not isinstance(c.get("messages"), list):
                c["messages"] = []
            c["messages"] = [m for m in c["messages"] if isinstance(m, dict)]
            c["_names"] = e["names"]
        loaded.append({"export": e, "chats": chats, "info": info, "kind": kind,
                       "other": other})
    all_chats = [c for x in loaded for c in x["chats"]]
    info = next((x["info"] for x in loaded if x["info"]), None)
    owner = _owner(all_chats, info, me)

    handles: dict[str, dict] = {}

    def seen(peer: str, shown) -> dict:
        k = peer.casefold()
        if k not in handles:
            handles[k] = {"kind": "app_account", "value": f"telegram:{peer}",
                          "display_name": shown if isinstance(shown, str)
                          and shown.strip() else None}
        elif handles[k]["display_name"] is None and isinstance(shown, str) and shown.strip():
            handles[k]["display_name"] = shown
        return {"kind": "app_account", "value": handles[k]["value"]}

    owner_part = seen(owner["id"], owner["label"]) if owner["id"] else None
    records: list[dict] = []
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    media = {"present": 0, "missing": 0, "omitted": 0}
    chat_facts = []
    warnings = [owner["warning"]] if owner["warning"] else []
    if html:
        warnings.append(f"left out {', '.join(html)}: exported as HTML, which this "
                        f"reader cannot use — export those chats again as "
                        f"Machine-readable JSON")

    def skip(reason: str, n: int = 1) -> None:
        skipped[reason] = skipped.get(reason, 0) + n

    for c in sorted(all_chats, key=lambda c: (str(c.get("id")), _chat_title(c))):
        ctype = c.get("type")
        title = _chat_title(c)
        msgs = c["messages"]
        if ctype == "bot_chat":
            skip("message in a bot chat", len(msgs))
            continue
        if ctype in _CHANNELS and not include_channels:
            skip("message in a channel or public group (include_channels = true "
                 "imports them)", len(msgs))
            continue
        if ctype not in _KNOWN_TYPES:
            what = f"chat of type '{ctype}'"
            unmapped[what] = unmapped.get(what, 0) + len(msgs)
            continue
        chat_id = c.get("id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)) \
                or not str(chat_id).strip():
            skip("message in a chat with no id", len(msgs))
            continue
        chat_id = str(chat_id).strip()
        group = ctype not in ("personal_chat", "saved_messages")
        saved = ctype == "saved_messages"
        # The other side of a one-to-one chat is the chat's own id, named as
        # the export names the chat (nothing, for a deleted account).
        peer = seen(_peer(c["id"]), c.get("name")) if ctype == "personal_chat" else None
        voices = {}
        for m in msgs:
            p = _peer(m.get("from_id"))
            if m.get("type") == "message" and p:
                voices.setdefault(p, seen(p, m.get("from")))
        kept = []
        for m in msgs:
            mtype = m.get("type")
            if mtype == "service":
                skip(f"service: {m.get('action') or 'unknown'}")
                continue
            if mtype != "message":
                what = f"message of type '{mtype}'"
                unmapped[what] = unmapped.get(what, 0) + 1
                continue
            mid = m.get("id")
            if isinstance(mid, bool) or not isinstance(mid, (int, str)) or not str(mid).strip():
                skip("message with no id")
                continue
            started = _instant(m, "date_unixtime", "date", zone)
            if started is None:
                skip("message with no usable timestamp")
                continue
            sender_id = _peer(m.get("from_id"))
            if sender_id is None:
                skip("message with no sender id")
                continue
            body = flatten(m.get("text")) or flatten(m.get("text_entities"))
            body = body.strip()
            detail: dict = {}
            if group:
                detail["group"] = True
            if ctype != "personal_chat":
                detail["chat_type"] = ctype
            if isinstance(m.get("reply_to_message_id"), int):
                detail["reply_to"] = m["reply_to_message_id"]
            edited = _instant(m, "edited_unixtime", "edited", zone)
            if edited:
                detail["edited"] = True
            if m.get("forwarded_from") is not None or m.get("forwarded_from_id"):
                detail["forwarded"] = True
            md = _media(m)
            media_path = None
            if md:
                detail["media"] = md
                if md.get("file"):
                    present = md["file"] in c["_names"]
                    media_path = md["file"] if present else None
                    media["present" if present else "missing"] += 1
                else:
                    media["omitted"] += 1
            loc = m.get("location_information")
            if isinstance(loc, dict) and isinstance(loc.get("latitude"), (int, float)) \
                    and isinstance(loc.get("longitude"), (int, float)):
                detail["location"] = {"lat": loc["latitude"], "lon": loc["longitude"]}
            if isinstance(m.get("place_name"), str) and m["place_name"].strip():
                detail["venue"] = m["place_name"].strip()
            poll = m.get("poll")
            if isinstance(poll, dict) and isinstance(poll.get("question"), str):
                answers = poll.get("answers")
                detail["poll"] = {"question": poll["question"],
                                  "answers": len(answers) if isinstance(answers, list) else 0}
                body = body or poll["question"].strip()
            if isinstance(m.get("contact_information"), dict):
                detail["contact_shared"] = True
            modified = max(x for x in (started, edited) if x)
            reactions = []
            for r in m.get("reactions") or []:
                if not isinstance(r, dict):
                    continue
                emoji = r.get("emoji") or r.get("document_id") or r.get("type")
                if not isinstance(emoji, str):
                    continue
                by = []
                for who in r.get("recent") or []:
                    if isinstance(who, dict) and _peer(who.get("from_id")):
                        by.append(seen(_peer(who["from_id"]), who.get("from"))["value"])
                        when = _instant(who, "date_unixtime", "date", zone)
                        if when and when > modified:
                            modified = when
                entry = {"reaction": emoji, "by": sorted(by)}
                if isinstance(r.get("count"), int):
                    entry["count"] = r["count"]
                reactions.append(entry)
            if reactions:
                detail["reactions"] = sorted(reactions, key=lambda r: r["reaction"])
            if not body and not md and not any(
                    k in detail for k in ("location", "venue", "poll", "contact_shared")):
                extra = sorted(k for k in m if k not in _BOOKKEEPING
                               and m[k] not in (None, "", [], {}))
                if extra:
                    what = f"message with no text, only: {', '.join(extra)}"
                    unmapped[what] = unmapped.get(what, 0) + 1
                else:
                    skip("empty message")
                continue

            sender = voices.get(sender_id) or seen(sender_id, m.get("from"))
            mine = owner["id"] is not None and sender_id.casefold() == owner["id"].casefold()
            if saved:
                recipients = []
            elif group:
                recipients = [v for k, v in sorted(voices.items()) if k != sender_id]
                if owner_part and not mine and owner_part not in recipients:
                    recipients.append(owner_part)
            else:
                other = peer if sender != peer else owner_part
                recipients = [other] if other and other != sender else []
            participants = [{**sender, "role": "sender"}] + \
                [{**r, "role": "recipient"} for r in recipients]
            records.append({
                "stream": "telegram",
                "external_id": f"tg:{chat_id}:{str(mid).strip()}",
                "journal_date": base.journal_date_for_iso(started, cfg),
                "started_at": started,
                "ended_at": None,
                "utc_offset_sec": None,
                "direction": "self" if saved else
                             ("out" if mine else "in") if owner["id"] else None,
                "title": title,
                "body": body or None,
                "value": None,
                "unit": None,
                "value_json": canonical_json(detail),
                "media_path": media_path,
                "origin_app": "telegram",
                "last_modified_at": modified,
                "participants": participants,
            })
            kept.append(records[-1]["journal_date"])
        chat_facts.append({"chat": title, "id": chat_id, "type": ctype,
                           "group": group, "left": bool(c.get("_left")),
                           "messages": len(kept),
                           "first": min(kept) if kept else None,
                           "last": max(kept) if kept else None})

    other_sections: dict[str, int] = {}
    for x in loaded:
        for k, n in x["other"].items():
            other_sections[k] = other_sections.get(k, 0) + n
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    return base.empty_scan(
        name,
        account=f"telegram:{owner['id']}" if owner["id"] else None,
        source_file=target.name,
        source_hash=h.hexdigest(),
        records=records,
        handles=[handles[k] for k in sorted(handles)],
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        # No present_ids: an export is a chosen range of chosen chats, so what
        # it lacks is not evidence of a deletion. The archive remembers.
        facts={"exports": [{"export": x["export"]["label"], "kind": x["kind"],
                            "chats": len(x["chats"])} for x in loaded],
               "chats": sorted(chat_facts, key=lambda f: (-f["messages"], f["chat"],
                                                          f["id"])),
               "you": owner["label"] or owner["id"], "you_id": owner["id"],
               "you_from": owner["source"], "other_sections": other_sections,
               "media": media, "warnings": warnings},
        target_kind=target_kind)


# ---------------------------------------------------------------- report

_SHOWN_CHATS = 10


def _sample(rec: dict) -> str:
    who = {"out": "you", "in": "them", "self": "saved"}.get(rec["direction"], "?")
    body = rec["body"]
    if body is None:
        detail = json.loads(rec["value_json"]) if rec["value_json"] else {}
        body = f"[{detail['media']['kind']}]" if "media" in detail else "[message]"
    body = " ".join(body.split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    lines = [f"telegram: {scan['source_file']} ("
             + "; ".join(f"{e['kind']}, {e['chats']} chat(s)" for e in facts["exports"])
             + ")"]
    if len(facts["exports"]) > 1:
        lines.append("  exports: " + ", ".join(e["export"] for e in facts["exports"]))
    if facts["you_id"]:
        lines.append(f"  you: {facts['you']} (telegram:{facts['you_id']}, "
                     f"{facts['you_from']})")
    chats = facts["chats"]
    kinds = {"personal_chat": "one-to-one", "saved_messages": "saved messages"}
    for f in chats[:_SHOWN_CHATS]:
        span = f" {f['first']}..{f['last']}" if f["first"] else ""
        left = " | left" if f["left"] else ""
        lines.append(f"  chat: {f['chat']} — {kinds.get(f['type'], 'group')}, "
                     f"{f['messages']} message(s){span}{left}")
    if len(chats) > _SHOWN_CHATS:
        lines.append(f"  … and {len(chats) - _SHOWN_CHATS} more chat(s)")
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

    people = [h for h in scan["handles"] if h["value"] != scan["account"]]
    if people:
        lines.append(f"  handles: {len(people)} Telegram account(s), by user id — "
                     f"review with 'lifecrm handles list --suggest'")
    m = facts["media"]
    if any(m.values()):
        lines.append(f"  media: {m['present']} file(s) in the export (path kept, never "
                     f"the bytes), {m['missing']} named but not in the export, "
                     f"{m['omitted']} left out by the export settings")
    if facts["other_sections"]:
        lines.append("  not chats (not imported): " + ", ".join(
            f"{k} ({n})" for k, n in facts["other_sections"].items()))
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:16]}…: differs "
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
