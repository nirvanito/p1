"""Finance connector: bank and card statement CSVs.

The connector is a pure reader, so most of this runs scan() against small
exports shaped like each bank's real download — header, quoting, sign
convention and quirks (Chase's trailing comma, Bank of America's summary
block, Wells Fargo's missing header, Citi's pending rows). The rest is the
part a statement importer can get quietly wrong: ids that must survive
overlapping exports, identical same-day rows that must stay two, and handles
that must not flood the review queue. OFX lives in test_finance_ofx.py.
"""

import json
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import base, finance
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

CHASE_CHECKING = (
    "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\r\n"
    'DEBIT,10/20/2026,"VERACRUZ ALL NATURAL     AUSTIN TX",-29.75,DEBIT_CARD,3150.67,,\r\n'
    'CREDIT,10/07/2026,"ACME ROBOTICS PAYROLL       PPD ID: 4567890123",4200.00,'
    "ACH_CREDIT,3180.42,,\r\n"
    "CHECK,10/05/2026,CHECK 1043,-150.00,CHECK_PAID,-1019.58,1043,\r\n")

CHASE_CARD = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "10/20/2026,10/21/2026,VERACRUZ ALL NATURAL,Food & Drink,Sale,-29.75,\n"
    "10/15/2026,10/15/2026,Payment Thank You-Mobile,,Payment,500.00,\n"
    "10/12/2026,10/13/2026,UCHI AUSTIN,Food & Drink,Return,12.00,birthday dinner\n")

APPLE_CARD = (
    '"Transaction Date","Clearing Date","Description","Merchant","Category","Type",'
    '"Amount (USD)","Purchased By"\n'
    '"10/20/2026","10/21/2026","VERACRUZ ALL NATURAL 1704 E CESAR CHAVEZ ST AUSTIN '
    '78702 TX USA","Veracruz All Natural","Restaurants","Purchase","29.75","Alex Rivera"\n'
    '"10/15/2026","10/15/2026","ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN '
    '8812","Ach Deposit Internet Transfer","Payment","Payment","-500.00","Alex Rivera"\n')

CAPITAL_ONE = (
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
    "2026-10-20,2026-10-21,5521,VERACRUZ ALL NATURAL,Dining,29.75,\n"
    "2026-10-15,2026-10-15,5521,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,500.00\n")

AMEX = (
    "Date,Description,Amount\n"
    "10/20/2026,VERACRUZ ALL NATURAL AUSTIN TX,29.75\n"
    "10/18/2026,AUTOPAY PAYMENT - THANK YOU,-500.00\n")

AMEX_DETAILED = (
    "Date,Description,Card Member,Account #,Amount,Extended Details,"
    "Appears On Your Statement As,Address,City/State,Zip Code,Country,Reference,Category\n"
    '10/20/2026,VERACRUZ ALL NATURAL,ALEX RIVERA,-41007,29.75,"VERACRUZ ALL NATURAL\n'
    'AUSTIN\nTX",VERACRUZ ALL NATURAL AUSTIN TX,1704 E CESAR CHAVEZ ST,"AUSTIN\nTX",'
    "78702,UNITED STATES,'320262930123456789',Restaurant-Restaurant\n"
    "10/18/2026,AUTOPAY PAYMENT - THANK YOU,ALEX RIVERA,-41007,-500.00,,,,,,,"
    "'320262910000000001',\n")

DISCOVER = (
    "Trans. Date,Post Date,Description,Amount,Category\n"
    "10/20/2026,10/20/2026,VERACRUZ ALL NATURAL AUSTIN TX,29.75,Restaurants\n"
    "10/18/2026,10/18/2026,INTERNET PAYMENT - THANK YOU,-500.00,Payments and Credits\n")

CITI = (
    "Status,Date,Description,Debit,Credit\n"
    "Cleared,10/20/2026,VERACRUZ ALL NATURAL AUSTIN TX,29.75,\n"
    "Cleared,10/18/2026,ONLINE PAYMENT THANK YOU,,-500.00\n"
    "Pending,10/21/2026,UCHI AUSTIN,212.40,\n")

BOFA_CHECKING = (
    "Description,,Summary Amt.\n"
    'Beginning balance as of 10/01/2026,,"3,180.42"\n'
    'Total credits,,"4,200.00"\n'
    'Total debits,,"-29.75"\n'
    'Ending balance as of 10/21/2026,,"7,350.67"\n'
    "\n"
    "Date,Description,Amount,Running Bal.\n"
    '10/01/2026,Beginning balance as of 10/01/2026,,"3,180.42"\n'
    '10/07/2026,"ACME ROBOTICS DES:PAYROLL ID:XXXXX4567 INDN:ALEX RIVERA PPD",'
    '"4,200.00","7,380.42"\n'
    '10/20/2026,"VERACRUZ ALL NATURAL 10/19 PURCHASE AUSTIN TX","-29.75","7,350.67"\n')

BOFA_CARD = (
    "Posted Date,Reference Number,Payee,Address,Amount\n"
    '10/21/2026,24692166293100123456789,"VERACRUZ ALL NATURAL AUSTIN TX","AUSTIN TX ",-29.75\n'
    '10/15/2026,24692166288100123456780,"PAYMENT - THANK YOU","",500.00\n')

WELLS_FARGO = (
    '"10/20/2026","-29.75","*","","PURCHASE AUTHORIZED ON 10/19 VERACRUZ ALL NATURAL '
    'AUSTIN TX S586293123456789 CARD 1234"\n'
    '"10/07/2026","4200.00","*","","ACME ROBOTICS PAYROLL 261007 XXXXX4567 ALEX RIVERA"\n'
    '"10/05/2026","-150.00","*","1043","CHECK # 1043"\n')

# (file name, content, format label, money-in row's title prefix, money in)
PRESETS = [
    ("Chase8812_Activity_20261021.CSV", CHASE_CHECKING, "Chase checking",
     "ACME ROBOTICS PAYROLL", 4200.00),
    ("Chase4417_Activity20260910_20261021_20261021.CSV", CHASE_CARD, "Chase card",
     "Payment Thank You", 500.00),
    ("Apple Card Transactions - October 2026.csv", APPLE_CARD, "Apple Card",
     "Ach Deposit", 500.00),
    ("transactions.csv", CAPITAL_ONE, "Capital One card", "CAPITAL ONE AUTOPAY", 500.00),
    ("activity.csv", AMEX, "American Express card", "AUTOPAY PAYMENT", 500.00),
    ("activity-detailed.csv", AMEX_DETAILED, "American Express card",
     "AUTOPAY PAYMENT", 500.00),
    ("Discover-RecentActivity-20261021.csv", DISCOVER, "Discover card",
     "INTERNET PAYMENT", 500.00),
    ("Citi-export.csv", CITI, "Citi card", "ONLINE PAYMENT", 500.00),
    ("stmt.csv", BOFA_CHECKING, "Bank of America checking", "ACME ROBOTICS", 4200.00),
    ("October2026_6011.csv", BOFA_CARD, "Bank of America card", "PAYMENT", 500.00),
    ("Checking1.csv", WELLS_FARGO, "Wells Fargo", "ACME ROBOTICS PAYROLL", 4200.00),
]


def export(tmp_path, name, text, encoding="utf-8", bom=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode(encoding))
    return p


def _veracruz(scan):
    return next(r for r in scan["records"] if "VERACRUZ" in (r["title"] or "").upper())


def _detail(rec):
    return json.loads(rec["value_json"])


def _with(cfg, **settings):
    cfg.connectors = {"finance": settings}
    return cfg


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- presets

@pytest.mark.parametrize("fname,text,label,in_title,money_in", PRESETS,
                         ids=[p[2] + (" detailed" if "detailed" in p[0] else "")
                              for p in PRESETS])
def test_each_bank_format_is_recognised_and_its_sign_convention_applied(
        tmp_path, cfg, fname, text, label, in_title, money_in):
    """Whatever the file's convention — Chase's negative purchases, Apple's
    and Amex's positive ones, Capital One's two columns — a purchase comes
    out negative and money in comes out positive."""
    scan = finance.scan(export(tmp_path, fname, text), cfg)
    assert scan["connector"] == "finance"
    assert [f["format"] for f in scan["files"]] == [label]
    spend = _veracruz(scan)
    assert spend["value"] == -29.75
    assert spend["direction"] == "out"
    assert spend["unit"] == "usd"
    assert spend["stream"] == "txn"
    income = next(r for r in scan["records"] if r["title"].startswith(in_title))
    assert income["value"] == money_in
    assert income["direction"] == "in"


def test_chase_checking_maps_type_details_and_check_number(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, PRESETS[0][0], CHASE_CHECKING), cfg)
    by_title = {r["title"]: r for r in scan["records"]}
    check = by_title["CHECK 1043"]
    assert _detail(check)["check_no"] == "1043"
    assert _detail(check)["type"] == "CHECK_PAID"
    assert _detail(check)["details"] == "CHECK"
    pay = by_title["ACME ROBOTICS PAYROLL PPD ID: 4567890123"]   # whitespace runs collapsed
    assert _detail(pay)["raw_description"] == \
        "ACME ROBOTICS PAYROLL       PPD ID: 4567890123"
    # The running balance changes with every same-day reorder; it must never
    # be part of the record, or overlapping exports would conflict.
    assert all("3150.67" not in (r["value_json"] or "") for r in scan["records"])


def test_chase_card_keeps_the_banks_category_posted_date_and_memo(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg)
    rec = _veracruz(scan)
    assert _detail(rec) == {"account": "chase:4417", "bank_category": "Food & Drink",
                            "posted_date": "2026-10-21", "type": "Sale"}
    refund = next(r for r in scan["records"] if r["title"] == "UCHI AUSTIN")
    assert refund["value"] == 12.00 and refund["direction"] == "in"
    assert refund["body"] == "birthday dinner"
    payment = next(r for r in scan["records"] if r["title"].startswith("Payment"))
    assert "bank_category" not in _detail(payment)      # the bank said nothing


def test_the_chase_file_name_names_the_card_and_it_becomes_one_card_handle(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg)
    assert scan["account"] == "chase:4417"
    assert scan["handles"] == [{"kind": "card", "value": "chase:4417",
                                "display_name": "Chase card ending 4417"}]
    assert _veracruz(scan)["participants"] == [
        {"kind": "card", "value": "chase:4417", "role": "sender"}]
    payment = next(r for r in scan["records"] if r["direction"] == "in")
    assert payment["participants"][0]["role"] == "recipient"


def test_a_renamed_chase_file_names_no_account_and_says_so(tmp_path, cfg, conn):
    scan = finance.scan(export(tmp_path, "my-card.csv", CHASE_CARD), cfg)
    assert scan["account"] == "chase"
    assert scan["handles"] == []
    assert all(r["participants"] == [] for r in scan["records"])
    text = "\n".join(finance.report_lines(scan, obs.build_plan(conn, scan)))
    assert "no account number in my-card.csv" in text


def test_merchants_never_become_handles(tmp_path, cfg):
    """One handle per spelling of every merchant would bury the people in the
    review queue; the card is the only identifier a statement carries."""
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg)
    assert {h["kind"] for h in scan["handles"]} == {"card"}
    assert all(p["kind"] == "card" for r in scan["records"] for p in r["participants"])


def test_apple_card_uses_apples_merchant_and_keeps_the_raw_description(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, PRESETS[2][0], APPLE_CARD), cfg)
    rec = _veracruz(scan)
    assert rec["title"] == "Veracruz All Natural"
    d = _detail(rec)
    assert d["raw_description"].startswith("VERACRUZ ALL NATURAL 1704 E CESAR CHAVEZ")
    assert d["bank_category"] == "Restaurants"
    assert d["card_member"] == "Alex Rivera"            # the bank's text, not a handle
    assert d["posted_date"] == "2026-10-21"
    assert scan["handles"] == [{"kind": "card", "value": "apple_card",
                                "display_name": "Apple Card"}]
    assert rec["origin_app"] == "apple_card"


def test_capital_one_card_number_column_names_the_account(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "transactions.csv", CAPITAL_ONE), cfg)
    assert scan["account"] == "capital_one:5521"
    assert scan["handles"][0]["display_name"] == "Capital One card ending 5521"
    assert _detail(_veracruz(scan))["bank_category"] == "Dining"


def test_amex_detailed_export_reads_account_reference_and_multiline_details(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "activity.csv", AMEX_DETAILED), cfg)
    rec = _veracruz(scan)
    assert scan["account"] == "amex:1007"
    d = _detail(rec)
    assert d["reference"] == "320262930123456789"      # Amex's leading apostrophe dropped
    assert d["card_member"] == "ALEX RIVERA"
    assert d["bank_category"] == "Restaurant-Restaurant"
    assert rec["body"] == "VERACRUZ ALL NATURAL\nAUSTIN\nTX"


def test_amex_basic_export_names_no_account(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "activity.csv", AMEX), cfg)
    assert scan["account"] == "amex"
    assert scan["handles"] == []
    assert scan["unnamed"] == ["activity.csv"]


def test_citi_pending_rows_are_skipped_and_counted(tmp_path, cfg):
    """A pending charge can still change amount or date, and would then get
    a different id from its posted self."""
    scan = finance.scan(export(tmp_path, "citi.csv", CITI), cfg)
    assert len(scan["records"]) == 2
    assert all(r["title"] != "UCHI AUSTIN" for r in scan["records"])
    assert "1 × pending transaction (imported once it posts)" in scan["skipped"]


def test_bank_of_america_summary_block_and_balance_line_are_skipped(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "stmt.csv", BOFA_CHECKING), cfg)
    assert len(scan["records"]) == 2
    assert "1 × balance line" in scan["skipped"]
    assert scan["unmapped"] == []
    pay = next(r for r in scan["records"] if r["value"] > 0)
    assert pay["value"] == 4200.00                      # "4,200.00" with its comma


def test_bank_of_america_card_file_name_and_reference(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "October2026_6011.csv", BOFA_CARD), cfg)
    assert scan["account"] == "bofa:6011"
    assert _detail(_veracruz(scan))["reference"] == "24692166293100123456789"


def test_wells_fargo_headerless_export_is_recognised(tmp_path, cfg):
    scan = finance.scan(export(tmp_path, "Checking1.csv", WELLS_FARGO), cfg)
    check = next(r for r in scan["records"] if r["title"] == "CHECK # 1043")
    assert _detail(check)["check_no"] == "1043"
    assert len(scan["records"]) == 3                    # the first row is data, not a header


def test_a_header_that_only_resembles_a_preset_is_refused(tmp_path, cfg):
    """An extra column means a format this connector has not seen; misreading
    its sign would be worse than asking for a mapping."""
    text = CHASE_CARD.replace("Amount,Memo", "Amount,Memo,Rewards").replace(",\n", ",,\n")
    with pytest.raises(DomainError) as e:
        finance.scan(export(tmp_path, "Chase4417_Activity.CSV", text), cfg)
    assert "Rewards" in str(e.value)
    assert "[connectors.finance]" in str(e.value)


# ---------------------------------------------------------------- money & dates

@pytest.mark.parametrize("raw,expected", [
    ("-29.75", (Decimal("-29.75"), None)),
    ("$1,234.56", (Decimal("1234.56"), None)),
    ("-$5.00", (Decimal("-5.00"), None)),
    ("$-5.00", (Decimal("-5.00"), None)),
    ("(34.50)", (Decimal("-34.50"), None)),
    ("($34.50)", (Decimal("-34.50"), None)),
    ("34.50-", (Decimal("-34.50"), None)),
    ("+4,200.00", (Decimal("4200.00"), None)),
    ("−12.00", (Decimal("-12.00"), None)),          # a real minus sign
    ("34.50 CR", (Decimal("34.50"), "CR")),
    ("34.50DR", (Decimal("34.50"), "DR")),
    ("USD 12.00", (Decimal("12.00"), None)),
    ("12.00 eur", (Decimal("12.00"), None)),
    ("34,50", (Decimal("34.50"), None)),                 # decimal comma
    ("1.234,56", (Decimal("1234.56"), None)),
    ("1,234", (Decimal("1234"), None)),                  # thousands, not decimals
    ("", (None, None)),
    ("  ", (None, None)),
])
def test_amounts_in_every_shape_banks_write_them(raw, expected):
    assert finance.parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["n/a", "12.3.4", "CR", "$"])
def test_text_that_is_not_an_amount_raises(raw):
    with pytest.raises(ValueError):
        finance.parse_amount(raw)


def test_cr_and_dr_override_the_files_sign_convention(tmp_path, cfg):
    text = "Date,Description,Amount\n10/20/2026,REFUND,12.00 CR\n10/21/2026,FEE,3.00 DR\n"
    scan = finance.scan(export(tmp_path, "activity.csv", text), cfg)   # Amex: positive = spend
    by_title = {r["title"]: r["value"] for r in scan["records"]}
    assert by_title == {"REFUND": 12.00, "FEE": -3.00}


@pytest.mark.parametrize("raw,expected", [
    ("10/20/2026", date(2026, 10, 20)),
    ("10/20/26", date(2026, 10, 20)),
    ("1/5/2026", date(2026, 1, 5)),
    ("2026-10-20", date(2026, 10, 20)),
    ("2026-10-20T00:00:00", date(2026, 10, 20)),
    ("10/20/2026 12:00:00 AM", date(2026, 10, 20)),
    ("20261020", date(2026, 10, 20)),
    ("Oct 20, 2026", date(2026, 10, 20)),
    ("20 Oct 2026", date(2026, 10, 20)),
    ("03/04/2026", date(2026, 3, 4)),                    # month first: US banks
])
def test_dates_in_the_forms_banks_use(raw, expected):
    assert finance.parse_date(raw) == expected


def test_a_configured_date_format_reads_day_first_dates():
    assert finance.parse_date("03/04/2026", "%d/%m/%Y") == date(2026, 4, 3)
    with pytest.raises(ValueError):
        finance.parse_date("2026-04-03", "%d/%m/%Y")


def test_rows_with_unreadable_dates_or_amounts_are_reported_not_imported(tmp_path, cfg):
    text = CHASE_CARD + "someday,10/21/2026,MYSTERY,,Sale,-1.00,\n" \
                        "10/20/2026,10/21/2026,ODD,,Sale,lots,\n"
    scan = finance.scan(export(tmp_path, PRESETS[1][0], text), cfg)
    assert len(scan["records"]) == 3
    what = {u["what"]: u for u in scan["unmapped"]}
    assert what["row with no readable date"]["count"] == 1
    assert what["row with no readable date"]["example"].endswith(":5")
    assert what["row with an amount that does not parse"]["count"] == 1


def test_a_zero_amount_line_has_no_direction(tmp_path, cfg):
    text = CHASE_CARD + "10/19/2026,10/19/2026,INTEREST CHARGE,,Fee,0.00,\n"
    rec = next(r for r in finance.scan(export(tmp_path, PRESETS[1][0], text), cfg)["records"]
               if r["title"] == "INTEREST CHARGE")
    assert rec["value"] == 0 and rec["direction"] is None
    assert rec["participants"][0]["role"] == "participant"


# ---------------------------------------------------------------- time

def test_transactions_sit_at_local_noon_on_the_banks_date(tmp_path, cfg):
    """Statements have no times. Noon survives any rollover hour and any DST
    shift without moving the day; the journal date is the bank's date."""
    text = CHASE_CARD + "01/15/2026,01/16/2026,WINTER COFFEE,Food & Drink,Sale,-4.50,\n"
    recs = {r["title"]: r for r in
            finance.scan(export(tmp_path, PRESETS[1][0], text), cfg)["records"]}
    assert recs["VERACRUZ ALL NATURAL"]["started_at"] == "2026-10-20T17:00:00Z"   # CDT
    assert recs["VERACRUZ ALL NATURAL"]["journal_date"] == "2026-10-20"
    assert recs["WINTER COFFEE"]["started_at"] == "2026-01-15T18:00:00Z"          # CST
    assert recs["WINTER COFFEE"]["journal_date"] == "2026-01-15"


def test_the_transaction_date_wins_over_the_posting_date(tmp_path, cfg):
    rec = _veracruz(finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg))
    assert rec["journal_date"] == "2026-10-20"
    assert _detail(rec)["posted_date"] == "2026-10-21"


# ---------------------------------------------------------------- identity

def _rows(text):
    head, *rows = text.strip("\n").split("\n")
    return head, rows


def test_overlapping_exports_give_the_same_rows_the_same_ids(tmp_path, cfg, conn):
    head = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo"
    early = [f"10/{d:02d}/2026,10/{d:02d}/2026,COFFEE {d},Food & Drink,Sale,-4.50,"
             for d in range(1, 16)]
    late = [f"10/{d:02d}/2026,10/{d:02d}/2026,COFFEE {d},Food & Drink,Sale,-4.50,"
            for d in range(10, 25)]
    twin = "10/12/2026,10/12/2026,BUS FARE,Travel,Sale,-2.75,"
    a = export(tmp_path / "a", "Chase4417_Activity20261001_20261015_20261016.CSV",
               "\n".join([head, *early, twin, twin]) + "\n")
    b = export(tmp_path / "b", "Chase4417_Activity20261010_20261024_20261025.CSV",
               "\n".join([head, twin, *reversed(late), twin]) + "\n")
    ids_a = {r["external_id"]: r for r in finance.scan(a, cfg)["records"]}
    ids_b = {r["external_id"]: r for r in finance.scan(b, cfg)["records"]}
    assert len(set(ids_a) & set(ids_b)) == 6 + 2     # Oct 10-15 plus both fares

    _apply(conn, finance.scan(a, cfg))
    plan, result = _apply(conn, finance.scan(b, cfg))
    assert plan["identical"] == 8
    assert result["added"] == 9
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 26


def test_identical_rows_on_one_day_stay_two_transactions(tmp_path, cfg):
    head = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo"
    fare = "10/12/2026,10/12/2026,BUS FARE,Travel,Sale,-2.75,"
    scan = finance.scan(export(tmp_path, "Chase4417_Activity.CSV",
                               "\n".join([head, fare, fare]) + "\n"), cfg)
    assert len({r["external_id"] for r in scan["records"]}) == 2


def test_a_folder_of_overlapping_statements_counts_the_overlap_once(tmp_path, cfg, conn):
    folder = tmp_path / "statements"
    export(folder, "Chase4417_Activity20261001_20261021_20261021.CSV", CHASE_CARD)
    export(folder / "older", "Chase4417_Activity20260915_20261020_20261020.CSV",
           CHASE_CARD.replace("10/15/2026,10/15/2026,Payment", "09/30/2026,09/30/2026,Payment"))
    scan = finance.scan(folder, cfg)
    plan = obs.build_plan(conn, scan)
    assert len(plan["new"]) == 4
    assert plan["dup_incoming"] == 2
    text = "\n".join(finance.report_lines(scan, plan))
    assert "2 transaction(s) in more than one file (overlapping statements) counted once" in text


def test_ids_name_the_account_so_two_cards_never_collide(tmp_path, cfg):
    a = finance.scan(export(tmp_path / "a", "Chase4417_Activity.CSV", CHASE_CARD), cfg)
    b = finance.scan(export(tmp_path / "b", "Chase9999_Activity.CSV", CHASE_CARD), cfg)
    assert not {r["external_id"] for r in a["records"]} & \
        {r["external_id"] for r in b["records"]}
    assert all(r["external_id"].startswith("csv:chase:4417:") for r in a["records"])


# ---------------------------------------------------------------- mapping

FIRST_LOCAL = (
    "Booked;Payee;Notes;Money Out;Money In\n"
    "03/04/2026;VERACRUZ ALL NATURAL;tacos;29,75;\n"
    "05/04/2026;SALARY;;;1.200,00\n")


def test_a_mapping_reads_a_bank_no_preset_knows(tmp_path, cfg):
    _with(cfg, date_col="Booked", description_col="Payee", memo_col="Notes",
          debit_col="Money Out", credit_col="Money In", date_format="%d/%m/%Y",
          currency="GBP", institution="First Local", account="12-34-56 87654321")
    scan = finance.scan(export(tmp_path, "first-local.csv", FIRST_LOCAL), cfg)
    rec = _veracruz(scan)
    assert rec["journal_date"] == "2026-04-03"           # day first, as configured
    assert rec["value"] == -29.75 and rec["unit"] == "gbp"
    assert rec["body"] == "tacos"
    salary = next(r for r in scan["records"] if r["title"] == "SALARY")
    assert salary["value"] == 1200.00
    assert scan["account"] == "first_local:4321"         # never the full number
    assert scan["handles"][0]["display_name"] == "First Local account ending 4321"
    assert "87654321" not in json.dumps(scan, default=str)


def test_a_mapping_with_purchases_positive(tmp_path, cfg):
    _with(cfg, date_col="When", description_col="What", amount_col="How Much",
          sign="spend_positive")
    text = "When,What,How Much\n2026-10-20,VERACRUZ ALL NATURAL,29.75\n"
    rec = _veracruz(finance.scan(export(tmp_path, "x.csv", text), cfg))
    assert rec["value"] == -29.75
    assert rec["origin_app"] == "csv"


def test_a_mapping_can_claim_a_shared_header_for_its_own_files_only(tmp_path, cfg):
    """'Date,Description,Amount' is Amex's header (charges positive) and also
    a credit union's (spend negative). `match` decides which file is whose."""
    _with(cfg, date_col="Date", description_col="Description", amount_col="Amount",
          institution="credit union", match="credit-union*.csv")
    folder = tmp_path / "mixed"
    export(folder, "credit-union-oct.csv",
           "Date,Description,Amount\n10/20/2026,VERACRUZ ALL NATURAL,-29.75\n")
    export(folder, "activity.csv",
           "Date,Description,Amount\n10/21/2026,VERACRUZ ALL NATURAL,29.75\n")
    scan = finance.scan(folder, cfg)
    assert sorted(f["format"] for f in scan["files"]) == \
        ["American Express card", "credit union CSV (mapping)"]
    assert [r["value"] for r in scan["records"]] == [-29.75, -29.75]


@pytest.mark.parametrize("settings,needle", [
    ({"date_col": "Date", "amount_col": "Amount"}, "missing description_col"),
    ({"date_col": "Date", "description_col": "D"}, "amount_col (or debit_col/credit_col)"),
    ({"date_col": "Date", "description_col": "D", "amount_col": "A", "debit_col": "X"},
     "give one or the other"),
    ({"date_col": "Date", "description_col": "D", "amount_col": "A", "sign": "backwards"},
     "sign must be 'spend_negative' or 'spend_positive', got 'backwards'"),
    ({"date_col": "Date", "description_col": "D", "amount_col": "A", "currency": "dollars"},
     "three-letter ISO code"),
])
def test_a_bad_mapping_fails_naming_the_setting(tmp_path, cfg, settings, needle):
    _with(cfg, **settings)
    with pytest.raises(DomainError) as e:
        finance.scan(export(tmp_path, "x.csv", AMEX), cfg)
    assert needle in str(e.value)
    assert "[connectors.finance]" in str(e.value)
    assert "\n" not in str(e.value)


def test_tab_separated_exports_are_recognised(tmp_path, cfg):
    text = CHASE_CARD.replace(",", "\t")
    scan = finance.scan(export(tmp_path, "Chase4417_Activity.CSV", text), cfg)
    assert _veracruz(scan)["value"] == -29.75


# ---------------------------------------------------------------- bad input

def test_an_unknown_header_says_what_it_saw(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        finance.scan(export(tmp_path, "budget.csv", "Month,Planned,Actual\nOct,100,90\n"), cfg)
    assert "budget.csv" in str(e.value)
    assert "Month, Planned, Actual" in str(e.value)


def test_an_empty_file_says_so(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        finance.scan(export(tmp_path, "empty.csv", "\n\n"), cfg)
    assert "empty.csv is empty" in str(e.value)


def test_utf8_bom_export_is_accepted(tmp_path, cfg):
    """PowerShell and Excel write BOMs; a BOM glued to the first header name
    must not hide the format."""
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD, bom=True), cfg)
    assert len(scan["records"]) == 3


def test_a_windows_1252_export_is_accepted(tmp_path, cfg):
    text = CHASE_CARD.replace("UCHI AUSTIN", "CAFÉ CRÈME AUSTIN")
    scan = finance.scan(export(tmp_path, PRESETS[1][0], text, encoding="cp1252"), cfg)
    assert any(r["title"] == "CAFÉ CRÈME AUSTIN" for r in scan["records"])
    assert scan["files"][0]["encoding"] == "cp1252"


# ---------------------------------------------------------------- targets

def test_a_folder_is_read_recursively_and_other_files_are_counted(tmp_path, cfg):
    folder = tmp_path / "money"
    export(folder, PRESETS[1][0], CHASE_CARD)
    export(folder / "2026" / "checking", PRESETS[0][0], CHASE_CHECKING)
    export(folder, "notes.txt", "remember to download Amex")
    export(folder, ".DS_Store", "junk")
    scan = finance.scan(folder, cfg)
    assert len(scan["files"]) == 2
    assert scan["account"] == "chase:4417, chase:8812"
    assert "1 × file that is not a statement export (e.g. notes.txt)" in scan["skipped"]


def test_an_unrecognised_csv_in_a_folder_is_reported_and_the_rest_imported(tmp_path, cfg):
    folder = tmp_path / "money"
    export(folder, PRESETS[1][0], CHASE_CARD)
    export(folder, "budget.csv", "Month,Planned,Actual\nOct,100,90\n")
    scan = finance.scan(folder, cfg)
    assert len(scan["records"]) == 3
    assert any("budget.csv" in u["what"] for u in scan["unmapped"])


def test_a_folder_with_nothing_readable_fails(tmp_path, cfg):
    folder = tmp_path / "money"
    export(folder, "budget.csv", "Month,Planned,Actual\nOct,100,90\n")
    with pytest.raises(DomainError) as e:
        finance.scan(folder, cfg)
    assert "budget.csv" in str(e.value)
    export(tmp_path / "words", "notes.txt", "hello")
    with pytest.raises(DomainError) as e:
        finance.scan(tmp_path / "words", cfg)
    assert "holds no statement export" in str(e.value)
    assert "notes.txt" in str(e.value)


def test_a_zip_of_statements_is_read_and_mac_junk_ignored(tmp_path, cfg):
    z = tmp_path / "statements.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(PRESETS[1][0], CHASE_CARD)
        zf.writestr("__MACOSX/._" + PRESETS[1][0], "junk")
        zf.writestr("checking/" + PRESETS[0][0], CHASE_CHECKING)
    scan = finance.scan(z, cfg)
    assert len(scan["files"]) == 2 and len(scan["records"]) == 6
    assert scan["source_hash"] == base.file_hash(z)


def test_scan_is_deterministic(tmp_path, cfg):
    folder = tmp_path / "money"
    export(folder, PRESETS[1][0], CHASE_CARD)
    export(folder, PRESETS[0][0], CHASE_CHECKING)
    a, b = finance.scan(folder, cfg), finance.scan(folder, cfg)
    assert a["records"] == b["records"]
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]
    keys = [(r["stream"], r["started_at"], r["external_id"]) for r in a["records"]]
    assert keys == sorted(keys)


def test_default_target_is_the_configured_path(tmp_path, cfg):
    folder = tmp_path / "statements"
    folder.mkdir()
    _with(cfg, path=str(folder))
    assert finance.default_target(cfg) == folder
    _with(cfg, path="statements")                        # relative to the project root
    assert finance.default_target(cfg) == cfg.root / "statements"
    _with(cfg, path=str(tmp_path / "missing"))
    with pytest.raises(DomainError) as e:
        finance.default_target(cfg)
    assert "does not exist" in str(e.value)
    _with(cfg)
    with pytest.raises(DomainError) as e:
        finance.default_target(cfg)
    assert "finance needs a path" in str(e.value)


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_span_money_and_the_sign_check(tmp_path, cfg, conn):
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg)
    text = "\n".join(finance.report_lines(scan, obs.build_plan(conn, scan)))
    assert "txn: 3 new, 0 changed | 2026-10-12..2026-10-20" in text
    assert "e.g. 2026-10-20 -29.75 usd VERACRUZ ALL NATURAL" in text
    assert "usd: -29.75 out, +512.00 in" in text
    assert "largest: out 2026-10-20 -29.75 usd VERACRUZ ALL NATURAL; " \
           "in 2026-10-15 +500.00 usd Payment Thank You-Mobile" in text
    assert "Chase card (spend negative), 3 transaction(s)" in text
    assert "handle: card:chase:4417 (Chase card ending 4417)" in text


def test_report_after_a_reimport_says_nothing_is_new(tmp_path, cfg, conn):
    scan = finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg)
    _apply(conn, scan)
    text = "\n".join(finance.report_lines(scan, obs.build_plan(conn, scan)))
    assert "nothing new — these statements match the database" in text
    assert "3 identical (skipped)" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    folder = tmp_path / "money"
    for fname, text, *_ in PRESETS:
        export(folder / fname.split(".")[0], fname, text)
    _, first = _apply(conn, finance.scan(folder, cfg))
    assert first["added"] == 25                     # Citi's pending row waits
    plan, again = _apply(conn, finance.scan(folder, cfg))
    assert again["added"] == 0 and again["identical"] == 25
    assert plan["conflicts"] == [] and plan["updates"] == []


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")

    def counts():
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in tables}
    before = counts()
    _apply(conn, finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg))
    assert counts() == before


def test_nothing_is_ever_removed_by_a_later_statement(tmp_path, cfg, conn):
    """Statements are windows: last month's transactions are absent from this
    month's file, and must stay."""
    _apply(conn, finance.scan(export(tmp_path / "a", PRESETS[1][0], CHASE_CARD), cfg))
    later = finance.scan(export(tmp_path / "b", PRESETS[1][0],
                                _rows(CHASE_CARD)[0] + "\n"
                                "10/22/2026,10/23/2026,H-E-B #742,Groceries,Sale,-64.12,\n"),
                         cfg)
    assert "present_ids" not in later
    plan, result = _apply(conn, later)
    assert plan["removed"] == [] and "mirror_note" not in plan
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 4


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    _apply(conn, finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_a_linked_card_attributes_every_transaction_on_it(tmp_path, cfg, conn):
    from lifecrm.domain import entities
    _apply(conn, finance.scan(export(tmp_path, PRESETS[1][0], CHASE_CARD), cfg))
    alex = entities.create(conn, "person", "Alex Rivera")["id"]
    obs.link_handle(conn, "card", "chase:4417", alex)
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) c FROM observation_participants p JOIN entity_handles h "
        "ON h.kind=p.handle_kind AND h.value=p.handle_value WHERE h.entity_id=?",
        (alex,)).fetchone()["c"]
    assert n == 3
    assert run_checks(conn, cfg)["failures"] == 0


# ---------------------------------------------------------------- staleness

def _status(report, check):
    return next((c["status"] for c in report["checks"] if c["check"] == check), None)


def test_doctor_warns_when_the_newest_transaction_is_old(tmp_path, cfg, conn):
    old = CHASE_CARD.replace("2026", "2024")
    _apply(conn, finance.scan(export(tmp_path, PRESETS[1][0], old), cfg))
    report = run_checks(conn, cfg)
    assert _status(report, "finance_data_age") == "warn"
    assert report["failures"] == 0


def test_doctor_is_quiet_when_statements_are_current(tmp_path, cfg, conn):
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    text = _rows(CHASE_CARD)[0] + f"\n{today},{today},VERACRUZ ALL NATURAL,,Sale,-1.00,\n"
    _apply(conn, finance.scan(export(tmp_path, PRESETS[1][0], text), cfg))
    assert _status(run_checks(conn, cfg), "finance_data_age") == "ok"


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra=""):
    root = cli_project(tmp_path, monkeypatch)
    if extra:
        with open(root / "config.toml", "a", encoding="utf-8") as f:
            f.write(extra)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def _count(where=""):
    r = runner.invoke(app, ["sql", f"SELECT COUNT(*) c FROM observations {where}", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = export(tmp_path / "exp", PRESETS[1][0], CHASE_CARD)
    r = runner.invoke(app, ["observe", "finance", str(p)])
    assert r.exit_code == 0, r.output
    assert "txn: 3 new" in r.output
    assert "dry run — nothing written" in r.output
    assert _count() == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = export(tmp_path / "exp", PRESETS[1][0], CHASE_CARD)
    r = runner.invoke(app, ["observe", "finance", str(p), "--apply"])
    assert r.exit_code == 0, r.output
    assert "3 added" in r.output
    again = runner.invoke(app, ["observe", "finance", str(p), "--apply"])
    assert "0 added" in again.output and "3 identical skipped" in again.output
    assert _count("WHERE status='active'") == 3
    note = runner.invoke(app, ["observe", "finance", str(p)])
    assert "already imported" in note.output


def test_cli_reads_the_configured_path_and_the_handle_queue_stays_short(tmp_path, monkeypatch):
    folder = tmp_path / "statements"
    export(folder, PRESETS[1][0], CHASE_CARD)
    export(folder, PRESETS[0][0], CHASE_CHECKING)
    _cli_setup(tmp_path, monkeypatch,
               f"[connectors.finance]\npath = '{folder.as_posix()}'\n")
    r = runner.invoke(app, ["observe", "finance", "--apply"])
    assert r.exit_code == 0, r.output
    assert "6 added" in r.output
    handles = json.loads(runner.invoke(app, ["handles", "list", "--json"]).output)
    assert sorted(h["value"] for h in handles) == ["chase:4417", "chase:8812"]


def test_cli_without_a_path_or_setting_says_how_to_give_one(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["observe", "finance"])
    assert r.exit_code == 1
    assert "finance needs a path" in r.output
