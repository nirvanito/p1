"""Finance connector: OFX, QFX and QBO statements.

OFX is the one statement format with a real transaction id (FITID), and it
comes in two dialects banks still ship side by side: v1 SGML, whose leaf
elements are never closed, and v2 XML. These exports are built by hand to
exercise what real files do — empty leaves, leaves closed in some places
and not others, PAYEE aggregates instead of NAME, foreign currency,
corrections, pending lists, investment statements, Windows-1252 bytes.
"""

import json

import pytest

from lifecrm.connectors import _ofx, finance
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

V1_HEADER = ("OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:102\r\nSECURITY:NONE\r\n"
             "ENCODING:USASCII\r\nCHARSET:1252\r\nCOMPRESSION:NONE\r\n"
             "OLDFILEUID:NONE\r\nNEWFILEUID:NONE\r\n\r\n")

SIGNON = ("<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
          "<DTSERVER>20261021120000[-5:CDT]<LANGUAGE>ENG<FI><ORG>HCCU<FID>7311</FI>"
          "<INTU.BID>7311</SONRS></SIGNONMSGSRSV1>\r\n")

CHECKING_TXNS = (
    "<STMTTRN><TRNTYPE>DIRECTDEP<DTPOSTED>20261007120000.000[-5:CDT]<TRNAMT>4200.00"
    "<FITID>202610070001<NAME>ACME ROBOTICS PAYROLL<MEMO>DIRECT DEPOSIT</STMTTRN>\r\n"
    "<STMTTRN><TRNTYPE>POS<DTPOSTED>20261021<DTUSER>20261020<TRNAMT>-29.75"
    "<FITID>202610210002<NAME>VERACRUZ ALL NATURAL<MEMO>\r\n</STMTTRN>\r\n"
    "<STMTTRN><TRNTYPE>CHECK<DTPOSTED>20261005<TRNAMT>-150.00<FITID>202610050003"
    "<CHECKNUM>1043<NAME>CHECK 1043</STMTTRN>\r\n"
    "<STMTTRN><TRNTYPE>FEE<DTPOSTED>20261015<TRNAMT>-2.50<FITID>202610150004"
    "<NAME>H&amp;R FEE &amp; CO<SIC>6012</STMTTRN>\r\n")


def checking(txns=CHECKING_TXNS, currency="USD", extra=""):
    return ("<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            f"<STMTRS><CURDEF>{currency}<BANKACCTFROM><BANKID>999999999"
            "<ACCTID>000123458812<ACCTTYPE>CHECKING</BANKACCTFROM>\r\n"
            "<BANKTRANLIST><DTSTART>20261001<DTEND>20261021\r\n"
            f"{txns}</BANKTRANLIST>{extra}\r\n"
            "<LEDGERBAL><BALAMT>7020.17<DTASOF>20261021</LEDGERBAL>\r\n"
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\r\n")


def v1(body):
    return V1_HEADER + "<OFX>\r\n" + SIGNON + body + "</OFX>\r\n"


V2_CARD = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20261021120000</DTSERVER><LANGUAGE>ENG</LANGUAGE>
    <FI><ORG>Example Card</ORG><FID>1234</FID></FI></SONRS></SIGNONMSGSRSV1>
  <CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1</TRNUID>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <CCSTMTRS><CURDEF>USD</CURDEF>
      <CCACCTFROM><ACCTID>4111111111114417</ACCTID></CCACCTFROM>
      <BANKTRANLIST><DTSTART>20261001</DTSTART><DTEND>20261021</DTEND>
        <STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261021000000.000[-7:MST]</DTPOSTED>
          <DTUSER>20261020</DTUSER><TRNAMT>-29.75</TRNAMT>
          <FITID>2026102024692166293100123456789</FITID>
          <NAME>VERACRUZ ALL NATURAL</NAME><MEMO></MEMO></STMTTRN>
        <STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20261015</DTPOSTED>
          <TRNAMT>500.00</TRNAMT><FITID>20261015PAY</FITID>
          <PAYEE><NAME>PAYMENT THANK YOU</NAME><CITY>AUSTIN</CITY></PAYEE></STMTTRN>
      </BANKTRANLIST>
      <LEDGERBAL><BALAMT>-29.75</BALAMT><DTASOF>20261021</DTASOF></LEDGERBAL>
    </CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""


def write(tmp_path, name, text, encoding="utf-8", bom=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode(encoding))
    return p


def _by_title(scan):
    return {r["title"]: r for r in scan["records"]}


def _detail(rec):
    return json.loads(rec["value_json"])


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- v1 SGML

def test_v1_sgml_checking_statement(tmp_path, cfg):
    scan = finance.scan(write(tmp_path, "HCCU_Checking.qfx", v1(checking())), cfg)
    assert [f["format"] for f in scan["files"]] == ["OFX v1 from HCCU"]
    recs = _by_title(scan)
    pay = recs["ACME ROBOTICS PAYROLL"]
    assert pay["external_id"] == "ofx:hccu:8812:202610070001"
    assert pay["value"] == 4200.00 and pay["direction"] == "in"
    assert pay["body"] == "DIRECT DEPOSIT"
    assert _detail(pay)["type"] == "DIRECTDEP"
    assert pay["origin_app"] == "hccu"
    check = recs["CHECK 1043"]
    assert check["value"] == -150.00 and _detail(check)["check_no"] == "1043"
    fee = recs["H&R FEE & CO"]                           # entities decoded
    assert _detail(fee)["sic"] == "6012"


def test_an_empty_v1_leaf_does_not_swallow_what_follows(tmp_path, cfg):
    """`<MEMO>` with nothing after it is an empty leaf, not an aggregate."""
    rec = _by_title(finance.scan(write(tmp_path, "c.ofx", v1(checking())), cfg))[
        "VERACRUZ ALL NATURAL"]
    assert rec["body"] is None
    assert rec["value"] == -29.75


def test_dtuser_is_the_transaction_date_and_dtposted_the_posted_one(tmp_path, cfg):
    rec = _by_title(finance.scan(write(tmp_path, "c.ofx", v1(checking())), cfg))[
        "VERACRUZ ALL NATURAL"]
    assert rec["journal_date"] == "2026-10-20"
    assert rec["started_at"] == "2026-10-20T17:00:00Z"   # local noon, like CSVs
    assert _detail(rec)["posted_date"] == "2026-10-21"


def test_the_account_is_masked_to_its_last_four_everywhere(tmp_path, cfg):
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking())), cfg)
    assert scan["account"] == "hccu:8812"
    assert scan["handles"] == [{"kind": "card", "value": "hccu:8812",
                                "display_name": "HCCU checking ending 8812"}]
    assert "000123458812" not in json.dumps(scan, default=str)


def test_a_v1_file_that_closes_some_leaves_reads_the_same(tmp_path, cfg):
    closed = CHECKING_TXNS.replace("<NAME>ACME ROBOTICS PAYROLL",
                                   "<NAME>ACME ROBOTICS PAYROLL</NAME>") \
                          .replace("<TRNAMT>-150.00", "<TRNAMT>-150.00</TRNAMT>")
    a = finance.scan(write(tmp_path / "a", "c.ofx", v1(checking())), cfg)
    b = finance.scan(write(tmp_path / "b", "c.ofx", v1(checking(closed))), cfg)
    assert a["records"] == b["records"]


def test_an_unknown_unclosed_element_stays_a_leaf(tmp_path, cfg):
    odd = CHECKING_TXNS.replace("<TRNAMT>-150.00", "<BANKEXTRA>\r\n<TRNAMT>-150.00")
    rec = _by_title(finance.scan(write(tmp_path, "c.ofx", v1(checking(odd))), cfg))[
        "CHECK 1043"]
    assert rec["value"] == -150.00


def test_a_windows_1252_file_is_read(tmp_path, cfg):
    txns = CHECKING_TXNS.replace("CHECK 1043", "CAFÉ CRÈME")
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking(txns)), encoding="cp1252"), cfg)
    assert "CAFÉ CRÈME" in _by_title(scan)
    assert scan["files"][0]["encoding"] == "cp1252"


# ---------------------------------------------------------------- v2 XML

def test_v2_xml_card_statement(tmp_path, cfg):
    scan = finance.scan(write(tmp_path, "card.ofx", V2_CARD), cfg)
    assert [f["format"] for f in scan["files"]] == ["OFX v2 from Example Card"]
    recs = _by_title(scan)
    spend = recs["VERACRUZ ALL NATURAL"]
    assert spend["value"] == -29.75 and spend["direction"] == "out"
    assert spend["journal_date"] == "2026-10-20"
    assert _detail(spend)["posted_date"] == "2026-10-21"   # the zone suffix is ignored
    assert spend["body"] is None                           # <MEMO></MEMO>
    assert spend["external_id"] == \
        "ofx:example_card:4417:2026102024692166293100123456789"
    pay = recs["PAYMENT THANK YOU"]                        # PAYEE/NAME, not NAME
    assert pay["value"] == 500.00
    assert scan["handles"] == [{"kind": "card", "value": "example_card:4417",
                                "display_name": "Example Card card ending 4417"}]


def test_a_bom_before_the_xml_declaration_is_accepted(tmp_path, cfg):
    assert len(finance.scan(write(tmp_path, "card.qbo", V2_CARD, bom=True),
                            cfg)["records"]) == 2


def test_several_statements_in_one_file_keep_their_own_accounts(tmp_path, cfg):
    card = V2_CARD[V2_CARD.index("<CREDITCARDMSGSRSV1>"):V2_CARD.index("</OFX>")]
    scan = finance.scan(write(tmp_path, "both.ofx", v1(checking() + card)), cfg)
    assert scan["account"] == "hccu:4417, hccu:8812"
    assert [h["value"] for h in scan["handles"]] == ["hccu:4417", "hccu:8812"]
    assert _by_title(scan)["PAYMENT THANK YOU"]["participants"] == [
        {"kind": "card", "value": "hccu:4417", "role": "recipient"}]


# ---------------------------------------------------------------- currency

def test_the_statement_currency_is_the_unit(tmp_path, cfg):
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking(currency="EUR"))), cfg)
    assert {r["unit"] for r in scan["records"]} == {"eur"}


def test_foreign_currency_aggregates(tmp_path, cfg):
    """ORIGCURRENCY: the amount is in the account's currency and the original
    is noted. CURRENCY: the amount itself is in that currency."""
    txns = (
        "<STMTTRN><TRNTYPE>POS<DTPOSTED>20220926<TRNAMT>-642.00<FITID>T1"
        "<NAME>VILA BLED HOTEL<ORIGCURRENCY><CURRATE>1.0700<CURSYM>EUR</ORIGCURRENCY>"
        "</STMTTRN>\r\n"
        "<STMTTRN><TRNTYPE>POS<DTPOSTED>20220927<TRNAMT>-36.00<FITID>T2"
        "<NAME>PLETNA BLED<CURRENCY><CURRATE>1.0700<CURSYM>EUR</CURRENCY></STMTTRN>\r\n")
    recs = _by_title(finance.scan(write(tmp_path, "c.ofx", v1(checking(txns))), cfg))
    hotel = recs["VILA BLED HOTEL"]
    assert hotel["unit"] == "usd"
    assert _detail(hotel)["original_currency"] == "eur"
    assert _detail(hotel)["currency_rate"] == "1.0700"
    assert recs["PLETNA BLED"]["unit"] == "eur"


# ---------------------------------------------------------------- corrections, pending, investments

def test_a_delete_correction_is_reported_never_applied(tmp_path, cfg):
    txns = CHECKING_TXNS + (
        "<STMTTRN><TRNTYPE>FEE<DTPOSTED>20261016<TRNAMT>-2.50<FITID>202610160005"
        "<CORRECTFITID>202610150004<CORRECTACTION>DELETE<NAME>H&amp;R FEE</STMTTRN>\r\n"
        "<STMTTRN><TRNTYPE>POS<DTPOSTED>20261021<TRNAMT>-31.75<FITID>202610210006"
        "<CORRECTFITID>202610210002<CORRECTACTION>REPLACE<NAME>VERACRUZ ALL NATURAL TIP"
        "</STMTTRN>\r\n")
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking(txns))), cfg)
    recs = _by_title(scan)
    assert len(scan["records"]) == 5                       # the DELETE is not a row
    assert _detail(recs["VERACRUZ ALL NATURAL TIP"])["corrects"] == "202610210002"
    what = " | ".join(u["what"] for u in scan["unmapped"])
    assert "deleting an earlier transaction (not applied)" in what
    assert "replacing an earlier transaction" in what


def test_pending_transactions_and_investment_statements_are_counted(tmp_path, cfg):
    pending = ("<BANKTRANLISTP><DTASOF>20261021<STMTTRNP><TRNTYPE>POS"
               "<DTTRAN>20261021<TRNAMT>-212.40<NAME>UCHI AUSTIN</STMTTRNP>"
               "</BANKTRANLISTP>")
    invest = ("<INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>2<INVSTMTRS><DTASOF>20261021"
              "<CURDEF>USD</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>\r\n")
    scan = finance.scan(write(tmp_path, "c.ofx", v1(checking(extra=pending) + invest)), cfg)
    assert len(scan["records"]) == 4
    assert "UCHI AUSTIN" not in _by_title(scan)
    assert "1 × pending transaction (imported once it posts)" in scan["skipped"]
    assert any("investment statement" in u["what"] for u in scan["unmapped"])


def test_a_transaction_without_fitid_gets_a_stable_derived_id(tmp_path, cfg):
    txns = CHECKING_TXNS.replace("<FITID>202610050003", "")
    a = finance.scan(write(tmp_path / "a", "c.ofx", v1(checking(txns))), cfg)
    b = finance.scan(write(tmp_path / "b", "c.ofx", v1(checking(txns))), cfg)
    rec = _by_title(a)["CHECK 1043"]
    assert rec["external_id"].startswith("ofx:hccu:8812:")
    assert rec["external_id"] == _by_title(b)["CHECK 1043"]["external_id"]


# ---------------------------------------------------------------- bad input

def test_an_ofx_header_without_an_ofx_element_says_so(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        finance.scan(write(tmp_path, "broken.qfx", V1_HEADER + "nothing here\r\n"), cfg)
    assert "no <OFX> element" in str(e.value)
    assert "broken.qfx" in str(e.value)


def test_an_ofx_file_without_a_statement_says_so(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        finance.scan(write(tmp_path, "signon.ofx", v1("")), cfg)
    assert "no bank or card statement" in str(e.value)


def test_ofx_dates_keep_the_banks_date_and_drop_time_and_zone():
    assert _ofx.ofx_date("20261021000000.000[-7:MST]") == "2026-10-21"
    assert _ofx.ofx_date("20261021") == "2026-10-21"
    assert _ofx.ofx_date("") is None
    assert _ofx.ofx_date("Oct 21") is None


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical_and_doctor_is_green(tmp_path, cfg, conn):
    folder = tmp_path / "downloads"
    write(folder, "HCCU_Checking.qfx", v1(checking()))
    write(folder, "card.qbo", V2_CARD)
    _, first = _apply(conn, finance.scan(folder, cfg))
    assert first["added"] == 6
    plan, again = _apply(conn, finance.scan(folder, cfg))
    assert again["added"] == 0 and again["identical"] == 6
    assert plan["conflicts"] == []
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
