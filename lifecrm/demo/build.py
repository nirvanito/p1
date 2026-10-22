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

It refuses to build into the project root or anywhere holding the real
database, and only ever deletes a directory it created itself (marked with
a `.lifecrm-demo` file).
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
from datetime import date
from pathlib import Path

from ..config import PROJECT_ROOT
from . import journal, persona

MARKER = ".lifecrm-demo"

# Import order matters a little: the address book first, so handles get the
# contact-card names before texts/calls/calendar reference them.
SOURCE_ORDER = ("contacts", "android_sms", "mac_messages", "ios_calls", "whatsapp",
                "calendar", "ai_chat", "notes", "finance", "photos", "location",
                "browser", "health_connect", "apple_health")
HEALTH_SOURCES = {"health_connect", "apple_health"}


class DemoError(Exception):
    """One line, shown as-is by the CLI."""


def default_dir(combo: str) -> Path:
    return PROJECT_ROOT / "demo" / combo


def check_target(root: Path) -> Path:
    root = root.expanduser().resolve()
    real_db = (PROJECT_ROOT / "data" / "lifecrm.db").resolve()
    if root == PROJECT_ROOT.resolve() or (root / "data" / "lifecrm.db").resolve() == real_db:
        raise DemoError(f"refusing to build a demo in {root}: that is the project's "
                        f"own data root")
    if root.exists() and any(root.iterdir()) and not (root / MARKER).exists():
        raise DemoError(f"{root} exists and is not a demo directory (no {MARKER}); "
                        f"pick an empty or new folder")
    return root


def available_sources() -> list[str]:
    from . import sources as pkg
    names = {m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_")}
    ordered = [n for n in SOURCE_ORDER if n in names]
    return ordered + sorted(names - set(ordered))


def _config_text(root: Path, combo: str) -> str:
    sync = root / ("iCloud Drive" if combo == "iphone" else "OneDrive") / "VoiceJournal"
    me = persona.OWNER
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
        f"onedrive_backups_dir = '{sync / 'backups'}'",
        "",
        "[time]",
        f'timezone = "{persona.TIMEZONE}"',
        "day_rollover_hour = 5",
        "",
        "[whisper]",
        'model = "small"',
        'device = "cpu"',
        'compute_type = "int8"',
        'language = "en"',
        "",
        "[ask]",
        "enabled = true",
        'endpoint = "http://localhost:11434"',
        'model = "llama3.1:8b"',
        "timeout_sec = 120",
        "",
        "[connectors.android_sms]",
        f'account = "{me.phone}"',
        "[connectors.whatsapp]",
        f'me = "{me.name}"',
        "[connectors.calendar]",
        f'account = "{me.email}"',
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
        self.anchor = anchor or date.today()
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
        """Run one CLI command as its own process, exactly as typed."""
        env = dict(os.environ, PYTHONUTF8="1",
                   HF_HUB_OFFLINE=os.environ.get("HF_HUB_OFFLINE", "1"))
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
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / MARKER).write_text(
            f"lifecrm demo, combo={self.combo}, anchor={self.anchor.isoformat()}\n",
            encoding="utf-8")
        (self.root / "config.toml").write_text(_config_text(self.root, self.combo),
                                               encoding="utf-8")
        sync = self.root / ("iCloud Drive" if self.combo == "iphone" else "OneDrive") / "VoiceJournal"
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
                made[name] = mod.write_sample(dest, self.anchor, self.combo)
            except ValueError as e:
                self.skipped.append(f"{name}: not on this combo ({e})")
                shutil.rmtree(dest, ignore_errors=True)
        return made

    def imports(self, made: dict[str, Path]) -> dict:
        self.echo("imports: dry-run report, then --apply, per source")
        from ..connectors import base as conn_base
        connectors = set(conn_base.registry())
        done = {}
        for name, path in made.items():
            if name in HEALTH_SOURCES:
                self._run("health", "import", str(path))
                self._run("health", "import", str(path), "--apply")
                done[name] = "health"
            elif name in connectors:
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
        me = persona.OWNER
        owner_id = None
        linked = unresolved = 0
        for r in rows:
            if r.get("entity_id") or r.get("ignored"):
                continue
            kind, value = r["kind"], r["value"]
            display = (r.get("display_name") or "").strip()
            target_key = None
            if kind == "phone":
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
                if low in (me.name.lower(), me.first.lower()) or low.startswith("you"):
                    target_key = "me"
                else:
                    target_key = names.get(low)
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
