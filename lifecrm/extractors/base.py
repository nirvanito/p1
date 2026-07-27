"""The provider-neutral extraction boundary.

    transcript -> versioned extraction JSON -> validation/review -> persistence

An extractor turns a transcript into a draft contract document
(docs/contract.md). The database and ingestion code depend only on that
document — never on any provider-specific behavior — so alternatives can be
added later without schema changes.

The one extractor in this build is the interactive Claude Code skill
(.claude/skills/log/SKILL.md, interface='claude_skill'): Claude reads the
transcript, resolves entities with `lifecrm entities match`, writes the draft
JSON to data/staging/, and hands it to `lifecrm ingest`. It runs on the
user's existing Claude Code subscription — no metered API calls. Manual
drafting (interface='manual') follows the identical path: any human or tool
that writes a valid contract document is an extractor.

A future non-interactive extractor (e.g. a local model) would subclass
Extractor, build the same document, and reuse `context_for` unchanged.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ExtractionContext:
    """Everything an extractor needs to draft a contract document."""
    journal_date: str
    source_id: int
    transcript_text: str
    segments: list[dict] = field(default_factory=list)   # idx, start_sec, end_sec, char_start, char_end, text
    known_categories: list[str] = field(default_factory=list)
    known_kinds: list[str] = field(default_factory=list)
    known_entities: list[dict] = field(default_factory=list)  # id, type, name, aliases
    contract_version: int = 1


class Extractor(ABC):
    """Interface for anything that drafts contract documents."""

    name: str = "base"
    interface: str = "manual"   # must be a valid extraction_runs.interface value

    @abstractmethod
    def extract(self, ctx: ExtractionContext) -> dict:
        """Return a draft contract document (NOT yet validated or persisted)."""


def context_for(conn: sqlite3.Connection, source_id: int) -> ExtractionContext:
    """Assemble the extraction context for a source from the database."""
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if src is None:
        raise ValueError(f"source id={source_id} does not exist")
    t = conn.execute(
        "SELECT * FROM transcripts WHERE source_id=? ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    if t is None:
        raise ValueError(f"source id={source_id} has no transcript")
    segments = [dict(r) for r in conn.execute(
        "SELECT idx, start_sec, end_sec, char_start, char_end, text "
        "FROM transcript_segments WHERE transcript_id=? ORDER BY idx", (t["id"],))]
    entities = []
    for e in conn.execute(
            "SELECT id, type, name FROM entities WHERE status='active' ORDER BY name"):
        aliases = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (e["id"],))]
        entities.append({"id": e["id"], "type": e["type"], "name": e["name"],
                         "aliases": aliases})
    return ExtractionContext(
        journal_date=src["journal_date"],
        source_id=source_id,
        transcript_text=t["text"],
        segments=segments,
        known_categories=[r["name"] for r in conn.execute(
            "SELECT name FROM categories ORDER BY id")],
        known_kinds=[r["name"] for r in conn.execute(
            "SELECT name FROM entry_kinds ORDER BY id")],
        known_entities=entities,
    )
