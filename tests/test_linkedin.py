"""LinkedIn connector: the "Get a copy of your data" export.

Most of this runs scan() against small exports shaped like LinkedIn's —
Connections.csv's "Notes:" preamble, messages.csv's upper-case header and
HTML bodies, Invitations.csv naming whose export it is. The rest is what a
connections importer can get quietly wrong: whose profile is yours (it
decides direction AND what mirroring may remove), a new job arriving as an
update rather than a conflict, and a removed connection leaving without
touching another account or the messages.
"""

import json
import zipfile
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import linkedin
from lifecrm.demo import persona
from lifecrm.demo.sources import linkedin as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

NOTE = ('Notes:\n"When exporting your connection data, you may notice that some of the '
        'email addresses are missing. You will only see email addresses for connections '
        'who have allowed their connections to see or download their email address."\n\n')

CONNECTIONS = (
    NOTE
    + "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Priya,Natarajan,https://www.linkedin.com/in/Priya-Natarajan-Acme,"
    "priya@acme-robotics.example,Acme Robotics,VP Product,10 Sep 2023\n"
    "Jake,Morales,https://www.linkedin.com/in/jakemorales-dev/,,Hill Country Software,"
    "Software Engineer,13 Jun 2017\n"
    "Sam,Okafor,https://www.linkedin.com/in/sam-okafor-pt,,,,15 Jul 2024\n")

MESSAGES = (
    "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,"
    "DATE,SUBJECT,CONTENT,FOLDER\n"
    "2-abc,,Priya Natarajan,https://www.linkedin.com/in/priya-natarajan-acme,Alex Rivera,"
    "https://www.linkedin.com/in/alex-rivera-pm,2026-10-02 13:40:00 UTC,,"
    '"<p>Congrats on the board deck, Alex! Customer story first &amp; it landed.</p>'
    '<p>Proud of you.</p>",INBOX\n'
    "2-abc,,Alex Rivera,https://www.linkedin.com/in/alex-rivera-pm,Priya Natarajan,"
    "https://www.linkedin.com/in/priya-natarajan-acme,2026-10-02 14:12:00 UTC,,"
    "Thank you Priya <3,INBOX\n"
    "2-grp,Bled 2027,Jake Morales,https://www.linkedin.com/in/jakemorales-dev,"
    "\"Alex Rivera, Sam Okafor\","
    "\"https://www.linkedin.com/in/alex-rivera-pm,https://www.linkedin.com/in/sam-okafor-pt\","
    "2026-10-03 09:30:00 UTC,,who's in for Bled?,INBOX\n")

INVITATIONS = (
    "From,To,Sent At,Message,Direction,inviterProfileUrl,inviteeProfileUrl\n"
    "Alex Rivera,Sam Okafor,\"7/14/24, 7:02 PM\",Great lifting with you!,OUTGOING,"
    "https://www.linkedin.com/in/alex-rivera-pm,https://www.linkedin.com/in/sam-okafor-pt\n")

PROFILE = ("First Name,Last Name,Maiden Name,Address,Birth Date,Headline\n"
           "Alex,Rivera,,,,Product Manager at Acme Robotics\n")

YOU = "linkedin:in/alex-rivera-pm"


def export(tmp_path, *, connections=CONNECTIONS, messages=MESSAGES,
           invitations=INVITATIONS, profile=PROFILE, extra=(), name="export.zip",
           stamp=(2026, 10, 21, 6, 0, 0)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, text in (("Connections.csv", connections), ("messages.csv", messages),
                             ("Invitations.csv", invitations), ("Profile.csv", profile),
                             *extra):
            if text is not None:
                zf.writestr(zipfile.ZipInfo(member, date_time=stamp), text)
    return path


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


def _by(scan, stream):
    return [r for r in scan["records"] if r["stream"] == stream]


# ---------------------------------------------------------------- connections

def test_a_connection_maps_name_position_company_and_its_handles(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    priya = next(r for r in _by(scan, "linkedin_connection") if r["title"] == "Priya Natarajan")
    assert priya["body"] == "VP Product at Acme Robotics"
    assert priya["external_id"] == "linkedin:in/priya-natarajan-acme"
    assert priya["direction"] is None and priya["value"] is None
    assert json.loads(priya["value_json"]) == {
        "company": "Acme Robotics", "position": "VP Product",
        "url": "https://www.linkedin.com/in/Priya-Natarajan-Acme",
        "email": "priya@acme-robotics.example"}
    assert priya["participants"] == [
        {"kind": "app_account", "value": "linkedin:in/priya-natarajan-acme",
         "role": "participant"},
        {"kind": "email", "value": "priya@acme-robotics.example", "role": "participant"}]
    assert priya["origin_app"] == "linkedin"


def test_connected_on_is_the_journal_date_at_local_noon(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    jake = next(r for r in _by(scan, "linkedin_connection") if r["title"] == "Jake Morales")
    assert jake["journal_date"] == "2017-06-13"
    assert jake["started_at"] == "2017-06-13T17:00:00Z"          # noon CDT
    sam = next(r for r in _by(scan, "linkedin_connection") if r["title"] == "Sam Okafor")
    assert sam["body"] is None                                   # no job on the row


def test_connections_carry_the_exports_own_time(tmp_path, cfg):
    """The zip's member time is when LinkedIn last said what the row holds:
    it is what lets a later export's new job win as an update."""
    scan = linkedin.scan(export(tmp_path), cfg)
    assert {r["last_modified_at"] for r in _by(scan, "linkedin_connection")} == \
        {"2026-10-21T11:00:00Z"}                                 # 06:00 CDT
    assert {r["last_modified_at"] for r in _by(scan, "linkedin_message")} == \
        {r["started_at"] for r in _by(scan, "linkedin_message")}


@pytest.mark.parametrize("raw,expected", [
    ("https://www.linkedin.com/in/Priya-Natarajan/", "in/priya-natarajan"),
    ("http://linkedin.com/in/jos%C3%A9-garc%C3%ADa", "in/josé-garcía"),
    ("www.linkedin.com/in/sam?trk=public", "in/sam"),
    ("sam-okafor-pt", "in/sam-okafor-pt"),
    ("https://www.linkedin.com/company/acme-robotics", "company/acme-robotics"),
    ("", None),
])
def test_a_profile_url_becomes_one_spelling_of_its_path(raw, expected):
    assert linkedin.profile_path(raw) == expected


def test_a_connection_with_no_url_is_keyed_on_name_and_date(tmp_path, cfg):
    text = CONNECTIONS + "Rosa,Diaz,,,,,02 Feb 2020\n"
    scan = linkedin.scan(export(tmp_path, connections=text), cfg)
    rosa = next(r for r in _by(scan, "linkedin_connection") if r["title"] == "Rosa Diaz")
    assert rosa["external_id"].startswith("linkedin-name:")
    assert rosa["participants"] == []
    assert any("no profile URL" in u["what"] for u in scan["unmapped"])


def test_an_unreadable_connected_on_is_reported_not_guessed(tmp_path, cfg):
    text = CONNECTIONS + "Rosa,Diaz,https://www.linkedin.com/in/rosa,,,,15 Mär 2021\n"
    scan = linkedin.scan(export(tmp_path, connections=text), cfg)
    assert "Rosa Diaz" not in {r["title"] for r in scan["records"]}
    u = next(u for u in scan["unmapped"] if "Connected On" in u["what"])
    assert u["count"] == 1 and "Mär" in u["example"]
    assert "linkedin:in/rosa" in scan["present_ids"]             # still in the file


# ---------------------------------------------------------------- messages

def test_messages_are_plain_text_with_direction_from_your_profile(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    msgs = _by(scan, "linkedin_message")
    congrats = next(r for r in msgs if "Congrats" in r["body"])
    assert congrats["body"] == ("Congrats on the board deck, Alex! Customer story first "
                                "& it landed.\nProud of you.")
    assert congrats["direction"] == "in"
    assert congrats["title"] == "Priya Natarajan"                # one-to-one: the other person
    reply = next(r for r in msgs if r["body"] == "Thank you Priya <3")
    assert reply["direction"] == "out"
    assert reply["participants"] == [
        {"kind": "app_account", "value": YOU, "role": "sender"},
        {"kind": "app_account", "value": "linkedin:in/priya-natarajan-acme",
         "role": "recipient"}]
    assert json.loads(reply["value_json"]) == {"conversation_id": "2-abc",
                                               "folder": "INBOX"}


def test_a_group_conversation_has_its_title_and_every_recipient(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    bled = next(r for r in _by(scan, "linkedin_message") if r["title"] == "Bled 2027")
    assert json.loads(bled["value_json"])["group"] is True
    assert [p["role"] for p in bled["participants"]] == ["sender", "recipient", "recipient"]
    assert {p["value"] for p in bled["participants"]} == {
        "linkedin:in/jakemorales-dev", YOU, "linkedin:in/sam-okafor-pt"}


@pytest.mark.parametrize("content,expected", [
    ("<p>a &amp; b</p><p>c<br>d</p>", "a & b\nc\nd"),
    ("<ul><li>one</li><li>two</li></ul>", "- one\n- two"),
    ("plain text\nkept as written", "plain text\nkept as written"),
    ("I <3 this, and a < b", "I <3 this, and a < b"),
    ("<a href=\"https://x.example\">the link</a>", "the link"),
])
def test_html_content_becomes_plain_text(content, expected):
    assert linkedin.plain_text(content) == expected


def test_message_times_are_utc_and_follow_the_rollover(tmp_path, cfg):
    """09:30 UTC is 04:30 in Austin — before the 05:00 rollover, so it is
    still the night of the 2nd."""
    scan = linkedin.scan(export(tmp_path), cfg)
    bled = next(r for r in _by(scan, "linkedin_message") if r["title"] == "Bled 2027")
    assert bled["started_at"] == "2026-10-03T09:30:00Z"
    assert bled["journal_date"] == "2026-10-02"
    cfg.day_rollover_hour = 0
    again = linkedin.scan(export(tmp_path / "b"), cfg)
    assert next(r for r in _by(again, "linkedin_message")
                if r["title"] == "Bled 2027")["journal_date"] == "2026-10-03"


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-02 13:40:00 UTC", "2026-10-02T13:40:00Z"),
    ("2026-10-02T13:40:00Z", "2026-10-02T13:40:00Z"),
    ("2026-10-02 08:40:00 -05:00", "2026-10-02T13:40:00Z"),
    ("2026-10-02 9:05 UTC", "2026-10-02T09:05:00Z"),
    ("02/10/2026 13:40", None),
])
def test_message_dates_in_the_forms_linkedin_writes(raw, expected):
    assert linkedin.message_time(raw) == expected


def test_drafts_spam_and_empty_messages_are_counted_not_imported(tmp_path, cfg):
    text = (
        "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,"
        "RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER,ATTACHMENTS,IS MESSAGE DRAFT\n"
        "c1,,Alex Rivera,https://www.linkedin.com/in/alex-rivera-pm,Priya Natarajan,"
        "https://www.linkedin.com/in/priya-natarajan-acme,2026-10-02 14:00:00 UTC,,"
        "half-written,DRAFT,,Yes\n"
        "c2,,Prize Desk,https://www.linkedin.com/in/prize,Alex Rivera,"
        "https://www.linkedin.com/in/alex-rivera-pm,2026-10-02 15:00:00 UTC,,you won,SPAM,,No\n"
        "c1,,Priya Natarajan,https://www.linkedin.com/in/priya-natarajan-acme,Alex Rivera,"
        "https://www.linkedin.com/in/alex-rivera-pm,2026-10-02 16:00:00 UTC,,,INBOX,,No\n"
        "c1,,Priya Natarajan,https://www.linkedin.com/in/priya-natarajan-acme,Alex Rivera,"
        "https://www.linkedin.com/in/alex-rivera-pm,2026-10-02 16:01:00 UTC,,,INBOX,"
        "https://www.linkedin.com/dms/deck.pdf,No\n"
        "c3,,LinkedIn Member,,Alex Rivera,https://www.linkedin.com/in/alex-rivera-pm,"
        "2026-10-02 17:00:00 UTC,,hello from nobody,INBOX,,No\n"
        "c1,,Priya Natarajan,https://www.linkedin.com/in/priya-natarajan-acme,Alex Rivera,"
        "https://www.linkedin.com/in/alex-rivera-pm,yesterday,,hi,INBOX,,No\n")
    scan = linkedin.scan(export(tmp_path, messages=text), cfg)
    assert "1 × draft message (never sent)" in scan["skipped"]
    assert "1 × message in the spam folder" in scan["skipped"]
    assert "1 × message with no text" in scan["skipped"]
    msgs = _by(scan, "linkedin_message")
    attached = next(r for r in msgs if r["body"] is None)
    assert json.loads(attached["value_json"])["attachments"] == 1
    nobody = next(r for r in msgs if r["body"] == "hello from nobody")
    assert [p["role"] for p in nobody["participants"]] == ["recipient"]
    assert nobody["direction"] == "in"
    assert json.loads(nobody["value_json"])["sender_shown_as"] == "LinkedIn Member"
    whats = {u["what"]: u["count"] for u in scan["unmapped"]}
    assert whats["message whose DATE does not read"] == 1
    assert whats["message whose sender has no profile URL (kept; sender not attributed)"] == 1


def test_identical_messages_in_one_minute_stay_two(tmp_path, cfg):
    row = ("2-abc,,Priya Natarajan,https://www.linkedin.com/in/priya-natarajan-acme,"
           "Alex Rivera,https://www.linkedin.com/in/alex-rivera-pm,"
           "2026-10-02 13:40:00 UTC,,ok,INBOX\n")
    text = MESSAGES.splitlines(keepends=True)[0] + row + row
    scan = linkedin.scan(export(tmp_path, messages=text), cfg)
    ids = [r["external_id"] for r in _by(scan, "linkedin_message")]
    assert len(ids) == 2 and len(set(ids)) == 2 and all(i.startswith("li:") for i in ids)


# ---------------------------------------------------------------- whose export

def test_your_profile_comes_from_invitations(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    assert scan["account"] == YOU
    assert scan["facts"]["you_from"].startswith("Invitations.csv")
    assert scan["facts"]["your_name"] == "Alex Rivera"


def test_the_setting_wins_and_a_bad_one_fails_naming_it(tmp_path, cfg):
    cfg.connectors = {"linkedin": {"profile_url": "https://www.linkedin.com/in/sam-okafor-pt"}}
    scan = linkedin.scan(export(tmp_path), cfg)
    assert scan["account"] == "linkedin:in/sam-okafor-pt"
    cfg.connectors = {"linkedin": {"profile_url": 42}}
    with pytest.raises(DomainError, match=r"\[connectors.linkedin\] profile_url"):
        linkedin.scan(export(tmp_path / "b"), cfg)


def test_without_invitations_the_profile_name_finds_your_messages(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path, invitations=None), cfg)
    assert scan["account"] == YOU
    assert "Profile.csv" in scan["facts"]["you_from"]


def test_without_profile_the_owner_name_finds_your_messages(tmp_path, cfg):
    cfg.extra = {"owner": {"name": "Alex Rivera"}}
    scan = linkedin.scan(export(tmp_path, invitations=None, profile=None), cfg)
    assert scan["account"] == YOU
    assert "[owner] name" in scan["facts"]["you_from"]


def test_the_one_profile_in_every_conversation_is_you(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path, invitations=None, profile=None), cfg)
    assert scan["account"] == YOU
    assert scan["facts"]["you_from"] == "the one profile in every conversation"


def test_an_export_that_cannot_say_whose_it_is_has_no_direction(tmp_path, cfg, conn):
    one = MESSAGES.split("2-grp")[0]                  # a single conversation
    scan = linkedin.scan(export(tmp_path, messages=one, invitations=None,
                                profile=None), cfg)
    assert scan["account"] is None
    assert {r["direction"] for r in _by(scan, "linkedin_message")} == {None}
    text = "\n".join(linkedin.report_lines(scan, obs.build_plan(conn, scan)))
    assert "WARNING which profile is yours is not in this export" in text
    assert "not mirroring deletions" in text


# ---------------------------------------------------------------- handles

def test_handles_are_profiles_and_emails_with_names_only_as_display(tmp_path, cfg):
    scan = linkedin.scan(export(tmp_path), cfg)
    handles = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert handles == {
        ("app_account", YOU): "Alex Rivera",
        ("app_account", "linkedin:in/jakemorales-dev"): "Jake Morales",
        ("app_account", "linkedin:in/priya-natarajan-acme"): "Priya Natarajan",
        ("app_account", "linkedin:in/sam-okafor-pt"): "Sam Okafor",
        ("email", "priya@acme-robotics.example"): "Priya Natarajan"}
    for r in scan["records"]:
        for p in r["participants"]:
            assert (p["kind"], p["value"]) in handles     # every participant declared
            assert " " not in p["value"]                  # an identifier, never a name


# ---------------------------------------------------------------- bad input

def test_a_zip_without_connections_or_messages_says_what_it_holds(tmp_path, cfg):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Positions.csv", "Company Name,Title\n")
    with pytest.raises(DomainError) as e:
        linkedin.scan(path, cfg)
    msg = str(e.value)
    assert "neither Connections.csv nor messages.csv" in msg and "Positions.csv" in msg
    assert "\n" not in msg


def test_a_csv_that_is_not_an_export_file_names_its_first_line(tmp_path, cfg):
    p = tmp_path / "people.csv"
    p.write_text("name,phone\nJake,555\n", encoding="utf-8")
    with pytest.raises(DomainError, match="first line reads: 'name,phone'"):
        linkedin.scan(p, cfg)


def test_a_renamed_connections_csv_is_recognised_by_its_header(tmp_path, cfg):
    p = tmp_path / "Connections (1).csv"
    p.write_text(CONNECTIONS, encoding="utf-8")
    scan = linkedin.scan(p, cfg)
    assert len(_by(scan, "linkedin_connection")) == 3
    assert scan["account"] is None                    # nothing says whose it is


def test_connections_without_its_header_fail_on_one_line(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        linkedin.scan(export(tmp_path, connections="Notes:\nnothing here\n"), cfg)
    assert "no 'First Name,Last Name" in str(e.value) and "\n" not in str(e.value)


def test_a_utf8_bom_export_is_read(tmp_path, cfg):
    path = export(tmp_path, connections="﻿" + CONNECTIONS,
                  messages="﻿" + MESSAGES)
    scan = linkedin.scan(path, cfg)
    assert len(_by(scan, "linkedin_connection")) == 3
    assert len(_by(scan, "linkedin_message")) == 3


def test_a_file_that_is_not_utf8_fails_naming_it(tmp_path, cfg):
    path = export(tmp_path, connections=None)
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("Connections.csv", "First Name,Last Name,Connected On\nJos\xe9".encode(
            "latin-1"))
    with pytest.raises(DomainError, match="Connections.csv is not valid UTF-8"):
        linkedin.scan(path, cfg)


def test_an_unzipped_folder_reads_the_same_as_the_zip(tmp_path, cfg):
    folder = tmp_path / "Basic_LinkedInDataExport_10-21-2026"
    folder.mkdir()
    for member, text in (("Connections.csv", CONNECTIONS), ("messages.csv", MESSAGES),
                         ("Invitations.csv", INVITATIONS), ("Profile.csv", PROFILE),
                         ("Skills.csv", "Name\nRobotics\n")):
        (folder / member).write_text(text, encoding="utf-8")
    a = linkedin.scan(folder, cfg)
    b = linkedin.scan(export(tmp_path / "z"), cfg)
    strip = [{k: v for k, v in r.items() if k != "last_modified_at"} for r in a["records"]]
    assert strip == [{k: v for k, v in r.items() if k != "last_modified_at"}
                     for r in b["records"]]
    assert a["facts"]["not_read"] == 1


# ---------------------------------------------------------------- determinism

def test_scan_is_deterministic(tmp_path, cfg):
    path = export(tmp_path)
    a, b = linkedin.scan(path, cfg), linkedin.scan(path, cfg)
    assert a["records"] == b["records"] and a["handles"] == b["handles"]
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_spans_and_whose_export(tmp_path, cfg, conn):
    path = export(tmp_path, extra=(("Skills.csv", "Name\nRobotics\n"),))
    scan = linkedin.scan(path, cfg)
    text = "\n".join(linkedin.report_lines(scan, obs.build_plan(conn, scan)))
    assert "linkedin_connection: 3 new, 0 changed | 2017-06-13..2024-07-15" in text
    assert "linkedin_message: 3 new, 0 changed | 2026-10-02..2026-10-02" in text
    assert f"you: {YOU} (Alex Rivera) — from Invitations.csv" in text
    assert "not imported: 1 other file(s) in the export (e.g. Skills.csv)" in text
    assert "skipped: 2 × line of LinkedIn's note above the Connections.csv header" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    path = export(tmp_path)
    _, first = _apply(conn, linkedin.scan(path, cfg))
    assert first["added"] == 6
    plan, again = _apply(conn, linkedin.scan(path, cfg))
    assert again["added"] == 0 and again["identical"] == 6
    assert plan["conflicts"] == [] and plan["removed"] == []
    text = "\n".join(linkedin.report_lines(linkedin.scan(path, cfg), plan))
    assert "nothing new — the export matches the database" in text


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities")
    _apply(conn, linkedin.scan(export(tmp_path), cfg))
    for t in tables:
        assert conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] == 0, t


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_a_newer_export_updates_a_new_job_instead_of_conflicting(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    promoted = CONNECTIONS.replace("Software Engineer", "Staff Engineer")
    plan, result = _apply(conn, linkedin.scan(
        export(tmp_path / "b", connections=promoted, stamp=(2026, 11, 30, 6, 0, 0)), cfg))
    assert result["updated"] == 1 and plan["conflicts"] == []
    assert len(plan["refreshed"]) == 2               # unchanged rows, re-dated
    body = conn.execute("SELECT body FROM observations WHERE external_id=?",
                        ("linkedin:in/jakemorales-dev",)).fetchone()["body"]
    assert body == "Staff Engineer at Hill Country Software"


# ---------------------------------------------------------------- mirroring

def _active(conn, stream):
    return {r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE status='active' AND stream=?",
        (stream,))}


def test_a_removed_connection_leaves_with_the_next_export(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    without_sam = "".join(ln for ln in CONNECTIONS.splitlines(keepends=True)
                          if not ln.startswith("Sam,"))
    scan = linkedin.scan(export(tmp_path / "b", connections=without_sam), cfg)
    assert scan["mirror_streams"] == ["linkedin_connection"]
    plan, result = _apply(conn, scan)
    assert [r["title"] for r in plan["removed"]] == ["Sam Okafor"]
    assert result["removed"] == 1
    assert "linkedin:in/sam-okafor-pt" not in _active(conn, "linkedin_connection")
    assert len(_active(conn, "linkedin_message")) == 3          # messages remember
    text = "\n".join(linkedin.report_lines(scan, plan))
    assert "remove: 1 connection(s) no longer in Connections.csv" in text


def test_messages_missing_from_a_later_export_are_remembered(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    first_only = "\n".join(MESSAGES.splitlines()[:2]) + "\n"
    plan, _ = _apply(conn, linkedin.scan(export(tmp_path / "b", messages=first_only), cfg))
    assert plan["removed"] == []
    assert len(_active(conn, "linkedin_message")) == 3


def test_another_accounts_connections_are_untouched(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    cfg.connectors = {"linkedin": {"profile_url": "https://www.linkedin.com/in/maya-chen"}}
    other = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
             "Linda,Rivera,https://www.linkedin.com/in/linda-r,,,,01 Jan 2020\n")
    plan, _ = _apply(conn, linkedin.scan(export(tmp_path / "b", connections=other,
                                                messages=None, invitations=None), cfg))
    assert plan["removed"] == []
    assert len(_active(conn, "linkedin_connection")) == 4


def test_an_export_without_an_owner_removes_nothing(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    without_sam = "".join(ln for ln in CONNECTIONS.splitlines(keepends=True)
                          if not ln.startswith("Sam,"))
    scan = linkedin.scan(export(tmp_path / "b", connections=without_sam, messages=None,
                                invitations=None, profile=None), cfg)
    assert scan["account"] is None
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == [] and "mirror_note" in plan
    assert len(_active(conn, "linkedin_connection")) == 3


def test_an_export_without_connections_csv_never_mirrors(tmp_path, cfg, conn):
    _apply(conn, linkedin.scan(export(tmp_path / "a"), cfg))
    scan = linkedin.scan(export(tmp_path / "b", connections=None), cfg)
    assert "present_ids" not in scan
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == []
    assert len(_active(conn, "linkedin_connection")) == 3


# ---------------------------------------------------------------- default target

def test_default_target_is_the_newest_export_in_downloads(tmp_path, cfg, monkeypatch):
    from lifecrm import platforms
    downloads = tmp_path / "home" / "Downloads"
    old = export(downloads, name="Basic_LinkedInDataExport_01-02-2026.zip")
    new = export(downloads, name="Complete_LinkedInDataExport_10-21-2026.zip")
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    monkeypatch.setattr(platforms, "home", lambda: tmp_path / "home")
    assert linkedin.default_target(cfg) == new


def test_default_target_says_how_to_get_an_export(tmp_path, cfg, monkeypatch):
    from lifecrm import platforms
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    with pytest.raises(DomainError, match="no LinkedIn export found"):
        linkedin.default_target(cfg)
    cfg.connectors = {"linkedin": {"path": str(tmp_path / "missing.zip")}}
    with pytest.raises(DomainError, match=r"\[connectors.linkedin\] path"):
        linkedin.default_target(cfg)


# ---------------------------------------------------------------- the demo sample

ANCHOR = date(2026, 10, 21)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_export_tells_the_personas_network(tmp_path, cfg, combo):
    scan = linkedin.scan(sample.write_sample(tmp_path, ANCHOR, combo), cfg)
    conns = {r["title"]: r for r in _by(scan, "linkedin_connection")}
    assert set(conns) == {"Priya Natarajan", "Jake Morales", "Sam Okafor"}
    assert conns["Priya Natarajan"]["body"] == "VP Product at Acme Robotics"
    assert "Software Engineer at" in conns["Jake Morales"]["body"]
    jake_email = persona.person("jake").email
    assert {"kind": "email", "value": jake_email, "role": "participant"} in \
        conns["Jake Morales"]["participants"]
    assert all(p["kind"] != "email" for p in conns["Sam Okafor"]["participants"])
    msgs = _by(scan, "linkedin_message")
    congrats = next(r for r in msgs if "Congrats on the board deck" in (r["body"] or ""))
    assert congrats["direction"] == "in"
    assert congrats["journal_date"] == persona.day(ANCHOR, -19).isoformat()
    assert "&amp;" not in congrats["body"] and "<p>" not in congrats["body"]
    assert any(r["direction"] == "out" for r in msgs)
    assert scan["account"] == "linkedin:in/alex-rivera-pm"
    assert scan["facts"]["not_read"] == 2


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_export_applies_cleanly_and_reimports_identical(tmp_path, cfg, conn,
                                                                 combo):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    _, first = _apply(conn, linkedin.scan(path, cfg))
    assert first["added"] == 5
    plan, _ = _apply(conn, linkedin.scan(sample.write_sample(tmp_path / "b", ANCHOR, combo),
                                         cfg))
    assert plan["identical"] == 5 and plan["new"] == [] and plan["removed"] == []
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_demo_export_is_deterministic_and_refuses_an_unknown_combo(tmp_path):
    a = sample.write_sample(tmp_path / "a", ANCHOR, "android").read_bytes()
    b = sample.write_sample(tmp_path / "b", ANCHOR, "iphone").read_bytes()
    assert a == b
    with pytest.raises(ValueError, match="linkedin has no sample for combo 'pager'"):
        sample.write_sample(tmp_path / "c", ANCHOR, "pager")


# ---------------------------------------------------------------- CLI

def _count(where=""):
    r = runner.invoke(app, ["sql", f"SELECT COUNT(*) c FROM observations {where}", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing_and_apply_then_rerun_is_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    path = export(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "linkedin", str(path)])
    assert r.exit_code == 0, r.output
    assert "linkedin_connection: 3 new" in r.output and "dry run — nothing written" in r.output
    assert _count() == 0
    r = runner.invoke(app, ["observe", "linkedin", str(path), "--apply"])
    assert r.exit_code == 0, r.output
    assert "6 added" in r.output
    again = runner.invoke(app, ["observe", "linkedin", str(path), "--apply"])
    assert again.exit_code == 0, again.output
    assert "0 added" in again.output and "6 identical skipped" in again.output
    assert _count("WHERE status='active'") == 6
