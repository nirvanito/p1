"""Read an Android Health Connect export into health_records.

The scheduled/manual export is a zip containing Health Connect's raw SQLite
database (an unzipped .db is accepted too). Table and column names were
verified against the AOSP HealthFitness storage helpers (July 2026),
service/.../storage/datatypehelpers/:

- every record table: row_id PK, uuid BLOB, app_info_id ->
  application_info_table.row_id, last_modified_time (epoch ms)
- interval tables: start_time/end_time (epoch ms), start/end_zone_offset (sec)
- instant tables: time (epoch ms), zone_offset (sec)
- children: sleep_stages_table(parent_key, stage_start_time, stage_end_time,
  stage_type), heart_rate_record_series_table(parent_key, epoch_millis,
  beats_per_minute)
- units: weight/nutrition masses in grams; energy in small calories
  (converted to kg / kcal below). Distance meters and hydration liters are
  the stated API units; both are flagged for eyeball-verification in the
  dry-run report on first real import.

The schema is Google's and undocumented; everything here is defensive —
a table with unexpected columns is skipped WITH a reason, tables no mapper
covers are reported with row counts, and nothing is ever written outside a
--apply run. The export file itself is never modified.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db as db_mod
from .config import Config
from .domain import days, health
from .domain.util import DomainError

# Platform SleepSessionRecord stage constants; 2/4/5/6 count as asleep.
STAGE_NAMES = {0: "unknown", 1: "awake", 2: "sleeping", 3: "out_of_bed",
               4: "light", 5: "deep", 6: "rem", 7: "awake_in_bed"}
ASLEEP_STAGES = {2, 4, 5, 6}
MEAL_NAMES = {0: "unknown", 1: "breakfast", 2: "lunch", 3: "dinner", 4: "snack"}

_BASE_COLS = {"uuid", "last_modified_time"}
_INTERVAL_COLS = {"start_time", "end_time"}
_INSTANT_COLS = {"time"}


@dataclass
class Mapper:
    metric: str
    table: str
    kind: str                      # 'interval' | 'instant'
    required: tuple = ()           # value columns beyond the time/base columns
    optional: tuple = ()           # extra columns read when present


def _iso(ms) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _uuid_hex(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return str(v)


def _journal_date(ms: int, offset_sec, cfg: Config) -> str:
    """Journal date honoring the record's own zone offset when it has one —
    the same per-source-timezone principle sources follow — with the
    configured rollover (a 00:30 record belongs to 'yesterday')."""
    if offset_sec is None:
        return days.journal_date_for(
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc),
            cfg.timezone, cfg.day_rollover_hour)
    local = datetime.fromtimestamp(ms / 1000 + offset_sec, tz=timezone.utc)
    if local.hour < cfg.day_rollover_hour:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


MAPPERS = [
    Mapper("sleep_session", "sleep_session_record_table", "interval",
           optional=("title", "notes", "row_id")),
    Mapper("steps", "steps_record_table", "interval", required=("count",)),
    Mapper("weight", "weight_record_table", "instant", required=("weight",)),
    Mapper("heart_rate", "heart_rate_record_table", "interval",
           optional=("row_id",)),
    Mapper("resting_heart_rate", "resting_heart_rate_record_table", "instant",
           required=("beats_per_minute",)),
    Mapper("exercise_session", "exercise_session_record_table", "interval",
           optional=("exercise_type", "title", "notes")),
    Mapper("distance", "distance_record_table", "interval",
           required=("distance",)),
    Mapper("total_calories", "total_calories_burned_record_table", "interval",
           required=("energy",)),
    Mapper("nutrition", "nutrition_record_table", "interval",
           optional=("energy", "protein", "total_fat", "total_carbohydrate",
                     "sugar", "meal_type", "meal_name")),
    Mapper("body_fat", "body_fat_record_table", "instant",
           required=("percentage",)),
    Mapper("hydration", "hydration_record_table", "interval",
           required=("volume",)),
]

# Child tables a mapper consumes — never reported as unmapped.
_CHILD_TABLES = {"sleep_stages_table", "heart_rate_record_series_table"}


def open_export(path: Path):
    """Return (cleanup_or_None, db_path, inner_name). Zip members are
    extracted to a temp dir; a bare .db path is used in place."""
    if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        members = [m for m in zf.namelist() if m.lower().endswith(".db")]
        if not members:
            listing = ", ".join(zf.namelist()[:10]) or "(empty)"
            zf.close()
            raise DomainError(
                f"{path.name} contains no .db file (members: {listing})")
        member = max(members, key=lambda m: zf.getinfo(m).file_size)
        tmp = tempfile.TemporaryDirectory(prefix="lifecrm-hc-")
        target = Path(tmp.name) / Path(member).name
        with zf.open(member) as src, open(target, "wb") as dst:
            dst.write(src.read())
        zf.close()
        return tmp, target, Path(member).name
    return None, path, path.name


def _tables(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out = {}
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = {c["name"] for c in conn.execute(f'PRAGMA table_info("{r["name"]}")')}
        out[r["name"]] = cols
    return out


def _value_for(m: Mapper, row: sqlite3.Row, cols: set[str], ctx: dict):
    """(value, unit, detail_dict|None) or None to skip the row."""
    def has(c):
        return c in cols and row[c] is not None

    def num(c):
        return row[c] if c in cols and row[c] is not None else None

    simple = {
        "steps": ("count", "count", lambda v: float(v)),
        "weight": ("weight", "kg", lambda v: round(v / 1000.0, 3)),          # grams
        "resting_heart_rate": ("beats_per_minute", "bpm", lambda v: float(v)),
        "distance": ("distance", "km", lambda v: round(v / 1000.0, 3)),      # meters
        "total_calories": ("energy", "kcal", lambda v: round(v / 1000.0, 3)),  # cal
        "body_fat": ("percentage", "%", lambda v: round(float(v), 3)),
        "hydration": ("volume", "ml", lambda v: round(v * 1000.0, 3)),       # liters
    }
    if m.metric in simple:
        col, unit, conv = simple[m.metric]
        v = num(col)
        return None if v is None else (conv(v), unit, None)
    if m.metric == "sleep_session":
        if row["start_time"] is None or row["end_time"] is None:
            return None
        detail = {}
        stages = ctx["stages"].get(row["row_id"], []) if has("row_id") else []
        if stages:
            per: dict[str, float] = {}
            asleep_ms = 0
            for s in stages:
                name = STAGE_NAMES.get(s["stage_type"], f"stage_{s['stage_type']}")
                dur = s["stage_end_time"] - s["stage_start_time"]
                per[name] = per.get(name, 0) + dur
                if s["stage_type"] in ASLEEP_STAGES:
                    asleep_ms += dur
            detail["stages"] = {k: round(v / 60000.0, 1)
                                for k, v in sorted(per.items())}
            value = round(asleep_ms / 60000.0, 1)
        else:
            value = round((row["end_time"] - row["start_time"]) / 60000.0, 1)
        for c in ("title", "notes"):
            if has(c) and str(row[c]).strip():
                detail[c] = row[c]
        return value, "min", detail or None
    if m.metric == "heart_rate":
        agg = ctx["hr"].get(row["row_id"]) if has("row_id") else None
        if not agg:
            return None                                               # no samples
        return (round(agg["avg"], 1), "bpm",
                {"min": agg["min"], "max": agg["max"], "samples": agg["n"]})
    if m.metric == "exercise_session":
        if row["start_time"] is None or row["end_time"] is None:
            return None
        detail = {}
        if has("exercise_type"):
            detail["exercise_type"] = row["exercise_type"]
        for c in ("title", "notes"):
            if has(c) and str(row[c]).strip():
                detail[c] = row[c]
        value = round((row["end_time"] - row["start_time"]) / 60000.0, 1)
        return value, "min", detail or None
    if m.metric == "nutrition":
        value = round(row["energy"] / 1000.0, 3) if has("energy") else None
        detail = {}
        for col, key in (("protein", "protein_g"), ("total_fat", "fat_g"),
                         ("total_carbohydrate", "carbs_g"), ("sugar", "sugar_g")):
            if has(col):
                detail[key] = round(row[col], 1)                       # grams
        if has("meal_type"):
            detail["meal"] = MEAL_NAMES.get(row["meal_type"],
                                            f"meal_type_{row['meal_type']}")
        if has("meal_name") and str(row["meal_name"]).strip():
            detail["meal_name"] = row["meal_name"]
        if value is None and not detail:
            return None
        return value, ("kcal" if value is not None else None), detail or None
    return None


def read_export(path: Path, cfg: Config) -> dict:
    tmp, db_path, inner = open_export(path)
    try:
        conn = db_mod.connect(db_path, readonly=True)
        try:
            return _read(conn, cfg, source_file=path.name, inner=inner)
        finally:
            conn.close()
    finally:
        if tmp is not None:
            tmp.cleanup()


def _read(conn: sqlite3.Connection, cfg: Config, source_file: str,
          inner: str) -> dict:
    tables = _tables(conn)
    if not any(t.endswith("_record_table") for t in tables):
        raise DomainError(
            f"{inner} has no *_record_table tables — not a Health Connect "
            f"database? ({len(tables)} table(s) found)")

    apps: dict[int, str] = {}
    if "application_info_table" in tables and \
            {"row_id", "package_name"} <= tables["application_info_table"]:
        apps = {r["row_id"]: r["package_name"] for r in conn.execute(
            "SELECT row_id, package_name FROM application_info_table")}

    ctx = {"stages": {}, "hr": {}}
    if "sleep_stages_table" in tables and \
            {"parent_key", "stage_start_time", "stage_end_time",
             "stage_type"} <= tables["sleep_stages_table"]:
        for r in conn.execute("SELECT parent_key, stage_start_time, "
                              "stage_end_time, stage_type FROM sleep_stages_table"):
            ctx["stages"].setdefault(r["parent_key"], []).append(r)
    if "heart_rate_record_series_table" in tables and \
            {"parent_key", "beats_per_minute"} <= \
            tables["heart_rate_record_series_table"]:
        for r in conn.execute(
                "SELECT parent_key, COUNT(*) n, MIN(beats_per_minute) mn, "
                "AVG(beats_per_minute) av, MAX(beats_per_minute) mx "
                "FROM heart_rate_record_series_table GROUP BY parent_key"):
            ctx["hr"][r["parent_key"]] = {"n": r["n"], "min": r["mn"],
                                          "avg": r["av"], "max": r["mx"]}

    records, skipped, covered = [], [], set()
    for m in MAPPERS:
        if m.table not in tables:
            continue
        covered.add(m.table)
        cols = tables[m.table]
        time_cols = _INTERVAL_COLS if m.kind == "interval" else _INSTANT_COLS
        missing = (_BASE_COLS | time_cols | set(m.required)) - cols
        if missing:
            skipped.append(f"{m.metric} ({m.table}): missing expected "
                           f"column(s) {', '.join(sorted(missing))}")
            continue
        select = sorted((_BASE_COLS | time_cols | set(m.required) |
                         {c for c in m.optional if c in cols} |
                         {c for c in ("app_info_id", "start_zone_offset",
                                      "end_zone_offset", "zone_offset")
                          if c in cols}))
        rows = conn.execute(
            f'SELECT {", ".join(select)} FROM "{m.table}"').fetchall()
        for row in rows:
            ext_id = _uuid_hex(row["uuid"])
            if not ext_id:
                continue
            if m.kind == "interval":
                start_ms, end_ms = row["start_time"], row["end_time"]
                offset = (row["end_zone_offset"] if "end_zone_offset" in select
                          and row["end_zone_offset"] is not None
                          else (row["start_zone_offset"]
                                if "start_zone_offset" in select else None))
                anchor_ms = end_ms if end_ms is not None else start_ms
            else:
                start_ms, end_ms = row["time"], None
                offset = row["zone_offset"] if "zone_offset" in select else None
                anchor_ms = start_ms
            if start_ms is None:
                continue
            built = _value_for(m, row, cols, ctx)
            if built is None:
                continue
            value, unit, detail = built
            records.append({
                "metric": m.metric,
                "journal_date": _journal_date(anchor_ms, offset, cfg),
                "started_at": _iso(start_ms),
                "ended_at": _iso(end_ms),
                "utc_offset_sec": offset,
                "value": value,
                "unit": unit,
                "value_json": health.canonical_json(detail),
                "origin_app": apps.get(row["app_info_id"])
                              if "app_info_id" in select else None,
                "external_id": ext_id,
                "source_file": source_file,
                "last_modified_at": _iso(row["last_modified_time"]),
            })

    unmapped = sorted(
        (t, conn.execute(f'SELECT COUNT(*) c FROM "{t}"').fetchone()["c"])
        for t in tables
        if t.endswith("_record_table") and t not in covered)
    records.sort(key=lambda r: (r["metric"], r["external_id"]))
    return {"source_file": source_file, "inner_db": inner,
            "tables_seen": len(tables), "records": records,
            "unmapped": [{"table": t, "rows": c} for t, c in unmapped if c],
            "skipped": skipped}


# ---------------------------------------------------------------- plan / apply

def build_plan(conn: sqlite3.Connection, records: list[dict]) -> dict:
    existing = health.existing_by_key(conn)
    seen: set[tuple[str, str]] = set()
    new, updates, refreshed, conflicts = [], [], [], []
    identical = dup_incoming = 0
    for rec in records:
        key = (rec["metric"], rec["external_id"])
        if key in seen:
            dup_incoming += 1
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            new.append(rec)
            continue
        kind, changes = health.classify(row, rec)
        if kind == "identical":
            identical += 1
        elif kind == "refreshed":
            refreshed.append((row, rec))
        elif kind == "update":
            updates.append((row, rec, changes))
        else:
            conflicts.append({"metric": rec["metric"],
                              "external_id": rec["external_id"],
                              "changes": {f: {"existing": old, "incoming": neu}
                                          for f, (old, neu) in changes.items()}})
    return {"new": new, "updates": updates, "refreshed": refreshed,
            "conflicts": conflicts, "identical": identical,
            "dup_incoming": dup_incoming}


def apply_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    for rec in plan["new"]:
        health.insert(conn, rec)
    for row, rec, changes in plan["updates"]:
        health.update(conn, row, rec, changes)
    for row, rec in plan["refreshed"]:
        health.refresh_modified(conn, row, rec)
    return {"added": len(plan["new"]), "updated": len(plan["updates"]),
            "refreshed": len(plan["refreshed"]),
            "identical": plan["identical"],
            "conflicts": len(plan["conflicts"])}


def plan_summary(scan: dict, plan: dict) -> dict:
    per: dict[str, dict] = {}
    for rec in plan["new"]:
        per.setdefault(rec["metric"], {"new": 0, "changed": 0})["new"] += 1
    for _, rec, _ in plan["updates"]:
        per.setdefault(rec["metric"], {"new": 0, "changed": 0})["changed"] += 1
    return {"source_file": scan["source_file"], "inner_db": scan["inner_db"],
            "tables_seen": scan["tables_seen"], "per_metric": per,
            "new": len(plan["new"]), "updated": len(plan["updates"]),
            "refreshed": len(plan["refreshed"]), "identical": plan["identical"],
            "duplicates_in_export": plan["dup_incoming"],
            "conflicts": plan["conflicts"], "unmapped": scan["unmapped"],
            "skipped": scan["skipped"]}


# ---------------------------------------------------------------- report

def _sample_line(recs: list[dict]) -> str:
    r = max(recs, key=lambda x: (x["journal_date"], x["started_at"]))
    if r["metric"] == "steps":
        day = r["journal_date"]
        total = sum(x["value"] for x in recs if x["journal_date"] == day)
        return f"{day} total {total:g} steps ({sum(1 for x in recs if x['journal_date'] == day)} record(s))"
    val = f"{r['value']:g} {r['unit']}" if r["value"] is not None else "-"
    detail = ""
    if r["value_json"]:
        d = json.loads(r["value_json"])
        if "stages" in d:
            detail = " (" + ", ".join(f"{k} {v:g}" for k, v in
                                      sorted(d["stages"].items())) + ")"
        elif r["metric"] == "nutrition":
            detail = " (" + ", ".join(f"{k} {v}" for k, v in sorted(d.items())
                                      if k.endswith("_g")) + ")"
    return f"{r['journal_date']} {val}{detail}"


def report_lines(scan: dict, plan: dict) -> list[str]:
    lines = [f"health-connect export: {scan['source_file']} "
             f"(db: {scan['inner_db']}, {scan['tables_seen']} tables)"]
    by_metric: dict[str, dict] = {}
    for rec in plan["new"]:
        by_metric.setdefault(rec["metric"], {"new": [], "upd": 0})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_metric.setdefault(rec["metric"], {"new": [], "upd": 0})["upd"] += 1
    for metric in sorted(by_metric):
        info = by_metric[metric]
        recs = info["new"] or [rec for _, rec, _ in plan["updates"]
                               if rec["metric"] == metric]
        span = (f"{min(r['journal_date'] for r in recs)}"
                f"..{max(r['journal_date'] for r in recs)}") if recs else "-"
        lines.append(f"  {metric}: {len(info['new'])} new, {info['upd']} "
                     f"changed | {span} | e.g. {_sample_line(recs)}")
    if not by_metric:
        lines.append("  nothing new — export matches the database")
    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate row(s) inside the export")
    if tail:
        lines.append("  " + ", ".join(tail))
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['metric']} {c['external_id'][:12]}…: "
                     f"differs from stored record but is not newer — kept "
                     f"existing ({', '.join(sorted(c['changes']))})")
    for s in scan["skipped"]:
        lines.append(f"  skipped mapper: {s}")
    if scan["unmapped"]:
        lines.append("  unmapped tables (present, not imported): " + ", ".join(
            f"{u['table']} ({u['rows']} rows)" for u in scan["unmapped"]))
    return lines
