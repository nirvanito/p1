"""`lifecrm nudges` — what the records say matters today, cited.

Reads only. Every nudge is recomputed from the database on each run and
rests on ids the reader can check: `E#` a confirmed entry, `O#` an observed
row or a count over observed rows (always "observed, not confirmed"), and the
nudge itself is `N#`. Nothing here is a model's opinion; the wording is fixed
per kind and says what the records show, never what the owner did.

The owner's only input is state: a nudge snoozed until a date or dismissed
for good (lifecrm/domain/nudges.py) goes to `hidden`. State attaches to a
durable key (`due:<entry uuid>`, `birthday:<entity id>:<year>`, ...), never to
the `N#` of one run, because an import or a closed task between two runs
renumbers everything.

A run can be saved (`--out FILE`); its `pack_id` hashes the pack's whole
content, so a changed title, date, value or state means a different pack. A
brief written from a saved pack is checked against that file (`check`),
never against a rebuild. `check` verifies that cited ids exist and that
every number and date in a sentence appears in the lines it cites; whether
the sentence's claim follows is the reader's judgement.

"Today" is the caller's: the CLI and /morning pass the clock's journal date
(recall.local_today), the Obsidian Home note passes the database's as-of
date, so the vault stays a pure function of the database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import nutrients
from .config import Config, ConfigError
from .domain import nudges as nudge_state
from .domain.reminders import CONTENT_CHANGE
from .domain import days as days_mod
from .domain.util import DomainError
from .recall import STREAM_GROUPS, check_text, local_today
from .review import NOT_DERIVED, _Ids, index_line
from .web import api

PACK_VERSION = 1
SECTION = "assistant"
DEFAULTS = {"due_days": 7, "birthday_days": 14, "nutrient_days": 14,
            "reminder_hour": 9, "reminders_dir": None, "auto_publish": True}
_DAY_KEYS = ("due_days", "birthday_days", "nutrient_days")
_FLAG_WORDS = {"true": True, "yes": True, "on": True, "1": True,
               "false": False, "no": False, "off": False, "0": False}
KIND_ORDER = ("reminder", "birthday", "nutrient", "stale")
TIER_NOTE = "(observed, not confirmed)"
CALENDAR_STREAMS = STREAM_GROUPS["calendar"]


# ---------------------------------------------------------------- settings

def settings(cfg: Config) -> dict:
    """[assistant] in config.toml with defaults; one-line errors for a value
    the layer cannot use."""
    raw = (cfg.extra or {}).get(SECTION)
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in _DAY_KEYS:
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 365:
                raise DomainError(f"[{SECTION}] {key} must be a whole number of days, "
                                  f"1-365, got {v!r}")
            out[key] = v
    if "reminder_hour" in raw:
        v = raw["reminder_hour"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 23:
            raise DomainError(f"[{SECTION}] reminder_hour must be an hour 0-23, got {v!r}")
        out["reminder_hour"] = v
    if "reminders_dir" in raw:
        v = raw["reminders_dir"]
        if v in ("", None):
            out["reminders_dir"] = None
        elif isinstance(v, str):
            out["reminders_dir"] = v
        else:
            raise DomainError(f"[{SECTION}] reminders_dir must be a folder path in "
                              f"quotes, got {v!r}")
    if "auto_publish" in raw:
        v = raw["auto_publish"]
        if isinstance(v, str) and v.strip().lower() in _FLAG_WORDS:
            v = _FLAG_WORDS[v.strip().lower()]
        if not isinstance(v, bool):
            raise DomainError(f"[{SECTION}] auto_publish must be true or false, got {v!r}")
        out["auto_publish"] = v
    known = set(DEFAULTS)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown setting(s) {', '.join(unknown)} — "
                          f"known: {', '.join(sorted(known))}")
    return out


def today_and_now(cfg: Config, today: str | None = None) -> tuple[date, datetime]:
    """The CLI's reference: the clock's journal date (timezone and rollover),
    or `today` as given with noon local as the instant."""
    if today is None:
        return local_today(cfg), datetime.now(timezone.utc)
    try:
        d = date.fromisoformat(today)
    except ValueError:
        raise DomainError(f"--today must be YYYY-MM-DD, got {today!r}") from None
    from zoneinfo import ZoneInfo
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(cfg.timezone))
    return d, noon.astimezone(timezone.utc)


def as_of_date(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """The journal date of the database's last content change (the rule the
    vault's start pages are dated by), or None for an empty audit log."""
    row = conn.execute(f"SELECT MAX(ts) FROM audit_log WHERE {CONTENT_CHANGE}").fetchone()
    if not row or not row[0]:
        return None
    try:
        stamp = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return days_mod.journal_date_for(stamp, cfg.timezone, cfg.day_rollover_hour)


# ---------------------------------------------------------------- helpers

def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _obs_item(r, **extra) -> dict:
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"], **extra}


_OBS_COLS = ("o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
             "o.ended_at, o.utc_offset_sec, o.direction, o.value_json, o.origin_app, "
             "i.uuid AS import_uuid")


def _detail(value_json: str | None) -> dict:
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- the kinds

def _reminders(conn: sqlite3.Connection, s: dict, today: date, ids: _Ids) -> list[dict]:
    """Open tasks and plans due within due_days, overdue first. Confirmed."""
    limit = (today + timedelta(days=s["due_days"])).isoformat()
    rows = conn.execute(
        "SELECT e.uuid, e.title, e.due_at, e.journal_date, k.name AS kind "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        "WHERE e.status = 'active' AND e.closed_at IS NULL "
        "AND k.name IN ('task', 'plan') AND e.due_at IS NOT NULL "
        f"AND substr(e.due_at, 1, 10) <= ? AND {NOT_DERIVED} "
        "ORDER BY substr(e.due_at, 1, 10), e.journal_date, e.id", (limit,)).fetchall()
    out = []
    for r in rows:
        due = date.fromisoformat(r["due_at"][:10])
        delta = (due - today).days
        eid = ids.cite("E", r["uuid"], {
            "type": "entry", "tier": "confirmed", "uuid": r["uuid"],
            "date": r["journal_date"], "kind": r["kind"], "title": r["title"],
            "due_at": r["due_at"]})
        if delta < 0:
            title = f"Overdue by {_days(-delta)}: {r['title']}"
        elif delta == 0:
            title = f"Due today: {r['title']}"
        else:
            title = f"Due in {_days(delta)} ({due.isoformat()}): {r['title']}"
        out.append({"kind": "reminder", "key": f"due:{r['uuid']}", "title": title,
                    "detail": f"{r['kind']} recorded {r['journal_date']}, due "
                              f"{due.isoformat()}",
                    "date": due.isoformat(), "ids": [eid], "tier": "confirmed",
                    "_sort": (0 if delta < 0 else 1, due.isoformat(), r["journal_date"])})
    return out


def parse_birthday(value) -> tuple[int | None, int, int] | None:
    """A contact card's birthday: 'YYYY-MM-DD' or '--MM-DD' (no year) ->
    (year or None, month, day); None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        if value.startswith("--") and len(value) == 7:
            m, d = int(value[2:4]), int(value[5:7])
            date(2000, m, d)                      # a leap year: Feb 29 is valid
            return None, m, d
        d_ = date.fromisoformat(value)
        return d_.year, d_.month, d_.day
    except ValueError:
        return None


def next_occurrence(month: int, day: int, today: date) -> date:
    """The next calendar day with this month and day, today included; a
    February 29 falls on the 28th in a year without one."""
    for year in (today.year, today.year + 1):
        try:
            occ = date(year, month, day)
        except ValueError:
            occ = date(year, month, 28)
        if occ >= today:
            return occ
    return date(today.year + 1, month, min(day, 28))


def _birthdays(conn: sqlite3.Connection, cfg: Config, s: dict, today: date,
               ids: _Ids, owners: frozenset) -> list[dict]:
    """People whose contact card carries a birthday within birthday_days;
    one nudge per person, through the card's handle link. Observed."""
    try:
        rows = conn.execute(
            f"SELECT {_OBS_COLS}, json_extract(o.value_json, '$.birthday') AS birthday, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS entity_id, "
            "CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END AS name, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observations o JOIN imports i ON i.id = o.import_id "
            "JOIN observation_participants p ON p.observation_id = o.id "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id "
            "LEFT JOIN entities m ON m.id = e.merged_into "
            "WHERE o.stream = 'contact_card' AND o.status = 'active' "
            "AND json_valid(o.value_json) "
            "AND json_extract(o.value_json, '$.birthday') IS NOT NULL "
            "AND h.ignored = 0 ORDER BY o.id, p.handle_kind, p.handle_value").fetchall()
    except sqlite3.OperationalError:
        return []                                  # pre-v4: no observed tier
    best: dict[int, tuple] = {}
    for r in rows:
        if r["etype"] != "person" or r["entity_id"] in owners:
            continue
        parsed = parse_birthday(r["birthday"])
        if parsed is None:
            continue
        year, month, day = parsed
        occ = next_occurrence(month, day, today)
        if (occ - today).days > s["birthday_days"]:
            continue
        current = best.get(r["entity_id"])
        # Prefer a card that knows the year; otherwise the first card seen.
        if current is None or (current[0] is None and year is not None):
            best[r["entity_id"]] = (year, occ, r)
    out = []
    for entity_id, (year, occ, r) in best.items():
        delta = (occ - today).days
        when = ("is today" if delta == 0 else "is tomorrow" if delta == 1
                else f"is in {_days(delta)} ({occ.isoformat()})")
        age = f", turning {occ.year - year}" if year else ""
        oid = ids.cite("O", ("obs", r["uuid"]), _obs_item(r))
        out.append({"kind": "birthday", "key": f"birthday:{entity_id}:{occ.year}",
                    "title": f"{r['name']}'s birthday {when}{age}",
                    "detail": f"from a contact card dated {r['journal_date']} {TIER_NOTE}",
                    "date": occ.isoformat(), "ids": [oid], "tier": "observed",
                    "entity_id": entity_id, "_sort": (occ.isoformat(), r["name"])})
    return out


def _nutrients(conn: sqlite3.Connection, cfg: Config, s: dict, today: date,
               ids: _Ids, notes: list[str]) -> list[dict]:
    """Nutrients below 70 % of the reference over nutrient_days, one nudge
    each, the report's disclaimer first. Observed."""
    try:
        window = nutrients.resolve_window(cfg, days=s["nutrient_days"], today=today)
        report = nutrients.build_report(conn, cfg, window)
    except DomainError as e:
        notes.append(f"nutrients: {e}")
        return []
    except sqlite3.OperationalError:
        return []
    if not report.get("low"):
        return []
    by_key = {n["key"]: n for n in report["nutrients"]}
    out = []
    for key in report["low"]:
        n = by_key.get(key)
        if n is None:
            continue
        row_ids = sorted(set(n["cites"].get("nutrition_day", []))
                         | set(n["cites"].get("food_log", [])))
        cited = _rows_by_id(conn, row_ids)
        if not cited:
            continue
        oid = ids.cite("O", ("nutrient", key), {
            "type": "observation_count", "tier": "observed",
            "what": f"food diary rows behind {n['label']}", "count": len(cited),
            "first_date": cited[0]["journal_date"], "last_date": cited[-1]["journal_date"],
            "uuids": [c["uuid"] for c in cited],
            "imports": sorted({c["import_uuid"] for c in cited})})
        ref = n["reference"]
        title = (f"{n['label']} averaged {n['average']} {n['unit']} a day over "
                 f"{_days(n['days_with_value'])}, {n['percent']}% of the "
                 f"{ref['value']} {ref['unit']} reference")
        out.append({"kind": "nutrient", "key": f"nutrient:{key}", "title": title,
                    "detail": f"{nutrients.DISCLAIMER} Food diary, {window['label']} "
                              f"{TIER_NOTE}.",
                    "date": None, "ids": [oid], "tier": "observed",
                    "_sort": (n["percent"], key)})
    return out


def _rows_by_id(conn: sqlite3.Connection, row_ids: list[int]) -> list:
    out = []
    for i in range(0, len(row_ids), 500):
        chunk = row_ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        out += conn.execute(
            f"SELECT o.uuid, o.journal_date, i.uuid AS import_uuid FROM observations o "
            f"JOIN imports i ON i.id = o.import_id WHERE o.id IN ({marks}) "
            f"ORDER BY o.journal_date, o.id", chunk).fetchall()
    return out


def _stale(conn: sqlite3.Connection, now: datetime, ids: _Ids) -> list[dict]:
    """Connectors whose newest record is older than their own
    stale_after_days — the rule doctor and the Sources page share. Observed."""
    try:
        rows, _err = api._connector_freshness(conn, now)
    except sqlite3.OperationalError:
        return []
    out = []
    for c in rows:
        if not c["stale"]:
            continue
        streams = [s_["stream"] for s_ in c["streams"]]
        marks = ",".join("?" for _ in streams)
        newest = conn.execute(
            f"SELECT {_OBS_COLS} FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
            f"ORDER BY o.started_at DESC, o.id DESC LIMIT 1", streams).fetchone()
        if newest is None:
            continue
        oid = ids.cite("O", ("obs", newest["uuid"]), _obs_item(newest))
        out.append({"kind": "stale", "key": f"stale:{c['connector']}",
                    "title": f"{c['connector']}: newest record is {c['latest'][:10]}, "
                             f"nothing newer in over {_days(c['stale_after_days'])}",
                    "detail": "the export or backup feeding it may have stopped; "
                              "`python -m lifecrm where` lists what this machine can "
                              f"import {TIER_NOTE}",
                    "date": c["latest"][:10], "ids": [oid], "tier": "observed",
                    "_sort": (c["connector"],)})
    return out


def _agenda(conn: sqlite3.Connection, cfg: Config, today: date, ids: _Ids,
            owners: frozenset, owner_handles: frozenset) -> list[dict]:
    """Today's and tomorrow's calendar events, local time, who is on them;
    cancelled ones marked. Observed, never snoozable."""
    days = (today.isoformat(), (today + timedelta(days=1)).isoformat())
    marks = ",".join("?" for _ in CALENDAR_STREAMS)
    try:
        rows = conn.execute(
            f"SELECT {_OBS_COLS} FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.stream IN ({marks}) AND o.status = 'active' "
            f"AND o.journal_date IN (?, ?) ORDER BY o.started_at, o.id",
            (*CALENDAR_STREAMS, *days)).fetchall()
    except sqlite3.OperationalError:
        return []
    parts = api._participants(conn, [r["id"] for r in rows]) if rows else {}
    out = []
    for r in rows:
        d = _detail(r["value_json"])
        all_day = bool(d.get("all_day"))
        local = api.local_time(r["started_at"], cfg.timezone, r["utc_offset_sec"])
        who = sorted({
            p["entity"] or p["display_name"]
            for p in parts.get(r["id"], [])
            if p["role"] in ("participant", "organizer")
            and (p["entity"] or p["display_name"])
            and p["entity_id"] not in owners
            and (p["kind"], (p["value"] or "").lower()) not in owner_handles})
        oid = ids.cite("O", ("obs", r["uuid"]), _obs_item(r))
        out.append({"id": oid, "date": r["journal_date"],
                    "time": None if all_day or not local else local[11:16],
                    "all_day": all_day, "title": r["title"] or "(untitled)",
                    "with": who, "location": d.get("location"),
                    "calendar": d.get("calendar"),
                    "cancelled": (d.get("status") or "").lower() == "cancelled"})
    return out


# ---------------------------------------------------------------- the pack

def build(conn: sqlite3.Connection, cfg: Config, *, today: date | str,
          now: datetime | None = None, include_hidden: bool = True) -> dict:
    """Everything the records say matters on `today`, as one pack."""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    now = now or datetime.now(timezone.utc)
    s = settings(cfg)
    ids = _Ids()
    notes: list[str] = []
    try:
        from . import owner as owner_mod
        ident = owner_mod.identity(conn, cfg)
        owners, owner_handles = ident.entities, ident.handles
    except (ConfigError, DomainError, sqlite3.Error) as e:   # a bad [owner] table
        owners, owner_handles = frozenset(), frozenset()
        notes.append(f"owner: {e}")

    found: list[dict] = []
    found += _reminders(conn, s, today, ids)
    found += _birthdays(conn, cfg, s, today, ids, owners)
    found += _nutrients(conn, cfg, s, today, ids, notes)
    found += _stale(conn, now, ids)
    agenda = _agenda(conn, cfg, today, ids, owners, owner_handles)

    try:
        states = nudge_state.state_for(conn)
    except sqlite3.OperationalError:
        states = {}
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    found.sort(key=lambda n: (rank[n["kind"]], n["_sort"]))
    shown, hidden = [], []
    for n in found:
        st = states.get(n["key"])
        state = None
        if st is not None:
            state = {"state": st["state"], "until": st["until"], "note": st["note"]}
            if st["state"] == "dismissed" or (st["until"] or "") > today.isoformat():
                hidden.append((n, state))
                continue
            state["expired"] = True
        shown.append((n, state))
    nudges_out, hidden_out = [], []
    counter = 0
    for bucket, src in ((nudges_out, shown), (hidden_out, hidden)):
        for n, state in src:
            counter += 1
            item = {k: v for k, v in n.items() if not k.startswith("_")}
            item = {"id": f"N{counter}", **item, "state": state}
            bucket.append(item)
            ids.index[item["id"]] = {"type": "nudge", **item}
    if not include_hidden:
        for h in hidden_out:
            ids.index.pop(h["id"], None)
        hidden_out = []

    pack = {"pack_version": PACK_VERSION, "today": today.isoformat(),
            "as_of": as_of_date(conn, cfg), "settings": s,
            "nudges": nudges_out, "agenda": agenda, "hidden": hidden_out,
            "notes": notes, "index": ids.index}
    pack["pack_id"] = pack_id(pack)
    return pack


def pack_id(pack: dict) -> str:
    """sha256 of the pack's content (everything but the id itself)."""
    body = {k: v for k, v in pack.items() if k != "pack_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- lines, check

def nudge_line(item: dict) -> str:
    """One durable line for an N#: its key, title and detail (so every number
    a brief copies is here), and the ids it rests on."""
    state = ""
    if item.get("state"):
        st = item["state"]
        state = (f" · {st['state']}" + (f" until {st['until']}" if st.get("until") else "")
                 + (" (expired)" if st.get("expired") else ""))
    return (f"nudge {item['kind']} · {item['key']} · {item['title']} · {item['detail']}"
            f" · rests on {', '.join(item['ids'])}{state}")


def lines(pack: dict) -> dict[str, str]:
    out = {}
    for cid, item in pack["index"].items():
        out[cid] = nudge_line(item) if item.get("type") == "nudge" else index_line(item)
    return out


def check(text: str, pack: dict) -> dict:
    """check_text over a saved pack: ids must be in its index, and every
    number or date a sentence states must appear in the lines it cites (or
    in the context every sentence may use: today's date, the disclaimer)."""
    context = [f"TODAY: {pack['today']}"]
    if any(n["kind"] == "nutrient" for n in pack["nudges"] + pack.get("hidden", [])):
        context.append(f"NUTRITION: {nutrients.DISCLAIMER}")
    return check_text(text, lines(pack), context)


def render_lines(pack: dict, *, show_hidden: bool = False) -> list[str]:
    out = [f"Nudges for {pack['today']}: {len(pack['nudges'])} shown, "
           f"{len(pack['hidden'])} hidden (snoozed or dismissed)"
           + (f"; database as of {pack['as_of']}" if pack.get("as_of") else "")]
    if not pack["nudges"] and not pack["agenda"]:
        out.append("  nothing due, no birthday near, nothing stale.")
    for n in pack["nudges"]:
        out += [f"  [{n['id']}] {n['title']}",
                f"       {n['detail']}",
                f"       key {n['key']} · rests on {', '.join(n['ids'])}"
                + (" · snooze expired" if n.get("state") else "")]
    if pack["agenda"]:
        out.append("Agenda (calendar, observed):")
        for a in pack["agenda"]:
            when = f"{a['date']} {a['time'] or 'all day':>7}"
            who = f" with {', '.join(a['with'])}" if a["with"] else ""
            out.append(f"  [{a['id']}] {when}  {a['title']}{who}"
                       + (" (cancelled)" if a["cancelled"] else ""))
    if show_hidden and pack["hidden"]:
        out.append("Hidden:")
        for n in pack["hidden"]:
            st = n["state"]
            how = st["state"] + (f" until {st['until']}" if st.get("until") else "")
            out.append(f"  [{n['id']}] {n['title']} — {how} · key {n['key']}")
    for note in pack["notes"]:
        out.append(f"note: {note}")
    out.append("Sources:")
    for cid, line in lines(pack).items():
        if not cid.startswith("N"):
            out.append(f"  {cid}: {line}")
    return out
