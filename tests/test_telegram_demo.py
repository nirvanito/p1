"""The demo's Telegram sample: Telegram Desktop's "Export Telegram data",
rendered as its real machine-readable JSON and read back by the real
connector.

What this guards: the persona's story comes through (Sam and the new
climbing gym on Saturday — the note waiting in today's inbox —, the group
he makes with Maya, Alex's Bled note in Saved Messages), both combos get the
same export, the sample is deterministic, and it goes through the real
pipeline — apply, re-apply, doctor.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lifecrm.connectors import telegram as tg
from lifecrm.demo import persona
from lifecrm.demo.sources import telegram as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 25)
SAM = f"telegram:user{sample.USER_IDS['sam']}"
MAYA = f"telegram:user{sample.USER_IDS['maya']}"
ME = f"telegram:user{sample.USER_IDS['me']}"


def _scan(cfg, tmp_path, combo, anchor=ANCHOR):
    return tg.scan(sample.write_sample(tmp_path / combo, anchor, combo), cfg)


def _local(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(persona.TIMEZONE))


# ---------------------------------------------------------------- the story

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_sam_asks_about_the_new_climbing_gym_on_saturday(cfg, tmp_path, combo):
    scan = _scan(cfg, tmp_path, combo)
    ask = next(r for r in scan["records"]
               if r["body"] == "Want to try the new climbing gym on Saturday?")
    assert ask["journal_date"] == ANCHOR.isoformat()
    assert ask["participants"][0]["value"] == SAM and ask["direction"] == "in"
    # The note waiting in today's inbox says the same thing.
    today = next(j for j in persona.JOURNAL if j.offset == 0)
    assert "new climbing gym on Saturday" in today.notes[0].text
    assert _local(ask["started_at"]).strftime("%H:%M") < today.notes[0].time[:5]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_group_and_the_saved_note_come_through(cfg, tmp_path, combo):
    scan = _scan(cfg, tmp_path, combo)
    group = [r for r in scan["records"] if r["title"] == "Saturday Climbers"]
    assert {p["value"] for r in group for p in r["participants"]} == {SAM, MAYA, ME}
    assert any(r["body"] == "I'm in, I'll bring the extra chalk" for r in group)
    snacks = next(r for r in group if (r["body"] or "").startswith("Bringing snacks"))
    assert snacks["direction"] == "out"
    assert json.loads(snacks["value_json"])["reactions"] == [
        {"by": [SAM, MAYA], "count": 2, "reaction": "\N{THUMBS UP SIGN}"}]
    saved = next(r for r in scan["records"] if r["direction"] == "self")
    assert "Vintgar Gorge" in saved["body"]
    assert saved["journal_date"] == persona.day(ANCHOR, -16).isoformat()


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_people_are_user_id_handles_waiting_to_be_linked(cfg, tmp_path, combo):
    scan = _scan(cfg, tmp_path, combo)
    assert scan["handles"] == [
        {"kind": "app_account", "value": ME, "display_name": persona.OWNER.name},
        {"kind": "app_account", "value": SAM, "display_name": persona.person("sam").name},
        {"kind": "app_account", "value": MAYA, "display_name": persona.person("maya").name}]
    assert scan["facts"]["you_from"] == "the export's personal_information"
    assert scan["facts"]["warnings"] == []


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_export_carries_what_real_ones_carry(cfg, tmp_path, combo):
    scan = _scan(cfg, tmp_path, combo)
    assert scan["skipped"] == [
        "1 × message in a channel or public group (include_channels = true imports them)",
        "1 × service: create_group", "1 × service: phone_call", "1 × service: pin_message"]
    assert scan["unmapped"] == []
    assert scan["facts"]["media"] == {"present": 1, "missing": 0, "omitted": 1}
    assert scan["facts"]["other_sections"] == {"contacts": 2}
    edited = next(r for r in scan["records"] if (r["body"] or "").startswith("Yes! Morning"))
    assert json.loads(edited["value_json"]) == {"edited": True}
    assert edited["last_modified_at"] > edited["started_at"]
    bodies = {r["body"] for r in scan["records"]}
    assert "Both. Opening week is half price day passes" in bodies


def test_both_combos_get_the_same_export(tmp_path):
    a = sample.write_sample(tmp_path / "a", ANCHOR, "android")
    b = sample.write_sample(tmp_path / "b", ANCHOR, "iphone")
    assert (a / "result.json").read_bytes() == (b / "result.json").read_bytes()


# ---------------------------------------------------------------- format

def test_the_folder_is_laid_out_as_telegram_desktop_writes_it(tmp_path):
    out = sample.write_sample(tmp_path, ANCHOR, "android")
    assert out.name == "DataExport_2026-10-25"
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["personal_information"]["user_id"] == sample.USER_IDS["me"]
    types = [c["type"] for c in data["chats"]["list"]]
    assert types == ["personal_chat", "private_group", "saved_messages", "public_channel"]
    photo = next(m["photo"] for c in data["chats"]["list"] for m in c["messages"]
                 if "photo" in m)
    assert (out / photo).read_bytes().startswith(b"\xff\xd8")
    msg = data["chats"]["list"][0]["messages"][1]
    assert msg["date_unixtime"].isdigit() and "T" in msg["date"]
    assert "\\u" not in (out / "result.json").read_text(encoding="utf-8")   # real UTF-8


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = sample.write_sample(tmp_path / "a", ANCHOR, combo)
    b = sample.write_sample(tmp_path / "b", ANCHOR, combo)
    assert (a / "result.json").read_bytes() == (b / "result.json").read_bytes()
    later = sample.write_sample(tmp_path / "c", ANCHOR + timedelta(days=1), combo)
    assert (later / "result.json").read_bytes() != (a / "result.json").read_bytes()


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_sample(tmp_path, ANCHOR, "windows")
    assert "android, iphone" in str(e.value)


# ---------------------------------------------------------------- pipeline

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_goes_through_the_real_pipeline(cfg, conn, tmp_path, combo):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    scan = tg.scan(path, cfg)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert result["added"] == 7 + 3 + 1          # Sam, the group, Saved Messages
    plan = obs.build_plan(conn, tg.scan(path, cfg))
    assert plan["new"] == [] and plan["conflicts"] == [] and plan["updates"] == []
    assert plan["identical"] == result["added"]
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
