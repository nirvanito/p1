"""Tiered snapshot retention: 14 daily, 12 weekly, 24 monthly, yearly
indefinitely. Snapshots are small; a decade of memory deserves more than a
week of history.

Policy detail: within a calendar day only the newest snapshot survives; the
day-level tiers then decide which days survive. Pure function so the policy
is testable without touching a filesystem.

Pre-removal snapshots (`pre-removal-*.db`, taken by `observe` before a
mirroring import removes anything) keep a rule of their own: every one from
the 14 most recent days that have one. They cannot share the daily tier: the
day's newest snapshot wins there, a backup later that day is newer, and it
was taken *after* the removal — so the one copy of what was removed would be
the first thing pruned.
"""

from __future__ import annotations

import re
from datetime import datetime

DAILY, WEEKLY, MONTHLY = 14, 12, 24
PRE_REMOVAL_DAYS = 14
PRE_REMOVAL_GLOB = "pre-removal-*.db"
_SNAP_RE = re.compile(r"^lifecrm-(\d{8})-(\d{6})\.db$")
_PRE_REMOVAL_RE = re.compile(r"^pre-removal-(\d{8})-(\d{6})\.db$")


def select_retained(names: list[str]) -> set[str]:
    """Given snapshot filenames (lifecrm-YYYYMMDD-HHMMSS.db, or
    pre-removal-YYYYMMDD-HHMMSS.db), return the set to KEEP. Unrecognized
    names are always kept (never delete what we do not understand)."""
    keep: set[str] = set()
    by_day: dict[str, str] = {}
    pre_removal: dict[str, list[str]] = {}
    for name in names:
        m = _SNAP_RE.match(name)
        if not m:
            pre = _PRE_REMOVAL_RE.match(name)
            if pre:
                pre_removal.setdefault(pre.group(1), []).append(name)
            else:
                keep.add(name)
            continue
        day = m.group(1)
        if day not in by_day or name > by_day[day]:
            by_day[day] = name

    for day in sorted(pre_removal, reverse=True)[:PRE_REMOVAL_DAYS]:
        keep.update(pre_removal[day])     # all of them: each guards its own removal

    days_desc = sorted(by_day, reverse=True)
    kept_days: set[str] = set(days_desc[:DAILY])

    weeks: dict[tuple, str] = {}
    months: dict[str, str] = {}
    years: dict[str, str] = {}
    for day in days_desc:   # newest first, so first-seen wins per bucket
        d = datetime.strptime(day, "%Y%m%d").date()
        weeks.setdefault(d.isocalendar()[:2], day)
        months.setdefault(day[:6], day)
        years.setdefault(day[:4], day)
    for wk in sorted(weeks, reverse=True)[:WEEKLY]:
        kept_days.add(weeks[wk])
    for mo in sorted(months, reverse=True)[:MONTHLY]:
        kept_days.add(months[mo])
    kept_days.update(years.values())   # yearly tier: indefinitely

    keep.update(by_day[day] for day in kept_days)
    return keep


def prune_dir(dirpath, pattern: str = "lifecrm-*.db") -> list[str]:
    """Apply the policy to a directory; returns the deleted names. One
    pattern per call: `backup` prunes the tiered snapshots and the
    pre-removal ones (PRE_REMOVAL_GLOB) separately."""
    if dirpath is None or not dirpath.exists():
        return []
    files = {p.name: p for p in dirpath.glob(pattern)}
    keep = select_retained(list(files))
    removed = []
    for name, path in sorted(files.items()):
        if name not in keep:
            path.unlink()
            removed.append(name)
    return removed
