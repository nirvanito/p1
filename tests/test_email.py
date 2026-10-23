"""Email connector: Gmail Takeout mbox, Apple Mail exports and store,
Thunderbird folders, .eml files.

The connector is a pure reader, so most of this runs scan() against archives
built here — shaped like the real ones, including the parts that are easy to
get wrong: RFC 2047 subjects and names, 8-bit headers, CRLF mbox lines,
'>From ' escapes, quoted replies in every client's style, HTML-only mail,
dates without a Date header, the owner's own mail to a list, the same
message in two mailboxes, and Apple's .emlx with its trailing plist.
"""

import base64
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import email as em
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

ME = "alex.rivera@example.com"
PRIYA = "priya@acme-robotics.example"
JAKE = "jake.morales@example.com"
TAKEOUT = "takeout-20261020T120000Z-001.zip"
MBOX_MEMBER = "Takeout/Mail/All mail Including Spam and Trash.mbox"


def mail(subject="Deck", sender=f"Priya Natarajan <{PRIYA}>", to=f"Alex Rivera <{ME}>",
         date="Tue, 22 Sep 2026 08:47:00 -0500", body="Lead with the customer story.",
         msgid="<advice@acme-robotics.example>", headers=(),
         ctype='text/plain; charset="UTF-8"', labels=None, thrid=None) -> str:
    """One message, '\\n' line ends. None for a header leaves it out."""
    heads = []
    if thrid:
        heads.append(f"X-GM-THRID: {thrid}")
    if labels is not None:
        heads.append(f"X-Gmail-Labels: {labels}")
    for k, v in (("Date", date), ("Message-ID", msgid), ("Subject", subject),
                 ("From", sender), ("To", to)):
        if v is not None:
            heads.append(f"{k}: {v}")
    heads += list(headers)
    heads += ["MIME-Version: 1.0", f"Content-Type: {ctype}"]
    return "\n".join(heads) + "\n\n" + body + "\n"


def mbox(*messages: str, sep="From 1781@xxx Tue Sep 22 13:47:00 +0000 2026") -> str:
    return "".join(f"{sep}\n{m.rstrip(chr(10))}\n\n" for m in messages)


def takeout(folder: Path, *messages: str, name=TAKEOUT, crlf=True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    text = mbox(*messages)
    if crlf:
        text = text.replace("\n", "\r\n")
    out = folder / name
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Takeout/archive_browser.html", "<html></html>")
        zf.writestr(MBOX_MEMBER, text.encode("utf-8"))
    return out


def sent(subject="Re: Deck", to=f"Priya Natarajan <{PRIYA}>", msgid="<reply@example.com>",
         body="Thanks!", date="Tue, 22 Sep 2026 09:20:00 -0500", **kw) -> str:
    return mail(subject=subject, sender=f"Alex Rivera <{ME}>", to=to, msgid=msgid,
                body=body, date=date, labels=kw.pop("labels", "Sent"), **kw)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _report(conn, scan) -> str:
    return "\n".join(em.report_lines(scan, obs.build_plan(conn, scan)))


def _by_id(scan) -> dict:
    return {r["external_id"]: r for r in scan["records"]}


def _detail(rec) -> dict:
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


@pytest.fixture
def me(cfg):
    cfg.connectors = {"email": {"addresses": [ME]}}
    return cfg


# ---------------------------------------------------------------- field mapping

ADVICE = (
    "Alex,\n\nMy one piece of advice: lead with the customer story, not the TAM slide.\n\n"
    "Priya\n\n-- \nPriya Natarajan\nVP Product, Acme Robotics\n\n"
    "On Mon, Sep 21, 2026 at 5:40 PM Alex Rivera <alex.rivera@example.com>\nwrote:\n\n"
    "> Hi Priya,\n>\n> Draft v3 attached.\n")


def test_a_takeout_message_maps_every_field(me, tmp_path):
    advice = mail(subject="=?UTF-8?Q?Re:_Q4_deck_=E2=80=94_draft?=", body=ADVICE,
                  sender=f"=?utf-8?q?Priya_Natarajan?= <{PRIYA.upper()}>",
                  to=f"Alex Rivera <{ME}>", thrid="1781234567890123456",
                  labels="Inbox,Important,Opened,Category Personal",
                  headers=("Cc: Product Team <product-team@acme-robotics.example>, "
                           f"Jake <{JAKE}>",
                           "In-Reply-To: <draft@example.com>",
                           "References: <root@example.com> <draft@example.com>",
                           f"Delivered-To: {ME}"))
    scan = em.scan(takeout(tmp_path, advice), me)
    assert scan["connector"] == "email" and scan["account"] == ME
    [rec] = scan["records"]
    assert rec["stream"] == "email"
    assert rec["external_id"] == "advice@acme-robotics.example"
    assert rec["title"] == "Re: Q4 deck \N{EM DASH} draft"
    assert rec["body"] == ("Alex,\n\nMy one piece of advice: lead with the customer story, "
                           "not the TAM slide.\n\nPriya")
    assert rec["started_at"] == "2026-09-22T13:47:00Z"
    assert rec["utc_offset_sec"] == -5 * 3600
    assert rec["journal_date"] == "2026-09-22"
    assert rec["direction"] == "in"
    assert rec["value"] is None and rec["unit"] is None and rec["media_path"] is None
    assert rec["origin_app"] == "gmail"
    assert rec["last_modified_at"] == "2026-10-20T12:00:00Z"     # the Takeout's own time
    assert rec["participants"] == [
        {"kind": "email", "value": PRIYA, "role": "sender"},
        {"kind": "email", "value": ME, "role": "recipient"},
        {"kind": "email", "value": "product-team@acme-robotics.example", "role": "recipient"},
        {"kind": "email", "value": JAKE, "role": "recipient"}]
    d = _detail(rec)
    # Gmail's read state (Opened) is not a label worth keeping.
    assert d["labels"] == ["Category Personal", "Important", "Inbox"]
    assert d["thread"] == "1781234567890123456"
    assert d["in_reply_to"] == "draft@example.com"
    assert d["cc_count"] == 2
    assert d["trimmed"] is True
    assert d["text_length"] == len(ADVICE.strip())
    assert "attachments" not in d and "mailbox" not in d


def test_attachments_are_described_never_kept(me, tmp_path):
    payload = b"%PDF-1.4\n" + b"x" * 2000
    body = ("--B\nContent-Type: text/plain; charset=utf-8\n\nDeck attached.\n"
            "--B\nContent-Type: application/pdf; name=\"deck.pdf\"\n"
            "Content-Disposition: attachment; filename*=utf-8''Q4%20Deck%20v3.pdf\n"
            "Content-Transfer-Encoding: base64\n\n"
            + base64.encodebytes(payload).decode() +
            "--B\nContent-Type: message/rfc822\nContent-Disposition: attachment\n\n"
            "Subject: Old thread\nFrom: x@example.com\n\nold text\n"
            "--B--\n")
    scan = em.scan(takeout(tmp_path, sent(subject="Deck", body=body,
                                          ctype='multipart/mixed; boundary="B"')), me)
    [rec] = scan["records"]
    assert rec["body"] == "Deck attached."
    pdf, forwarded = _detail(rec)["attachments"]
    assert pdf == {"name": "Q4 Deck v3.pdf", "mime": "application/pdf", "size": len(payload)}
    # An attached message is described, never read as this message's text.
    assert forwarded["name"] == "Old thread.eml" and forwarded["mime"] == "message/rfc822"
    assert forwarded["size"] > 0
    assert "old text" not in (rec["body"] or "")
    assert "%PDF" not in rec["value_json"]
    assert scan["facts"]["attachments"] == 2


def test_a_calendar_invitation_is_kept_as_an_email(me, tmp_path):
    body = ("--A\nContent-Type: text/plain; charset=utf-8\n\nBoard meeting: Q4 deck\n"
            "--A\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\n\n"
            "BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR\n--A--\n")
    scan = em.scan(takeout(tmp_path, mail(subject="Invitation: Board meeting", body=body,
                                          ctype='multipart/alternative; boundary="A"')), me)
    [rec] = scan["records"]
    assert rec["body"] == "Board meeting: Q4 deck"
    assert _detail(rec)["invite"] is True
    assert scan["facts"]["invites"] == 1


def test_html_only_mail_becomes_text(me, tmp_path):
    html = ("<html><head><title>x</title><style>p{color:red}</style>"
            "<script>var a = 1;</script></head><body><h1>Booking confirmed</h1>"
            "<p>Reference&nbsp;K7Q2LM &amp; total <b>USD 1,124.60</b></p>"
            "<table><tr><td>Out</td><td>AUS &rarr; LJU</td></tr></table>"
            "<div class=\"gmail_quote\"><div>On Mon, Sep 21, 2026 at 9:00 AM Alex "
            "&lt;alex.rivera@example.com&gt; wrote:<br></div>"
            "<blockquote>older text</blockquote></div></body></html>")
    scan = em.scan(takeout(tmp_path, mail(body=html, ctype="text/html; charset=utf-8")), me)
    [rec] = scan["records"]
    # Entities decoded; a no-break space reads as a space, so search finds it.
    assert rec["body"] == ("Booking confirmed\nReference K7Q2LM & total USD 1,124.60\n"
                           "Out AUS \N{RIGHTWARDS ARROW} LJU")
    assert "var a" not in rec["body"] and "color" not in rec["body"]
    assert "older text" not in rec["body"]


# ---------------------------------------------------------------- trimming

@pytest.mark.parametrize("text,expected", [
    # Gmail, the attribution wrapped onto a second line
    ("Sounds good.\n\nOn Mon, Sep 21, 2026 at 5:40 PM Priya Natarajan <\n"
     "priya@acme-robotics.example> wrote:\n\n> the deck\n> is due\n", "Sounds good."),
    # Apple Mail / iPhone: the attribution itself quoted, a mobile signature
    ("Will do!\n\nSent from my iPhone\n\n> On Sep 21, 2026, at 17:40, Priya "
     "<priya@x.example> wrote:\n> \n> the deck\n", "Will do!"),
    # Outlook: the original message below a separator
    ("Confirmed.\n\n-----Original Message-----\nFrom: Ellis Dental\nSent: Monday\n\n"
     "Reminder", "Confirmed."),
    # Outlook on the web: a From:/Sent: block, no separator
    ("Thanks!\n\nFrom: Priya Natarajan <priya@x.example>\nSent: Monday, September 21, "
     "2026 5:40 PM\nTo: Alex\nSubject: Deck\n\nOriginal", "Thanks!"),
    # an inline reply keeps what was written between the quoted lines
    ("On Mon, Sep 21, 2026 at 5:40 PM Priya <priya@x.example> wrote:\n> Thursday?\n"
     "Yes, Thursday works.\n> And the room?\nThe big one.\n",
     "Yes, Thursday works.\nThe big one."),
    # a '-- ' signature, and a German attribution
    ("Danke!\n\n-- \nAlex Rivera\n+1 512\n", "Danke!"),
    ("Gut.\n\nAm 21.09.2026 um 17:40 schrieb Priya <priya@x.example>:\n> Text\n", "Gut."),
    # Thunderbird without a date, capitalised by some clients
    ("OK.\n\nPriya Natarajan Wrote:\n> text\n", "OK."),
    # someone quoting a writer, with no quote under it, is not a reply
    ("Here is what she wrote:\nthe plan stands", "Here is what she wrote:\nthe plan stands"),
])
def test_quoted_replies_and_signatures_are_trimmed(text, expected):
    assert em.trim(text, "Re: Deck") == expected


def test_a_forward_keeps_what_was_forwarded():
    gmail = ("FYI, see below.\n\n---------- Forwarded message ---------\nFrom: Priya "
             "<priya@x.example>\nDate: Mon, Sep 21, 2026\nSubject: Deck\n\nLead with "
             "the customer story.\n")
    assert em.trim(gmail, "Fwd: Deck").endswith("Lead with the customer story.")
    assert em.trim(gmail, "Fwd: Deck").startswith("FYI, see below.")
    outlook = ("See Priya's note.\n\nFrom: Priya <priya@x.example>\nSent: Monday\n"
               "Subject: Deck\n\nLead with the customer story.")
    assert em.trim(outlook, "FW: Deck").endswith("Lead with the customer story.")
    assert em.trim(outlook, "Re: Deck") == "See Priya's note."


def test_a_message_that_is_only_a_quote_has_no_body(me, tmp_path):
    body = "On Mon, Sep 21, 2026 at 5:40 PM Priya <priya@x.example> wrote:\n> the deck\n"
    scan = em.scan(takeout(tmp_path, mail(body=body)), me)
    [rec] = scan["records"]
    assert rec["body"] is None and rec["title"] == "Deck"
    assert _detail(rec)["text_length"] == len(body.strip())


def test_a_very_long_body_is_capped_and_says_so(me, tmp_path):
    scan = em.scan(takeout(tmp_path, mail(body="word " * 20_000)), me)
    [rec] = scan["records"]
    assert len(rec["body"]) == em.BODY_CAP
    assert _detail(rec)["body_truncated"] is True


# ---------------------------------------------------------------- time

def test_dates_fall_back_to_received_then_the_from_line(me, tmp_path):
    no_date = mail(date=None, msgid="<a@x>", headers=(
        "Received: from mx.example by mx.google.com; Wed, 23 Sep 2026 10:00:00 +0200",))
    nothing = mail(date=None, msgid="<b@x>")
    text = mbox(no_date) + mbox(nothing, sep="From x@x Thu Sep 24 07:30:00 2026")
    p = tmp_path / "Inbox.mbox"
    p.write_text(text, encoding="utf-8")
    recs = _by_id(em.scan(p, me))
    assert recs["a@x"]["started_at"] == "2026-09-23T08:00:00Z"
    assert recs["a@x"]["utc_offset_sec"] == 7200
    # A From_ line without a zone (Thunderbird) is read as UTC, the offset unknown.
    assert recs["b@x"]["started_at"] == "2026-09-24T07:30:00Z"
    assert recs["b@x"]["utc_offset_sec"] is None


def test_a_message_with_no_date_anywhere_is_counted_not_imported(me, tmp_path):
    p = tmp_path / "one.eml"
    p.write_text(mail(date=None), encoding="utf-8")
    scan = em.scan(p, me)
    assert scan["records"] == []
    assert scan["unmapped"][0]["what"].startswith("message with no readable date")


def test_journal_dates_follow_the_home_zone_and_the_rollover(me, tmp_path):
    late = mail(date="Wed, 23 Sep 2026 02:30:00 -0500", msgid="<late@x>")      # 02:30 CDT
    early = mail(date="Wed, 23 Sep 2026 12:30:00 +0200", msgid="<abroad@x>")   # 05:30 CDT
    utc = mail(date="Wed, 23 Sep 2026 09:00:00 -0000", msgid="<utc@x>")
    recs = _by_id(em.scan(takeout(tmp_path, late, early, utc), me))
    assert recs["late@x"]["journal_date"] == "2026-09-22"          # before 05:00: yesterday
    assert recs["abroad@x"]["journal_date"] == "2026-09-23"
    assert recs["abroad@x"]["utc_offset_sec"] == 7200              # the sender's own zone
    assert recs["utc@x"]["utc_offset_sec"] is None                 # '-0000': zone unknown


# ---------------------------------------------------------------- handles

def test_addresses_are_handles_and_names_only_display_names(me, tmp_path):
    a = mail(msgid="<1@x>", sender=f'"Natarajan, Priya" <{PRIYA}>')
    b = mail(msgid="<2@x>", sender=f"Priya Natarajan <{PRIYA}>")
    c = mail(msgid="<3@x>", sender=f"Priya Natarajan <{PRIYA}>",
             to=f"undisclosed-recipients:;, MAILER-DAEMON, {ME}")
    scan = em.scan(takeout(tmp_path, a, b, c), me)
    handles = {h["value"]: h for h in scan["handles"]}
    assert set(handles) == {PRIYA, ME}
    assert all(h["kind"] == "email" for h in scan["handles"])
    assert handles[PRIYA]["display_name"] == "Priya Natarajan"      # the most frequent
    assert handles[ME]["display_name"] == "Alex Rivera"
    for rec in scan["records"]:
        for p in rec["participants"]:
            assert p["value"] in handles and "Priya" not in p["value"]
    bad = next(u for u in scan["unmapped"] if u["what"].startswith("address that is not"))
    assert bad["count"] == 1                                        # MAILER-DAEMON


def test_an_8bit_header_reads_as_utf8(me, tmp_path):
    raw = mail(subject="Café plans", sender="Tía Rosa <rosa@example.com>")
    p = tmp_path / "m.eml"
    p.write_bytes(raw.encode("utf-8"))
    scan = em.scan(p, me)
    assert scan["records"][0]["title"] == "Café plans"
    assert scan["handles"][1] == {"kind": "email", "value": "rosa@example.com",
                                  "display_name": "Tía Rosa"}


# ---------------------------------------------------------------- who is you

def test_owner_from_config_addresses_sets_direction(me, tmp_path):
    scan = em.scan(takeout(tmp_path, mail(), sent()), me)
    by = {r["external_id"]: r["direction"] for r in scan["records"]}
    assert by == {"advice@acme-robotics.example": "in", "reply@example.com": "out"}
    assert scan["facts"]["you_from"] == "[connectors.email] addresses"


def test_owner_from_the_owner_table(cfg, tmp_path):
    cfg.extra = {"owner": {"handles": [f"email:{ME}", "phone:+15125550100"]}}
    scan = em.scan(takeout(tmp_path, mail(), sent(labels="Inbox")), cfg)
    assert scan["account"] == ME
    assert scan["facts"]["you_from"] == "[owner] handles in config.toml"
    assert {r["direction"] for r in scan["records"]} == {"in", "out"}


def test_owner_inferred_from_sent_mail_and_other_senders_reported(cfg, tmp_path):
    alias = sent(msgid="<alias@x>", labels="Sent").replace(
        f"From: Alex Rivera <{ME}>", "From: Alex <alex@work.example>")
    scan = em.scan(takeout(tmp_path, mail(), sent(), sent(msgid="<r2@x>"), alias), cfg)
    assert scan["account"] == ME
    assert scan["facts"]["you_from"].startswith("inferred: the most frequent sender")
    assert scan["facts"]["also_sent_as"] == [{"address": "alex@work.example", "messages": 1}]
    report = "\n".join(em.report_lines(scan, {"new": scan["records"], "updates": [],
                                              "identical": 0, "refreshed": [],
                                              "dup_incoming": 0, "conflicts": [],
                                              "invalid": []}))
    assert "also sent from: alex@work.example (1)" in report


def test_owner_inferred_from_delivered_to_when_nothing_is_sent(cfg, tmp_path):
    scan = em.scan(takeout(tmp_path, mail(headers=(f"Delivered-To: {ME}",))), cfg)
    assert scan["account"] == ME
    assert scan["records"][0]["direction"] == "in"


def test_without_an_owner_direction_is_unknown_and_the_report_warns(cfg, conn, tmp_path):
    scan = em.scan(takeout(tmp_path, mail()), cfg)
    assert scan["account"] is None
    assert scan["records"][0]["direction"] is None
    assert "WARNING nothing in this archive says which address is yours" in _report(conn, scan)


def test_mail_to_only_yourself_is_self(me, tmp_path):
    note = sent(subject="Flights", to=f"Alex Rivera <{ME}>", labels="Inbox,Sent")
    scan = em.scan(takeout(tmp_path, note), me)
    assert scan["records"][0]["direction"] == "self"


def test_a_bad_addresses_setting_is_one_line(cfg, tmp_path):
    cfg.connectors = {"email": {"addresses": ["not an address"]}}
    with pytest.raises(DomainError) as e:
        em.scan(takeout(tmp_path, mail()), cfg)
    assert "addresses" in str(e.value) and "\n" not in str(e.value)
    for bad in ({"since": "last year"}, {"include_lists": "yes"}, {"skip_labels": 3}):
        cfg.connectors = {"email": bad}
        with pytest.raises(DomainError) as e:
            em.scan(takeout(tmp_path, mail()), cfg)
        assert next(iter(bad)) in str(e.value)


# ---------------------------------------------------------------- what is skipped

NEWSLETTER = mail(subject="This week at Movement", msgid="<news@x>",
                  sender="Movement Gym <news@movementgym.example>",
                  headers=("List-Unsubscribe: <mailto:u@movementgym.example>",
                           "List-Id: Movement News <news.movementgym.example>",
                           "Precedence: bulk"), labels="Inbox,Category Promotions")


def test_spam_trash_and_bulk_mail_are_skipped_and_counted(me, tmp_path):
    spam = mail(msgid="<spam@x>", labels="Spam,Unread", subject="You won")
    trash = mail(msgid="<trash@x>", labels="Trash,Opened")
    bounce = mail(msgid="<bounce@x>", ctype='multipart/report; report-type=delivery-status; '
                  'boundary="R"', body="--R\n\nfailed\n--R--")
    scan = em.scan(takeout(tmp_path, mail(), spam, trash, NEWSLETTER, bounce), me)
    assert [r["external_id"] for r in scan["records"]] == ["advice@acme-robotics.example"]
    assert sorted(scan["skipped"]) == sorted([
        "1 × delivery report (a bounce or a read receipt)",
        "1 × mailing-list or bulk mail (List-Unsubscribe or Precedence: bulk; "
        "include_lists = true keeps it)",
        "1 × message labelled Spam (skip_labels)",
        "1 × message labelled Trash (skip_labels)"])
    assert scan["facts"]["messages_read"] == 5


def test_include_lists_keeps_the_newsletter_with_its_list_id(cfg, tmp_path):
    cfg.connectors = {"email": {"addresses": [ME], "include_lists": True}}
    [rec] = em.scan(takeout(tmp_path, NEWSLETTER), cfg)["records"]
    assert _detail(rec)["list_id"] == "news.movementgym.example"


def test_your_own_mail_to_a_list_is_never_skipped(me, tmp_path):
    mine = sent(msgid="<tolist@x>", headers=("List-Unsubscribe: <mailto:u@list.example>",))
    [rec] = em.scan(takeout(tmp_path, mine), me)["records"]
    assert rec["direction"] == "out"


def test_skip_labels_can_be_changed(cfg, tmp_path):
    cfg.connectors = {"email": {"addresses": [ME], "skip_labels": ["Trash"]}}
    spam = mail(msgid="<spam@x>", labels="Spam")
    assert [r["external_id"] for r in em.scan(takeout(tmp_path, spam), cfg)["records"]] \
        == ["spam@x"]


def test_since_imports_a_window_and_counts_the_rest(cfg, tmp_path):
    cfg.connectors = {"email": {"addresses": [ME], "since": "2026-01-01"}}
    old = mail(msgid="<old@x>", date="Mon, 7 Sep 2020 10:00:00 -0500")
    scan = em.scan(takeout(tmp_path, old, mail()), cfg)
    assert [r["external_id"] for r in scan["records"]] == ["advice@acme-robotics.example"]
    assert "1 × message before [connectors.email] since (2026-01-01)" in scan["skipped"]


def test_a_second_copy_of_a_message_is_read_once(me, tmp_path):
    folder = tmp_path / "export"
    for box in ("INBOX", "Sent Messages"):
        (folder / f"{box}.mbox").mkdir(parents=True)
        (folder / f"{box}.mbox" / "mbox").write_text(
            mbox(sent(to=f"Alex <{ME}>", labels=None)), encoding="utf-8")
    scan = em.scan(folder, me)
    assert len(scan["records"]) == 1
    assert "1 × second copy of a message already read (same Message-ID)" in scan["skipped"]


def test_a_message_without_a_message_id_gets_a_stable_hash(me, tmp_path):
    no_id = mail(msgid=None)
    one = em.scan(takeout(tmp_path / "a", no_id), me)["records"][0]["external_id"]
    two = em.scan(takeout(tmp_path / "b", no_id, crlf=False), me)["records"][0]["external_id"]
    assert one.startswith("sha1:") and one == two


# ---------------------------------------------------------------- the mbox reader

def test_the_mbox_splitter_reads_only_real_separators(me, tmp_path):
    body = ("First line.\n\nFrom the start, this was the plan.\n\n>From 1@xxx Tue Sep 22 "
            "13:47:00 +0000 2026 was escaped\nend")
    text = mbox(mail(body=body, msgid="<one@x>"), mail(msgid="<two@x>"))
    p = tmp_path / "x.mbox"
    p.write_bytes(text.encode("utf-8"))
    recs = _by_id(em.scan(p, me))
    assert set(recs) == {"one@x", "two@x"}
    assert "From the start, this was the plan." in recs["one@x"]["body"]
    assert "\nFrom 1@xxx Tue Sep 22 13:47:00 +0000 2026 was escaped" in recs["one@x"]["body"]


def test_a_separator_without_a_blank_line_before_it_still_splits(me, tmp_path):
    text = (f"From a@x Tue Sep 22 13:47:00 2026\n{mail(msgid='<a@x>')}"
            f"From b@x Tue Sep 22 14:00:00 +0000 2026\n{mail(msgid='<b@x>')}")
    p = tmp_path / "tight.mbox"
    p.write_bytes(text.encode("utf-8"))
    assert set(_by_id(em.scan(p, me))) == {"a@x", "b@x"}


def test_the_mbox_reader_streams(tmp_path):
    """It takes any line iterator — a file, a zip member — and yields one
    message at a time."""
    lines = iter(mbox(mail(msgid="<a@x>"), mail(msgid="<b@x>")).encode().splitlines(True))
    gen = em.mbox_messages(lines, "x")
    sep, first = next(gen)
    assert sep.startswith(b"From ") and b"<a@x>" in first and b"<b@x>" not in first
    assert b"<b@x>" in next(gen)[1]


def test_a_bom_before_the_first_from_line_is_tolerated(me, tmp_path):
    p = tmp_path / "bom.mbox"
    p.write_bytes(b"\xef\xbb\xbf" + mbox(mail()).encode("utf-8"))
    assert len(em.scan(p, me)["records"]) == 1
    q = tmp_path / "bom.eml"
    q.write_bytes(b"\xef\xbb\xbf" + mail().encode("utf-8"))
    assert em.scan(q, me)["records"][0]["title"] == "Deck"


# ---------------------------------------------------------------- other stores

def test_an_apple_mail_export_folder(cfg, tmp_path):
    folder = tmp_path / "Mail export"
    boxes = {"INBOX": [mail()], "Sent Messages": [sent(labels=None)],
             "Junk": [mail(msgid="<junk@x>", subject="Prize")]}
    for box, msgs in boxes.items():
        (folder / f"{box}.mbox").mkdir(parents=True)
        (folder / f"{box}.mbox" / "mbox").write_text(
            mbox(*msgs, sep=f"From {PRIYA} Tue Sep 22 08:47:00 2026"), encoding="utf-8")
        (folder / f"{box}.mbox" / "table_of_contents").write_bytes(b"\x00\x01\x02")
    scan = em.scan(folder, cfg)
    recs = _by_id(scan)
    assert set(recs) == {"advice@acme-robotics.example", "reply@example.com"}
    assert _detail(recs["reply@example.com"])["mailbox"] == "Sent Messages"
    assert recs["reply@example.com"]["origin_app"] == "apple_mail"
    # No configuration: the Sent Messages mailbox says who you are.
    assert scan["account"] == ME and recs["reply@example.com"]["direction"] == "out"
    assert "1 × message in the Junk mailbox (skip_labels)" in scan["skipped"]
    assert not any("table_of_contents" in s for s in scan["skipped"])


def emlx(message: str, flags: int = 0, received: int | None = None) -> bytes:
    raw = message.encode("utf-8")
    plist = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD '
             'PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
             '<plist version="1.0">\n<dict>\n'
             + (f"\t<key>date-received</key>\n\t<integer>{received}</integer>\n"
                if received else "")
             + f"\t<key>flags</key>\n\t<integer>{flags}</integer>\n</dict>\n</plist>\n")
    return f"{len(raw)}\n".encode() + raw + plist.encode()


def _store(tmp_path) -> Path:
    store = tmp_path / "Library" / "Mail"
    msgs = store / "V10" / "4F2C-UUID" / "INBOX.mbox" / "8A1B-UUID" / "Data" / "Messages"
    msgs.mkdir(parents=True)
    (msgs / "101.emlx").write_bytes(emlx(mail(), flags=1))
    (msgs / "102.emlx").write_bytes(emlx(mail(msgid="<gone@x>"), flags=0b11))   # deleted
    (msgs / "103.emlx").write_bytes(emlx(mail(msgid="<nodate@x>", date=None),
                                         received=1790000000))
    att = ("--P\nContent-Type: text/plain\n\nphotos\n--P\nContent-Type: image/jpeg; "
           "name=a.jpg\nContent-Disposition: attachment; filename=a.jpg\n\n\n--P--\n")
    (msgs / "104.partial.emlx").write_bytes(emlx(mail(msgid="<partial@x>", body=att,
                                                      ctype='multipart/mixed; boundary="P"')))
    (store / "V10" / "MailData").mkdir()
    (store / "V10" / "MailData" / "Envelope Index").write_bytes(b"SQLite format 3\x00")
    return store


def test_the_apple_mail_store_reads_emlx(me, tmp_path):
    scan = em.scan(_store(tmp_path), me)
    assert scan["target_kind"] == "apple_mail_store"
    recs = _by_id(scan)
    assert set(recs) == {"advice@acme-robotics.example", "nodate@x", "partial@x"}
    assert "1 × message marked deleted in Apple Mail" in scan["skipped"]
    # No Date, no Received: the plist's date-received.
    assert recs["nodate@x"]["started_at"] == datetime.fromtimestamp(
        1790000000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A partial .emlx keeps its attachments elsewhere: the size is not known here.
    assert _detail(recs["partial@x"])["attachments"] == [
        {"name": "a.jpg", "mime": "image/jpeg", "size": None}]
    assert {r["origin_app"] for r in scan["records"]} == {"apple_mail"}
    assert _detail(recs["partial@x"])["mailbox"] == "INBOX"


def test_a_thunderbird_profile_folder(me, tmp_path):
    mail_dir = tmp_path / "Mail" / "Local Folders"
    (mail_dir / "Archives.sbd").mkdir(parents=True)
    tb = mail(headers=("X-Mozilla-Status: 0001",))
    (mail_dir / "Inbox").write_text(mbox(tb, sep="From - Tue Sep 22 13:47:00 2026"),
                                    encoding="utf-8")
    (mail_dir / "Inbox.msf").write_text("// mork", encoding="utf-8")
    (mail_dir / "Archives.sbd" / "2026").write_text(
        mbox(mail(msgid="<arch@x>"), sep="From - Tue Sep 22 13:47:00 2026"),
        encoding="utf-8")
    (mail_dir / "Archives.sbd" / "2026.msf").write_text("", encoding="utf-8")
    (mail_dir / "filterlog.html").write_text("", encoding="utf-8")
    scan = em.scan(tmp_path / "Mail", me)
    recs = _by_id(scan)
    assert _detail(recs["arch@x"])["mailbox"] == "Archives/2026"
    assert recs["advice@acme-robotics.example"]["origin_app"] == "thunderbird"
    assert "1 × file that is not a mail message (e.g. Local Folders/filterlog.html)" \
        in scan["skipped"]


def test_a_folder_of_eml_files_with_one_broken_file(me, tmp_path):
    folder = tmp_path / "saved"
    folder.mkdir()
    (folder / "advice.eml").write_text(mail(), encoding="utf-8")
    (folder / "reply.eml").write_text(sent(), encoding="utf-8")
    (folder / "broken.emlx").write_bytes(b"not an emlx at all")
    (folder / "notes.pdf").write_bytes(b"%PDF")
    scan = em.scan(folder, me)
    assert len(scan["records"]) == 2
    broken = next(u for u in scan["unmapped"] if u["what"] == "mail file that could not be read")
    assert "broken.emlx is not an .emlx message" in broken["example"]
    assert "1 × file that is not a mail message (e.g. notes.pdf)" in scan["skipped"]


# ---------------------------------------------------------------- bad input

@pytest.mark.parametrize("name,data,says", [
    ("deck.pdf", b"%PDF-1.4\x00\x01", "is not a mail file"),
    ("empty.mbox", b"", "is empty"),
    ("x.mbox", b"Subject: no separator\n\nhi\n", "is not an mbox mailbox"),
    ("x.emlx", b"abc\nSubject: hi\n", "is not an .emlx message"),
])
def test_bad_input_fails_with_one_line_naming_what_it_saw(me, tmp_path, name, data, says):
    p = tmp_path / name
    p.write_bytes(data)
    with pytest.raises(DomainError) as e:
        em.scan(p, me)
    assert says in str(e.value) and "\n" not in str(e.value)


def test_a_zip_or_folder_without_mail_says_what_it_holds(me, tmp_path):
    z = tmp_path / "takeout-x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Keep/note.json", "{}")
    with pytest.raises(DomainError) as e:
        em.scan(z, me)
    assert "holds no mail" in str(e.value) and "Takeout/Keep/note.json" in str(e.value)
    (tmp_path / "empty").mkdir()
    with pytest.raises(DomainError) as e:
        em.scan(tmp_path / "empty", me)
    assert "(has: (empty))" in str(e.value)


def test_a_denied_mail_store_asks_for_full_disk_access(me, tmp_path, monkeypatch):
    store = _store(tmp_path)

    def denied(root):
        raise PermissionError(1, "Operation not permitted", str(root / "V10"))

    monkeypatch.setattr(em, "_walk", denied)
    with pytest.raises(DomainError) as e:
        em.scan(store, me)
    msg = str(e.value)
    assert "macOS did not allow reading" in msg and "Full Disk Access" in msg
    assert "docs/email.md" in msg and "\n" not in msg


# ---------------------------------------------------------------- determinism & report

def test_two_scans_are_equal_and_a_change_changes_the_hash(me, tmp_path):
    p = takeout(tmp_path / "a", mail(), sent(), NEWSLETTER)
    one, two = em.scan(p, me), em.scan(p, me)
    assert one == two
    q = takeout(tmp_path / "b", mail(), sent(body="Thanks again!"), NEWSLETTER)
    assert em.scan(q, me)["source_hash"] != one["source_hash"]


def test_report_lines_name_the_stream_the_span_and_the_skips(me, conn, tmp_path):
    scan = em.scan(takeout(tmp_path, mail(), sent(), NEWSLETTER), me)
    text = _report(conn, scan)
    assert "email: 2 new, 0 changed | 2026-09-22..2026-09-22" in text
    assert "mailbox All mail Including Spam and Trash: 2 kept, 1 skipped" in text
    assert "direction: 1 in, 1 out" in text
    assert f"you: {ME} ([connectors.email] addresses)" in text
    assert "handles: 1 email address(es)" in text
    assert "skipped: 1 × mailing-list or bulk mail" in text


# ---------------------------------------------------------------- the pipeline

def test_apply_then_reapply_is_all_identical(me, conn, tmp_path):
    p = takeout(tmp_path, mail(), sent())
    _, result = _apply(conn, em.scan(p, me))
    assert result["added"] == 2
    plan = obs.build_plan(conn, em.scan(p, me))
    assert plan["new"] == [] and plan["updates"] == [] and plan["conflicts"] == []
    assert plan["identical"] == 2
    assert "nothing new — the archive matches the database" in _report(conn, em.scan(p, me))


def test_a_newer_export_updates_labels_and_an_older_one_conflicts(me, conn, tmp_path):
    first = takeout(tmp_path / "1", mail(labels="Inbox,Opened"),
                    name="takeout-20261020T120000Z-001.zip")
    _apply(conn, em.scan(first, me))
    newer = takeout(tmp_path / "2", mail(labels="Archived,Starred"),
                    name="takeout-20261101T120000Z-001.zip")
    plan, _ = _apply(conn, em.scan(newer, me))
    assert len(plan["updates"]) == 1
    row = conn.execute("SELECT value_json FROM observations").fetchone()
    assert json.loads(row["value_json"])["labels"] == ["Archived", "Starred"]
    older = takeout(tmp_path / "3", mail(labels="Inbox"), name="takeout-20260901T120000Z-001.zip")
    plan = obs.build_plan(conn, em.scan(older, me))
    assert len(plan["conflicts"]) == 1 and plan["updates"] == []
    # The same messages again, only the export is newer: a silent refresh.
    again = takeout(tmp_path / "4", mail(labels="Archived,Starred"),
                    name="takeout-20261201T120000Z-001.zip")
    plan = obs.build_plan(conn, em.scan(again, me))
    assert len(plan["refreshed"]) == 1 and plan["updates"] == []


def test_the_archive_remembers_what_a_later_export_lacks(me, conn, tmp_path):
    _apply(conn, em.scan(takeout(tmp_path / "1", mail(), sent()), me))
    scan = em.scan(takeout(tmp_path / "2", mail(), name="takeout-20261101T120000Z-001.zip"),
                   me)
    assert "present_ids" not in scan
    plan, _ = _apply(conn, scan)
    assert plan.get("removed", []) == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 2


def test_the_confirmed_tables_stay_empty(me, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    _apply(conn, em.scan(takeout(tmp_path, mail(), sent(), NEWSLETTER), me))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    assert conn.execute("SELECT COUNT(*) c FROM entity_handles WHERE entity_id IS NOT NULL"
                        ).fetchone()["c"] == 0


def test_doctor_stays_green_after_an_import_and_its_retraction(me, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, me)
    assert run_checks(conn, me)["failures"] == 0, "seed itself is not green"
    _, result = _apply(conn, em.scan(takeout(tmp_path, mail(), sent()), me))
    report = run_checks(conn, me)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    obs.retract_import(conn, result["import_uuid"])
    conn.commit()
    assert run_checks(conn, me)["failures"] == 0


# ---------------------------------------------------------------- default target

def test_default_target_prefers_the_configured_path(cfg, tmp_path):
    p = takeout(tmp_path / "mail", mail())
    cfg.connectors = {"email": {"path": str(p)}}
    assert em.default_target(cfg) == p
    cfg.connectors = {"email": {"path": str(tmp_path / "missing.zip")}}
    with pytest.raises(DomainError) as e:
        em.default_target(cfg)
    assert "does not exist" in str(e.value)


def test_default_target_finds_the_newest_takeout_with_mail(cfg, tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    old = takeout(downloads, mail(), name="takeout-20260101T000000Z-001.zip")
    new = takeout(downloads, mail(), name="takeout-20261001T000000Z-001.zip")
    keep_only = downloads / "takeout-20261015T000000Z-001.zip"
    with zipfile.ZipFile(keep_only, "w") as zf:
        zf.writestr("Takeout/Keep/a.json", "{}")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    os.utime(keep_only, (3_000_000, 3_000_000))
    monkeypatch.setattr(em, "downloads_dir", lambda: downloads)
    monkeypatch.setattr(em.platforms, "is_mac", lambda: False)
    assert em.default_target(cfg) == new


def test_default_target_on_a_mac_is_the_mail_store(cfg, tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(em, "downloads_dir", lambda: tmp_path / "Downloads")
    monkeypatch.setattr(em, "mail_store", lambda: store)
    monkeypatch.setattr(em.platforms, "is_mac", lambda: True)
    assert em.default_target(cfg) == store
    monkeypatch.setattr(em.platforms, "is_mac", lambda: False)
    with pytest.raises(DomainError) as e:
        em.default_target(cfg)
    assert "no mail archive found" in str(e.value) and "\n" not in str(e.value)


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def test_cli_dry_run_apply_and_reimport(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = takeout(tmp_path / "exp", mail(), sent(), NEWSLETTER)
    r = runner.invoke(app, ["observe", "email", str(export)])
    assert r.exit_code == 0, r.output
    assert "email: 2 new" in r.output and "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0

    r = runner.invoke(app, ["observe", "email", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 2 added" in r.output

    r = runner.invoke(app, ["observe", "email", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "0 added" in r.output and "2 identical skipped" in r.output
    r = runner.invoke(app, ["observe", "email", str(export)])
    assert "already imported" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_a_bad_file_is_one_error_line(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = tmp_path / "deck.pdf"
    p.write_bytes(b"%PDF-1.4")
    r = runner.invoke(app, ["observe", "email", str(p)])
    assert r.exit_code == 1
    assert "is not a mail file" in r.output and "Traceback" not in r.output


def test_cli_connectors_lists_email(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "email" in r.output
