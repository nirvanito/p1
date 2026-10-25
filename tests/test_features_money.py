"""Subscriptions and self-experiments.

The load-bearing claims: a charge that repeats monthly at a steady amount is
a subscription and an irregular one is not; a rise over max($1.99, 5 %) is a
price rise and nudges, a new series nudges once; "stopped" is said only
where the statements declared the days complete; `ask` lists them with an
answer that passes its own check. An experiment's shape is refused on every
write path; its two windows are compared over known days only; a short side
says so; it nudges while running and sits in the review pack.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from feature_seed import apply, entry, mood, txn

from lifecrm import experiments, nudges, recall, review, subscriptions
from lifecrm.domain import corrections, entries
from lifecrm.domain.util import DomainError

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)


def _monthly(start: date, n: int, title: str, amounts) -> list[dict]:
    out = []
    for i in range(n):
        d = date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1,
                 start.day)
        amount = amounts[i] if isinstance(amounts, list) else amounts
        out.append(txn(d.isoformat(), f"{title} {1000 + i}", amount))
    return out


@pytest.fixture
def bank(conn, cfg):
    recs = _monthly(date(2026, 1, 5), 10, "NETFLIX.COM 866-579-7172 CA",
                    [15.49] * 9 + [17.99])
    recs += _monthly(date(2026, 8, 12), 3, "SPOTIFY P0A1B2", 11.99)
    # irregular: groceries at random gaps and amounts
    recs += [txn(d, "H-E-B #42", a) for d, a in (("2026-09-01", 81.2), ("2026-09-04", 23.0),
                                                ("2026-09-19", 140.9), ("2026-10-02", 12.5))]
    # a gym that charged until June, statements complete through October
    recs += _monthly(date(2026, 1, 20), 6, "GOLDS GYM", 39.0)
    apply(conn, recs, connector="finance_fixture", account="chase-1234", source="stmt",
          coverage=[{"stream": "txn", "start": "2026-01-01", "end": "2026-10-20",
                     "basis": "OFX statement DTSTART/DTEND"}])
    conn.commit()


def test_steady_monthly_charges_are_subscriptions_and_groceries_are_not(conn, cfg, bank):
    data = subscriptions.detect(conn, cfg, TODAY)
    by = {x["key"]: x for x in data["items"]}
    assert set(by) == {"netflix ca", "spotify", "golds gym"}
    nf = by["netflix ca"]
    assert nf["cadence"] == "monthly" and nf["count"] == 10 and nf["amount"] == 17.99
    assert nf["price_rise"] and nf["previous_amount"] == 15.49 and nf["rise"] == 2.5
    assert nf["status"] == "active" and nf["next_due"] == "2026-11-05"   # same day, next month
    assert subscriptions._next_due(date(2026, 1, 31), "monthly", 30) == date(2026, 2, 28)
    sp = by["spotify"]
    assert sp["new"] and not sp["price_rise"] and sp["first_date"] == "2026-08-12"
    gym = by["golds gym"]
    assert gym["status"] == "stopped" and gym["judged"]        # coverage said so
    assert data["monthly"] == {"USD": round(17.99 + 11.99, 2)}     # the gym stopped
    assert subscriptions.merchant_key("NETFLIX.COM 866-579-7172 CA") == "netflix ca"


def test_without_declared_coverage_a_late_series_is_not_judged(conn, cfg):
    apply(conn, _monthly(date(2026, 1, 20), 6, "GOLDS GYM", 39.0), source="nocover")
    conn.commit()
    gym = subscriptions.detect(conn, cfg, TODAY)["items"][0]
    assert gym["status"] == "overdue" and gym["judged"] is False
    assert "not judged" in subscriptions.describe(gym)


def test_price_rises_and_new_series_nudge(conn, cfg, bank):
    pack = nudges.build(conn, cfg, today=TODAY, now=NOW)
    kinds = {n["kind"]: n for n in pack["nudges"]}
    pr = kinds["price_change"]
    assert pr["title"] == ("NETFLIX.COM 866-579-7172 CA 1009: 17.99 USD charged 2026-10-05, up "
                           "from 15.49 USD on 2026-09-05")
    assert pr["key"] == "price:netflix-ca:2026-10-05" and len(pr["ids"]) == 2
    new = kinds["new_recurring"]
    assert new["key"] == "recurring:spotify" and "a new monthly charge of 11.99 USD" in new["title"]
    order = [n["kind"] for n in pack["nudges"]]
    assert order.index("price_change") < order.index("new_recurring")
    assert not nudges.check(f"Netflix went up to 17.99 USD on 2026-10-05 [{pr['id']}].",
                            pack)["unsupported"]
    # the new kinds snooze and dismiss by key like every other
    from lifecrm.domain import nudges as state
    state.snooze(conn, pr["key"], until="2026-11-01")
    state.dismiss(conn, new["key"])
    conn.commit()
    pack = nudges.build(conn, cfg, today=TODAY, now=NOW)
    assert {n["kind"] for n in pack["hidden"]} >= {"price_change", "new_recurring"}
    assert not {n["kind"] for n in pack["nudges"]} & {"price_change", "new_recurring"}


def test_ask_lists_subscriptions_and_the_finance_lens_holds_them(conn, cfg, bank):
    r = recall.answer(conn, cfg, "what subscriptions do I pay for?", use_llm=False,
                      today=TODAY)
    check = recall.check_citations(r["answer"], r["evidence"])
    assert not check["unknown"] and not check["unsupported"], (r["answer"], check)
    assert "2 charge(s) repeat at a steady rhythm" in r["answer"]
    assert "Series with no recent charge" in r["answer"] and "GOLDS GYM" in r["answer"]
    period = review.resolve_period(cfg, start="2026-10-01", end="2026-10-20", today=TODAY)
    pack = review.build_pack(conn, cfg, period, lens="finance")
    subs = pack["subscriptions"]
    assert {x["merchant"].split()[0] for x in subs["items"]} == {"NETFLIX.COM", "SPOTIFY"}
    assert "## Recurring charges" in review.render_markdown(pack)


# ---------------------------------------------------------------- experiments

def test_experiment_shapes_are_refused_on_every_write_path(conn, cfg):
    def mk(exp, kind="plan"):
        return entries.create(conn, journal_date="2026-10-01", kind=kind, title="Try",
                              primary_category="health", data={"experiment": exp})
    with pytest.raises(DomainError, match="needs kind 'plan'"):
        mk({"change": "x", "outcome": "mood"}, kind="task")
    with pytest.raises(DomainError, match="outcome must be one of"):
        mk({"change": "x", "outcome": "vibes"})
    with pytest.raises(DomainError, match="days must be a whole number of days, 7-90"):
        mk({"change": "x", "outcome": "mood", "days": 3})
    with pytest.raises(DomainError, match="unknown field"):
        mk({"change": "x", "outcome": "mood", "color": "red"})
    ok = mk({"change": "no caffeine after 2pm", "outcome": "sleep_hours"})
    with pytest.raises(DomainError, match="hope must be"):
        corrections.edit_entry(conn, ok["uuid"], {"data": {"experiment": {
            "change": "x", "outcome": "mood", "hope": "sideways"}}})
    with pytest.raises(DomainError, match="needs kind 'plan'"):
        corrections.edit_entry(conn, ok["uuid"], {"kind": "task"})


@pytest.fixture
def trial(conn, cfg):
    start = date(2026, 10, 1)
    for i in range(14):                     # baseline: mood 5
        mood(conn, (start - timedelta(days=14 - i)).isoformat(), 5)
    for i in range(14):                     # during: mood 7, two days unknown
        if i in (3, 9):
            continue
        mood(conn, (start + timedelta(days=i)).isoformat(), 7)
    plan = entry(conn, "2026-09-30", "plan", "Morning walks", "health",
                 data={"experiment": {"change": "a 20-minute walk before work",
                                      "outcome": "mood", "start": "2026-10-01", "days": 14,
                                      "hope": "up"}})
    conn.commit()
    return plan


def test_an_experiment_compares_its_two_windows_over_known_days(conn, cfg, trial):
    [e] = experiments.current(conn, cfg, TODAY)
    assert e["status"] == "done" and e["baseline"]["known"] == 14 and e["during"]["known"] == 12
    assert e["baseline"]["mean"] == 5 and e["during"]["mean"] == 7
    assert e["delta"] == 2 and e["delta_pct"] == 40.0 and e["went"] == "up" and e["as_hoped"]
    line = experiments.describe(e)
    assert line.startswith("a 20-minute walk before work (mood, 2026-10-01 to 2026-10-14, done): "
                           "7/10 over 12 known day(s) since 2026-10-01, against 5/10 over 14 "
                           "known day(s) from 2026-09-17 to 2026-09-30 — up 2/10 (40%)")
    assert line.endswith("co-occurrence over two windows, not cause")
    running = experiments.current(conn, cfg, date(2026, 10, 5))[0]
    assert running["status"] == "running" and running["during"]["known"] == 4
    planned = experiments.current(conn, cfg, date(2026, 9, 25))[0]
    assert planned["status"] == "planned"


def test_a_short_side_is_not_compared(conn, cfg):
    mood(conn, "2026-10-02", 6)
    entry(conn, "2026-10-01", "plan", "Cold showers", "health",
          data={"experiment": {"change": "cold showers", "outcome": "mood", "days": 7}})
    conn.commit()
    [e] = experiments.current(conn, cfg, TODAY)
    assert not e["enough"] and "not enough known days to compare" in experiments.describe(e)


def test_an_experiment_nudges_and_sits_in_every_lens(conn, cfg, trial):
    pack = nudges.build(conn, cfg, today=date(2026, 10, 10), now=NOW)
    exp = next(n for n in pack["nudges"] if n["kind"] == "experiment")
    assert exp["key"] == f"experiment:{trial['uuid']}" and exp["tier"] == "confirmed"
    assert any(i.startswith("M") for i in exp["ids"])            # the measurements behind it
    assert not nudges.check(f"Mood averaged 7/10 since 2026-10-01 [{exp['id']}].",
                            pack)["unsupported"]
    assert all("experiments" in s for lens, s in review.LENS_SECTIONS.items()
               if lens != "all") and "experiments" in review.LENS_SECTIONS["all"]
    period = review.resolve_period(cfg, week="2026-W41", today=TODAY)
    pack = review.build_pack(conn, cfg, period, lens="health")
    [item] = pack["experiments"]["items"]
    # decided as of the day after the week (10-12): the change runs to 10-14
    assert item["status"] == "running" and item["id"].startswith("E")
    later = review.build_pack(conn, cfg, review.resolve_period(cfg, week="2026-W42",
                                                               today=TODAY), lens="week")
    assert later["experiments"]["items"][0]["status"] == "done"
    assert "## Experiments" in review.render_markdown(pack)
    # a closed plan leaves the nudges
    corrections.close_entry(conn, trial["uuid"], "done")
    conn.commit()
    pack = nudges.build(conn, cfg, today=date(2026, 10, 10), now=NOW)
    assert not any(n["kind"] == "experiment" for n in pack["nudges"])
