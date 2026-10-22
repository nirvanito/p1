"""WhatsApp connector: "Export chat" text files from Android and iPhone.

The connector is a pure reader, so most of this runs scan() against exports
built here as text — shaped like the real ones, including the parts that are
easy to get wrong: the iPhone's invisible marks and narrow no-break spaces,
numbers wrapped in direction marks, system lines whose text contains ': ',
multi-line messages, minute-only Android timestamps, a locale that does not
say whether the day or the month comes first, and media that may or may not
be in the export.
"""

import json
import zipfile
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import base
from lifecrm.connectors import whatsapp as wa
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

LRM = "\N{LEFT-TO-RIGHT MARK}"
NNBSP = "\N{NARROW NO-BREAK SPACE}"
LRE = "\N{LEFT-TO-RIGHT EMBEDDING}"
PDF = "\N{POP DIRECTIONAL FORMATTING}"
NBH = "\N{NON-BREAKING HYPHEN}"
LQ, RQ = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
ROSA_IOS = f"{LRE}+1 (210) 555{NBH}0131{PDF}"

# A one-to-one chat as a Samsung writes it: UK-style day-first, 24-hour, no
# seconds, system lines with no sender at all.
ANDROID = (
    "27/07/2026, 21:30 - Messages and calls are end-to-end encrypted. No one "
    "outside of this chat, not even WhatsApp, can read or listen to them. Tap to "
    "learn more.\n"
    "27/07/2026, 21:30 - Jake Morales: Veracruz tonight? you know it's my favorite\n"
    "27/07/2026, 21:31 - Alex Rivera: always.\n"
    "see you at 7:30\n"
    "bring cash\n"
    "27/07/2026, 21:32 - Alex Rivera: ok\n"
    "27/07/2026, 21:32 - Alex Rivera: ok\n"
    "27/07/2026, 21:40 - Jake Morales: <Media omitted>\n"
    "27/07/2026, 21:41 - Jake Morales: This message was deleted\n"
    "27/07/2026, 21:42 - Jake Morales: IMG-20260727-WA0003.jpg (file attached)\n"
    "the rowboat on Lake Bled!\n"
    "27/07/2026, 21:43 - Jake Morales: VID-20260727-WA0004.mp4 (file attached)\n"
    "27/07/2026, 21:45 - Alex Rivera: best trip ever <This message was edited>\n"
    "28/07/2026, 00:20 - Jake Morales: still up?\n"
    "28/07/2026, 00:30 - Jake Morales: Waiting for this message. This may take a while.\n"
    "28/07/2026, 00:31 - Jake Morales: null\n"
    "28/07/2026, 00:32 - Your security code with Jake Morales changed. Tap to learn more.\n"
    "13/08/2026, 06:30 - Jake Morales: morning run?\n"
)
ANDROID_MESSAGES = 10

# A group as an iPhone set to US English writes it: month-first, 12-hour with
# a narrow no-break space, seconds, the group's own name as the sender of its
# system lines, LRM marks on media and system lines, an unsaved number.
IOS = (
    f"{LRM}[8/20/25, 6:42:07{NNBSP}PM] Rivera Family: {LRM}Messages and calls are "
    f"end-to-end encrypted. Only people in this chat can read, listen to, or share them.\r\n"
    f"{LRM}[8/20/25, 6:42:26{NNBSP}PM] Rivera Family: {LRM}Linda Rivera created group "
    f"{LQ}Rivera Family{RQ}\r\n"
    f"{LRM}[8/20/25, 6:42:45{NNBSP}PM] Rivera Family: {LRM}Linda Rivera added you\r\n"
    f"{LRM}[8/20/25, 6:43:07{NNBSP}PM] Rivera Family: {LRM}Linda Rivera changed the "
    f"subject from {LQ}Riveras: 2025{RQ} to {LQ}Rivera Family{RQ}\r\n"
    f"[10/10/26, 7:00:07{NNBSP}PM] Linda Rivera: Family dinner on Sunday?\r\n"
    f"[10/10/26, 7:04:07{NNBSP}PM] {ROSA_IOS}: Sí! Bring Maya\r\n"
    f"{LRM}[10/10/26, 7:05:00{NNBSP}PM] Linda Rivera: {LRM}image omitted\r\n"
    f"{LRM}[10/10/26, 7:06:00{NNBSP}PM] Alex Rivera: {LRM}<attached: "
    f"00000009-PHOTO-2026-10-10-19-06-00.jpg>\r\n"
    f"[10/10/26, 7:10:07{NNBSP}PM] Alex Rivera: We'll be there {LRM}<This message was edited>\r\n"
    f"{LRM}[10/10/26, 7:12:00{NNBSP}PM] Linda Rivera: {LRM}Missed voice call\r\n"
    f"{LRM}[10/10/26, 7:13:00{NNBSP}PM] Rivera Family: {LRM}Linda Rivera started a poll\r\n"
    f"[10/11/26, 12:05:00{NNBSP}AM] ~{NNBSP}Tío Beto: buenas noches\r\n"
)
IOS_PHOTO = "00000009-PHOTO-2026-10-10-19-06-00.jpg"

# The same group's start as Android writes it: no sender on system lines, a
# subject whose own text holds ': ', an unsaved number joining by link.
ANDROID_GROUP = (
    "20/08/2025, 18:42 - Messages and calls are end-to-end encrypted. No one outside "
    "of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.\n"
    '20/08/2025, 18:42 - Linda Rivera created group "Rivera Family"\n'
    "20/08/2025, 18:42 - Linda Rivera added you\n"
    '20/08/2025, 18:43 - Linda Rivera changed the subject from "Riveras: 2025" to '
    '"Rivera Family"\n'
    "20/08/2025, 18:44 - +1 210-555-0131 joined using this group's invite link\n"
    "20/08/2025, 18:45 - Linda Rivera: Welcome, familia\n"
    "20/08/2025, 18:46 - Alex Rivera: hola!\n"
)


def android_export(folder: Path, text: str = ANDROID, chat: str = "Jake Morales",
                   media=("IMG-20260727-WA0003.jpg",), suffix: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"WhatsApp Chat with {chat}{suffix}.txt"
    p.write_bytes(text.encode("utf-8"))
    for m in media:
        (folder / m).write_bytes(b"\xff\xd8 media bytes the importer never reads")
    return p


def ios_zip(folder: Path, text: str = IOS, chat: str = "Rivera Family",
            media=(IOS_PHOTO,)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / f"WhatsApp Chat - {chat}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("_chat.txt", text.encode("utf-8"))
        for m in media:
            zf.writestr(m, b"\xff\xd8 media bytes the importer never reads")
    return z


@pytest.fixture
def me(cfg):
    cfg.connectors = {"whatsapp": {"me": "Alex Rivera"}}
    return cfg


def _bodies(scan):
    return [r["body"] for r in scan["records"]]


def _by_body(scan):
    return {r["body"]: r for r in scan["records"] if r["body"]}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _report(conn, scan):
    return "\n".join(wa.report_lines(scan, obs.build_plan(conn, scan)))


# ---------------------------------------------------------------- shape

def test_an_android_export_reads_into_the_whatsapp_stream(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    assert scan["connector"] == "whatsapp"
    assert len(scan["records"]) == ANDROID_MESSAGES
    assert {r["stream"] for r in scan["records"]} == {"whatsapp"}
    assert {r["origin_app"] for r in scan["records"]} == {"whatsapp"}
    assert {r["title"] for r in scan["records"]} == {"Jake Morales"}
    assert all(r["external_id"].startswith("wa:") for r in scan["records"])
    assert scan["facts"]["chats"][0]["phone"] == "android"


def test_an_iphone_zip_reads_with_its_marks_undone(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    recs = _by_body(scan)
    assert set(recs) == {"Family dinner on Sunday?", "Sí! Bring Maya",
                         "We'll be there", "buenas noches"}
    assert recs["Sí! Bring Maya"]["participants"][0] == {
        "kind": "phone", "value": "+12105550131", "role": "sender"}
    assert scan["facts"]["chats"][0]["phone"] == "iphone"
    assert scan["facts"]["chats"][0]["chat"] == "Rivera Family"


def test_multi_line_messages_keep_their_lines(me, tmp_path):
    recs = _by_body(wa.scan(android_export(tmp_path), me))
    assert "always.\nsee you at 7:30\nbring cash" in recs


def test_crlf_line_ends_do_not_leak_into_bodies(me, tmp_path):
    text = ("[7/27/26, 9:30:05 PM] Jake Morales: line one\r\nline two\r\n"
            "[7/27/26, 9:31:00 PM] Alex Rivera: ok\r\n")
    scan = wa.scan(android_export(tmp_path, text), me)
    assert _bodies(scan) == ["line one\nline two", "ok"]


# ---------------------------------------------------------------- what is not a message

def test_android_system_lines_are_counted_not_imported(me, tmp_path):
    scan = wa.scan(android_export(tmp_path, ANDROID_GROUP, chat="Rivera Family",
                                  media=()), me)
    assert _bodies(scan) == ["Welcome, familia", "hola!"]
    assert "1 × system line: encryption notice" in scan["skipped"]
    assert "4 × system line: group change" in scan["skipped"]


def test_a_subject_holding_a_colon_is_not_mistaken_for_a_sender(me, tmp_path):
    """'X changed the subject from "Riveras: 2025"' splits at ': ' like a
    message would — it must stay a system line, not a message from
    'Linda Rivera changed the subject from "Riveras'."""
    scan = wa.scan(android_export(tmp_path, ANDROID_GROUP, chat="Rivera Family",
                                  media=()), me)
    values = {h["value"] for h in scan["handles"]}
    assert not any("subject" in v for v in values)
    assert values == {"whatsapp:Alex Rivera", "whatsapp:Linda Rivera"}


def test_iphone_system_lines_from_the_groups_own_name_are_counted(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    assert "1 × system line: encryption notice" in scan["skipped"]
    assert "3 × system line: group change" in scan["skipped"]
    assert "1 × system line: call notice" in scan["skipped"]
    # A system line with no known pattern is still not a message from a
    # person called "Rivera Family" — it is counted as not understood.
    assert {"what": "system line of a kind not recognised", "count": 1} in scan["unmapped"]
    assert "whatsapp:Rivera Family" not in {h["value"] for h in scan["handles"]}


def test_skips_are_counted_not_hidden(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    joined = " | ".join(scan["skipped"])
    assert "1 × deleted message ('This message was deleted')" in joined
    assert "1 × message the phone never received ('Waiting for this message')" in joined
    assert "1 × view-once media ('null')" in joined
    assert "1 × system line: security code change" in joined
    assert "1 × system line: encryption notice" in joined


def test_words_that_look_like_markers_are_still_what_a_person_wrote(me, tmp_path):
    """Only a marked line can carry a document's name before 'document
    omitted', and only Android writes view-once media as 'null'."""
    text = ("[7/27/26, 9:30:05 PM] Jake Morales: sorry, the audio omitted\r\n"
            "[7/27/26, 9:31:00 PM] Jake Morales: null\r\n"
            f"{LRM}[7/27/26, 9:32:00 PM] Jake Morales: Itinerary.pdf \N{BULLET} 3 pages "
            f"{LRM}document omitted\r\n")
    scan = wa.scan(android_export(tmp_path, text, media=()), me)
    assert _bodies(scan) == ["sorry, the audio omitted", "null", None]
    assert json.loads(scan["records"][2]["value_json"]) == {
        "media": "document", "media_omitted": True}


def test_an_iphone_deleted_message_is_skipped_too(me, tmp_path):
    text = (f"[7/27/26, 9:30:05 PM] Jake Morales: hi\r\n"
            f"{LRM}[7/27/26, 9:31:00 PM] Jake Morales: {LRM}This message was deleted.\r\n"
            f"{LRM}[7/27/26, 9:32:00 PM] Alex Rivera: {LRM}You deleted this message.\r\n")
    scan = wa.scan(android_export(tmp_path, text, media=()), me)
    assert _bodies(scan) == ["hi"]
    assert "2 × deleted message ('This message was deleted')" in scan["skipped"]


# ---------------------------------------------------------------- edits and media

def test_the_edited_marker_is_taken_off_and_recorded(me, tmp_path):
    recs = _by_body(wa.scan(android_export(tmp_path), me))
    assert json.loads(recs["best trip ever"]["value_json"]) == {"edited": True}
    ios = _by_body(wa.scan(ios_zip(tmp_path / "ios"), me))
    assert json.loads(ios["We'll be there"]["value_json"])["edited"] is True


def test_media_in_the_export_is_a_relative_path_never_bytes(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    photo = _by_body(scan)["the rowboat on Lake Bled!"]
    assert photo["media_path"] == "IMG-20260727-WA0003.jpg"
    assert json.loads(photo["value_json"]) == {"attachment": "IMG-20260727-WA0003.jpg",
                                               "media": "image"}
    video = next(r for r in scan["records"] if '"video"' in (r["value_json"] or ""))
    assert video["media_path"] is None            # named, but not in the export
    assert video["body"] is None
    assert scan["facts"]["media"] == {"present": 1, "missing": 1, "omitted": 1}


def test_media_inside_the_iphone_zip_is_found_by_its_member_name(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    photo = next(r for r in scan["records"] if r["media_path"])
    assert photo["media_path"] == IOS_PHOTO
    assert photo["body"] is None and photo["direction"] == "out"


def test_media_exported_without_the_file_is_kept_and_marked(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    omitted = [r for r in scan["records"]
               if r["value_json"] and "media_omitted" in r["value_json"]]
    assert len(omitted) == 1 and omitted[0]["body"] is None
    ios = wa.scan(ios_zip(tmp_path / "ios"), me)
    image = next(r for r in ios["records"]
                 if r["value_json"] and "media_omitted" in r["value_json"])
    assert json.loads(image["value_json"]) == {"group": True, "media": "image",
                                               "media_omitted": True}


# ---------------------------------------------------------------- identity

def test_identical_messages_in_one_minute_stay_two(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    oks = [r for r in scan["records"] if r["body"] == "ok"]
    assert len(oks) == 2
    assert oks[0]["started_at"] == oks[1]["started_at"]
    assert oks[0]["external_id"] != oks[1]["external_id"]


def test_ids_are_stable_when_a_later_export_starts_later(me, tmp_path):
    """WhatsApp caps exports, so the same chat exported twice need not start
    at the same message; what both hold must get the same ids."""
    full = wa.scan(android_export(tmp_path / "a"), me)
    lines = ANDROID.splitlines(keepends=True)
    later = wa.scan(android_export(tmp_path / "b", "".join(lines[6:])), me)
    full_ids = {r["external_id"] for r in full["records"]}
    later_ids = {r["external_id"] for r in later["records"]}
    assert later_ids and later_ids < full_ids


def test_a_second_download_of_the_same_export_is_the_same_chat(me, tmp_path):
    a = wa.scan(android_export(tmp_path / "a"), me)
    b = wa.scan(android_export(tmp_path / "b", suffix=" (1)"), me)
    assert b["facts"]["chats"][0]["chat"] == "Jake Morales"
    assert [r["external_id"] for r in a["records"]] == \
           [r["external_id"] for r in b["records"]]


def test_scan_is_deterministic(me, tmp_path):
    p = ios_zip(tmp_path)
    a, b = wa.scan(p, me), wa.scan(p, me)
    assert a["records"] == b["records"]
    assert a["source_hash"] == b["source_hash"] == base.file_hash(p)


# ---------------------------------------------------------------- dates and times

def test_a_day_over_12_makes_the_file_day_first(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    chat = scan["facts"]["chats"][0]
    assert chat["date_order"] == "day-first" and chat["date_order_from"] == "e.g. 27/07/2026"
    assert _by_body(scan)["morning run?"]["journal_date"] == "2026-08-13"
    assert scan["facts"]["warnings"] == []


def test_a_second_field_over_12_makes_the_file_month_first(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    assert scan["facts"]["chats"][0]["date_order"] == "month-first"
    assert _by_body(scan)["Family dinner on Sunday?"]["journal_date"] == "2026-10-10"


AMBIGUOUS = ("01/02/2026, 10:00 - Jake Morales: hi\n"
             "02/03/2026, 11:00 - Alex Rivera: yo\n")


def test_an_ambiguous_file_is_read_day_first_and_says_it_guessed(me, conn, tmp_path):
    scan = wa.scan(android_export(tmp_path, AMBIGUOUS, media=()), me)
    assert scan["records"][0]["journal_date"] == "2026-02-01"
    assert scan["facts"]["chats"][0]["date_order_from"] == "guessed"
    report = _report(conn, scan)
    assert "WARNING" in report and 'date_order = "mdy"' in report


def test_the_date_order_setting_settles_an_ambiguous_file(me, tmp_path):
    me.connectors["whatsapp"]["date_order"] = "mdy"
    scan = wa.scan(android_export(tmp_path, AMBIGUOUS, media=()), me)
    assert scan["records"][0]["journal_date"] == "2026-01-02"
    assert scan["facts"]["warnings"] == []


def test_the_dates_overrule_a_wrong_setting_and_say_so(me, tmp_path):
    me.connectors["whatsapp"]["date_order"] = "month-first"
    scan = wa.scan(android_export(tmp_path), me)
    assert scan["facts"]["chats"][0]["date_order"] == "day-first"
    assert any("date_order ignored" in w for w in scan["facts"]["warnings"])


def test_dates_that_fit_neither_order_fail_naming_both(me, tmp_path):
    text = ("13/01/2026, 10:00 - Jake Morales: hi\n"
            "01/13/2026, 11:00 - Alex Rivera: yo\n")
    with pytest.raises(DomainError) as e:
        wa.scan(android_export(tmp_path, text, media=()), me)
    assert "13/01/2026" in str(e.value) and "01/13/2026" in str(e.value)


def test_an_impossible_date_names_its_line(me, tmp_path):
    text = ("13/01/2026, 10:00 - Jake Morales: hi\n"
            "31/02/2026, 11:00 - Alex Rivera: yo\n")
    with pytest.raises(DomainError) as e:
        wa.scan(android_export(tmp_path, text, media=()), me)
    assert "line 2" in str(e.value) and "31/02/2026" in str(e.value)


def test_a_year_first_locale(me, tmp_path):
    text = "2026-07-27, 21:30 - Jake Morales: hi\n2026-07-28 08:00 - Alex Rivera: yo\n"
    scan = wa.scan(android_export(tmp_path, text, media=()), me)
    assert [r["journal_date"] for r in scan["records"]] == ["2026-07-27", "2026-07-28"]


@pytest.mark.parametrize("clock,utc", [
    ("12:05 AM", "2026-07-27T05:05:00Z"),     # just after midnight, CDT
    ("12:30 PM", "2026-07-27T17:30:00Z"),     # just after noon
    ("9:30 PM", "2026-07-28T02:30:00Z"),
    (f"9:30{NNBSP}PM", "2026-07-28T02:30:00Z"),
    ("9:30 p.m.", "2026-07-28T02:30:00Z"),
])
def test_twelve_hour_clocks(me, tmp_path, clock, utc):
    text = f"7/27/26, {clock} - Jake Morales: hi\n7/13/26, 1:00 PM - Alex Rivera: yo\n"
    scan = wa.scan(android_export(tmp_path, text, media=()), me)
    assert scan["records"][-1]["started_at"] == utc


def test_times_are_read_in_the_home_zone_and_the_rollover_applies(me, tmp_path):
    """00:20 in Austin is 05:20Z — but before the 05:00 local rollover, so it
    belongs to the evening before."""
    rec = _by_body(wa.scan(android_export(tmp_path), me))["still up?"]
    assert rec["started_at"] == "2026-07-28T05:20:00Z"
    assert rec["journal_date"] == "2026-07-27"
    assert rec["utc_offset_sec"] is None            # assumed, not recorded


def test_an_export_made_abroad_can_name_its_zone(me, tmp_path):
    me.connectors["whatsapp"]["timezone"] = "Europe/Ljubljana"
    rec = _by_body(wa.scan(android_export(tmp_path), me))["morning run?"]
    assert rec["started_at"] == "2026-08-13T04:30:00Z"     # 06:30 CEST


def test_android_minutes_and_iphone_seconds(me, tmp_path):
    android = wa.scan(android_export(tmp_path / "a"), me)
    assert all(r["started_at"].endswith(":00Z") for r in android["records"])
    ios = _by_body(wa.scan(ios_zip(tmp_path / "i"), me))
    assert ios["Family dinner on Sunday?"]["started_at"] == "2026-10-11T00:00:07Z"


# ---------------------------------------------------------------- senders and handles

def test_names_become_whatsapp_accounts_named_by_the_export(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    assert scan["handles"] == [
        {"kind": "app_account", "value": "whatsapp:Alex Rivera",
         "display_name": "Alex Rivera"},
        {"kind": "app_account", "value": "whatsapp:Jake Morales",
         "display_name": "Jake Morales"}]


def test_an_unsaved_number_is_a_phone_handle_from_either_phone(me, tmp_path):
    ios = wa.scan(ios_zip(tmp_path / "i"), me)
    android = wa.scan(android_export(tmp_path / "a", ANDROID_GROUP,
                                     chat="Rivera Family", media=()), me)
    phone = {"kind": "phone", "value": "+12105550131", "display_name": None}
    assert phone in ios["handles"]
    # On Android the number only joined (a system line): it sent nothing.
    assert phone not in android["handles"]
    text = ANDROID_GROUP + "20/08/2025, 18:50 - +1 210-555-0131: Sí! Bring Maya\n"
    android = wa.scan(android_export(tmp_path / "b", text, chat="Rivera Family",
                                     media=()), me)
    assert phone in android["handles"]


def test_a_chosen_name_keeps_its_tilde_in_the_handle_but_not_the_display_name(me, tmp_path):
    # The '~' marks someone who is not in the contacts: it keeps them apart
    # from a contact who happens to share the name (tests/test_fix_connectors_whatsapp.py).
    scan = wa.scan(ios_zip(tmp_path), me)
    assert {"kind": "app_account", "value": "whatsapp:~Tío Beto",
            "display_name": "Tío Beto"} in scan["handles"]


def test_participants_carry_handles_never_names(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    handles = {(h["kind"], h["value"]) for h in scan["handles"]}
    for rec in scan["records"]:
        assert rec["participants"], rec
        for p in rec["participants"]:
            assert set(p) == {"kind", "value", "role"}
            assert (p["kind"], p["value"]) in handles
            assert p["kind"] in obs.HANDLE_KINDS and p["role"] in obs.ROLES


def test_a_group_message_names_every_member_and_you(me, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    rec = _by_body(scan)["Family dinner on Sunday?"]
    assert rec["participants"] == [
        {"kind": "app_account", "value": "whatsapp:Linda Rivera", "role": "sender"},
        {"kind": "app_account", "value": "whatsapp:Alex Rivera", "role": "recipient"},
        {"kind": "app_account", "value": "whatsapp:~Tío Beto", "role": "recipient"},
        {"kind": "phone", "value": "+12105550131", "role": "recipient"}]
    assert json.loads(rec["value_json"]) == {"group": True}
    assert scan["facts"]["chats"][0]["group"] is True


def test_a_one_to_one_message_names_the_other_person(me, tmp_path):
    scan = wa.scan(android_export(tmp_path), me)
    mine = _by_body(scan)["best trip ever"]
    assert mine["direction"] == "out"
    assert mine["participants"] == [
        {"kind": "app_account", "value": "whatsapp:Alex Rivera", "role": "sender"},
        {"kind": "app_account", "value": "whatsapp:Jake Morales", "role": "recipient"}]
    theirs = _by_body(scan)["still up?"]
    assert theirs["direction"] == "in"
    assert theirs["participants"][1]["value"] == "whatsapp:Alex Rivera"
    assert scan["facts"]["chats"][0]["group"] is False


def test_a_chat_where_only_you_wrote_still_names_the_other_person(me, tmp_path):
    text = "27/07/2026, 21:30 - Alex Rivera: you there?\n13/08/2026, 09:00 - Alex Rivera: ?\n"
    scan = wa.scan(android_export(tmp_path, text, media=()), me)
    assert scan["records"][0]["participants"][1] == {
        "kind": "app_account", "value": "whatsapp:Jake Morales", "role": "recipient"}
    assert {"kind": "app_account", "value": "whatsapp:Jake Morales",
            "display_name": "Jake Morales"} in scan["handles"]


# ---------------------------------------------------------------- which sender is you

def test_a_one_to_one_chat_gives_you_away(cfg, tmp_path):
    scan = wa.scan(android_export(tmp_path), cfg)
    assert scan["facts"]["you"] == "Alex Rivera"
    assert scan["facts"]["you_from"].startswith("inferred: the other sender")
    assert _by_body(scan)["best trip ever"]["direction"] == "out"
    assert scan["account"] == "whatsapp:Alex Rivera"


def test_the_one_sender_in_every_chat_is_you(cfg, tmp_path):
    folder = tmp_path / "exports"
    ios_zip(folder)
    android_export(folder, ANDROID_GROUP.replace("Linda Rivera", "Sam Okafor"),
                   chat="Gym crew", media=())
    scan = wa.scan(folder, cfg)
    assert scan["facts"]["you"] == "Alex Rivera"
    assert "every chat" in scan["facts"]["you_from"]


def test_with_no_way_to_tell_direction_stays_unknown_and_the_report_says_so(
        cfg, conn, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), cfg)
    assert scan["facts"]["you"] is None and scan["account"] is None
    assert {r["direction"] for r in scan["records"]} == {None}
    report = _report(conn, scan)
    assert "WARNING nothing in the export says which sender is you" in report
    assert "[connectors.whatsapp] me" in report


def test_a_me_that_matches_no_sender_is_flagged(cfg, tmp_path):
    cfg.connectors = {"whatsapp": {"me": "Alex Rivers"}}
    scan = wa.scan(android_export(tmp_path), cfg)
    assert any("Alex Rivers" in w for w in scan["facts"]["warnings"])


def test_me_can_list_several_names(cfg, tmp_path):
    cfg.connectors = {"whatsapp": {"me": ["Alex", "Alex Rivera"]}}
    scan = wa.scan(android_export(tmp_path), cfg)
    assert _by_body(scan)["best trip ever"]["direction"] == "out"


@pytest.mark.parametrize("settings,fragment", [
    ({"me": 5}, "me must be your name"),
    ({"date_order": "dym"}, "date_order must be"),
    ({"timezone": "Mars/Olympus"}, "not a known IANA zone"),
])
def test_bad_settings_fail_saying_which(cfg, tmp_path, settings, fragment):
    cfg.connectors = {"whatsapp": settings}
    with pytest.raises(DomainError) as e:
        wa.scan(android_export(tmp_path), cfg)
    assert fragment in str(e.value)


# ---------------------------------------------------------------- targets

def test_a_folder_of_exports_reads_each_chat_and_ignores_the_rest(me, tmp_path):
    folder = tmp_path / "Downloads"
    android_export(folder)
    ios_zip(folder)
    unzipped = folder / "WhatsApp Chat - Gym crew"
    unzipped.mkdir()
    (unzipped / "_chat.txt").write_bytes(
        "[8/20/25, 6:45:00 PM] Sam Okafor: gym 6:30?\r\n".encode("utf-8"))
    (folder / "notes.pdf").write_bytes(b"%PDF")
    with zipfile.ZipFile(folder / "photos.zip", "w") as zf:
        zf.writestr("a.jpg", b"x")
    scan = wa.scan(folder, me)
    assert [c["chat"] for c in scan["facts"]["chats"]] == [
        "Gym crew", "Rivera Family", "Jake Morales"]
    assert scan["target_kind"] == "folder"
    assert {r["title"] for r in scan["records"]} == {"Gym crew", "Rivera Family",
                                                    "Jake Morales"}


def test_the_same_chat_twice_in_a_folder_counts_as_duplicates(me, conn, tmp_path):
    folder = tmp_path / "exports"
    android_export(folder)
    android_export(folder, suffix=" (1)", media=())
    scan = wa.scan(folder, me)
    plan = obs.build_plan(conn, scan)
    assert len(plan["new"]) == ANDROID_MESSAGES
    assert plan["dup_incoming"] == ANDROID_MESSAGES
    assert "duplicate message(s) across the exports" in "\n".join(
        wa.report_lines(scan, plan))


def test_an_unzipped_iphone_export_takes_its_folders_name(me, tmp_path):
    folder = tmp_path / "WhatsApp Chat - Rivera Family"
    folder.mkdir()
    (folder / "_chat.txt").write_bytes(IOS.encode("utf-8"))
    (folder / IOS_PHOTO).write_bytes(b"x")
    from_folder = wa.scan(folder, me)
    from_zip = wa.scan(ios_zip(tmp_path / "z"), me)
    assert from_folder["records"] == from_zip["records"]
    assert wa.scan(folder / "_chat.txt", me)["records"] == from_zip["records"]


def test_a_folder_hash_changes_when_media_arrives(me, tmp_path):
    folder = tmp_path / "exports"
    android_export(folder, media=())
    before = wa.scan(folder, me)["source_hash"]
    (folder / "IMG-20260727-WA0003.jpg").write_bytes(b"x")
    after = wa.scan(folder, me)
    assert after["source_hash"] != before
    assert _by_body(after)["the rowboat on Lake Bled!"]["media_path"]


def test_utf8_bom_export_is_accepted(me, tmp_path):
    p = android_export(tmp_path)
    p.write_bytes(b"\xef\xbb\xbf" + ANDROID.encode("utf-8"))
    assert len(wa.scan(p, me)["records"]) == ANDROID_MESSAGES


def test_a_text_file_that_is_not_a_chat_says_what_it_saw(me, tmp_path):
    p = tmp_path / "WhatsApp Chat with Nobody.txt"
    p.write_text("Shopping list\nmilk\n", encoding="utf-8")
    with pytest.raises(DomainError) as e:
        wa.scan(p, me)
    assert "no WhatsApp message lines" in str(e.value)
    assert "Shopping list" in str(e.value)


def test_a_zip_without_a_chat_lists_its_members(me, tmp_path):
    z = tmp_path / "WhatsApp Chat - Nothing.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("IMG-1.jpg", b"x")
    with pytest.raises(DomainError) as e:
        wa.scan(z, me)
    assert "IMG-1.jpg" in str(e.value)


def test_a_folder_without_exports_lists_what_it_has(me, tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "holiday.jpg").write_bytes(b"x")
    with pytest.raises(DomainError) as e:
        wa.scan(tmp_path / "x", me)
    assert "holiday.jpg" in str(e.value)


def test_another_kind_of_file_is_refused(me, tmp_path):
    p = tmp_path / "chat.pdf"
    p.write_bytes(b"%PDF-1.7")
    with pytest.raises(DomainError) as e:
        wa.scan(p, me)
    assert "chat.pdf is not a WhatsApp chat export" in str(e.value)


def test_text_that_is_not_utf8_fails_on_one_line(me, tmp_path):
    p = android_export(tmp_path, media=())
    p.write_bytes("27/07/2026, 21:30 - Jake: caf\N{LATIN SMALL LETTER E WITH ACUTE}\n"
                  .encode("cp1252"))
    with pytest.raises(DomainError) as e:
        wa.scan(p, me)
    assert "is not valid UTF-8" in str(e.value)


def test_default_target_is_the_configured_exports_folder(cfg, tmp_path):
    folder = tmp_path / "wa-exports"
    android_export(folder)
    cfg.connectors = {"whatsapp": {"exports_dir": str(folder)}}
    assert wa.default_target(cfg) == folder
    cfg.connectors = {"whatsapp": {"exports_dir": "wa-exports"}}       # relative to root
    assert wa.default_target(cfg) == cfg.root / "wa-exports"


def test_default_target_falls_back_to_downloads(cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(DomainError) as e:
        wa.default_target(cfg)
    assert "no WhatsApp chat export found" in str(e.value)
    ios_zip(home / "Downloads")
    assert wa.default_target(cfg) == home / "Downloads"


# ---------------------------------------------------------------- report

def test_the_report_reads_like_the_other_connectors(me, conn, tmp_path):
    text = _report(conn, wa.scan(android_export(tmp_path), me))
    assert text.startswith("whatsapp: WhatsApp Chat with Jake Morales.txt (1 chat export(s)")
    assert "chat: Jake Morales — one-to-one, 10 message(s) 2026-07-27..2026-08-13" in text
    assert "android format, dates day-first (e.g. 27/07/2026)" in text
    assert "you: Alex Rivera ([connectors.whatsapp] me)" in text
    assert "times read as America/Chicago" in text
    assert "whatsapp: 10 new, 0 changed | 2026-07-27..2026-08-13 | e.g. 2026-08-13 them: " \
           "morning run?" in text
    assert "media: 1 file(s) in the export" in text
    assert "handles: 1 (1 by the name the export shows, 0 by phone number)" in text
    assert "skipped: 1 × deleted message" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reapply_is_all_identical(me, conn, tmp_path):
    scan = wa.scan(ios_zip(tmp_path), me)
    _, first = _apply(conn, scan)
    assert first["added"] == len(scan["records"]) == 6
    plan, again = _apply(conn, wa.scan(ios_zip(tmp_path), me))
    assert again["added"] == 0 and again["identical"] == 6
    assert plan["conflicts"] == [] and plan["updates"] == []
    assert "nothing new" in "\n".join(wa.report_lines(scan, plan))


def test_the_archive_remembers_what_a_later_export_lacks(me, conn, tmp_path):
    """Exports are capped and per chat: a shorter re-export is not a list of
    deletions, so nothing is removed."""
    _apply(conn, wa.scan(android_export(tmp_path / "a"), me))
    later = wa.scan(android_export(tmp_path / "b", "".join(
        ANDROID.splitlines(keepends=True)[6:])), me)
    assert "present_ids" not in later
    plan, result = _apply(conn, later)
    assert plan["removed"] == [] and plan["new"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE "
                        "stream='whatsapp' AND status='active'").fetchone()["c"] \
        == ANDROID_MESSAGES


def test_an_edit_made_after_an_import_arrives_beside_the_old_wording(me, conn, tmp_path):
    _apply(conn, wa.scan(android_export(tmp_path / "a", ANDROID.replace(
        "best trip ever <This message was edited>", "best trp ever")), me))
    plan, _ = _apply(conn, wa.scan(android_export(tmp_path / "b"), me))
    assert [r["body"] for r in plan["new"]] == ["best trip ever"]
    bodies = {r["body"] for r in conn.execute(
        "SELECT body FROM observations WHERE stream='whatsapp'")}
    assert {"best trp ever", "best trip ever"} <= bodies


def test_handles_land_unlinked_with_their_display_names(me, conn, tmp_path):
    _apply(conn, wa.scan(ios_zip(tmp_path), me))
    rows = {r["value"]: r for r in conn.execute("SELECT * FROM entity_handles")}
    assert rows["whatsapp:Linda Rivera"]["display_name"] == "Linda Rivera"
    assert rows["+12105550131"]["display_name"] is None
    assert all(r["entity_id"] is None for r in rows.values())
    assert rows["whatsapp:Linda Rivera"]["observation_count"] == 6


def test_the_importer_writes_none_of_the_confirmed_tables(me, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    _apply(conn, wa.scan(android_export(tmp_path), me))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before


def test_doctor_stays_green_after_a_whatsapp_import_and_its_retraction(me, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, me)
    assert run_checks(conn, me)["failures"] == 0, "seed itself is not green"
    folder = tmp_path / "exports"
    android_export(folder)
    ios_zip(folder)
    _, result = _apply(conn, wa.scan(folder, me))
    report = run_checks(conn, me)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert run_checks(conn, me)["failures"] == 0


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra: str = 'me = "Alex Rivera"\n'):
    root = cli_project(tmp_path, monkeypatch)
    with open(root / "config.toml", "a", encoding="utf-8") as f:
        f.write("[connectors.whatsapp]\n" + extra)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def test_cli_dry_run_apply_and_reimport(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = android_export(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "whatsapp", str(export)])
    assert r.exit_code == 0, r.output
    assert "whatsapp: 10 new" in r.output
    assert "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0

    r = runner.invoke(app, ["observe", "whatsapp", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 10 added" in r.output

    r = runner.invoke(app, ["observe", "whatsapp", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "0 added" in r.output and "10 identical skipped" in r.output
    r = runner.invoke(app, ["observe", "whatsapp", str(export)])
    assert "already imported" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_observe_without_a_path_uses_the_exports_folder(tmp_path, monkeypatch):
    folder = tmp_path / "wa-exports"
    ios_zip(folder)
    _cli_setup(tmp_path, monkeypatch,
               f'me = "Alex Rivera"\nexports_dir = \'{folder}\'\n')
    r = runner.invoke(app, ["observe", "whatsapp", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["new"] == 6
    assert out["facts"]["chats"][0]["chat"] == "Rivera Family"


def test_cli_a_bad_export_is_one_error_line(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = tmp_path / "chat.pdf"
    p.write_bytes(b"%PDF")
    r = runner.invoke(app, ["observe", "whatsapp", str(p)])
    assert r.exit_code == 1
    assert "is not a WhatsApp chat export" in r.output
    assert "Traceback" not in r.output


def test_cli_connectors_lists_the_stream(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "whatsapp" in r.output
