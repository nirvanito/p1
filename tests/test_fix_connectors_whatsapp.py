"""WhatsApp: a stranger's chosen name is not a contact's handle, and an
Android export named in the phone's language is still read as the chat it is.

- WhatsApp writes '~ Name' for a group member who is not in your contacts and
  chose that name themself. Stripping the '~' gave the stranger the handle of
  your contact with the same name — and one who called themself by your own
  name was recorded as you.
- Android names the export file in the phone's language ('WhatsApp Chat mit
  Max.txt'). Only the English name was recognised: such a zip was refused, a
  folder skipped the file silently, and the file itself was read as a group
  named 'WhatsApp Chat mit Max', with ids that a later English-named export
  would not match.
"""

import zipfile

import pytest

from lifecrm.connectors import whatsapp as wa
from lifecrm.domain import observations as obs

NNBSP = "\N{NARROW NO-BREAK SPACE}"

GROUP = (
    "20/08/2026, 18:45 - Linda Rivera: who is bringing the cake?\n"
    "20/08/2026, 18:46 - Sam: I will\n"
    f"20/08/2026, 18:47 - ~{NNBSP}Sam: I can bring candles\n"
    "20/08/2026, 18:48 - ~ Sam: and plates\n"
    "20/08/2026, 18:49 - Alex Rivera: thanks both\n"
    f"20/08/2026, 18:50 - ~{NNBSP}Alex Rivera: hi everyone, new here\n"
)

ONE_TO_ONE = (
    "27/07/2026, 21:30 - Max Mustermann: Treffen wir uns um 7?\n"
    "27/07/2026, 21:31 - Alex Rivera: ja, bis dann\n"
    "28/07/2026, 08:02 - Max Mustermann: guten Morgen\n"
)


@pytest.fixture
def me(cfg):
    cfg.connectors = {"whatsapp": {"me": "Alex Rivera"}}
    return cfg


def _write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _by_body(scan):
    return {r["body"]: r for r in scan["records"]}


def _sender(rec):
    return next(p["value"] for p in rec["participants"] if p["role"] == "sender")


# ---------------------------------------------------------------- the '~' marker

def test_a_strangers_chosen_name_is_not_the_contact_with_that_name(me, tmp_path):
    scan = wa.scan(_write(tmp_path / "WhatsApp Chat with Party.txt", GROUP), me)
    recs = _by_body(scan)
    assert _sender(recs["I will"]) == "whatsapp:Sam"
    assert _sender(recs["I can bring candles"]) == "whatsapp:~Sam"
    assert _sender(recs["and plates"]) == "whatsapp:~Sam"     # any space after '~'
    handles = {h["value"]: h["display_name"] for h in scan["handles"]}
    assert handles["whatsapp:Sam"] == "Sam"
    assert handles["whatsapp:~Sam"] == "Sam"                    # shown without it


def test_a_stranger_who_chose_your_name_is_not_you(me, tmp_path):
    scan = wa.scan(_write(tmp_path / "WhatsApp Chat with Party.txt", GROUP), me)
    recs = _by_body(scan)
    assert recs["thanks both"]["direction"] == "out"
    stranger = recs["hi everyone, new here"]
    assert stranger["direction"] == "in"
    assert _sender(stranger) == "whatsapp:~Alex Rivera"
    assert scan["account"] == "whatsapp:Alex Rivera"


def test_the_two_sams_are_two_handles_once_applied(me, conn, tmp_path):
    scan = wa.scan(_write(tmp_path / "WhatsApp Chat with Party.txt", GROUP), me)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    sent = {r["handle_value"]: r["n"] for r in conn.execute(
        "SELECT handle_value, COUNT(*) n FROM observation_participants "
        "WHERE role='sender' AND handle_value IN ('whatsapp:Sam', 'whatsapp:~Sam') "
        "GROUP BY handle_value")}
    assert sent == {"whatsapp:Sam": 1, "whatsapp:~Sam": 2}


def test_a_lone_tilde_is_not_a_sender(me):
    assert wa.clean_label(f"~{NNBSP}") == ""
    assert wa.clean_label("~ Tío Beto") == "~Tío Beto"
    assert wa.clean_label("Sam") == "Sam"


# ---------------------------------------------------------------- localized file names

@pytest.mark.parametrize("file_name", [
    "WhatsApp Chat mit Max Mustermann.txt",          # German
    "Chat de WhatsApp con Max Mustermann.txt",       # Spanish
    "Conversa do WhatsApp com Max Mustermann.txt",   # Portuguese
    "Discussion WhatsApp avec Max Mustermann.txt",   # French
    "WhatsApp-chat met Max Mustermann.txt",          # Dutch
    "WhatsApp Chat mit Max Mustermann (1).txt",      # a second download
])
def test_a_localized_android_file_name_still_names_the_chat(me, tmp_path, file_name):
    english = wa.scan(_write(tmp_path / "en" / "WhatsApp Chat with Max Mustermann.txt",
                             ONE_TO_ONE), me)
    local = wa.scan(_write(tmp_path / "local" / file_name, ONE_TO_ONE), me)
    chat = local["facts"]["chats"][0]
    assert chat["chat"] == "Max Mustermann" and chat["group"] is False
    assert local["facts"]["warnings"] == []
    # the same chat, whatever language the phone named it in
    assert [r["external_id"] for r in local["records"]] == \
        [r["external_id"] for r in english["records"]]


def test_a_localized_zip_is_read_not_refused(me, tmp_path):
    z = tmp_path / "WhatsApp Chat mit Max Mustermann.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("WhatsApp Chat mit Max Mustermann.txt", ONE_TO_ONE.encode("utf-8"))
        zf.writestr("IMG-20260727-WA0003.jpg", b"\xff\xd8")
    scan = wa.scan(z, me)
    assert scan["facts"]["chats"][0]["chat"] == "Max Mustermann"
    assert len(scan["records"]) == 3


def test_a_folder_reads_localized_exports_beside_english_ones(me, tmp_path):
    folder = tmp_path / "Downloads"
    _write(folder / "WhatsApp Chat with Jake Morales.txt",
           "27/07/2026, 21:30 - Jake Morales: Veracruz tonight?\n"
           "27/07/2026, 21:31 - Alex Rivera: always\n")
    _write(folder / "WhatsApp Chat mit Max Mustermann.txt", ONE_TO_ONE)
    _write(folder / "shopping list.txt", "milk\neggs\n")
    _write(folder / "whatsapp tips.txt", "Tip 1: mute busy groups\n")
    scan = wa.scan(folder, me)
    assert sorted(c["chat"] for c in scan["facts"]["chats"]) == ["Jake Morales",
                                                                  "Max Mustermann"]


def test_downloads_holding_only_a_localized_export_is_found(tmp_path):
    _write(tmp_path / "Downloads" / "Chat de WhatsApp con Max Mustermann.txt", ONE_TO_ONE)
    assert wa._has_exports(tmp_path / "Downloads")


def test_an_unknown_wording_is_read_by_its_contents_and_the_report_warns(me, conn, tmp_path):
    folder = tmp_path / "exports"
    _write(folder / "WhatsApp Unterhaltung Max Mustermann.txt", ONE_TO_ONE)
    scan = wa.scan(folder, me)
    assert scan["facts"]["chats"][0]["chat"] == "WhatsApp Unterhaltung Max Mustermann"
    assert any("is the file's own name" in w and "rename the file" in w
               for w in scan["facts"]["warnings"])
    text = "\n".join(wa.report_lines(scan, obs.build_plan(conn, scan)))
    assert "WARNING" in text and "rename the file" in text


def test_a_zip_whose_text_has_an_unknown_name_is_read_by_its_contents(me, tmp_path):
    z = tmp_path / "WhatsApp Chat mit Max Mustermann.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("chat.txt", ONE_TO_ONE.encode("utf-8"))
        zf.writestr("readme.txt", b"not a chat")
    scan = wa.scan(z, me)
    chat = scan["facts"]["chats"][0]
    assert chat["chat"] == "Max Mustermann"       # from the zip's own name
    assert scan["facts"]["warnings"] == []
