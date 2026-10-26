"""Online sources (lifecrm/online): a service the daily job fetches from by
itself — the secrets it keeps, the fetch into data/fetched/<service>/, and
the very same collect flow after it: a first import asked about, a yes, a
routine morning, a failed fetch said rather than skipped. The network is
never touched: each test hands the service a fake opener.
"""

from __future__ import annotations

import io
import json

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import collect, online
from lifecrm.cli import app
from lifecrm.domain.util import DomainError
from lifecrm.online import google_calendar, secrets
from test_collect import home  # noqa: F401  (the autouse home isolation)

runner = CliRunner()
LINK = "https://calendar.google.com/calendar/ical/you%40gmail.com/private-abc123/basic.ics"


def _ics(*events) -> bytes:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
            "X-WR-CALNAME:you@gmail.com", "X-WR-TIMEZONE:America/Chicago"]
    for uid, start, title in events:
        body += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{start[:-3]}00Z",
                 f"DTSTAMP:20261020T120000Z", f"SUMMARY:{title}", "END:VEVENT"]
    return ("\r\n".join(body + ["END:VCALENDAR", ""])).encode("utf-8")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def served(monkeypatch):
    """What the calendar link returns, and every URL asked for."""
    state = {"body": _ics(("e1@google.com", "20261020T150000Z", "Dentist")), "asked": []}

    def opener(url):
        state["asked"].append(url)
        if isinstance(state["body"], Exception):
            raise state["body"]
        return _Resp(state["body"])
    monkeypatch.setattr(google_calendar, "_open", opener)
    return state


def test_secrets_are_kept_apart_and_forgotten(cfg):
    assert secrets.load(cfg) == {}
    secrets.put(cfg, "a", {"k": 1})
    secrets.update(cfg, "a", cursor=5)
    secrets.put(cfg, "b", {"k": 2})
    assert secrets.load(cfg) == {"a": {"k": 1, "cursor": 5}, "b": {"k": 2}}
    assert secrets.forget(cfg, "a") and not secrets.forget(cfg, "a")
    assert json.loads(secrets.path(cfg).read_text(encoding="utf-8")) == {"b": {"k": 2}}


def test_the_registry_knows_calendar_and_what_it_feeds():
    assert "google_calendar" in online.registry()
    assert "calendar" in online.feeds()
    with pytest.raises(DomainError, match="unknown online service 'nope'"):
        online.get("nope")


def test_connecting_checks_the_link_once_and_keeps_it(cfg, served):
    with pytest.raises(DomainError, match="starts with https://"):
        online.connect(cfg, "google_calendar", ["http://example.com/x.ics"], None)
    served["body"] = b"<html>not a calendar</html>"
    with pytest.raises(DomainError, match="did not return a calendar"):
        online.connect(cfg, "google_calendar", [LINK], None)
    assert secrets.load(cfg) == {}                        # nothing kept on a failure
    served["body"] = _ics(("e1@google.com", "20261020T150000Z", "Dentist"))
    online.connect(cfg, "google_calendar", [], lambda prompt, hide: f" {LINK} ")
    assert secrets.load(cfg)["google_calendar"] == {"urls": [LINK]}
    assert set(online.connected(cfg)) == {"google_calendar"}


def test_collect_fetches_asks_then_collects_every_morning(conn, cfg, served):
    online.connect(cfg, "google_calendar", [LINK], None)
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    cal = by["calendar"]
    assert (cal["outcome"], cal["hold"]) == ("held", "first_import"), cal
    fetched = list(online.dest_dir(cfg, "google_calendar").glob("*.ics"))
    assert len(fetched) == 1 and b"Dentist" in fetched[0].read_bytes()
    assert LINK not in json.dumps(by)                    # the link never shows

    done = collect.approve(conn, cfg, "calendar", apply=True)
    assert done["applied"] and done["applied_counts"]["added"] == 1
    assert _only_calendar(collect.run(conn, cfg))["outcome"] == "unchanged"

    served["body"] = _ics(("e1@google.com", "20261020T150000Z", "Dentist"),
                          ("e2@google.com", "20261021T160000Z", "Climbing with Jake"))
    r = _only_calendar(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 1


def _only_calendar(result):
    return next(r for r in result["sources"] if r["source"] == "calendar")


def test_a_failed_fetch_is_said_never_skipped(conn, cfg, served):
    online.connect(cfg, "google_calendar", [LINK], None)
    served["body"] = OSError("Name or service not known")
    r = _only_calendar(collect.run(conn, cfg))
    assert r["outcome"] == "error" and "calendar link" in r["reason"]


def test_a_dropped_link_drops_its_file(cfg, served):
    secrets.put(cfg, "google_calendar", {"urls": [LINK, LINK + "?2"]})
    dest = online.fetch(cfg, online.get("google_calendar"))
    assert len(list(dest.glob("*.ics"))) == 2
    secrets.put(cfg, "google_calendar", {"urls": [LINK]})
    online.fetch(cfg, online.get("google_calendar"))
    assert len(list(dest.glob("*.ics"))) == 1


def test_the_checklist_lists_online_services(conn, cfg, served):
    data = collect.setup_checklist(conn, cfg, "android")
    cal = next(s for s in data["online"] if s["service"] == "google_calendar")
    assert not cal["connected"] and any("Secret address" in s for s in cal["steps"])
    online.connect(cfg, "google_calendar", [LINK], None)
    data = collect.setup_checklist(conn, cfg, "android")
    cal = next(s for s in data["online"] if s["service"] == "google_calendar")
    assert cal["connected"] and cal["steps"] == []


def test_cli_connect_list_fetch_disconnect(tmp_path, monkeypatch, served):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["connect"])
    assert r.exit_code == 0 and "google_calendar" in r.output and "Secret address" in r.output
    r = runner.invoke(app, ["connect", "google_calendar", LINK])
    assert r.exit_code == 0 and "connected: Google Calendar" in r.output, r.output
    assert LINK not in r.output
    r = runner.invoke(app, ["fetch", "google_calendar"])
    assert r.exit_code == 0 and "nothing imported" in r.output
    r = runner.invoke(app, ["observe", "calendar"])             # no path: the link's file
    assert r.exit_code == 0 and "Dentist" in r.output, r.output
    r = runner.invoke(app, ["collect", "setup", "--phone", "android"])
    assert "[done ] Google Calendar" in r.output
    r = runner.invoke(app, ["disconnect", "google_calendar"])
    assert r.exit_code == 0 and "disconnected" in r.output
