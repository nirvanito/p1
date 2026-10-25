"""The read-side features' commands: each runs against a real project root,
prints what its module returns, and refuses in one line while its feature
is unchecked.
"""

from __future__ import annotations

import json

from conftest import cli_project
from feature_seed import JAKE, apply, entry, mood, people, rec, sms, txn
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import places
from lifecrm.cli import app
from lifecrm.config import load_config

runner = CliRunner()


def _root(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    try:
        who = people(conn)
        apply(conn, [sms("2026-10-01", "10:00", JAKE, "see https://example.com/a?utm_source=x"),
                     sms("2026-10-02", "10:00", JAKE, "lake soon?", direction="out"),
                     rec("place_visit", "2025-10-20", "12:00", [], title="Lake Bled",
                         value_json={"lat": 46.3625, "lon": 14.0938}),
                     rec("highlight", "2026-09-01", "21:00", [], title="A Book",
                         body="a line worth keeping", value_json={"kind": "highlight"})]
              + [txn(f"2026-{m:02d}-05", "NETFLIX.COM", 15.49) for m in range(1, 11)],
              source="cli")
        entry(conn, "2025-10-20", "event", "Swam in Lake Bled", "exercise",
              mentions=[who["Jake Morales"]])
        for d in range(1, 29):
            mood(conn, f"2026-09-{d:02d}", 5 + d % 3)
        conn.commit()
    finally:
        conn.close()
    places.write_compact(cfg.data_dir / "geo" / "places.tsv",
                         [("Austin", 30.2672, -97.7431, "US", "Texas", 961855),
                          ("Bled", 46.3683, 14.1146, "SI", "Bled", 5100)])
    return root


def _ok(args, *expect):
    r = runner.invoke(app, args)
    assert r.exit_code == 0, (args, r.output)
    for text in expect:
        assert text in r.output, (args, r.output)
    return r.output


def test_every_command_runs_and_refuses_while_unchecked(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch)
    _ok(["onthisday", "--date", "2026-10-20"], "2025-10-20 (1 year(s) ago)",
        "Swam in Lake Bled", "at Lake Bled (near Bled, Slovenia)")
    _ok(["prompts", "--date", "2026-10-01"], "Worth a word in the journal for 2026-10-01")
    _ok(["highlights", "--date", "2026-10-20"], "a line worth keeping")
    _ok(["subscriptions", "--today", "2026-10-21"], "NETFLIX.COM: 15.49 USD monthly")
    _ok(["experiments"], "Experiments as of")
    _ok(["search", "lake"], "match(es) for \"lake\"")
    _ok(["links", "example.com"], "1 record(s) link to the site example.com")
    _ok(["links", "--from", "Jake Morales"], "link(s) Jake Morales sent you")
    _ok(["conversation", "Jake"], "Conversation with Jake Morales")
    _ok(["weekdays", "--days", "60"], "mood by weekday")
    _ok(["places", "status"], "2 towns")
    _ok(["places", "countries"], "Slovenia (SI)")
    _ok(["places", "where", "2025-10-20"], "2025-10-20: Bled, Slovenia")
    _ok(["places", "fetch", "--dry-run"], "would download: https://download.geonames.org/")
    data = json.loads(_ok(["places", "trips", "--json"]))
    assert isinstance(data, list)
    _ok(["review", "--year", "2025", "--json"], '"lens": "year"')
    for key, args in (("on_this_day", ["onthisday"]), ("journal_prompts", ["prompts"]),
                      ("highlights", ["highlights"]), ("subscriptions", ["subscriptions"]),
                      ("experiments", ["experiments"]), ("search", ["search", "lake"]),
                      ("link_memory", ["links", "example.com"]),
                      ("conversations", ["conversation", "Jake"]),
                      ("weekday_profile", ["weekdays"]), ("places", ["places", "trips"]),
                      ("year_review", ["review", "--year", "2025", "--json"])):
        assert runner.invoke(app, ["features", "uncheck", key]).exit_code == 0
        r = runner.invoke(app, args)
        assert r.exit_code == 1 and f"features check {key}" in r.output, (key, r.output)
        assert runner.invoke(app, ["features", "check", key]).exit_code == 0
    r = runner.invoke(app, ["conversation", "Nobody Here"])
    assert r.exit_code == 1 and "no person called 'Nobody Here'" in r.output
