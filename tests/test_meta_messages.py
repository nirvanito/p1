"""Meta messages connector: Instagram DMs and Messenger, from the "Download
your information" JSON export.

The connector is a pure reader, so most of this runs scan() against exports
built here — shaped like the real ones, including the parts that are easy to
get wrong: every string escaped the way Meta escapes it (UTF-8 bytes as
Latin-1 code points), messages newest first and split across message_N
files, the older layout without the activity folder, Meta's notices that
look like messages, placeholder text beside an attachment, message
requests, and an export asked for in HTML.
"""

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import meta_messages as meta
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

IG = "your_instagram_activity/messages"
FB = "your_facebook_activity/messages"
MAYA = "mayachen_17841400000000187"
IG_PROFILE = "personal_information/personal_information/personal_information.json"
FB_PROFILE = "personal_information/profile_information/profile_information.json"


def ms(y, mo, d, h, mi, s=0, extra=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()) * 1000 + extra


# 2026-07-27 21:30 in Austin is 2026-07-28 02:30 UTC.
T0 = ms(2026, 7, 28, 2, 30)


def mojibake(value):
    """Meta's escaping: each string's UTF-8 bytes as Latin-1 code points."""
    if isinstance(value, str):
        return value.encode("utf-8").decode("latin-1")
    if isinstance(value, list):
        return [mojibake(v) for v in value]
    if isinstance(value, dict):
        return {k: mojibake(v) for k, v in value.items()}
    return value


def dump(obj) -> bytes:
    return json.dumps(mojibake(obj), indent=2, ensure_ascii=True).encode("ascii")


def msg(sender, when, content=None, **extra) -> dict:
    m = {"sender_name": sender, "timestamp_ms": when}
    if content is not None:
        m["content"] = content
    m.update(extra)
    m.setdefault("is_geoblocked_for_viewer", False)
    return m


def thread(people, messages, title=None) -> dict:
    """A message_N.json: participants, messages newest first, title."""
    return {"participants": [{"name": p} for p in people],
            "messages": sorted(messages, key=lambda m: -m["timestamp_ms"]),
            "title": title or people[0], "is_still_participant": True,
            "thread_path": "inbox/x", "magic_words": []}


def ig_profile(name="Alex Rivera", username="alexrivera.atx") -> dict:
    return {"profile_user": [{"media_map_data": {}, "string_map_data": {
        "Name": {"href": "", "value": name, "timestamp": 0},
        "Username": {"href": "", "value": username, "timestamp": 0}}}]}


def fb_profile(name="Alex Rivera") -> dict:
    return {"profile_v2": {"name": {"full_name": name, "first_name": "Alex",
                                    "last_name": "Rivera"}}}


MAYA_THREAD = thread(["Maya Chen", "Alex Rivera"], [
    msg("Maya Chen", T0, "Maya Chen sent an attachment.",
        share={"link": "https://www.instagram.com/reel/DEMOclay/",
               "share_text": "Wheel throwing for beginners",
               "original_content_owner": "austinclaystudio"}),
    msg("Maya Chen", T0 + 60_000, "I neeeed to take this class \N{SMILING FACE WITH HEART-SHAPED EYES}"),
    msg("Alex Rivera", T0 + 120_000, "Coffee at Café Medici?",
        reactions=[{"reaction": "\N{HEAVY BLACK HEART}", "actor": "Maya Chen",
                    "timestamp": T0 // 1000 + 180}]),
    msg("Maya Chen", T0 + 130_000, "Liked a message"),
    msg("Maya Chen", T0 + 140_000,
        photos=[{"uri": f"{IG}/inbox/{MAYA}/photos/1.jpg", "creation_timestamp": 1}]),
    msg("Maya Chen", T0 + 150_000, "that jalapeño crudo though"),
])
MAYA_MESSAGES = 5
GROUP_THREAD = thread(["Jake Morales", "Maya Chen", "Sam Okafor", "Alex Rivera"], [
    msg("Alex Rivera", T0 - 600_000, "Alex Rivera named the group Bled crew."),
    msg("Jake Morales", T0 - 590_000, "Lakeside place is booked"),
    msg("Sam Okafor", T0 - 580_000, "Sam Okafor unsent a message."),
    msg("Sam Okafor", T0 - 570_000, "Count me in"),
], title="Bled crew")


def ig_zip(folder: Path, threads=None, profile=None, name="instagram-alex-2026-07-30-x.zip",
           media=(f"{IG}/inbox/{MAYA}/photos/1.jpg",), extra=None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    threads = {f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD,
               f"{IG}/inbox/bledcrew_1784260000/message_1.json": GROUP_THREAD} \
        if threads is None else threads
    z = folder / name
    with zipfile.ZipFile(z, "w") as zf:
        for member, data in threads.items():
            zf.writestr(member, data if isinstance(data, bytes) else dump(data))
        if profile is not False:
            zf.writestr(IG_PROFILE, dump(profile or ig_profile()))
        for m in media:
            zf.writestr(m, b"\xff\xd8 bytes the importer never reads")
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return z


ROSA = "rosatrevino_10158234567890123"
ROSA_THREAD = thread(["Rosa Treviño", "Alex Rivera"], [
    msg("Rosa Treviño", T0, "can you bring the folding chairs mañana?"),
    msg("Alex Rivera", T0 + 60_000, "Claro, Tía"),
    msg("Rosa Treviño", T0 + 120_000, "Rosa Treviño called you.", call_duration=420),
    msg("Alex Rivera", T0 + 180_000, "You called it."),
])


def fb_zip(folder: Path, threads=None, name="facebook-alex-2026-07-30-y.zip") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    threads = {f"{FB}/inbox/{ROSA}/message_1.json": ROSA_THREAD} \
        if threads is None else threads
    z = folder / name
    with zipfile.ZipFile(z, "w") as zf:
        for member, data in threads.items():
            zf.writestr(member, dump(data))
        zf.writestr(FB_PROFILE, dump(fb_profile()))
    return z


def _by_body(scan):
    return {r["body"]: r for r in scan["records"] if r["body"]}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _report(conn, scan):
    return "\n".join(meta.report_lines(scan, obs.build_plan(conn, scan)))


# ---------------------------------------------------------------- shape

def test_an_instagram_export_reads_into_the_instagram_dm_stream(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    assert scan["connector"] == "meta_messages"
    assert len(scan["records"]) == MAYA_MESSAGES + 2
    assert {r["stream"] for r in scan["records"]} == {"instagram_dm"}
    assert {r["origin_app"] for r in scan["records"]} == {"instagram"}
    assert {r["title"] for r in scan["records"]} == {"Maya Chen", "Bled crew"}
    assert all(r["external_id"].startswith("meta:") for r in scan["records"])
    assert scan["facts"]["platforms"] == {"instagram": {"conversations": 2, "files": 2}}


def test_a_messenger_export_reads_into_the_messenger_stream(cfg, tmp_path):
    scan = meta.scan(fb_zip(tmp_path), cfg)
    assert {r["stream"] for r in scan["records"]} == {"messenger"}
    assert {r["origin_app"] for r in scan["records"]} == {"messenger"}
    assert [r["body"] for r in scan["records"]] == [
        "can you bring the folding chairs mañana?", "Claro, Tía", "You called it."]
    assert scan["records"][0]["title"] == "Rosa Treviño"


def test_the_activity_folders_of_newer_facebook_exports_both_read(cfg, tmp_path):
    for area in ("your_facebook_activity", "your_activity_across_facebook"):
        z = fb_zip(tmp_path / area, {f"{area}/messages/inbox/{ROSA}/message_1.json":
                                     ROSA_THREAD})
        assert {r["stream"] for r in meta.scan(z, cfg)["records"]} == {"messenger"}


# ---------------------------------------------------------------- the text quirk

def test_repair_undoes_metas_escaping_and_leaves_right_text_alone():
    assert meta.repair("cafÃ©") == "café"
    assert meta.repair("jalapeÃ±o") == "jalapeño"
    assert meta.repair("â\u009d¤") == "\N{HEAVY BLACK HEART}"
    # Already right: past U+00FF it cannot be Latin-1; a lone accent is not UTF-8.
    assert meta.repair("café") == "café"
    assert meta.repair("\N{FLEXED BICEPS}") == "\N{FLEXED BICEPS}"
    assert meta.repair("plain ascii") == "plain ascii"


def test_every_string_is_repaired_names_titles_and_reactions_too(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    bodies = _by_body(scan)
    assert "Coffee at Café Medici?" in bodies
    assert "that jalapeño crudo though" in bodies
    detail = json.loads(bodies["Coffee at Café Medici?"]["value_json"])
    assert detail["reactions"] == [{"by": "instagram:Maya Chen",
                                    "reaction": "\N{HEAVY BLACK HEART}"}]
    rosa = meta.scan(fb_zip(tmp_path / "fb"), cfg)
    assert {h["value"] for h in rosa["handles"]} == {"messenger:Alex Rivera",
                                                     "messenger:Rosa Treviño"}
    assert scan["facts"]["repaired"] > 0


def test_a_correctly_encoded_export_reads_the_same(cfg, tmp_path):
    """If Meta ever writes real UTF-8, nothing changes."""
    member = f"{IG}/inbox/{MAYA}/message_1.json"
    right = json.dumps(MAYA_THREAD, ensure_ascii=False).encode("utf-8")
    plain = meta.scan(ig_zip(tmp_path / "a", {member: right}), cfg)
    escaped = meta.scan(ig_zip(tmp_path / "b", {member: MAYA_THREAD}), cfg)
    assert plain["records"] == escaped["records"]


# ---------------------------------------------------------------- order and layout

def test_messages_read_oldest_first_across_message_files(cfg, tmp_path):
    """message_1.json holds the newest messages, each file newest first."""
    people = ["Maya Chen", "Alex Rivera"]
    newer = thread(people, [msg("Maya Chen", T0 + 2000, "three"),
                            msg("Alex Rivera", T0 + 3000, "four")])
    older = thread(people, [msg("Maya Chen", T0, "one"),
                            msg("Alex Rivera", T0 + 1000, "two")])
    z = ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": newer,
                          f"{IG}/inbox/{MAYA}/message_2.json": older}, media=())
    scan = meta.scan(z, cfg)
    assert [r["body"] for r in scan["records"]] == ["one", "two", "three", "four"]
    assert scan["facts"]["threads"][0]["messages"] == 4


def test_the_older_layout_reads_by_its_profile_or_its_name(cfg, tmp_path):
    old = {f"messages/inbox/{MAYA}/message_1.json": MAYA_THREAD}
    by_profile = meta.scan(ig_zip(tmp_path / "a", old, name="export.zip", media=()), cfg)
    assert {r["stream"] for r in by_profile["records"]} == {"instagram_dm"}
    by_name = meta.scan(ig_zip(tmp_path / "b", old, profile=False, media=(),
                               name="facebook-alex-2021-01-01-z.zip"), cfg)
    assert {r["stream"] for r in by_name["records"]} == {"messenger"}


def test_an_export_that_does_not_say_which_app_needs_the_setting(cfg, tmp_path):
    old = {f"messages/inbox/{MAYA}/message_1.json": MAYA_THREAD}
    z = ig_zip(tmp_path, old, profile=False, media=(), name="download.zip")
    with pytest.raises(DomainError) as e:
        meta.scan(z, cfg)
    assert "cannot tell whether this is an Instagram or a Facebook export" in str(e.value)
    assert "\n" not in str(e.value)
    cfg.connectors = {"meta_messages": {"platform": "instagram"}}
    assert {r["stream"] for r in meta.scan(z, cfg)["records"]} == {"instagram_dm"}


def test_a_zip_that_wraps_the_export_in_a_folder_still_finds_its_media(cfg, tmp_path):
    root = "instagram-alex-2026"
    z = ig_zip(tmp_path, {f"{root}/{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD},
               media=(f"{root}/{IG}/inbox/{MAYA}/photos/1.jpg",))
    photo = next(r for r in meta.scan(z, cfg)["records"] if r["media_path"])
    assert photo["media_path"] == f"{IG}/inbox/{MAYA}/photos/1.jpg"


# ---------------------------------------------------------------- time

def test_times_are_utc_instants_and_the_rollover_applies(cfg, tmp_path):
    late = thread(["Maya Chen", "Alex Rivera"], [
        msg("Maya Chen", ms(2026, 7, 28, 8, 30, 0, 999), "3:30 in the morning in Austin"),
        msg("Maya Chen", ms(2026, 7, 28, 10, 30), "5:30 in the morning in Austin")])
    scan = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": late},
                            media=()), cfg)
    early, after = scan["records"]
    assert early["started_at"] == "2026-07-28T08:30:00Z"       # milliseconds dropped
    assert early["journal_date"] == "2026-07-27"                # before the 05:00 rollover
    assert after["journal_date"] == "2026-07-28"
    assert early["last_modified_at"] == early["started_at"]


# ---------------------------------------------------------------- handles

def test_senders_are_handles_by_the_name_shown_never_bare_names(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    assert scan["handles"] == [
        {"kind": "app_account", "value": f"instagram:{n}", "display_name": n}
        for n in ("Alex Rivera", "Jake Morales", "Maya Chen", "Sam Okafor")]
    rec = _by_body(scan)["Count me in"]
    assert rec["participants"] == [
        {"kind": "app_account", "value": "instagram:Sam Okafor", "role": "sender"},
        {"kind": "app_account", "value": "instagram:Jake Morales", "role": "recipient"},
        {"kind": "app_account", "value": "instagram:Maya Chen", "role": "recipient"},
        {"kind": "app_account", "value": "instagram:Alex Rivera", "role": "recipient"}]
    values = {h["value"] for h in scan["handles"]}
    for r in scan["records"]:
        assert {p["value"] for p in r["participants"]} <= values


def test_a_deleted_account_is_one_handle_per_conversation(cfg, tmp_path):
    def gone(text):
        return thread(["Instagram User", "Alex Rivera"],
                      [msg("Instagram User", T0, text)])
    z = ig_zip(tmp_path, {f"{IG}/inbox/a_1/message_1.json": gone("hi"),
                          f"{IG}/inbox/b_2/message_1.json": gone("hello")}, media=())
    values = {h["value"] for h in meta.scan(z, cfg)["handles"]}
    assert {"instagram:Instagram User (a_1)", "instagram:Instagram User (b_2)"} <= values


# ---------------------------------------------------------------- who is you

def test_the_exports_own_profile_says_which_participant_is_you(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    assert scan["facts"]["you"]["instagram"] == {"name": "Alex Rivera",
                                                 "from": "the export's own profile"}
    bodies = _by_body(scan)
    assert bodies["Coffee at Café Medici?"]["direction"] == "out"
    assert bodies["Count me in"]["direction"] == "in"
    assert scan["account"] == "instagram:Alex Rivera"


def test_me_in_the_config_wins_and_a_miss_is_a_warning(cfg, tmp_path):
    cfg.connectors = {"meta_messages": {"me": "Maya Chen"}}
    scan = meta.scan(ig_zip(tmp_path), cfg)
    assert _by_body(scan)["that jalapeño crudo though"]["direction"] == "out"
    cfg.connectors = {"meta_messages": {"me": "Alex Rivers"}}
    scan = meta.scan(ig_zip(tmp_path / "b"), cfg)
    assert any("no Instagram participant is [connectors.meta_messages] me" in w
               for w in scan["facts"]["warnings"])


def test_without_a_profile_the_one_common_participant_is_you(cfg, tmp_path):
    jake = thread(["Jake Morales", "Alex Rivera"], [msg("Jake Morales", T0, "tacos?")])
    scan = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD,
                                       f"{IG}/inbox/jake_1/message_1.json": jake},
                            profile=False), cfg)
    assert scan["facts"]["you"]["instagram"]["name"] == "Alex Rivera"
    assert scan["facts"]["you"]["instagram"]["from"].startswith("inferred")


def test_with_nothing_to_go_on_direction_stays_unknown_and_the_report_says_so(
        cfg, conn, tmp_path):
    z = ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD},
               profile=False)
    scan = meta.scan(z, cfg)
    assert {r["direction"] for r in scan["records"]} == {None}
    assert "WARNING nothing in the Instagram export says which participant is you" \
        in _report(conn, scan)


# ---------------------------------------------------------------- what is not a message

def test_notices_are_counted_not_imported(cfg, tmp_path):
    people = ["Maya Chen", "Alex Rivera"]
    t = thread(people, [
        msg("Maya Chen", T0, "Liked a message"),
        msg("Maya Chen", T0 + 1, "Maya Chen reacted \N{FIRE} to your message "),
        msg("Alex Rivera", T0 + 2, "You unsent a message."),
        msg("Maya Chen", T0 + 3, "Maya Chen started a video chat"),
        msg("Maya Chen", T0 + 4, "The video chat ended."),
        msg("Alex Rivera", T0 + 5, "You called Maya Chen."),
        msg("Alex Rivera", T0 + 6, "You changed the group photo."),
        msg("Maya Chen", T0 + 7, "Maya Chen set the nickname for Alex Rivera to Lex."),
        msg("Maya Chen", T0 + 8, "You are now connected on Messenger"),
        msg("Maya Chen", T0 + 9, "hello", type="Subscribe"),
        msg("Maya Chen", T0 + 10, "a real message"),
    ])
    scan = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": t},
                            media=()), cfg)
    assert [r["body"] for r in scan["records"]] == ["a real message"]
    assert scan["skipped"] == [
        "3 × system line: call notice", "1 × system line: connection notice",
        "3 × system line: group change", "2 × system line: reaction notice",
        "1 × system line: unsent message"]


def test_words_that_look_like_notices_are_still_what_a_person_wrote(cfg, tmp_path):
    """'You called it.' names no participant; a sentence about someone else
    is not about its sender."""
    t = thread(["Maya Chen", "Alex Rivera"], [
        msg("Alex Rivera", T0, "You called it."),
        msg("Maya Chen", T0 + 1, "Jake left the group lol"),
        msg("Maya Chen", T0 + 2, "she liked a message I sent her"),
    ])
    scan = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": t},
                            media=()), cfg)
    assert {r["body"] for r in scan["records"]} == {
        "You called it.", "Jake left the group lol", "she liked a message I sent her"}


def test_metas_placeholder_comes_off_an_attachment(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    share = next(r for r in scan["records"] if "share" in (r["value_json"] or ""))
    assert share["body"] is None
    assert json.loads(share["value_json"])["share"] == {
        "link": "https://www.instagram.com/reel/DEMOclay/",
        "text": "Wheel throwing for beginners", "owner": "austinclaystudio"}
    t = thread(["Maya Chen", "Alex Rivera"],
               [msg("Maya Chen", T0, "Maya Chen sent an attachment.")])
    lone = meta.scan(ig_zip(tmp_path / "b", {f"{IG}/inbox/{MAYA}/message_1.json": t},
                            media=()), cfg)["records"]
    assert [(r["body"], json.loads(r["value_json"])) for r in lone] == [
        (None, {"attachment_missing": True})]


def test_message_requests_are_counted_unless_included(cfg, tmp_path):
    req = thread(["Lake Bled Boat Tours", "Alex Rivera"],
                 [msg("Lake Bled Boat Tours", T0, "20% off pletna rides"),
                  msg("Lake Bled Boat Tours", T0 + 1, "book now")])
    threads = {f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD,
               f"{IG}/message_requests/boat_1/message_1.json": req,
               f"{IG}/filtered_threads/spam_2/message_1.json": req}
    scan = meta.scan(ig_zip(tmp_path, threads), cfg)
    assert "20% off pletna rides" not in _by_body(scan)
    assert "4 × message in a message request (include_requests = true imports them)" \
        in scan["skipped"]
    assert scan["facts"]["requests"] == {"threads": 2, "messages": 4}
    cfg.connectors = {"meta_messages": {"include_requests": True}}
    scan = meta.scan(ig_zip(tmp_path / "b", threads), cfg)
    got = [r for r in scan["records"] if r["body"] == "20% off pletna rides"]
    assert {json.loads(r["value_json"])["folder"] for r in got} == {
        "message_requests", "filtered_threads"}


def test_empty_messages_are_counted_and_unknown_ones_not_understood(cfg, tmp_path):
    t = thread(["Maya Chen", "Alex Rivera"], [
        msg("Maya Chen", T0),
        msg("Maya Chen", T0 + 1, payment_info={"amount": 5}),
        msg("Maya Chen", T0 + 2, "ok"),
    ])
    t["messages"].append({"sender_name": "Maya Chen", "content": "no time"})
    scan = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": t},
                            media=()), cfg)
    assert [r["body"] for r in scan["records"]] == ["ok"]
    assert "1 × empty message" in scan["skipped"]
    assert "1 × message with no usable timestamp" in scan["skipped"]
    assert scan["unmapped"] == [{"what": "message with no text, only: payment_info",
                                 "count": 1}]


# ---------------------------------------------------------------- media

def test_media_is_a_path_inside_the_export_never_bytes(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path), cfg)
    photo = next(r for r in scan["records"] if r["media_path"])
    assert photo["media_path"] == f"{IG}/inbox/{MAYA}/photos/1.jpg"
    assert json.loads(photo["value_json"]) == {
        "media": [{"kind": "photo", "uri": f"{IG}/inbox/{MAYA}/photos/1.jpg"}]}
    assert scan["facts"]["media"] == {"present": 1, "missing": 0}
    without = meta.scan(ig_zip(tmp_path / "b", media=()), cfg)
    assert [r["media_path"] for r in without["records"]] == [None] * len(without["records"])
    assert without["facts"]["media"] == {"present": 0, "missing": 1}


# ---------------------------------------------------------------- identity

def test_scans_are_deterministic_and_a_folder_reads_like_its_zip(cfg, tmp_path):
    z = ig_zip(tmp_path)
    a, b = meta.scan(z, cfg), meta.scan(z, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    folder = tmp_path / "unzipped"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(folder)
    c = meta.scan(folder, cfg)
    assert c["records"] == a["records"]
    assert c["source_hash"] == a["source_hash"]


def test_identical_messages_in_one_millisecond_stay_two(cfg, tmp_path):
    t = thread(["Maya Chen", "Alex Rivera"], [msg("Maya Chen", T0, "ok"),
                                              msg("Maya Chen", T0, "ok")])
    recs = meta.scan(ig_zip(tmp_path, {f"{IG}/inbox/{MAYA}/message_1.json": t},
                            media=()), cfg)["records"]
    assert len({r["external_id"] for r in recs}) == 2


def test_an_archived_conversation_keeps_its_ids(cfg, tmp_path):
    inbox = meta.scan(ig_zip(tmp_path / "a"), cfg)
    archived = meta.scan(ig_zip(tmp_path / "b", {
        f"{IG}/archived_threads/{MAYA}/message_1.json": MAYA_THREAD}), cfg)
    maya = {r["external_id"] for r in inbox["records"] if r["title"] == "Maya Chen"}
    assert {r["external_id"] for r in archived["records"]} == maya


def test_two_copies_of_one_export_are_duplicates_not_new(cfg, conn, tmp_path):
    folder = tmp_path / "Downloads"
    ig_zip(folder)
    ig_zip(folder, name="instagram-alex-2026-07-30-x (1).zip")
    (folder / "holiday.jpg").write_bytes(b"x")
    scan = meta.scan(folder, cfg)
    plan = obs.build_plan(conn, scan)
    assert len(plan["new"]) == MAYA_MESSAGES + 2
    assert plan["dup_incoming"] == MAYA_MESSAGES + 2
    assert "duplicate message(s) across the exports" in "\n".join(
        meta.report_lines(scan, plan))


def test_a_folder_with_both_apps_reads_both(cfg, tmp_path):
    folder = tmp_path / "Downloads"
    ig_zip(folder)
    fb_zip(folder)
    scan = meta.scan(folder, cfg)
    assert {r["stream"] for r in scan["records"]} == {"instagram_dm", "messenger"}
    assert set(scan["facts"]["you"]) == {"instagram", "messenger"}


# ---------------------------------------------------------------- bad input

def test_an_html_export_is_refused_in_one_line(cfg, tmp_path):
    z = tmp_path / "instagram-alex-2026-07-30-h.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"{IG}/inbox/{MAYA}/message_1.html", b"<html></html>")
    with pytest.raises(DomainError) as e:
        meta.scan(z, cfg)
    assert "is a Meta export in HTML format" in str(e.value)
    assert "Format: JSON" in str(e.value) and "\n" not in str(e.value)


def test_a_zip_without_messages_lists_its_members(cfg, tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("your_instagram_activity/likes/liked_posts.json", b"{}")
    with pytest.raises(DomainError) as e:
        meta.scan(tmp_path, cfg)
    assert "holds no Meta export" in str(e.value) and "x.zip" in str(e.value)
    folder = tmp_path / "unzipped"
    (folder / "your_instagram_activity" / "likes").mkdir(parents=True)
    (folder / "your_instagram_activity" / "likes" / "liked_posts.json").write_text("{}")
    with pytest.raises(DomainError) as e:
        meta.scan(folder, cfg)
    assert "liked_posts.json" in str(e.value)


def test_one_file_out_of_the_export_is_refused(cfg, tmp_path):
    p = tmp_path / "message_1.json"
    p.write_bytes(dump(MAYA_THREAD))
    with pytest.raises(DomainError) as e:
        meta.scan(p, cfg)
    assert "not one file out of it" in str(e.value)


def test_bad_json_fails_on_one_line_naming_the_file(cfg, tmp_path):
    member = f"{IG}/inbox/{MAYA}/message_1.json"
    with pytest.raises(DomainError) as e:
        meta.scan(ig_zip(tmp_path, {member: b"{\"participants\": ["}), cfg)
    assert member in str(e.value) and "is not valid JSON" in str(e.value)
    assert "\n" not in str(e.value)


def test_a_utf8_bom_is_accepted(cfg, tmp_path):
    member = f"{IG}/inbox/{MAYA}/message_1.json"
    scan = meta.scan(ig_zip(tmp_path, {member: b"\xef\xbb\xbf" + dump(MAYA_THREAD)}), cfg)
    assert len(scan["records"]) == MAYA_MESSAGES


def test_a_file_without_a_messages_list_is_not_understood(cfg, tmp_path):
    scan = meta.scan(ig_zip(tmp_path, {
        f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD,
        f"{IG}/inbox/odd_1/message_1.json": {"participants": []}}), cfg)
    assert {"what": "message file without a messages list", "count": 1} in scan["unmapped"]


@pytest.mark.parametrize("settings,words", [
    ({"me": 42}, "me must be your name"),
    ({"include_requests": "yes"}, "include_requests must be true or false"),
    ({"platform": "myspace"}, "platform must be"),
])
def test_bad_settings_fail_on_one_line(cfg, tmp_path, settings, words):
    cfg.connectors = {"meta_messages": settings}
    with pytest.raises(DomainError) as e:
        meta.scan(ig_zip(tmp_path), cfg)
    assert words in str(e.value)


def test_default_target_is_the_configured_folder_or_downloads(cfg, tmp_path, monkeypatch):
    folder = tmp_path / "meta-exports"
    ig_zip(folder)
    cfg.connectors = {"meta_messages": {"exports_dir": "meta-exports"}}
    assert meta.default_target(cfg) == cfg.root / "meta-exports"
    cfg.connectors = {}
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(DomainError) as e:
        meta.default_target(cfg)
    assert "no Meta export found" in str(e.value)
    ig_zip(home / "Downloads")
    assert meta.default_target(cfg) == home / "Downloads"


# ---------------------------------------------------------------- report

def test_the_report_reads_like_the_other_connectors(cfg, conn, tmp_path):
    folder = tmp_path / "exp"
    ig_zip(folder, threads={f"{IG}/inbox/{MAYA}/message_1.json": MAYA_THREAD,
                            f"{IG}/inbox/bledcrew_1784260000/message_1.json": GROUP_THREAD,
                            f"{IG}/message_requests/boat_1/message_1.json": thread(
                                ["Boat Tours", "Alex Rivera"],
                                [msg("Boat Tours", T0, "deal")])})
    text = _report(conn, meta.scan(folder / "instagram-alex-2026-07-30-x.zip", cfg))
    assert text.startswith("meta_messages: instagram-alex-2026-07-30-x.zip (Instagram: "
                           "3 conversation(s), 3 message file(s))")
    assert "you on Instagram: Alex Rivera (the export's own profile)" in text
    assert "conversation: Maya Chen — one-to-one, 5 message(s) 2026-07-27..2026-07-27" in text
    assert "conversation: Bled crew — group, 2 message(s)" in text
    assert "message requests: 1 conversation(s), 1 message(s) left out" in text
    assert "string(s) repaired from Meta's Latin-1 escaping" in text
    assert "instagram_dm: 7 new, 0 changed | 2026-07-27..2026-07-27 | e.g. " \
           "2026-07-27 them: that jalapeño crudo though" in text
    assert "handles: 3 by the name the export shows" in text
    assert "media: 1 message(s) with the file in the export" in text
    assert "skipped: 1 × system line: unsent message" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reapply_is_all_identical(cfg, conn, tmp_path):
    z = ig_zip(tmp_path)
    scan = meta.scan(z, cfg)
    _, first = _apply(conn, scan)
    assert first["added"] == len(scan["records"]) == MAYA_MESSAGES + 2
    plan, again = _apply(conn, meta.scan(z, cfg))
    assert again["added"] == 0 and again["identical"] == MAYA_MESSAGES + 2
    assert plan["conflicts"] == [] and plan["updates"] == []
    assert "nothing new" in "\n".join(meta.report_lines(scan, plan))


def test_a_reaction_added_later_updates_instead_of_conflicting(cfg, conn, tmp_path):
    people = ["Maya Chen", "Alex Rivera"]
    member = f"{IG}/inbox/{MAYA}/message_1.json"
    before = thread(people, [msg("Alex Rivera", T0, "Uchi at 7")])
    after = thread(people, [msg("Alex Rivera", T0, "Uchi at 7", reactions=[
        {"reaction": "\N{HEAVY BLACK HEART}", "actor": "Maya Chen",
         "timestamp": T0 // 1000 + 3600}])])
    _apply(conn, meta.scan(ig_zip(tmp_path / "a", {member: before}, media=()), cfg))
    plan, result = _apply(conn, meta.scan(ig_zip(tmp_path / "b", {member: after},
                                                 media=()), cfg))
    assert result["updated"] == 1 and plan["conflicts"] == []
    row = conn.execute("SELECT value_json, last_modified_at FROM observations").fetchone()
    assert "Maya Chen" in row["value_json"]
    assert row["last_modified_at"] == "2026-07-28T03:30:00Z"


def test_the_archive_remembers_what_a_later_export_lacks(cfg, conn, tmp_path):
    """A date-limited export is not a list of deletions: nothing is removed."""
    _apply(conn, meta.scan(ig_zip(tmp_path / "a"), cfg))
    later = meta.scan(ig_zip(tmp_path / "b", {
        f"{IG}/inbox/bledcrew_1784260000/message_1.json": GROUP_THREAD}, media=()), cfg)
    assert "present_ids" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == [] and plan["new"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == MAYA_MESSAGES + 2


def test_handles_land_unlinked_with_their_display_names(cfg, conn, tmp_path):
    _apply(conn, meta.scan(fb_zip(tmp_path), cfg))
    rows = {r["value"]: r for r in conn.execute("SELECT * FROM entity_handles")}
    assert rows["messenger:Rosa Treviño"]["display_name"] == "Rosa Treviño"
    assert all(r["entity_id"] is None for r in rows.values())
    assert rows["messenger:Rosa Treviño"]["observation_count"] == 3


def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    _apply(conn, meta.scan(ig_zip(tmp_path), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before


def test_doctor_stays_green_after_an_import_and_its_retraction(cfg, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0, "seed itself is not green"
    folder = tmp_path / "exports"
    ig_zip(folder)
    fb_zip(folder)
    _, result = _apply(conn, meta.scan(folder, cfg))
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
    export = ig_zip(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "meta_messages", str(export)])
    assert r.exit_code == 0, r.output
    assert "instagram_dm: 7 new" in r.output
    assert "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0

    r = runner.invoke(app, ["observe", "meta_messages", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 7 added" in r.output
    r = runner.invoke(app, ["observe", "meta_messages", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "0 added" in r.output and "7 identical skipped" in r.output
    r = runner.invoke(app, ["observe", "meta_messages", str(export)])
    assert "already imported" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_an_html_export_is_one_error_line(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    z = tmp_path / "facebook-alex-2026-07-30-h.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"{FB}/inbox/{ROSA}/message_1.html", b"<html></html>")
    r = runner.invoke(app, ["observe", "meta_messages", str(z)])
    assert r.exit_code == 1
    assert "HTML format" in r.output and "Traceback" not in r.output


def test_cli_connectors_lists_both_streams(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "meta_messages" in r.output
    assert "instagram_dm" in r.output and "messenger" in r.output
