"""Read-only view assembly shared by every front end (CLI, web UI, voice).

Each function takes a connection (read-only is enough), reads, and returns
plain dicts. Nothing here writes, and nothing here decides what is true —
it only gathers what the database already says, with its provenance.
"""

from __future__ import annotations

import sqlite3


def entry_provenance(conn: sqlite3.Connection, entry_id: int) -> list[dict]:
    """Every source passage behind an entry, with the transcript excerpt."""
    out = []
    for r in conn.execute(
            "SELECT es.*, s.type AS source_type, s.journal_date AS source_date "
            "FROM entry_sources es JOIN sources s ON s.id=es.source_id "
            "WHERE es.entry_id=?", (entry_id,)):
        item = {"source_id": r["source_id"], "source_type": r["source_type"],
                "source_date": r["source_date"], "char_start": r["char_start"],
                "char_end": r["char_end"], "segment_start": r["segment_start"],
                "segment_end": r["segment_end"]}
        if r["transcript_id"] is not None and r["char_start"] is not None:
            t = conn.execute("SELECT text FROM transcripts WHERE id=?",
                             (r["transcript_id"],)).fetchone()
            if t:
                item["transcript_excerpt"] = t["text"][r["char_start"]:r["char_end"]]
        elif r["transcript_id"] is not None and r["segment_start"] is not None:
            segs = conn.execute(
                "SELECT text FROM transcript_segments WHERE transcript_id=? "
                "AND idx BETWEEN ? AND ? ORDER BY idx",
                (r["transcript_id"], r["segment_start"], r["segment_end"])).fetchall()
            item["transcript_excerpt"] = " ".join(s["text"] for s in segs)
        out.append(item)
    return out


def entry_detail(conn: sqlite3.Connection, entry_uuid: str) -> dict:
    """An entry with categories, entities, measurements, provenance,
    revisions and links. Raises DomainError for an unknown uuid."""
    from .domain import entries as entries_dom
    from .domain import links as links_dom
    row = entries_dom.get_by_uuid(conn, entry_uuid)
    snap = entries_dom.snapshot(conn, row["id"])
    snap["provenance"] = entry_provenance(conn, row["id"])
    snap["revisions"] = [dict(r) for r in conn.execute(
        "SELECT revised_at, actor, change_kind, note FROM entry_revisions "
        "WHERE entry_uuid=? ORDER BY id", (entry_uuid,))]
    snap["links"] = links_dom.for_entry(conn, entry_uuid)
    return snap
