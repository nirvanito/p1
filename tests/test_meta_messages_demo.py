"""The demo's Meta sample: Alex's Instagram DMs and Messenger, rendered as
the real "Download your information" JSON export and read back by the real
connector.

What this guards: the persona's story comes through (Maya's ceramics-class
reel before the gift idea, the Uchi birthday plan, the Bled crew, Tía Rosa
on Facebook), Meta's escaping is really in the files and really repaired,
both combos get the same export, the sample is byte-for-byte deterministic,
and it goes through the real pipeline — apply, re-apply, doctor.
"""

import json
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lifecrm.connectors import meta_messages as meta
from lifecrm.demo import persona
from lifecrm.demo.sources import meta_messages as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 25)


def _scans(cfg, tmp_path, combo, anchor=ANCHOR):
    return [meta.scan(p, cfg) for p in sample.write_samples(tmp_path / combo, anchor, combo)]


def _local(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(persona.TIMEZONE))


def _day(offset: int) -> str:
    return persona.day(ANCHOR, offset).isoformat()


# ---------------------------------------------------------------- the story

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_maya_shares_the_ceramics_reel_before_the_gift_idea(cfg, tmp_path, combo):
    ig, _ = _scans(cfg, tmp_path, combo)
    share = next(r for r in ig["records"] if "share" in (r["value_json"] or ""))
    detail = json.loads(share["value_json"])["share"]
    assert detail["owner"] == "austinclaystudio"      # Maya's studio (persona org)
    assert share["participants"][0]["value"] == "instagram:Maya Chen"
    gift_note = next(n for n in persona.NOTES if n.key == "gifts")
    assert share["journal_date"] < _day(gift_note.offset)
    assert "ceramics class voucher" in gift_note.body


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_birthday_dinner_is_planned_the_night_before(cfg, tmp_path, combo):
    ig, _ = _scans(cfg, tmp_path, combo)
    plan = next(r for r in ig["records"] if (r["body"] or "").startswith("Birthday dinner"))
    assert "Uchi" in plan["body"] and plan["direction"] == "out"
    assert plan["journal_date"] == _day(persona.MAYA_BIRTHDAY_OFFSET - 1)
    photo = next(r for r in ig["records"] if r["media_path"])
    assert photo["journal_date"] == _day(persona.MAYA_BIRTHDAY_OFFSET)
    assert _local(photo["started_at"]).strftime("%H:%M") == "23:40"


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_metas_escaping_is_in_the_files_and_comes_out_repaired(cfg, tmp_path, combo):
    paths = sample.write_samples(tmp_path / combo, ANCHOR, combo)
    with zipfile.ZipFile(paths[0]) as zf:
        raw = zf.read(next(n for n in zf.namelist() if "mayachen" in n
                           and n.endswith("message_1.json")))
    assert b"jalape\\u00c3\\u00b1o" in raw and b"Caf\\u00c3\\u00a9" in raw
    ig, fb = (meta.scan(p, cfg) for p in paths)
    bodies = {r["body"] for r in ig["records"] + fb["records"] if r["body"]}
    assert any("jalapeño" in b for b in bodies)
    assert "Coffee at Café Medici before work?" in bodies
    assert any("mañana" in b for b in bodies)
    assert {"messenger:Rosa Treviño", "messenger:Alex Rivera"} <= {
        h["value"] for h in fb["handles"]}


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_bled_crew_is_a_group_of_the_persona(cfg, tmp_path, combo):
    ig, _ = _scans(cfg, tmp_path, combo)
    crew = [r for r in ig["records"] if r["title"] == "Bled crew"]
    assert crew and all(json.loads(r["value_json"])["group"] for r in crew)
    people = {p["value"] for r in crew for p in r["participants"]}
    assert people == {f"instagram:{persona.person(k).name}" for k in ("jake", "maya", "sam")} \
        | {f"instagram:{persona.OWNER.name}"}
    assert any("Vintgar Gorge" in (r["body"] or "") for r in crew)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_people_are_handles_waiting_to_be_linked(cfg, tmp_path, combo):
    ig, fb = _scans(cfg, tmp_path, combo)
    names = {persona.person(k).name for k in ("jake", "maya", "sam")} | {persona.OWNER.name}
    assert ig["handles"] == [{"kind": "app_account", "value": f"instagram:{n}",
                              "display_name": n} for n in sorted(names)]
    assert {h["display_name"] for h in fb["handles"]} == {
        persona.OWNER.name, persona.person("mom").name, sample.ROSA_NAME}
    for scan in (ig, fb):
        assert scan["facts"]["warnings"] == []
        you = next(iter(scan["facts"]["you"].values()))
        assert you == {"name": persona.OWNER.name, "from": "the export's own profile"}


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_export_carries_what_real_ones_carry(cfg, tmp_path, combo):
    ig, fb = _scans(cfg, tmp_path, combo)
    assert ig["skipped"] == [
        "1 × message in a message request (include_requests = true imports them)",
        "1 × system line: group change", "2 × system line: reaction notice",
        "1 × system line: unsent message"]
    assert fb["skipped"] == ["1 × system line: call notice"]
    assert ig["unmapped"] == [] and fb["unmapped"] == []
    mom = [r for r in fb["records"] if r["title"] == persona.person("mom").name]
    assert {json.loads(r["value_json"])["folder"] for r in mom} == {"e2ee_cutover"}
    for scan in (ig, fb):
        assert scan["facts"]["media"] == {"present": 1, "missing": 0}


def test_both_combos_get_the_same_export(cfg, tmp_path):
    a = [p.read_bytes() for p in sample.write_samples(tmp_path / "a", ANCHOR, "android")]
    b = [p.read_bytes() for p in sample.write_samples(tmp_path / "b", ANCHOR, "iphone")]
    assert a == b


# ---------------------------------------------------------------- format

def test_the_zips_are_named_and_laid_out_as_meta_does(tmp_path):
    ig, fb = sample.write_samples(tmp_path, ANCHOR, "android")
    assert ig.name.startswith("instagram-alexrivera.atx-2026-10-25-")
    assert fb.name.startswith("facebook-alexrivera-2026-10-25-")
    with zipfile.ZipFile(ig) as zf:
        names = zf.namelist()
    assert any(n.startswith("your_instagram_activity/messages/inbox/") for n in names)
    assert any(n.startswith("your_instagram_activity/messages/message_requests/")
               for n in names)
    assert "personal_information/personal_information/personal_information.json" in names
    with zipfile.ZipFile(fb) as zf:
        assert any("/messages/e2ee_cutover/" in n for n in zf.namelist())


def test_messages_are_written_newest_first(tmp_path):
    ig, _ = sample.write_samples(tmp_path, ANCHOR, "android")
    with zipfile.ZipFile(ig) as zf:
        data = json.loads(zf.read(next(n for n in zf.namelist() if "mayachen" in n
                                       and n.endswith(".json"))))
    stamps = [m["timestamp_ms"] for m in data["messages"]]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = [p.read_bytes() for p in sample.write_samples(tmp_path / "a", ANCHOR, combo)]
    b = [p.read_bytes() for p in sample.write_samples(tmp_path / "b", ANCHOR, combo)]
    assert a == b
    later = sample.write_samples(tmp_path / "c", ANCHOR + timedelta(days=1), combo)
    assert [p.read_bytes() for p in later] != a


def test_an_unknown_combo_or_platform_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_sample(tmp_path, ANCHOR, "windows")
    assert "android, iphone" in str(e.value)
    with pytest.raises(ValueError):
        sample.write_sample(tmp_path, ANCHOR, "android", platform="myspace")


# ---------------------------------------------------------------- pipeline

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_goes_through_the_real_pipeline(cfg, conn, tmp_path, combo):
    paths = sample.write_samples(tmp_path, ANCHOR, combo)
    added = 0
    for path in paths:
        scan = meta.scan(path, cfg)
        result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
        conn.commit()
        added += result["added"]
        plan = obs.build_plan(conn, meta.scan(path, cfg))
        assert plan["new"] == [] and plan["conflicts"] == [] and plan["updates"] == []
        assert plan["identical"] == result["added"]
    assert added == 13 + 7
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
