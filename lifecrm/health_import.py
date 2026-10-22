"""Which health importer reads a file, and where to look when none is named.

Two phones, two export shapes: Android's Health Connect writes a zip around
its SQLite database; the iPhone's Health app writes export.zip around an XML
file — and a Mac's Safari often unzips that into an `apple_health_export`
folder on the way. `health import` takes any of them and picks the reader
from the file itself, so the owner never has to say which phone it came from.

With no file named, the usual landing spots are searched: Downloads (where
AirDrop and browser downloads go), iCloud Drive (Files -> Save to iCloud
Drive on the iPhone) and OneDrive (the Windows + Android combo's sync
folder). The newest export found is used, and the search is reported either
way — finding nothing names every folder that was looked in.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from . import apple_health, health_connect, platforms
from .domain.util import DomainError

APPLE = "apple_health"
HEALTH_CONNECT = "health_connect"

# File and folder names each phone's export arrives under. A second download
# of the same name gets ' (1)' from browsers, hence the globs.
EXPORT_PATTERNS = ("export*.zip", "apple_health_export*", "Health Connect*.zip")


def detect_format(path: Path) -> str:
    """APPLE or HEALTH_CONNECT, decided by content rather than by file name
    (every Apple export is export.zip; Health Connect's name varies)."""
    if path.is_dir():
        if (path / "export.xml").exists() or (path / "apple_health_export").is_dir() \
                or any(path.glob("*.xml")):
            return APPLE
        raise DomainError(f"folder {path.name} holds no Apple Health export.xml "
                          f"(Health Connect exports are zip or .db files)")
    if path.suffix.lower() == ".xml":
        return APPLE
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        if any(n.lower().endswith(".xml") and not n.startswith("__MACOSX/")
               for n in names) and not any(n.lower().endswith(".db") for n in names):
            return APPLE
    return HEALTH_CONNECT


def reader_for(path: Path):
    """The importer module for `path`: read_export / build_plan / apply_plan /
    plan_summary / report_lines all have the same shape in both."""
    return apple_health if detect_format(path) == APPLE else health_connect


# ---------------------------------------------------------------- finding

def search_dirs() -> list[Path]:
    """Where a phone's health export usually lands on this machine, in order."""
    home = platforms.home()
    out = [home / "Downloads", platforms.icloud_drive(),
           home / "iCloudDrive"]                  # iCloud for Windows
    out.extend(platforms.onedrive_candidates())
    seen, uniq = set(), []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _is_export(p: Path) -> bool:
    try:
        if p.is_dir():
            return (p / "export.xml").exists()
        if not zipfile.is_zipfile(p):
            return False
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
    except OSError:
        return False
    if any(n.lower().endswith(".db") for n in names):
        return True                                       # Health Connect
    return any(n.rsplit("/", 1)[-1].lower() == "export.xml" for n in names)


def find_exports(dirs: list[Path] | None = None) -> dict:
    """{found: [paths, newest first], searched: [every dir, in order],
    looked: [the ones that exist], placeholders: [iCloud stubs]}.

    A candidate counts only if its contents look like a health export, so an
    unrelated export.zip in Downloads is passed over. An iCloud stub
    (.export.zip.icloud) means the file is in iCloud Drive but not on this
    Mac yet — reported so the owner knows to download it, never opened.
    """
    searched = list(dirs if dirs is not None else search_dirs())
    found, looked, stubs = [], [], []
    for d in searched:
        if not d.is_dir():
            continue
        looked.append(d)
        for pattern in EXPORT_PATTERNS:
            for p in sorted(d.glob(pattern)):
                if _is_export(p) and p not in found:
                    found.append(p)
            for p in sorted(d.glob("." + pattern + ".icloud")):
                if platforms.is_icloud_placeholder(p):
                    stubs.append(p)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {"found": found, "searched": searched, "looked": looked,
            "placeholders": stubs}


def not_found_message(result: dict) -> str:
    where = ", ".join(str(d) if d in result["looked"] else f"{d} (absent)"
                      for d in result["searched"])
    msg = (f"no health export given and none found — looked for "
           f"{', '.join(EXPORT_PATTERNS)} in: {where}")
    if result["placeholders"]:
        msg += (" — in iCloud Drive but not downloaded yet: "
                + ", ".join(platforms.icloud_placeholder_target(p)
                            for p in result["placeholders"])
                + " (open it in Finder to download, then re-run)")
    return msg + ". Name the file: python -m lifecrm health import <path>"
