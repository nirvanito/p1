"""The demo's mail: the persona's emails rendered as a Gmail Takeout (the
Samsung setup) and as an Apple Mail export (the Mac), read back by the real
connector.

What this guards: both combos tell the same story — Priya's advice in her
own words, the board's verdict, the dentist's reminder, Jake's Bled tips on
the trip's first day — skip the same newsletter and spam, read the owner
from the demo's [owner] table, are byte-for-byte deterministic, and go
through the real pipeline (apply, re-apply, doctor) the way a real export
would.
"""

import json
import zipfile
from datetime import date, timedelta

import pytest

from lifecrm.connectors import email as em
from lifecrm.demo import persona
from lifecrm.demo.sources import email as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 22)
ME = persona.OWNER.email
PRIYA = persona.person("priya").email


@pytest.fixture
def owner(cfg):
    """What the demo's config.toml says: [owner] handles (lifecrm/demo/build.py)."""
    cfg.extra = {"owner": {"name": persona.OWNER.name,
                           "handles": [f"email:{ME}", f"phone:{persona.OWNER.phone}"]}}
    return cfg


def _scan(cfg, tmp_path, combo, anchor=ANCHOR):
    return em.scan(sample.write_sample(tmp_path / combo, anchor, combo), cfg)


def _by_id(scan) -> dict:
    return {r["external_id"]: r for r in scan["records"]}


def _mail(key: str) -> sample.Mail:
    return next(m for m in sample._mails(ANCHOR) if m.key == key)


def _rec(scan, key):
    return _by_id(scan)[sample.message_id(_mail(key))]


def _sender(rec) -> str:
    return next(p["value"] for p in rec["participants"] if p["role"] == "sender")


# ---------------------------------------------------------------- the story

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_priyas_advice_comes_through_in_her_own_words(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    advice = _rec(scan, "priya_advice")
    assert "lead with the customer story, not the TAM slide" in advice["body"]
    assert _sender(advice) == PRIYA and advice["direction"] == "in"
    assert advice["journal_date"] == persona.day(ANCHOR, -27).isoformat()
    # Her signature and the quoted draft are trimmed; the length of all is kept.
    assert "VP Product" not in advice["body"] and "Draft v3" not in advice["body"]
    detail = json.loads(advice["value_json"])
    assert detail["trimmed"] is True and detail["text_length"] > len(advice["body"])
    loved = _rec(scan, "board_loved_it")
    assert loved["body"].startswith("Alex,\n\nThe board loved it.")
    assert loved["journal_date"] == persona.day(ANCHOR, -20).isoformat()
    assert json.loads(loved["value_json"])["cc_count"] == 1


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_deck_thread_is_one_thread_of_re_subjects(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    draft, advice, thanks = (_rec(scan, k) for k in ("deck_draft", "priya_advice",
                                                      "deck_thanks"))
    threads = {json.loads(r["value_json"])["thread"] for r in (draft, advice, thanks)}
    assert len(threads) == 1
    assert advice["title"] == thanks["title"] == "Re: " + draft["title"]
    assert json.loads(thanks["value_json"])["in_reply_to"] == advice["external_id"]
    assert [r["direction"] for r in (draft, advice, thanks)] == ["out", "in", "out"]
    assert json.loads(draft["value_json"])["attachments"] == [
        {"name": "Q4 Board Deck v3.pdf", "mime": "application/pdf", "size": 48_212}]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_jakes_bled_tips_are_dated_on_the_trip(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    trip = persona.bled_trip_days(ANCHOR)
    tips = _rec(scan, "jake_tips")
    assert tips["journal_date"] == trip[0].isoformat()
    assert _sender(tips) == persona.person("jake").email
    for words in ("pletna boat", "Vintgar Gorge", "cream cake", "Park Hotel"):
        assert words in tips["body"], words
    # Written in Slovenia: the Date header's own zone is kept.
    assert tips["utc_offset_sec"] in (3600, 7200)
    reply = _rec(scan, "jake_tips_reply")
    assert reply["direction"] == "out" and "Sent from my iPhone" not in reply["body"]
    assert reply["body"].startswith("Landed.")


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_dentist_reminder_the_flights_and_the_rest(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    reminder = _rec(scan, "dentist_reminder")
    assert _sender(reminder) == "frontdesk@ellisdental.example"
    assert "Dr. Rebecca Ellis" in reminder["body"] and "10:30 AM" in reminder["body"]
    assert _rec(scan, "dentist_confirm")["direction"] == "out"
    flights = _rec(scan, "flights_booked")
    assert "USD 1,124.60" in flights["body"] and sample.BOOKING_REFERENCE in flights["body"]
    assert flights["journal_date"] < persona.bled_trip_days(ANCHOR)[0].isoformat()
    invite = json.loads(_rec(scan, "board_invite")["value_json"])
    assert invite["invite"] is True
    assert _rec(scan, "flights_note")["direction"] == "self"
    mom = _rec(scan, "mom_reply")
    assert mom["body"].endswith("Love, Mom") and "iPad" not in mom["body"]
    photos = json.loads(_rec(scan, "birthday_photos")["value_json"])
    assert photos["attachments"][0]["name"] == "maya_birthday.jpg"


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_newsletter_and_the_spam_are_counted_not_kept(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    kept = {sample.message_id(_mail(k)) for k in sample.kept_keys()}
    assert set(_by_id(scan)) == kept and len(kept) == 14
    skipped = " ".join(scan["skipped"])
    assert "1 × mailing-list or bulk mail" in skipped
    # a Gmail Takeout labels it (both Windows laptops); Apple Mail files it in Junk
    spam = "1 × message in the Junk mailbox" if persona.on_mac(combo) \
        else "1 × message labelled Spam"
    assert spam in skipped
    assert scan["unmapped"] == []


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_people_are_email_handles_waiting_to_be_linked(owner, tmp_path, combo):
    scan = _scan(owner, tmp_path, combo)
    handles = {h["value"]: h["display_name"] for h in scan["handles"]}
    for key in ("jake", "maya", "mom", "priya", "sam"):
        p = persona.person(key)
        assert handles[p.email] == p.name
    assert handles["frontdesk@ellisdental.example"] == "Ellis Dental"
    assert handles[ME] == persona.OWNER.name
    assert scan["account"] == ME
    assert scan["facts"]["you_from"] == "[owner] handles in config.toml"
    # Nothing from the skipped mail becomes a handle.
    assert sample.NEWSLETTER[1] not in handles and sample.SPAM[1] not in handles


def test_both_setups_tell_the_same_story(owner, tmp_path):
    def story(scan):
        return sorted((r["external_id"], r["journal_date"], r["started_at"],
                       r["direction"], r["title"], r["body"],
                       tuple((p["role"], p["value"]) for p in r["participants"]))
                      for r in scan["records"])
    android = _scan(owner, tmp_path, "android")
    iphone = _scan(owner, tmp_path, "iphone")
    assert story(android) == story(iphone)
    assert {r["origin_app"] for r in android["records"]} == {"gmail"}
    assert {r["origin_app"] for r in iphone["records"]} == {"apple_mail"}


def test_without_owner_settings_the_sent_mail_says_who_you_are(cfg, tmp_path):
    for combo in persona.COMBOS:
        scan = _scan(cfg, tmp_path, combo)
        assert scan["account"] == ME
        assert scan["facts"]["you_from"].startswith("inferred")
        assert _rec(scan, "deck_thanks")["direction"] == "out"


# ---------------------------------------------------------------- format

def test_the_android_sample_is_a_takeout_zip(tmp_path):
    path = sample.write_sample(tmp_path, ANCHOR, "android")
    assert path.name == f"takeout-{ANCHOR:%Y%m%d}T120000Z-001.zip"
    with zipfile.ZipFile(path) as zf:
        assert sample.TAKEOUT_MBOX in zf.namelist()
        text = zf.read(sample.TAKEOUT_MBOX).decode("utf-8")
    assert text.startswith("From ") and "\r\n" in text
    assert "X-GM-THRID: " in text and "X-Gmail-Labels: Spam" in text
    assert "=?utf-8?q?" in text.lower()                  # the em dash in a subject


def test_the_iphone_sample_is_an_apple_mail_export(tmp_path):
    root = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert root.name == sample.EXPORT_FOLDER
    boxes = sorted(p.name for p in root.iterdir())
    assert boxes == ["INBOX.mbox", "Junk.mbox", "Sent Messages.mbox"]
    for box in boxes:
        assert sorted(p.name for p in (root / box).iterdir()) == ["mbox", "table_of_contents"]
    inbox = (root / "INBOX.mbox" / "mbox").read_bytes()
    assert inbox.startswith(b"From ") and b"\r\n" not in inbox


def test_the_note_to_self_is_in_two_mailboxes_and_imported_once(owner, tmp_path):
    scan = _scan(owner, tmp_path, "iphone")
    note = _rec(scan, "flights_note")
    assert json.loads(note["value_json"])["mailbox"] == "INBOX"
    assert "1 × second copy of a message already read (same Message-ID)" in scan["skipped"]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    def contents(path):
        """Each file's bytes and its time (the reader's last_modified_at)."""
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        return [(p.relative_to(path).as_posix() if p != path else p.name, p.read_bytes(),
                 p.stat().st_mtime) for p in files]
    a = sample.write_sample(tmp_path / "a", ANCHOR, combo)
    b = sample.write_sample(tmp_path / "b", ANCHOR, combo)
    assert contents(a) == contents(b)
    later = sample.write_sample(tmp_path / "c", ANCHOR + timedelta(days=1), combo)
    assert contents(later) != contents(a)


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_sample(tmp_path, ANCHOR, "windows")
    assert "android, iphone" in str(e.value)


@pytest.mark.parametrize("anchor", [date(2026, 1, 3), date(2026, 3, 30), date(2027, 7, 1)])
def test_the_story_holds_for_any_anchor(owner, tmp_path, anchor):
    for combo in persona.COMBOS:
        scan = _scan(owner, tmp_path / str(anchor), combo, anchor)
        assert len(scan["records"]) == 14 and scan["facts"]["warnings"] == []
        tips = next(r for r in scan["records"] if r["title"] == "Bled tips")
        assert tips["journal_date"] == persona.bled_trip_days(anchor)[0].isoformat()


# ---------------------------------------------------------------- pipeline

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_goes_through_the_real_pipeline(owner, conn, tmp_path, combo):
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    scan = em.scan(path, owner)
    result = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert result["added"] == 14
    plan = obs.build_plan(conn, em.scan(path, owner))
    assert plan["new"] == [] and plan["conflicts"] == [] and plan["updates"] == []
    assert plan["identical"] == 14
    report = run_checks(conn, owner)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    hits = conn.execute("SELECT o.title FROM observations_fts f JOIN observations o "
                        "ON o.id = f.rowid WHERE observations_fts MATCH ?",
                        ('"customer story" AND TAM',)).fetchall()
    assert [h["title"] for h in hits] == ["Re: " + _mail("deck_draft").subject]
