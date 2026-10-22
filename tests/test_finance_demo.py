"""The finance demo sample: the persona's statements, through the real connector.

Both combos must tell the persona's story — the Veracruz tacos, the
paychecks, the Lake Bled trip four years back — through their own bank's
format (Chase CSVs on the Windows + Samsung combo, Apple Card CSVs and a
credit union QFX on the Mac + iPhone one), and come out as the same money.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from lifecrm.connectors import finance
from lifecrm.demo import persona
from lifecrm.demo.sources import finance as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 21)


def _scan(tmp_path, cfg, combo, anchor=ANCHOR):
    return finance.scan(sample.write_sample(tmp_path / combo, anchor, combo), cfg)


def _spend_at(scan, word):
    return sorted((r["journal_date"], r["value"]) for r in scan["records"]
                  if word in (r["title"] or "").upper())


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_veracruz_spend_is_the_personas(tmp_path, cfg, combo):
    scan = _scan(tmp_path, cfg, combo)
    expected = sorted((persona.day(ANCHOR, t.offset).isoformat(), t.amount)
                      for t in persona.TRANSACTIONS if t.merchant == "VERACRUZ ALL NATURAL")
    assert _spend_at(scan, "VERACRUZ") == expected
    assert len(expected) == 3


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_bled_trip_is_four_years_back(tmp_path, cfg, combo):
    scan = _scan(tmp_path, cfg, combo)
    trip = persona.bled_trip_days(ANCHOR)
    flight = next(r for r in scan["records"] if "AIR FRANCE" in r["title"].upper())
    assert flight["journal_date"] == trip[0].isoformat()
    assert flight["value"] == -1124.60
    assert ANCHOR.year - int(flight["journal_date"][:4]) == 4
    boat = next(r for r in scan["records"] if "PLETNA" in r["title"].upper())
    assert boat["journal_date"] == trip[2].isoformat()


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_both_combos_add_up_to_the_same_money(tmp_path, cfg, combo):
    scan = _scan(tmp_path, cfg, combo)
    got = sum(Decimal(str(r["value"])) for r in scan["records"])
    want = sum(Decimal(str(t.amount)) for t in persona.TRANSACTIONS)
    assert got == want
    assert len(scan["records"]) == len(persona.TRANSACTIONS)
    paychecks = [r for r in scan["records"] if r["direction"] == "in"]
    assert len(paychecks) == 2
    assert all(r["title"].startswith("ACME ROBOTICS PAYROLL") for r in paychecks)


def test_android_is_chase_card_and_checking_csvs(tmp_path, cfg):
    scan = _scan(tmp_path, cfg, "android")
    assert sorted(f["format"] for f in scan["files"]) == \
        ["Chase card", "Chase card", "Chase checking"]
    assert scan["handles"] == [
        {"kind": "card", "value": "chase:4417", "display_name": "Chase card ending 4417"},
        {"kind": "card", "value": "chase:8812",
         "display_name": "Chase checking ending 8812"}]
    gym = next(r for r in scan["records"] if r["title"] == "MOVEMENT GYM AUSTIN TX")
    assert gym["body"] == "monthly membership"
    assert json.loads(gym["value_json"])["bank_category"] == "Health & Wellness"


def test_iphone_is_apple_card_csvs_and_a_credit_union_qfx(tmp_path, cfg):
    scan = _scan(tmp_path, cfg, "iphone")
    formats = sorted(f["format"] for f in scan["files"])
    assert formats[-1] == "OFX v1 from HCCU"
    assert set(formats[:-1]) == {"Apple Card"}
    assert [h["value"] for h in scan["handles"]] == ["apple_card", "hccu:8812"]
    tacos = [r for r in scan["records"] if r["title"] == "Veracruz All Natural"]
    assert len(tacos) == 3                               # Apple's Merchant column
    assert json.loads(tacos[0]["value_json"])["raw_description"].startswith(
        "VERACRUZ ALL NATURAL 1704 E CESAR CHAVEZ")


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_applies_cleanly_and_reimports_as_identical(tmp_path, cfg, conn, combo):
    scan = _scan(tmp_path, cfg, combo)
    plan = obs.build_plan(conn, scan)
    assert plan["invalid"] == [] and plan["dup_incoming"] == 0
    first = obs.apply_plan(conn, scan, plan)
    conn.commit()
    assert first["added"] == len(persona.TRANSACTIONS)
    again = obs.build_plan(conn, _scan(tmp_path / "again", cfg, combo))
    assert again["identical"] == len(persona.TRANSACTIONS) and again["new"] == []
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = sample.write_sample(tmp_path / "a", ANCHOR, combo)
    b = sample.write_sample(tmp_path / "b", ANCHOR, combo)
    files_a = {p.name: p.read_bytes() for p in sorted(a.iterdir())}
    files_b = {p.name: p.read_bytes() for p in sorted(b.iterdir())}
    assert files_a == files_b and files_a


def test_the_sample_follows_the_anchor(tmp_path, cfg):
    later = date(2027, 3, 2)
    scan = _scan(tmp_path, cfg, "android", anchor=later)
    newest = max(r["journal_date"] for r in scan["records"])
    assert newest == persona.day(later, -1).isoformat()


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_sample(tmp_path, ANCHOR, "blackberry")
    assert "blackberry" in str(e.value)
