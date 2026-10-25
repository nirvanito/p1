"""What a Mac + iPhone audit found, each pinned (2026-10-25):

- a phone set to keep messages for 30 days or a year ages its oldest texts
  out by itself — mirroring must not delete them from the archive, nor
  leave their days "known empty";
- a folder macOS keeps the job out of (Downloads, iCloud Drive) is an empty
  glob to pathlib, so it is listed directly and said once;
- several candidates (an iPad's backup beside the iPhone's) are a question,
  never "not found" — and the backup to use can be named in config;
- SQLite's backup of a database another program holds locked gives up
  instead of retrying forever (the 06:30 job must end);
- the checklist keeps the "Calls from iPhone" / "Messages in iCloud" steps
  until those sources are collected — a file existing is not a setup done;
- python.org's Python without its certificates says how to fix it.
"""

from __future__ import annotations

import os
import sqlite3
import ssl
import urllib.error

import pytest

from lifecrm import collect, nudges
from lifecrm.connectors import base, ios_messages
from lifecrm.domain import collect as collect_dom
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from test_collect import home, _phone_backup  # noqa: F401  (home: the autouse isolation)
from test_ios_messages import make_backup as make_ios_backup

OWNER = "+15125550100"
JAKE = "+15125550142"


def _text(n, day):
    return {"stream": "imessage", "external_id": f"t-{n}", "journal_date": day,
            "started_at": f"{day}T17:00:00Z", "direction": "in", "body": f"hi {n}",
            "participants": [{"kind": "phone", "value": JAKE, "role": "sender"},
                             {"kind": "phone", "value": OWNER, "role": "recipient"}]}


def _store(days, *, ages_out=True):
    records = [_text(i, d) for i, d in days]
    return {"connector": "age_fixture", "account": OWNER, "source_file": "chat.db",
            "source_hash": "h" + "".join(d for _, d in days), "records": records,
            "handles": [{"kind": "phone", "value": OWNER, "display_name": "you"},
                        {"kind": "phone", "value": JAKE, "display_name": "Jake"}],
            "present_ids": [r["external_id"] for r in records],
            "mirror_streams": ["imessage"], "mirror_ages_out": ages_out,
            "coverage": [{"stream": "imessage", "start": days[0][1], "end": days[-1][1],
                          "basis": "a whole store"}]}


# ---------------------------------------------------------------- aged out, not deleted

def test_texts_that_age_out_of_the_phone_stay_and_so_do_their_days(conn, cfg):
    first = _store([(1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03"),
                    (4, "2026-09-04"), (5, "2026-09-05")])
    obs.apply_plan(conn, first, obs.build_plan(conn, first))
    conn.commit()
    # a year later the phone holds only from the 3rd on, and the 4th was deleted
    later = _store([(3, "2026-09-03"), (5, "2026-09-05")])
    plan = obs.build_plan(conn, later)
    assert [r["external_id"] for r in plan["removed"]] == ["t-4"]    # a real deletion
    assert plan["aged_out"] == 2 and obs.mass_removal(plan) is None
    lines = base.removal_lines(plan, "no longer on the phone (deleted)", lambda r: "Jake")
    assert any(line.startswith("  kept: 2 older than the oldest message") for line in lines)
    obs.apply_plan(conn, later, plan)
    conn.commit()
    left = {r[0] for r in conn.execute("SELECT external_id FROM observations")}
    assert left == {"t-1", "t-2", "t-3", "t-5"}
    # the first import declared the 1st and 2nd complete, and their texts are there
    assert {"2026-09-01", "2026-09-02"} <= obs.coverage_days(
        conn, ("imessage",), "2026-09-01", "2026-09-05")


def test_a_store_that_does_not_age_out_mirrors_everything_as_before(conn, cfg):
    first = _store([(1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03")],
                   ages_out=False)
    obs.apply_plan(conn, first, obs.build_plan(conn, first))
    conn.commit()
    plan = obs.build_plan(conn, _store([(3, "2026-09-03")], ages_out=False))
    assert sorted(r["external_id"] for r in plan["removed"]) == ["t-1", "t-2"]
    assert "aged_out" not in plan


def test_the_three_message_stores_declare_that_they_age_out(cfg, tmp_path):
    from test_android_sms import make_backup as make_sms_backup
    from lifecrm.connectors import android_sms
    assert android_sms.scan(make_sms_backup(tmp_path / "sms"), cfg)["mirror_ages_out"]
    backup = make_ios_backup(tmp_path / "ios")
    assert ios_messages.scan(backup, cfg)["mirror_ages_out"]
    import inspect
    from lifecrm.connectors import mac_messages
    assert "mirror_ages_out=True" in inspect.getsource(mac_messages.scan)


# ---------------------------------------------------------------- folders macOS hides

def _refuse(monkeypatch, folder):
    real = os.listdir

    def listdir(path="."):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(folder)):
            raise PermissionError(1, "Operation not permitted", os.fspath(path))
        return real(path)
    monkeypatch.setattr(os, "listdir", listdir)


def test_a_downloads_folder_macos_hides_is_said_once_and_goes_when_it_may_look(
        conn, cfg, home, monkeypatch):
    downloads = home / "Downloads"
    downloads.mkdir()
    with monkeypatch.context() as refusal:   # undone on leaving; the home stays isolated
        _refuse(refusal, downloads)
        assert collect.refused_folders() == {"downloads": downloads}
        by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
        assert by["downloads"]["outcome"] == "error" and by["downloads"]["blocked"]
        assert "Full Disk Access" in by["downloads"]["reason"]
        # sources that list Downloads themselves say nothing of their own
        assert {r["source"] for r in by.values() if r["outcome"] == "error"} == {"downloads"}
        pack = nudges.build(conn, cfg, today="2026-09-21")
        (n,) = [x for x in pack["nudges"] if x["kind"] == "collect"]
        assert n["key"] == "collect:macos:blocked"
        assert n["title"] == ("macOS is keeping the daily import out of the Downloads "
                              "folder. Give Full Disk Access?")
        items = {it["source"]: it for it in
                 collect.setup_checklist(conn, cfg, "iphone-mac")["items"]}
        assert items["health"]["state"] == "blocked"

    assert collect.refused_folders() == {}           # access granted
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["downloads"]["outcome"] == "unchanged"
    assert collect_dom.latest(conn)["downloads"]["outcome"] == "unchanged"
    assert not [x for x in nudges.build(conn, cfg, today="2026-09-21")["nudges"]
                if x["kind"] == "collect"]
    assert "downloads" not in {r["source"] for r in collect.run(conn, cfg)["sources"]}


def test_a_named_source_that_looked_where_macos_said_no_is_blocked(
        conn, cfg, home, monkeypatch):
    downloads = home / "Downloads"
    downloads.mkdir()
    _refuse(monkeypatch, downloads)
    from lifecrm import health_import
    monkeypatch.setattr(health_import, "search_dirs", lambda: [downloads])
    (r,) = collect.run(conn, cfg, ["health"])["sources"]
    assert r["outcome"] == "error" and r["blocked"] and "Full Disk Access" in r["reason"]


def test_a_hidden_folder_can_be_declined(conn, cfg, home, monkeypatch):
    downloads = home / "Downloads"
    downloads.mkdir()
    _refuse(monkeypatch, downloads)
    collect.decline(conn, "downloads")
    assert "downloads" not in {r["source"] for r in collect.run(conn, cfg)["sources"]}


# ---------------------------------------------------------------- several candidates

def test_two_iphone_backups_are_a_question_and_config_names_one(conn, cfg, home):
    root = home / "Apple" / "MobileSync" / "Backup"
    iphone = make_ios_backup(root, udid="00008110-IPHONE", device="My iPhone")
    make_ios_backup(root, udid="00008110-IPAD", device="My iPad")
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    r = by["ios_messages"]
    assert r["outcome"] == "error" and "2 iPhone backups found" in r["reason"]
    assert "[connectors.ios_messages]" in r["reason"] and "collect decline" in r["reason"]

    cfg.connectors = {"ios_messages": {"path": str(iphone)}}
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert (by["ios_messages"]["outcome"], by["ios_messages"]["hold"]) == \
        ("held", "first_import")
    cfg.connectors = {"ios_messages": {"path": str(root / "nope")}}
    with pytest.raises(DomainError, match=r"\[connectors\.ios_messages\] path"):
        ios_messages.default_target(cfg)


# ---------------------------------------------------------------- a locked database

def test_a_backup_of_a_locked_database_gives_up(tmp_path, monkeypatch):
    db = tmp_path / "locked.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO t VALUES (1)")
    monkeypatch.setattr(base, "LOCKED_TRIES", 2)
    source = sqlite3.connect(db, timeout=0.05)
    copy = sqlite3.connect(tmp_path / "copy.db")
    try:
        with pytest.raises(base.SourceLocked, match="stayed locked by another program"):
            base.sqlite_backup(source, copy, db)
    finally:
        copy.close()
        source.close()
        holder.execute("ROLLBACK")
        holder.close()


# ---------------------------------------------------------------- the checklist

def test_found_is_not_set_up_for_calls_and_messages(conn, cfg, home):
    store = home / "Library" / "Application Support" / "CallHistoryDB"
    store.mkdir(parents=True)
    from test_ios_calls import make_store
    make_store(store / "CallHistory.storedata")
    from lifecrm import platforms
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    try:
        mp.setattr(platforms, "SYSTEM", "mac")
        items = {it["source"]: it for it in
                 collect.setup_checklist(conn, cfg, "iphone-mac")["items"]}
    finally:
        mp.undo()
    assert items["ios_calls"]["state"] == "waiting"
    assert any("Calls from iPhone" in s for s in items["ios_calls"]["steps"])


# ---------------------------------------------------------------- python.org's Python

def test_places_fetch_names_the_certificate_fix(cfg):
    from lifecrm import places

    def opener(url):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    with pytest.raises(DomainError, match="Install Certificates.command"):
        places.fetch(cfg, opener=opener)
