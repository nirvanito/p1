"""Link memory: where a link or a site turned up, and who sent it.

Promnesia (karlicoss) shows, beside a web page, every place you met its
link before — a chat, a note, a tweet. Here the same lookup runs over what
you imported: texts and chats, emails, notes, AI chats, calendar invites,
browser history and the journal. Give it a URL or a domain; every record
whose text holds a link to it is listed, oldest first, with who sent it.

Links are compared normalised: scheme and `www.` dropped, the host
lowercased, tracking parameters (utm_*, fbclid, gclid, ...) and the
fragment removed, a trailing slash ignored. A domain matches itself and its
subdomains. Candidates come from the full-text index (the host's words), so
the lookup stays fast on a large archive. Reads only.
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit

from .config import Config
from .recall import TEXT_STREAMS, _fts_expr

_TLDS = ("com|org|net|io|co|uk|de|fr|es|it|nl|se|no|dk|fi|ch|at|be|pl|cz|si|pt|ie|ca|us|au|"
         "nz|jp|in|br|mx|edu|gov|app|dev|news|tv|me|ai|ly|gl|gg|so|to|fm|xyz|info|blog")
URL_RE = re.compile(r"""(?i)(?<![@\w.-])((?:https?://|www\.)[^\s<>"'\])}]+"""
                    rf"""|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:{_TLDS})\b"""
                    r"""(?:/[^\s<>"'\])}]*)?)""")
TRACKING = re.compile(r"^(?:utm_.*|fbclid|gclid|dclid|mc_cid|mc_eid|igshid|si|ref_src|"
                      r"ref|spm|_hsenc|_hsmi|mkt_tok)$", re.IGNORECASE)
MAX_CANDIDATES = 5000
MAX_SIGHTINGS = 200


def _clean_tail(url: str) -> str:
    return url.rstrip(".,;:!?)]}'\"")


def normalize(url: str) -> str | None:
    """'https://www.NYTimes.com/a/b/?utm_source=x#top' -> 'nytimes.com/a/b'.
    None for something that is not a web link."""
    raw = _clean_tail(url.strip())
    if not raw:
        return None
    if not re.match(r"(?i)^https?://", raw):
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        number = parts.port                   # a port that is not a number raises here
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    port = f":{number}" if number and number not in (80, 443) else ""
    path = re.sub(r"/+$", "", parts.path or "")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING.match(k)])
    return f"{host}{port}{path}" + (f"?{query}" if query else "")


def host_of(norm: str) -> str:
    return norm.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]


def target(text: str) -> dict | None:
    """What the person asked for: a URL (match that page) or a bare domain
    (match the site and its subdomains)."""
    text = text.strip().strip("<>\"'")
    norm = normalize(text)
    if norm is None:
        return None
    is_site = "/" not in norm and "?" not in norm and not re.match(r"(?i)^https?://", text)
    return {"text": text, "norm": norm, "host": host_of(norm), "site": is_site
            or norm == host_of(norm)}


def matches(t: dict, link_norm: str) -> bool:
    """A site matches itself and its subdomains; a page matches the same
    page — with any parameters, when the page was given without any."""
    host = host_of(link_norm)
    if t["site"]:
        return host == t["host"] or host.endswith("." + t["host"])
    if "?" not in t["norm"]:
        return link_norm.split("?", 1)[0] == t["norm"]
    return link_norm == t["norm"]


def urls_in(text: str | None) -> list[str]:
    return [_clean_tail(m.group(1)) for m in URL_RE.finditer(text or "")]


def _host_terms(host: str) -> list[str]:
    """The words FTS5 split a host into, minus the ones every site shares."""
    words = [w for w in re.split(r"[^\w]+", host.lower()) if w]
    common = {"com", "org", "net", "www", "co", "io", "uk", "de", "html", "http", "https"}
    kept = [w for w in words if w not in common]
    return kept or words


def sightings(conn: sqlite3.Connection, cfg: Config, text: str, *,
              start: str | None = None, end: str | None = None) -> dict:
    """{'target', 'sightings': [...]}: every record holding a link to the
    target, oldest first; each with its row, the link as written, and who
    sent it (observed) — or the journal entry (confirmed)."""
    t = target(text)
    out = {"target": t, "sightings": [], "truncated": False}
    if t is None:
        return out
    match = _fts_expr(_host_terms(t["host"]), " ")
    if not match:
        return out
    clause, args = "", []
    if start:
        clause += " AND {d} >= ?"
        args.append(start)
    if end:
        clause += " AND {d} <= ?"
        args.append(end)
    found = []
    has_obs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='observations'").fetchone() is not None
    if has_obs:
        rows = conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
            "o.direction, o.value_json, i.uuid AS import_uuid FROM observations_fts "
            "JOIN observations o ON o.id = observations_fts.rowid "
            "JOIN imports i ON i.id = o.import_id "
            f"WHERE observations_fts MATCH ? AND o.status = 'active'"
            + clause.format(d="o.journal_date")
            + " ORDER BY o.journal_date, o.started_at, o.id LIMIT ?",
            (match, *args, MAX_CANDIDATES)).fetchall()
        out["truncated"] = len(rows) >= MAX_CANDIDATES
        senders = _senders(conn, cfg, [r["id"] for r in rows])
        for r in rows:
            texts = [r["title"], r["body"]]
            if r["stream"] == "browser_visit":
                texts.append(_url_field(r["value_json"]))
            links = [u for x in texts for u in urls_in(x)]
            hit = next((u for u in links if (n := normalize(u)) and matches(t, n)), None)
            if hit is None:
                continue
            found.append({"type": "observation", "tier": "observed", "uuid": r["uuid"],
                          "stream": r["stream"], "date": r["journal_date"],
                          "occurred_at": r["started_at"], "direction": r["direction"],
                          "title": r["title"], "import_uuid": r["import_uuid"],
                          "link": hit, "from": senders.get(r["id"])})
    rows = conn.execute(
        "SELECT e.uuid, e.title, e.body, e.journal_date, k.name AS kind FROM entries_fts "
        "JOIN entries e ON e.id = entries_fts.rowid JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE entries_fts MATCH ? AND e.status = 'active'" + clause.format(d="e.journal_date")
        + " ORDER BY e.journal_date, e.id LIMIT ?", (match, *args, MAX_CANDIDATES)).fetchall()
    for r in rows:
        links = urls_in(r["title"]) + urls_in(r["body"])
        hit = next((u for u in links if (n := normalize(u)) and matches(t, n)), None)
        if hit:
            found.append({"type": "entry", "tier": "confirmed", "uuid": r["uuid"],
                          "date": r["journal_date"], "kind": r["kind"], "title": r["title"],
                          "link": hit, "from": None})
    found.sort(key=lambda x: (x["date"], x.get("occurred_at") or "", x["uuid"]))
    if len(found) > MAX_SIGHTINGS:
        out["truncated"] = True
    out["sightings"] = found[:MAX_SIGHTINGS]
    out["total"] = len(found)
    return out


def _url_field(value_json: str | None) -> str | None:
    import json
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return None
    return d.get("url") if isinstance(d, dict) else None


def _senders(conn: sqlite3.Connection, cfg: Config, obs_ids: list[int]) -> dict[int, str]:
    """Who sent each row: 'you' for an outgoing one, else the sender's linked
    name, else the name the source gave the handle (marked unlinked)."""
    out: dict[int, str] = {}
    if not obs_ids:
        return out
    for i in range(0, len(obs_ids), 500):
        chunk = obs_ids[i:i + 500]
        for r in conn.execute(
                "SELECT p.observation_id, o.direction, p.handle_kind, p.handle_value, "
                "h.display_name, CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END "
                "AS name FROM observation_participants p JOIN observations o ON "
                "o.id = p.observation_id LEFT JOIN entity_handles h ON h.kind = p.handle_kind "
                "AND h.value = p.handle_value AND h.ignored = 0 "
                "LEFT JOIN entities e ON e.id = h.entity_id "
                "LEFT JOIN entities m ON m.id = e.merged_into "
                f"WHERE p.role = 'sender' AND p.observation_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY p.observation_id, p.handle_kind", chunk):
            if r["observation_id"] in out:
                continue
            if r["direction"] == "out":
                out[r["observation_id"]] = "you"
            elif r["name"]:
                out[r["observation_id"]] = r["name"]
            else:
                shown = r["display_name"] or f"{r['handle_kind']}:{r['handle_value']}"
                out[r["observation_id"]] = f"{shown} (unlinked)"
    return out


def from_person(conn: sqlite3.Connection, cfg: Config, entity_id: int, *,
                limit: int = 20) -> list[dict]:
    """The links a person sent you in texts, chats and email, newest first."""
    from .recall import EMAIL_STREAMS, _entity_family
    family = _entity_family(conn, entity_id)
    streams = TEXT_STREAMS + EMAIL_STREAMS
    marks = ",".join("?" * len(family))
    smarks = ",".join("?" * len(streams))
    try:
        rows = conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
            "o.direction, i.uuid AS import_uuid FROM observations_fts "
            "JOIN observations o ON o.id = observations_fts.rowid "
            "JOIN imports i ON i.id = o.import_id "
            "WHERE observations_fts MATCH '\"http\" OR \"https\" OR \"www\"' "
            f"AND o.status = 'active' AND o.stream IN ({smarks}) AND o.direction = 'in' "
            "AND EXISTS (SELECT 1 FROM observation_participants p JOIN entity_handles h "
            "ON h.kind = p.handle_kind AND h.value = p.handle_value "
            f"WHERE p.observation_id = o.id AND p.role = 'sender' AND h.ignored = 0 "
            f"AND h.entity_id IN ({marks})) "
            "ORDER BY o.journal_date DESC, o.started_at DESC, o.id DESC LIMIT 500",
            (*streams, *family)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        for u in urls_in(r["title"]) + urls_in(r["body"]):
            n = normalize(u)
            if n is None:
                continue
            out.append({"type": "observation", "tier": "observed", "uuid": r["uuid"],
                        "stream": r["stream"], "date": r["journal_date"],
                        "occurred_at": r["started_at"], "direction": r["direction"],
                        "title": r["title"], "import_uuid": r["import_uuid"],
                        "link": u, "site": host_of(n)})
            if len(out) >= limit:
                return out
    return out


def render_lines(data: dict) -> list[str]:
    t = data["target"]
    if t is None:
        return ["not a link or a domain — try a URL (https://...) or a site (example.com)"]
    what = f"the site {t['host']}" if t["site"] else t["norm"]
    out = [f"{data.get('total', 0)} record(s) link to {what} (oldest first):"]
    for s in data["sightings"]:
        who = f" from {s['from']}" if s.get("from") else ""
        tag = "observed" if s["tier"] == "observed" else "journal"
        kind = s.get("stream") or s.get("kind")
        out.append(f"  {s['date']} [{tag}] {kind}{who}: {s['link']} · {s['type']} {s['uuid']}")
    if data.get("truncated"):
        out.append("  (more than the lookup reads at once — narrow it with --start/--end)")
    return out
