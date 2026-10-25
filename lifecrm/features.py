"""Feature switches: one list to check and uncheck the read-side features.

Fourteen features sit on top of the records — what earlier years held on
today's date, a brief before a meeting, place names and trips, prompts for
the journal, subscriptions, self-experiments, search, LinkedIn job changes,
the year in review, where a link was seen, gift ideas and anniversaries, one
conversation per person, weekday rhythms, and highlights brought back (the
table below, docs/features.md). Each one only READS: unchecking a feature
hides what it shows and changes no record, and checking it again brings it
back exactly as it was, because it is recomputed from the database every
time.

The switches live in config.toml, the file every other setting lives in:

    [features]
    on_this_day = true
    subscriptions = false

A feature not listed is on. `lifecrm features` lists them as a checklist and
`features check` / `uncheck` (one key, several, or --all) write the table,
touching nothing else in the file — `save` rewrites only the [features]
lines, then re-reads the whole file and refuses unless everything outside
that table parses exactly as before. The web UI's Sources page writes the
same table through the same `save` (POST /api/features), only on a server
started with a key.

What a switch never turns off is a rule on the write path: the shape of
`data.experiment`, `data.gift` and `data.anniversary` is checked on every
write whether or not their feature is shown, so a plan written while a
feature was unchecked still reads correctly once it is checked again.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .domain.util import DomainError

SECTION = "features"
CONFIG_NAME = "config.toml"


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    summary: str
    shows_in: tuple[str, ...]


FEATURES: tuple[Feature, ...] = (
    Feature("on_this_day", "On this day",
            "entries and records from today's date in earlier years",
            ("nudges / /morning", "web Today", "vault day notes and Home")),
    Feature("meeting_prep", "Meeting prep",
            "before today's and tomorrow's meetings: last contact, open loops, recent "
            "messages and what you know about each person on them",
            ("nudges agenda / /morning", "web Today", "vault Home")),
    Feature("places", "Places, trips and countries",
            "town and country names for visits and photos from a local GeoNames file, "
            "trips away from home, countries visited",
            ("ask", "review pack", "web Places", "vault Trips", "lifecrm places")),
    Feature("journal_prompts", "Journal prompts",
            "moments from a day's imports worth a word in the journal: a first visit, a "
            "burst of photos, a long call, a meeting, a workout, an unusual spend",
            ("nudges / /morning", "web Today", "vault Home", "lifecrm prompts", "/log")),
    Feature("subscriptions", "Subscriptions",
            "charges that repeat weekly, monthly or yearly, new ones, and price rises",
            ("nudges", "review finance", "ask", "lifecrm subscriptions")),
    Feature("experiments", "Self-experiments",
            "a plan with data.experiment: the outcome before and during the change",
            ("nudges", "review pack", "lifecrm experiments")),
    Feature("search", "Search",
            "every match across journal entries, transcripts and imports, with facets",
            ("web Search", "lifecrm search")),
    Feature("job_changes", "LinkedIn job changes",
            "a linked person's position or company changed between two LinkedIn exports",
            ("nudges", "review career", "web person page")),
    Feature("year_review", "Year in review",
            "the review pack's year lens: top people, new people, places, books, "
            "listening, bests, money, mood by month",
            ("lifecrm review --lens year", "/review-week")),
    Feature("link_memory", "Link memory",
            "where a link or a site turned up: who sent it, in which chat, email or note",
            ("ask", "web person page", "lifecrm links")),
    Feature("gift_ideas", "Gift ideas and anniversaries",
            "gift ideas you noted for a person beside their birthday, and anniversaries "
            "from data.anniversary",
            ("nudges", "reminders calendar", "web person page")),
    Feature("conversations", "Conversations",
            "one thread per person across texts, chats and email",
            ("web person page", "lifecrm conversation")),
    Feature("weekday_profile", "Weekday profile",
            "each series averaged by day of the week, over the days it knows",
            ("review pack", "ask", "lifecrm weekdays")),
    Feature("highlights", "Highlights",
            "a few of your book highlights each day, in a fixed rotation",
            ("nudges / /morning", "vault Home", "web Today")),
)
BY_KEY = {f.key: f for f in FEATURES}
KEYS = tuple(BY_KEY)
# Switches that existed before this list and keep their own tables (and
# their own defaults): named in the listing so every switch is findable here.
OTHER_SWITCHES = (
    ("[assistant] patterns", '"on" | "off" (default off)',
     "the experimental pattern finder in nudges, review and /morning (docs/patterns.md)"),
    ("[ask] enabled", "true | false (default false)",
     "a local model phrasing ask's answers (docs/ask.md)"),
    ("[obsidian] auto_refresh", "true | false (default true once a vault is set)",
     "re-render the vault after every command that changes the database"),
    ("[assistant] auto_publish", "true | false (default true)",
     "republish the reminders calendar after every change"),
)

_FLAG_WORDS = {"true": True, "yes": True, "on": True, "1": True,
               "false": False, "no": False, "off": False, "0": False}


def _flag(key: str, value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _FLAG_WORDS:
        return _FLAG_WORDS[value.strip().lower()]
    raise DomainError(f"[{SECTION}] {key} must be true or false, got {value!r}")


def settings(cfg: Config) -> dict[str, bool]:
    """Every feature's switch: [features] in config.toml over the default
    (on). One-line DomainError for a key or a value the table cannot hold."""
    raw = (cfg.extra or {}).get(SECTION)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DomainError(f"[{SECTION}] must be a table of <feature> = true|false, "
                          f"got {raw!r}")
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown feature(s) {', '.join(unknown)} — "
                          f"known: {', '.join(KEYS)} (python -m lifecrm features)")
    return {k: _flag(k, raw[k]) if k in raw else True for k in KEYS}


def enabled(cfg: Config, key: str) -> bool:
    """Whether feature `key` is checked. A key not in the registry is a
    programming error, never a silent False."""
    if key not in BY_KEY:
        raise KeyError(f"no feature {key!r}")
    return settings(cfg)[key]


def resolve(names: list[str]) -> list[str]:
    """Feature keys from what was typed: a key, a number from the list (1-14),
    or a key with dashes for underscores. One-line DomainError otherwise."""
    out = []
    for name in names:
        text = name.strip().lower().replace("-", "_")
        if text.isdigit() and 1 <= int(text) <= len(FEATURES):
            key = FEATURES[int(text) - 1].key
        elif text in BY_KEY:
            key = text
        else:
            raise DomainError(f"no feature '{name}' — known: {', '.join(KEYS)} "
                              f"(or their numbers 1-{len(FEATURES)})")
        if key not in out:
            out.append(key)
    return out


def config_file(cfg: Config) -> Path:
    return cfg.root / CONFIG_NAME


# ---------------------------------------------------------------- writing

def _render(text: str, values: dict[str, bool]) -> str:
    """`text` with each key of `values` set inside [features]: a key's own
    line rewritten in place (its trailing comment kept), a missing key added
    after the table's last line, a missing table appended at the end. No
    other line changes."""
    from .commands.configure import _KEY_RE, _SECTION_RE, _value_end
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    trailing = lines and lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    pending = {k: ("true" if v else "false") for k, v in values.items()}
    out: list[str] = []
    section = None
    seen_table = False
    header_at = 0

    def flush() -> None:
        # after the table's last setting (or its header), never after a
        # commented-out example block that happens to follow it
        if not pending:
            return
        settings_at = [i for i in range(header_at + 1, len(out))
                       if out[i].strip() and not out[i].lstrip().startswith("#")]
        if settings_at:
            at = settings_at[-1] + 1
        else:                        # past the comment lines right under the header
            at = header_at + 1
            while at < len(out) and out[at].strip() and out[at].lstrip().startswith("#"):
                at += 1
        out[at:at] = [f"{k} = {v}" for k, v in pending.items()]
        pending.clear()

    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            if section == SECTION:
                flush()
            section = m.group(1).strip()
            if section == SECTION:
                seen_table = True
                header_at = len(out)
            out.append(line)
            continue
        km = _KEY_RE.match(line)
        if section == SECTION and km and not line.lstrip().startswith("#") \
                and km.group(2) in pending:
            indent, key, eq, rest = km.groups()
            end = _value_end(rest)
            out.append(f"{indent}{key}{eq}{pending.pop(key)}{rest[end:]}")
            continue
        out.append(line)
    if section == SECTION:
        flush()
    if pending:
        if seen_table:                      # the table was closed before the file ended
            raise DomainError(f"[{SECTION}] could not be updated in place")
        while out and not out[-1].strip():
            out.pop()
        out += ["", f"[{SECTION}]"] + [f"{k} = {v}" for k, v in pending.items()]
    return newline.join(out) + newline


def save(cfg: Config, values: dict[str, bool]) -> Path:
    """Write `values` ({feature key: on?}) into [features] of this root's
    config.toml and return its path. Refuses — changing nothing — when the
    file is missing or not valid TOML, when [features] is not a plain table,
    or when anything outside the switches would read differently after."""
    for key in values:
        if key not in BY_KEY:
            raise DomainError(f"no feature '{key}' — known: {', '.join(KEYS)}")
    path = config_file(cfg)
    if not path.exists():
        raise DomainError(f"no {CONFIG_NAME} in {cfg.root} — run: python -m lifecrm "
                          f"configure (the switches are saved there)")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        before = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DomainError(f"cannot read {path} ({e}) — fix it, then check or uncheck "
                          f"again") from e
    table = before.get(SECTION)
    if table is not None and not isinstance(table, dict):
        raise DomainError(f"{path}: {SECTION} is not a [{SECTION}] table — edit it by hand")
    new_text = _render(text, values)
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise DomainError(f"{path}: [{SECTION}] could not be updated safely ({e}) — "
                          f"edit it by hand") from e
    expected = {**before, SECTION: {**(table or {}), **values}}
    if after != expected:
        raise DomainError(f"{path}: [{SECTION}] could not be updated without touching "
                          f"other settings — edit it by hand")
    data = new_text.encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data          # keep the BOM Notepad wrote
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


# ---------------------------------------------------------------- reading

def listing(cfg: Config) -> dict:
    """The checklist as data: every feature with its switch, plus the error
    when the table cannot be read (the list then shows the defaults)."""
    error = None
    try:
        states = settings(cfg)
    except DomainError as e:
        error = str(e)
        raw = (cfg.extra or {}).get(SECTION)
        raw = raw if isinstance(raw, dict) else {}
        states = {}
        for k in KEYS:
            try:
                states[k] = _flag(k, raw[k]) if k in raw else True
            except DomainError:
                states[k] = True
    listed = (cfg.extra or {}).get(SECTION)
    listed = set(listed) if isinstance(listed, dict) else set()
    items = [{"n": i + 1, "key": f.key, "title": f.title, "summary": f.summary,
              "shows_in": list(f.shows_in), "on": states[f.key],
              "set_in_config": f.key in listed}
             for i, f in enumerate(FEATURES)]
    return {"config": str(config_file(cfg)), "features": items,
            "on": sum(1 for x in items if x["on"]), "total": len(items), "error": error,
            "other_switches": [{"setting": s, "values": v, "what": w}
                               for s, v, w in OTHER_SWITCHES]}


def render_lines(data: dict) -> list[str]:
    out = [f"Features — {data['on']} of {data['total']} checked "
           f"([{SECTION}] in {data['config']}; a feature not listed there is on):"]
    width = max(len(x["key"]) for x in data["features"])
    for x in data["features"]:
        box = "[x]" if x["on"] else "[ ]"
        out.append(f"  {box} {x['n']:>2} {x['key']:<{width}}  {x['title']} — {x['summary']}")
    if data.get("error"):
        out.append(f"error: {data['error']}")
    out.append("check: python -m lifecrm features check <key|number>... (or --all) · "
               "uncheck: features uncheck ... · pick: features pick")
    if data.get("other_switches"):
        out.append("Older switches keep their own settings in config.toml:")
        out += [f"  {x['setting']} = {x['values']} — {x['what']}"
                for x in data["other_switches"]]
    return out
