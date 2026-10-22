"""Android texts and calls connector against synthetic SMS Backup & Restore files.

The fixtures are written the way the app writes: <smses>/<calls> roots with
count/backup_set/backup_date/type, epoch-millisecond dates, the literal
"null" for absent values, '(Unknown)' for strangers, MMS with smil/text/media
parts and PDU-typed <addr>s, and emoji as UTF-16 surrogate-pair character
references (which expat rejects until the connector repairs them).

The mirroring tests import one backup, then a later one, and check the
archive follows the phone (the owner's decision, 2026-09-24) only when the
file is the phone's whole state — and that removal cannot reach what it
must not: another phone's archive, the calls when only texts were read, or
most of the archive at once.
"""

import base64
import json
import zipfile
from datetime import datetime, timezone

import pytest

from lifecrm.connectors import android_sms as an
from lifecrm.connectors import base
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks

ME = "+15125550100"
JAKE = "+15125550142"
SAM = "+15125550166"
DANA = "+15125559876"
MOM = "+12105550110"


class Raw(str):
    """An attribute value written as-is — for character references."""


def _esc(v) -> str:
    if isinstance(v, Raw):
        return str(v)
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\n", "&#10;"))


def _attrs(**kw) -> str:
    return " ".join(f'{k}="{_esc(v)}"' for k, v in kw.items())


def ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(round(dt.timestamp() * 1000))


def sms(address, when, type_=1, body="hi", contact="(Unknown)", **extra) -> str:
    a = dict(protocol=0, address=address, date=ms(when), type=type_, subject="null",
             body=body, toa="null", sc_toa="null",
             service_center="+12063130004" if type_ == 1 else "null", read=1,
             status=-1, locked=0, date_sent=0, sub_id=1,
             readable_date="Sep 1, 2026 1:00:00 PM", contact_name=contact)
    a.update(extra)
    return f"  <sms {_attrs(**a)} />"


def mms(when, box, address, contact, addrs, texts=(), media=(), m_id="null",
        m_type=None, seconds=False, **extra) -> str:
    date = ms(when) // 1000 if seconds else ms(when) // 1000 * 1000
    a = dict(date=date, ct_t="application/vnd.wap.multipart.related", sub="null",
             msg_box=box, address=address, m_id=m_id,
             m_type=m_type or (128 if box == 2 else 132),
             creator="com.samsung.android.messaging", date_sent=0, read=1,
             text_only=0 if media else 1, readable_date="Sep 1, 2026 1:00:00 PM",
             contact_name=contact)
    a.update(extra)
    parts = ['      <part seq="-1" ct="application/smil" name="null" chset="null" '
             'cl="smil.xml" text="&lt;smil&gt;&lt;/smil&gt;" />']
    for name, mime in media:
        data = base64.b64encode(b"\xff\xd8MEDIA-BYTES-NEVER-STORED\xff\xd9").decode()
        parts.append(f"      <part {_attrs(seq=0, ct=mime, name=name, chset='null', cl=name, text='null', data=data)} />")
    for t in texts:
        parts.append(f"      <part {_attrs(seq=0, ct='text/plain', name='null', chset=106, cl='text_0.txt', text=t)} />")
    lines = [f"  <mms {_attrs(**a)}>", "    <parts>", *parts, "    </parts>", "    <addrs>"]
    lines += [f"      <addr {_attrs(address=addr, type=t, charset=106)} />" for addr, t in addrs]
    return "\n".join(lines + ["    </addrs>", "  </mms>"])


def call(number, when, type_, duration, contact="(Unknown)", presentation=1) -> str:
    return (f"  <call {_attrs(number=number, duration=duration, date=ms(when), type=type_, presentation=presentation, subscription_id=1, post_dial_digits='', readable_date='Sep 1, 2026 1:00:00 PM', contact_name=contact)} />")


def document(root, rows, type_="full", count=None, version="10.20.002",
             stylesheet=True) -> str:
    n = len(rows) if count is None else count
    type_attr = f' type="{type_}"' if type_ is not None else ""
    head = ["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>",
            f"<!--File Created By SMS Backup & Restore v{version} on 20/09/2026 02:00:03-->"]
    if stylesheet:
        head.append(f'<?xml-stylesheet type="text/xsl" href="{root}.xsl"?>')
    head.append(f'<{"smses" if root == "sms" else "calls"} count="{n}" '
                f'backup_set="4b9e6c1e-0000-4000-8000-000000000001" '
                f'backup_date="{ms("2026-09-20T07:00:03Z")}"{type_attr}>')
    return "\n".join(head + rows + [f"</{'smses' if root == 'sms' else 'calls'}>", ""])


GROUP = f"{JAKE}~{SAM}~{DANA}"
GROUP_NAMES = "Jake Reilly, Sam Ortiz, Dana Whitfield"


def texts(variant="after") -> list[str]:
    """The phone's messages at one moment.

    'after':   everything below.
    'deleted': as 'after', with Jake's first text deleted on the phone.
    'few':     only the two newest messages left (an incomplete-looking file).
    """
    rows = {
        "S1": sms(JAKE, "2026-09-01T18:00:00.250Z", 1, "see you at 6", "Jake Reilly"),
        "S2": sms("(512) 555-0142", "2026-09-01T18:05:00.999Z", 2,
                  "perfect, bringing snacks", "Jake Reilly"),
        "S3": sms("5125559876", "2019-03-03T12:00:00Z", 1, "running late", "Dana Whitfield"),
        "S4": sms("AMAZON", "2026-09-02T15:00:00Z", 1, "Your package was delivered"),
        "S5": sms(JAKE, "2026-09-02T16:00:00Z", 3, "unsent thought", "Jake Reilly"),
        "S6": sms(JAKE, "2026-09-03T06:30:00.987Z", 1,
                  Raw("see you there &#55357;&#56832;"), "Jake Reilly"),
        "M1": mms("2026-09-02T09:00:00Z", 1, GROUP, GROUP_NAMES,
                  [(JAKE, 137), (ME, 151), (SAM, 151), (DANA, 151)],
                  texts=["who's in for saturday"], m_id="mavodi-1-aa-0001"),
        "M2": mms("2026-09-02T09:05:00Z", 2, GROUP, GROUP_NAMES,
                  [(ME, 137), (JAKE, 151), (SAM, 151), (DANA, 151)],
                  texts=["me!"], media=[("IMG_0001.jpg", "image/jpeg")],
                  m_id="mavodi-1-aa-0002"),
        "M3": mms("2026-09-02T10:00:00Z", 1, SAM, "Sam Ortiz", [(SAM, 137), (ME, 151)],
                  media=[("climb.jpg", "image/jpeg")]),
        "M4": mms("2026-09-02T11:00:00Z", 1, SAM, "Sam Ortiz", [(SAM, 137), (ME, 151)],
                  m_type=130),
    }
    if variant == "deleted":
        rows.pop("S1")
    if variant == "few":
        rows = {k: rows[k] for k in ("S6", "M2")}
    return list(rows.values())


def calls(variant="after") -> list[str]:
    rows = {
        "C1": call(JAKE, "2026-09-01T20:00:00.500Z", 1, 300, "Jake Reilly"),
        "C2": call("(210) 555-0110", "2026-09-02T01:00:00Z", 2, 720, "Mom"),
        "C3": call(DANA, "2026-09-02T14:00:00Z", 3, 0, "Dana Whitfield"),
        "C4": call("", "2026-09-02T15:00:00Z", 1, 60, presentation=2),
        "C5": call("+18005550000", "2026-09-02T16:00:00Z", 6, 0),
    }
    if variant == "deleted":
        rows.pop("C1")
    return list(rows.values())


def make_backup(folder, variant="after", stamp="20260920020003", sms_type="full",
                calls_type="full", with_calls=True, bom=False, sms_count=None):
    folder.mkdir(parents=True, exist_ok=True)
    body = document("sms", texts(variant), type_=sms_type, count=sms_count)
    (folder / f"sms-{stamp}.xml").write_bytes(
        (b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))
    if with_calls:
        (folder / f"calls-{stamp}.xml").write_text(
            document("calls", calls(variant), type_=calls_type), encoding="utf-8")
    return folder


def _streams(scan):
    out = {}
    for r in scan["records"]:
        out.setdefault(r["stream"], []).append(r)
    return out


def _body(scan, text):
    return next(r for r in scan["records"] if r["body"] == text)


def _detail(rec):
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- reading

def test_reads_a_backup_folder_into_the_three_streams(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    by = _streams(scan)
    assert sorted(r["body"] for r in by["android_sms"]) == [
        "Your package was delivered", "perfect, bringing snacks", "running late",
        "see you at 6", "see you there 😀"]
    assert sorted(r["body"] or "" for r in by["android_mms"]) == [
        "", "me!", "who's in for saturday"]
    assert sorted(_detail(r)["call"] for r in by["android_call"]) == [
        "in", "in", "missed", "out"]
    assert {r["origin_app"] for r in by["android_sms"] + by["android_mms"]} == {"messages"}
    assert {r["origin_app"] for r in by["android_call"]} == {"phone"}
    assert all(r["external_id"].startswith("android:") for r in scan["records"])
    assert scan["connector"] == "android_sms"
    assert scan["account"] == ME
    assert scan["source_file"] == "sms-20260920020003.xml + calls-20260920020003.xml"


def test_message_fields_map_onto_the_observation(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    incoming = _body(scan, "see you at 6")
    assert incoming["direction"] == "in" and incoming["title"] is None
    assert _detail(incoming) == {"thread": f"phone:{JAKE}"}
    outgoing = _body(scan, "perfect, bringing snacks")
    assert outgoing["direction"] == "out"
    assert _detail(outgoing)["thread"] == f"phone:{JAKE}"     # "(512) 555-0142" too
    assert incoming["last_modified_at"] == incoming["started_at"]


def test_calls_carry_duration_outcome_and_end_time(cfg, tmp_path):
    by = _streams(an.scan(make_backup(tmp_path / "b"), cfg))
    jake = next(r for r in by["android_call"] if r["started_at"] == "2026-09-01T20:00:00Z")
    assert (jake["value"], jake["unit"], jake["direction"]) == (300, "s", "in")
    assert jake["ended_at"] == "2026-09-01T20:05:00Z"
    missed = next(r for r in by["android_call"] if _detail(r)["call"] == "missed")
    assert missed["value"] == 0 and missed["ended_at"] is None
    assert missed["direction"] == "in"


def test_millisecond_dates_truncate_and_follow_the_rollover_hour(cfg, tmp_path):
    rec = _body(an.scan(make_backup(tmp_path / "b"), cfg), "see you there 😀")
    assert rec["started_at"] == "2026-09-03T06:30:00Z"      # .987 dropped
    assert rec["journal_date"] == "2026-09-02"               # 01:30 in Austin, before 05:00


@pytest.mark.parametrize("raw,expected", [
    ("1789000000000", 1789000000000),     # ms, as `date` is written
    ("1789000000", 1789000000000),        # seconds, as an MMS date_sent is
    ("0", None), ("null", None), ("", None), (None, None), ("-5", None),
    ("99999999999999999999", None),       # a corrupt row, not the year 3 billion
])
def test_ms_epoch(raw, expected):
    assert an.ms_epoch(raw) == expected


def test_an_mms_dated_in_seconds_reads_the_same_instant(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        mms("2026-09-02T09:00:00Z", 1, SAM, "Sam Ortiz", [(SAM, 137), (ME, 151)],
            texts=["in seconds"], seconds=True)]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    assert scan["records"][0]["started_at"] == "2026-09-02T09:00:00Z"
    assert scan["facts"]["mms_date_units"] == {"ms": 0, "s": 1}


# ---------------------------------------------------------------- the XML the app writes

def test_surrogate_pair_emoji_are_repaired_and_counted(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00Z", 1, Raw("pair &#55357;&#56832; hex &#xD83C;&#xDF2E;")),
        sms(JAKE, "2026-09-01T18:01:00Z", 1, Raw("lone &#55357; half")),
        sms(JAKE, "2026-09-01T18:02:00Z", 1, Raw("bell &#7;here")),
        sms(JAKE, "2026-09-01T18:03:00Z", 1, Raw("fine &#233; &#10;next")),
    ]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    bodies = sorted(r["body"] for r in scan["records"])
    assert bodies == ["bell here", "fine é \nnext", "lone � half", "pair 😀 hex 🌮"]
    assert scan["facts"]["repaired"] == {"surrogate_pairs": 2, "lone_surrogates": 1,
                                         "invalid_refs_dropped": 1}


class _Trickle:
    """A file that hands back three bytes per read: every reference and every
    surrogate pair ends up straddling a chunk boundary somewhere."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, size=-1):
        out = self.data[self.pos:self.pos + 3]
        self.pos += len(out)
        return out


def test_the_repair_never_splits_a_reference_between_chunks(cfg, tmp_path):
    doc = document("sms", [sms(JAKE, f"2026-09-01T18:0{i}:00Z", 1,
                               Raw("x" * i + "&#55357;&#56832;&#55356;&#57134;&#233;y"))
                           for i in range(8)]).encode("utf-8")
    whole = an._Reader(cfg)
    info_whole = an._parse(_Trickle(doc), "a.xml", whole)
    assert {r["body"] for r in whole.records} == {"x" * i + "😀🌮éy" for i in range(8)}
    assert info_whole["repaired"]["surrogate_pairs"] == 16
    import hashlib
    assert info_whole["sha256"] == hashlib.sha256(doc).hexdigest()


def test_a_utf8_bom_is_tolerated(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b", bom=True, with_calls=False), cfg)
    assert len(_streams(scan)["android_sms"]) == 5


def test_attachments_are_described_and_media_never_enters_the_database(cfg, conn, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    sent = _body(scan, "me!")
    assert _detail(sent)["attachments"] == [{"mime": "image/jpeg", "name": "IMG_0001.jpg"}]
    photo = next(r for r in _streams(scan)["android_mms"] if r["body"] is None)
    assert _detail(photo)["attachments"] == [{"mime": "image/jpeg", "name": "climb.jpg"}]
    assert sent["media_path"] is None
    _apply(conn, scan)
    blob = base64.b64encode(b"\xff\xd8MEDIA-BYTES-NEVER-STORED\xff\xd9").decode()
    dump = "\n".join(conn.iterdump())
    assert blob not in dump and "MEDIA-BYTES" not in dump


def test_a_group_mms_names_every_member_in_their_role(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    received = _body(scan, "who's in for saturday")
    roles = {(p["value"], p["role"]) for p in received["participants"]}
    assert roles == {(JAKE, "sender"), (ME, "recipient"), (SAM, "recipient"),
                     (DANA, "recipient")}
    assert _detail(received)["group"] is True
    assert _detail(received)["thread"] == f"phone:{JAKE},phone:{SAM},phone:{DANA}"
    sent = _body(scan, "me!")
    roles = {(p["value"], p["role"]) for p in sent["participants"]}
    assert roles == {(ME, "sender"), (JAKE, "recipient"), (SAM, "recipient"),
                     (DANA, "recipient")}
    assert _detail(sent)["thread"] == _detail(received)["thread"]


def test_handles_are_normalized_and_named_from_the_backups_contact_names(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", JAKE)] == "Jake Reilly"
    assert names[("phone", DANA)] == "Dana Whitfield"       # stored as 5125559876
    assert names[("phone", MOM)] == "Mom"                   # dialled as (210) 555-0110
    assert names[("app_account", "messages:AMAZON")] is None   # '(Unknown)' is no name
    assert names[("phone", ME)] == an.OWNER_LABEL
    # every participant handle is declared, so the foreign keys hold
    for rec in scan["records"]:
        for p in rec["participants"]:
            assert (p["kind"], p["value"]) in names
            assert set(p) == {"kind", "value", "role"}       # handles, never names
    normalized = {n["raw"]: n["handle"] for n in scan["facts"]["normalized"]}
    assert normalized["(512) 555-0142"] == f"phone:{JAKE}"
    assert normalized["5125559876"] == f"phone:{DANA}"


def test_a_comma_in_a_group_members_name_leaves_the_names_off_rather_than_guessing(
        cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        mms("2026-09-02T09:00:00Z", 1, f"{JAKE}~{SAM}", "Reilly, Jake, Sam Ortiz",
            [(JAKE, 137), (ME, 151), (SAM, 151)], texts=["hey"]),
        mms("2026-09-02T09:10:00Z", 1, MOM, "Rivera, Linda", [(MOM, 137), (ME, 151)],
            texts=["one to one"]),
    ]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    names = {h["value"]: h["display_name"] for h in scan["handles"]}
    assert names[JAKE] is None and names[SAM] is None
    assert names[MOM] == "Rivera, Linda"          # one member: the whole name is theirs
    assert scan["facts"]["group_names_unaligned"] == 1


def test_undelivered_and_unknown_rows_are_counted_not_hidden(cfg, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    skipped = " | ".join(scan["skipped"])
    assert "1 × draft (never delivered)" in skipped
    assert "1 × MMS never downloaded" in skipped
    assert "1 × blocked call (not kept)" in skipped
    assert "unsent thought" not in {r["body"] for r in scan["records"]}


def test_an_unknown_message_type_is_not_understood(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00Z", 9, "odd"),
        "  <rcs_message foo=\"bar\" />"]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    assert scan["records"] == []
    assert {u["what"] for u in scan["unmapped"]} == {"sms type '9'",
                                                     "<rcs_message> element in texts backup"}


def test_a_withheld_number_is_a_call_with_nobody_attached(cfg, tmp_path):
    by = _streams(an.scan(make_backup(tmp_path / "b"), cfg))
    private = next(r for r in by["android_call"] if _detail(r).get("number"))
    assert _detail(private) == {"call": "in", "number": "private"}
    assert [p["value"] for p in private["participants"]] == [ME]     # only you


def test_two_scans_of_the_same_backup_are_identical(cfg, tmp_path):
    folder = make_backup(tmp_path / "b")
    a, b = an.scan(folder, cfg), an.scan(folder, cfg)
    assert a["records"] == b["records"]
    assert a["source_hash"] == b["source_hash"] and a["handles"] == b["handles"]
    assert sorted(a["present_ids"]) == sorted(b["present_ids"])


def test_ids_do_not_depend_on_how_a_number_was_spelled(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    one = document("sms", [sms(JAKE, "2026-09-01T18:00:00Z", 1, "same")])
    (folder / "sms-1.xml").write_text(one, encoding="utf-8")
    first = an.scan(folder / "sms-1.xml", cfg)["records"][0]["external_id"]
    (folder / "sms-1.xml").write_text(one.replace(JAKE, "(512) 555-0142"), encoding="utf-8")
    assert an.scan(folder / "sms-1.xml", cfg)["records"][0]["external_id"] == first


# ---------------------------------------------------------------- targets

def test_a_bare_file_is_recognised_by_its_root_not_its_name(cfg, tmp_path):
    p = tmp_path / "my phone's call log.xml"
    p.write_text(document("calls", calls()), encoding="utf-8")
    scan = an.scan(p, cfg)
    assert set(_streams(scan)) == {"android_call"}
    assert scan["mirror_streams"] == ["android_call"]
    assert scan["target_kind"] == "file"


def test_a_folder_reads_the_newest_file_of_each_kind(cfg, tmp_path):
    folder = make_backup(tmp_path / "b", variant="deleted", stamp="20260901020000")
    make_backup(folder, stamp="20260920020003")
    scan = an.scan(folder, cfg)
    assert scan["facts"]["files"] == {"texts": "sms-20260920020003.xml",
                                      "calls": "calls-20260920020003.xml"}
    assert "see you at 6" in {r["body"] for r in scan["records"]}


def test_a_zip_of_the_backup_reads_like_the_folder(cfg, tmp_path):
    folder = make_backup(tmp_path / "b")
    z = tmp_path / "phone.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in folder.iterdir():
            zf.write(p, f"SMSBackupRestore/{p.name}")
    from_zip, from_folder = an.scan(z, cfg), an.scan(folder, cfg)
    assert from_zip["records"] == from_folder["records"]
    assert from_zip["target_kind"] == "zip"
    assert from_zip["source_hash"] == from_folder["source_hash"]


def test_a_folder_without_backups_says_what_it_holds(cfg, tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "notes.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(DomainError, match=r"x holds no sms-\*\.xml or calls-\*\.xml \(has: notes\.txt\)"):
        an.scan(tmp_path / "x", cfg)


def test_a_zip_without_backups_lists_its_members(cfg, tmp_path):
    z = tmp_path / "other.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", "[]")
    with pytest.raises(DomainError, match="members: conversations.json"):
        an.scan(z, cfg)


def test_a_file_with_another_root_is_refused_by_name(cfg, tmp_path):
    p = tmp_path / "sms-1.xml"
    p.write_text("<?xml version='1.0'?><contacts count='0'></contacts>", encoding="utf-8")
    with pytest.raises(DomainError, match="root element is <contacts>, expected <smses> or <calls>"):
        an.scan(p, cfg)


def test_a_file_that_is_not_xml_is_refused_with_its_position(cfg, tmp_path):
    p = tmp_path / "sms-1.xml"
    p.write_bytes(b"SQLite format 3\x00 not xml")
    with pytest.raises(DomainError, match="not well-formed XML at line 1"):
        an.scan(p, cfg)


def test_a_file_cut_off_mid_upload_imports_nothing(cfg, tmp_path):
    folder = make_backup(tmp_path / "b", with_calls=False)
    p = next(folder.iterdir())
    data = p.read_bytes()
    p.write_bytes(data[:len(data) * 2 // 3])
    with pytest.raises(DomainError, match="ends early .* still uploading"):
        an.scan(folder, cfg)


def test_the_report_reads_like_the_other_connectors(cfg, conn, tmp_path):
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    text = "\n".join(an.report_lines(scan, obs.build_plan(conn, scan)))
    assert "android-sms: sms-20260920020003.xml + calls-20260920020003.xml (folder; " \
           "SMS Backup & Restore v10.20.002)" in text
    assert "android_sms: 5 new, 0 changed | 2019-03-03..2026-09-02" in text
    assert "android_mms: 3 new" in text and "android_call: 4 new" in text
    assert f"account: {ME} (from the sender of this phone's sent MMS)" in text
    assert "texts: full backup taken 2026-09-20, 10 message(s) — deletions on the " \
           "phone are mirrored" in text
    assert "messages span 2019-03-03..2026-09-02" in text
    assert "repaired: 1 emoji written as UTF-16 surrogate pairs" in text
    assert "5125559876 -> +15125559876 (Dana Whitfield)" in text
    assert "skipped: 1 × draft (never delivered)" in text
    assert "handles: 5 (4 named by the phone's contacts)" in text


# ---------------------------------------------------------------- whose phone

def test_the_configured_number_is_the_account(cfg, tmp_path):
    cfg.connectors = {"android_sms": {"account": "(512) 555-0100"}}
    scan = an.scan(make_backup(tmp_path / "b"), cfg)
    assert scan["account"] == ME and scan["facts"]["account_source"] == "config.toml"


def test_a_configured_account_that_is_not_a_number_is_refused(cfg, tmp_path):
    cfg.connectors = {"android_sms": {"account": "alex@example.com"}}
    with pytest.raises(DomainError, match=r"\[connectors\.android_sms\] account = 'alex@example\.com' is not a phone number"):
        an.scan(make_backup(tmp_path / "b"), cfg)


def test_with_no_sent_mms_the_number_every_received_mms_went_to_is_used(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        mms("2026-09-02T09:00:00Z", 1, GROUP, GROUP_NAMES,
            [(JAKE, 137), (ME, 151), (SAM, 151), (DANA, 151)], texts=["a"]),
        mms("2026-09-02T10:00:00Z", 1, SAM, "Sam Ortiz", [(SAM, 137), (ME, 151)],
            texts=["b"]),
    ]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    assert scan["account"] == ME
    assert scan["facts"]["account_source"] == "the one number every received MMS went to"


def test_without_any_way_to_tell_whose_phone_nothing_is_removed(cfg, conn, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00Z", 1, "see you at 6")]), encoding="utf-8")
    scan = an.scan(folder, cfg)
    assert scan["account"] is None
    plan = obs.build_plan(conn, scan)
    assert "does not say which account" in plan["mirror_note"]
    text = "\n".join(an.report_lines(scan, plan))
    assert "account: unknown" in text and "[connectors.android_sms]" in text
    assert "deletions on the phone are mirrored" not in text
    rec = scan["records"][0]
    assert [p["value"] for p in rec["participants"]] == [JAKE]


# ---------------------------------------------------------------- the archive mirrors the phone

def test_reimporting_the_same_backup_is_a_no_op(cfg, conn, tmp_path):
    folder = make_backup(tmp_path / "b")
    _apply(conn, an.scan(folder, cfg))
    plan = obs.build_plan(conn, an.scan(folder, cfg))
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert not plan["removed"]
    assert plan["identical"] == 12


def test_a_text_and_a_call_deleted_on_the_phone_are_removed_and_return_if_restored(
        cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    later = an.scan(make_backup(tmp_path / "b", variant="deleted"), cfg)
    text = "\n".join(an.report_lines(later, obs.build_plan(conn, later)))
    assert "remove: 2 no longer on the phone (deleted)" in text
    plan, result = _apply(conn, later)
    assert sorted(r["stream"] for r in plan["removed"]) == ["android_call", "android_sms"]
    assert result["removed"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM observations_fts WHERE "
                        "observations_fts MATCH '\"see you at 6\"'").fetchone()["c"] == 0
    assert run_checks(conn, cfg)["failures"] == 0

    # restored with the app's own Restore: the next backup has it again
    plan, _ = _apply(conn, an.scan(make_backup(tmp_path / "c"), cfg))
    assert sorted(r["body"] or r["stream"] for r in plan["new"]) == ["android_call",
                                                                      "see you at 6"]


def test_every_id_in_the_file_counts_as_present_skipped_ones_included(cfg, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "sms-1.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00Z", 4, "still in the outbox")]), encoding="utf-8")
    cfg.connectors = {"android_sms": {"account": ME}}
    queued = an.scan(folder, cfg)
    assert queued["records"] == [] and len(queued["present_ids"]) == 1

    # sent by the next backup: the same message, so the same id
    (folder / "sms-1.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00Z", 2, "still in the outbox")]), encoding="utf-8")
    sent = an.scan(folder, cfg)
    assert [r["external_id"] for r in sent["records"]] == list(queued["present_ids"])


def test_an_incremental_backup_never_removes_anything(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    scan = an.scan(make_backup(tmp_path / "b", variant="few", sms_type="incremental",
                               with_calls=False), cfg)
    assert "present_ids" not in scan
    plan = obs.build_plan(conn, scan)
    assert plan["removed"] == []
    text = "\n".join(an.report_lines(scan, plan))
    assert "not mirroring deletions: texts: sms-20260920020003.xml is marked " \
           "type=\"incremental\", not full — only a full backup is the phone's whole " \
           "state" in text


def test_a_backup_that_does_not_state_its_type_mirrors_only_when_configured(
        cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    untyped = make_backup(tmp_path / "b", variant="deleted", sms_type=None, calls_type=None)
    scan = an.scan(untyped, cfg)
    assert "present_ids" not in scan
    assert any("does not say whether it is a full backup" in w for w in scan["not_mirroring"])
    assert obs.build_plan(conn, scan)["removed"] == []

    cfg.connectors = {"android_sms": {"mirror": True}}
    assert len(obs.build_plan(conn, an.scan(untyped, cfg))["removed"]) == 2


def test_mirror_false_turns_mirroring_off_even_for_a_full_backup(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    cfg.connectors = {"android_sms": {"mirror": False}}
    scan = an.scan(make_backup(tmp_path / "b", variant="deleted"), cfg)
    assert obs.build_plan(conn, scan)["removed"] == []
    assert scan["not_mirroring"] == [
        "texts: mirror = false in [connectors.android_sms]",
        "calls: mirror = false in [connectors.android_sms]"]


def test_a_mirror_setting_that_is_not_true_or_false_is_refused(cfg, tmp_path):
    cfg.connectors = {"android_sms": {"mirror": "yes"}}
    with pytest.raises(DomainError, match="mirror = 'yes' must be true or false"):
        an.scan(make_backup(tmp_path / "b"), cfg)


def test_a_file_whose_count_disagrees_with_its_contents_never_mirrors(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    scan = an.scan(make_backup(tmp_path / "b", variant="deleted", sms_count=99,
                               with_calls=False), cfg)
    assert obs.build_plan(conn, scan)["removed"] == []
    assert "says it holds 99 but has 9" in scan["not_mirroring"][0]
    text = "\n".join(an.report_lines(scan, obs.build_plan(conn, scan)))
    assert "9 message(s) (file says 99)" in text


def test_reading_only_the_texts_never_removes_calls(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    scan = an.scan(make_backup(tmp_path / "b", variant="deleted", with_calls=False), cfg)
    assert scan["mirror_streams"] == ["android_mms", "android_sms"]
    assert [r["stream"] for r in obs.build_plan(conn, scan)["removed"]] == ["android_sms"]


def test_removal_never_reaches_another_phones_archive(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    cfg.connectors = {"android_sms": {"account": "+15125559999"}}
    other = an.scan(make_backup(tmp_path / "b", variant="deleted"), cfg)
    assert other["account"] == "+15125559999"
    assert obs.build_plan(conn, other)["removed"] == []


def test_a_text_a_journal_entry_cites_is_hidden_not_deleted(cfg, conn, tmp_path):
    from conftest import golden_draft
    from lifecrm.domain import reconcile
    from test_reconcile import _ingest
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    entry_id = conn.execute("SELECT id FROM entries LIMIT 1").fetchone()["id"]
    obs_id = conn.execute("SELECT id FROM observations WHERE body='see you at 6'"
                          ).fetchone()["id"]
    conn.execute("INSERT INTO entry_observations (entry_id, observation_id) VALUES (?, ?)",
                 (entry_id, obs_id))
    conn.commit()
    _, result = _apply(conn, an.scan(make_backup(tmp_path / "b", variant="deleted"), cfg))
    assert result.get("retracted_cited") == 1
    assert conn.execute("SELECT status FROM observations WHERE id=?",
                        (obs_id,)).fetchone()["status"] == "retracted"
    assert run_checks(conn, cfg)["failures"] == 0


def test_removing_most_of_the_archive_at_once_is_refused(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 2)
    _apply(conn, an.scan(make_backup(tmp_path / "a"), cfg))
    scan = an.scan(make_backup(tmp_path / "b", variant="few", with_calls=False), cfg)
    plan = obs.build_plan(conn, scan)
    assert len(plan["removed"]) == 6
    with pytest.raises(DomainError, match="remove 6 of 8.*--allow-mass-removal"):
        obs.apply_plan(conn, scan, plan)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 12


# ---------------------------------------------------------------- the confirmed tier

def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    _apply(conn, an.scan(make_backup(tmp_path / "b"), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    assert conn.execute("SELECT COUNT(*) c FROM entity_handles WHERE entity_id IS NOT NULL"
                        ).fetchone()["c"] == 0


def test_doctor_is_green_after_an_import(cfg, conn, tmp_path):
    _apply(conn, an.scan(make_backup(tmp_path / "b"), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def _status(report, check):
    return next((c["status"] for c in report["checks"] if c["check"] == check), None)


def test_doctor_warns_when_the_newest_android_record_is_old(cfg, conn, tmp_path):
    assert _status(run_checks(conn, cfg), "android_sms_data_age") is None
    _apply(conn, an.scan(make_backup(tmp_path / "b"), cfg))        # newest: 2026-09-03
    report = run_checks(conn, cfg)
    assert _status(report, "android_sms_data_age") == "warn"
    assert report["failures"] == 0


def test_doctor_is_quiet_when_android_texts_are_current(cfg, conn):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _apply(conn, base.empty_scan("android_sms", records=[{
        "stream": "android_call", "external_id": "android:fresh",
        "journal_date": now[:10], "started_at": now, "direction": "in", "value": 30,
        "unit": "s", "origin_app": "phone", "last_modified_at": now,
        "participants": []}]))
    assert _status(run_checks(conn, cfg), "android_sms_data_age") == "ok"


# ---------------------------------------------------------------- finding the backup

def _no_onedrive(monkeypatch, tmp_path):
    monkeypatch.setattr("lifecrm.platforms.onedrive_candidates", lambda: [tmp_path / "OneDrive"])
    monkeypatch.setattr("lifecrm.platforms.home", lambda: tmp_path / "home")


def test_no_path_uses_the_configured_folder_first(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    make_backup(tmp_path / "OneDrive" / "Apps" / "SMS Backup & Restore")
    mine = make_backup(tmp_path / "phone-backups")
    cfg.connectors = {"android_sms": {"folder": str(mine)}}
    assert an.default_target(cfg) == mine


def test_no_path_finds_the_apps_onedrive_folder(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    folder = make_backup(tmp_path / "OneDrive" / "Apps" / "SMS Backup & Restore")
    assert an.default_target(cfg) == folder


def test_no_path_falls_back_to_downloads(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    (tmp_path / "OneDrive" / "Apps" / "SMS Backup & Restore").mkdir(parents=True)  # empty
    folder = make_backup(tmp_path / "home" / "Downloads")
    assert an.default_target(cfg) == folder


def test_no_path_and_no_backup_says_where_it_looked(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    with pytest.raises(DomainError) as e:
        an.default_target(cfg)
    msg = str(e.value)
    assert "no SMS Backup & Restore file" in msg and "\n" not in msg
    assert str(tmp_path / "OneDrive" / "Apps" / "SMS Backup & Restore") in msg
    assert str(tmp_path / "home" / "Downloads") in msg
    assert "[connectors.android_sms] folder" in msg


def test_a_configured_folder_that_does_not_exist_is_named(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    cfg.connectors = {"android_sms": {"folder": str(tmp_path / "nope")}}
    with pytest.raises(DomainError, match="folder .*nope does not exist"):
        an.default_target(cfg)


def test_a_configured_folder_may_start_with_a_tilde(cfg, tmp_path, monkeypatch):
    _no_onedrive(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    folder = make_backup(tmp_path / "home" / "texts")
    cfg.connectors = {"android_sms": {"folder": "~/texts"}}
    assert an.default_target(cfg) == folder


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_reimport_and_default_target(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    _no_onedrive(monkeypatch, tmp_path)
    folder = make_backup(tmp_path / "OneDrive" / "Apps" / "SMS Backup & Restore")

    r = runner.invoke(app, ["observe", "android_sms"])
    assert r.exit_code == 0, r.output
    assert "android-sms: sms-20260920020003.xml + calls-20260920020003.xml" in r.output
    assert "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0

    r = runner.invoke(app, ["observe", "android_sms", str(folder), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 12 added" in r.output

    r = runner.invoke(app, ["observe", "android_sms", str(folder), "--apply"])
    assert r.exit_code == 0, r.output
    assert "0 added" in r.output and "12 identical skipped" in r.output

    r = runner.invoke(app, ["observe", "android_sms", str(folder), "--json"])
    out = json.loads(r.output)
    assert out["previously_imported_as"] and out["facts"]["backups"]["texts"]["type"] == "full"
    assert out["account"] == ME

    r = runner.invoke(app, ["connectors"])
    assert "android_sms" in r.output and "android_call" in r.output

    r = runner.invoke(app, ["handles", "list", "--json"])
    names = {row["value"]: row["display_name"] for row in json.loads(r.output)}
    assert names[JAKE] == "Jake Reilly" and names[ME] == an.OWNER_LABEL
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_removal_takes_a_snapshot(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["observe", "android_sms", str(make_backup(tmp_path / "a")),
                            "--apply"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["observe", "android_sms",
                            str(make_backup(tmp_path / "b", variant="deleted")), "--apply"])
    assert r.exit_code == 0, r.output
    assert "2 removed" in r.output and "snapshot before removing:" in r.output
    assert list((root / "backups").glob("pre-removal-*.db"))
