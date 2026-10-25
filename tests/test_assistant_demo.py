"""The demo persona gives the assistant something to say on every combo: a
task due in a few days, a birthday coming up, the agenda — and the same pack
twice is the same pack."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lifecrm import db as db_mod
from lifecrm import nudges as mod
from lifecrm.demo import persona
from lifecrm.domain.util import now_utc
from test_ask_demo import ANCHOR, build_world, make_cfg

NOW = datetime(ANCHOR.year, ANCHOR.month, ANCHOR.day, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module", params=persona.COMBOS)
def world(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"assistant-{request.param}")
    cfg = make_cfg(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    info = build_world(conn, cfg, root, combo=request.param)
    yield conn, cfg, info, request.param
    conn.close()


def test_the_persona_has_a_due_task_a_birthday_and_an_agenda(world):
    conn, cfg, info, combo = world
    pack = mod.build(conn, cfg, today=ANCHOR, now=NOW)
    titles = [n["title"] for n in pack["nudges"]]
    assert "Due in 3 days (2026-10-24): Book flights to Ljubljana" in titles
    assert ("Sam Okafor's birthday is in 4 days (2026-10-25), turning "
            f"{ANCHOR.year - 1992}") in titles
    kinds = [n["kind"] for n in pack["nudges"]]
    assert kinds == sorted(kinds, key=mod.KIND_ORDER.index)
    # Maya's birthday was nine days ago: next year's is not within 14 days;
    # the owner's own card never becomes a birthday nudge
    birthdays = [n["title"] for n in pack["nudges"] if n["kind"] == "birthday"]
    assert birthdays == [f"Sam Okafor's birthday is in 4 days (2026-10-25), turning "
                         f"{ANCHOR.year - 1992}"]
    assert not any(persona.OWNER.name in t for t in titles)
    # Maya's late text five days before the anchor got no reply in the records
    follow = [n for n in pack["nudges"] if n["kind"] == "follow_up"]
    assert any("Maya Chen's text" in n["title"] for n in follow), [n["title"] for n in follow]
    # the agenda is exactly the calendar rows of today and tomorrow (the test
    # world stops its calendar at the anchor; the full demo has tomorrow's 1:1)
    rows = [r["title"] for r in conn.execute(
        "SELECT title FROM observations WHERE stream='calendar_event' AND status='active' "
        "AND journal_date IN (?, ?) ORDER BY started_at, id",
        (ANCHOR.isoformat(), "2026-10-22"))]
    assert [a["title"] for a in pack["agenda"]] == rows
    assert all(persona.OWNER.name not in a["with"] for a in pack["agenda"])
    assert pack["hidden"] == []
    # what could not be judged says so, instead of guessing
    assert all("insufficient coverage" in n or "not judged" in n for n in pack["notes"]), \
        pack["notes"]


def test_the_pack_is_deterministic_and_checkable(world):
    conn, cfg, info, combo = world
    a = mod.build(conn, cfg, today=ANCHOR, now=NOW)
    b = mod.build(conn, cfg, today=ANCHOR, now=NOW)
    assert a["pack_id"] == b["pack_id"] and a == b
    flights = next(n for n in a["nudges"] if "Ljubljana" in n["title"])
    brief = f"Book the Ljubljana flights by 2026-10-24, 3 days out [{flights['id']}]."
    r = mod.check(brief, a)
    assert r["cited"] == [flights["id"]] and not r["unsupported"] and not r["unknown"]
    wrong = mod.check(f"Book the Ljubljana flights by 2026-10-25 [{flights['id']}].", a)
    assert wrong["unsupported"]
