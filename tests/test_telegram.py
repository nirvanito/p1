"""Telegram connector: Telegram Desktop's export in machine-readable JSON.

The connector is a pure reader, so most of this runs scan() against exports
built here — shaped like the real ones: the full export's
personal_information and chats.list, a single chat's own result.json, text
written as a string or as a list of formatting runs, service messages,
media the export settings left out, edits with their own times, and an
export made in HTML.
"""

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import telegram as tg
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

ME, SAM, MAYA = 700000100, 700000166, 700000187
GROUP = 4000000007
NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"
# 2026-07-27 21:30 in Austin is 2026-07-28 02:30 UTC.
T0 = datetime(2026, 7, 28, 2, 30, tzinfo=timezone.utc)
PHOTO = "chats/chat_001/photos/photo_1@27-07-2026_21-31-00.jpg"


def stamp(dt: datetime, key: str = "date") -> dict:
    local = dt.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
    return {key: local.isoformat(timespec="seconds"),
            f"{key}_unixtime": str(int(dt.timestamp()))}


def m(mid, when, who, uid, text="", **extra) -> dict:
    out = {"id": mid, "type": "message", **stamp(when), "from": who, "from_id": f"user{uid}",
           "text": text,
           "text_entities": [{"type": "plain", "text": text}] if isinstance(text, str)
           and text else []}
    out.update(extra)
    return out


def service(mid, when, who, uid, action, **extra) -> dict:
    return {"id": mid, "type": "service", **stamp(when), "actor": who,
            "actor_id": f"user{uid}", "action": action, "text": "", "text_entities": [],
            **extra}


def sam_chat() -> dict:
    return {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [
        service(1, T0 - timedelta(days=5), "Sam Okafor", SAM, "phone_call",
                duration_seconds=180, discard_reason="hangup"),
        m(2, T0, "Sam Okafor", SAM, "Want to try the new climbing gym on Saturday?"),
        m(3, T0 + timedelta(minutes=1), "Alex Rivera", ME, "Yes! Morning works",
          **stamp(T0 + timedelta(minutes=3), "edited")),
        m(4, T0 + timedelta(minutes=2), "Sam Okafor", SAM,
          ["Opening week is ", {"type": "bold", "text": "half price"},
           " ", {"type": "link", "text": "https://example.com/gym"}]),
        m(5, T0 + timedelta(minutes=4), "Sam Okafor", SAM, "Their comp wall",
          photo=PHOTO, width=1280, height=960),
        m(6, T0 + timedelta(minutes=5), "Sam Okafor", SAM, "", file=NOT_INCLUDED,
          media_type="sticker", sticker_emoji="\N{FLEXED BICEPS}"),
    ]}


SAM_MESSAGES = 5


def group_chat() -> dict:
    return {"name": "Saturday Climbers", "type": "private_group", "id": GROUP, "messages": [
        service(10, T0, "Sam Okafor", SAM, "create_group", title="Saturday Climbers",
                members=["Sam Okafor", "Maya Chen", "Alex Rivera"]),
        m(11, T0 + timedelta(minutes=10), "Sam Okafor", SAM, "Saturday 10am, who's in?"),
        m(12, T0 + timedelta(minutes=11), "Maya Chen", MAYA,
          [{"type": "bold", "text": "I'm in"}, ", I'll bring chalk"]),
        m(13, T0 + timedelta(minutes=12), "Alex Rivera", ME, "Bringing snacks",
          reactions=[{"type": "emoji", "count": 2, "emoji": "\N{THUMBS UP SIGN}",
                      "recent": [{"from": "Sam Okafor", "from_id": f"user{SAM}",
                                  **stamp(T0 + timedelta(minutes=14))},
                                 {"from": "Maya Chen", "from_id": f"user{MAYA}",
                                  **stamp(T0 + timedelta(minutes=15))}]}]),
        service(14, T0 + timedelta(minutes=20), "Sam Okafor", SAM, "pin_message",
                message_id=11),
    ]}


GROUP_MESSAGES = 3


def info() -> dict:
    return {"user_id": ME, "first_name": "Alex", "last_name": "Rivera",
            "phone_number": "+1 512 555 0100", "username": "@alexrivera_atx", "bio": ""}


def full_export(folder: Path, chats=None, personal=True, left=None, extra=None,
                media=(PHOTO,), raw: bytes | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = {"about": "Here is the data you requested."}
    if personal:
        data["personal_information"] = info()
    data["contacts"] = {"about": "contacts", "list": [{"first_name": "Sam"}, {"first_name": "Maya"}]}
    data["chats"] = {"about": "chats", "list": [sam_chat(), group_chat()]
                     if chats is None else chats}
    if left is not None:
        data["left_chats"] = {"about": "left", "list": left}
    data.update(extra or {})
    (folder / "result.json").write_bytes(
        raw if raw is not None else json.dumps(data, indent=1, ensure_ascii=False).encode())
    for p in media:
        (folder / p).parent.mkdir(parents=True, exist_ok=True)
        (folder / p).write_bytes(b"\xff\xd8 bytes the importer never reads")
    return folder


def single_export(folder: Path, chat: dict, media=()) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(chat, indent=1, ensure_ascii=False),
                                        encoding="utf-8")
    for p in media:
        (folder / p).parent.mkdir(parents=True, exist_ok=True)
        (folder / p).write_bytes(b"x")
    return folder


def _by_body(scan):
    return {r["body"]: r for r in scan["records"] if r["body"]}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _report(conn, scan):
    return "\n".join(tg.report_lines(scan, obs.build_plan(conn, scan)))


# ---------------------------------------------------------------- shape

def test_a_full_export_reads_into_the_telegram_stream(cfg, tmp_path):
    scan = tg.scan(full_export(tmp_path / "DataExport_2026-07-30"), cfg)
    assert scan["connector"] == "telegram"
    assert len(scan["records"]) == SAM_MESSAGES + GROUP_MESSAGES
    assert {r["stream"] for r in scan["records"]} == {"telegram"}
    assert {r["origin_app"] for r in scan["records"]} == {"telegram"}
    assert {r["title"] for r in scan["records"]} == {"Sam Okafor", "Saturday Climbers"}
    assert _by_body(scan)["Want to try the new climbing gym on Saturday?"][
        "external_id"] == f"tg:{SAM}:2"
    assert scan["facts"]["exports"] == [{"export": "DataExport_2026-07-30",
                                         "kind": "full export", "chats": 2}]


def test_a_single_chat_export_gives_the_same_ids(cfg, tmp_path):
    full = tg.scan(full_export(tmp_path / "full", chats=[sam_chat()]), cfg)
    one = tg.scan(single_export(tmp_path / "ChatExport_2026-07-30", sam_chat(),
                                media=(PHOTO,)), cfg)
    assert one["facts"]["exports"][0]["kind"] == "single chat"
    assert [r["external_id"] for r in one["records"]] == \
        [r["external_id"] for r in full["records"]]
    assert [r["direction"] for r in one["records"]] == \
        [r["direction"] for r in full["records"]]


def test_result_json_itself_or_a_zip_of_the_folder_reads_the_same(cfg, tmp_path):
    folder = full_export(tmp_path / "DataExport_2026-07-30")
    from_folder = tg.scan(folder, cfg)
    assert tg.scan(folder / "result.json", cfg)["records"] == from_folder["records"]
    z = tmp_path / "telegram.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in folder.rglob("*"):
            if p.is_file():
                zf.write(p, f"DataExport_2026-07-30/{p.relative_to(folder).as_posix()}")
    from_zip = tg.scan(z, cfg)
    assert from_zip["records"] == from_folder["records"]
    # One export, however it is handed over: "already imported" holds.
    assert from_zip["source_hash"] == from_folder["source_hash"] == \
        tg.scan(folder / "result.json", cfg)["source_hash"]


def test_text_runs_are_flattened_to_what_the_chat_shows(cfg, tmp_path):
    bodies = _by_body(tg.scan(full_export(tmp_path), cfg))
    assert "Opening week is half price https://example.com/gym" in bodies
    assert "I'm in, I'll bring chalk" in bodies
    assert tg.flatten(["a", {"type": "bold", "text": "b"}, {"type": "x"}, 3]) == "ab"
    chat = {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [
        {**m(2, T0, "Sam Okafor", SAM, ""),
         "text_entities": [{"type": "plain", "text": "only in entities"}]}]}
    scan = tg.scan(single_export(tmp_path / "e", chat), cfg)
    assert [r["body"] for r in scan["records"]] == ["only in entities"]


# ---------------------------------------------------------------- time

def test_times_come_from_date_unixtime_and_the_rollover_applies(cfg, tmp_path):
    late = T0.replace(hour=8, minute=30)            # 03:30 in Austin
    chat = {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [
        m(1, late, "Sam Okafor", SAM, "still up?"),
        m(2, late + timedelta(hours=2), "Sam Okafor", SAM, "gym?")]}
    early, later = tg.scan(single_export(tmp_path, chat), cfg)["records"]
    assert early["started_at"] == "2026-07-28T08:30:00Z"
    assert early["journal_date"] == "2026-07-27"
    assert later["journal_date"] == "2026-07-28"


def test_an_export_without_unix_times_is_read_in_the_home_zone(cfg, tmp_path):
    msg = m(1, T0, "Sam Okafor", SAM, "old export")
    del msg["date_unixtime"]
    msg["date"] = "2026-07-27T21:30:00"
    chat = {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [msg]}
    (rec,) = tg.scan(single_export(tmp_path, chat), cfg)["records"]
    assert rec["started_at"] == "2026-07-28T02:30:00Z"        # CDT is UTC-5
    assert rec["journal_date"] == "2026-07-27"


# ---------------------------------------------------------------- handles

def test_senders_are_user_id_handles_with_the_name_shown(cfg, tmp_path):
    scan = tg.scan(full_export(tmp_path), cfg)
    assert scan["handles"] == [
        {"kind": "app_account", "value": f"telegram:user{ME}", "display_name": "Alex Rivera"},
        {"kind": "app_account", "value": f"telegram:user{SAM}", "display_name": "Sam Okafor"},
        {"kind": "app_account", "value": f"telegram:user{MAYA}", "display_name": "Maya Chen"}]
    ask = _by_body(scan)["Want to try the new climbing gym on Saturday?"]
    assert ask["participants"] == [
        {"kind": "app_account", "value": f"telegram:user{SAM}", "role": "sender"},
        {"kind": "app_account", "value": f"telegram:user{ME}", "role": "recipient"}]
    reply = _by_body(scan)["Yes! Morning works"]
    assert reply["participants"][1] == {"kind": "app_account",
                                        "value": f"telegram:user{SAM}", "role": "recipient"}
    snacks = _by_body(scan)["Bringing snacks"]
    assert {p["value"] for p in snacks["participants"] if p["role"] == "recipient"} == {
        f"telegram:user{SAM}", f"telegram:user{MAYA}"}
    values = {h["value"] for h in scan["handles"]}
    for r in scan["records"]:
        assert {p["value"] for p in r["participants"]} <= values


def test_a_deleted_account_keeps_its_id_and_old_numeric_ids_read_too(cfg, tmp_path):
    gone = {**m(1, T0, None, 999, "hello from the past"), "from": None}
    old = {**m(2, T0 + timedelta(minutes=1), "Sam Okafor", SAM, "numeric"), "from_id": SAM}
    chat = {"name": None, "type": "personal_chat", "id": 999, "messages": [gone]}
    scan = tg.scan(full_export(tmp_path, chats=[chat, {
        "name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [old]}]), cfg)
    handles = {h["value"]: h for h in scan["handles"]}
    assert handles["telegram:user999"]["display_name"] is None
    assert _by_body(scan)["hello from the past"]["title"] == "Deleted Account"
    assert _by_body(scan)["numeric"]["participants"][0]["value"] == f"telegram:user{SAM}"


# ---------------------------------------------------------------- who is you

def test_personal_information_says_which_sender_is_you(cfg, tmp_path):
    scan = tg.scan(full_export(tmp_path), cfg)
    assert scan["facts"]["you_id"] == f"user{ME}"
    assert scan["facts"]["you_from"] == "the export's personal_information"
    assert scan["account"] == f"telegram:user{ME}"
    bodies = _by_body(scan)
    assert bodies["Yes! Morning works"]["direction"] == "out"
    assert bodies["Saturday 10am, who's in?"]["direction"] == "in"


def test_a_single_chat_export_infers_you_from_the_chats_own_id(cfg, tmp_path):
    scan = tg.scan(single_export(tmp_path, sam_chat()), cfg)
    assert scan["facts"]["you_id"] == f"user{ME}"
    assert scan["facts"]["you_from"].startswith("inferred: the other sender in the chat")
    assert _by_body(scan)["Yes! Morning works"]["direction"] == "out"


def test_me_in_the_config_names_you_by_id_or_by_name(cfg, tmp_path):
    export = single_export(tmp_path, group_chat())
    for me in (f"user{ME}", "Alex Rivera", ["someone", "alex rivera"]):
        cfg.connectors = {"telegram": {"me": me}}
        scan = tg.scan(export, cfg)
        assert _by_body(scan)["Bringing snacks"]["direction"] == "out", me
        assert scan["facts"]["warnings"] == []
    cfg.connectors = {"telegram": {"me": "user1"}}
    scan = tg.scan(export, cfg)
    assert any("no sender in this export is [connectors.telegram] me" in w
               for w in scan["facts"]["warnings"])


def test_saved_messages_are_self_and_give_you_away(cfg, tmp_path):
    saved = {"type": "saved_messages", "id": ME, "messages": [
        m(1, T0, "Alex Rivera", ME, "pack the type F adapter")]}
    scan = tg.scan(full_export(tmp_path, chats=[saved, group_chat()], personal=False), cfg)
    note = _by_body(scan)["pack the type F adapter"]
    assert note["direction"] == "self" and note["title"] == "Saved Messages"
    assert note["participants"] == [{"kind": "app_account", "value": f"telegram:user{ME}",
                                     "role": "sender"}]
    assert scan["facts"]["you_from"].startswith("inferred: the sender of Saved Messages")
    assert _by_body(scan)["Bringing snacks"]["direction"] == "out"


def test_with_nothing_to_go_on_direction_stays_unknown_and_the_report_says_so(
        cfg, conn, tmp_path):
    scan = tg.scan(single_export(tmp_path, group_chat()), cfg)
    assert {r["direction"] for r in scan["records"]} == {None}
    assert "WARNING nothing in the export says which sender is you" in _report(conn, scan)


# ---------------------------------------------------------------- what is not a message

def test_service_messages_are_counted_by_action(cfg, tmp_path):
    scan = tg.scan(full_export(tmp_path), cfg)
    assert scan["skipped"] == ["1 × service: create_group", "1 × service: phone_call",
                               "1 × service: pin_message"]


def test_channels_and_bots_are_counted_unless_included(cfg, tmp_path):
    news = {"name": "Austin Climbing News", "type": "public_channel", "id": 1500000042,
            "messages": [{**m(9001, T0, "Austin Climbing News", 0, "gym opens"),
                          "from_id": "channel1500000042"}]}
    bot = {"name": "WeatherBot", "type": "bot_chat", "id": 5, "messages": [
        m(7, T0, "WeatherBot", 5, "sunny")]}
    export = full_export(tmp_path, chats=[sam_chat(), news, bot])
    scan = tg.scan(export, cfg)
    assert "gym opens" not in _by_body(scan) and "sunny" not in _by_body(scan)
    assert "1 × message in a bot chat" in scan["skipped"]
    assert "1 × message in a channel or public group (include_channels = true imports " \
           "them)" in scan["skipped"]
    cfg.connectors = {"telegram": {"include_channels": True}}
    scan = tg.scan(export, cfg)
    rec = _by_body(scan)["gym opens"]
    assert json.loads(rec["value_json"])["chat_type"] == "public_channel"
    assert rec["participants"][0]["value"] == "telegram:channel1500000042"
    assert "sunny" not in _by_body(scan)


def test_empty_messages_are_counted_and_unknown_ones_not_understood(cfg, tmp_path):
    chat = {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [
        m(1, T0, "Sam Okafor", SAM, ""),
        m(2, T0, "Sam Okafor", SAM, "", dice={"emoji": "\N{GAME DIE}", "value": 4}),
        {**m(3, T0, "Sam Okafor", SAM, "no time"), "date_unixtime": "", "date": ""},
        {"id": 4, "type": "mystery"},
        m(5, T0, "Sam Okafor", SAM, "kept"),
    ]}
    scan = tg.scan(single_export(tmp_path, chat), cfg)
    assert [r["body"] for r in scan["records"]] == ["kept"]
    assert "1 × empty message" in scan["skipped"]
    assert "1 × message with no usable timestamp" in scan["skipped"]
    assert scan["unmapped"] == [{"what": "message of type 'mystery'", "count": 1},
                                {"what": "message with no text, only: dice", "count": 1}]


def test_sections_that_are_not_chats_are_listed_not_imported(cfg, conn, tmp_path):
    scan = tg.scan(full_export(tmp_path, extra={"sessions": {"about": "", "list": [{}]}}),
                   cfg)
    assert scan["facts"]["other_sections"] == {"contacts": 2, "sessions": 1}
    assert "not chats (not imported): contacts (2), sessions (1)" in _report(conn, scan)


def test_left_chats_are_read_too(cfg, tmp_path):
    left = {"name": "Old Crew", "type": "private_supergroup", "id": 77, "messages": [
        m(1, T0, "Sam Okafor", SAM, "bye all")]}
    scan = tg.scan(full_export(tmp_path, left=[left]), cfg)
    assert _by_body(scan)["bye all"]["title"] == "Old Crew"
    assert next(c for c in scan["facts"]["chats"] if c["chat"] == "Old Crew")["left"]


# ---------------------------------------------------------------- media and detail

def test_media_is_a_path_inside_the_export_never_bytes(cfg, tmp_path):
    scan = tg.scan(full_export(tmp_path), cfg)
    photo = _by_body(scan)["Their comp wall"]
    assert photo["media_path"] == PHOTO
    assert json.loads(photo["value_json"])["media"] == {"kind": "photo", "file": PHOTO}
    sticker = next(r for r in scan["records"] if r["body"] is None)
    assert json.loads(sticker["value_json"])["media"] == {
        "kind": "sticker", "omitted": True, "sticker_emoji": "\N{FLEXED BICEPS}"}
    assert scan["facts"]["media"] == {"present": 1, "missing": 0, "omitted": 1}
    missing = tg.scan(full_export(tmp_path / "b", media=()), cfg)
    assert _by_body(missing)["Their comp wall"]["media_path"] is None
    assert missing["facts"]["media"]["missing"] == 1


def test_polls_places_replies_and_forwards_are_kept_as_detail(cfg, tmp_path):
    chat = {"name": "Sam Okafor", "type": "personal_chat", "id": SAM, "messages": [
        m(1, T0, "Sam Okafor", SAM, "", poll={"question": "Saturday or Sunday?",
                                              "answers": [{"text": "Sat"}, {"text": "Sun"}]}),
        m(2, T0 + timedelta(seconds=1), "Sam Okafor", SAM, "", place_name="The new gym",
          location_information={"latitude": 30.26, "longitude": -97.7}),
        m(3, T0 + timedelta(seconds=2), "Alex Rivera", ME, "this one",
          reply_to_message_id=1, forwarded_from="Someone"),
    ]}
    recs = tg.scan(single_export(tmp_path, chat), cfg)["records"]
    detail = {r["body"]: json.loads(r["value_json"]) for r in recs}
    assert detail["Saturday or Sunday?"]["poll"] == {"question": "Saturday or Sunday?",
                                                     "answers": 2}
    assert detail[None]["location"] == {"lat": 30.26, "lon": -97.7}
    assert detail[None]["venue"] == "The new gym"
    assert detail["this one"] == {"forwarded": True, "reply_to": 1}


def test_an_edit_carries_its_own_time(cfg, tmp_path):
    reply = _by_body(tg.scan(full_export(tmp_path), cfg))["Yes! Morning works"]
    assert json.loads(reply["value_json"]) == {"edited": True}
    assert reply["last_modified_at"] == "2026-07-28T02:33:00Z"
    assert reply["started_at"] == "2026-07-28T02:31:00Z"


# ---------------------------------------------------------------- determinism and input

def test_scans_are_deterministic(cfg, tmp_path):
    export = full_export(tmp_path)
    a, b = tg.scan(export, cfg), tg.scan(export, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    assert a["handles"] == b["handles"]


def test_an_html_export_is_refused_in_one_line(cfg, tmp_path):
    folder = tmp_path / "ChatExport_2026-07-30"
    folder.mkdir()
    (folder / "messages.html").write_text("<html></html>")
    for target in (folder, folder / "messages.html"):
        with pytest.raises(DomainError) as e:
            tg.scan(target, cfg)
        assert "is a Telegram export in HTML format" in str(e.value)
        assert "Machine-readable JSON" in str(e.value) and "\n" not in str(e.value)
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("DataExport/export_results.html", "<html></html>")
    with pytest.raises(DomainError) as e:
        tg.scan(z, cfg)
    assert "HTML format" in str(e.value)


def test_a_folder_of_exports_reads_each_and_leaves_html_out(cfg, tmp_path):
    folder = tmp_path / "Telegram Desktop"
    single_export(folder / "ChatExport_2026-07-01", sam_chat())
    single_export(folder / "ChatExport_2026-07-02", group_chat())
    (folder / "ChatExport_2025-01-01").mkdir()
    (folder / "ChatExport_2025-01-01" / "messages.html").write_text("<html></html>")
    scan = tg.scan(folder, cfg)
    assert {r["title"] for r in scan["records"]} == {"Sam Okafor", "Saturday Climbers"}
    assert [e["export"] for e in scan["facts"]["exports"]] == [
        "ChatExport_2026-07-01", "ChatExport_2026-07-02"]
    # Inferred from the one-to-one chat, it holds across both exports.
    assert _by_body(scan)["Bringing snacks"]["direction"] == "out"
    assert any("left out ChatExport_2025-01-01: exported as HTML" in w
               for w in scan["facts"]["warnings"])


def test_a_folder_without_an_export_lists_what_it_has(cfg, tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "holiday.jpg").write_bytes(b"x")
    with pytest.raises(DomainError) as e:
        tg.scan(tmp_path / "x", cfg)
    assert "holds no Telegram export" in str(e.value) and "holiday.jpg" in str(e.value)
    p = tmp_path / "chat.txt"
    p.write_text("hi")
    with pytest.raises(DomainError) as e:
        tg.scan(p, cfg)
    assert "chat.txt is not a Telegram export" in str(e.value)


def test_bad_json_fails_on_one_line_and_a_bom_is_accepted(cfg, tmp_path):
    with pytest.raises(DomainError) as e:
        tg.scan(full_export(tmp_path / "a", raw=b'{"chats": ['), cfg)
    assert "result.json is not valid JSON" in str(e.value) and "\n" not in str(e.value)
    good = json.dumps(sam_chat()).encode("utf-8")
    scan = tg.scan(full_export(tmp_path / "b", raw=b"\xef\xbb\xbf" + good), cfg)
    assert len(scan["records"]) == SAM_MESSAGES


def test_a_json_that_is_not_an_export_says_what_it_has(cfg, tmp_path):
    with pytest.raises(DomainError) as e:
        tg.scan(full_export(tmp_path, raw=b'{"conversations": [], "user": {}}'), cfg)
    assert "has neither 'messages'" in str(e.value)
    assert "conversations, user" in str(e.value)


@pytest.mark.parametrize("settings,words", [
    ({"me": 7}, "me must be your user id"),
    ({"include_channels": "yes"}, "include_channels must be true or false"),
])
def test_bad_settings_fail_on_one_line(cfg, tmp_path, settings, words):
    cfg.connectors = {"telegram": settings}
    with pytest.raises(DomainError) as e:
        tg.scan(full_export(tmp_path), cfg)
    assert words in str(e.value)


def test_default_target_is_the_configured_folder_or_telegram_desktops(
        cfg, tmp_path, monkeypatch):
    full_export(tmp_path / "tg-exports" / "DataExport_2026-07-30")
    cfg.connectors = {"telegram": {"exports_dir": "tg-exports"}}
    assert tg.default_target(cfg) == cfg.root / "tg-exports"
    cfg.connectors = {}
    home = tmp_path / "home"
    (home / "Downloads" / "Telegram Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(DomainError) as e:
        tg.default_target(cfg)
    assert "no Telegram export found" in str(e.value)
    single_export(home / "Downloads" / "Telegram Desktop" / "ChatExport_2026-07-30",
                  sam_chat())
    assert tg.default_target(cfg) == home / "Downloads" / "Telegram Desktop"


# ---------------------------------------------------------------- report

def test_the_report_reads_like_the_other_connectors(cfg, conn, tmp_path):
    text = _report(conn, tg.scan(full_export(tmp_path / "DataExport_2026-07-30"), cfg))
    assert text.startswith("telegram: DataExport_2026-07-30 (full export, 2 chat(s))")
    assert f"you: Alex Rivera (telegram:user{ME}, the export's personal_information)" in text
    assert "chat: Sam Okafor — one-to-one, 5 message(s) 2026-07-27..2026-07-27" in text
    assert "chat: Saturday Climbers — group, 3 message(s)" in text
    assert "telegram: 8 new, 0 changed | 2026-07-27..2026-07-27 | e.g. 2026-07-27 you: " \
           "Bringing snacks" in text
    assert "handles: 2 Telegram account(s), by user id" in text
    assert "media: 1 file(s) in the export" in text and "1 left out by the export" in text
    assert "skipped: 1 × service: phone_call" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reapply_is_all_identical(cfg, conn, tmp_path):
    export = full_export(tmp_path)
    scan = tg.scan(export, cfg)
    _, first = _apply(conn, scan)
    assert first["added"] == SAM_MESSAGES + GROUP_MESSAGES
    plan, again = _apply(conn, tg.scan(export, cfg))
    assert again["added"] == 0 and again["identical"] == SAM_MESSAGES + GROUP_MESSAGES
    assert plan["conflicts"] == [] and plan["updates"] == []
    assert "nothing new" in "\n".join(tg.report_lines(scan, plan))


def test_an_edit_after_an_import_updates_the_message(cfg, conn, tmp_path):
    chat = sam_chat()
    _apply(conn, tg.scan(single_export(tmp_path / "a", chat), cfg))
    chat["messages"][1].update(text="Want to try the new gym on Saturday morning?",
                               **stamp(T0 + timedelta(hours=1), "edited"))
    plan, result = _apply(conn, tg.scan(single_export(tmp_path / "b", chat), cfg))
    assert result["updated"] == 1 and plan["conflicts"] == []
    row = conn.execute("SELECT body, value_json FROM observations WHERE external_id=?",
                       (f"tg:{SAM}:2",)).fetchone()
    assert row["body"] == "Want to try the new gym on Saturday morning?"
    assert json.loads(row["value_json"]) == {"edited": True}


def test_a_reaction_added_later_updates_instead_of_conflicting(cfg, conn, tmp_path):
    chat = sam_chat()
    _apply(conn, tg.scan(single_export(tmp_path / "a", chat), cfg))
    chat["messages"][1]["reactions"] = [{"type": "emoji", "count": 1, "emoji": "\N{FIRE}",
                                         "recent": [{"from": "Alex Rivera",
                                                     "from_id": f"user{ME}",
                                                     **stamp(T0 + timedelta(hours=2))}]}]
    plan, result = _apply(conn, tg.scan(single_export(tmp_path / "b", chat), cfg))
    assert result["updated"] == 1 and plan["conflicts"] == []


def test_the_archive_remembers_what_a_later_export_lacks(cfg, conn, tmp_path):
    _apply(conn, tg.scan(full_export(tmp_path / "a"), cfg))
    later = tg.scan(full_export(tmp_path / "b", chats=[group_chat()]), cfg)
    assert "present_ids" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == [] and plan["new"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == SAM_MESSAGES + GROUP_MESSAGES


def test_handles_land_unlinked_with_their_display_names(cfg, conn, tmp_path):
    _apply(conn, tg.scan(full_export(tmp_path), cfg))
    rows = {r["value"]: r for r in conn.execute("SELECT * FROM entity_handles")}
    assert rows[f"telegram:user{SAM}"]["display_name"] == "Sam Okafor"
    assert all(r["entity_id"] is None for r in rows.values())
    assert rows[f"telegram:user{MAYA}"]["observation_count"] == GROUP_MESSAGES


def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    _apply(conn, tg.scan(full_export(tmp_path), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before


def test_doctor_stays_green_after_an_import_and_its_retraction(cfg, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"
    _, result = _apply(conn, tg.scan(full_export(tmp_path), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert run_checks(conn, cfg)["failures"] == 0


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output


def test_cli_dry_run_apply_and_reimport(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = full_export(tmp_path / "exp" / "DataExport_2026-07-30")
    r = runner.invoke(app, ["observe", "telegram", str(export)])
    assert r.exit_code == 0, r.output
    assert "telegram: 8 new" in r.output
    assert "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0

    r = runner.invoke(app, ["observe", "telegram", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 8 added" in r.output
    r = runner.invoke(app, ["observe", "telegram", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "0 added" in r.output and "8 identical skipped" in r.output
    r = runner.invoke(app, ["observe", "telegram", str(export)])
    assert "already imported" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_an_html_export_is_one_error_line(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    folder = tmp_path / "ChatExport_2026-07-30"
    folder.mkdir()
    (folder / "messages.html").write_text("<html></html>")
    r = runner.invoke(app, ["observe", "telegram", str(folder)])
    assert r.exit_code == 1
    assert "HTML format" in r.output and "Traceback" not in r.output


def test_cli_connectors_lists_the_stream(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "telegram" in r.output
