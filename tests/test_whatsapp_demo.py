"""The demo's WhatsApp sample: the persona's family group, rendered as each
phone's real "Export chat" and read back by the real connector.

What this guards: both combos tell the same story (the same messages on the
same days from the same people), the US month-first dates resolve from the
dates themselves whatever the anchor, the sample is byte-for-byte
deterministic, and it goes through the real pipeline — apply, re-apply,
doctor — the way a real export would.
"""

import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lifecrm.connectors import whatsapp as wa
from lifecrm.demo import persona
from lifecrm.demo.sources import whatsapp as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

# Offsets -15 and -13 land on the 10th and the 12th: on their own those dates
# fit both orders, so only the group's creation date can settle it.
ANCHOR = date(2026, 10, 25)
GROUP = persona.WHATSAPP_GROUP


@pytest.fixture
def me(cfg):
    cfg.connectors = {"whatsapp": {"me": persona.OWNER.name}}
    return cfg


def _scan(cfg, tmp_path, combo, anchor=ANCHOR):
    return wa.scan(sample.write_sample(tmp_path / combo, anchor, combo), cfg)


def _local_hhmm(iso: str) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(persona.TIMEZONE))
    return dt.strftime("%H:%M")


# ---------------------------------------------------------------- the story

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_every_persona_line_comes_through_on_its_day(me, tmp_path, combo):
    scan = _scan(me, tmp_path, combo)
    got = {(r["journal_date"], r["body"]): r for r in scan["records"] if r["body"]}
    sender = {"me": "whatsapp:Alex Rivera", "mom": "whatsapp:Linda Rivera",
              "Tía Rosa": sample.ROSA_PHONE}
    for off, hhmm, who, text in GROUP["messages"]:
        rec = got[(persona.day(ANCHOR, off).isoformat(), text)]
        assert _local_hhmm(rec["started_at"]) == hhmm
        assert rec["direction"] == ("out" if who == "me" else "in")
        assert rec["title"] == GROUP["name"]
        assert rec["participants"][0] == {
            "kind": "phone" if who == "Tía Rosa" else "app_account",
            "value": sender[who], "role": "sender"}


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_people_are_handles_waiting_to_be_linked(me, tmp_path, combo):
    scan = _scan(me, tmp_path, combo)
    assert scan["handles"] == [
        {"kind": "app_account", "value": "whatsapp:Alex Rivera",
         "display_name": "Alex Rivera"},
        {"kind": "app_account", "value": "whatsapp:Linda Rivera",
         "display_name": persona.person("mom").name},
        # Tía Rosa has no contact card: the export knows only her number.
        {"kind": "phone", "value": sample.ROSA_PHONE, "display_name": None}]
    assert scan["facts"]["chats"][0]["group"] is True


def test_both_phones_tell_the_same_story(me, tmp_path):
    def story(scan):
        return sorted((r["journal_date"], _local_hhmm(r["started_at"]), r["direction"],
                       r["body"] or "", r["participants"][0]["value"],
                       "photo" if r["media_path"] else "")
                      for r in scan["records"])
    android = _scan(me, tmp_path, "android")
    iphone = _scan(me, tmp_path, "iphone")
    assert story(android) == story(iphone)
    assert android["skipped"] == iphone["skipped"]
    assert android["facts"]["chats"][0]["phone"] == "android"
    assert iphone["facts"]["chats"][0]["phone"] == "iphone"


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_export_carries_what_real_chats_carry(me, tmp_path, combo):
    scan = _scan(me, tmp_path, combo)
    assert "1 × deleted message ('This message was deleted')" in scan["skipped"]
    assert "1 × system line: encryption notice" in scan["skipped"]
    assert "3 × system line: group change" in scan["skipped"]
    assert scan["unmapped"] == []
    bodies = {r["body"]: r for r in scan["records"] if r["body"]}
    assert '"edited": true' in bodies["We'll be there"]["value_json"]
    assert "Menu so far:\ntamales\narroz con pollo\nRosa is making her flan" in bodies


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_dinner_photo_is_in_the_export_and_opens(me, tmp_path, combo):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    scan = wa.scan(path, me)
    photo = next(r for r in scan["records"] if r["media_path"])
    assert photo["journal_date"] == persona.day(ANCHOR, -13).isoformat()
    assert photo["participants"][0]["value"] == sample.ROSA_PHONE
    with zipfile.ZipFile(path) as zf:
        data = zf.read(photo["media_path"])
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
    assert scan["facts"]["media"] == {"present": 1, "missing": 0, "omitted": 0}


# ---------------------------------------------------------------- format

@pytest.mark.parametrize("combo,zip_name,text_name", [
    ("android", "WhatsApp Chat with Rivera Family.zip", "WhatsApp Chat with Rivera Family.txt"),
    ("iphone", "WhatsApp Chat - Rivera Family.zip", "_chat.txt"),
])
def test_the_files_are_named_as_each_phone_names_them(tmp_path, combo, zip_name, text_name):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    assert path.name == zip_name
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist()[0] == text_name


def _text(path):
    with zipfile.ZipFile(path) as zf:
        return zf.read(zf.namelist()[0]).decode("utf-8")


def test_the_iphone_sample_carries_the_marks_ios_writes(tmp_path):
    text = _text(sample.write_sample(tmp_path, ANCHOR, "iphone"))
    assert "\r\n" in text
    assert text.startswith(sample.LRM + "[")
    assert sample.NNBSP + "PM]" in text
    assert "\N{NON-BREAKING HYPHEN}" in text and "\N{LEFT-TO-RIGHT EMBEDDING}" in text
    assert "<attached: " in text


def test_the_android_sample_writes_minutes_and_no_marks(tmp_path):
    text = _text(sample.write_sample(tmp_path, ANCHOR, "android"))
    assert "\r\n" not in text and sample.LRM not in text
    assert sample.NNBSP + "PM - " in text
    assert "(file attached)" in text and "+1 210-555-0131" in text


def test_month_first_reads_from_the_dates_whatever_the_anchor(me, tmp_path):
    """On the 25th, days -15 and -13 are the 10th and the 12th — the anchors
    where the conversation's own dates fit both orders — plus the turn of a
    year, where the creation date's month wraps."""
    anchors = [date(2026, m, 25) for m in range(1, 13)] + [date(2027, 1, 1)]
    for i, anchor in enumerate(anchors):
        for combo in persona.COMBOS:
            scan = _scan(me, tmp_path / str(i), combo, anchor)
            chat = scan["facts"]["chats"][0]
            assert chat["date_order"] == "month-first", (anchor, combo)
            assert chat["date_order_from"] != "guessed", (anchor, combo)
            assert scan["facts"]["warnings"] == [], (anchor, combo)


def test_the_group_was_created_on_the_20th_before_the_window(tmp_path):
    for anchor in (date(2026, 1, 3), date(2026, 2, 28), date(2026, 12, 31)):
        created = sample.created_on(anchor)
        assert created.day == sample.CREATED_DAY
        assert (anchor - created).days > persona.WINDOW_DAYS


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = sample.write_sample(tmp_path / "a", ANCHOR, combo).read_bytes()
    b = sample.write_sample(tmp_path / "b", ANCHOR, combo).read_bytes()
    assert a == b
    later = sample.write_sample(tmp_path / "c", ANCHOR + timedelta(days=1), combo)
    assert later.read_bytes() != a


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_sample(tmp_path, ANCHOR, "windows")
    assert "android, iphone" in str(e.value)


# ---------------------------------------------------------------- pipeline

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_goes_through_the_real_pipeline(me, conn, tmp_path, combo):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    scan = wa.scan(path, me)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert result["added"] == len(GROUP["messages"]) + 3     # menu, photo, caption
    plan = obs.build_plan(conn, wa.scan(path, me))
    assert plan["new"] == [] and plan["conflicts"] == [] and plan["updates"] == []
    assert plan["identical"] == result["added"]
    report = run_checks(conn, me)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_without_me_the_demo_cannot_tell_direction_and_says_so(cfg, tmp_path):
    """One group chat names nobody as you; the demo's config must set
    [connectors.whatsapp] me."""
    scan = _scan(cfg, tmp_path, "iphone")
    assert {r["direction"] for r in scan["records"]} == {None}
    assert any("which sender is you" in w for w in scan["facts"]["warnings"])
