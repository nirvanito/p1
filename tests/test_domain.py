import sqlite3
from datetime import datetime, timezone

import pytest

from lifecrm.domain import days, entries, measurements
from lifecrm.domain.util import DomainError


# ---------------------------------------------------------------- journal dates

def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_rollover_late_evening_is_same_day():
    # 22:30 CDT on Jul 27
    assert days.journal_date_for(_utc(2026, 7, 28, 3, 30), "America/Chicago", 5) == "2026-07-27"


def test_rollover_after_midnight_is_previous_day():
    # 02:00 CDT on Jul 28 -> still Jul 27's journal
    assert days.journal_date_for(_utc(2026, 7, 28, 7, 0), "America/Chicago", 5) == "2026-07-27"


def test_rollover_morning_is_new_day():
    # 06:00 CDT on Jul 28
    assert days.journal_date_for(_utc(2026, 7, 28, 11, 0), "America/Chicago", 5) == "2026-07-28"


def test_rollover_respects_timezone():
    # Same instant, different timezone -> different journal date
    instant = _utc(2026, 7, 28, 7, 0)
    assert days.journal_date_for(instant, "America/Chicago", 5) == "2026-07-27"
    assert days.journal_date_for(instant, "Europe/Berlin", 5) == "2026-07-28"


# ---------------------------------------------------------------- entries

def test_create_entry_with_categories(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="Lunch", primary_category="food",
                       secondary_categories=["social"])
    snap = entries.snapshot(conn, e["id"])
    assert snap["categories"]["primary"] == "food"
    assert snap["categories"]["secondary"] == ["social"]


def test_exactly_one_primary_enforced_in_domain(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="X", primary_category="food",
                       secondary_categories=["food", "social", "social"])
    snap = entries.snapshot(conn, e["id"])
    # primary deduped out of secondary; duplicates collapsed
    assert snap["categories"]["primary"] == "food"
    assert snap["categories"]["secondary"] == ["social"]


def test_primary_index_backstop(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="X", primary_category="food")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO entry_categories (entry_id, category_id, is_primary) "
            "SELECT ?, id, 1 FROM categories WHERE name='work'", (e["id"],))


def test_future_plan_entry(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="plan",
                       title="Book October flights", primary_category="travel",
                       due_at="2026-10-01")
    snap = entries.snapshot(conn, e["id"])
    assert snap["due_at"] == "2026-10-01"
    assert snap["journal_date"] == "2026-07-27"   # journal date stays historical


def test_unknown_kind_rejected(conn):
    with pytest.raises(DomainError, match="unknown entry kind"):
        entries.create(conn, journal_date="2026-07-27", kind="happening",
                       title="X", primary_category="food")


def test_extensible_kind_without_migration(conn):
    from lifecrm.domain.util import now_utc
    conn.execute("INSERT INTO entry_kinds (name, created_at) VALUES ('dream', ?)",
                 (now_utc(),))
    e = entries.create(conn, journal_date="2026-07-27", kind="dream",
                       title="Flying", primary_category="other")
    assert entries.snapshot(conn, e["id"])["kind"] == "dream"


def test_user_created_entry_is_confirmed(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="X", primary_category="food", actor="user")
    assert entries.get_by_uuid(conn, e["uuid"])["confirmed_at"] is not None


def test_extraction_entry_unconfirmed_unless_flagged(conn):
    e = entries.create(conn, journal_date="2026-07-27", kind="event",
                       title="X", primary_category="food", actor="extraction")
    assert entries.get_by_uuid(conn, e["uuid"])["confirmed_at"] is None


# ---------------------------------------------------------------- measurements

def test_weight_normalized_to_kg(conn):
    m = measurements.add(conn, journal_date=None, entry_id=_entry(conn),
                         metric="weight", numeric_value=154, unit="lb")
    assert m["normalized_unit"] == "kg"
    assert abs(m["normalized_value"] - 69.85) < 0.1


def test_duration_normalized_to_minutes(conn):
    m = measurements.add(conn, entry_id=_entry(conn), metric="duration",
                         numeric_value=1.5, unit="h")
    assert m["normalized_value"] == 90
    assert m["normalized_unit"] == "min"


def test_out_of_range_rejected(conn):
    with pytest.raises(DomainError, match="range"):
        measurements.add(conn, entry_id=_entry(conn), metric="mood", numeric_value=15)


def test_unknown_unit_rejected(conn):
    with pytest.raises(DomainError, match="unknown unit"):
        measurements.add(conn, entry_id=_entry(conn), metric="distance",
                         numeric_value=3, unit="furlongs")


def test_open_ended_metric_allowed(conn):
    m = measurements.add(conn, journal_date="2026-07-27", metric="focus_score",
                         numeric_value=42)
    assert m["uuid"]


def test_text_measurement(conn):
    measurements.add(conn, journal_date="2026-07-27", metric="dream_theme",
                     text_value="flying over water")


def test_exactly_one_owner(conn):
    with pytest.raises(DomainError, match="exactly one owner"):
        measurements.add(conn, entry_id=1, journal_date="2026-07-27",
                         metric="mood", numeric_value=5)
    with pytest.raises(DomainError, match="exactly one owner"):
        measurements.add(conn, metric="mood", numeric_value=5)


def _entry(conn) -> int:
    days.ensure_day(conn, "2026-07-27")
    return entries.create(conn, journal_date="2026-07-27", kind="event",
                          title="host", primary_category="health")["id"]
