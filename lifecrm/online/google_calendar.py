"""Google Calendar through its private iCal link — no sign-in, read-only.

Google Calendar › Settings › (a calendar) › Integrate calendar › *Secret
address in iCal format* is a link to that calendar's whole current state as
an .ics file, refreshed by Google every few hours. Each morning every link
is downloaded into `data/fetched/google_calendar/` (one file per link, its
name a hash of the link), and the calendar connector reads that folder like
any calendar export: repeats expanded to today + 90 days, an event deleted
from the calendar gone from the next file. Any other https iCal link works
the same way (Outlook's published calendar, an iCloud public calendar).

The link is a password in all but name: whoever has it reads the calendar.
It is kept in data/secrets.json, never printed; Google's "Reset" next to it
invalidates the old one.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from ..config import Config
from ..domain.util import DomainError

SERVICE = "google_calendar"
CONNECTOR = "calendar"
TITLE = "Google Calendar"
DOC = "docs/online.md"
TIMEOUT = 60


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "lifecrm (personal use)"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def steps() -> list[str]:
    return [
        "On calendar.google.com: Settings (the gear) › under 'Settings for my "
        "calendars' click a calendar › Integrate calendar › copy 'Secret address in "
        "iCal format'.",
        "In a terminal here: python -m lifecrm connect google_calendar — paste the "
        "link when asked (or pass several links, one per calendar).",
        "The next morning collect finds it; /morning asks before the first import.",
    ]


def _name(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + ".ics"


def _download(url: str) -> bytes:
    try:
        with _open(url) as resp:
            body = resp.read()
    except OSError as e:
        raise DomainError(f"could not download the calendar link ({e}) — check it on "
                          f"calendar.google.com, or reconnect: python -m lifecrm connect "
                          f"google_calendar") from e
    if b"BEGIN:VCALENDAR" not in body[:4096]:
        raise DomainError("the calendar link did not return a calendar (no "
                          "BEGIN:VCALENDAR) — copy the 'Secret address in iCal format', "
                          "not the public or embed link")
    return body


def _check(url: str) -> str:
    url = url.strip().strip("<>\"'")
    if not url.lower().startswith("https://"):
        raise DomainError("a calendar link starts with https:// — copy the 'Secret "
                          "address in iCal format' from Google Calendar's settings")
    return url


def connect(cfg: Config, args: list[str], ask) -> dict:
    urls = [_check(u) for u in args] or [_check(ask(
        "Paste the calendar's 'Secret address in iCal format'", True))]
    for url in urls:
        _download(url)                     # a typo is caught now, not at 06:30
    return {"urls": list(dict.fromkeys(urls))}


def fetch(cfg: Config, secret: dict, dest: Path) -> Path:
    urls = secret.get("urls") or []
    if not urls:
        raise DomainError("no calendar link kept — run: python -m lifecrm connect "
                          "google_calendar")
    keep = set()
    for url in urls:
        body = _download(url)
        out = dest / _name(url)
        tmp = out.with_name(out.name + ".part")
        tmp.write_bytes(body)
        tmp.replace(out)
        keep.add(out.name)
    for old in dest.glob("*.ics"):          # a link the owner dropped
        if old.name not in keep:
            old.unlink()
    return dest
