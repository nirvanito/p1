"""The demo's Google Takeout: the persona's searches and YouTube watches, as a
JSON Takeout (android) and an HTML one (iphone), through the real connector
and the real CLI.

The story has to come through on the right days — the passport search the
evening it went on the list, Mom's knee surgery after the call, the Bled trip
four years back — and both formats have to land as the same records.
"""

import json
import zipfile
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import google_activity as ga
from lifecrm.demo import persona
from lifecrm.demo.sources import google_activity as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

runner = CliRunner()
ANCHOR = date(2026, 10, 21)


def day(offset):
    return persona.day(ANCHOR, offset).isoformat()


@pytest.fixture(params=["android", "iphone"])
def demo(request, tmp_path, cfg):
    return request.param, ga.scan(sample.write_sample(tmp_path, ANCHOR, request.param), cfg)


def _searches(scan):
    return {r["title"]: r for r in scan["records"] if r["stream"] == "web_search"}


def _watches(scan):
    return {r["title"]: r for r in scan["records"] if r["stream"] == "video_watch"}


# ---------------------------------------------------------------- the story

def test_the_persona_searches_land_on_the_days_of_the_story(demo):
    _, scan = demo
    got = _searches(scan)
    for query in ("lake bled pletna boat", "vintgar gorge opening hours",
                  "veracruz all natural menu", "how to renew passport usps",
                  "board deck customer story template", "uchi austin reservations",
                  "knee surgery recovery time", "climbing gym austin new"):
        assert query in got, query
    assert got["how to renew passport usps"]["journal_date"] == day(-40)   # put on the list
    assert got["veracruz all natural menu"]["journal_date"] == day(-38)    # Jake's text
    assert got["knee surgery recovery time"]["journal_date"] == day(-34)   # the call with Mom
    assert got["board deck customer story template"]["journal_date"] == day(-27)  # Priya
    assert got["uchi austin reservations"]["journal_date"] == day(-11)     # before -9
    assert got["climbing gym austin new"]["journal_date"] == day(0)        # Sam's idea
    # 00:38 in the stretched-thin week belongs to the evening before.
    assert got["how to fall asleep when stressed"]["journal_date"] == day(-5)
    assert got["how to fall asleep when stressed"]["started_at"][11:13] == "05"


def test_the_bled_trip_searches_are_four_years_back(demo):
    _, scan = demo
    trip = [d.isoformat() for d in persona.bled_trip_days(ANCHOR)]
    got = _searches(scan)
    assert got["ljubljana to bled bus"]["journal_date"] == trip[0]
    assert got["vintgar gorge opening hours"]["journal_date"] == trip[1]
    assert got["lake bled pletna boat"]["journal_date"] == trip[2]
    castle = got["Bled Castle"]
    assert castle["journal_date"] == trip[0]
    assert json.loads(castle["value_json"]) == {
        "product": "Maps", "url": "https://www.google.com/maps/search/Bled+Castle/"}
    assert castle["origin_app"] == "google_maps"
    # 13:18 at the lake is 11:18 UTC (CEST).
    assert got["lake bled pletna boat"]["started_at"] == f"{trip[2]}T11:18:02Z"


def test_youtube_watches_follow_the_climbing_the_deck_and_the_return_trip(demo):
    _, scan = demo
    got = _watches(scan)
    climb = got["Bouldering Technique: Footwork Drills for V3 and Up"]
    assert (climb["journal_date"], climb["body"]) == (day(-31), "Crag Notes")
    talk = got["How to Tell a Customer Story in a Board Deck"]
    assert (talk["journal_date"], talk["body"]) == (day(-28), "Founder Talks")
    vlog = got["Lake Bled, Slovenia in 4K — Travel Vlog"]
    assert vlog["journal_date"] == day(-16)
    detail = json.loads(vlog["value_json"])
    assert detail == {"product": "YouTube", "channel": "Wander Slowly",
                      "channel_url": sample.channel_url("Wander Slowly"),
                      "url": sample.video_url("Lake Bled, Slovenia in 4K — Travel Vlog")}
    assert json.loads(got["Be Sweet"]["value_json"])["product"] == "YouTube Music"
    assert got["Be Sweet"]["origin_app"] == "youtube_music"
    assert len(got) == 6
    youtube_searches = {t for t, r in _searches(scan).items()
                        if json.loads(r["value_json"])["product"] == "YouTube"}
    assert youtube_searches == {"bouldering technique", "board deck storytelling",
                                "lake bled travel vlog"}


def test_what_the_connector_skips_is_in_the_sample_too(demo):
    combo, scan = demo
    expected = [
        "1 × Maps: Directions to — neither a search nor a YouTube watch",
        "1 × YouTube: ad (From Google Ads)",
        "1 × YouTube: watch of a video since removed (no link left)",
        "2 × Discover: feed activity — not imported",
        "2 × Search: Visited a page — browsing is the browser connector's (docs/browser.md)",
    ]
    if combo == "android":
        expected += ["1 × Assistant: voice activity — not imported",
                     "2 × Chrome: Visited a page — browsing is the browser connector's "
                     "(docs/browser.md)"]
    assert sorted(scan["skipped"]) == sorted(expected)
    assert [u["what"] for u in scan["unmapped"]] == [
        "archive_browser.html — not My Activity or YouTube history",
        "file(s) in YouTube and YouTube Music/subscriptions/ — not My Activity or "
        "YouTube history"]


def test_the_two_combos_differ_in_format_not_in_what_they_say(tmp_path, cfg):
    a = ga.scan(sample.write_sample(tmp_path / "a", ANCHOR, "android"), cfg)
    i = ga.scan(sample.write_sample(tmp_path / "i", ANCHOR, "iphone"), cfg)
    assert a["facts"]["formats"] == {"json": 8} and i["facts"]["formats"] == {"html": 6}
    assert a["records"] == i["records"] and len(a["records"]) == 25
    assert "My Activity: Chrome" in a["facts"]["files"]
    assert "My Activity: Chrome" not in i["facts"]["files"]


def test_the_sample_is_a_takeout_zip_with_takeouts_own_paths(tmp_path):
    for combo, ext in (("android", "json"), ("iphone", "html")):
        z = sample.write_sample(tmp_path / combo, ANCHOR, combo)
        assert z.name == "takeout-20261021T190000Z-001.zip"
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
            assert f"Takeout/My Activity/Search/MyActivity.{ext}" in names
            assert f"Takeout/YouTube and YouTube Music/history/watch-history.{ext}" in names
            assert f"Takeout/YouTube and YouTube Music/history/search-history.{ext}" in names
            body = zf.read(f"Takeout/My Activity/Search/MyActivity.{ext}").decode("utf-8")
        if ext == "json":
            first = json.loads(body)[0]                      # newest first
            assert first["title"] == "Searched for climbing gym austin new"
            assert first["time"].endswith("Z") and "." in first["time"]
        else:
            assert 'class="outer-cell' in body and " PM CDT" in body
            assert "Searched for&nbsp;<a href=" in body


def test_the_sample_is_deterministic_and_follows_the_anchor(tmp_path, cfg):
    a = sample.write_sample(tmp_path / "a", ANCHOR, "iphone").read_bytes()
    b = sample.write_sample(tmp_path / "b", ANCHOR, "iphone").read_bytes()
    assert a == b
    later = ga.scan(sample.write_sample(tmp_path / "c", date(2026, 12, 1), "android"), cfg)
    assert _searches(later)["climbing gym austin new"]["journal_date"] == "2026-12-01"


def test_an_unknown_combo_has_no_sample(tmp_path):
    with pytest.raises(ValueError, match="no sample for combo 'linux'"):
        sample.write_sample(tmp_path, ANCHOR, "linux")


@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_the_demo_applies_once_then_is_identical_and_doctor_stays_green(
        tmp_path, cfg, conn, combo):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entry_sources", "extraction_runs", "entity_handles")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    path = sample.write_sample(tmp_path / "exp", ANCHOR, combo)
    scan = ga.scan(path, cfg)
    first = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert first["added"] == 25
    plan = obs.build_plan(conn, ga.scan(path, cfg))
    assert plan["identical"] == 25 and not plan["new"] and not plan["conflicts"]
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_then_apply_then_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    z = sample.write_sample(tmp_path / "exp", ANCHOR, "android")
    dry = runner.invoke(app, ["observe", "google_activity", str(z)])
    assert dry.exit_code == 0, dry.output
    assert "web_search: 19 new" in dry.output and "video_watch: 6 new" in dry.output
    assert "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    applied = runner.invoke(app, ["observe", "google_activity", str(z), "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "25 added" in applied.output
    again = runner.invoke(app, ["observe", "google_activity", str(z), "--apply"])
    assert again.exit_code == 0, again.output
    assert "0 added" in again.output and "25 identical skipped" in again.output
    note = runner.invoke(app, ["observe", "google_activity", str(z)])
    assert "this exact file was already imported" in note.output
    streams = runner.invoke(app, ["sql", "SELECT stream, COUNT(*) c FROM observations "
                                         "GROUP BY stream ORDER BY stream", "--json"])
    assert json.loads(streams.output) == [{"stream": "video_watch", "c": 6},
                                          {"stream": "web_search", "c": 19}]
    listing = runner.invoke(app, ["connectors"])
    assert "google_activity" in listing.output and "web_search" in listing.output


def test_cli_with_no_path_reads_the_newest_takeout_in_downloads(tmp_path, monkeypatch):
    from lifecrm import platforms
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    sample.write_sample(tmp_path / "Downloads", ANCHOR, "iphone")
    dry = runner.invoke(app, ["observe", "google_activity"])
    assert dry.exit_code == 0, dry.output
    assert "takeout-20261021T190000Z-001.zip (Google Takeout, HTML" in dry.output
