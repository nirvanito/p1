"""Meta messages connector: Instagram DMs and Facebook Messenger, from the
"Download your information" export in JSON format.

Meta has no API for message history, but both apps hand the whole account
over through Accounts Center > Your information and permissions > Download
your information. Asked for in **JSON** (the default is HTML), the export is
a zip — big accounts arrive as several parts — whose messages are one folder
per conversation:

  Instagram   your_instagram_activity/messages/inbox/<thread>/message_1.json
              (exports before 2023: messages/inbox/<thread>/message_1.json)
  Facebook    your_facebook_activity/messages/inbox/<thread>/message_1.json
              (also your_activity_across_facebook/..., and messages/... in
              older exports)

Beside inbox/ sit archived_threads/, e2ee_cutover/ (Messenger chats from
before end-to-end encryption), message_requests/ and filtered_threads/. A
long conversation is split into message_1.json, message_2.json, ... —
message_1 the newest, each file newest-first. This connector reads those
files and the export's own profile. It never logs in, never calls Meta, and
never reads a photo's bytes.

Accepted targets
----------------
- one export: the .zip, or the folder an unzipped export leaves
- a folder holding exports — the parts of a split export, or an Instagram
  and a Facebook export side by side — each read in turn; other files are
  ignored
- nothing at all: `default_target` uses [connectors.meta_messages]
  exports_dir, else ~/Downloads when it holds an export named the way Meta
  names them (instagram-<user>-<date>-<id>.zip, facebook-...)

An export asked for in HTML holds message_1.html instead and is refused in
one line: re-request it in JSON. HTML is a page layout, not data.

The text quirk
--------------
Meta writes every string as its UTF-8 *bytes*, each escaped as if it were a
Latin-1 character: "é" arrives as "\\u00c3\\u00a9", which a JSON reader
turns into "Ã©". Every string is repaired with
s.encode("latin-1").decode("utf-8") when that round-trips — it cannot for
text that is already right (anything past U+00FF, or a lone Latin-1 accent),
so correct text passes through unchanged. The report counts the repairs.

What comes in
-------------
One observation per message: stream 'instagram_dm' or 'messenger', titled
with the conversation's name, body the message's own text.

- value_json: group (more than two participants), folder (when not the
  inbox), share {link, text, owner} for a shared post or reel, media
  [{kind, uri}] for photos, videos, audio, files, GIFs and stickers — uri is
  the file's path inside the export, never its bytes —, reactions [{by,
  reaction}] with `by` a handle value, and attachment_missing when Meta says
  something was sent but exported nothing of it.
- media_path: the first attachment's path inside the export, when the file
  is there.
- Meta's own wording for what is not a message is counted, never imported:
  "Liked a message", "reacted ❤ to your message", "unsent a message",
  calls ("called you", "started a video chat", call_duration), group changes
  ("named the group", "added X to the group", "left the group", nicknames,
  themes), connection notices. "sent an attachment." beside the attachment
  is Meta's placeholder, not the person's text, and is taken off.
- Message requests (message_requests/, filtered_threads/) are counted and
  left out — most are strangers and spam — unless [connectors.meta_messages]
  include_requests = true.

Identity
--------
The export carries no message ids, so one is derived: 'meta:' + SHA-1 of the
platform, the conversation's folder name (e.g. 'mayachen_17841400123'), the
instant in milliseconds, the sender as shown, the text (with what was
attached), and the ordinal among messages identical in all of those. The
folder name, not the folder it sits in: a conversation archived since the
last export keeps its ids. Re-importing a later export is a no-op for what
is already stored. A message's last_modified_at is its sent time, or the
newest reaction's when Instagram dates them: a reaction added since the
last import updates the row; on Messenger, whose reactions carry no time,
it is a conflict (kept existing) instead.

Senders are handles, never names
--------------------------------
The export names people only as the app displayed them — no account id, no
username, no number. So a sender is app_account 'instagram:<name as shown>'
or 'messenger:<name as shown>', with that name as display_name: the export's
only identifier for the person. Someone who renames their profile becomes a
second handle (link both); two people with one name are one handle. A
deleted account shows as the generic 'Instagram User' / 'Facebook user', so
that handle is scoped to its conversation ('instagram:Instagram User
(<folder>)') — otherwise every deleted account would be one person.

Which participant is you: [connectors.meta_messages] me = "Alex Rivera" (a
name or a list, as your messages show it); else the export's own profile
(personal_information.json on Instagram, profile_information.json on
Facebook); else the one participant common to every conversation. Without
any of those, direction stays unknown and the report says so loudly:
direction is recorded once, and changing it later is a conflict.

Deletion semantics — the archive remembers
------------------------------------------
Texts mirror the phone (owner's decision, 2026-09-24), because each backup
is the account's complete current state. A Meta export need not be: the
request form offers a date range ("last year", a custom range), so a later
export can lack years of messages nobody deleted, and nothing in the files
says which range was asked for. A message unsent in the app is gone from
the export without a trace. This connector declares no `present_ids` and
removes nothing; `imports retract` undoes an import.

Known limitations, by design
----------------------------
- Times are UTC instants (timestamp_ms); journal dates use the configured
  home timezone and rollover, like every chat export.
- No edit history: an edit made after an import arrives as a new message
  beside the old wording.
- Messenger's end-to-end encrypted chats after the cutover are not in this
  export at all — Messenger offers them as a separate secure-storage
  download, in another format, which this connector does not read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "meta_messages"
streams = ("instagram_dm", "messenger")

PLATFORMS = {
    "instagram": {"stream": "instagram_dm", "label": "Instagram"},
    "messenger": {"stream": "messenger", "label": "Messenger"},
}
_PLATFORM_SETTINGS = {"instagram": "instagram", "ig": "instagram",
                      "messenger": "messenger", "facebook": "messenger"}
_AREAS = {"your_instagram_activity": "instagram",
          "your_facebook_activity": "messenger",
          "your_activity_across_facebook": "messenger"}
_REQUEST_BOXES = ("message_requests", "filtered_threads")
# root: whatever folder the export sits in, inside a zip that wraps it; lazy,
# so that a layout folder is read as the area, never swallowed into the root.
_MSG_FILE = re.compile(
    r"(?P<root>(?:.*?/)??)(?:(?P<area>your_instagram_activity|your_facebook_activity"
    r"|your_activity_across_facebook)/)?messages/(?P<box>inbox|archived_threads"
    r"|message_requests|filtered_threads|e2ee_cutover)/(?P<thread>[^/]+)/"
    r"message_(?P<n>\d+)\.(?P<ext>json|html)", re.I)
_PROFILE_FILES = ("personal_information.json", "profile_information.json")
# What Meta shows for a deleted or deactivated account: a name shared by all
# of them, so it cannot be an identity on its own.
_PLACEHOLDER_NAMES = {"instagram user", "facebook user", "facebook member",
                      "deleted user"}
# A zip or folder in Downloads named the way Meta names an export.
_EXPORT_NAME = re.compile(r"(?:instagram|facebook|messenger|meta)[-_].+", re.I)
_LAYOUT_DIRS = ("your_instagram_activity", "your_facebook_activity",
                "your_activity_across_facebook", "messages")


# ---------------------------------------------------------------- the text quirk

def repair(text: str) -> str:
    """Undo Meta's escaping: UTF-8 bytes written as Latin-1 code points.

    Only when the string survives the round trip — text that is already
    right (anything past U+00FF, or a lone accented Latin-1 letter) does not,
    and is kept as it is."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _repaired(value, counter: list[int]):
    """Every string in a parsed JSON value, repaired; counter[0] counts the
    strings that changed."""
    if isinstance(value, str):
        fixed = repair(value)
        if fixed != value:
            counter[0] += 1
        return fixed
    if isinstance(value, list):
        return [_repaired(v, counter) for v in value]
    if isinstance(value, dict):
        return {k: _repaired(v, counter) for k, v in value.items()}
    return value


def _load(raw: bytes, label: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{label} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{label} is not valid JSON: line {e.lineno} "
                          f"column {e.colno}: {e.msg}") from e


# ---------------------------------------------------------------- finding exports

def _zip_container(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
    except (zipfile.BadZipFile, OSError) as e:
        raise DomainError(f"cannot open {path.name} as a zip: {e}") from e
    return {"label": path.name, "kind": "zip", "path": path, "names": names}


def _folder_container(path: Path, label: str) -> dict:
    names = []
    for top, dirs, files in os.walk(path):
        dirs.sort()
        rel = Path(top).relative_to(path).as_posix()
        for f in files:
            names.append(f if rel == "." else f"{rel}/{f}")
    return {"label": label, "kind": "folder", "path": path, "names": sorted(names)}


def _has_messages(names: list[str]) -> bool:
    return any(_MSG_FILE.fullmatch(n) for n in names)


def _is_layout_folder(p: Path) -> bool:
    return any((p / d).is_dir() for d in _LAYOUT_DIRS)


def _containers(target: Path) -> tuple[list[dict], str]:
    """[container] and what kind of target it was."""
    if target.is_file():
        if zipfile.is_zipfile(target):
            return [_zip_container(target)], "zip"
        raise DomainError(
            f"{target.name} is not a Meta export — pass the .zip that 'Download "
            f"your information' made (or the folder it unzips to), not one file "
            f"out of it")
    if not target.is_dir():
        raise DomainError(f"not found: {target}")
    if _is_layout_folder(target):
        return [_folder_container(target, target.name)], "folder"
    found = []
    entries = sorted(target.iterdir(), key=lambda p: p.name)
    for p in entries:
        if p.is_file() and p.suffix.lower() == ".zip" and zipfile.is_zipfile(p):
            c = _zip_container(p)
            if _has_messages(c["names"]):
                found.append(c)
        elif p.is_dir() and _is_layout_folder(p):
            c = _folder_container(p, p.name)
            if _has_messages(c["names"]):
                found.append(c)
    if not found:
        listing = ", ".join(p.name for p in entries[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no Meta export — looked for zips and folders with "
            f"messages/inbox/<thread>/message_1.json (has: {listing})")
    return found, "folder"


def _reader(container: dict):
    """(read, close) for a container's members."""
    if container["kind"] == "zip":
        zf = zipfile.ZipFile(container["path"])
        return zf.read, zf.close
    root = container["path"]
    return (lambda n: (root / n).read_bytes()), (lambda: None)


def _has_exports(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(
            _EXPORT_NAME.fullmatch(p.name) and (
                (p.is_file() and p.suffix.lower() == ".zip")
                or (p.is_dir() and _is_layout_folder(p)))
            for p in folder.iterdir())
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.meta_messages] exports_dir, else ~/Downloads when it holds
    an export: the download link Meta emails opens in the browser, and the
    browser saves the zip there."""
    configured = cfg.connector_settings(name).get("exports_dir")
    if configured:
        p = cfg.resolve(configured)
        if not p.is_dir():
            raise DomainError(f"[connectors.meta_messages] exports_dir {p} is not a folder")
        return p
    downloads = Path.home() / "Downloads"
    if _has_exports(downloads):
        return downloads
    raise DomainError(
        f"no Meta export found in {downloads} — request one in JSON format "
        f"(docs/meta_messages.md) and pass the .zip, or set "
        f"[connectors.meta_messages] exports_dir")


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> tuple[list[str], bool, str | None]:
    s = cfg.connector_settings(name)
    me = s.get("me")
    if me is None or me == "":
        names: list[str] = []
    elif isinstance(me, str):
        names = [me.strip()]
    elif isinstance(me, list) and all(isinstance(x, str) for x in me):
        names = [x.strip() for x in me if x.strip()]
    else:
        raise DomainError(f"[connectors.meta_messages] me must be your name as your "
                          f"messages show it, or a list of names — got {me!r}")
    include = s.get("include_requests", False)
    if not isinstance(include, bool):
        raise DomainError(f"[connectors.meta_messages] include_requests must be true "
                          f"or false — got {include!r}")
    platform = s.get("platform")
    if platform is not None:
        key = str(platform).strip().lower()
        if key not in _PLATFORM_SETTINGS:
            raise DomainError(f"[connectors.meta_messages] platform must be "
                              f"\"instagram\" or \"messenger\" — got {platform!r}")
        platform = _PLATFORM_SETTINGS[key]
    return names, include, platform


# ---------------------------------------------------------------- reading one export

def _profile(raw) -> tuple[str | None, list[str]]:
    """(platform, the owner's names) from a profile file, or (None, [])."""
    if not isinstance(raw, dict):
        return None, []
    users = raw.get("profile_user")
    if isinstance(users, list) and users and isinstance(users[0], dict):
        data = users[0].get("string_map_data") or {}
        names = [((data.get(k) or {}).get("value") or "").strip()
                 for k in ("Name", "Username")]
        return "instagram", [n for n in names if n]
    for key in ("profile_v2", "profile"):
        prof = raw.get(key)
        if isinstance(prof, dict):
            full = ((prof.get("name") or {}).get("full_name") or "").strip() \
                if isinstance(prof.get("name"), dict) else ""
            return "messenger", [full] if full else []
    return None, []


def _read_export(c: dict, platform_setting: str | None, hasher) -> dict:
    """One container -> {threads (each with its platform), profiles
    {platform: the owner's names}, names, repaired}."""
    names = c["names"]
    msg_files, html = [], 0
    for n in names:
        m = _MSG_FILE.fullmatch(n)
        if m and m["ext"].lower() == "json":
            msg_files.append((n, m))
        elif m:
            html += 1
    profiles = [n for n in names if PurePosixPath(n).name.lower() in _PROFILE_FILES]
    if not msg_files:
        if html:
            raise DomainError(
                f"{c['label']} is a Meta export in HTML format — this reader needs "
                f"JSON: request the export again with Format: JSON (Accounts Center > "
                f"Your information and permissions > Download your information)")
        listing = ", ".join(names[:10]) or "(empty)"
        raise DomainError(
            f"{c['label']} holds no messages/inbox/<thread>/message_N.json "
            f"(members: {listing})")

    read, close = _reader(c)
    repaired = [0]
    try:
        profiles_found: dict[str, list[str]] = {}
        # The hash names members, not the container: a zip and the folder it
        # unzips to are the same export.
        for member in sorted(profiles):
            raw = read(member)
            hasher.update(f"{member}\0".encode() + hashlib.sha256(raw).digest())
            plat, found = _profile(_repaired(_load(raw, f"{c['label']}: {member}"),
                                             repaired))
            if plat and not profiles_found.get(plat):
                profiles_found[plat] = found
        threads: dict[tuple, dict] = {}
        for member, m in sorted(msg_files, key=lambda x: x[0]):
            raw = read(member)
            hasher.update(f"{member}\0".encode() + hashlib.sha256(raw).digest())
            data = _repaired(_load(raw, f"{c['label']}: {member}"), repaired)
            area = m["area"].lower() if m["area"] else None
            key = (area, m["root"], m["box"].lower(), m["thread"])
            t = threads.setdefault(key, {
                "area": area, "root": m["root"], "box": m["box"].lower(),
                "folder": m["thread"], "files": []})
            t["files"].append((int(m["n"]), data, member))
    finally:
        close()
    hasher.update(("\0".join(names)).encode())

    # Current exports say which app in the folder name; an older one says it
    # with its profile file, or the name Meta gave the zip.
    fallback = None
    if len(profiles_found) == 1:
        fallback = next(iter(profiles_found))
    elif re.match(r"instagram", c["label"], re.I):
        fallback = "instagram"
    elif re.match(r"(?:facebook|messenger)", c["label"], re.I):
        fallback = "messenger"
    elif platform_setting:
        fallback = platform_setting
    for t in threads.values():
        t["platform"] = _AREAS[t["area"]] if t["area"] else fallback
        if t["platform"] is None:
            raise DomainError(
                f"{c['label']}: cannot tell whether this is an Instagram or a Facebook "
                f"export (no your_instagram_activity/ or your_facebook_activity/ "
                f"folder, no profile file) — set [connectors.meta_messages] platform "
                f"= \"instagram\" or \"messenger\"")
    return {"threads": list(threads.values()), "profiles": profiles_found,
            "names": set(names), "repaired": repaired[0]}


# ---------------------------------------------------------------- what a message is

def _subject_rest(content: str, sender: str) -> tuple[bool, str]:
    """(whether the text starts with its sender or 'You', what follows)."""
    for subject in (sender, "You"):
        if subject and content.startswith(subject + " "):
            return True, content[len(subject) + 1:]
    return False, content


# Matched against what follows the sender's name (or 'You'): Meta writes its
# notices as sentences about the sender.
_SUBJECT_NOTICES = (
    ("reaction notice", re.compile(
        r"liked a message\.?|reacted .{1,24} to (?:your|a|.{1,80}'s) message\.?"
        r"|changed (?:the|their|your|his|her) reaction .{1,80}", re.I | re.S)),
    ("unsent message", re.compile(r"unsent a message\.?", re.I)),
    ("call notice", re.compile(
        r"(?:started|joined|ended) (?:a |the )?(?:video chat|audio call|video call"
        r"|call|group call|group video chat)\.?|called you\."
        r"|missed (?:a |your )?(?:video |audio |group )?(?:call|video chat)"
        r"(?: from .{1,80})?\.?", re.I)),
    ("group change", re.compile(
        r"(?:named|renamed) the group .{1,200}|changed the group name to .{1,200}"
        r"|removed the group name\.?|changed the group (?:photo|image)\.?"
        r"|removed the group (?:photo|image)\.?|added .{1,200} to the group\.?"
        r"|removed .{1,200} from the group\.?|left the group\.?|joined the group\.?"
        r"|created the group\.?|made .{1,80} an admin\.?|removed .{1,80} as an admin\.?"
        r"|(?:set|changed|cleared|removed) (?:the|your|their|his|her|.{1,80}'s)"
        r" nickname.{0,200}|changed the (?:chat )?theme.{0,200}"
        r"|(?:set|changed) the (?:quick reaction|emoji|chat emoji)(?: to .{1,24})?\.?"
        r"|(?:pinned|unpinned) a message\.?"
        r"|turned (?:on|off) (?:vanish mode|disappearing messages).{0,200}", re.I | re.S)),
)
# Whole-text notices with no sender in them.
_BARE_NOTICES = (
    ("reaction notice", re.compile(r"liked a message\.?|reacted .{1,24} to your message\.?",
                                   re.I | re.S)),
    ("unsent message", re.compile(r"(?:this )?message (?:was )?unsent\.?", re.I)),
    ("call notice", re.compile(
        r"(?:the )?(?:video chat|audio call|video call|call|group call) ended\.?"
        r"|missed (?:video |audio |group )?(?:call|video chat)\.?", re.I)),
    ("encryption notice", re.compile(r"messages and calls are secured with end-to-end"
                                     r" encryption.{0,200}", re.I | re.S)),
    ("connection notice", re.compile(
        r"you are now connected on messenger\.?|you can now message and call each"
        r" other.{0,200}|say hi to your new facebook friend.{0,200}"
        r"|you're now friends.{0,200}", re.I | re.S)),
)
# Meta's stand-in text beside (or instead of) an attachment.
_PLACEHOLDER = re.compile(
    r"sent an attachment\.?|sent (?:a|an|\d+) (?:photo|video|voice message|audio clip"
    r"|gif|sticker|link|file|location)s?\.?", re.I)
_MEDIA_KEYS = (("photos", "photo"), ("videos", "video"), ("audio_files", "audio"),
               ("files", "file"), ("gifs", "gif"))
# Bookkeeping a message may carry besides what it says.
_BOOKKEEPING = {"sender_name", "timestamp_ms", "content", "reactions", "type",
                "is_geoblocked_for_viewer", "is_unsent_image_by_messenger_kid_parent",
                "is_unsent", "is_taken_down", "bumped_message_metadata", "ip", "users",
                "share", "sticker", *(k for k, _ in _MEDIA_KEYS)}


_CALLED = re.compile(r"called (?P<who>.{1,120})\.")


def _notice(msg: dict, sender: str, content: str, people: set[str]) -> str | None:
    """The kind of notice a message is, or None for a message a person sent.
    `people` are the conversation's participants (casefolded): 'You called
    Rosa Treviño.' is a call only when Rosa is one of them — 'You called
    it.' is something a person wrote."""
    if "call_duration" in msg or msg.get("type") == "Call":
        return "call notice"
    if msg.get("type") in ("Subscribe", "Unsubscribe"):
        return "group change"
    if msg.get("is_unsent") is True:
        return "unsent message"
    if not content:
        return None
    text = content.strip()
    has_subject, rest = _subject_rest(text, sender)
    if has_subject:
        for kind, rx in _SUBJECT_NOTICES:
            if rx.fullmatch(rest):
                return kind
        called = _CALLED.fullmatch(rest)
        if called and _key(called["who"]) in people:
            return "call notice"
    for kind, rx in _BARE_NOTICES:
        if rx.fullmatch(text):
            return kind
    return None


def _attachments(msg: dict, root: str, names: set[str]) -> tuple[list[dict], str | None]:
    """([{kind, uri}], media_path): what the message carried, and the first
    file of it that is actually in the export."""
    out: list[dict] = []
    for key, kind in _MEDIA_KEYS:
        items = msg.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("uri"), str):
                    out.append({"kind": kind, "uri": item["uri"]})
    sticker = msg.get("sticker")
    if isinstance(sticker, dict) and isinstance(sticker.get("uri"), str):
        out.append({"kind": "sticker", "uri": sticker["uri"]})
    media_path = next((a["uri"] for a in out if root + a["uri"] in names), None)
    return out, media_path


def _share(msg: dict) -> dict | None:
    share = msg.get("share")
    if not isinstance(share, dict):
        return None
    out = {}
    for src, dst in (("link", "link"), ("share_text", "text"),
                     ("original_content_owner", "owner")):
        v = share.get(src)
        if isinstance(v, str) and v.strip():
            out[dst] = v.strip()
    return out or None


# ---------------------------------------------------------------- who is you

def _key(name_: str) -> str:
    return " ".join(name_.split()).casefold()


def _participants(t: dict) -> list[str]:
    """The conversation's participants as shown, in the export's order, from
    the newest file that lists them."""
    for _, data, _ in sorted(t["files"], key=lambda f: f[0]):
        parts = data.get("participants") if isinstance(data, dict) else None
        if isinstance(parts, list):
            out = []
            for p in parts:
                n = p.get("name") if isinstance(p, dict) else p
                if isinstance(n, str) and n.strip() and _key(n) not in {_key(x) for x in out}:
                    out.append(" ".join(n.split()))
            if out:
                return out
    return []


def _owner(platform: str, threads: list[dict], me: list[str],
           profile_names: list[str]) -> dict:
    """{'keys', 'label', 'source', 'warning'} for one platform."""
    label = PLATFORMS[platform]["label"]
    seen: dict[str, str] = {}
    for t in threads:
        for n in t["people"]:
            seen.setdefault(_key(n), n)
    if me:
        keys = {_key(n) for n in me}
        hit = next((seen[k] for k in sorted(keys) if k in seen), None)
        warning = None if hit else (
            f"no {label} participant is [connectors.meta_messages] me "
            f"({', '.join(me)}) — check it matches your name as your messages show it")
        return {"keys": keys, "label": hit or me[0],
                "source": "[connectors.meta_messages] me", "warning": warning}
    hits = [seen[_key(n)] for n in profile_names if _key(n) in seen]
    if hits:
        return {"keys": {_key(hits[0])}, "label": hits[0],
                "source": "the export's own profile", "warning": None}
    note = (f" (the export's profile names {', '.join(profile_names)}, who is in "
            f"none of them)") if profile_names else ""
    inbox = [t for t in threads if t["people"]]
    if len(inbox) >= 2:
        common = set.intersection(*({_key(n) for n in t["people"]} for t in inbox))
        if len(common) == 1:
            k = next(iter(common))
            return {"keys": {k}, "label": seen[k],
                    "source": (f"inferred: the one participant in every conversation"
                               f"{note}; set [connectors.meta_messages] me to be sure"),
                    "warning": None}
    return {"keys": set(), "label": None, "source": None,
            "warning": (f"nothing in the {label} export says which participant is you"
                        f"{note} — direction stays unknown, and changing it after "
                        f"--apply is a conflict, not an update; set "
                        f"[connectors.meta_messages] me = \"<your name as your "
                        f"messages show it>\" first")}


# ---------------------------------------------------------------- scan

def _handle_value(platform: str, shown: str, folder: str) -> str:
    if _key(shown) in _PLACEHOLDER_NAMES:
        return f"{platform}:{shown} ({folder})"
    return f"{platform}:{shown}"


def _external_id(platform: str, folder: str, ms: int, sender: str, text: str,
                 ordinal: int) -> str:
    raw = "\x1f".join((platform, folder, str(ms), sender, text, str(ordinal)))
    return "meta:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ms(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _thread_messages(t: dict) -> list[dict]:
    """The conversation's messages oldest first: files message_N..1, each
    newest-first, so each is reversed; then a stable sort by time."""
    out = []
    for _, data, member in sorted(t["files"], key=lambda f: -f[0]):
        msgs = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(msgs, list):
            t["unreadable"].append(member)
            continue
        out.extend(m for m in reversed(msgs))
    return sorted(out, key=lambda m: _ms(m.get("timestamp_ms") if isinstance(m, dict)
                                         else None) or 0)


def _title(t: dict) -> str:
    for _, data, _ in sorted(t["files"], key=lambda f: f[0]):
        title = data.get("title") if isinstance(data, dict) else None
        if isinstance(title, str) and title.strip():
            return " ".join(title.split())
    return ", ".join(t["people"]) or t["folder"]


def _read_message(msg, t: dict) -> tuple[str, object]:
    """One message of conversation `t`: ('skip', reason), ('unmapped', what)
    or ('message', what scan() makes a record of)."""
    if not isinstance(msg, dict):
        return "skip", "message that is not an object"
    ms = _ms(msg.get("timestamp_ms"))
    if ms is None:
        return "skip", "message with no usable timestamp"
    sender = msg.get("sender_name")
    sender = " ".join(sender.split()) if isinstance(sender, str) else ""
    content = msg.get("content")
    content = content.strip() if isinstance(content, str) else ""
    kind = _notice(msg, sender, content, t["keys"])
    if kind is not None:
        return "skip", f"system line: {kind}"
    if not sender:
        return "skip", "message with no sender"
    attached, media_path = _attachments(msg, t["root"], t["names"])
    share = _share(msg)
    body, missing = content, False
    has_subject, rest = _subject_rest(content, sender)
    if content and ((has_subject and _PLACEHOLDER.fullmatch(rest))
                    or _PLACEHOLDER.fullmatch(content)):
        body = ""
        missing = not (attached or share)
    if not body and not attached and not share and not missing:
        extra = sorted(k for k in msg if k not in _BOOKKEEPING
                       and msg[k] not in (None, "", [], {}))
        if extra:
            return "unmapped", f"message with no text, only: {', '.join(extra)}"
        return "skip", "empty message"
    reactions, reacted = [], 0
    for r in msg.get("reactions") or []:
        if isinstance(r, dict) and isinstance(r.get("reaction"), str):
            actor = r.get("actor")
            reactions.append((" ".join(actor.split()) if isinstance(actor, str)
                              and actor.strip() else None, r["reaction"]))
            when = r.get("timestamp")          # Instagram dates reactions; Messenger does not
            if isinstance(when, int) and not isinstance(when, bool):
                reacted = max(reacted, when)
    # What the id hashes as the message's text: its words, and what it
    # carried, so two photos sent in one millisecond stay two messages.
    key_text = "\x1e".join([content, share.get("link", "") if share else "",
                            *(PurePosixPath(a["uri"]).name for a in attached)])
    return "message", {"ms": ms, "sender": sender, "body": body or None,
                       "attached": attached, "media_path": media_path, "share": share,
                       "missing": missing, "reactions": reactions, "reacted": reacted,
                       "key_text": key_text}


def scan(target: Path, cfg: Config) -> base.Scan:
    me, include_requests, platform_setting = _settings(cfg)
    hasher = hashlib.sha256()
    try:
        containers, target_kind = _containers(target)
        containers.sort(key=lambda c: c["label"])
        exports = [_read_export(c, platform_setting, hasher) for c in containers]
    except OSError as e:
        raise DomainError(f"cannot read {target.name}: {e}") from e

    by_platform: dict[str, list[dict]] = {}
    profile_names: dict[str, list[str]] = {}
    for c, ex in zip(containers, exports):
        for t in ex["threads"]:
            t.update(label=c["label"], names=ex["names"], unreadable=[])
            t["people"] = _participants(t)
            t["keys"] = {_key(n) for n in t["people"]}
            t["title"] = _title(t)
            by_platform.setdefault(t["platform"], []).append(t)
        for plat, found in sorted(ex["profiles"].items()):
            if found and not profile_names.get(plat):
                profile_names[plat] = found

    handles: dict[tuple[str, str], dict] = {}

    def seen(value: str, shown: str) -> dict:
        k = ("app_account", value.casefold())
        if k not in handles:
            handles[k] = {"kind": "app_account", "value": value, "display_name": shown}
        return {"kind": "app_account", "value": handles[k]["value"]}

    records: list[dict] = []
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    media = {"present": 0, "missing": 0}
    requests = {"threads": 0, "messages": 0}
    owners: dict[str, dict] = {}
    warnings: list[str] = []
    thread_facts: dict[tuple[str, str], dict] = {}

    def skip(reason: str, n: int = 1) -> None:
        skipped[reason] = skipped.get(reason, 0) + n

    for platform in sorted(by_platform):
        threads = sorted(by_platform[platform],
                         key=lambda t: (t["label"], t["box"], t["folder"]))
        stream = PLATFORMS[platform]["stream"]
        owner = _owner(platform, [t for t in threads if t["box"] not in _REQUEST_BOXES],
                       me, profile_names.get(platform, []))
        owners[platform] = owner
        if owner["warning"]:
            warnings.append(owner["warning"])
        owner_part = seen(_handle_value(platform, owner["label"], ""), owner["label"]) \
            if owner["label"] else None

        for t in threads:
            msgs = _thread_messages(t)
            for member in t["unreadable"]:
                what = "message file without a messages list"
                unmapped[what] = unmapped.get(what, 0) + 1
            if t["box"] in _REQUEST_BOXES and not include_requests:
                requests["threads"] += 1
                requests["messages"] += len(msgs)
                continue
            people = t["people"]
            group = len(people) > 2
            parts = {_key(n): seen(_handle_value(platform, n, t["folder"]), n)
                     for n in people}
            ordinals: dict[tuple, int] = {}
            kept: dict[str, str] = {}             # external id -> journal date
            for msg in msgs:
                what, got = _read_message(msg, t)
                if what == "skip":
                    skip(got)
                    continue
                if what == "unmapped":
                    unmapped[got] = unmapped.get(got, 0) + 1
                    continue
                sk = _key(got["sender"])
                sender_part = parts.get(sk) or seen(
                    _handle_value(platform, got["sender"], t["folder"]), got["sender"])
                mine = sk in owner["keys"]
                recipients = [p for k, p in parts.items() if k != sk]
                if owner_part and not mine and owner_part not in recipients:
                    recipients.append(owner_part)
                participants = [{**sender_part, "role": "sender"}] + \
                    [{**r, "role": "recipient"} for r in recipients]

                tup = (got["ms"], sk, got["key_text"])
                ordinal = ordinals.get(tup, 0)
                ordinals[tup] = ordinal + 1

                detail: dict = {}
                if group:
                    detail["group"] = True
                if t["box"] != "inbox":
                    detail["folder"] = t["box"]
                if got["share"]:
                    detail["share"] = got["share"]
                if got["attached"]:
                    detail["media"] = got["attached"]
                    media["present" if got["media_path"] else "missing"] += 1
                if got["missing"]:
                    detail["attachment_missing"] = True
                if got["reactions"]:
                    detail["reactions"] = sorted(
                        ({"by": seen(_handle_value(platform, actor, t["folder"]),
                                     actor)["value"] if actor else None,
                          "reaction": reaction} for actor, reaction in got["reactions"]),
                        key=lambda r: (r["by"] or "", r["reaction"]))

                started = base.iso_utc(got["ms"] / 1000)
                # A reaction added since the last export is newer than the
                # message: dated (Instagram), it updates instead of conflicting.
                modified = max(started, base.iso_utc(got["reacted"]) or started)
                records.append({
                    "stream": stream,
                    "external_id": _external_id(platform, t["folder"], got["ms"],
                                                got["sender"], got["key_text"], ordinal),
                    "journal_date": base.journal_date_for_iso(started, cfg),
                    "started_at": started,
                    "ended_at": None,
                    "utc_offset_sec": None,
                    "direction": ("out" if mine else "in") if owner["keys"] else None,
                    "title": t["title"],
                    "body": got["body"],
                    "value": None,
                    "unit": None,
                    "value_json": canonical_json(detail),
                    "media_path": got["media_path"],
                    "origin_app": platform,
                    "last_modified_at": modified,
                    "participants": participants,
                })
                kept[records[-1]["external_id"]] = records[-1]["journal_date"]

            # One line per conversation, however many copies or parts of the
            # export it came from.
            f = thread_facts.setdefault((platform, t["folder"]), {
                "platform": platform, "thread": t["title"], "folder": t["folder"],
                "box": t["box"], "group": group, "ids": {}})
            f["ids"].update(kept)

    if requests["threads"]:
        skip("message in a message request (include_requests = true imports them)",
             requests["messages"])

    for f in thread_facts.values():
        dates = sorted(f.pop("ids").values())
        f.update(messages=len(dates), first=dates[0] if dates else None,
                 last=dates[-1] if dates else None)
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    first_platform = next((p for p in sorted(owners) if owners[p]["label"]), None)
    return base.empty_scan(
        name,
        account=(_handle_value(first_platform, owners[first_platform]["label"], "")
                 if first_platform else None),
        source_file=target.name,
        source_hash=hasher.hexdigest(),
        records=records,
        handles=[handles[k] for k in sorted(handles)],
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        # No present_ids: an export can be date-limited, so what it lacks is
        # not evidence of a deletion. The archive remembers.
        facts={"exports": [{"export": c["label"],
                            "platforms": sorted({t["platform"] for t in ex["threads"]}),
                            "threads": len(ex["threads"]),
                            "files": sum(len(t["files"]) for t in ex["threads"])}
                           for c, ex in zip(containers, exports)],
               "platforms": {p: {"conversations": len({t["folder"] for t in ts}),
                                 "files": sum(len(t["files"]) for t in ts)}
                             for p, ts in sorted(by_platform.items())},
               "threads": sorted(thread_facts.values(),
                                 key=lambda f: (f["platform"], -f["messages"],
                                                f["thread"], f["folder"])),
               "you": {p: {"name": o["label"], "from": o["source"]}
                       for p, o in sorted(owners.items())},
               "requests": requests, "include_requests": include_requests,
               "repaired": sum(ex["repaired"] for ex in exports),
               "media": media, "warnings": warnings},
        target_kind=target_kind)


# ---------------------------------------------------------------- report

_SHOWN_THREADS = 10


def _sample(rec: dict) -> str:
    who = {"out": "you", "in": "them"}.get(rec["direction"], "?")
    body = rec["body"]
    if body is None:
        detail = json.loads(rec["value_json"]) if rec["value_json"] else {}
        if "share" in detail:
            body = "[shared] " + (detail["share"].get("link") or "")
        elif "media" in detail:
            body = f"[{detail['media'][0]['kind']}]"
        else:
            body = "[attachment]"
    body = " ".join(body.split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    exports = facts["exports"]
    lines = [f"meta_messages: {scan['source_file']} ("
             + "; ".join(f"{PLATFORMS[p]['label']}: {v['conversations']} "
                         f"conversation(s), {v['files']} message file(s)"
                         for p, v in facts["platforms"].items()) + ")"]
    if len(exports) > 1:
        lines.append("  exports: " + ", ".join(e["export"] for e in exports))
    for platform, you in facts["you"].items():
        if you["name"]:
            lines.append(f"  you on {PLATFORMS[platform]['label']}: {you['name']} "
                         f"({you['from']})")
    threads = facts["threads"]
    for f in threads[:_SHOWN_THREADS]:
        span = f" {f['first']}..{f['last']}" if f["first"] else ""
        where = "" if f["box"] == "inbox" else f" | {f['box']}"
        lines.append(f"  conversation: {f['thread']} — "
                     f"{'group' if f['group'] else 'one-to-one'}, {f['messages']} "
                     f"message(s){span}{where}")
    if len(threads) > _SHOWN_THREADS:
        lines.append(f"  … and {len(threads) - _SHOWN_THREADS} more conversation(s)")
    req = facts["requests"]
    if req["threads"]:
        lines.append(f"  message requests: {req['threads']} conversation(s), "
                     f"{req['messages']} message(s) left out — set "
                     f"[connectors.meta_messages] include_requests = true to import them")
    if facts["repaired"]:
        lines.append(f"  text: {facts['repaired']} string(s) repaired from Meta's "
                     f"Latin-1 escaping of UTF-8")
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

    you = {v for v in (scan["account"],) if v}
    mine = {f"{p}:{y['name']}".casefold() for p, y in facts["you"].items() if y["name"]}
    people = [h for h in scan["handles"]
              if h["value"].casefold() not in mine and h["value"] not in you]
    if people:
        lines.append(f"  handles: {len(people)} by the name the export shows (Meta "
                     f"exports carry no account ids) — review with 'lifecrm handles "
                     f"list --suggest'")
    m = facts["media"]
    if any(m.values()):
        lines.append(f"  media: {m['present']} message(s) with the file in the export "
                     f"(path kept, never the bytes), {m['missing']} named but not in "
                     f"the export")
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
