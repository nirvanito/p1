"""Search: every match across the journal, its transcripts and the imports,
with facets — the browse-shaped twin of `ask`.

`ask` answers one question from a few cited records; search lists every
record a few words match, the way Dogsheep Beta searches across many
tables of personal data: journal entries and transcript passages
(confirmed) and observations (observed, each naming its import), newest
first, with counts by tier, by record kind, by year and by linked person to
narrow by. Scores from different tables are not comparable (BM25 depends on
each table's own statistics), so results are ordered by date, never mixed by
score. Reads only; the query is re-quoted word by word (recall._fts_expr),
so nothing typed can reach FTS5 as syntax.
"""

from __future__ import annotations

import sqlite3

from .config import Config
from .recall import STREAM_LABELS, _fts_expr, _terms, _words

MAX_ROWS = 2000          # per table, before facets and paging
DEFAULT_LIMIT = 50
TIERS = ("confirmed", "observed")


def query_expr(q: str) -> str | None:
    """Every word of `q` AND-ed, each a quoted phrase (a prefix from four
    letters on), so 'bled lake' finds records holding both."""
    terms = _terms(_words(q or ""), frozenset())
    return _fts_expr(terms, " ", prefix=True)


def _observed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='observations'").fetchone() is not None


def search(conn: sqlite3.Connection, cfg: Config, q: str, *, tier: str | None = None,
           stream: str | None = None, person: int | None = None, year: str | None = None,
           limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """{'query', 'total', 'results', 'facets', 'filters', ...}. `stream`
    narrows observations to one stream ('entry' / 'passage' for the two
    confirmed kinds); `person` to records naming that entity; `year` to a
    calendar year of the journal date."""
    match = query_expr(q)
    out = {"query": q, "match": match, "total": 0, "results": [], "limit": limit,
           "offset": offset, "filters": {"tier": tier, "stream": stream, "person": person,
                                         "year": year},
           "facets": {"tier": {}, "stream": {}, "year": {}, "person": []},
           "truncated": False}
    if not match:
        return out
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier must be confirmed or observed, got {tier!r}")
    rows: list[dict] = []
    try:
        for r in conn.execute(
                "SELECT e.id, e.uuid, e.journal_date, e.title, k.name AS kind, "
                "snippet(entries_fts, -1, '«', '»', '…', 16) AS snippet "
                "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
                "JOIN entry_kinds k ON k.id = e.kind_id "
                "WHERE entries_fts MATCH ? AND e.status = 'active' "
                "ORDER BY e.journal_date DESC, e.id DESC LIMIT ?", (match, MAX_ROWS)):
            rows.append({"type": "entry", "tier": "confirmed", "id": r["id"],
                         "uuid": r["uuid"], "date": r["journal_date"], "title": r["title"],
                         "kind": r["kind"], "stream": "entry", "snippet": r["snippet"]})
        for r in conn.execute(
                "SELECT t.id, s.uuid, s.journal_date, s.type, "
                "snippet(transcripts_fts, -1, '«', '»', '…', 24) AS snippet "
                "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
                "JOIN sources s ON s.id = t.source_id WHERE transcripts_fts MATCH ? "
                "ORDER BY s.journal_date DESC, t.id DESC LIMIT ?", (match, MAX_ROWS)):
            rows.append({"type": "passage", "tier": "confirmed", "id": r["id"],
                         "uuid": r["uuid"], "date": r["journal_date"],
                         "title": f"{r['type']} transcript", "kind": r["type"],
                         "stream": "passage", "snippet": r["snippet"]})
        if _observed(conn):
            for r in conn.execute(
                    "SELECT o.id, o.uuid, o.journal_date, o.title, o.stream, o.direction, "
                    "i.uuid AS import_uuid, i.connector, "
                    "snippet(observations_fts, -1, '«', '»', '…', 16) AS snippet "
                    "FROM observations_fts JOIN observations o ON o.id = observations_fts.rowid "
                    "JOIN imports i ON i.id = o.import_id "
                    "WHERE observations_fts MATCH ? AND o.status = 'active' "
                    "ORDER BY o.journal_date DESC, o.started_at DESC, o.id DESC LIMIT ?",
                    (match, MAX_ROWS)):
                rows.append({"type": "observation", "tier": "observed", "id": r["id"],
                             "uuid": r["uuid"], "date": r["journal_date"],
                             "title": r["title"], "kind": STREAM_LABELS.get(r["stream"],
                                                                         r["stream"]),
                             "stream": r["stream"], "direction": r["direction"],
                             "import_uuid": r["import_uuid"], "connector": r["connector"],
                             "snippet": r["snippet"]})
    except sqlite3.OperationalError as e:
        raise ValueError(f"search failed: {e}") from e
    out["truncated"] = any(n >= MAX_ROWS for n in (
        sum(1 for r in rows if r["type"] == t) for t in ("entry", "passage", "observation")))
    people = _people(conn, cfg, rows)

    def keep(r: dict, skip: str | None = None) -> bool:
        if skip != "tier" and tier and r["tier"] != tier:
            return False
        if skip != "stream" and stream and r["stream"] != stream:
            return False
        if skip != "year" and year and r["date"][:4] != str(year):
            return False
        if skip != "person" and person is not None and person not in people.get(
                (r["type"], r["id"]), ()):
            return False
        return True

    # Each facet counts what the OTHER filters leave, so picking one value
    # still shows its siblings.
    for name, key in (("tier", lambda r: r["tier"]), ("stream", lambda r: r["stream"]),
                      ("year", lambda r: r["date"][:4])):
        counts: dict[str, int] = {}
        for r in rows:
            if keep(r, skip=name):
                counts[key(r)] = counts.get(key(r), 0) + 1
        out["facets"][name] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                                   if name != "year" else sorted(counts.items(), reverse=True))
    names = _names(conn, {e for v in people.values() for e in v})
    pc: dict[int, int] = {}
    for r in rows:
        if keep(r, skip="person"):
            for e in people.get((r["type"], r["id"]), ()):
                pc[e] = pc.get(e, 0) + 1
    out["facets"]["person"] = [{"id": e, "name": names.get(e, f"entity {e}"), "count": n}
                               for e, n in sorted(pc.items(), key=lambda kv: (
                                   -kv[1], names.get(kv[0], "").lower()))][:15]
    hits = [r for r in rows if keep(r)]
    hits.sort(key=lambda r: (r["date"], r["type"] != "observation", r["id"]), reverse=True)
    out["total"] = len(hits)
    for r in hits[offset:offset + limit]:
        item = {k: v for k, v in r.items() if k != "id"}
        item["people"] = [names.get(e, f"entity {e}") for e in people.get((r["type"], r["id"]), ())]
        out["results"].append(item)
    return out


def _people(conn: sqlite3.Connection, cfg: Config, rows: list[dict]
            ) -> dict[tuple[str, int], list[int]]:
    """(type, row id) -> the person entities each record names: an entry's
    mentions, an observation's linked handles (merges followed, the owner
    left out)."""
    out: dict[tuple[str, int], list[int]] = {}
    owners = frozenset()
    try:
        from . import owner as owner_mod
        owners = owner_mod.identity(conn, cfg).entities
    except Exception:                          # a bad [owner] table: nobody left out
        pass
    entry_ids = [r["id"] for r in rows if r["type"] == "entry"]
    obs_ids = [r["id"] for r in rows if r["type"] == "observation"]
    for i in range(0, len(entry_ids), 500):
        chunk = entry_ids[i:i + 500]
        for r in conn.execute(
                "SELECT m.entry_id, CASE WHEN en.status = 'merged' THEN en.merged_into "
                "ELSE en.id END AS eid FROM mentions m JOIN entities en ON en.id = m.entity_id "
                f"WHERE en.type = 'person' AND m.entry_id IN ({','.join('?' * len(chunk))})",
                chunk):
            if r["eid"] not in owners:
                ids = out.setdefault(("entry", r["entry_id"]), [])
                if r["eid"] not in ids:
                    ids.append(r["eid"])
    for i in range(0, len(obs_ids), 500):
        chunk = obs_ids[i:i + 500]
        for r in conn.execute(
                "SELECT p.observation_id, CASE WHEN e.status = 'merged' THEN e.merged_into "
                "ELSE e.id END AS eid, CASE WHEN e.status = 'merged' THEN m.type "
                "ELSE e.type END AS etype FROM observation_participants p "
                "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
                "JOIN entities e ON e.id = h.entity_id LEFT JOIN entities m ON m.id = e.merged_into "
                f"WHERE h.ignored = 0 AND p.observation_id IN ({','.join('?' * len(chunk))})",
                chunk):
            if r["etype"] == "person" and r["eid"] not in owners:
                ids = out.setdefault(("observation", r["observation_id"]), [])
                if r["eid"] not in ids:
                    ids.append(r["eid"])
    return out


def _names(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM entities WHERE id IN ({marks})", sorted(ids))}


def render_lines(data: dict) -> list[str]:
    f = data["facets"]
    out = [f"{data['total']} match(es) for \"{data['query']}\""
           + (" (showing the newest)" if data["total"] > len(data["results"]) else "")
           + (" — more than the search reads at once; narrow it" if data["truncated"] else "")]
    if f["tier"]:
        out.append("  by tier: " + ", ".join(f"{k} {v}" for k, v in f["tier"].items()))
    if f["stream"]:
        out.append("  by kind: " + ", ".join(f"{k} {v}" for k, v in f["stream"].items()))
    if f["year"]:
        out.append("  by year: " + ", ".join(f"{k} {v}" for k, v in f["year"].items()))
    if f["person"]:
        out.append("  by person: " + ", ".join(f"{p['name']} {p['count']} (id {p['id']})"
                                               for p in f["person"]))
    for r in data["results"]:
        tag = "observed" if r["tier"] == "observed" else "confirmed"
        snippet = " ".join((r.get("snippet") or "").split())
        out.append(f"  {r['date']} [{tag}] {r['kind']}: {r.get('title') or ''}"
                   + (f" — {snippet}" if snippet else "") + f" · {r['type']} {r['uuid']}")
    return out
