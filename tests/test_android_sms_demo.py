"""The demo's Samsung backup: persona.THREADS and persona.CALLS rendered as
SMS Backup & Restore files, read back by the real connector.

What matters is that the story survives the round trip — Jake's number and
name, the Veracruz texts, the Bled crew as one group thread, the texts from
the trip four years ago — that the sample is deterministic for an anchor,
and that it lands in a fresh database cleanly: a second import changes
nothing and doctor stays green.
"""

from datetime import date

import pytest

from lifecrm.connectors import android_sms as an
from lifecrm.demo import persona
from lifecrm.demo.sources import android_sms as gen
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks

ANCHOR = date(2026, 10, 21)


def _scan(cfg, tmp_path, anchor=ANCHOR):
    return an.scan(gen.write_sample(tmp_path / "demo", anchor, "android"), cfg)


def _bodies(scan, stream=None):
    return [r["body"] for r in scan["records"]
            if r["body"] and (stream is None or r["stream"] == stream)]


# ---------------------------------------------------------------- the files

def test_the_sample_is_the_apps_two_files_in_its_folder(tmp_path):
    folder = gen.write_sample(tmp_path, ANCHOR, "android")
    assert folder.name == "SMSBackupRestore"
    assert sorted(p.name for p in folder.iterdir()) == [
        "calls-20261021020003.xml", "sms-20261021020003.xml"]
    sms = (folder / "sms-20261021020003.xml").read_text(encoding="utf-8")
    assert sms.startswith("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>")
    assert '<smses count="' in sms and 'type="full"' in sms
    assert "&#55357;&#56995;" in sms          # the rowboat emoji, as the app writes it
    assert 'contact_name="Jake Morales, Maya Chen, Sam Okafor"' in sms


def test_the_sample_is_deterministic_for_an_anchor(tmp_path):
    a = gen.write_sample(tmp_path / "a", ANCHOR, "android")
    b = gen.write_sample(tmp_path / "b", ANCHOR, "android")
    for p in a.iterdir():
        assert p.read_bytes() == (b / p.name).read_bytes()


def test_the_iphone_has_no_sms_backup_and_restore_sample(tmp_path):
    with pytest.raises(ValueError, match="android_sms has no 'iphone' sample"):
        gen.write_sample(tmp_path, ANCHOR, "iphone")


# ---------------------------------------------------------------- the story, read back

def test_every_persona_text_and_call_comes_through(cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    bodies = _bodies(scan)
    for thread in persona.THREADS:
        for _, _, _, text in thread.messages:
            assert text in bodies
    assert len(bodies) == sum(len(t.messages) for t in persona.THREADS) + len(gen.EXTRA)
    calls = [r for r in scan["records"] if r["stream"] == "android_call"]
    assert len(calls) == len(persona.CALLS)
    assert scan["skipped"] == [] and scan["unmapped"] == []


def test_jake_is_a_named_phone_handle_and_the_phone_knows_its_owner(cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    jake = persona.person("jake")
    assert names[("phone", jake.phone)] == "Jake Morales"
    assert scan["account"] == persona.OWNER.phone
    assert names[("phone", persona.OWNER.phone)] == an.OWNER_LABEL
    veracruz = next(r for r in scan["records"]
                    if r["body"] == "Veracruz tonight? you know it's my favorite")
    assert veracruz["stream"] == "android_sms" and veracruz["direction"] == "in"
    assert {(p["value"], p["role"]) for p in veracruz["participants"]} == {
        (jake.phone, "sender"), (persona.OWNER.phone, "recipient")}
    assert veracruz["journal_date"] == persona.day(ANCHOR, -38).isoformat()


def test_the_bled_crew_is_one_group_mms_thread(cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    group = [r for r in scan["records"] if r["stream"] == "android_mms"
             and '"group": true' in (r["value_json"] or "")]
    crew = next(t for t in persona.THREADS if t.key == "bled_crew")
    assert sorted(r["body"] for r in group) == sorted(m[3] for m in crew.messages)
    threads = {r["value_json"] for r in group}
    assert len(threads) == 1
    members = {persona.person(k).phone for k in crew.members}
    first = next(r for r in group if r["body"] == "Who's up for Lake Bled next summer?")
    assert first["direction"] == "out"
    assert {p["value"] for p in first["participants"] if p["role"] == "recipient"} == members


def test_texts_from_the_bled_trip_four_years_ago_are_in_the_backup(cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    rowboat = next(r for r in scan["records"] if "Rowboat out to the island" in (r["body"] or ""))
    assert rowboat["body"].endswith("\U0001F6A3")              # repaired, not mangled
    assert rowboat["journal_date"] == persona.bled_trip_days(ANCHOR)[2].isoformat()
    assert scan["facts"]["oldest_message"] == rowboat["journal_date"]
    assert scan["facts"]["repaired"]["surrogate_pairs"] == 3
    assert "Still thinking about that rowboat on Lake Bled lol" in _bodies(scan)


def test_calls_carry_the_personas_durations_and_outcomes(cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    calls = {r["journal_date"]: r for r in scan["records"] if r["stream"] == "android_call"}
    mom = calls[persona.day(ANCHOR, -34).isoformat()]
    assert (mom["value"], mom["direction"], mom["value_json"]) == (1320, "out",
                                                                   '{"call": "out"}')
    priya = calls[persona.day(ANCHOR, -3).isoformat()]
    assert priya["value_json"] == '{"call": "missed"}' and priya["value"] == 0


def test_the_climbing_photo_is_described_not_stored(cfg, conn, tmp_path):
    scan = _scan(cfg, tmp_path)
    photo = next(r for r in scan["records"] if r["body"] == "sent the purple V3!! \U0001F9D7")
    assert '"attachments": [{"mime": "image/jpeg", "name": "movement_climb.jpg"}]' \
        in photo["value_json"]
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert "/9j/" not in "\n".join(conn.iterdump())          # no JPEG base64 anywhere


@pytest.mark.parametrize("anchor", [date(2026, 10, 21), date(2028, 2, 29), date(2027, 1, 3)])
def test_the_sample_imports_cleanly_and_a_second_import_changes_nothing(
        cfg, conn, tmp_path, anchor):
    folder = gen.write_sample(tmp_path / "demo", anchor, "android")
    scan = an.scan(folder, cfg)
    assert scan["facts"]["backups"]["texts"]["mirrored"] is True
    plan = obs.build_plan(conn, scan)
    assert plan["invalid"] == [] and plan["conflicts"] == []
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    assert result["added"] == len(scan["records"])

    again = obs.build_plan(conn, an.scan(folder, cfg))
    assert not again["new"] and not again["updates"] and not again["removed"]
    assert again["identical"] == len(scan["records"])
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
