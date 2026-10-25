"""The reminders calendar: due tasks and plans, and linked birthdays, as one
`.ics` the phone's calendar can read (docs/assistant.md).

One publisher. `publish()` is the only code that produces the calendar and
it runs in the CLI (`lifecrm reminders publish`), in the after-write hook of
every CLI command (cli._arm_vault_refresh, when `[assistant] reminders_dir`
is set) and in the daily job (scripts/reminders.*). `lifecrm serve` answers
`/reminders.ics` with the bytes this wrote and computes nothing.

Why a publication is more than a render: a calendar client re-reads a
component only when its SEQUENCE moved (RFC 5545 3.8.7.4), and the client
keeps whatever it fetched last, so every change to an event — an overdue task
re-dated to today, a snooze, a title edit, an item dismissed and later
restored — must be a recorded revision. lifecrm/domain/reminders.py keeps
those revisions in the database; the write transaction that records them is
also the lock between two publishers, and the file is written inside it so a
failed write leaves the stored revisions untouched. DTSTAMP is each
component's own last revision, so an unchanged calendar is byte-identical
and nothing is rewritten.

Content rules: every open task or plan with a due date is a 30-minute event
at `[assistant] reminder_hour` local on its due day (an overdue one on
`today`, so the phone nags each morning); every linked contact-card birthday
is an all-day event, one dated occurrence for this year and one for next,
never a yearly rule, so a snooze moves one occurrence and a dismissal drops
one. A snoozed item sits on its `until` day; a dismissed one is left out and
its revision row becomes a tombstone. The nudge keys and the UIDs line up
one to one.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db as db_mod, ics, nudges
from .config import Config
from .domain import nudges as nudge_state
from .domain import reminders as revisions
from .domain.util import DomainError
from .review import NOT_DERIVED

FILE_NAME = "lifecrm-reminders.ics"
PRODID = "-//lifecrm//reminders//EN"
CALNAME = "Life CRM reminders"
DUE_MINUTES = 30
STAMP_PROPS = ("DTSTAMP", "SEQUENCE", "LAST-MODIFIED")


# ---------------------------------------------------------------- where

def data_path(cfg: Config) -> Path:
    """The published calendar, always written here (what `serve` returns)."""
    return cfg.data_dir / "reminders" / FILE_NAME


def synced_path(cfg: Config, s: dict | None = None) -> Path | None:
    """The copy in `[assistant] reminders_dir` (a synced folder the phone's
    calendar app reads), or None when unset."""
    s = s or nudges.settings(cfg)
    folder = s.get("reminders_dir")
    if not folder:
        return None
    return cfg.resolve(folder) / FILE_NAME


def auto_publish(cfg: Config) -> bool:
    """True when the after-write hook should publish: a reminders folder is
    set, `[assistant] auto_publish` is not false, and the demo builder has
    not switched the hooks off for its own run (obsidian.NO_REFRESH_ENV)."""
    from .obsidian import NO_REFRESH_ENV
    if os.environ.get(NO_REFRESH_ENV, "").strip() not in ("", "0"):
        return False
    s = nudges.settings(cfg)
    return bool(s["reminders_dir"]) and bool(s["auto_publish"])


# ---------------------------------------------------------------- components

def _due_components(conn: sqlite3.Connection, today: date, states: dict) -> list[dict]:
    rows = conn.execute(
        "SELECT e.uuid, e.title, e.due_at, e.journal_date, k.name AS kind "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        "WHERE e.status = 'active' AND e.closed_at IS NULL "
        "AND k.name IN ('task', 'plan') AND e.due_at IS NOT NULL "
        f"AND {NOT_DERIVED} ORDER BY e.due_at, e.id").fetchall()
    out = []
    for r in rows:
        key = f"due:{r['uuid']}"
        st = states.get(key)
        if st is not None and st["state"] == "dismissed":
            continue
        due = date.fromisoformat(r["due_at"][:10])
        if st is not None and st["state"] == "snoozed" and st["until"] > today.isoformat():
            day, note = date.fromisoformat(st["until"]), f"snoozed until {st['until']}"
        elif due < today:
            day, note = today, f"overdue by {(today - due).days} day(s)"
        else:
            day, note = due, None
        desc = (f"{r['kind']} recorded {r['journal_date']}, due {due.isoformat()}"
                + (f"; {note}" if note else "") + f"\nlifecrm {key}")
        out.append({"uid": f"lifecrm-due-{r['uuid']}@lifecrm", "key": key, "kind": "due",
                    "summary": r["title"], "description": desc, "day": day,
                    "all_day": False})
    return out


def _birthday_components(conn: sqlite3.Connection, cfg: Config, today: date,
                         states: dict, owners: frozenset) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT o.uuid, o.journal_date, "
            "json_extract(o.value_json, '$.birthday') AS birthday, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS entity_id, "
            "CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END AS name, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observations o "
            "JOIN observation_participants p ON p.observation_id = o.id "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id "
            "LEFT JOIN entities m ON m.id = e.merged_into "
            "WHERE o.stream = 'contact_card' AND o.status = 'active' "
            "AND json_valid(o.value_json) "
            "AND json_extract(o.value_json, '$.birthday') IS NOT NULL "
            "AND h.ignored = 0 ORDER BY o.id, p.handle_kind, p.handle_value").fetchall()
    except sqlite3.OperationalError:
        return []
    best: dict[int, tuple] = {}
    for r in rows:
        if r["etype"] != "person" or r["entity_id"] in owners:
            continue
        parsed = nudges.parse_birthday(r["birthday"])
        if parsed is None:
            continue
        current = best.get(r["entity_id"])
        if current is None or (current[0][0] is None and parsed[0] is not None):
            best[r["entity_id"]] = (parsed, r)
    out = []
    for entity_id, ((year, month, day_), r) in sorted(best.items()):
        first = nudges.next_occurrence(month, day_, today)
        for occ in (first, nudges.next_occurrence(month, day_, first + timedelta(days=1))):
            key = f"birthday:{entity_id}:{occ.year}"
            st = states.get(key)
            if st is not None and st["state"] == "dismissed":
                continue
            day = occ
            note = None
            if st is not None and st["state"] == "snoozed" and st["until"] > today.isoformat():
                day, note = date.fromisoformat(st["until"]), f"snoozed until {st['until']}"
            age = f" (turning {occ.year - year})" if year else ""
            desc = (f"{r['name']}'s birthday{age}, from a contact card dated "
                    f"{r['journal_date']} (observed, not confirmed)"
                    + (f"; {note}" if note else "") + f"\nlifecrm {key}")
            out.append({"uid": f"lifecrm-bday-{entity_id}-{occ.year}@lifecrm", "key": key,
                        "kind": "birthday", "summary": f"{r['name']}'s birthday{age}",
                        "description": desc, "day": day, "all_day": True})
    return out


def _anniversary_components(conn: sqlite3.Connection, today: date, states: dict,
                            owners: frozenset) -> list[dict]:
    """Anniversaries you recorded (data.anniversary, lifecrm/gifts.py) as
    dated occurrences this year and next, like birthdays; the nudge's key,
    so a snooze or a dismissal moves or omits it here too. Confirmed."""
    from . import gifts
    out = []
    for a in gifts.recorded(conn, owners):
        _year, month, day_ = a["parsed"]
        first = nudges.next_occurrence(month, day_, today)
        for occ in (first, nudges.next_occurrence(month, day_, first + timedelta(days=1))):
            key = f"anniversary:{a['uuid']}:{occ.year}"
            st = states.get(key)
            if st is not None and st["state"] == "dismissed":
                continue
            day = occ
            note = None
            if st is not None and st["state"] == "snoozed" and st["until"] > today.isoformat():
                day, note = date.fromisoformat(st["until"]), f"snoozed until {st['until']}"
            summary = gifts.label(a, occ)
            desc = (f"{summary}, from your entry of {a['date']}" + (f"; {note}" if note else "")
                    + f"\nlifecrm {key}")
            out.append({"uid": f"lifecrm-anniv-{a['uuid']}-{occ.year}@lifecrm", "key": key,
                        "kind": "anniversary", "summary": summary, "description": desc,
                        "day": day, "all_day": True})
    return out


def components(conn: sqlite3.Connection, cfg: Config, *, today: date,
               s: dict | None = None) -> list[dict]:
    """Every component the calendar holds on `today`, each with the lines of
    its body (without the revision stamps) and the fingerprint of them."""
    s = s or nudges.settings(cfg)
    try:
        states = nudge_state.state_for(conn)
    except sqlite3.OperationalError:
        states = {}
    try:
        from . import owner as owner_mod
        owners = owner_mod.identity(conn, cfg).entities
    except Exception:            # a bad [owner] table: nobody is skipped
        owners = frozenset()
    comps = _due_components(conn, today, states)
    comps += _birthday_components(conn, cfg, today, states, owners)
    from . import features
    if features.enabled(cfg, "gift_ideas"):
        comps += _anniversary_components(conn, today, states, owners)
    tz = ZoneInfo(cfg.timezone)
    for c in comps:
        c["body"] = _body_lines(c, s["reminder_hour"], tz)
        c["fingerprint"] = hashlib.sha256(
            "\r\n".join(c["body"]).encode("utf-8")).hexdigest()
    return sorted(comps, key=lambda c: c["uid"])


def _body_lines(c: dict, reminder_hour: int, tz: ZoneInfo) -> list[str]:
    """The component without DTSTAMP, SEQUENCE and LAST-MODIFIED: what the
    fingerprint hashes, and what the stamps are appended to."""
    day: date = c["day"]
    lines = ["BEGIN:VEVENT", f"UID:{c['uid']}", f"SUMMARY:{ics.esc(c['summary'])}"]
    if c["all_day"]:
        nxt = day + timedelta(days=1)
        lines += [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}"]
        trigger = f"PT{reminder_hour}H"              # that morning, not midnight
    else:
        start = datetime(day.year, day.month, day.day, reminder_hour, tzinfo=tz)
        lines += [f"DTSTART:{ics.utc(start)}",
                  f"DTEND:{ics.utc(start + timedelta(minutes=DUE_MINUTES))}"]
        trigger = "-PT0M"
    lines += [f"DESCRIPTION:{ics.esc(c['description'])}", f"CATEGORIES:lifecrm,{c['kind']}",
              "TRANSP:TRANSPARENT", "BEGIN:VALARM", "ACTION:DISPLAY",
              f"DESCRIPTION:{ics.esc(c['summary'])}", f"TRIGGER:{trigger}", "END:VALARM"]
    return lines


def render(comps: list[dict], stamps: dict[str, dict]) -> bytes:
    """The calendar's bytes: the header, every component with its revision
    stamps (DTSTAMP = its last revision, so nothing changes without cause)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics.esc(CALNAME)}",
             "X-WR-CALDESC:" + ics.esc("Due tasks and plans, and birthdays, from your "
                                       "Life CRM. Regenerated by lifecrm reminders "
                                       "publish; edits made in a calendar app are not "
                                       "read back.")]
    for c in comps:
        st = stamps[c["uid"]]
        stamp = _instant(st["last_modified"])
        lines += c["body"] + [f"DTSTAMP:{stamp}", f"SEQUENCE:{st['sequence']}",
                              f"LAST-MODIFIED:{stamp}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ics.render(lines)


def _instant(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------- publication

def write_atomic(path: Path, body: bytes) -> bool:
    """Write `body` to `path` through a temp file and a replace; False (and
    no write) when the file already holds exactly these bytes."""
    if path.exists() and path.read_bytes() == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return True


def publish(cfg: Config, *, today: date | str, now: datetime | None = None,
            out: Path | str | None = None, dry_run: bool = False,
            if_configured: bool = False) -> dict:
    """Regenerate the calendar, record its revisions, write it.

    In order: a write transaction (BEGIN IMMEDIATE: two publishers serialize
    on it); the components; the revision rules (domain.reminders); the bytes;
    the data copy written atomically (skipped when identical); commit; then
    the copies to `[assistant] reminders_dir` and `out`, each atomically. A
    failed file write rolls the revisions back; a commit that fails after the
    file was written leaves stored fingerprints behind the file's, and the
    next publication moves those sequences once more — harmless to a client,
    and the recovery is to publish again. A failed copy is one warning, and
    the next publication copies again because the bytes differ.

    Without `[assistant] reminders_dir`, `out` or `if_configured`, refuses:
    a calendar nobody reads is a surprise waiting in data/.
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    now = now or datetime.now(timezone.utc)
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = nudges.settings(cfg)
    synced = synced_path(cfg, s)
    if synced is None and out is None and not if_configured:
        raise DomainError("no reminders folder: set [assistant] reminders_dir in "
                          "config.toml (a synced folder the phone's calendar reads), or "
                          "pass --out FILE (docs/assistant.md)")
    if not cfg.db_path.exists():
        raise DomainError(f"database not found at {cfg.db_path} — run: python -m lifecrm init")
    conn = db_mod.connect(cfg.db_path)
    try:
        pending = db_mod.pending_migrations(conn)
        if pending:
            raise DomainError(f"database schema v{db_mod.schema_version(conn)} is behind "
                              f"code — run: python -m lifecrm migrate")
        conn.execute("BEGIN IMMEDIATE")
        comps = components(conn, cfg, today=today, s=s)
        result = revisions.record_publication(
            conn, {c["uid"]: c["fingerprint"] for c in comps}, now_iso)
        body = render(comps, result["stamps"])
        target = data_path(cfg)
        if dry_run:
            conn.rollback()
            return {"dry_run": True, "path": str(target), "components": len(comps),
                    "bytes": len(body), "would_change": any(
                        result[k] for k in ("added", "changed", "restored", "removed")),
                    "added": result["added"], "changed": result["changed"],
                    "restored": result["restored"], "removed": result["removed"],
                    "copies": [str(p) for p in (synced, out) if p], "warnings": []}
        wrote = write_atomic(target, body)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    copies, warnings = [], []
    for dest in (synced, Path(out) if out else None):
        if dest is None:
            continue
        try:
            write_atomic(Path(dest), body)
            copies.append(str(dest))
        except OSError as e:
            warnings.append(f"could not copy the calendar to {dest}: {e}")
    return {"dry_run": False, "path": str(target), "wrote": wrote,
            "components": len(comps), "bytes": len(body),
            "added": result["added"], "changed": result["changed"],
            "restored": result["restored"], "removed": result["removed"],
            "copies": copies, "warnings": warnings}
