"""Obsidian as a main GUI — the second front end beside `lifecrm serve`.

The vault itself is export.py's read-only mirror (docs/obsidian.md): day
notes, entity notes, loops, an index, owned through a marker line and a
manifest. This module adds what makes it the place you look first:

- the start pages — Home, People, Health and Sources — built from the same
  reads as the web UI's pages (lifecrm/web/api.py), so the two front ends
  cannot disagree about who you last talked to or what last week's sleep
  was;
- where the vault lives (`[obsidian] vault`) and which front end `lifecrm
  open` opens (`[gui] main`);
- refresh after every change: the CLI notes the audit log's newest row
  before each command and re-exports the vault when a command moved it
  (`[obsidian] auto_refresh`, on once a vault is set). Every domain write
  logs one audit row, so a write through any command refreshes the vault,
  and a question logged to recall_log or a read-only command never does;
- opening Obsidian on Home through its obsidian:// link.

Still one way. Nothing here reads a note back, and the vault is rebuilt from
the database each time, so an edit made in Obsidian is overwritten by the
next refresh (docs/obsidian.md, "Read-only by design").

The start pages are dated as of the database's last change (the newest audit
row), never the clock: the same database renders the same vault on any day,
the rule the rest of the vault follows, and a refresh that finds nothing new
writes nothing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import db as db_mod
from . import export as ex
from .config import Config
from .domain import days as days_mod
from .domain.util import DomainError

GUIS = ("web", "obsidian")
HOME = "Home"
# The start pages, as the stems the vault reserves for them (no entity note
# may take one of these names: export.unique_stems escalates it).
HUB_STEMS = ("Home", "People", "Health", "Sources")
# Set by the demo builder (and usable in any script): a run of many writes
# refreshes once at the end instead of after every command.
NO_REFRESH_ENV = "LIFECRM_NO_VAULT_REFRESH"

_SPARKS = "▁▂▃▄▅▆▇█"
_HOME_PEOPLE = 8
_HOME_LOOPS = 5
_QUIET_DAYS = 30
_QUIET_SHOWN = 6
_RECENT_RUNS = 10


# ---------------------------------------------------------------- settings

def _section(cfg: Config, name: str) -> dict:
    value = (cfg.extra or {}).get(name)
    return value if isinstance(value, dict) else {}


def vault_dir(cfg: Config, override: str | None = None) -> Path | None:
    """The vault folder: --vault as given (relative to where you are, like
    export --out), else `[obsidian] vault` (relative to the project root,
    like every path in config.toml), else None."""
    if override:
        return Path(override).expanduser()
    value = _section(cfg, "obsidian").get("vault")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DomainError(f"[obsidian] vault must be a folder path in quotes, "
                          f"got {value!r}")
    return cfg.resolve(value)


def main_gui(cfg: Config, override: str | None = None) -> str:
    """'web' or 'obsidian': --gui, else `[gui] main`, else the web UI."""
    value = override or _section(cfg, "gui").get("main") or "web"
    if value not in GUIS:
        raise DomainError(f"[gui] main must be \"web\" or \"obsidian\", "
                          f"got {value!r}")
    return value


def auto_refresh(cfg: Config) -> bool:
    """True when a vault is set and `[obsidian] auto_refresh` is not false
    (and the demo builder has not switched it off for its own run)."""
    if os.environ.get(NO_REFRESH_ENV, "").strip() not in ("", "0"):
        return False
    section = _section(cfg, "obsidian")
    if not section.get("vault"):
        return False
    flag = section.get("auto_refresh", True)
    if isinstance(flag, str):
        return flag.strip().lower() not in ("false", "no", "off", "0", "")
    return bool(flag)


# ---------------------------------------------------------------- refresh

def change_stamp(cfg: Config) -> tuple | None:
    """What moves when the database changes: the newest audit row (every
    domain write logs one) and the schema version (a migration). None when
    there is no database to read. A read-only open, so it is safe beside a
    running import."""
    if not cfg.db_path.exists():
        return None
    try:
        conn = db_mod.connect(cfg.db_path, readonly=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT MAX(id), MAX(ts) FROM audit_log").fetchone()
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return (row[0], row[1], version[0] if version else None)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def refresh(cfg: Config, vault: Path, *, dry_run: bool = False) -> dict:
    """Bring the vault in line with the database: export_obsidian with this
    machine's config (timezone, rollover, [owner], [nutrition]) through a
    read-only connection."""
    if not cfg.db_path.exists():
        raise DomainError(f"database not found at {cfg.db_path} — run: "
                          f"python -m lifecrm init")
    try:
        conn = db_mod.connect(cfg.db_path, readonly=True)
    except sqlite3.Error as e:
        raise DomainError(f"cannot read {cfg.db_path} ({e})")
    try:
        return ex.export_obsidian(conn, vault, tz_name=cfg.timezone,
                                  dry_run=dry_run, cfg=cfg)
    finally:
        conn.close()


def changed(result: dict) -> int:
    return len(result["created"]) + len(result["updated"]) + len(result["deleted"])


# ---------------------------------------------------------------- opening it

def _obsidian_settings_file() -> Path | None:
    """Obsidian's own list of vaults (obsidian.json), per platform."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "obsidian" / "obsidian.json" if base else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") \
        / "obsidian" / "obsidian.json"


def _within(folder: str | Path, vault_root: str | Path) -> bool:
    """`folder` is `vault_root` or somewhere inside it (case and slashes as
    the file system sees them)."""
    def norm(p) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))
    f, root = norm(folder), norm(vault_root)
    try:
        return os.path.commonpath([f, root]) == root
    except ValueError:                   # different drives
        return False


def known_vault(vault: Path, settings: Path | None = None) -> bool | None:
    """Whether Obsidian can already open this folder: it is one of Obsidian's
    vaults, or inside one (a subfolder of an existing vault — Obsidian opens
    the most specific vault holding the note). True, False, or None when
    Obsidian's own settings file is not there to say (never run, or not
    installed). Only ever read — that file is Obsidian's, not ours."""
    settings = settings if settings is not None else _obsidian_settings_file()
    if settings is None or not settings.is_file():
        return None
    try:
        data = json.loads(settings.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict):
        return False
    return any(isinstance(v, dict) and isinstance(v.get("path"), str)
               and _within(vault, v["path"]) for v in vaults.values())


def open_url(vault: Path) -> str:
    """obsidian://open?path=<Home.md>: Obsidian opens the vault holding that
    file, at that note."""
    home = (vault / f"{HOME}.md").resolve()
    return "obsidian://open?path=" + quote(str(home), safe="")


CHOOSE_VAULT_URL = "obsidian://choose-vault"


def launch(url: str) -> None:
    """Hand an obsidian:// link to the operating system — the same as
    clicking it. No shell: the link is one argument."""
    try:
        if sys.platform == "win32":
            os.startfile(url)                      # noqa: S606 — a URL, not a path
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        done = subprocess.run([opener, url], capture_output=True, text=True)
    except OSError as e:
        raise DomainError(f"nothing on this computer opens obsidian:// links "
                          f"({e.strerror or e}) — install Obsidian from "
                          f"https://obsidian.md")
    if done.returncode != 0:
        raise DomainError(f"{opener} could not open an obsidian:// link "
                          f"({(done.stderr or done.stdout).strip() or done.returncode}) "
                          f"— install Obsidian from https://obsidian.md")


def open_vault(vault: Path, *, launcher=None, settings: Path | None = None) -> list[str]:
    """Open Obsidian on the vault's Home note, or — the first time, when
    Obsidian does not know the folder yet — on its vault manager, with the
    one step to take there. Returns the lines to print."""
    launcher = launcher or launch          # looked up now: tests replace launch
    if not (vault / f"{HOME}.md").is_file():
        raise DomainError(f"{vault} has no {HOME}.md yet — run: python -m lifecrm "
                          f"obsidian refresh")
    known = known_vault(vault, settings)
    if known is False:
        launcher(CHOOSE_VAULT_URL)
        return [f"Obsidian does not know {vault} yet, so its vault manager is open — "
                f"often behind this window, as a second Obsidian window on the "
                f"taskbar.",
                f"There choose \"Open folder as vault\" and pick {vault} (the "
                f"folder picker's address bar takes the path pasted). Once: after "
                f"that `lifecrm obsidian open` goes straight to {HOME}."]
    launcher(open_url(vault))
    lines = [f"opened {vault / (HOME + '.md')} in Obsidian"]
    if known is None:
        lines.append(f"if Obsidian says the vault is not found, choose \"Open folder "
                     f"as vault\" there and pick {vault} (once)")
    return lines


# ---------------------------------------------------------------- start pages

def _last_change(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(ts) FROM audit_log").fetchone()
    try:
        return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) if row and row[0] else None
    except ValueError:
        return None


def as_of(conn: sqlite3.Connection, cfg: Config, days: list[str]) -> tuple:
    """(journal date, UTC instant) the start pages are dated by: the
    database's last change, as a journal date in this machine's timezone and
    rollover. A database written without the audit log (a test fixture)
    falls back to its latest day."""
    stamp = _last_change(conn)
    if stamp is not None:
        return days_mod.journal_date_for(stamp, cfg.timezone,
                                         cfg.day_rollover_hour), stamp
    if days:
        last = days[-1]
        return last, datetime.fromisoformat(last).replace(
            hour=23, minute=59, tzinfo=timezone.utc)
    return None, None


def _spark(series: list[dict], start: str, n: int) -> str:
    """One character per day of the window, low to high; '·' for a day with
    no value."""
    by_day = {p["date"]: p["value"] for p in series}
    values = [v for v in by_day.values() if v is not None]
    if not values:
        return ""
    lo, hi = min(values), max(values)
    first = date.fromisoformat(start)
    out = []
    for i in range(n):
        v = by_day.get((first + timedelta(days=i)).isoformat())
        if v is None:
            out.append("·")
        elif hi == lo:
            out.append(_SPARKS[3])
        else:
            out.append(_SPARKS[round((v - lo) / (hi - lo) * (len(_SPARKS) - 1))])
    return "".join(out)


def _per_day(m: dict) -> str:
    return "per day" if m.get("per_day_agg") == "sum" else "on average"


def _metric_line(m: dict, start: str, n: int, *, observed: bool) -> str:
    unit = m["unit"]
    label = m["label"][:1].upper() + m["label"][1:]
    parts = [f"**{label}** · {ex._amount(m['mean'], unit)} {_per_day(m)} "
             f"over {m['days']} day(s)",
             f"min {ex._amount(m['min'], unit)} ({m['min_date']})",
             f"max {ex._amount(m['max'], unit)} ({m['max_date']})"]
    if m.get("trend_pct") is not None:
        parts.append(f"trend {m['trend_pct']:+.0f}%")
    spark = _spark(m.get("series") or [], start, n)
    line = " · ".join(parts) + (f" {ex._code(spark)}" if spark else "")
    if observed:
        files = m.get("source_files") or []
        shown = ", ".join(ex._md_text(str(f)) for f in files[:2]) + (
            f" +{len(files) - 2}" if len(files) > 2 else "")
        line += " — observed" + (f", from {shown}" if shown else "")
    return f"- {line}"


def _person_link(ctx: dict, p: dict) -> str:
    return ex._entity_link(ctx, p["id"], ex._label(p["name"]))


def _contact(p: dict) -> str:
    last = p.get("last_contact")
    if not last:
        return "no contact on record"
    tier = "your journal" if last["tier"] == "confirmed" else "observed"
    return f"last contact {ex._wikilink(last['date'])} ({tier})"


def _quiet(people: list[dict], as_of_date: str) -> list[dict]:
    """People last in touch 30+ days before the as-of date, longest first."""
    cutoff = (date.fromisoformat(as_of_date) - timedelta(days=_QUIET_DAYS)).isoformat()
    quiet = [p for p in people if p.get("last_contact")
             and p["last_contact"]["date"] <= cutoff]
    return sorted(quiet, key=lambda p: (p["last_contact"]["date"], p["name"].casefold()))


def _loop_line(e: dict) -> str:
    due = e.get("due_at")
    lead = ("**overdue** " if e.get("overdue") else "") + (f"**{due[:10]}** · " if due else "")
    return (f"- {lead}{e['title']} `{e['kind']}` — "
            f"{'from' if due else 'since'} {ex._wikilink(e['date'])}")


def _local(iso: str | None, tz: str) -> str | None:
    """'YYYY-MM-DD HH:MM' on this machine's clock — when an import ran."""
    from .web import api
    return api.local_time(iso, tz)


def _local_day(iso: str | None, tz: str) -> str | None:
    stamp = _local(iso, tz)
    return stamp[:10] if stamp else None


def _home_note(ctx: dict, cfg: Config, hub: dict) -> str:
    conn, as_of_date = ctx["conn"], hub["as_of"]
    lines = ex._front_matter([("tags", ["lifecrm", "home"]),
                              ("as_of", as_of_date)])
    lines += ["# Home", ""]
    if as_of_date is None:
        return ex._render(lines + ["Nothing recorded yet. Log a day with `/log`, or "
                                   "import a source (`lifecrm where` lists them); "
                                   "this page fills in on the next refresh."])
    changed_at = _local(hub["now"].strftime("%Y-%m-%dT%H:%M:%SZ"), cfg.timezone)
    lines += [f"As of **{as_of_date}**, the database's last change ({changed_at}). "
              f"Journal entries are the confirmed record; anything marked *observed* was "
              f"bulk-imported and never reviewed. Ask a question with `lifecrm ask` "
              f"(or the web UI, or voice); change anything with the CLI or `/log` — "
              f"edits made here are not read back.", "",
              " · ".join(ex._wikilink(s) for s in ("People", "Health", "Loops",
                                                     "Sources", "Index")), ""]

    days = [d for d in hub["days"] if d <= as_of_date]
    if days:
        jd = days[-1]
        weekday = ex._WEEKDAYS[date.fromisoformat(jd).weekday()]
        lines += ["## Latest day", "", f"### {ex._wikilink(jd)} · {weekday}", ""]
        row = conn.execute("SELECT summary FROM days WHERE journal_date=?",
                           (jd,)).fetchone()
        if row and row["summary"]:
            lines += [row["summary"].strip(), ""]
        props, _ = ex._day_measurements(conn, jd)
        own = [f"{k} {ex._amount(v, '/10' if k != 'sleep' else 'h')}"
               for k, v in props.items()]
        if own:
            lines += [" · ".join(own) + " — your journal", ""]
        health = ex._health_line(conn, jd)
        if health:
            lines += [health, ""]
        n = conn.execute("SELECT COUNT(*) FROM entries WHERE journal_date=? "
                         "AND status='active'", (jd,)).fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM observations WHERE journal_date=? "
                         "AND status='active'", (jd,)).fetchone()[0]
        lines += [f"{n} journal entr{'y' if n == 1 else 'ies'} · {m:,} observed "
                  f"record(s)", ""]
        earlier = list(reversed(days[-4:-1]))
        if earlier:
            lines.append("Earlier: " + " · ".join(
                f"{ex._wikilink(d)} {ex._WEEKDAYS[date.fromisoformat(d).weekday()][:3]}"
                for d in earlier))
            lines.append("")

    loops = hub["loops"]
    lines += ["## Open loops", ""]
    if loops["open"]:
        lines += [f"{len(loops['open'])} open, {loops['overdue']} overdue as of "
                  f"{as_of_date} — {ex._wikilink('Loops')}", ""]
        lines += [_loop_line(e) for e in loops["open"][:_HOME_LOOPS]]
    else:
        lines.append("No open tasks or plans.")
    lines.append("")

    people = hub["people"]["people"]
    if people:
        lines += ["## People", "", f"Most recent contact first — "
                  f"{ex._wikilink('People')}", ""]
        recent = [p for p in people if p.get("last_contact")][:_HOME_PEOPLE]
        lines += [f"- {_person_link(ctx, p)} · {_contact(p)}" for p in recent]
        quiet = _quiet(people, as_of_date)
        if quiet:
            shown = ", ".join(f"{_person_link(ctx, p)} ({p['last_contact']['date']})"
                              for p in quiet[:_QUIET_SHOWN])
            more = f" and {len(quiet) - _QUIET_SHOWN} more" if len(quiet) > _QUIET_SHOWN else ""
            lines += ["", f"Not in touch for {_QUIET_DAYS}+ days: {shown}{more}"]
        lines.append("")

    week = hub["health7"]
    shown = week["metrics"][:3] + week["journal"][:2]
    workouts = (hub["health30"].get("workouts") or {}).get("weeks") or []
    nutrition = hub["health30"].get("nutrition")
    if shown or workouts or nutrition:
        lines += [f"## Last 7 days", "",
                  f"{week['start']} … {week['end']} — {ex._wikilink('Health')}", ""]
        for m in shown:
            lines.append(_metric_line(m, week["start"], 7,
                                      observed=m.get("tier") == "observed"))
        if workouts and workouts[-1]["sessions"]:
            w = workouts[-1]
            lines.append(f"- **Logged workouts** · {w['sessions']} session(s), "
                         f"{ex._amount(w['minutes'], 'min')} in the week of "
                         f"{w['week']} — observed")
        if nutrition and not nutrition.get("error") and nutrition.get("low"):
            low = ", ".join(x["label"] for x in nutrition["low"])
            lines.append(f"- **Nutrients** (last 30 days; not medical advice) · "
                         f"below 70% of the reference intake: {low} — observed")
        lines.append("")

    src = hub["sources"]
    with_rows = [c for c in src["connectors"] if c["records"]]
    if with_rows or src["health_files"]:
        total = sum(c["records"] for c in with_rows)
        newest = max((r["started_at"] for r in src["imports"] if r["started_at"]),
                     default=None)
        lines += ["## Sources", ""]
        parts = [f"{len(with_rows)} connector(s) hold {total:,} observed record(s)"]
        if newest:
            parts.append(f"newest import {_local(newest, cfg.timezone)}")
        if src["unresolved_handles"]:
            parts.append(f"{src['unresolved_handles']} handle(s) wait to be linked")
        lines.append(" · ".join(parts) + f" — {ex._wikilink('Sources')}")
        stale = [c["connector"] for c in with_rows if c.get("stale")]
        if stale:
            lines.append(f"Gone stale: {', '.join(stale)}")
    return ex._render(lines)


def _people_note(ctx: dict, hub: dict) -> str:
    lines = ex._front_matter([("tags", ["lifecrm", "people"]),
                              ("as_of", hub["as_of"])])
    lines += ["# People", ""]
    people = hub["people"]["people"]
    if not people:
        return ex._render(lines + ["No people yet."])
    lines += [f"As of {hub['as_of']}, most recent contact first: a text on any "
              f"messaging app, an email that passed between you, a call that "
              f"connected or a calendar event with them — or a journal entry "
              f"mentioning them, whichever is newer. It is the rule `lifecrm ask` "
              f"answers \"when did I last talk to X\" with: a contact card, a "
              f"tapback, a missed call, an email they were only copied on or a "
              f"meeting still ahead is never contact. You are not on the list.", ""]
    contacted = [p for p in people if p.get("last_contact")]
    never = [p for p in people if not p.get("last_contact")]
    for p in contacted:
        extra = []
        if p["mentions"]:
            extra.append(f"{p['mentions']} journal entr{'y' if p['mentions'] == 1 else 'ies'}")
        if p["observations"]:
            extra.append(f"{p['observations']:,} observed record(s)")
        lines.append(f"- {_person_link(ctx, p)} · {_contact(p)}"
                     + (f" · {' · '.join(extra)}" if extra else ""))
    if never:
        lines += ["", "## No contact on record", ""]
        lines += [f"- {_person_link(ctx, p)}" for p in never]
    quiet = _quiet(people, hub["as_of"])
    if quiet:
        lines += ["", f"## Not in touch for {_QUIET_DAYS}+ days", "",
                  "Longest first.", ""]
        lines += [f"- {_person_link(ctx, p)} · {_contact(p)}" for p in quiet]
    return ex._render(lines)


def _nutrient_lines(n: dict) -> list[str]:
    lines = ["## Nutrients — observed", "", n["disclaimer"], ""]
    if n.get("error"):
        return lines + [f"The report could not run: {n['error']}"]
    lines += [f"{n['start']} … {n['end']}: {n['days_counted']} day(s) of the food "
              f"diary counted, against the reference intakes for {n['profile']}.", ""]
    for heading, key in (("Low", "low"), ("Borderline", "borderline"),
                         ("High", "high"), ("Over the upper limit on some days",
                                            "over_on_some_days")):
        for x in n.get(key) or []:
            avg = ex._amount(round(x["average"], 2), x["unit"])
            ref = ex._amount(x["reference"], x["unit"]) if x["reference"] is not None else "?"
            foods = ", ".join(f"{ex._md_text(s['food'])} {s['share_percent']:.0f}%"
                              for s in x["sources"][:3])
            line = (f"- **{heading}** · {x['label']}: {avg}/day, "
                    f"{x['percent']:.1f}% of the {x['reference_kind']} {ref}")
            if key == "over_on_some_days" and x.get("upper") is not None:
                line += (f" — above the {x['upper_kind'] or 'upper limit'} "
                         f"{ex._amount(x['upper'], x['unit'])} on {x['days_over']} day(s)")
            if foods:
                line += f" · most from {foods}"
            lines.append(line)
    lines.append(f"- Meets the reference: {n['meets']} nutrient(s)")
    for note in n.get("notes") or []:
        lines.append(f"- _{note}_")
    return lines


def _health_note(hub: dict) -> str:
    h = hub["health30"]
    lines = ex._front_matter([("tags", ["lifecrm", "health"]),
                              ("as_of", hub["as_of"])])
    lines += ["# Health", "",
              f"As of {hub['as_of']}: the last 30 days ({h['start']} … {h['end']}) — "
              f"the series `lifecrm ask` cites and the web UI's Health page shows. "
              f"Sensor imports are *observed* (bulk-imported, never reviewed); the "
              f"journal's own numbers are the confirmed record. When two apps "
              f"recorded the same day, a summed metric takes the one with the "
              f"larger total, never both added up.", ""]
    empty = True
    if h["metrics"]:
        empty = False
        lines += ["## Sensor imports — observed", ""]
        lines += [_metric_line(m, h["start"], 30, observed=True) for m in h["metrics"]]
        if h["missing"]:
            lines.append("- Not in this window: " + ", ".join(
                f"{m['label']} (latest {m['latest']})" for m in h["missing"]))
        lines.append("")
    if h["journal"]:
        empty = False
        lines += ["## Your journal — confirmed", ""]
        lines += [_metric_line(m, h["start"], 30, observed=False) for m in h["journal"]]
        lines.append("")
    w = h.get("workouts")
    if w:
        empty = False
        lines += ["## Logged workouts — observed", "",
                  f"Strong, Hevy and Strava sessions per Monday-to-Sunday week "
                  f"({w['sessions']} session(s), {ex._amount(w['minutes'], 'min')} "
                  f"in all):", ""]
        lines += [f"- week of {wk['week']} · {wk['sessions']} session(s), "
                  f"{ex._amount(wk['minutes'], 'min')}" for wk in w["weeks"]]
        lines.append("")
    if h.get("nutrition"):
        empty = False
        lines += _nutrient_lines(h["nutrition"]) + [""]
    if empty:
        lines.append("Nothing in the last 30 days: import Health Connect or Apple "
                     "Health (`lifecrm health import`), a workout app or a food diary.")
    return ex._render(lines)


def _sources_note(cfg: Config, hub: dict) -> str:
    s = hub["sources"]
    tz = cfg.timezone
    lines = ex._front_matter([("tags", ["lifecrm", "sources"]),
                              ("as_of", hub["as_of"])])
    lines += ["# Sources", "",
              f"As of {hub['as_of']}: where the data came from and how fresh it is "
              f"— the web UI's Sources page.", ""]
    with_rows = [c for c in s["connectors"] if c["records"] or c["runs"]]
    if with_rows:
        lines += ["## Imported — observed", ""]
        for c in with_rows:
            streams = ", ".join(f"{x['stream']} {x['count']:,}" for x in c["streams"]
                                if x["count"])
            parts = [f"**{c['connector']}** · {c['records']:,} record(s)"
                     + (f" ({streams})" if streams else "")]
            if c["latest"]:
                parts.append(f"newest record {_local_day(c['latest'], tz)}")
            if c["last_run"]:
                parts.append(f"last import {_local(c['last_run'], tz)}")
            if c.get("stale"):
                parts.append(f"**stale** (nothing in the last {c['stale_after_days']} days)")
            lines.append("- " + " · ".join(parts))
        never = [c["connector"] for c in s["connectors"]
                 if not c["records"] and not c["runs"]]
        if never:
            lines += ["", "Never imported: " + ", ".join(never)
                      + " — `lifecrm where` shows what this computer can import."]
        lines.append("")
    if s["unresolved"]:
        lines += ["## Waiting to be linked", "",
                  f"{s['unresolved_handles']} handle(s) seen in imports belong to "
                  f"no one yet. Link one with `lifecrm handles link <kind:value> "
                  f"--entity <id>` (or `--new \"<name>\"`); `lifecrm handles list "
                  f"--suggest` offers candidates.", ""]
        for h in s["unresolved"]:
            name = f" — {ex._md_text(h['display_name'])}" if h["display_name"] else ""
            lines.append(f"- {ex._code(h['kind'] + ':' + h['value'])}{name} · "
                         f"{h['observation_count']:,} record(s)")
        lines.append("")
    if s["health_files"]:
        lines += ["## Health exports — observed", ""]
        lines += [f"- {ex._code(str(f['source_file']))} · {f['records']:,} record(s), "
                  f"{f['metrics']} metric(s), {f['first_date']} … {f['last_date']}"
                  for f in s["health_files"]]
        lines.append("")
    j = s["journal"]
    lines += ["## Your journal — confirmed", ""]
    if j["entries"]:
        srcs = ", ".join(f"{x['type']} {x['count']}" for x in j["sources"])
        lines.append(f"- {j['entries']} entr{'y' if j['entries'] == 1 else 'ies'} on "
                     f"{j['days']} day(s), the newest {ex._wikilink(j['last_entry'])}"
                     + (f" · sources: {srcs}" if srcs else ""))
    else:
        lines.append("- No journal entries yet — `/log` turns a day's notes into them.")
    if j["open_drafts"]:
        lines.append(f"- {j['open_drafts']} draft(s) waiting for review "
                     f"(`lifecrm status`)")
    runs = s["imports"][:_RECENT_RUNS]
    if runs:
        lines += ["", "## Recent import runs", ""]
        for r in runs:
            account = f" · {ex._code(str(r['account']))}" if r["account"] else ""
            lines.append(f"- {_local(r['started_at'], tz)} · {r['connector']}"
                         f"{account} · {r['status']} · {r['live']:,} live row(s)")
    return ex._render(lines)


def hub_notes(ctx: dict, cfg: Config, days: list[str]) -> dict[str, str]:
    """The start pages, {vault path: text}. Built from web/api.py's reads with
    the as-of date in place of today."""
    from .web import api
    conn = ctx["conn"]
    as_of_date, now = as_of(conn, cfg, days)
    hub = {"as_of": as_of_date, "now": now, "days": days}
    if as_of_date is None:
        empty = {"people": []}
        hub.update(people=empty, loops={"open": [], "overdue": 0},
                   health7={"metrics": [], "journal": []}, health30={},
                   sources={"connectors": [], "health_files": [], "imports": [],
                            "unresolved": [], "unresolved_handles": 0})
        return {"Home.md": _home_note(ctx, cfg, hub)}
    hub["people"] = api.people(conn, cfg, as_of=as_of_date)
    hub["loops"] = api.loops(conn, cfg, as_of=as_of_date)
    hub["health7"] = api.health(conn, cfg, days=7, end=as_of_date, as_of=as_of_date)
    hub["health30"] = api.health(conn, cfg, days=30, end=as_of_date, as_of=as_of_date)
    hub["sources"] = api.imports(conn, cfg, now=now)
    return {"Home.md": _home_note(ctx, cfg, hub),
            "People.md": _people_note(ctx, hub),
            "Health.md": _health_note(hub),
            "Sources.md": _sources_note(cfg, hub)}
