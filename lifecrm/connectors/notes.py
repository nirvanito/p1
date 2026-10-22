"""Notes connector: a folder of markdown/text notes, a Google Keep Takeout, or
a Mac's Apple Notes store.

Notes are where plans, lists and half-formed thoughts live between the voice
journal and the calendar — the packing list from a trip, the gift ideas, the
outline of a deck. None of the apps that hold them has an API worth the name,
but each has a file:

  a folder        any directory of .md / .markdown / .txt files: an Obsidian
                  vault, Apple Notes exported by a third-party exporter, Samsung
                  Notes saved one by one as text files into OneDrive
  Google Keep     a Takeout export (takeout.google.com, Keep only): a zip, or
                  the folder it unpacks to, holding Takeout/Keep/<note>.json
  Apple Notes     ~/Library/Group Containers/group.com.apple.notes/
                  NoteStore.sqlite on a Mac (iCloud syncs the iPhone's notes
                  into it). Copied through SQLite's backup API, never read in
                  place; reading it needs Full Disk Access

One observation per note, stream `note`, keyed on the source's own identity:

  folder   file:<folder name>/<path inside it>    (POSIX separators)
  Keep     keep:<json file name without .json>
  Apple    apple:<ZIDENTIFIER>

The prefix keeps three sources apart in one stream; the folder's own name in
the folder id keeps two vaults that both hold an Inbox.md apart, while the
same vault synced to a second machine keeps its ids. Renaming a note in a
folder or in Keep renames its file, so it reads as one note deleted and one
added — the price of using the only identity those sources have.

The archive mirrors each source (decision #2, extended to notes): a note
deleted at the source leaves on the next import — a file gone or moved into
.trash/, a Keep note trashed or gone, an Apple note marked for deletion, in
Recently Deleted, or gone. "Gone" is judged against everything the source
still holds, including notes this connector cannot read today (a locked
Apple note, a file that is not UTF-8, an iCloud placeholder), and removal is
scoped to the account the scan names: the folder's full path, the Keep
account's address, the Apple Notes store's own Core Data UUID. A Keep export
does not say whose it is, so its account is read from config or from the one
address present on every shared note; without either, nothing is removed.
The shared write layer refuses a removal large enough to look like an
incomplete source, and snapshots before any removal.

Dates: a note's started_at is when it was written — front matter's created
date, Keep's createdTimestampUsec, Apple's creation date — falling back to the
last edit. last_modified_at is the last edit (a file's mtime), so an edited
note updates through newer-wins. Journal dates use the home timezone; a note
written abroad lands on the home-timezone day, as for every export that does
not record where you were.

No LLM anywhere in here, and no third-party code: the protobuf inside Apple
Notes is read by `_protobuf.py`, written from the wire-format specification.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _protobuf, base

name = "notes"
streams = ("note",)
# No stale_after_days: notes come in bursts, and a note's started_at is when
# it was written, so a month with no new note is ordinary, not a dead export.

APPLE_EPOCH = 978307200            # 2001-01-01T00:00:00Z: Core Data's zero
NOTE_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".txt": "text"}
OBJECT_REPLACEMENT = "\N{OBJECT REPLACEMENT CHARACTER}"   # an attachment's place in the text
SQLITE_MAGIC = b"SQLite format 3\x00"
TRASH_FOLDER_TYPE = 1              # ZFOLDERTYPE of Apple Notes' Recently Deleted
TRASH_FOLDER_TITLE = "Recently Deleted"

# Front-matter keys that say when a note was written, most specific first.
_CREATED_KEYS = ("created", "created_at", "creation_date", "date_created", "date")
_KEEP_KEYS = ("textContent", "listContent", "title", "userEditedTimestampUsec")
_TAKEOUT_PART = re.compile(r"^(takeout-.+)-(\d{3})\.zip$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_FM_KEY = re.compile(r"^([A-Za-z_][\w \-]*?)\s*:(?:\s+(.*))?$")


def _count(d: dict, key: str) -> None:
    d[key] = d.get(key, 0) + 1


# ---------------------------------------------------------------- time

def _epoch_iso(seconds: int) -> str | None:
    # A corrupt value can be past what a datetime holds; that is a note with
    # no usable timestamp, counted as such, not a traceback.
    try:
        return base.iso_utc(seconds)
    except (OverflowError, ValueError, OSError):
        return None


def _usec_iso(value) -> str | None:
    """Keep's microseconds since the epoch -> 'YYYY-MM-DDTHH:MM:SSZ'.
    base.iso_utc takes seconds; a raw microsecond value would be the year
    55,000. Integer division, so no float rounding lands on the next second."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return _epoch_iso(value // 1_000_000)


def _core_data_iso(value) -> str | None:
    """Core Data timestamp (seconds since 2001, a REAL) -> ISO UTC, with the
    fraction dropped."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(float(value) // 1)
    except (TypeError, ValueError, OverflowError):     # NaN, inf, text
        return None
    return _epoch_iso(APPLE_EPOCH + seconds)


def _local_iso(value, cfg: Config) -> str | None:
    """A front-matter date as ISO UTC, or None if it is not one.

    A bare date means that day: it is read as local noon, because local
    midnight falls before the rollover hour and would file the note on the
    previous journal day. A time with no zone is local time — that is what a
    person typing `created: 2026-10-01 14:30` means.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().strip("\"'")
    tz = ZoneInfo(cfg.timezone)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            d = date.fromisoformat(s)
        except ValueError:
            return None
        dt = datetime(d.year, d.month, d.day, 12, tzinfo=tz)
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):        # year 1 or 9999 shifted past the edge
        return None


# ---------------------------------------------------------------- access

def _no_access(path: Path, err: Exception) -> DomainError:
    """One line naming the fix. On a Mac the usual cause is privacy
    protection (TCC): Apple Notes' container, and any folder an app has not
    been granted, read as 'permission denied' or 'unable to open'."""
    return DomainError(
        f"cannot read {path}: {err} — on a Mac, give the terminal app you run "
        f"lifecrm from Full Disk Access (System Settings > Privacy & Security > "
        f"Full Disk Access), then re-run")


def _raise(err: OSError) -> None:
    # os.walk swallows a directory it cannot list unless told otherwise, and
    # a silently skipped directory would read as every note in it deleted.
    raise err


# ---------------------------------------------------------------- folder

def _decode(raw: bytes) -> str | None:
    """UTF-8 with or without a BOM, or UTF-16 when it carries a BOM (Windows
    Notepad's 'Unicode'). Anything else is not guessed at."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _scalar(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if " #" in s:                      # a YAML comment after the value
        s = s.split(" #", 1)[0].rstrip()
    return s


def _value(raw: str | None):
    s = (raw or "").strip()
    if s in ("|", "|-", ">", ">-"):
        return ""                      # a block scalar: its lines follow
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(x) for x in s[1:-1].split(",") if x.strip()]
    return _scalar(s)


def _front_matter(text: str) -> tuple[dict, str]:
    """(front matter, the text after it).

    Front matter is the YAML block between a first line of '---' and the next
    '---' (or '...'). Only the shapes notes actually use are understood —
    `key: value`, inline `[a, b]` lists, `- item` block lists. Anything deeper
    (nested maps, multi-line strings) is kept as its raw text under its key
    rather than parsed wrongly; PyYAML is not a dependency for one header.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines))
                if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return {}, text                # an opening rule, not front matter
    meta: dict = {}
    key = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None and (meta.get(key) == "" or isinstance(meta.get(key), list)):
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_scalar(item.group(1)))
            continue
        m = _FM_KEY.match(line)
        if m and not line[:1].isspace():
            key = m.group(1).strip()
            meta[key] = _value(m.group(2))
            continue
        if key is None:
            continue
        prev = meta.get(key)
        if isinstance(prev, list) and prev:
            prev[-1] = f"{prev[-1]}\n{line.strip()}"
        else:
            meta[key] = f"{prev}\n{line.strip()}" if prev else line.strip()
    return meta, "\n".join(lines[end + 1:])


def _fm_get(meta: dict, *keys: str):
    norm = {re.sub(r"[\s\-]+", "_", k.strip().lower()): v for k, v in meta.items()}
    for k in keys:
        v = norm.get(k)
        if v not in (None, "", []):
            return v
    return None


def _title(meta: dict, body: str, stem: str) -> str:
    """Front matter's title, else the first line when it is a heading, else
    the file name. Only the first line: a '## Packing' halfway down a note
    called 'Bled trip' is a section, not the note's name."""
    t = _fm_get(meta, "title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    first_line = next((ln for ln in body.split("\n") if ln.strip()), "")
    m = _HEADING.match(first_line.strip())
    if m and m.group(1).strip():
        return m.group(1).strip()
    return stem


def _file_id(vault: str, rel: str) -> str:
    return f"file:{vault}/{rel}"


def _read_folder(folder: Path, cfg: Config) -> dict:
    root = folder.resolve()
    vault = root.name or str(root)
    origin = "obsidian" if (root / ".obsidian").is_dir() else "notes_folder"
    records: list[dict] = []
    present: set[str] = set()
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    other: dict[str, int] = {}
    digest = hashlib.sha256()
    seen = 0

    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        # .obsidian/ is settings, .trash/ is Obsidian's deleted notes, .git
        # is history: none of it is a note the vault still holds.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        here = Path(dirpath)
        for fname in sorted(filenames):
            path = here / fname
            if platforms.is_icloud_placeholder(path):
                real = platforms.icloud_placeholder_target(path)
                if Path(real).suffix.lower() in NOTE_SUFFIXES:
                    seen += 1
                    present.add(_file_id(vault, (here / real).relative_to(root).as_posix()))
                    _count(skipped, "note not downloaded from iCloud "
                                    "(kept — it is still in the folder)")
                continue
            if fname.startswith("."):
                continue
            fmt = NOTE_SUFFIXES.get(path.suffix.lower())
            if fmt is None:
                _count(other, path.suffix.lower() or "(no extension)")
                continue
            seen += 1
            rel = path.relative_to(root).as_posix()
            ext_id = _file_id(vault, rel)
            present.add(ext_id)            # in the folder, whether readable or not
            try:
                raw = path.read_bytes()
                mtime = path.stat().st_mtime
            except OSError as e:
                _count(skipped, f"note file that could not be read "
                                f"({type(e).__name__})")
                continue
            digest.update(rel.encode("utf-8") + b"\0" + raw + b"\0")
            text = _decode(raw)
            if text is None:
                _count(skipped, "note file that is not UTF-8 text")
                continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            meta, rest = _front_matter(text) if fmt == "markdown" else ({}, text)
            body = rest.strip()
            if not body:
                _count(skipped, "empty note")
                continue
            modified = _epoch_iso(mtime)
            created_raw = _fm_get(meta, *_CREATED_KEYS)
            created = _local_iso(created_raw, cfg) if created_raw is not None else None
            if created_raw is not None and created is None:
                _count(unmapped, "front-matter date not understood "
                                 "(dated by the file's modified time)")
            started = created or modified
            if started is None:
                _count(skipped, "note with no usable timestamp")
                continue
            detail = {"source": "folder", "path": rel, "format": fmt}
            if meta:
                detail["front_matter"] = meta
            records.append(_record(ext_id, started, cfg,
                                   title=_title(meta, body, path.stem), body=body,
                                   detail=detail, origin=origin, modified=modified))

    if not seen:
        listing = ", ".join(sorted(p.name for p in root.iterdir())[:10]) or "(empty)"
        raise DomainError(f"{folder.name} holds no .md or .txt notes and is not a "
                          f"Google Keep Takeout (has: {listing})")
    for ext, n in sorted(other.items()):
        unmapped[f"{ext} file (not a note)"] = n
    return {"kind": origin, "records": records, "handles": set(), "account": str(root),
            "account_from": "the folder's full path", "skipped": skipped,
            "unmapped": unmapped, "present": present, "seen": seen,
            "source_file": folder.name, "source_hash": digest.hexdigest(),
            "read_from": str(root)}


def _record(ext_id: str, started: str, cfg: Config, *, title, body, detail,
            origin, modified, participants=()) -> dict:
    return {"stream": "note", "external_id": ext_id,
            "journal_date": base.journal_date_for_iso(started, cfg),
            "started_at": started, "ended_at": None, "utc_offset_sec": None,
            "direction": None, "title": title or None, "body": body or None,
            "value": None, "unit": None, "value_json": canonical_json(detail),
            "media_path": None, "origin_app": origin,
            "last_modified_at": modified, "participants": list(participants)}


# ---------------------------------------------------------------- Google Keep

def _is_keep_member(member: str) -> bool:
    p = PurePosixPath(member)
    return p.parent.name.lower() == "keep" and p.suffix.lower() == ".json"


def _keep_dir(target: Path) -> Path | None:
    """The Keep folder of an unpacked Takeout: the folder itself, its Keep/,
    or its Takeout/Keep/."""
    for cand in (target, target / "Keep", target / "Takeout" / "Keep"):
        if cand.is_dir() and cand.name.lower() == "keep" and any(cand.glob("*.json")):
            return cand
    return None


def _other_keep_parts(target: Path) -> list[str]:
    """Sibling parts of a split Takeout that also hold Keep notes. A large
    export is cut into takeout-<stamp>-001.zip, -002.zip, ...; mirroring from
    one part would read the notes in the others as deleted."""
    m = _TAKEOUT_PART.match(target.name)
    if not m:
        return []
    out = []
    for sib in sorted(target.parent.glob("*.zip")):
        sm = _TAKEOUT_PART.match(sib.name)
        if sib.name == target.name or not sm or sm.group(1) != m.group(1):
            continue
        try:
            with zipfile.ZipFile(sib) as zf:
                if any(_is_keep_member(n) for n in zf.namelist()):
                    out.append(sib.name)
        except (zipfile.BadZipFile, OSError):
            continue
    return out


def _keep_files(target: Path, keep: Path | None) -> list[tuple[str, bytes]]:
    if keep is not None:
        return [(p.name, p.read_bytes()) for p in sorted(keep.glob("*.json"))]
    with zipfile.ZipFile(target) as zf:
        members = sorted(n for n in zf.namelist() if _is_keep_member(n))
        if not members:
            listing = ", ".join(zf.namelist()[:10]) or "(empty)"
            raise DomainError(f"{target.name} holds no Takeout/Keep/*.json notes — is "
                              f"this a Google Keep Takeout? (members: {listing})")
        return [(PurePosixPath(n).name, zf.read(n)) for n in members]


def _keep_body(data: dict) -> tuple[str, dict | None]:
    """The note's text: textContent, or a checklist as '- [x] item' lines —
    the markdown task-list form, so a checked item still reads as checked."""
    parts = []
    text = data.get("textContent")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())
    checklist = None
    items = data.get("listContent")
    if isinstance(items, list) and items:
        lines, checked = [], 0
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("text"), str):
                continue
            done = bool(it.get("isChecked"))
            checked += done
            lines.append(f"- [{'x' if done else ' '}] {it['text'].strip()}")
        if lines:
            parts.append("\n".join(lines))
            checklist = {"items": len(lines), "checked": checked}
    return "\n\n".join(parts), checklist


def _keep_account(shared: list[set], cfg: Config) -> tuple[str | None, str | None]:
    """(account, how it was found). Keep's export names no account. Config
    says it outright; otherwise the owner is the one address on every shared
    note — on their own notes as owner, on notes shared with them as a
    collaborator, so when exactly one address is common to all of them it
    can only be theirs. Anything less certain names no account, and
    mirroring is off: a wrong account could let one person's export remove
    another's."""
    configured = cfg.connector_settings(name).get("keep_account")
    if isinstance(configured, str) and "@" in configured:
        return configured.strip().lower(), "[connectors.notes] keep_account"
    if not shared:
        return None, None
    common = set.intersection(*shared)
    if len(common) == 1:
        return common.pop(), "the one address on every shared note"
    return None, None


def _read_keep(target: Path, keep: Path | None, cfg: Config) -> dict:
    files = _keep_files(target, keep)
    records: list[dict] = []
    present: set[str] = set()
    handles: set[tuple[str, str]] = set()
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    shared: list[set] = []
    digest = hashlib.sha256()
    seen = 0
    for fname, raw in files:
        digest.update(fname.encode("utf-8") + b"\0" + raw + b"\0")
        ext_id = f"keep:{PurePosixPath(fname).stem}"
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            seen += 1
            present.add(ext_id)        # a note we cannot parse is still in Keep
            _count(skipped, "Keep note file that is not valid JSON")
            continue
        if not isinstance(data, dict) or not any(k in data for k in _KEEP_KEYS):
            _count(unmapped, "JSON file in Keep/ that is not a note")
            continue
        seen += 1
        sharees = [(s["email"].strip().lower(), bool(s.get("isOwner")))
                   for s in data.get("sharees") or []
                   if isinstance(s, dict) and isinstance(s.get("email"), str)
                   and "@" in s["email"]]
        if sharees:
            # A trashed note still says whose export this is: counting it
            # keeps the account — and mirroring — steady when a shared note
            # is thrown away.
            shared.append({e for e, _ in sharees})
        if data.get("isTrashed"):
            _count(skipped, "note in Keep's trash (deleted — not kept)")
            continue
        present.add(ext_id)

        # Collaborators are the note's participants; the owner is the account.
        participants = [{"kind": "email", "value": email, "role": "participant"}
                        for email in sorted({e for e, own in sharees if not own})]
        body, checklist = _keep_body(data)
        title = (data.get("title") or "").strip() if isinstance(data.get("title"), str) else ""
        modified = _usec_iso(data.get("userEditedTimestampUsec"))
        started = _usec_iso(data.get("createdTimestampUsec")) or modified
        if started is None:
            _count(skipped, "Keep note with no usable timestamp")
            continue
        detail = {"source": "keep", "file": fname}
        labels = sorted({lb["name"] for lb in data.get("labels") or []
                         if isinstance(lb, dict) and isinstance(lb.get("name"), str)
                         and lb["name"].strip()})
        if labels:
            detail["labels"] = labels
        if data.get("isArchived"):
            detail["archived"] = True
        if data.get("isPinned"):
            detail["pinned"] = True
        if isinstance(data.get("color"), str) and data["color"] not in ("", "DEFAULT"):
            detail["color"] = data["color"]
        if checklist:
            detail["checklist"] = checklist
        links = [{k: v for k, v in (("url", a.get("url")), ("title", a.get("title")))
                  if isinstance(v, str) and v}
                 for a in data.get("annotations") or []
                 if isinstance(a, dict) and a.get("url")]
        if links:
            detail["links"] = links
        files_ = [{k: v for k, v in (("file", a.get("filePath")), ("mime", a.get("mimetype")))
                   if isinstance(v, str) and v}
                  for a in data.get("attachments") or [] if isinstance(a, dict)]
        files_ = [f for f in files_ if f]
        if files_:
            detail["attachments"] = files_
        if participants:
            detail["shared_with"] = len(participants)
        if not title and not body and not files_ and not links:
            _count(skipped, "empty note")
            continue
        records.append(_record(ext_id, started, cfg, title=title, body=body,
                               detail=detail, origin="google_keep",
                               modified=modified or started, participants=participants))
        handles.update((p["kind"], p["value"]) for p in participants)

    account, account_from = _keep_account(shared, cfg)
    result = {"kind": "keep", "records": records, "handles": handles,
              "account": account, "account_from": account_from,
              "skipped": skipped, "unmapped": unmapped, "present": present,
              "seen": seen, "source_file": target.name,
              "source_hash": base.file_hash(target) if keep is None else digest.hexdigest(),
              "read_from": str((keep or target).resolve())}
    if keep is None:
        parts = _other_keep_parts(target)
        if parts:
            result["present"] = None
            result["mirror_off"] = (
                f"this Takeout is split and {', '.join(parts)} also holds Keep notes "
                f"— extract every part into one folder and import that, so a note "
                f"in another part is not mistaken for a deleted one")
    if result["present"] is not None and account is None:
        result["account_hint"] = ("set [connectors.notes] keep_account in config.toml "
                                  "to mirror Keep deletions")
    return result


# ---------------------------------------------------------------- Apple Notes

def _snapshot(src: Path, tmp: Path) -> sqlite3.Connection:
    """A private, consistent copy of NoteStore.sqlite, opened query-only.

    The live store is held open by Notes, with a -wal and -shm beside it; the
    backup API reads it consistently from a read-only connection. A copied
    store that has a -wal but no -shm cannot be opened read-only, so both
    files are copied instead. The original is never opened for writing.
    """
    dest = tmp / "NoteStore.sqlite"
    wal = src.with_name(src.name + "-wal")
    shm = src.with_name(src.name + "-shm")
    conn = None
    try:
        if wal.exists() and not shm.exists():
            shutil.copyfile(src, dest)
            shutil.copyfile(wal, tmp / "NoteStore.sqlite-wal")
        else:
            source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
            try:
                # Closed even when the copy fails: on Windows an open handle
                # keeps the temporary folder from being removed.
                copy = sqlite3.connect(dest)
                try:
                    source.backup(copy)
                finally:
                    copy.close()
            finally:
                source.close()
        conn = sqlite3.connect(dest)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        return conn
    except PermissionError as e:
        if conn is not None:
            conn.close()
        raise _no_access(src, e) from e
    except sqlite3.DatabaseError as e:
        if conn is not None:
            conn.close()
        if "unable to open" in str(e) or "authorization denied" in str(e):
            raise _no_access(src, e) from e
        raise DomainError(f"cannot read {src.name} as a database: {e}") from e


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def note_text(blob) -> str:
    """The plain text of one ZICNOTEDATA.ZDATA: gzip, then the protobuf path
    NoteStoreProto.document (2) -> Document.note (3) -> Note.note_text (2).
    ValueError when the bytes are not that — a corrupt body is reported,
    never imported as an empty note."""
    raw = bytes(blob)
    if raw[:2] != b"\x1f\x8b":
        raise ValueError("note data is not gzip-compressed")
    try:
        raw = gzip.decompress(raw)
        text = _protobuf.first(raw, 2, 3, 2)
        return text.decode("utf-8") if text is not None else ""
    except (OSError, EOFError, zlib.error, UnicodeDecodeError) as e:
        raise ValueError(str(e)) from e


def _read_apple(conn: sqlite3.Connection, cfg: Config) -> dict:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"ZICCLOUDSYNCINGOBJECT", "ZICNOTEDATA"} - tables
    if missing:
        raise DomainError(f"not an Apple Notes store — no {', '.join(sorted(missing))} "
                          f"table (has: {', '.join(sorted(tables)[:10]) or 'nothing'})")
    cols = _columns(conn, "ZICCLOUDSYNCINGOBJECT")
    dcols = _columns(conn, "ZICNOTEDATA")

    def pick(*names):
        return next((c for c in names if c in cols), None)

    # Column names drift between macOS versions (a note's title is ZTITLE1
    # in current stores and ZTITLE in old ones; its creation date moved from
    # ZCREATIONDATE1 to ZCREATIONDATE3), so each is looked up, never assumed.
    def coalesce(*names):
        present_ = [f"o.{c}" for c in names if c in cols]
        return f"COALESCE({', '.join(present_)})" if len(present_) > 1 \
            else (present_[0] if present_ else "NULL")

    title_col, folder_title_col = pick("ZTITLE1", "ZTITLE"), pick("ZTITLE2", "ZTITLE")
    entities = {}
    if "Z_PRIMARYKEY" in tables:
        entities = {r[1]: r[0] for r in conn.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")}
    if "ZNOTE" in dcols:
        join = "LEFT JOIN ZICNOTEDATA d ON d.ZNOTE = o.Z_PK"
    elif "ZNOTEDATA" in cols:
        join = "LEFT JOIN ZICNOTEDATA d ON d.Z_PK = o.ZNOTEDATA"
    else:
        raise DomainError("Apple Notes store links no note data to notes (no "
                          "ZICNOTEDATA.ZNOTE or ZNOTEDATA column)")
    if "ICNote" in entities:
        where = f"o.Z_ENT = {int(entities['ICNote'])}"
    else:
        where = "d.Z_PK IS NOT NULL"   # no entity map: a note is what has note data

    def col(c, alias):
        return f"o.{c} AS {alias}" if c and c in cols else f"NULL AS {alias}"

    rows = conn.execute(
        "SELECT o.Z_PK AS pk, "
        + ", ".join([col("ZIDENTIFIER", "ident"), col(title_col, "title"),
                     f"{coalesce('ZCREATIONDATE3', 'ZCREATIONDATE1', 'ZCREATIONDATE')} AS created",
                     f"{coalesce('ZMODIFICATIONDATE1', 'ZMODIFICATIONDATE')} AS modified",
                     col("ZMARKEDFORDELETION", "marked"),
                     col("ZISPASSWORDPROTECTED", "locked"), col("ZFOLDER", "folder"),
                     col("ZISPINNED", "pinned"),
                     "d.ZDATA AS data" if "ZDATA" in dcols else "NULL AS data"])
        + f" FROM ZICCLOUDSYNCINGOBJECT o {join} WHERE {where} "
          f"ORDER BY o.Z_PK, d.Z_PK").fetchall()

    folders: dict[int, dict] = {}
    if "ZFOLDER" in cols:
        for r in conn.execute(
                "SELECT Z_PK, "
                + ", ".join([f"{folder_title_col} AS title" if folder_title_col else "NULL AS title",
                             "ZFOLDERTYPE AS type" if "ZFOLDERTYPE" in cols else "NULL AS type",
                             "ZOWNER AS owner" if "ZOWNER" in cols else "NULL AS owner"])
                + " FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK IN (SELECT ZFOLDER FROM "
                  "ZICCLOUDSYNCINGOBJECT WHERE ZFOLDER IS NOT NULL)"):
            folders[r["Z_PK"]] = {"title": r["title"], "type": r["type"],
                                  "owner": r["owner"]}
    accounts: dict[int, str] = {}
    owners = sorted({f["owner"] for f in folders.values() if f["owner"] is not None})
    if owners and "ZNAME" in cols:
        marks = ",".join("?" for _ in owners)
        accounts = {r[0]: r[1] for r in conn.execute(
            f"SELECT Z_PK, ZNAME FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK IN ({marks})",
            owners) if r[1]}

    store_uuid = None
    if "Z_METADATA" in tables and "Z_UUID" in _columns(conn, "Z_METADATA"):
        row = conn.execute("SELECT Z_UUID FROM Z_METADATA LIMIT 1").fetchone()
        store_uuid = row[0] if row and row[0] else None

    records: list[dict] = []
    present: set[str] = set()
    skipped: dict[str, int] = {}
    per_folder: dict[str, int] = {}
    done: set[int] = set()
    seen = 0
    for r in rows:
        if r["pk"] in done:
            continue                   # a second note-data row for the same note
        done.add(r["pk"])
        seen += 1
        ext_id = f"apple:{r['ident'] or 'pk' + str(r['pk'])}"
        if r["marked"]:
            _count(skipped, "note marked for deletion (deleted — not kept)")
            continue
        folder = folders.get(r["folder"]) or {}
        if folder.get("type") == TRASH_FOLDER_TYPE or folder.get("title") == TRASH_FOLDER_TITLE:
            _count(skipped, "note in Recently Deleted (deleted — not kept)")
            continue
        present.add(ext_id)            # still in Notes, even if unreadable below
        if r["locked"]:
            _count(skipped, "locked note (password-protected — its text is encrypted)")
            continue
        if r["data"] is None:
            _count(skipped, "note whose text is not on this Mac (not yet downloaded)")
            continue
        try:
            text = note_text(r["data"])
        except ValueError:
            _count(skipped, "note whose text could not be decoded")
            continue
        attachments = text.count(OBJECT_REPLACEMENT)
        body = "\n".join(ln.rstrip() for ln in
                         text.replace(OBJECT_REPLACEMENT, "").split("\n")).strip()
        title = (r["title"] or "").strip() or \
            next((ln.strip() for ln in body.split("\n") if ln.strip()), "")
        modified = _core_data_iso(r["modified"])
        started = _core_data_iso(r["created"]) or modified
        if started is None:
            _count(skipped, "note with no usable timestamp")
            continue
        if not body and not title and not attachments:
            _count(skipped, "empty note")
            continue
        detail = {"source": "apple_notes", "folder": folder.get("title"),
                  "account": accounts.get(folder.get("owner"))}
        if r["pinned"]:
            detail["pinned"] = True
        if attachments:
            detail["attachments"] = attachments
        detail = {k: v for k, v in detail.items() if v is not None}
        if folder.get("title"):
            _count(per_folder, folder["title"])
        records.append(_record(ext_id, started, cfg, title=title, body=body,
                               detail=detail, origin="apple_notes",
                               modified=modified or started))
    return {"kind": "apple_notes", "records": records, "handles": set(),
            "account": f"apple_notes:{store_uuid}" if store_uuid else None,
            "account_from": "the store's Core Data UUID" if store_uuid else None,
            "skipped": skipped, "unmapped": {}, "present": present, "seen": seen,
            "facts": {"folders": dict(sorted(per_folder.items()))}}


def _scan_apple(store: Path, cfg: Config) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-notes-", ignore_cleanup_errors=True)
    try:
        conn = _snapshot(store, Path(tmp.name))
        try:
            result = _read_apple(conn, cfg)
        finally:
            conn.close()
    finally:
        tmp.cleanup()
    digest = hashlib.sha256()
    for p in (store, store.with_name(store.name + "-wal")):
        if p.exists():
            digest.update(base.file_hash(p).encode())
    return {**result, "source_file": store.name, "source_hash": digest.hexdigest(),
            "read_from": str(store.resolve())}


# ---------------------------------------------------------------- scan

def _classify(target: Path) -> tuple[str, Path | None]:
    if not target.exists():
        raise DomainError(f"not found: {target}")
    if target.is_dir():
        store = target / "NoteStore.sqlite"
        if store.is_file():
            return "apple", store
        keep = _keep_dir(target)
        if keep is not None:
            return "keep", keep
        return "folder", target
    if zipfile.is_zipfile(target):
        return "keep", None
    with open(target, "rb") as f:
        head = f.read(len(SQLITE_MAGIC))
    if head == SQLITE_MAGIC:
        return "apple", target
    raise DomainError(
        f"{target.name} is not a notes folder, a Google Keep Takeout (.zip or its "
        f"folder) or an Apple Notes store (NoteStore.sqlite) — pass the folder, "
        f"not a single note")


def scan(target: Path, cfg: Config) -> base.Scan:
    try:
        kind, where = _classify(target)
        if kind == "apple":
            result = _scan_apple(where, cfg)
        elif kind == "keep":
            result = _read_keep(target, where, cfg)
        else:
            result = _read_folder(target, cfg)
    except PermissionError as e:
        raise _no_access(Path(e.filename) if e.filename else target, e) from e

    records = sorted(result["records"],
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    dates = [r["journal_date"] for r in records]
    facts = {"source": result["kind"], "read_from": result.get("read_from"),
             "oldest_note": min(dates) if dates else None,
             "newest_note": max(dates) if dates else None,
             **result.get("facts", {})}
    mirroring = {}
    if result["present"] is not None:
        # Each scan is the source's complete current state, so the archive
        # mirrors it — within this one account; another vault, Keep account
        # or Mac is never touched.
        mirroring = {"present_ids": result["present"], "mirror_streams": list(streams)}
    return base.empty_scan(
        name,
        account=result["account"],
        source_file=result["source_file"],
        source_hash=result["source_hash"],
        records=records,
        handles=[{"kind": k, "value": v, "display_name": None}
                 for k, v in sorted(result["handles"])],
        unmapped=[{"what": k, "count": v} for k, v in sorted(result["unmapped"].items())],
        skipped=[f"{v} × {k}" for k, v in sorted(result["skipped"].items())],
        facts=facts,
        source_kind=result["kind"],
        notes_seen=result["seen"],
        account_from=result.get("account_from"),
        account_hint=result.get("account_hint"),
        mirror_off=result.get("mirror_off"),
        **mirroring)


# ---------------------------------------------------------------- default target

def downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _newest_keep_takeout(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    zips = sorted(folder.glob("takeout-*.zip"),
                  key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                if any(_is_keep_member(n) for n in zf.namelist()):
                    return z
        except (zipfile.BadZipFile, OSError):
            continue
    return None


def default_target(cfg: Config) -> Path:
    """What `lifecrm observe notes` reads when given no path.

    [connectors.notes] path, when set — config always wins; then, on a Mac,
    the Apple Notes store; then the newest Google Takeout zip in Downloads
    that holds Keep notes. Never a guess beyond those.
    """
    configured = cfg.connector_settings(name).get("path")
    if isinstance(configured, str) and configured.strip():
        p = Path(configured.strip()).expanduser()
        if not p.is_absolute():
            p = cfg.root / p
        if not p.exists():
            raise DomainError(f"[connectors.notes] path {p} does not exist")
        return p
    looked = []
    if platforms.is_mac():
        store = platforms.mac_notes_db()
        looked.append(str(store))
        try:
            if store.exists():
                return store
        except PermissionError as e:
            raise _no_access(store, e) from e
    downloads = downloads_dir()
    looked.append(f"{downloads}{os.sep}takeout-*.zip")
    found = _newest_keep_takeout(downloads)
    if found is not None:
        return found
    raise DomainError(
        f"no notes source found (looked in: {'; '.join(looked)}) — pass a folder of "
        f".md/.txt notes, a Google Keep Takeout zip or NoteStore.sqlite, or set "
        f"[connectors.notes] path in config.toml")


# ---------------------------------------------------------------- report

_KIND_LABEL = {"obsidian": "Obsidian vault", "notes_folder": "notes folder",
               "keep": "Google Keep Takeout", "apple_notes": "Apple Notes store"}


def _sample(rec: dict) -> str:
    lines = [ln for ln in (rec["body"] or "").split("\n") if ln.strip()]
    # An Apple note's first line is its title, and so is a leading markdown
    # heading; the sample shows what follows instead of saying it twice.
    if lines and rec["title"] and lines[0].lstrip("#").strip() == rec["title"]:
        lines = lines[1:]
    body = " ".join(" ".join(lines).split())
    if len(body) > 50:
        body = body[:49] + "…"
    title = rec["title"] or "(untitled)"
    return f'{rec["journal_date"]} "{title}": {body}' if body \
        else f'{rec["journal_date"]} "{title}"'


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    kind = _KIND_LABEL.get(scan.get("source_kind"), "notes")
    lines = [f"notes: {scan['source_file']} ({kind}; "
             f"{scan.get('notes_seen', len(scan['records']))} note(s) in the source)"]
    facts = scan.get("facts") or {}
    if facts.get("read_from"):
        lines.append(f"  read from: {facts['read_from']}")
    if scan["account"]:
        lines.append(f"  account: {scan['account']}"
                     + (f" (from {scan['account_from']})" if scan.get("account_from") else ""))
    if facts.get("oldest_note"):
        lines.append(f"  notes span {facts['oldest_note']}..{facts['newest_note']} — "
                     f"check the oldest against what the app shows")
    if facts.get("folders"):
        lines.append("  folders: " + ", ".join(f"{k} ({v})" for k, v in facts["folders"].items()))

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  note: {len(new)} new, {len(upd)} changed | {span} | "
                     f"e.g. {_sample(latest)}")
    elif not plan.get("removed"):
        lines.append("  nothing new — the source matches the database")
    if scan.get("mirror_off"):
        lines.append(f"  not mirroring deletions: {scan['mirror_off']}")
    lines += base.removal_lines(plan, "no longer in the source (deleted or trashed)",
                                lambda row: row["title"])
    if plan.get("mirror_note") and scan.get("account_hint"):
        lines.append(f"  (to mirror: {scan['account_hint']})")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate id(s) inside the source")
    if tail:
        lines.append("  " + ", ".join(tail))
    if scan["handles"]:
        lines.append(f"  handles: {len(scan['handles'])} address(es) notes are shared "
                     f"with — review with 'lifecrm handles list --suggest'")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:40]}: differs from "
                     f"the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not imported: {u['count']} × {u['what']}")
    return lines
