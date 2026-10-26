"""The phone on this computer without a sync app (docs/android.md).

Windows 11 shows a linked Android phone in File Explorer (Link to Windows ›
*Show mobile device in File Explorer*), and what it shows is a real folder:
a cloud-files sync root under `%USERPROFILE%\\CrossDevice\\<phone>\\storage`,
the same mechanism as OneDrive's Files On-Demand. So intake and the
connectors can read the phone's own app folders — `Recordings\\Voice
Recorder`, `SMSBackupRestore`, `GPSLogger` — with nothing installed on the
phone and nothing uploaded anywhere.

Never relied on. It is one more place each reader looks: the sync-app route
(docs/android.md) works unchanged and is the only one on a Mac, with an
iPhone, or on a Windows without Link to Windows. The phone has to be on the
same Wi-Fi; one that is away reads as "not reachable", never as a failure,
and every call here has a timeout, because a cloud-files folder whose
provider is gone can hold a listing for minutes. Files are copied, never
moved: moving a file out of the mount deletes it from the phone.

Only Windows registers these roots, under
`HKLM\\...\\Explorer\\SyncRootManager\\CrossDevice!!<sid>!...\\UserSyncRoots`
(the value is `C:\\Users\\<you>\\CrossDevice\\<phone>`, DisplayNameResource
the phone's name). A folder under CrossDevice with no registry entry is a
leftover from a phone no longer linked and is never read.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import platforms
from .config import Config
from .domain.util import DomainError

SECTION = "phone"
TIMEOUT_SEC = 15             # a listing or a stat through the mount
COPY_TIMEOUT_SEC = 180       # one note copied off the phone
LEDGER = "phone-pulled.json"
# The phone apps' own folders, relative to its storage, and the inbox a
# file from each one belongs in: the recorder's notes (and Samsung's call
# recordings, held by intake like any other) to the audio inbox, a
# quick-note app's text files to the text inbox.
VOICE_FOLDERS = ("Recordings/Voice Recorder", "Recordings/Call")
TEXT_FOLDERS = ("VoiceJournal-text", "Documents/VoiceJournal-text")

_SYNC_ROOTS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager"
_top: dict[Path, list[str] | None] = {}      # one probe per phone per process


class Unreachable(DomainError):
    """The phone did not answer: away, asleep, or on another network."""


@dataclass(frozen=True)
class Mount:
    name: str
    root: Path        # ...\CrossDevice\<name>\storage — the phone's storage


# ---------------------------------------------------------------- settings

def settings(cfg: Config) -> dict:
    """[phone] in config.toml: `read = false` is the opt-out."""
    raw = (cfg.extra or {}).get(SECTION)
    raw = raw if isinstance(raw, dict) else {}
    read = raw.get("read", True)
    if not isinstance(read, bool):
        raise DomainError(f"[{SECTION}] read must be true or false, got {read!r}")
    return {"read": read}


# ---------------------------------------------------------------- finding the phone

def _sync_roots() -> list[tuple[str, Path]]:
    """(phone name, its CrossDevice folder) for every phone Windows shows in
    File Explorer, from the registry; nothing anywhere else."""
    if not platforms.is_windows():
        return []
    try:
        import winreg
    except ImportError:          # a Windows faked on another OS (the tests)
        return []
    out = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SYNC_ROOTS)
    except OSError:
        return []
    with base:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            if not sub.startswith("CrossDevice!"):
                continue
            try:
                with winreg.OpenKey(base, sub) as key:
                    name = winreg.QueryValueEx(key, "DisplayNameResource")[0]
                with winreg.OpenKey(base, sub + r"\UserSyncRoots") as key:
                    j = 0
                    while True:
                        try:
                            _sid, value, _kind = winreg.EnumValue(key, j)
                        except OSError:
                            break
                        j += 1
                        if isinstance(value, str) and value:
                            out.append((str(name), Path(value)))
            except OSError:
                continue
    return out


def mounts(cfg: Config) -> list[Mount]:
    """Every linked phone with a storage folder on this computer."""
    if not settings(cfg)["read"]:
        return []
    out = []
    for name, root in _sync_roots():
        storage = root / "storage"
        try:
            if storage.is_dir():
                out.append(Mount(name, storage))
        except OSError:
            continue
    return out


def under(cfg: Config, path: Path | str) -> Mount | None:
    """The phone a path is on, or None."""
    p = str(path).lower()
    for m in mounts(cfg):
        if p.startswith(str(m.root).lower()):
            return m
    return None


# ---------------------------------------------------------------- reading with a timeout

def _within(seconds: float, fn, *args):
    """fn's result, or Unreachable when it has not returned in `seconds`.
    The call runs in a thread that is left to finish on its own: a
    cloud-files operation cannot be cancelled, only not waited for."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args)
        except BaseException as e:          # re-raised in the caller's thread
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise Unreachable(f"no answer from the phone in {seconds:g} s — is it on the "
                          f"same Wi-Fi as this computer?")
    if "error" in box:
        raise box["error"]
    return box["value"]


def listing(path: Path, seconds: float | None = None) -> list[str]:
    """The names in a folder on the phone. Unreachable when the phone does
    not answer; a folder that is not there is an empty list."""
    try:
        return _within(seconds or TIMEOUT_SEC, os.listdir, path)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise Unreachable(f"cannot read {path.name} on the phone ({e}) — is it on the "
                          f"same Wi-Fi as this computer?") from e


def _top_names(m: Mount) -> list[str] | None:
    """The phone's top-level folders, or None when it does not answer. A
    phone that answered is listed afresh each time (cheap: Windows keeps the
    listing); one that did not is remembered for the process, so a run that
    looks in several places waits for it once, not once per place."""
    if _top.get(m.root, ()) is None:
        return None
    try:
        _top[m.root] = listing(m.root)
    except Unreachable:
        _top[m.root] = None
    return _top[m.root]


def forget() -> None:
    """Drop the probes (a long-lived process, or a test)."""
    _top.clear()


def reachable(m: Mount) -> bool:
    return _top_names(m) is not None


def folders(cfg: Config, *names: str) -> list[Path]:
    """The phone's folders called one of `names` (`a/b` for one inside
    another), on every phone that answers — one more place a connector
    looks. Nothing on a phone that is away."""
    out = []
    for m in mounts(cfg):
        top = _top_names(m)
        if top is None:
            continue
        have = {n.lower() for n in top}
        for name in names:
            if name.split("/")[0].lower() not in have:
                continue
            p = m.root / name
            try:
                if _within(TIMEOUT_SEC, p.is_dir):
                    out.append(p)
            except (Unreachable, OSError):
                continue
    return out


# ---------------------------------------------------------------- the inboxes

def _ledger_path(cfg: Config) -> Path:
    return cfg.data_dir / LEDGER


def _ledger(cfg: Config) -> dict:
    try:
        data = json.loads(_ledger_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(cfg: Config, ledger: dict) -> None:
    p = _ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _copy(src: Path, dest: Path) -> None:
    """Copy one note off the phone under a hidden name, then rename it: the
    inbox never sees a half-copied file (intake skips dotted names). Its
    modification time comes along — a recorder that names files without a
    date leaves the date to it."""
    part = dest.with_name("." + dest.name + ".part")
    _within(COPY_TIMEOUT_SEC, shutil.copy2, src, part)
    os.replace(part, dest)


def pull(cfg: Config) -> list[dict]:
    """Copy the phone's new notes into the inboxes, each file once: the
    recorder's audio into the audio inbox, a quick-note app's text files
    into the text inbox. From there intake treats them like any note (the
    content-hash check catches a file the ledger forgot). A phone that is
    away is one line in the result, never a failure; a file still being
    written (younger than intake's own threshold) waits for the next run."""
    from .transcribe import AUDIO_EXTS, MIN_FILE_AGE_SEC, TEXT_EXTS
    out: list[dict] = []
    phones = mounts(cfg)
    if not phones:
        return out
    ledger = _ledger(cfg)
    changed = False
    for m in phones:
        top = _top_names(m)
        if top is None:
            out.append({"phone": m.name, "status": "unreachable",
                        "reason": f"{m.name} did not answer — is it on the same Wi-Fi "
                                  f"as this computer?"})
            continue
        have = {n.lower() for n in top}
        seen = ledger.setdefault(m.name, {})
        for rels, exts, dest in ((VOICE_FOLDERS, AUDIO_EXTS, cfg.inbox_dir),
                                 (TEXT_FOLDERS, TEXT_EXTS, cfg.inbox_text_dir)):
            if dest is None:
                continue
            for rel in rels:
                if rel.split("/")[0].lower() not in have:
                    continue
                folder = m.root / rel
                try:
                    names = listing(folder)
                except Unreachable as e:
                    out.append({"phone": m.name, "status": "unreachable", "reason": str(e)})
                    break
                for n in sorted(names):
                    if n.startswith(".") or Path(n).suffix.lower() not in exts:
                        continue
                    src = folder / n
                    try:
                        st = _within(TIMEOUT_SEC, os.stat, src)
                    except (Unreachable, OSError):
                        continue
                    key = f"{rel}/{n}"
                    stamp = [st.st_size, int(st.st_mtime)]
                    if seen.get(key) == stamp:
                        continue
                    if time.time() - st.st_mtime < MIN_FILE_AGE_SEC:
                        continue                      # still being written
                    target = Path(dest) / n
                    if target.exists():              # pulled before, or synced
                        seen[key] = stamp
                        changed = True
                        continue
                    try:
                        Path(dest).mkdir(parents=True, exist_ok=True)
                        _copy(src, target)
                    except Unreachable as e:
                        out.append({"phone": m.name, "file": n, "status": "unreachable",
                                    "reason": str(e)})
                        break
                    except OSError as e:
                        out.append({"phone": m.name, "file": n, "status": "failed",
                                    "warnings": [f"ERROR: {n}: could not copy it off "
                                                 f"{m.name}: {e}"]})
                        continue
                    seen[key] = stamp
                    changed = True
                    out.append({"phone": m.name, "file": n, "status": "pulled",
                                "inbox": str(dest)})
    if changed:
        _save_ledger(cfg, ledger)
    return out


# ---------------------------------------------------------------- status

def status(cfg: Config) -> list[dict]:
    """Each linked phone and whether it answers right now."""
    return [{"name": m.name, "root": str(m.root), "reachable": reachable(m)}
            for m in mounts(cfg)]
