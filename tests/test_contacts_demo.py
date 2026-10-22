"""The demo's address books: a Google Contacts vCard (Windows + Samsung) and a
Mac's Contacts folder (MacBook + iPhone), both rendering the persona.

Each sample goes through the real connector into a fresh database: the story's
facts must come out the other side — Jake's number and his card's notes about
El Paso and Veracruz, Maya's birthday inside the demo window — a re-import must
be all-identical, and doctor must stay green.
"""

import json
from datetime import date

import pytest

from lifecrm.connectors import contacts
from lifecrm.demo import persona
from lifecrm.demo.sources import contacts as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 21)


def _cards(scan) -> dict:
    return {r["title"]: r for r in scan["records"]}


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_carries_the_persona(combo, cfg, tmp_path):
    target = sample.write_sample(tmp_path / combo, ANCHOR, combo)
    scan = contacts.scan(target, cfg)
    cards = _cards(scan)
    jake = cards["Jake Morales"]
    assert {"kind": "phone", "value": "+15125550142", "role": "participant"} \
        in jake["participants"]
    assert "Grew up in El Paso" in jake["body"]
    assert "Veracruz All Natural" in jake["body"]
    assert "Lives in Austin, TX" in jake["body"]
    assert "Birthday: March 14, 1993" in jake["body"]
    assert "Veracruz All Natural" in cards                       # the restaurant's card
    maya_bday = persona.birthday_date(persona.person("maya"), ANCHOR)
    assert json.loads(cards["Maya Chen"]["value_json"])["birthday"] == \
        maya_bday.replace(year=1995).isoformat()
    assert "Lives in San Antonio, TX" in cards["Linda Rivera"]["body"]
    assert "Director of Product at Acme Robotics" in cards["Priya Natarajan"]["body"]
    assert "Dr. Rebecca Ellis" in cards
    names = {h["value"]: h["display_name"] for h in scan["handles"]}
    for p in persona.PEOPLE:
        assert names[p.phone] == p.name
        assert names[p.email] == p.name


def test_android_is_a_google_vcard_dated_by_its_export(cfg, tmp_path):
    vcf = sample.write_sample(tmp_path, ANCHOR, "android")
    assert vcf.name == "contacts.vcf"
    raw = vcf.read_bytes()
    assert raw.startswith(b"BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert b"UID:" not in raw and b"REV:" not in raw              # Google writes neither
    assert b"item1.X-ABLabel:Front desk" in raw
    scan = contacts.scan(vcf, cfg)
    assert scan["source_detail"] == "vCard 3.0, google_contacts"
    assert {r["journal_date"] for r in scan["records"]} == {ANCHOR.isoformat()}
    assert scan["account"] is None                                # a vCard says no account


def test_iphone_is_a_mac_contacts_folder_with_an_icloud_store(cfg, tmp_path):
    book = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert book.name == "AddressBook"
    assert (book / "Sources" / sample.ICLOUD_SOURCE / contacts.STORE).exists()
    scan = contacts.scan(book, cfg)
    assert scan["account"] == f"mac-addressbook:{sample.LOCAL_STORE_UUID}"
    cards = _cards(scan)
    assert "Alex Rivera" in cards                                  # the "me" card
    assert len(cards) == len(persona.PEOPLE) + 2                   # + me + the restaurant
    assert cards["Jake Morales"]["journal_date"] == persona.day(ANCHOR, -37).isoformat()
    assert all(r["journal_date"] <= ANCHOR.isoformat() for r in scan["records"])


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_imports_cleanly_and_reimports_as_identical(combo, cfg, conn, tmp_path):
    target = sample.write_sample(tmp_path / combo, ANCHOR, combo)
    scan = contacts.scan(target, cfg)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert result["added"] == len(scan["records"]) > 0
    again = contacts.scan(target, cfg)
    plan = obs.build_plan(conn, again)
    assert plan["new"] == [] and plan["updates"] == [] and not plan["conflicts"]
    assert plan["identical"] == len(scan["records"]) and plan["removed"] == []
    assert run_checks(conn, cfg)["failures"] == 0


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(combo, cfg, tmp_path):
    a = contacts.scan(sample.write_sample(tmp_path / "a", ANCHOR, combo), cfg)
    b = contacts.scan(sample.write_sample(tmp_path / "b", ANCHOR, combo), cfg)
    assert a["records"] == b["records"] and a["handles"] == b["handles"]
    if combo == "android":
        assert (tmp_path / "a" / "contacts.vcf").read_bytes() == \
            (tmp_path / "b" / "contacts.vcf").read_bytes()
    rewritten = contacts.scan(sample.write_sample(tmp_path / "a", ANCHOR, combo), cfg)
    assert rewritten["records"] == a["records"]                    # over an old sample too


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no contacts export for combo 'windows'"):
        sample.write_sample(tmp_path, ANCHOR, "windows")
