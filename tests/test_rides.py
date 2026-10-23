"""Rides connector: Uber's "Download your data" export.

scan() runs against files shaped like Uber's — trips_data.csv with its
' +0000 UTC' times and the epoch written for a pickup that never happened,
eats_order_details.csv with one row per item. The rest is what a trips
importer can get quietly wrong: trips that never happened counted as
rides, Eats deliveries counted twice, an order split into its items, and
a late-night ride landing on the wrong journal day.
"""

import json
import zipfile
from datetime import date, timedelta

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import rides
from lifecrm.demo import persona
from lifecrm.demo.sources import rides as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

HEADER = ("City,Product Type,Trip or Order Status,Request Time,Begin Trip Time,"
          "Begin Trip Lat,Begin Trip Lng,Begin Trip Address,Dropoff Time,Dropoff Lat,"
          "Dropoff Lng,Dropoff Address,Distance (miles),Fare Amount,Fare Currency\n")
HOME = '"1100 E 5th St, Austin, TX 78702, US"'
UCHI = '"Uchi, 801 S Lamar Blvd, Austin, TX 78704, US"'
EPOCH = "1970-01-01 00:00:00 +0000 UTC"


def trip(status="COMPLETED", product="UberX", req="2026-10-12 23:32:00 +0000 UTC",
         begin="2026-10-12 23:38:00 +0000 UTC", drop="2026-10-12 23:55:37 +0000 UTC",
         pickup=HOME, dropoff=UCHI, fare="16.87", currency="USD", city="Austin",
         miles="4.62"):
    return (f"{city},{product},{status},{req},{begin},30.264000,-97.733000,{pickup},"
            f"{drop},30.257700,-97.761600,{dropoff},{miles},{fare},{currency}\n")


TRIPS = (HEADER + trip()
         + trip("CANCELED", req="2026-10-02 01:48:00 +0000 UTC", begin=EPOCH, drop=EPOCH,
                dropoff="", fare="0.00", miles="0.00")
         + trip("DRIVER_CANCELED", req="2026-10-03 01:48:00 +0000 UTC", begin=EPOCH,
                drop=EPOCH)
         + trip("UNFULFILLED", req="2026-10-04 01:48:00 +0000 UTC", begin=EPOCH, drop=EPOCH)
         + trip(product="UberEATS Marketplace", req="2026-10-05 01:48:00 +0000 UTC")
         + trip(product="Comfort", req="2026-10-15 08:30:00 +0000 UTC", begin=EPOCH,
                drop="2026-10-15 08:50:00 +0000 UTC", pickup=UCHI, dropoff=HOME,
                fare="22.10"))

EATS = (
    "Restaurant_Name,Order_Time,Order_Status,Item_Name,Item_quantity,Customizations,"
    "Item_Price,Order_Price\n"
    'Chipotle,2026-10-17 01:40:00 +0000 UTC,completed,Chicken Burrito Bowl,1,'
    '"White Rice, Black Beans",11.45,18.95\n'
    "Chipotle,2026-10-17 01:40:00 +0000 UTC,completed,,,Extra Guac,,18.95\n"
    "Chipotle,2026-10-17 01:40:00 +0000 UTC,completed,Chips & Guacamole,1,,5.35,18.95\n"
    "Veracruz All Natural,2026-10-10 18:00:00 +0000 UTC,completed,Fish Tacos,2,,9.00,21.50\n"
    "Uchi,2026-10-11 01:00:00 +0000 UTC,canceled,Hama Chili,1,,18.00,18.00\n")


def export(tmp_path, trips=TRIPS, eats=EATS, extra=(), name="Uber Data.zip"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        if trips is not None:
            zf.writestr("Uber Data/Rider/trips_data.csv", trips)
        if eats is not None:
            zf.writestr("Uber Data/Eats/eats_order_details.csv", eats)
        for member, text in extra:
            zf.writestr(member, text)
    return path


def one_trip(tmp_path, cfg, **kw):
    p = tmp_path / "trips_data.csv"
    tmp_path.mkdir(parents=True, exist_ok=True)
    p.write_text(HEADER + trip(**kw), encoding="utf-8")
    return rides.scan(p, cfg)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _by(scan, stream):
    return [r for r in scan["records"] if r["stream"] == stream]


# ---------------------------------------------------------------- trips

def test_a_trip_is_titled_by_where_it_went_with_its_fare_and_route(tmp_path, cfg):
    scan = rides.scan(export(tmp_path), cfg)
    uchi = next(r for r in _by(scan, "ride") if r["title"] == "Uber to Uchi")
    assert uchi["body"] == ("from 1100 E 5th St, Austin, TX 78702, US to Uchi, "
                            "801 S Lamar Blvd, Austin, TX 78704, US")
    assert uchi["value"] == 16.87 and uchi["unit"] == "usd"
    assert uchi["started_at"] == "2026-10-12T23:38:00Z"
    assert uchi["ended_at"] == "2026-10-12T23:55:37Z"
    assert uchi["journal_date"] == "2026-10-12"
    assert uchi["origin_app"] == "uber" and uchi["direction"] is None
    assert uchi["participants"] == []
    assert json.loads(uchi["value_json"]) == {
        "product": "UberX", "city": "Austin", "status": "completed", "distance_mi": 4.62,
        "pickup": {"lat": 30.264, "lon": -97.733,
                   "address": "1100 E 5th St, Austin, TX 78702, US"},
        "dropoff": {"lat": 30.2577, "lon": -97.7616,
                    "address": "Uchi, 801 S Lamar Blvd, Austin, TX 78704, US"},
        "requested_at": "2026-10-12T23:32:00Z"}
    assert uchi["external_id"].startswith("uber:")


def test_a_street_address_is_its_own_title(tmp_path, cfg):
    scan = rides.scan(export(tmp_path), cfg)
    assert "Uber to 1100 E 5th St" in {r["title"] for r in _by(scan, "ride")}


def test_a_pickup_written_as_the_epoch_falls_back_to_the_request(tmp_path, cfg):
    """Uber writes 1970-01-01 for a time it does not have; 08:30 UTC is 03:30
    in Austin, before the 05:00 rollover, so the ride is the night before."""
    scan = rides.scan(export(tmp_path), cfg)
    comfort = next(r for r in _by(scan, "ride") if r["value"] == 22.10)
    assert comfort["started_at"] == "2026-10-15T08:30:00Z"
    assert comfort["journal_date"] == "2026-10-14"
    assert "requested_at" not in json.loads(comfort["value_json"])
    cfg.day_rollover_hour = 0
    again = rides.scan(export(tmp_path / "b"), cfg)
    assert next(r for r in _by(again, "ride") if r["value"] == 22.10)[
        "journal_date"] == "2026-10-15"


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-12 23:41:00 +0000 UTC", "2026-10-12T23:41:00Z"),
    ("2026-10-12T23:41:00.000Z", "2026-10-12T23:41:00Z"),
    ("2026-10-12 18:41:00 -0500", "2026-10-12T23:41:00Z"),
    ("2026-10-12 23:41 UTC", "2026-10-12T23:41:00Z"),
    (EPOCH, None),
    ("10/12/2026", None),
])
def test_uber_times_in_the_forms_the_export_writes(raw, expected):
    assert rides.uber_time(raw) == expected


def test_trips_that_never_happened_are_counted_not_imported(tmp_path, cfg):
    scan = rides.scan(export(tmp_path), cfg)
    assert len(_by(scan, "ride")) == 2
    assert "1 × canceled trip (only completed trips are imported)" in scan["skipped"]
    assert "1 × driver canceled trip (only completed trips are imported)" in scan["skipped"]
    assert "1 × unfulfilled trip (only completed trips are imported)" in scan["skipped"]
    assert ("1 × Uber Eats delivery in the trips file (its order comes from the Eats "
            "file)") in scan["skipped"]


def test_unreadable_trips_are_reported(tmp_path, cfg):
    text = (HEADER + trip(req="yesterday", begin="", drop="")
            + trip(begin="2026-10-12 23:38:00 +0000 UTC", drop="2026-10-12 23:00:00 +0000 UTC")
            + trip(req="2026-10-13 23:32:00 +0000 UTC", begin="2026-10-13 23:38:00 +0000 UTC",
                   drop="2026-10-13 23:55:00 +0000 UTC", fare="n/a"))
    p = tmp_path / "trips_data.csv"
    p.write_text(text, encoding="utf-8")
    scan = rides.scan(p, cfg)
    whats = {u["what"]: u for u in scan["unmapped"]}
    assert whats["trip whose Request/Begin Trip Time does not read"]["count"] == 1
    early = whats["trip whose Dropoff Time is before it began (end time dropped)"]
    assert early["example"] == "trips_data.csv:3"
    assert whats["trip whose Fare Amount does not parse (fare left out)"]["count"] == 1
    kept = _by(scan, "ride")
    assert len(kept) == 2
    assert next(r for r in kept if r["value"] is None)["unit"] is None
    assert next(r for r in kept if r["value"] is not None)["ended_at"] is None


def test_a_fare_keeps_its_own_currency(tmp_path, cfg):
    scan = one_trip(tmp_path, cfg, city="Ljubljana", fare='"14,50"', currency="EUR")
    (ride,) = scan["records"]
    assert ride["value"] == 14.5 and ride["unit"] == "eur"


def test_two_identical_requests_stay_two_rides(tmp_path, cfg):
    p = tmp_path / "trips_data.csv"
    p.write_text(HEADER + trip() + trip(), encoding="utf-8")
    ids = [r["external_id"] for r in rides.scan(p, cfg)["records"]]
    assert len(ids) == 2 and len(set(ids)) == 2


# ---------------------------------------------------------------- Eats

def test_an_eats_order_is_one_record_with_its_items(tmp_path, cfg):
    scan = rides.scan(export(tmp_path), cfg)
    orders = {r["title"]: r for r in _by(scan, "food_order")}
    assert set(orders) == {"Chipotle", "Veracruz All Natural"}
    chipotle = orders["Chipotle"]
    assert chipotle["value"] == 18.95 and chipotle["unit"] == "usd"
    assert chipotle["body"] == ("1 × Chicken Burrito Bowl (White Rice, Black Beans, Extra "
                                "Guac), 1 × Chips & Guacamole")
    assert json.loads(chipotle["value_json"]) == {
        "status": "completed",
        "items": [{"name": "Chicken Burrito Bowl", "quantity": 1, "price": 11.45,
                   "customizations": "White Rice, Black Beans, Extra Guac"},
                  {"name": "Chips & Guacamole", "quantity": 1, "price": 5.35}]}
    assert chipotle["journal_date"] == "2026-10-16"           # 20:40 in Austin
    assert chipotle["origin_app"] == "ubereats" and chipotle["participants"] == []
    assert orders["Veracruz All Natural"]["body"] == "2 × Fish Tacos"
    assert ("1 × canceled Uber Eats order (only completed orders are imported)"
            in scan["skipped"])


def test_eats_prices_take_the_trips_currency_or_the_setting(tmp_path, cfg):
    euros = HEADER + trip(fare="14.50", currency="EUR")
    scan = rides.scan(export(tmp_path / "a", trips=euros), cfg)
    assert {r["unit"] for r in _by(scan, "food_order")} == {"eur"}
    scan = rides.scan(export(tmp_path / "b", trips=None), cfg)
    assert {r["unit"] for r in _by(scan, "food_order")} == {"usd"}
    cfg.connectors = {"rides": {"currency": "GBP"}}
    scan = rides.scan(export(tmp_path / "c"), cfg)
    assert {r["unit"] for r in _by(scan, "food_order")} == {"gbp"}
    cfg.connectors = {"rides": {"currency": "pounds"}}
    with pytest.raises(DomainError, match=r"\[connectors.rides\] currency"):
        rides.scan(export(tmp_path / "d"), cfg)


# ---------------------------------------------------------------- targets

def test_any_csv_is_recognised_by_its_header_and_others_are_counted(tmp_path, cfg):
    path = export(tmp_path, extra=(
        ("Uber Data/Rider/trips_data-1.csv", HEADER + trip(req="2026-10-20 01:00:00 +0000 UTC",
                                                           begin="", drop="")),
        ("Uber Data/Account and Profile/profile_data.csv", "Email,First Name\n"),
        ("Uber Data/readme.txt", "hi")))
    scan = rides.scan(path, cfg)
    assert len(_by(scan, "ride")) == 3
    assert ("2 × other file in the export, not read (e.g. Uber Data/Account and "
            "Profile/profile_data.csv)") in scan["skipped"]


def test_an_unzipped_folder_reads_like_the_zip(tmp_path, cfg):
    folder = tmp_path / "Uber Data"
    (folder / "Rider").mkdir(parents=True)
    (folder / "Eats").mkdir()
    (folder / "Rider" / "trips_data.csv").write_text(TRIPS, encoding="utf-8")
    (folder / "Eats" / "eats_order_details.csv").write_text(EATS, encoding="utf-8")
    assert rides.scan(folder, cfg)["records"] == rides.scan(export(tmp_path / "z"), cfg)[
        "records"]


def test_a_file_that_is_not_an_uber_export_says_what_it_saw(tmp_path, cfg):
    p = tmp_path / "rides.csv"
    p.write_text("date,where,cost\n2026-10-01,Uchi,17\n", encoding="utf-8")
    with pytest.raises(DomainError) as e:
        rides.scan(p, cfg)
    assert "not an Uber trips_data.csv or eats_order_details.csv — first row: date, " \
           "where, cost" in str(e.value)
    assert "\n" not in str(e.value)


def test_an_export_with_no_trips_or_orders_lists_what_it_has(tmp_path, cfg):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Uber Data/Account and Profile/profile_data.csv", "Email\n")
    with pytest.raises(DomainError, match="holds no Uber trips or Eats orders .*has: "
                                          "Uber Data/Account and Profile/profile_data.csv"):
        rides.scan(path, cfg)


def test_a_utf8_bom_trips_file_is_read(tmp_path, cfg):
    p = tmp_path / "trips_data.csv"
    p.write_bytes(("﻿" + TRIPS).encode("utf-8"))
    assert len(rides.scan(p, cfg)["records"]) == 2


def test_default_target_is_the_configured_path(tmp_path, cfg):
    with pytest.raises(DomainError, match="rides needs a path"):
        rides.default_target(cfg)
    path = export(tmp_path)
    cfg.connectors = {"rides": {"path": str(path)}}
    assert rides.default_target(cfg) == path
    cfg.connectors = {"rides": {"path": str(tmp_path / "gone.zip")}}
    with pytest.raises(DomainError, match="path does not exist"):
        rides.default_target(cfg)


def test_scan_is_deterministic(tmp_path, cfg):
    path = export(tmp_path)
    a, b = rides.scan(path, cfg), rides.scan(path, cfg)
    assert a["records"] == b["records"] and a["handles"] == b["handles"] == []
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_spans_and_money(tmp_path, cfg, conn):
    scan = rides.scan(export(tmp_path), cfg)
    text = "\n".join(rides.report_lines(scan, obs.build_plan(conn, scan)))
    assert "ride: 2 new, 0 changed | 2026-10-12..2026-10-14" in text
    assert "food_order: 2 new, 0 changed | 2026-10-10..2026-10-16" in text
    assert "usd: 38.97 in fares across these rows" in text
    assert "usd: 40.45 in orders across these rows" in text
    assert "rides: Uber Data.zip (trips_data.csv: 2 trip(s), eats_order_details.csv: " \
           "2 order(s))" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    path = export(tmp_path)
    _, first = _apply(conn, rides.scan(path, cfg))
    assert first["added"] == 4
    plan, again = _apply(conn, rides.scan(path, cfg))
    assert again["added"] == 0 and again["identical"] == 4
    assert plan["conflicts"] == [] and plan["removed"] == []
    text = "\n".join(rides.report_lines(rides.scan(path, cfg), plan))
    assert "nothing new — the export matches the database" in text


def test_a_later_export_removes_nothing(tmp_path, cfg, conn):
    _apply(conn, rides.scan(export(tmp_path / "a"), cfg))
    later = rides.scan(export(tmp_path / "b", trips=HEADER + trip(), eats=None), cfg)
    assert "present_ids" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == [] and plan["identical"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 4


def test_a_fare_changed_later_is_a_conflict_never_an_overwrite(tmp_path, cfg, conn):
    _apply(conn, one_trip(tmp_path / "a", cfg))
    plan, _ = _apply(conn, one_trip(tmp_path / "b", cfg, fare="12.00"))
    assert len(plan["conflicts"]) == 1 and plan["updates"] == []
    assert conn.execute("SELECT value FROM observations").fetchone()["value"] == 16.87


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    _apply(conn, rides.scan(export(tmp_path), cfg))
    for t in ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entity_handles"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] == 0, t


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    _apply(conn, rides.scan(export(tmp_path), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- the demo sample

ANCHOR = date(2026, 10, 21)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_rides_follow_the_personas_story(tmp_path, cfg, combo):
    scan = rides.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg)
    got = {r["title"]: r for r in _by(scan, "ride")}
    day = lambda n: persona.day(ANCHOR, n).isoformat()      # noqa: E731
    assert got["Uber to Uchi"]["journal_date"] == day(persona.MAYA_BIRTHDAY_OFFSET)
    airport = got["Uber to Austin-Bergstrom International Airport (AUS)"]
    first_trip_day = persona.bled_trip_days(ANCHOR)[0]
    assert airport["journal_date"] == (first_trip_day - timedelta(days=1)).isoformat()
    assert ANCHOR.year - int(airport["journal_date"][:4]) == 4
    home = got["Uber to 1100 E 5th St"]
    assert home["journal_date"] == day(-20)
    assert json.loads(home["value_json"])["pickup"]["address"].startswith(
        "Veracruz All Natural")
    assert "1 × canceled trip (only completed trips are imported)" in scan["skipped"]
    (order,) = _by(scan, "food_order")
    assert order["title"] == "Chipotle" and order["journal_date"] == day(-5)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_export_applies_cleanly_and_reimports_identical(tmp_path, cfg, conn,
                                                                 combo):
    _, first = _apply(conn, rides.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg))
    assert first["added"] == 4
    plan = obs.build_plan(conn, rides.scan(
        sample.write_sample(tmp_path / "b", ANCHOR, combo), cfg))
    assert plan["identical"] == 4 and plan["new"] == []
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_demo_export_is_deterministic_and_refuses_an_unknown_combo(tmp_path):
    a = sample.write_sample(tmp_path / "a", ANCHOR, "android").read_bytes()
    b = sample.write_sample(tmp_path / "b", ANCHOR, "iphone").read_bytes()
    assert a == b
    with pytest.raises(ValueError, match="rides has no sample for combo 'pager'"):
        sample.write_sample(tmp_path / "c", ANCHOR, "pager")


# ---------------------------------------------------------------- CLI

def _count(where=""):
    r = runner.invoke(app, ["sql", f"SELECT COUNT(*) c FROM observations {where}", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing_and_apply_then_rerun_is_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    path = export(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "rides", str(path)])
    assert r.exit_code == 0, r.output
    assert "ride: 2 new" in r.output and "dry run — nothing written" in r.output
    assert _count() == 0
    r = runner.invoke(app, ["observe", "rides", str(path), "--apply"])
    assert r.exit_code == 0, r.output
    assert "4 added" in r.output
    again = runner.invoke(app, ["observe", "rides", str(path), "--apply"])
    assert "0 added" in again.output and "4 identical skipped" in again.output
    assert _count("WHERE status='active'") == 4
