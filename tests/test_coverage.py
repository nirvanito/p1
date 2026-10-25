"""Declared import coverage (schema v7): a day is known-empty only where an
import declared completeness for that stream and interval from a guarantee in
the source — never from the rows' own dates.

The load-bearing claims: a declaration is validated and stored with the
import, counts only while the import is applied, and is read as a set of
days clipped to a window; the connectors with a source guarantee (an OFX
statement range, a full SMS backup, a whole Messages store, a full-history
fitness export) declare it and the others declare nothing.
"""

from __future__ import annotations

import pytest

from lifecrm import db as db_mod
from lifecrm.connectors import android_sms, finance, fitness, ios_messages
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.export import export_jsonl
from test_android_sms import make_backup
from test_finance_ofx import checking, v1, write
from test_fitness import STRONG, export

OWNER = "+15125550100"
JAKE = "+15125550142"


def _scan(records, coverage=None, account=OWNER, connector="cover_fixture"):
    return {"connector": connector, "account": account, "source_file": "fixture",
            "source_hash": "c" * 64, "records": records,
            "handles": [{"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
                        {"kind": "phone", "value": JAKE, "display_name": "Jake Morales"}],
            "coverage": coverage or []}


def _text(n, day, direction="in"):
    other, me = JAKE, OWNER
    return {"stream": "imessage", "external_id": f"t-{n}", "journal_date": day,
            "started_at": f"{day}T17:00:00Z", "direction": direction, "body": "hi",
            "participants": [{"kind": "phone", "value": other if direction == "in" else me,
                              "role": "sender"},
                             {"kind": "phone", "value": me if direction == "in" else other,
                              "role": "recipient"}]}


# ---------------------------------------------------------------- schema

def test_schema_sql_carries_migration_0007_verbatim():
    def norm(text):
        return text.replace("\r\n", "\n")
    mig = norm((db_mod.MIGRATIONS_DIR / "0007_import_coverage.sql")
               .read_text(encoding="utf-8-sig"))
    body = mig.split("\n\n", 1)[1]
    assert body in norm(db_mod.SCHEMA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- domain

def test_a_declaration_is_validated_and_stored_with_its_import(conn, cfg):
    cov = [{"stream": "imessage", "start": "2026-10-01", "end": "2026-10-20",
            "basis": "a whole store"}]
    scan = _scan([_text(1, "2026-10-05")], cov)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert result["coverage"] == 1
    row = conn.execute("SELECT * FROM import_coverage").fetchone()
    assert (row["stream"], row["start_date"], row["end_date"], row["account"]) == \
        ("imessage", "2026-10-01", "2026-10-20", OWNER)   # the scan's account, by default
    assert run_checks(conn)["failures"] == 0

    days = obs.coverage_days(conn, ("imessage",), "2026-09-25", "2026-10-03")
    assert days == {"2026-10-01", "2026-10-02", "2026-10-03"}       # clipped to the window
    assert obs.coverage_days(conn, ("sms",), "2026-10-01", "2026-10-20") == set()
    assert obs.coverage_days(conn, ("imessage",), "2026-10-01", "2026-10-02",
                             account="+10000000000") == set()      # another account
    assert len(obs.coverage_days(conn, ("imessage",), "2026-10-01", "2026-10-31",
                                 account=OWNER)) == 20


@pytest.mark.parametrize("bad, msg", [
    ([{"start": "2026-10-01", "end": "2026-10-02", "basis": "x"}], "needs a stream"),
    ([{"stream": "imessage", "start": "2026-10-01", "end": "2026-10-02"}], "needs a basis"),
    ([{"stream": "imessage", "start": "Oct 1", "end": "2026-10-02", "basis": "x"}], "YYYY-MM-DD"),
    ([{"stream": "imessage", "start": "2026-10-05", "end": "2026-10-02", "basis": "x"}],
     "ends 2026-10-02 before it starts"),
    ("2026-10-01..2026-10-02", "must be a list"),
])
def test_a_bad_declaration_is_refused_and_nothing_is_written(conn, cfg, bad, msg):
    scan = _scan([_text(1, "2026-10-05")], bad)
    with pytest.raises(DomainError, match=msg):
        obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_coverage_counts_only_while_its_import_is_applied(conn, cfg):
    scan = _scan([_text(1, "2026-10-05")],
                 [{"stream": "imessage", "start": "2026-10-01", "end": "2026-10-20",
                   "basis": "a whole store"}])
    uid = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))["import_uuid"]
    conn.commit()
    assert obs.coverage_days(conn, ("imessage",), "2026-10-01", "2026-10-20")
    obs.retract_import(conn, uid)
    conn.commit()
    assert obs.coverage_days(conn, ("imessage",), "2026-10-01", "2026-10-20") == set()
    obs.restore_import(conn, uid)
    conn.commit()
    assert len(obs.coverage_days(conn, ("imessage",), "2026-10-01", "2026-10-20")) == 20


def test_two_declarations_union_and_the_archive_carries_them(conn, cfg, tmp_path):
    a = _scan([_text(1, "2026-10-02")], [{"stream": "imessage", "start": "2026-10-01",
                                          "end": "2026-10-05", "basis": "store A"}])
    b = _scan([_text(2, "2026-10-12")], [{"stream": "imessage", "start": "2026-10-10",
                                          "end": "2026-10-12", "basis": "store B"}],
              connector="cover_fixture_b")
    for s in (a, b):
        obs.apply_plan(conn, s, obs.build_plan(conn, s))
    conn.commit()
    days = obs.coverage_days(conn, ("imessage", "sms"), "2026-10-01", "2026-10-31")
    assert days == {f"2026-10-{d:02d}" for d in (1, 2, 3, 4, 5, 10, 11, 12)}
    manifest = export_jsonl(conn, tmp_path / "archive")
    assert manifest["tables"]["import_coverage"] == 2
    text = (tmp_path / "archive" / "import_coverage.jsonl").read_text(encoding="utf-8")
    assert '"basis": "store A"' in text and '"import_uuid"' in text


def test_doctor_sees_a_malformed_declaration(conn, cfg):
    scan = _scan([_text(1, "2026-10-05")])
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.execute("INSERT INTO import_coverage VALUES (1, NULL, 'imessage', '2026-10-01', "
                 "'2026-10-02', '  ')")
    conn.commit()
    status = {c["check"]: c for c in run_checks(conn)["checks"]}
    assert status["import_coverage_dates"]["status"] == "fail"


# ---------------------------------------------------------------- connectors

def test_an_ofx_statement_declares_its_own_range(tmp_path, cfg):
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking())), cfg)
    assert scan["coverage"] == [{
        "stream": "txn", "account": "hccu:8812", "start": "2026-10-01", "end": "2026-10-21",
        "basis": "OFX statement range (BANKTRANLIST DTSTART/DTEND)"}]


def test_a_full_sms_backup_declares_to_the_day_it_was_taken(tmp_path, cfg):
    scan = android_sms.scan(make_backup(tmp_path / "b"), cfg)
    assert {c["stream"] for c in scan["coverage"]} == {"android_sms", "android_mms"}
    c = scan["coverage"][0]
    assert c["end"] == "2026-09-20" and c["start"] <= c["end"]
    assert c["account"] == scan["account"] and 'type="full"' in c["basis"]
    partial = android_sms.scan(make_backup(tmp_path / "p", sms_type="incremental"), cfg)
    assert partial["coverage"] == []


def test_a_fitness_export_declares_its_whole_history(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    assert len(scan["coverage"]) == 1
    c = scan["coverage"][0]
    assert c["stream"] == "workout" and c["start"] == "2026-10-07"
    assert c["end"] >= "2026-10-09" and "full-history" in c["basis"]


def test_a_messages_store_declares_from_its_oldest_message_to_its_date():
    facts = {"oldest_message": "2026-01-03", "newest_message": "2026-10-20"}
    rows = ios_messages.coverage_for(facts, "+15125550100", "2026-10-22", "a backup")
    assert [r["stream"] for r in rows] == ["imessage", "sms", "rcs"]
    assert rows[0]["start"] == "2026-01-03" and rows[0]["end"] == "2026-10-22"
    newest = ios_messages.coverage_for(facts, "+15125550100", None, "a Mac's store")
    assert newest[0]["end"] == "2026-10-20"
    assert ios_messages.coverage_for(facts, None, None, "x") == []          # whose store?
    assert ios_messages.coverage_for({}, "+15125550100", None, "x") == []    # no messages
