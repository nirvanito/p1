"""RFC 5545 text, writer side: escaping, 75-octet folding, CRLF joining and
the two timestamp shapes. Shared by the demo's calendar export
(demo/sources/calendar.py) and the reminders calendar (reminders.py), so
both produce the same bytes for the same lines. The reader is
connectors/_ics.py, which parses what this writes."""

from __future__ import annotations

from datetime import datetime, timezone


def esc(text: str) -> str:
    """TEXT value escaping (RFC 5545 3.3.11): backslash, semicolon, comma and
    newline."""
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\n", "\\n"))


def fold(line: str) -> str:
    """Content-line folding (3.1): 75 octets per line, never splitting a
    character; continuation lines start with one space."""
    chunks, cur, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            chunks.append("".join(cur))
            cur, size, limit = [], 0, 74
        cur.append(ch)
        size += n
    chunks.append("".join(cur))
    return "\r\n ".join(chunks)


def render(lines: list[str]) -> bytes:
    """The calendar's bytes: every line folded, CRLF-terminated, UTF-8."""
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode("utf-8")


def utc(dt: datetime) -> str:
    """A UTC DATE-TIME (`20261024T140000Z`)."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def local(dt: datetime) -> str:
    """A floating or TZID-qualified DATE-TIME (`20261024T090000`)."""
    return dt.strftime("%Y%m%dT%H%M%S")
