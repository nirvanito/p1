"""Build a demo instance by running the real pipeline over synthetic exports.

Every step is the same `python -m lifecrm ...` command a person would type,
run as a subprocess against a throwaway root (`--root <demo dir>`), so the
demo exercises exactly the code real data goes through: intake, ingest,
apply, health import, observe, handles link, backup, doctor. The only parts
played by the demo itself are the ones a person plays in real use:

- the extractor (/log in Claude Code): drafts come from lifecrm/demo/journal.py;
- the reviewer: the demo accepts its own diffs' decision items. That is
  only acceptable because every record here is synthetic — real diffs are
  never applied unreviewed;
- the person who says "this phone number is Jake": handle links come from
  the persona, printed one by one.

It refuses to build into the project root, into or around the real data,
backups and journal folders, or anywhere holding the real database — under
any spelling of them, since folders are compared by file identity — and only
ever deletes a directory it created itself (marked with a `.lifecrm-demo`
file) — checked again right before the delete.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import PROJECT_ROOT, ConfigError, load_config
from ..domain.days import journal_date_for
from . import journal, persona

MARKER = ".lifecrm-demo"
# The demo's [time] day_rollover_hour. Its default "today" uses the same
# hour, so a build at 01:00 is anchored on the journal day the demo's own
# ask and status call today — not on a calendar date they see as tomorrow.
ROLLOVER_HOUR = 5

# Import order matters a little: the address book first, so handles get the
# contact-card names before texts/calls/calendar reference them.
SOURCE_ORDER = ("contacts", "android_sms", "mac_messages", "ios_messages", "ios_calls",
                "whatsapp", "calendar", "ai_chat", "notes", "finance", "photos",
                "location", "browser", "health_connect", "apple_health")
HEALTH_SOURCES = {"health_connect", "apple_health"}


class DemoError(Exception):
    """One line, shown as-is by the CLI."""


def default_dir(combo: str) -> Path:
    return PROJECT_ROOT / "demo" / combo


def default_anchor(now: datetime | None = None) -> date:
    """The demo's 'today': the journal date in the persona's timezone with
    the demo's rollover, which is what its ask, status and review use."""
    now = now or datetime.now(timezone.utc)
    return date.fromisoformat(journal_date_for(now, persona.TIMEZONE, ROLLOVER_HOUR))


def _resolved(p: Path) -> Path | None:
    try:
        return p.expanduser().resolve()
    except (OSError, ValueError):
        return None


def _protected() -> tuple[list[Path], list[Path]]:
    """The real project's own folders and database files, resolved (so a
    symlink or junction to one is caught too): data/ (which holds the real
    lifecrm.db) and backups/, and — when there is a real config.toml — every
    folder it names, the journal inboxes included, and the database in its
    data folder."""
    folders = [PROJECT_ROOT / "data", PROJECT_ROOT / "backups"]
    dbs = [PROJECT_ROOT / "data" / "lifecrm.db"]
    if (PROJECT_ROOT / "config.toml").exists():
        try:
            cfg = load_config(PROJECT_ROOT)
        except ConfigError:
            cfg = None
        if cfg is not None:
            folders += [p for p in (cfg.data_dir, cfg.backups_dir, cfg.inbox_dir,
                                    cfg.inbox_text_dir, cfg.archive_dir,
                                    cfg.cloud_backups_dir) if p is not None]
            dbs.append(cfg.db_path)
    return ([p for p in map(_resolved, folders) if p is not None],
            [p for p in map(_resolved, dbs) if p is not None])


def _fold(name: str) -> str:
    """One path name the way the file system compares it: Windows (and a
    default macOS volume) ignore case, and Windows drops trailing dots and
    spaces."""
    if os.name == "nt":
        return name.rstrip(" .").casefold()
    return name.casefold() if sys.platform == "darwin" else name


def _anchors(path: Path) -> list[tuple[tuple[int, int], tuple[str, ...]]]:
    r"""Each existing folder on the way to `path` (itself included) as its
    file identity, plus the names below it that lead on to `path`. Every
    spelling of one folder — C:\..., \\?\C:\..., \\?\Volume{...}\...,
    \\localhost\C$\..., a junction — has the one identity, even where no two
    of their texts compare equal (Path.resolve() keeps those prefixes)."""
    out = []
    for p in (path, *path.parents):
        try:
            st = os.stat(p)
        except (OSError, ValueError):
            continue
        if st.st_ino == 0:               # a file system with no file ids
            continue
        out.append(((st.st_dev, st.st_ino),
                    tuple(_fold(n) for n in path.parts[len(p.parts):])))
    return out


def _relation(target: list, guarded: list) -> str | None:
    """How `target` stands to `guarded` (both from _anchors): 'is',
    'inside' or 'holds' — decided below the deepest folder they share by
    identity, not by comparing their text."""
    for ident, below_t in target:
        for gident, below_g in guarded:
            if ident != gident:
                continue
            if below_t == below_g:
                return "is"
            if below_t[:len(below_g)] == below_g:
                return "inside"
            if below_g[:len(below_t)] == below_t:
                return "holds"
    return None


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def check_dir_option(text: str) -> None:
    r"""--dir as typed. A \\?\ device path or a \\server\share path is
    refused outright: those are the spellings that reach a folder under some
    other name (\\?\C:\..., \\?\Volume{...}\..., \\localhost\C$\...), and a
    demo folder never needs one. (check_target compares by file identity
    regardless; this only says so before anything else happens.)"""
    typed = text.replace("/", "\\") if os.name == "nt" else text
    if typed.startswith("\\\\"):
        raise DemoError(r"--dir must be a plain folder path (a drive letter, ~ or "
                        r"relative), not a \\?\ device or \\server\share path: " + text)


def check_target(root: Path) -> Path:
    """Refuse a demo root that is the project root or a folder above it;
    that is, is inside or holds one of the real project's folders; or whose
    data/ is the real database's folder. Paths are compared by file
    identity as well as by resolved text, so no other spelling of a
    protected folder gets past."""
    root = root.expanduser().resolve()
    folders, dbs = _protected()
    target = _anchors(root)
    project = _resolved(PROJECT_ROOT) or PROJECT_ROOT
    # The project root (a folder inside it, demo/<combo>/, is the default),
    # or a root whose data/ is the real one — a junction named data
    # included: the demo's init and apply would write the real database.
    own_db = root / "data" / "lifecrm.db"
    own_data = _anchors(own_db.parent)
    if root == project or _relation(target, _anchors(project)) == "is" \
            or any(_resolved(own_db) == db or _same_file(own_db, db)
                   or _relation(own_data, _anchors(db.parent)) == "is" for db in dbs):
        raise DemoError(f"refusing to build a demo in {root}: that is the project's "
                        f"own data root")
    for p in folders:
        # Inside one (a demo in data\staging would drop its files among real
        # drafts, and a later --force would delete them all), the folder
        # itself, or a folder that holds one — the project root and every
        # folder above it hold data/.
        if root == p or p in root.parents or root in p.parents \
                or _relation(target, _anchors(p)) is not None:
            raise DemoError(f"refusing to build a demo in {root}: it is or holds "
                            f"the project's own {p} — pick a folder outside it")
    if root.exists() and any(root.iterdir()) and not (root / MARKER).exists():
        raise DemoError(f"{root} exists and is not a demo directory (no {MARKER}); "
                        f"pick an empty or new folder")
    return root


def available_sources() -> list[str]:
    from . import sources as pkg
    names = {m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_")}
    ordered = [n for n in SOURCE_ORDER if n in names]
    return ordered + sorted(names - set(ordered))


def _owner_age(anchor: date) -> int | None:
    """The persona's age on the demo's 'today', from their birthday."""
    me = persona.OWNER
    if me.birth_year is None:
        return None
    before_birthday = me.birthday is not None and (anchor.month, anchor.day) < me.birthday
    return anchor.year - me.birth_year - (1 if before_birthday else 0)


def _sync_root(root: Path, combo: str) -> Path:
    """The demo's journal folder: iCloud Drive with a Mac, OneDrive with a
    Windows laptop (the iPhone's Shortcuts save there through the OneDrive
    app, docs/windows-iphone.md)."""
    return root / ("iCloud Drive" if persona.on_mac(combo) else "OneDrive") / "VoiceJournal"


def _config_text(root: Path, combo: str, anchor: date | None = None) -> str:
    sync = _sync_root(root, combo)
    me = persona.OWNER
    age = _owner_age(anchor or default_anchor())
    lines = [
        f"# Demo instance ({combo}: {persona.DEVICE[combo]['computer']} + "
        f"{persona.DEVICE[combo]['phone']}). Synthetic data only — safe to delete.",
        "[paths]",
        'data_dir = "data"',
        'backups_dir = "backups"',
        f"inbox_dir = '{sync / 'inbox'}'",
        f"inbox_text_dir = '{sync / 'inbox-text'}'",
        f"archive_dir = '{sync / 'archive'}'",
        f"cloud_backups_dir = '{sync / 'backups'}'",
        "",
        "[time]",
        f'timezone = "{persona.TIMEZONE}"',
        f"day_rollover_hour = {ROLLOVER_HOUR}",
        "",
        # Who the owner is: the persona's own name and identifiers, in the
        # kind:value shape `handles link` takes.
        "[owner]",
        f'name = "{me.name}"',
        f'handles = ["email:{me.email}", "phone:{me.phone}"]',
        "",
        "[whisper]",
        'model = "small"',
        'device = "cpu"',
        'compute_type = "int8"',
        'language = "en"',
        "",
        # Answers are deterministic and cited; prose comes from /ask in
        # Claude Code. The optional local model stays off (docs/ask.md).
        "[ask]",
        "enabled = false",
        "",
        # Whose reference intakes `lifecrm nutrients` compares against: Alex
        # is a man (his mother calls him "mijo"), born in persona.OWNER's year.
        "[nutrition]",
        'sex = "male"',
        *([f"age = {age}"] if age is not None else []),
        "",
        # The Obsidian vault, inside the demo root: `obsidian open` and
        # `open --gui obsidian` find it, and every write refreshes it.
        "[obsidian]",
        'vault = "vault"',
        "",
        # The reminders calendar, inside the demo root too: `reminders
        # publish` writes it there, and every write republishes it.
        "[assistant]",
        'reminders_dir = "reminders"',
        "",
        "[connectors.android_sms]",
        f'account = "{me.phone}"',
        "[connectors.whatsapp]",
        f'me = "{me.name}"',
        "[connectors.calendar]",
        f'account = "{me.email}"',
        # The iPhone sample is one calendar named "Home"; a name alone could be
        # shared by two calendars, so mirroring it is the owner's say-so.
        'mirror_calendars = ["Home"]',
        "[connectors.contacts]",
        f'account = "{me.email}"',
        "",
    ]
    return "\n".join(lines)


class DemoBuilder:
    def __init__(self, root: Path, combo: str, anchor: date | None = None, *,
                 echo=print, python: str | None = None, with_audio: bool = False,
                 ask: bool = True, force: bool = False, runner=None):
        if combo not in persona.COMBOS:
            raise DemoError(f"combo must be one of {', '.join(persona.COMBOS)}")
        self.root = check_target(root)
        self.combo = combo
        self.anchor = anchor or default_anchor()
        self.echo = echo
        self.python = python or sys.executable
        self.with_audio = with_audio
        self.do_ask = ask
        self.force = force
        self.runner = runner or self._subprocess
        self.log: list[dict] = []
        self.skipped: list[str] = []

    # ------------------------------------------------------------ plumbing

    def _subprocess(self, args: list[str]) -> tuple[int, str, str]:
        """Run one CLI command as its own process, exactly as typed. The
        vault is refreshed once at the end (finish), not after each of the
        build's hundred-odd writes."""
        from ..obsidian import NO_REFRESH_ENV
        env = dict(os.environ, PYTHONUTF8="1",
                   HF_HUB_OFFLINE=os.environ.get("HF_HUB_OFFLINE", "1"),
                   **{NO_REFRESH_ENV: "1"})
        env.pop("LIFECRM_ROOT", None)
        p = subprocess.run([self.python, "-m", "lifecrm", *args], cwd=PROJECT_ROOT,
                           env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return p.returncode, p.stdout, p.stderr

    def _run(self, *args: str, want_json: bool = False, check: bool = True):
        shown = "lifecrm " + " ".join(a if " " not in a else f'"{a}"' for a in args)
        self.echo(f"  $ {shown}")
        code, out, err = self.runner(["--root", str(self.root), *args])
        self.log.append({"cmd": shown, "exit": code})
        if check and code != 0:
            tail = (err or out).strip().splitlines()[-6:]
            raise DemoError(f"`{shown}` failed (exit {code}): " + " | ".join(tail))
        if want_json:
            try:
                return json.loads(out)
            except json.JSONDecodeError as e:
                raise DemoError(f"`{shown}` did not print JSON: {out[:200]!r}") from e
        return out

    def _sql(self, query: str) -> list[dict]:
        return self._run("sql", query, "--json", want_json=True)

    # ------------------------------------------------------------ steps

    def prepare(self) -> None:
        if self.root.exists() and (self.root / MARKER).exists():
            if not self.force:
                raise DemoError(f"{self.root} already holds a demo; pass --force to rebuild it")
            check_target(self.root)          # again, at the moment it matters
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MARKER).write_text(
            f"lifecrm demo, combo={self.combo}, anchor={self.anchor.isoformat()}\n",
            encoding="utf-8")
        (self.root / "config.toml").write_text(
            _config_text(self.root, self.combo, self.anchor), encoding="utf-8")
        sync = _sync_root(self.root, self.combo)
        for sub in ("inbox", "inbox-text", "archive", "backups"):
            (sync / sub).mkdir(parents=True, exist_ok=True)
        self.sync = sync
        self.echo(f"demo root: {self.root}")
        self._run("init")

    def _write_notes(self, notes: list[dict]) -> None:
        inbox = self.sync / "inbox-text"
        old = time.time() - 3600                 # intake ignores files younger than 60 s
        for n in notes:
            p = inbox / n["filename"]
            p.write_text(n["text"] + "\n", encoding="utf-8")
            os.utime(p, (old, old))

    def journal(self) -> dict:
        self.echo("journal: phone notes -> intake -> draft -> ingest -> apply")
        notes = journal.note_files(self.anchor, self.combo)
        self._write_notes(notes)
        results = self._run("intake", "--json", want_json=True)
        by_file = {Path(r["file"]).name: r for r in results if isinstance(r, dict)}
        known: dict[str, int] = {}
        keyed: dict[str, str] = {}
        applied = 0
        for jday in persona.JOURNAL:
            if jday.pending:
                continue
            names = [journal.note_filename(self.anchor, jday.offset, n, self.combo)
                     for n in jday.notes]
            try:
                source_ids = [by_file[n]["source_id"] for n in names]
            except KeyError as e:
                raise DemoError(f"intake did not import {e}") from e
            draft, keys = journal.build_draft(jday, self.anchor, source_ids, known, keyed)
            date_s = draft["journal_date"]
            path = self.root / "data" / "staging" / f"draft-{date_s}.json"
            path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
            self._run("ingest", str(path))
            diffs = sorted((self.root / "data" / "staging").glob(f"diff-{date_s}-*.json"))
            if not diffs:
                raise DemoError(f"ingest wrote no diff for {date_s}")
            diff_path = diffs[-1]
            diff = json.loads(diff_path.read_text(encoding="utf-8-sig"))
            for item in diff["items"]:
                if item.get("accepted") is None and item["action"] == "entry_update":
                    item["accepted"] = True          # the demo's stand-in for "y"
            diff_path.write_text(json.dumps(diff, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
            self._run("apply", str(diff_path), "--json", want_json=True)
            applied += 1
            # Learn what this day created, for later days' refs: two reads.
            ents = {(r["type"], r["name"].lower()): r["id"] for r in self._sql(
                "SELECT id, type, name FROM entities WHERE status='active'")}
            for ref in journal.entity_keys(jday):
                spec = journal.entity_spec(ref)
                hit = ents.get((spec["type"], spec["name"].lower()))
                if hit is not None:
                    known.setdefault(ref, hit)
            titles = {r["title"]: r["uuid"] for r in self._sql(
                "SELECT uuid, title FROM entries WHERE status='active' "
                "AND journal_date='" + date_s + "'")}
            for entry, key in zip(draft["entries"], keys):
                if key and entry["title"] in titles:
                    keyed[key] = titles[entry["title"]]
        self.entities = known
        return {"days": applied, "entities": len(known)}

    def exports(self) -> dict:
        self.echo("exports: synthetic files in each app's real export format")
        out_dir = self.root / "exports"
        made: dict[str, Path] = {}
        for name in available_sources():
            try:
                mod = importlib.import_module(f"lifecrm.demo.sources.{name}")
            except Exception as e:                       # a generator that does not import
                self.skipped.append(f"{name}: generator failed to import ({e})")
                continue
            dest = out_dir / name
            dest.mkdir(parents=True, exist_ok=True)
            try:
                # A generator for a source with several exports (Claude AND
                # ChatGPT) offers write_samples(); each one is imported.
                many = getattr(mod, "write_samples", None)
                made[name] = (list(many(dest, self.anchor, self.combo)) if many
                              else mod.write_sample(dest, self.anchor, self.combo))
            except ValueError as e:
                self.skipped.append(f"{name}: not on this combo ({e})")
                shutil.rmtree(dest, ignore_errors=True)
        return made

    def imports(self, made: dict[str, Path]) -> dict:
        self.echo("imports: dry-run report, then --apply, per source")
        from ..connectors import base as conn_base
        connectors = set(conn_base.registry())
        done = {}
        for name, made_paths in made.items():
            paths = made_paths if isinstance(made_paths, list) else [made_paths]
            if name in HEALTH_SOURCES:
                for path in paths:
                    self._run("health", "import", str(path))
                    self._run("health", "import", str(path), "--apply")
                done[name] = "health"
            elif name in connectors:
                for path in paths:
                    self._run("observe", name, str(path))
                    self._run("observe", name, str(path), "--apply")
                done[name] = "observed"
            else:
                self.skipped.append(f"{name}: sample written but no connector named "
                                    f"'{name}' to import it")
        return done

    def link_handles(self) -> dict:
        """The person's one-time decisions: which identifier is whom."""
        self.echo("handles: linking identifiers to people (one decision each)")
        rows = self._run("handles", "list", "--json", "--limit", "5000", want_json=True)
        if isinstance(rows, dict):
            rows = rows.get("handles", [])
        by_phone = {p.phone: p.key for p in persona.PEOPLE if p.phone}
        by_email = {p.email.lower(): p.key for p in persona.PEOPLE if p.email}
        names: dict[str, str] = {}
        for p in persona.PEOPLE:
            for n in (p.name, p.first, *p.aliases):
                names.setdefault(n.lower(), p.key)
        for pl in persona.PLACES:
            for n in (pl.name, *pl.aliases):
                names.setdefault(n.lower(), pl.key)
        # Place ids the location sample uses (Google-shaped, one per place).
        try:
            from .sources.location import PLACE_IDS
            by_place_id = {v: k for k, v in PLACE_IDS.items()}
        except ImportError:
            by_place_id = {}
        me = persona.OWNER
        owner_id = None
        linked = unresolved = 0
        for r in rows:
            if r.get("entity_id") or r.get("ignored"):
                continue
            kind, value = r["kind"], r["value"]
            display = (r.get("display_name") or "").strip()
            target_key = None
            if kind == "card":
                target_key = "me"                     # the owner's own card
            elif kind == "place_id" and value in by_place_id:
                target_key = by_place_id[value]
            elif kind == "phone":
                if value == me.phone:
                    target_key = "me"
                else:
                    target_key = by_phone.get(value)
            elif kind == "email":
                if value.lower() == me.email.lower():
                    target_key = "me"
                else:
                    target_key = by_email.get(value.lower())
            if target_key is None and display:
                low = display.lower()
                if low in (me.name.lower(), me.first.lower()) or low.startswith("you") \
                        or low.endswith(" account"):     # 'Claude account': the owner's
                    target_key = "me"
                else:
                    # A calendar location reads "Uchi, 801 S Lamar Blvd, ...":
                    # the place is the part before the address.
                    target_key = names.get(low) or names.get(low.split(",")[0].strip())
            if target_key is None:
                unresolved += 1
                continue
            if target_key == "me":
                if owner_id is None:
                    self._run("handles", "link", f"{kind}:{value}", "--new", me.name)
                    owner_id = self._sql(
                        "SELECT id FROM entities WHERE status='active' AND name='"
                        + me.name + "'")[0]["id"]
                else:
                    self._run("handles", "link", f"{kind}:{value}", "--entity", str(owner_id))
                linked += 1
                continue
            eid = self.entities.get(target_key)
            if eid is None:
                unresolved += 1
                continue
            self._run("handles", "link", f"{kind}:{value}", "--entity", str(eid))
            linked += 1
        return {"linked": linked, "left_for_review": unresolved}

    def pending(self) -> None:
        """Today's note stays in the inbox: `status` and `/log` have work to show."""
        self._write_notes(journal.note_files(self.anchor, self.combo, pending=True))
        if self.with_audio:
            from . import speech
            target = self.sync / "inbox" / journal.note_filename(
                self.anchor, 0, persona.Note("21:05:00", "evening", []), self.combo,
                ext=".wav")
            text = ("Evening note. Climbed with Sam at the new gym after work, about an "
                    "hour. Dinner was leftover turkey chili. Booked the flights to "
                    "Ljubljana for June.")
            speech.synthesize(text, target)
            old = time.time() - 3600
            os.utime(target, (old, old))
            self.echo("  audio note synthesized; transcribing with local whisper")
            self._run("intake")

    def finish(self) -> dict:
        # A read-only Obsidian vault of the same data, start pages included —
        # the demo config's [obsidian] vault, to open in Obsidian.
        self._run("obsidian", "refresh")
        # The reminders calendar (docs/assistant.md), published once here
        # like the vault; the hooks were off for the build.
        self._run("reminders", "publish", "--if-configured")
        self._run("backup")
        doctor = self._run("doctor", "--json", want_json=True, check=False)
        status = self._run("status", "--json", want_json=True)
        answers = []
        if self.do_ask:
            self.echo("asking the demo questions (deterministic, cited)")
            for q in persona.QUESTIONS:
                res = self._run("ask", q, "--json", "--no-llm", want_json=True, check=False)
                if isinstance(res, dict):
                    answers.append({"question": q, "answer": res.get("answer"),
                                    "cited": res.get("citations", {}).get("cited", [])})
        return {"doctor": doctor, "status": status, "answers": answers}

    def run(self) -> dict:
        self.prepare()
        j = self.journal()
        made = self.exports()
        imported = self.imports(made)
        handles = self.link_handles()
        self.pending()
        tail = self.finish()
        summary = {"root": str(self.root), "combo": self.combo,
                   "anchor": self.anchor.isoformat(), "journal": j,
                   "imported": imported, "handles": handles,
                   "skipped": self.skipped, **tail}
        (self.root / "demo-summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return summary
