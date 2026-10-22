"""Mirroring's safety net, as fixed after review.

- A record mirroring hid because something cites it comes back when its
  source holds it again — before, classify ignored status, so it read as
  identical and stayed hidden for good while the relationship kept citing it.
- The mass-removal guard refuses on share in a small archive, and always at
  half — before, nothing under 101 removals was ever refused, so a one-card
  vCard could empty an address book.
- The undo printed after a removal is the pre-removal snapshot — before, it
  was `imports retract`, which cannot bring back a removed row.
- That snapshot survives the next backup the same day — before, it was named
  like a daily snapshot, and the day's newest one (taken after the removal)
  pruned it.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import cli_project, golden_draft
from lifecrm import db as db_mod
from lifecrm.cli import app
from lifecrm.connectors import base
from lifecrm.doctor import run_checks
from lifecrm.domain import entities, entity_links
from lifecrm.domain import observations as obs

runner = CliRunner()


# ---------------------------------------------------------------- helpers

def _scan(ids, *, account="me@example.com", connector="synthetic",
          body="Works at Acme", modified=None, mirror=True):
    s = base.empty_scan(connector, account=account, source_file="cards.vcf",
                        source_hash=f"h-{'-'.join(ids)}-{body}-{account}")
    for i in ids:
        handle = {"kind": "email", "value": f"{i}@example.com"}
        s["records"].append({
            "stream": "contact_card", "external_id": i, "journal_date": "2026-07-27",
            "started_at": "2026-07-27T15:00:00Z", "title": f"Card {i}",
            "body": body, "last_modified_at": modified,
            "participants": [{**handle, "role": "participant"}]})
        s["handles"].append({**handle, "display_name": f"Card {i}"})
    if mirror:
        s["present_ids"] = set(ids)
        s["mirror_streams"] = ["contact_card"]
    return s


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


def _status(conn, external_id):
    return conn.execute("SELECT status FROM observations WHERE external_id=?",
                        (external_id,)).fetchone()["status"]


def _cite(conn, external_id) -> dict:
    """A relationship that cites the card, as `entities link add --observation`."""
    card = conn.execute("SELECT uuid FROM observations WHERE external_id=?",
                        (external_id,)).fetchone()
    jake = entities.create(conn, "person", "Jake Morales")
    acme = entities.create(conn, "organization", "Acme")
    link = entity_links.add(conn, jake["id"], acme["id"], "works_at",
                            observation_uuid=card["uuid"])
    conn.commit()
    return link


def _hide_cited(conn) -> dict:
    """Cards a, b, c; a relationship cites a; then a is deleted at the source."""
    _apply(conn, _scan(["a", "b", "c"]))
    link = _cite(conn, "a")
    _, result = _apply(conn, _scan(["b", "c"]))
    assert result["retracted_cited"] == 1 and _status(conn, "a") == "retracted"
    return link


def _check(conn, cfg, name):
    return next(c for c in run_checks(conn, cfg)["checks"] if c["check"] == name)


# ---------------------------------------------------------------- reappearing

def test_a_cited_record_mirroring_hid_is_active_again_once_the_source_holds_it(
        conn, cfg):
    _hide_cited(conn)
    plan, result = _apply(conn, _scan(["a", "b", "c"]))
    assert [row["external_id"] for row, _ in plan["reappeared"]] == ["a"]
    assert plan["identical"] == 2               # coming back is a change, not a skip
    assert result["reappeared"] == 1
    assert _status(conn, "a") == "active"
    count = conn.execute("SELECT observation_count FROM entity_handles "
                         "WHERE value='a@example.com'").fetchone()[0]
    assert count == 1                            # attributed again
    audit = conn.execute("SELECT details FROM audit_log WHERE action='mirror_restore'"
                         ).fetchone()
    assert json.loads(audit["details"])["external_ids"] == ["a"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_record_that_changed_while_hidden_comes_back_with_the_newer_data(conn):
    _hide_cited(conn)
    plan, result = _apply(conn, _scan(["a", "b", "c"], body="Works at Initech",
                                      modified="2026-08-01T00:00:00Z"))
    assert result["reappeared"] == 1 and result["updated"] == 3
    row = conn.execute("SELECT status, body FROM observations WHERE external_id='a'"
                       ).fetchone()
    assert (row["status"], row["body"]) == ("active", "Works at Initech")


def test_only_the_same_connector_and_account_brings_a_hidden_record_back(conn):
    _hide_cited(conn)
    for other in (_scan(["a", "b", "c"], account="someone-else@example.com"),
                  _scan(["a", "b", "c"], connector="another"),
                  _scan(["a", "b", "c"], mirror=False)):
        assert obs.build_plan(conn, other)["reappeared"] == []
    _apply(conn, _scan(["a", "b", "c"], mirror=False))
    assert _status(conn, "a") == "retracted"


def test_a_record_hidden_by_imports_retract_does_not_come_back_by_mirroring(conn):
    _, result = _apply(conn, _scan(["a", "b", "c"]))
    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert obs.build_plan(conn, _scan(["a", "b", "c"]))["reappeared"] == []


def test_a_cited_text_recovered_on_the_phone_is_back_on_the_next_backup(
        conn, cfg, tmp_path):
    """docs/texts.md: "Recovered on the phone (Recently Deleted): back on the
    next import" — true before only for a message nothing cited."""
    from lifecrm.connectors import ios_messages as im
    from test_ios_messages import make_backup
    _apply(conn, im.scan(make_backup(tmp_path / "a"), cfg))
    _cite(conn, "M1")
    _, result = _apply(conn, im.scan(make_backup(tmp_path / "b", variant="deleted"), cfg))
    assert result["retracted_cited"] == 1 and _status(conn, "M1") == "retracted"
    _, result = _apply(conn, im.scan(make_backup(tmp_path / "c"), cfg))
    assert result["reappeared"] == 1 and result["added"] == 0
    assert _status(conn, "M1") == "active"
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE external_id='M1'"
                        ).fetchone()[0] == 1           # the same row, not a copy


def test_doctor_warns_while_a_relationship_cites_a_hidden_record(conn, cfg):
    link = _hide_cited(conn)
    check = _check(conn, cfg, "cited_observations_hidden")
    assert check["status"] == "warn"
    assert "1 active relationship(s)" in check["detail"]
    assert f"relationship {link['uuid'][:8]}" in check["detail"]

    _apply(conn, _scan(["a", "b", "c"]))
    assert _check(conn, cfg, "cited_observations_hidden")["status"] == "ok"


def test_doctor_warns_while_an_entry_cites_a_hidden_record(conn, cfg):
    from lifecrm.domain import reconcile
    from test_reconcile import _ingest
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    _apply(conn, _scan(["a", "b", "c"]))
    entry = conn.execute("SELECT id, uuid FROM entries LIMIT 1").fetchone()
    card = conn.execute("SELECT id FROM observations WHERE external_id='a'").fetchone()
    conn.execute("INSERT INTO entry_observations (entry_id, observation_id) "
                 "VALUES (?, ?)", (entry["id"], card["id"]))
    conn.commit()
    _apply(conn, _scan(["b", "c"]))

    check = _check(conn, cfg, "cited_observations_hidden")
    assert check["status"] == "warn" and f"entry {entry['uuid'][:8]}" in check["detail"]
    assert run_checks(conn, cfg)["failures"] == 0      # a warning, not corruption


# ---------------------------------------------------------------- the guard

@pytest.mark.parametrize("removed,scope,refused", [
    (6, 7, True),           # a one-card vCard against a seven-card address book
    (5, 40, True),          # the floor, and more than a tenth
    (60, 70, True),         # most of a small archive
    (2, 4, True),           # half is always too much
    (4, 30, False),         # under the floor and under half
    (1, 2, False),          # one record is never a mass removal
    (101, 2000, False),     # a big archive's real deletions: under a tenth
])
def test_the_mass_removal_bar_refuses_on_share_in_a_small_archive(
        removed, scope, refused):
    msg = obs.mass_removal({"removed": [None] * removed, "mirror_scope": scope})
    assert (msg is not None) == refused


# ---------------------------------------------------------------- the CLI

NAMES = ["Ana Diaz", "Ben Ito", "Cy Park", "Dee Moss", "Eve Lund", "Fay Ruiz",
         "Gus Hale"]
EXPORTED = datetime.fromisoformat("2026-10-01T12:00:00+00:00").timestamp()


def _vcf(path: Path, names) -> str:
    """A vCard of these cards, each card's text (so its id) fixed by name, and
    one export time for every file, so a card reads identical in each."""
    text = "".join(f"BEGIN:VCARD\nVERSION:3.0\nFN:{n}\n"
                   f"TEL;TYPE=CELL:+1 512-555-{100 + NAMES.index(n):04d}\nEND:VCARD\n"
                   for n in names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    os.utime(path, (EXPORTED, EXPORTED))
    return str(path)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    with open(root / "config.toml", "a", encoding="utf-8") as f:
        f.write('[connectors.contacts]\naccount = "google:alex"\n')
    assert runner.invoke(app, ["init"]).exit_code == 0
    return root


def _observe(*args):
    r = runner.invoke(app, ["observe", "contacts", *args])
    assert r.exit_code == 0, r.output
    return r.output


def _snapshot_from(output: str) -> Path:
    return Path(re.search(r"^snapshot before removing: (.+)$", output,
                          re.M).group(1).strip())


def _sql(query):
    r = runner.invoke(app, ["sql", query, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_cli_brings_a_cited_card_back_and_says_so(project, tmp_path):
    full = _vcf(tmp_path / "full.vcf", NAMES[:3])
    _observe(full, "--apply")
    conn = db_mod.connect(project / "data" / "lifecrm.db")
    try:
        card = conn.execute("SELECT uuid FROM observations WHERE title=?",
                            (NAMES[0],)).fetchone()
        ana = entities.create(conn, "person", NAMES[0])
        acme = entities.create(conn, "organization", "Acme")
        entity_links.add(conn, ana["id"], acme["id"], "works_at",
                         observation_uuid=card["uuid"])
        conn.commit()
    finally:
        conn.close()

    out = _observe(_vcf(tmp_path / "partial.vcf", NAMES[1:3]), "--apply")
    assert "1 hidden instead (cited by an entry or a relationship" in out

    dry = _observe(full)
    assert "back: 1 hidden record(s) the source holds again" in dry
    assert "nothing new" not in dry
    assert json.loads(_observe(full, "--json"))["reappeared"] == 1
    out = _observe(full, "--apply")
    assert "1 hidden record(s) back in the source, active again" in out
    assert _sql(f"SELECT status FROM observations WHERE title='{NAMES[0]}'") == [
        {"status": "active"}]


def test_cli_refuses_a_one_card_vcard_that_would_empty_the_address_book(
        project, tmp_path):
    _observe(_vcf(tmp_path / "full.vcf", NAMES), "--apply")
    one = _vcf(tmp_path / "one.vcf", NAMES[:1])
    assert "WILL BE REFUSED on --apply: this would remove 6 of 7" in _observe(one)
    r = runner.invoke(app, ["observe", "contacts", one, "--apply"])
    assert r.exit_code == 1 and "--allow-mass-removal" in r.output
    assert _sql("SELECT COUNT(*) n FROM observations WHERE status='active'") == [
        {"n": 7}]
    _observe(one, "--apply", "--allow-mass-removal")
    assert _sql("SELECT COUNT(*) n FROM observations WHERE status='active'") == [
        {"n": 1}]


def test_cli_names_the_snapshot_as_the_undo_for_a_removal(project, tmp_path):
    _observe(_vcf(tmp_path / "full.vcf", NAMES[:3]), "--apply")
    out = _observe(_vcf(tmp_path / "partial.vcf", NAMES[1:3]), "--apply")
    snap = _snapshot_from(out)
    assert snap.name.startswith("pre-removal-") and snap.exists()
    assert f'undo the removal with: python -m lifecrm restore "{snap}"' in out
    assert "undo with: python -m lifecrm imports retract" not in out
    assert "imports retract would only hide the 0 record(s) it added" in out

    uid = re.search(r"^import ([0-9a-f-]{36})", out, re.M).group(1)
    r = runner.invoke(app, ["imports", "retract", uid])
    assert r.exit_code == 0, r.output
    assert "retract does not bring those back" in r.output
    assert f'python -m lifecrm restore "{snap}"' in r.output

    r = runner.invoke(app, ["restore", str(snap)])
    assert r.exit_code == 0, r.output
    assert _sql(f"SELECT COUNT(*) n FROM observations WHERE title='{NAMES[0]}'") == [
        {"n": 1}]                                   # the undo really undoes it


def test_cli_json_after_a_removal_carries_the_undo(project, tmp_path):
    _observe(_vcf(tmp_path / "full.vcf", NAMES[:3]), "--apply")
    out = json.loads(_observe(_vcf(tmp_path / "partial.vcf", NAMES[1:3]),
                              "--apply", "--json"))
    assert out["applied"]["removed"] == 1
    assert out["undo_removal"] == f'python -m lifecrm restore "{out["snapshot"]}"'
    assert out["applied"]["snapshot"] == Path(out["snapshot"]).name


def test_the_pre_removal_snapshot_survives_the_next_backup_the_same_day(
        project, tmp_path):
    _observe(_vcf(tmp_path / "full.vcf", NAMES[:3]), "--apply")
    assert runner.invoke(app, ["backup"]).exit_code == 0
    out = _observe(_vcf(tmp_path / "partial.vcf", NAMES[1:3]), "--apply")
    snap = _snapshot_from(out)
    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    assert snap.exists()
    held = db_mod.connect(snap)
    try:
        n = held.execute("SELECT COUNT(*) FROM observations WHERE title=?",
                         (NAMES[0],)).fetchone()[0]
    finally:
        held.close()
    assert n == 1                               # the removed card is still in it


# ---------------------------------------------------------------- retention

def _pre(day, t="120000"):
    return f"pre-removal-{day}-{t}.db"


def test_pre_removal_snapshots_are_kept_beside_the_days_newest_backup():
    from lifecrm.retention import select_retained
    names = ["lifecrm-20261022-063000.db", _pre("20261022", "063117"),
             _pre("20261022", "090000"), "lifecrm-20261022-213000.db"]
    assert select_retained(names) == {"lifecrm-20261022-213000.db",
                                      _pre("20261022", "063117"),
                                      _pre("20261022", "090000")}


def test_pre_removal_snapshots_keep_the_most_recent_days_that_have_one(tmp_path):
    from lifecrm.retention import PRE_REMOVAL_DAYS, PRE_REMOVAL_GLOB, prune_dir
    days = [f"202609{d:02d}" for d in range(1, PRE_REMOVAL_DAYS + 3)]
    for day in days:
        (tmp_path / _pre(day)).write_bytes(b"x")
    (tmp_path / "lifecrm-20260901-120000.db").write_bytes(b"x")
    removed = prune_dir(tmp_path, PRE_REMOVAL_GLOB)
    assert removed == [_pre(days[0]), _pre(days[1])]
    assert (tmp_path / "lifecrm-20260901-120000.db").exists()   # not its family
    assert prune_dir(tmp_path, PRE_REMOVAL_GLOB) == []
