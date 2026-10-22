"""`lifecrm configure`: write config.toml for this machine.

A fresh checkout used to copy config.example.toml and print EDIT THE PATHS.
On a Mac every one of those example paths was a Windows path, and the
timezone was Chicago for everyone. configure proposes this machine's own
answers instead: the VoiceJournal folder in iCloud Drive (Mac + iPhone) or
OneDrive (Windows + Android), the operating system's timezone as an IANA
name, and the four folders the phone and the backups need, created so the
phone has somewhere to save from the first minute.

Everything else is config.example.toml's own text, comments included, so a
setting added to the example reaches every new config without an edit here.

It proposes, it does not decide silently: every value is printed with where
it came from; an existing config.toml is never replaced without --force (and
the replaced file is kept under data/, which git ignores — a config can hold
an owner's phone number); and the cloud drive itself is never created. A
VoiceJournal folder made inside a missing iCloud Drive or OneDrive folder
would be an ordinary local folder the phone can never reach, which looks
exactly like a quiet day.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer

from .. import platforms
from ..config import PROJECT_ROOT

FALLBACK_TIMEZONE = "America/Chicago"

# The journal's four folders, as config keys. A key with several names is
# set under whichever one the example uses (the last is the one added when
# the example has none).
JOURNAL_FOLDERS = (
    (("inbox_dir",), "inbox"),
    (("inbox_text_dir",), "inbox-text"),
    (("archive_dir",), "archive"),
    (("cloud_backups_dir", "onedrive_backups_dir"), "backups"),
)

# Windows zone ids (what `tzutil /g` prints) -> IANA, from CLDR's
# windowsZones.xml "001" rows: each Windows zone's representative city. Covers
# every zone a Windows laptop in the Americas, Europe, Africa, Asia or
# Oceania is likely to be set to; an unlisted one falls back with a note.
WINDOWS_ZONES = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Venezuela Standard Time": "America/Caracas",
    "Atlantic Standard Time": "America/Halifax",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "SA Eastern Standard Time": "America/Cayenne",
    "Greenland Standard Time": "America/Nuuk",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "GTB Standard Time": "Europe/Bucharest",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kyiv",
    "Israel Standard Time": "Asia/Jerusalem",
    "Egypt Standard Time": "Africa/Cairo",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Pakistan Standard Time": "Asia/Karachi",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Kathmandu",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Myanmar Standard Time": "Asia/Yangon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "W. Australia Standard Time": "Australia/Perth",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Tasmania Standard Time": "Australia/Hobart",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Tonga Standard Time": "Pacific/Tongatapu",
}


# ---------------------------------------------------------------- timezone

def _valid_zone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def iana_from_windows(windows_name: str) -> str | None:
    """'Central Standard Time' -> 'America/Chicago'. tzutil appends '_dstoff'
    when daylight saving is switched off for the zone; the zone is the same."""
    name = windows_name.strip().removesuffix("_dstoff")
    return WINDOWS_ZONES.get(name)


def zone_from_localtime_link(target: str) -> str | None:
    """The IANA name in /etc/localtime's link target. macOS points it at
    /var/db/timezone/zoneinfo/<Area/City>, most Linux at
    /usr/share/zoneinfo/<Area/City> (sometimes under posix/ or right/)."""
    _, sep, tail = target.replace("\\", "/").partition("zoneinfo/")
    if not sep:
        return None
    for prefix in ("posix/", "right/"):
        tail = tail.removeprefix(prefix)
    return tail or None


def _windows_zone_name() -> str | None:
    try:
        done = subprocess.run(["tzutil", "/g"], capture_output=True, text=True,
                              timeout=10, check=False)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:   # the same value tzutil reads, for a machine without tzutil on PATH
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as k:
            return str(winreg.QueryValueEx(k, "TimeZoneKeyName")[0]).strip("\x00 ") or None
    except (ImportError, OSError):
        return None


def detect_timezone() -> tuple[str, str]:
    """(IANA zone, where it came from). Never raises: an undetectable zone
    is the fallback, and the note says so."""
    if platforms.is_windows():
        win = _windows_zone_name()
        if not win:
            return FALLBACK_TIMEZONE, (f"could not read the Windows timezone — "
                                       f"using {FALLBACK_TIMEZONE}; set [time] timezone")
        zone = iana_from_windows(win)
        if _valid_zone(zone):
            return zone, f"from Windows' '{win}'"
        return FALLBACK_TIMEZONE, (f"Windows zone '{win}' is not in the table — "
                                   f"using {FALLBACK_TIMEZONE}; set [time] timezone")
    try:
        zone = zone_from_localtime_link(os.readlink("/etc/localtime"))
    except OSError:
        zone = None
    if _valid_zone(zone):
        return zone, "from /etc/localtime"
    return FALLBACK_TIMEZONE, (f"could not read the system timezone — using "
                               f"{FALLBACK_TIMEZONE}; set [time] timezone")


# ---------------------------------------------------------------- rendering

_SECTION_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


def toml_string(value: str) -> str:
    """A literal string ('...') when it can be one — Windows backslashes need
    no escaping there — else a basic string with escapes."""
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _value_end(rest: str) -> int:
    """Index just past the value at the start of `rest` (a quoted string or a
    bare token), so a trailing comment can be kept."""
    if rest[:1] in ("'", '"'):
        quote, i = rest[0], 1
        while i < len(rest):
            if quote == '"' and rest[i] == "\\":
                i += 2
                continue
            if rest[i] == quote:
                return i + 1
            i += 1
        return len(rest)
    hash_at = rest.find("#")
    return len(rest[:hash_at].rstrip()) if hash_at >= 0 else len(rest.rstrip())


def render(example: str, values: dict[str, dict[str, str]], header: list[str]) -> str:
    """The example's text with `values` ({section: {key: toml literal}}) set.

    A key's own line is rewritten in place, its trailing comment kept at the
    same column; commented-out lines are left alone (they are the other OS's
    examples); a key the section lacks is added at the end of that section,
    and a missing section at the end of the file. The example's leading
    comment block is replaced by `header`.
    """
    lines = example.splitlines()
    while lines and (lines[0].lstrip().startswith("#") or not lines[0].strip()):
        lines.pop(0)
    pending = {s: dict(kv) for s, kv in values.items()}
    out: list[str] = []
    section = ""

    def flush(sec: str) -> None:
        # missing keys go after the section's last non-blank line
        extra = pending.pop(sec, {})
        if not extra:
            return
        at = len(out)
        while at > 0 and not out[at - 1].strip():
            at -= 1
        out[at:at] = [f"{k} = {v}" for k, v in extra.items()]

    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            flush(section)
            section = m.group(1)
            out.append(line)
            continue
        km = _KEY_RE.match(line)
        if km and not line.lstrip().startswith("#") and km.group(2) in pending.get(section, {}):
            indent, key, eq, rest = km.groups()
            end = _value_end(rest)
            tail = rest[end:]
            comment = tail.lstrip()
            head = f"{indent}{key}{eq}{pending[section].pop(key)}"
            if comment:
                column = len(indent + key + eq) + end + (len(tail) - len(comment))
                head = head.ljust(max(column, len(head) + 1)) + comment
            out.append(head)
            continue
        out.append(line)
    flush(section)
    for sec, extra in pending.items():
        if extra:
            out += ["", f"[{sec}]"] + [f"{k} = {v}" for k, v in extra.items()]
    return "\n".join(header + [""] + out).rstrip("\n") + "\n"


def _present_keys(example: str, section: str) -> set[str]:
    found, current = set(), ""
    for line in example.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1)
            continue
        km = _KEY_RE.match(line)
        if current == section and km and not line.lstrip().startswith("#"):
            found.add(km.group(2))
    return found


_MINIMAL_EXAMPLE = "[paths]\ndata_dir = \"data\"\nbackups_dir = \"backups\"\n\n[time]\n"


def _example_text() -> str:
    p = PROJECT_ROOT / "config.example.toml"
    try:
        return p.read_text(encoding="utf-8-sig")
    except OSError:
        return _MINIMAL_EXAMPLE


# ---------------------------------------------------------------- command

def _cloud_label(given: bool) -> str:
    if given:
        return "given with --journal-root"
    if platforms.is_mac():
        return "iCloud Drive — the iPhone Shortcuts save to a fixed folder there"
    return "OneDrive — what Windows syncs and the Android sync apps upload to"


def configure(
    force: bool = typer.Option(False, "--force",
                               help="Replace an existing config.toml (kept under data/)"),
    journal_root: str = typer.Option(
        None, "--journal-root", metavar="DIR",
        help="The VoiceJournal folder (default: iCloud Drive on a Mac, "
             "OneDrive on Windows)"),
    tz: str = typer.Option(None, "--timezone", metavar="ZONE",
                           help="IANA zone (default: this machine's)"),
    no_mkdir: bool = typer.Option(False, "--no-mkdir",
                                  help="Write the config only; create no folders"),
) -> None:
    """Write config.toml with this machine's journal folders and timezone."""
    from ..cli import _fail
    env_root = os.environ.get("LIFECRM_ROOT")
    root = Path(env_root) if env_root else PROJECT_ROOT
    if not root.is_dir():
        _fail(f"project root {root} does not exist")
    cfg_file = root / "config.toml"
    if cfg_file.exists() and not force:
        _fail(f"{cfg_file} already exists — edit it, or re-run with --force to "
              f"replace it (the current one is kept under data/)")

    given = journal_root is not None
    jroot = (Path(journal_root).expanduser().absolute() if given
             else platforms.default_journal_root())
    if tz:
        if not _valid_zone(tz):
            _fail(f"--timezone '{tz}' is not a known IANA zone "
                  f"(e.g. America/Chicago, Europe/London)")
        zone, zone_note = tz, "given with --timezone"
    else:
        zone, zone_note = detect_timezone()

    example = _example_text()
    present = _present_keys(example, "paths")
    folders: list[tuple[str, Path]] = []
    paths: dict[str, str] = {}
    for names, sub in JOURNAL_FOLDERS:
        key = next((n for n in names if n in present), names[-1])
        folders.append((key, jroot / sub))
        paths[key] = toml_string(str(jroot / sub))
    system = {"mac": "Mac", "windows": "Windows PC"}.get(platforms.SYSTEM, "computer")
    header = [
        "# Life CRM configuration.",
        f"# Written by `lifecrm configure` on {datetime.now():%Y-%m-%d} for this {system};",
        "# edit freely. Relative paths resolve against the project root; '~' is your",
        "# home folder. `lifecrm configure --force` starts over (the replaced file is",
        "# kept under data/).",
    ]
    text = render(example, {"paths": paths, "time": {"timezone": toml_string(zone)}},
                  header)

    if cfg_file.exists():
        keep_dir = root / "data"
        keep_dir.mkdir(parents=True, exist_ok=True)
        kept = keep_dir / f"config.toml.replaced-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(cfg_file, kept)
        typer.echo(f"kept the previous config as {kept}")
    cfg_file.write_text(text, encoding="utf-8", newline="\n")

    typer.echo(f"wrote {cfg_file}")
    width = max(len(k) for k in [k for k, _ in folders] + ["journal folder"])
    typer.echo(f"  {'journal folder':<{width}}  {jroot}  ({_cloud_label(given)})")
    for key, path in folders:
        typer.echo(f"  {key:<{width}}  {path}")
    typer.echo(f"  {'timezone':<{width}}  {zone}  ({zone_note})")
    leftovers = [line.split("=")[0].strip() for line in text.splitlines()
                 if "<you>" in line and not line.lstrip().startswith("#")]
    if leftovers:
        typer.echo(f"  note: still the example's placeholder path: "
                   f"{', '.join(leftovers)} — edit config.toml")

    if no_mkdir:
        typer.echo("folders not created (--no-mkdir)")
    elif not jroot.parent.is_dir():
        # Never create the cloud drive: see the module docstring.
        cloud = ("iCloud Drive is not turned on (System Settings › Apple Account "
                 "› iCloud › iCloud Drive)" if platforms.is_mac() and not given
                 else "sign in to OneDrive first" if not given
                 else "create it first")
        typer.echo(f"folders NOT created: {jroot.parent} does not exist — {cloud}, "
                   f"then re-run with --force")
    else:
        made = []
        for _, path in folders:
            if not path.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                made.append(path.name)
        typer.echo(f"created in {jroot}: {', '.join(made)}" if made
                   else f"folders already exist in {jroot}")
        if platforms.is_mac() and not given:
            typer.echo("  in Finder, right-click VoiceJournal → Keep Downloaded, so "
                       "notes are real files on this Mac, not iCloud stubs")
    typer.echo("next: python -m lifecrm init, then point the phone at the inbox "
               "folders (docs/mac.md, docs/iphone.md, docs/android.md); "
               "python -m lifecrm where lists everything else this machine can import")


def register(app: typer.Typer) -> None:
    app.command("configure")(configure)
