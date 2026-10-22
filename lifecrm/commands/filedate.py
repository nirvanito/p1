"""`lifecrm filedate NAME...` — the journal day and recording instant intake
would give a file, before a real note depends on it.

Phone setup is where capture goes quietly wrong. A recorder template the
parser does not read falls back to the file's modified time, which a sync
round trip can turn into the upload time, and nothing errors: the note just
lands on the wrong day. This answers "what will intake make of this name?"
for a name you are planning (no file needed) or a file already on disk. It
reads config.toml and the file's mtime; it never opens the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer


def describe(target: str, cfg) -> dict:
    """What intake would record for `target` — a bare name or a path."""
    from ..domain import days
    from ..transcribe import AUDIO_EXTS, TEXT_EXTS, filename_stamp, filename_times
    path = Path(target)
    ext = path.suffix.lower()
    out = {"name": path.name,
           "kind": "audio" if ext in AUDIO_EXTS else "text" if ext in TEXT_EXTS else None,
           "journal_date": None, "recorded_at": None,
           "date_from": None, "time_from": None, "convention": None}
    mtime = None
    if path.is_file():
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    stamp = filename_stamp(path.name)
    if stamp is not None:
        out["convention"] = (stamp.convention if cfg.filename_date == "auto"
                             else cfg.filename_date)
        out["journal_date"], out["recorded_at"] = filename_times(path.name, cfg)
        out["date_from"] = "name"
        out["time_from"] = "name" if out["recorded_at"] else None
    elif mtime is not None:
        out["journal_date"] = days.journal_date_for(
            mtime, cfg.timezone, cfg.day_rollover_hour)
        out["date_from"] = "mtime"
    if out["recorded_at"] is None and mtime is not None:
        out["recorded_at"] = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
        out["time_from"] = "mtime"
    return out


def _lines(d: dict) -> list[str]:
    from ..transcribe import AUDIO_EXTS, TEXT_EXTS
    head = f"{d['name']}: journal day {d['journal_date']}"
    if d["date_from"] is None:
        line = (f"{d['name']}: no date in the name — intake will use the file's "
                f"modified time, which a sync round trip can turn into the upload "
                f"time")
    elif d["date_from"] == "mtime":
        line = (f"{head}, recorded {d['recorded_at']} — from the file's modified "
                f"time (no date in the name)")
    elif d["time_from"] == "name":
        shape = ("journal date + clock time (iPhone Shortcut)"
                 if d["convention"] == "journal" else "calendar date + clock time")
        line = f"{head}, recorded {d['recorded_at']} — {shape} in the name"
    elif d["time_from"] == "mtime":
        line = (f"{head}, recorded {d['recorded_at']} — date in the name, time from "
                f"the file's modified time")
    else:
        line = (f"{head} — date in the name but no time; recorded time will be the "
                f"file's modified time")
    if d["kind"] is not None:
        return [line]
    return [line, f"  WARNING: intake does not read this file type — audio: "
                  f"{', '.join(sorted(AUDIO_EXTS))}; text: {', '.join(sorted(TEXT_EXTS))}"]


def register(app: typer.Typer) -> None:
    @app.command("filedate")
    def filedate(
        names: list[str] = typer.Argument(
            ..., help="File names to check — planned names or files on disk"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the journal day and recording time intake would give a file
        name — check a phone's recorder template before relying on it."""
        from ..cli import _cfg, _echo_json
        cfg = _cfg()
        results = [describe(n, cfg) for n in names]
        if as_json:
            _echo_json(results)
            return
        for d in results:
            for line in _lines(d):
                typer.echo(line)
        if cfg.filename_date != "auto":
            typer.echo(f"([time] filename_date = \"{cfg.filename_date}\": every "
                       f"name's date is read as a {cfg.filename_date} date)")
