"""iPhone call history: phone and FaceTime calls, from a Mac or an iPhone backup.

The phone's Recents list is a Core Data store, CallHistory.storedata. Two
copies of it are within reach:

- **On a Mac**, ~/Library/Application Support/CallHistoryDB/CallHistory.storedata.
  With the iPhone's calls syncing to the Mac (FaceTime > Calls from iPhone,
  and the same Apple ID), it holds the phone's calls as well as the Mac's own
  FaceTime calls, kept current without a backup. This is the everyday path
  for the MacBook + iPhone setup.
- **In an iPhone backup**, as HomeDomain / Library/CallHistoryDB/
  CallHistory.storedata — the hashed file 5a4935c78a5255723f707230a451d79c540d2741,
  which is SHA1 of 'HomeDomain-Library/CallHistoryDB/CallHistory.storedata'.
  Apple lists call history among what only an *encrypted* backup carries, so
  an ordinary unencrypted backup is expected not to have it; when the file is
  absent the import says so and points at the Mac instead of failing
  mysteriously. (An encrypted backup cannot be read at all — see texts.md.)

What comes in
-------------
One `ios_call` observation per call record, keyed on ZUNIQUE_ID:

  ZDATE            started_at (Core Data time: seconds since 2001-01-01 UTC)
  ZDURATION        value, unit 's'; ended_at = start + duration
  ZORIGINATED      direction: 1 out, 0 in
  ZANSWERED        an incoming call never answered is a missed call
  ZCALLTYPE        1 phone, 8 FaceTime video, 16 FaceTime audio; a call carried
                   by another app through CallKit names that app in
                   ZSERVICE_PROVIDER and is kept as an app call
  ZADDRESS         the other party, as a phone/email **handle** — never a name
  ZNAME            the name the phone showed: a display_name only, used when
                   no contact card names the handle
  ZISO_COUNTRY_CODE, ZLOCATION   kept in value_json

Columns differ across iOS versions, so each is checked before it is read; a
missing one reads as NULL. Group FaceTime participants are archived blobs
(ZREMOTE_PARTICIPANT_HANDLES) and are not decoded: such a call carries the
handle in ZADDRESS when there is one, and none otherwise.

Deletion semantics — the archive mirrors the source (decision #2)
-----------------------------------------------------------------
Like texts: a call deleted from Recents is gone from the next read, and its
observation is removed — scoped to rows first imported from the same
account. The store does not name the phone's own number, so the account is
the backup's 'Phone Number' (Info.plist) for a backup, and the store's own
Core Data identity (Z_METADATA.Z_UUID) otherwise. That keeps a backup import
and a Mac import from ever removing each other's calls: the same call held by
both is one record (ZUNIQUE_ID travels with the sync), and each source can
only remove what it brought in.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _abcddb, base
from . import ios_messages as im
from . import mac_messages

name = "ios_calls"
streams = ("ios_call",)
# Calls are rarer than texts, but two weeks with none at all means the sync
# or the backups have stopped.
stale_after_days = 14

STORE = "CallHistory.storedata"
CALL_HISTORY = ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
CALL_HISTORY_FILE_ID = hashlib.sha1(
    f"{CALL_HISTORY[0]}-{CALL_HISTORY[1]}".encode()).hexdigest()

_CALL_TYPES = {1: "phone", 8: "facetime_video", 16: "facetime_audio"}
_LABELS = {"phone": "phone call", "facetime_video": "FaceTime video call",
           "facetime_audio": "FaceTime audio call", "app": "app call"}
_ORIGIN = {"phone": "phone", "facetime_video": "facetime",
           "facetime_audio": "facetime"}
_COLUMNS = ("ZUNIQUE_ID", "ZADDRESS", "ZDATE", "ZDURATION", "ZORIGINATED",
            "ZANSWERED", "ZCALLTYPE", "ZNAME", "ZISO_COUNTRY_CODE",
            "ZSERVICE_PROVIDER", "ZLOCATION")


def mac_store() -> Path:
    """This Mac's call history (needs Full Disk Access to read)."""
    return platforms.home() / "Library" / "Application Support" / "CallHistoryDB" / STORE


# ---------------------------------------------------------------- finding the store

def default_target(cfg: Config) -> Path:
    """This Mac's call history when there is one, else the one iPhone backup
    on this machine — never a guess between several phones."""
    store = mac_store()
    with im.privacy_guard(store):
        if store.exists():
            return store
    roots = im.backup_roots()
    return im._choose([b for root in roots for b in im.find_backups(root)],
                      ", ".join([str(store.parent)] + [str(r) for r in roots]))


def _resolve(target: Path) -> dict:
    """{'store', 'store_wal', 'books': [(path, wal)], 'kind', 'folder'}."""
    if target.is_dir():
        if (target / "Manifest.db").exists():
            files = im.backup_files(target, {"store": CALL_HISTORY,
                                             "contacts": im.ADDRESS_BOOK})
            if files["store"] is None:
                raise DomainError(
                    f"{target.name} is an iPhone backup with no call history "
                    f"(Library/CallHistoryDB/{STORE}) — Apple keeps call history "
                    f"out of unencrypted backups. Read it from the Mac instead "
                    f"(FaceTime > Calls from iPhone): see docs/calls.md")
            books = [(files["contacts"], files["contacts_wal"])] \
                if files["contacts"] else []
            return {"store": files["store"], "store_wal": files["store_wal"],
                    "books": books, "kind": "backup", "folder": target}
        if (target / STORE).exists():
            return _resolve(target / STORE)
        backups = im.find_backups(target)
        if backups:          # the MobileSync/Backup folder itself: its one backup
            return _resolve(im._choose(backups, str(target)))
        listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} is neither an iPhone backup (no Manifest.db) nor a "
            f"folder holding {STORE} (has: {listing})")
    # A store sits in <Application Support>/CallHistoryDB, and the Contacts
    # stores that name its handles in <Application Support>/AddressBook.
    live = _same(target, mac_store())
    try:
        books = platforms.mac_addressbook_dbs() if live \
            else mac_messages.address_books_under(target.parent.parent)
    except PermissionError:
        books = []                  # names are a courtesy; the calls still come in
    return {"store": target, "store_wal": None, "books": [(b, None) for b in books],
            "kind": "live" if live else "file", "folder": target.parent}


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# ---------------------------------------------------------------- reading

def _iso(seconds: float) -> str:
    return (_abcddb.APPLE_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value) -> str | None:
    """ZADDRESS is TEXT on most versions and a BLOB of the same UTF-8 on some."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    s = str(value).strip()
    return s or None


def _store_uuid(conn) -> str | None:
    """The store's own Core Data identity: stable for the life of the file,
    different on every device."""
    if not im._columns(conn, "Z_METADATA"):
        return None
    row = conn.execute("SELECT Z_UUID FROM Z_METADATA LIMIT 1").fetchone()
    return str(row[0]) if row and row[0] else None


def _read(conn, contacts: dict, cfg: Config) -> dict:
    cols = im._columns(conn, "ZCALLRECORD")
    if not cols:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        raise DomainError(f"not a call history database — no ZCALLRECORD table "
                          f"(has: {', '.join(tables[:10]) or 'nothing'})")
    if "ZDATE" not in cols:
        raise DomainError("ZCALLRECORD has no ZDATE column — not a call history "
                          "this reader understands")
    select = ", ".join(c if c in cols else f"NULL AS {c}" for c in _COLUMNS)

    records: list[dict] = []
    handles: dict[tuple[str, str], str | None] = {}
    present: set[str] = set()
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    examples: dict[str, tuple[str, str]] = {}
    seen = 0
    for row in conn.execute(f"SELECT Z_PK, {select} FROM ZCALLRECORD ORDER BY Z_PK"):
        r = dict(zip(["pk", *_COLUMNS], row))
        seen += 1
        address = _text(r["ZADDRESS"])
        uid = _text(r["ZUNIQUE_ID"])
        if uid is None:
            # No id of its own: one made from what the call is, so it is still
            # the same record on the next read.
            uid = "call:" + hashlib.sha1(
                f"{address}|{r['ZDATE']}|{r['ZORIGINATED']}".encode()).hexdigest()
        present.add(uid)                       # held by the source, even if skipped
        try:
            when = float(r["ZDATE"]) if r["ZDATE"] is not None else None
        except (TypeError, ValueError):
            when = None
        if when is None or when <= 0:
            skipped["call with no usable timestamp"] = \
                skipped.get("call with no usable timestamp", 0) + 1
            continue
        started = _iso(when)
        try:
            duration = max(0.0, float(r["ZDURATION"] or 0))
        except (TypeError, ValueError):
            duration = 0.0
        seconds = int(round(duration))
        out = bool(r["ZORIGINATED"])
        answered = bool(r["ZANSWERED"])

        provider = _text(r["ZSERVICE_PROVIDER"])
        code = r["ZCALLTYPE"]
        if provider and not provider.startswith("com.apple."):
            kind = "app"                       # carried by another app via CallKit
        elif code in _CALL_TYPES:
            kind = _CALL_TYPES[code]
        else:
            kind = f"type {code}"
            unmapped[f"call type {code} (imported as a call)"] = \
                unmapped.get(f"call type {code} (imported as a call)", 0) + 1
        label = _LABELS.get(kind, "call")
        missed = not out and not answered
        title = (f"missed {label}" if missed
                 else f"{label}, not answered" if out and not answered else label)

        participants = []
        h = base.handle_for(address)
        if h:
            name_shown = _text(r["ZNAME"])
            label_for = contacts.get(h) or name_shown
            if h not in handles or (label_for and not handles[h]):
                handles[h] = label_for
            participants.append({"kind": h[0], "value": h[1], "role": "participant"})
            if len(examples) < 50 and address != h[1]:
                examples.setdefault(address, h)

        detail = {"call_type": kind, "answered": answered,
                  "missed": True if missed else None, "service": provider,
                  "country": _text(r["ZISO_COUNTRY_CODE"]),
                  "number_location": _text(r["ZLOCATION"]),
                  "caller_unknown": True if h is None else None}
        records.append({
            "stream": "ios_call", "external_id": uid,
            "journal_date": base.journal_date_for_iso(started, cfg),
            "started_at": started,
            "ended_at": _iso(when + seconds) if seconds else None,
            "utc_offset_sec": None,
            "direction": "out" if out else "in",
            "title": title, "body": None,
            "value": seconds, "unit": "s",
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None,
            "origin_app": _ORIGIN.get(kind, provider if kind == "app" else "phone"),
            "last_modified_at": started,
            "participants": participants,
        })
    return {"records": records, "handles": handles, "present": present,
            "skipped": skipped, "unmapped": unmapped, "examples": examples,
            "seen": seen, "store_uuid": _store_uuid(conn)}


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    with im.privacy_guard(target):
        files = _resolve(target)
        info = im.backup_info(files["folder"]) if files["kind"] == "backup" else {}
        tmp = tempfile.TemporaryDirectory(prefix="lifecrm-calls-")
        try:
            tmp_path = Path(tmp.name)
            conn, wal_read = im._snapshot(files["store"], files["store_wal"],
                                          tmp_path, "calls")
            try:
                contacts, books_read = im.contact_names(files["books"], tmp_path)
                result = _read(conn, contacts, cfg)
            finally:
                conn.close()
        finally:
            tmp.cleanup()
        store = files["store"]
        source_hash = im._content_hash(store, files["store_wal"]
                                       or store.with_name(store.name + "-wal"))
        folder = str(files["folder"].resolve())

    account = None
    if files["kind"] == "backup" and isinstance(info.get("Phone Number"), str):
        h = base.handle_for(info["Phone Number"])
        account = h[1] if h and h[0] == "phone" else None
    if account is None and result["store_uuid"]:
        account = f"callhistory:{result['store_uuid']}"

    records = sorted(result["records"],
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    handles = [{"kind": k, "value": v, "display_name": label}
               for (k, v), label in sorted(result["handles"].items())]
    dates = [r["journal_date"] for r in records]
    names = dict(result["handles"])
    device = {}
    if isinstance(info.get("Device Name"), str):
        device["device"] = info["Device Name"]
    if hasattr(info.get("Last Backup Date"), "strftime"):
        device["backup_date"] = info["Last Backup Date"].strftime("%Y-%m-%d")
    return base.empty_scan(
        name,
        account=account,
        source_file=files["folder"].name if files["kind"] == "backup" else store.name,
        source_hash=source_hash,
        records=records,
        handles=handles,
        unmapped=[{"what": k, "count": v} for k, v in sorted(result["unmapped"].items())],
        skipped=[f"{v} × {k}" for k, v in sorted(result["skipped"].items())],
        facts={
            "folder": folder, "wal_read": wal_read,
            "oldest_call": min(dates) if dates else None,
            "newest_call": max(dates) if dates else None,
            "normalized": [{"raw": raw, "handle": f"{h[0]}:{h[1]}",
                            "name": names.get(h)}
                           for raw, h in sorted(result["examples"].items())[:5]],
            "address_books": len(files["books"]), "address_books_read": books_read,
        },
        present_ids=result["present"],
        mirror_streams=list(streams),
        target_kind=files["kind"],
        records_seen=result["seen"],
        named_handles=sum(1 for h in handles if h["display_name"]),
        **device)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    who = "you" if rec["direction"] == "out" else "them"
    minutes, secs = divmod(int(rec["value"] or 0), 60)
    length = f" ({minutes}m{secs:02d}s)" if rec["value"] else ""
    other = rec["participants"][0]["value"] if rec["participants"] else "an unknown caller"
    return f"{rec['journal_date']} {who}: {rec['title']}{length} with {other}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    where = {"backup": "iOS backup", "live": "this Mac's call history",
             "file": "call history file"}
    head = f"ios-calls: {scan['source_file']} ({where.get(scan['target_kind'], '?')}"
    if scan.get("device"):
        head += f" of {scan['device']}"
    if scan.get("backup_date"):
        head += f", taken {scan['backup_date']}"
    lines = [head + f"; {scan['records_seen']} call record(s))"]
    facts = scan.get("facts") or {}
    if facts.get("folder"):
        lines.append(f"  read from: {facts['folder']}")
    if scan["account"]:
        lines.append(f"  account: {scan['account']}")
    if facts.get("oldest_call"):
        lines.append(f"  calls span {facts['oldest_call']}..{facts['newest_call']} "
                     f"— check the oldest against the Recents list")
    lines.append(f"  {STORE}-wal: " + ("read — writes not yet folded into the main "
                                       "file are included" if facts.get("wal_read")
                                       else "none"))
    if facts.get("normalized"):
        lines.append("  normalized, e.g.: " + "; ".join(
            f"{n['raw']} -> {n['handle'].split(':', 1)[1]}"
            + (f" ({n['name']})" if n.get("name") else "")
            for n in facts["normalized"]))

    new, upd = plan["new"], [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        missed = sum(1 for r in new if json.loads(r["value_json"]).get("missed"))
        lines.append(f"  ios_call: {len(new)} new ({missed} missed), {len(upd)} changed "
                     f"| {span} | e.g. {_sample(latest)}")
    elif not plan.get("removed"):
        lines.append("  nothing new — the call history matches the database")
    lines += base.removal_lines(plan, "no longer in the call history (deleted)",
                                lambda row: row["title"])

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate id(s) inside the store")
    if tail:
        lines.append("  " + ", ".join(tail))
    if scan["handles"]:
        source = "from contacts and the names the phone showed"
        lines.append(f"  handles: {len(scan['handles'])} ({scan['named_handles']} "
                     f"named {source}) — review with 'lifecrm handles list --suggest'")
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
