"""Payments connector: Venmo statements and PayPal activity downloads.

The connector is a pure reader, so most of this runs scan() against files
shaped like each service's download — Venmo's preamble, unnamed first
column, balance rows and '+ $' amounts; PayPal's BOM, quoted cells, zone
abbreviations and Memo lines. The rest is what a person-to-person importer
can get quietly wrong: which side of a payment is you (never a
counterparty), which way a Charge runs, transfers between your own
accounts, and unfinished payments that must never count.
"""

import csv
import io
import json
import zipfile
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import payments
from lifecrm.demo import persona
from lifecrm.demo.sources import payments as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

VENMO_COLUMNS = ["", "ID", "Datetime", "Type", "Status", "Note", "From", "To",
                 "Amount (total)", "Amount (tip)", "Amount (tax)", "Amount (fee)", "Tax Rate",
                 "Tax Exempt", "Funding Source", "Destination", "Beginning Balance",
                 "Ending Balance", "Statement Period Venmo Fees", "Terminal Location",
                 "Year to Date Venmo Fees", "Disclaimer"]


def vrow(tid, when, kind, status, note, frm, to, total, tip="", fee="", funding="",
         dest=""):
    row = [""] * len(VENMO_COLUMNS)
    row[1:12] = [tid, when, kind, status, note, frm, to, total, tip, "0", fee]
    row[12], row[14], row[15], row[19] = "0", funding, dest, "Venmo"
    return row


def venmo_csv(rows, username="Alex-Rivera-7"):
    blank = [""] * len(VENMO_COLUMNS)
    head = []
    if username:
        head = [[f"Account Statement - (@{username}) "] + blank[1:],
                ["Account Activity"] + blank[1:]]
    opening, closing = list(blank), list(blank)
    opening[16] = "$212.55"
    closing[17], closing[18], closing[20], closing[21] = \
        "$100.00", "$0.00", "$0.00", "In case of errors or questions..."
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n").writerows(
        head + [VENMO_COLUMNS, opening] + rows + [closing])
    return buf.getvalue()


VENMO_ROWS = [
    vrow("3912000000000000001", "2026-10-21T02:05:00", "Payment", "Complete",
         "Veracruz tacos \N{TACO}", "Jake Morales", "Alex Rivera", "+ $14.88",
         dest="Venmo balance"),
    vrow("3912000000000000002", "2026-10-20T01:05:00", "Payment", "Complete",
         "Bled lakeside place", "Alex Rivera", "Jake Morales", "- $150.00",
         funding="Venmo balance"),
    vrow("3912000000000000003", "2026-10-15T18:00:00", "Charge", "Complete", "October rent",
         "Alex Rivera", "Maya Chen", "+ $500.00", dest="Venmo balance"),
    vrow("3912000000000000004", "2026-10-16T15:00:00", "Standard Transfer", "Issued", "",
         "", "", "- $1,000.00", dest="Chase Bank *8812"),
    vrow("3912000000000000005", "2026-10-17T15:00:00", "Payment", "Pending", "coffee",
         "Alex Rivera", "Sam Okafor", "- $5.00", funding="Venmo balance"),
    vrow("3912000000000000006", "2026-10-17T16:00:00", "Payment", "Declined", "coffee",
         "Alex Rivera", "Sam Okafor", "- $5.00", funding="Visa Debit *4417"),
    vrow("3.912E+18", "2026-10-17T17:00:00", "Payment", "Complete", "lost digits",
         "Alex Rivera", "Sam Okafor", "- $5.00"),
    vrow("3912000000000000008", "2026-10-18T15:30:00", "Merchant Transaction", "Complete",
         "", "Alex Rivera", "Fleet Coffee", "- $7.25", tip="+ $1.00",
         funding="Venmo balance"),
]

PAYPAL_COLUMNS = [
    "Date", "Time", "TimeZone", "Name", "Type", "Status", "Currency", "Gross", "Fee", "Net",
    "From Email Address", "To Email Address", "Transaction ID", "Shipping Address",
    "Address Status", "Item Title", "Item ID", "Reference Txn ID", "Balance", "Subject",
    "Note", "Balance Impact"]


def prow(day, time, zone, name, kind, status, gross, frm, to, tid, fee="0.00", net=None,
         item="", note="", impact=None, currency="USD"):
    row = dict.fromkeys(PAYPAL_COLUMNS, "")
    row.update({"Date": day, "Time": time, "TimeZone": zone, "Name": name, "Type": kind,
                "Status": status, "Currency": currency, "Gross": gross, "Fee": fee,
                "Net": net if net is not None else gross, "From Email Address": frm,
                "To Email Address": to, "Transaction ID": tid, "Item Title": item,
                "Note": note,
                "Balance Impact": impact or ("Debit" if gross.startswith("-") else "Credit")})
    return [row[c] for c in PAYPAL_COLUMNS]


ME = "alex.rivera@example.com"
PAYPAL_ROWS = [
    prow("10/07/2026", "18:20:00", "CDT", "Linda Rivera", "Mobile Payment", "Completed",
         "40.00", "linda.rivera@example.com", ME, "7XK00000000000001",
         note="Gas money for Sunday"),
    prow("10/18/2026", "08:15:00", "PDT", "Fleet Coffee", "Express Checkout Payment",
         "Completed", "-12.50", ME, "billing@fleetcoffee.example", "7XK00000000000002",
         item="Coffee beans 12oz"),
    prow("10/19/2026", "10:00:00", "PDT", "Sam Okafor", "Website Payment", "Completed",
         "100.00", "sam.okafor@example.com", ME, "7XK00000000000003", fee="-3.79",
         net="96.21", note="old bike rack"),
    prow("10/18/2026", "08:14:00", "PDT", "Fleet Coffee", "General Authorization",
         "Completed", "-12.50", ME, "billing@fleetcoffee.example", "7XK00000000000004",
         impact="Memo"),
    prow("10/18/2026", "08:15:00", "PDT", "", "Bank Deposit to PP Account", "Completed",
         "12.50", "", ME, "7XK00000000000005"),
    prow("10/20/2026", "09:00:00", "PDT", "Jake Morales", "General Payment", "Pending",
         "-20.00", ME, "jake.morales@example.com", "7XK00000000000006"),
    prow("10/20/2026", "09:30:00", "PDT", "Jake Morales", "General Payment", "Reversed",
         "-20.00", ME, "jake.morales@example.com", "7XK00000000000007"),
]


def paypal_csv(rows):
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_ALL).writerows(
        [PAYPAL_COLUMNS] + rows)
    return "﻿" + buf.getvalue()


def write(tmp_path, name, text):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


def venmo(tmp_path, rows=VENMO_ROWS, **kw):
    return write(tmp_path, "VenmoStatement_Oct_2026.csv", venmo_csv(rows, **kw))


def paypal(tmp_path, rows=PAYPAL_ROWS):
    return write(tmp_path, "Download.CSV", paypal_csv(rows))


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _rec(scan, external_id):
    return next(r for r in scan["records"] if r["external_id"] == external_id)


def _parts(rec):
    return {p["role"]: p["value"] for p in rec["participants"]}


# ---------------------------------------------------------------- Venmo

def test_a_venmo_payment_names_the_counterparty_and_your_side(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path), cfg)
    tacos = _rec(scan, "venmo:3912000000000000001")
    assert tacos["stream"] == "txn" and tacos["origin_app"] == "venmo"
    assert tacos["value"] == 14.88 and tacos["unit"] == "usd"
    assert tacos["direction"] == "in"
    assert tacos["title"] == "Jake Morales: Veracruz tacos \N{TACO}"
    assert tacos["body"] == "Veracruz tacos \N{TACO}"
    assert json.loads(tacos["value_json"]) == {
        "app": "venmo", "type": "Payment", "status": "Complete",
        "note": "Veracruz tacos \N{TACO}", "destination": "Venmo balance"}
    assert tacos["participants"] == [
        {"kind": "app_account", "value": "venmo:Jake Morales", "role": "counterparty"},
        {"kind": "app_account", "value": "venmo:Alex Rivera", "role": "recipient"}]
    bled = _rec(scan, "venmo:3912000000000000002")
    assert bled["value"] == -150.0 and bled["direction"] == "out"
    assert _parts(bled) == {"counterparty": "venmo:Jake Morales",
                            "sender": "venmo:Alex Rivera"}


def test_a_charge_you_made_has_the_payer_as_counterparty(tmp_path, cfg):
    """A Charge's From asked its To for money: Alex charged Maya, Maya paid."""
    rent = _rec(payments.scan(venmo(tmp_path), cfg), "venmo:3912000000000000003")
    assert rent["value"] == 500.0 and rent["direction"] == "in"
    assert _parts(rent) == {"counterparty": "venmo:Maya Chen",
                            "recipient": "venmo:Alex Rivera"}


def test_a_transfer_to_the_bank_has_no_counterparty_and_says_so(tmp_path, cfg):
    cash_out = _rec(payments.scan(venmo(tmp_path), cfg), "venmo:3912000000000000004")
    assert cash_out["value"] == -1000.0
    assert cash_out["title"] == "Standard Transfer to Chase Bank *8812"
    assert cash_out["participants"] == []
    assert json.loads(cash_out["value_json"])["transfer"] is True


def test_a_merchant_payment_keeps_its_tip(tmp_path, cfg):
    coffee = _rec(payments.scan(venmo(tmp_path), cfg), "venmo:3912000000000000008")
    assert coffee["title"] == "Fleet Coffee" and coffee["value"] == -7.25
    assert json.loads(coffee["value_json"])["tip"] == 1.0
    assert _parts(coffee)["counterparty"] == "venmo:Fleet Coffee"


def test_unfinished_summary_and_mangled_rows_are_counted_not_imported(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path), cfg)
    assert {r["external_id"] for r in scan["records"]} == {
        f"venmo:391200000000000000{n}" for n in (1, 2, 3, 4, 8)}
    assert "1 × pending transaction (only completed ones are imported)" in scan["skipped"]
    assert "1 × declined transaction (only completed ones are imported)" in scan["skipped"]
    assert "2 × statement summary row (balances, fees, disclaimer)" in scan["skipped"]
    mangled = next(u for u in scan["unmapped"] if "spreadsheet rounded" in u["what"])
    assert mangled["count"] == 1 and mangled["example"].endswith(":11")


def test_a_status_this_reader_does_not_know_is_reported(tmp_path, cfg):
    rows = [vrow("1", "2026-10-21T02:05:00", "Payment", "Mystery", "x", "Jake Morales",
                 "Alex Rivera", "+ $1.00")]
    scan = payments.scan(venmo(tmp_path, rows), cfg)
    assert scan["records"] == []
    assert scan["unmapped"][0]["what"] == \
        "row with a status this reader does not know ('Mystery')"


def test_venmo_times_are_read_as_utc_and_follow_the_rollover(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path), cfg)
    tacos = _rec(scan, "venmo:3912000000000000001")
    assert tacos["started_at"] == "2026-10-21T02:05:00Z"
    assert tacos["journal_date"] == "2026-10-20"              # 21:05 in Austin
    early = [vrow("9", "2026-10-21T09:30:00", "Payment", "Complete", "x", "Jake Morales",
                  "Alex Rivera", "+ $1.00")]
    assert payments.scan(venmo(tmp_path / "e", early), cfg)["records"][0][
        "journal_date"] == "2026-10-20"                       # 04:30, before 05:00
    cfg.day_rollover_hour = 0
    assert payments.scan(venmo(tmp_path / "f", early), cfg)["records"][0][
        "journal_date"] == "2026-10-21"


def test_venmo_timezone_reads_local_statements(tmp_path, cfg):
    cfg.connectors = {"payments": {"venmo_timezone": "America/Chicago"}}
    tacos = _rec(payments.scan(venmo(tmp_path), cfg), "venmo:3912000000000000001")
    assert tacos["started_at"] == "2026-10-21T07:05:00Z"
    cfg.connectors = {"payments": {"venmo_timezone": "Mars/Olympus"}}
    with pytest.raises(DomainError, match="venmo_timezone 'Mars/Olympus'"):
        payments.scan(venmo(tmp_path / "b"), cfg)


def test_a_venmo_time_that_states_its_zone_keeps_it(tmp_path, cfg):
    cfg.connectors = {"payments": {"venmo_timezone": "America/Chicago"}}
    rows = [vrow("1", "2026-10-21T02:05:00Z", "Payment", "Complete", "x", "Jake Morales",
                 "Alex Rivera", "+ $1.00"),
            vrow("2", "2026-10-21T02:05:00-04:00", "Payment", "Complete", "y",
                 "Jake Morales", "Alex Rivera", "+ $2.00")]
    scan = payments.scan(venmo(tmp_path, rows), cfg)
    assert [r["started_at"] for r in scan["records"]] == ["2026-10-21T02:05:00Z",
                                                          "2026-10-21T06:05:00Z"]


# ---------------------------------------------------------------- Venmo: who is you

def test_you_are_the_name_on_every_row(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path), cfg)
    assert scan["account"] == "venmo:Alex Rivera"
    assert scan["facts"]["you"]["venmo"] == \
        "Alex Rivera (the name on every row of the statement)"


def test_the_setting_and_the_owner_name_say_who_you_are(tmp_path, cfg):
    one = VENMO_ROWS[:1]                                   # Jake and Alex on every row
    cfg.connectors = {"payments": {"me": "Alex Rivera"}}
    scan = payments.scan(venmo(tmp_path, one, username=None), cfg)
    assert scan["facts"]["you"]["venmo"] == "Alex Rivera ([connectors.payments] me)"
    cfg.connectors = {}
    cfg.extra = {"owner": {"name": "Alex Rivera"}}
    scan = payments.scan(venmo(tmp_path / "b", one, username=None), cfg)
    assert scan["facts"]["you"]["venmo"] == "Alex Rivera ([owner] name)"
    cfg.connectors = {"payments": {"me": 7}}
    with pytest.raises(DomainError, match=r"\[connectors.payments\] me must be"):
        payments.scan(venmo(tmp_path / "c", one), cfg)


def test_the_username_breaks_a_tie_between_two_names(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path, VENMO_ROWS[:1]), cfg)
    assert scan["account"] == "venmo:Alex Rivera"
    assert "@username" in scan["facts"]["you"]["venmo"]


def test_unknown_owner_reads_the_counterparty_from_the_sign_and_warns(tmp_path, cfg, conn):
    rows = VENMO_ROWS[:1] + [vrow("10", "2026-10-15T18:00:00", "Charge", "Complete", "rent",
                                  "Maya Chen", "Sam Okafor", "- $5.00")]
    scan = payments.scan(venmo(tmp_path, rows, username=None), cfg)
    assert scan["account"] is None
    assert _parts(_rec(scan, "venmo:3912000000000000001")) == \
        {"counterparty": "venmo:Jake Morales"}
    # a Charge you paid: its From asked you for the money
    assert _parts(_rec(scan, "venmo:10")) == {"counterparty": "venmo:Maya Chen"}
    text = "\n".join(payments.report_lines(scan, obs.build_plan(conn, scan)))
    assert "WARNING which Venmo name is yours is not known" in text


def test_your_side_is_never_a_counterparty(tmp_path, cfg):
    scan = payments.scan(venmo(tmp_path), cfg)
    for r in scan["records"]:
        assert all(not (p["role"] == "counterparty" and p["value"] == "venmo:Alex Rivera")
                   for p in r["participants"])


# ---------------------------------------------------------------- PayPal

def test_a_paypal_payment_names_the_counterparty_by_email(tmp_path, cfg):
    scan = payments.scan(paypal(tmp_path), cfg)
    gas = _rec(scan, "paypal:7XK00000000000001")
    assert gas["origin_app"] == "paypal" and gas["unit"] == "usd"
    assert gas["value"] == 40.0 and gas["direction"] == "in"
    assert gas["title"] == "Linda Rivera: Gas money for Sunday"
    assert gas["participants"] == [
        {"kind": "email", "value": "linda.rivera@example.com", "role": "counterparty"},
        {"kind": "email", "value": ME, "role": "recipient"}]
    handles = {h["value"]: h["display_name"] for h in scan["handles"]}
    assert handles["linda.rivera@example.com"] == "Linda Rivera"
    assert handles[ME] == "you (PayPal)"
    assert scan["account"] == ME


def test_paypal_zones_become_utc(tmp_path, cfg):
    scan = payments.scan(paypal(tmp_path), cfg)
    assert _rec(scan, "paypal:7XK00000000000001")["started_at"] == "2026-10-07T23:20:00Z"
    coffee = _rec(scan, "paypal:7XK00000000000002")
    assert coffee["started_at"] == "2026-10-18T15:15:00Z"    # 08:15 PDT
    assert coffee["journal_date"] == "2026-10-18"


@pytest.mark.parametrize("zone,expected", [
    ("CET", "2026-10-18T07:15:00Z"), ("GMT+01:00", "2026-10-18T07:15:00Z"),
    ("-0700", "2026-10-18T15:15:00Z"), ("Europe/Ljubljana", "2026-10-18T06:15:00Z"),
])
def test_paypal_time_zones_in_every_spelling(tmp_path, cfg, zone, expected):
    rows = [prow("18/10/2026", "08:15:00", zone, "Fleet Coffee", "Express Checkout Payment",
                 "Completed", "-12.50", ME, "billing@fleetcoffee.example", "Z1")]
    assert payments.scan(paypal(tmp_path, rows), cfg)["records"][0]["started_at"] == expected


def test_an_unknown_zone_is_read_at_home_and_reported(tmp_path, cfg):
    rows = [prow("10/18/2026", "08:15:00", "XYZT", "Fleet Coffee", "Express Checkout Payment",
                 "Completed", "-12.50", ME, "billing@fleetcoffee.example", "Z1")]
    scan = payments.scan(paypal(tmp_path, rows), cfg)
    assert scan["records"][0]["started_at"] == "2026-10-18T13:15:00Z"   # CDT
    assert scan["unmapped"][0]["what"] == \
        "time zone 'XYZT' not recognised (read in America/Chicago)"


def test_paypal_fees_transfers_memos_and_unfinished_rows(tmp_path, cfg):
    scan = payments.scan(paypal(tmp_path), cfg)
    sale = _rec(scan, "paypal:7XK00000000000003")
    assert sale["value"] == 100.0
    assert json.loads(sale["value_json"])["fee"] == -3.79
    assert json.loads(sale["value_json"])["net"] == 96.21
    top_up = _rec(scan, "paypal:7XK00000000000005")
    assert top_up["participants"] == [{"kind": "email", "value": ME, "role": "recipient"}]
    assert top_up["title"] == "Bank Deposit to PP Account"
    assert json.loads(top_up["value_json"])["transfer"] is True
    coffee = _rec(scan, "paypal:7XK00000000000002")
    assert coffee["title"] == "Fleet Coffee: Coffee beans 12oz"
    assert _parts(coffee)["counterparty"] == "billing@fleetcoffee.example"
    assert len(scan["records"]) == 4
    assert "1 × memo line with no balance impact (an authorization or hold)" in \
        scan["skipped"]
    assert "1 × pending transaction (only completed ones are imported)" in scan["skipped"]
    assert any("'Reversed'" in u["what"] for u in scan["unmapped"])


def test_paypal_date_order_from_the_file_the_setting_or_us_zones(tmp_path, cfg, conn):
    dmy = [prow("18/10/2026", "18:15:00", "CET", "Fleet Coffee", "Express Checkout Payment",
                "Completed", "-12.50", ME, "billing@fleetcoffee.example", "Z1"),
           prow("03/10/2026", "18:15:00", "CET", "Fleet Coffee", "Express Checkout Payment",
                "Completed", "-12.50", ME, "billing@fleetcoffee.example", "Z2")]
    scan = payments.scan(paypal(tmp_path / "a", dmy), cfg)
    assert _rec(scan, "paypal:Z2")["journal_date"] == "2026-10-03"
    ambiguous = dmy[1:]
    scan = payments.scan(paypal(tmp_path / "b", ambiguous), cfg)
    assert _rec(scan, "paypal:Z2")["journal_date"] == "2026-03-10"
    text = "\n".join(payments.report_lines(scan, obs.build_plan(conn, scan)))
    assert "WARNING Download.CSV: every date fits both" in text
    cfg.connectors = {"payments": {"paypal_date_order": "dmy"}}
    scan = payments.scan(paypal(tmp_path / "c", ambiguous), cfg)
    assert _rec(scan, "paypal:Z2")["journal_date"] == "2026-10-03"
    cfg.connectors = {}
    us = [prow("10/07/2026", "18:20:00", "CDT", "Linda Rivera", "Mobile Payment",
               "Completed", "40.00", "linda.rivera@example.com", ME, "Z3")]
    scan = payments.scan(paypal(tmp_path / "d", us), cfg)
    assert scan["records"][0]["journal_date"] == "2026-10-07"
    assert scan["facts"]["warnings"] == []
    cfg.connectors = {"payments": {"paypal_date_order": "sideways"}}
    with pytest.raises(DomainError, match="paypal_date_order"):
        payments.scan(paypal(tmp_path / "e", us), cfg)


def test_a_configured_email_says_which_side_is_you(tmp_path, cfg):
    cfg.extra = {"owner": {"name": "Alex Rivera", "handles": [f"email:{ME}"]}}
    scan = payments.scan(paypal(tmp_path), cfg)
    assert scan["facts"]["you"]["paypal"] == f"{ME} ([connectors.payments] me / [owner] handles)"
    assert {h["value"]: h["display_name"] for h in scan["handles"]}[ME] == "Alex Rivera"


# ---------------------------------------------------------------- files

def test_a_folder_reads_both_services_and_counts_other_files(tmp_path, cfg):
    folder = tmp_path / "money"
    venmo(folder)
    paypal(folder / "pp")
    write(folder, "Chase4417_Activity.CSV",
          "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n")
    write(folder, "notes.txt", "hi")
    scan = payments.scan(folder, cfg)
    assert sorted(f["app"] for f in scan["files"]) == ["paypal", "venmo"]
    assert scan["account"] == f"venmo:Alex Rivera, {ME}"
    assert "2 × file that is not a Venmo or PayPal download (e.g. " \
           "Chase4417_Activity.CSV)" in scan["skipped"]


def test_a_zip_of_statements_is_read(tmp_path, cfg):
    path = tmp_path / "statements.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("2026/VenmoStatement_Oct_2026.csv", venmo_csv(VENMO_ROWS))
        zf.writestr("__MACOSX/2026/._VenmoStatement_Oct_2026.csv", "junk")
    scan = payments.scan(path, cfg)
    assert len(scan["records"]) == 5 and scan["target_kind"] == "zip"


def test_a_file_that_is_neither_says_what_it_saw(tmp_path, cfg):
    p = write(tmp_path, "export.csv", "Date,Description,Amount\n10/20/2026,TACOS,-9.00\n")
    with pytest.raises(DomainError) as e:
        payments.scan(p, cfg)
    msg = str(e.value)
    assert "not a Venmo statement or a PayPal activity download" in msg
    assert "first row: Date, Description, Amount" in msg and "\n" not in msg


def test_a_folder_with_nothing_readable_fails(tmp_path, cfg):
    write(tmp_path / "f", "notes.txt", "hi")
    with pytest.raises(DomainError, match="holds no .csv statement — has: notes.txt"):
        payments.scan(tmp_path / "f", cfg)
    write(tmp_path / "g", "bank.csv", "Date,Description,Amount\n")
    with pytest.raises(DomainError, match="none of the 1 .csv file"):
        payments.scan(tmp_path / "g", cfg)


def test_a_utf8_bom_venmo_statement_is_read(tmp_path, cfg):
    p = write(tmp_path, "v.csv", "﻿" + venmo_csv(VENMO_ROWS))
    assert len(payments.scan(p, cfg)["records"]) == 5


def test_scan_is_deterministic(tmp_path, cfg):
    folder = tmp_path / "money"
    venmo(folder)
    paypal(folder)
    a, b = payments.scan(folder, cfg), payments.scan(folder, cfg)
    assert a["records"] == b["records"] and a["handles"] == b["handles"]
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]


# ---------------------------------------------------------------- handles

def test_handles_are_identifiers_with_names_only_as_display(tmp_path, cfg):
    folder = tmp_path / "money"
    venmo(folder)
    paypal(folder)
    scan = payments.scan(folder, cfg)
    declared = {(h["kind"], h["value"]) for h in scan["handles"]}
    for r in scan["records"]:
        for p in r["participants"]:
            assert (p["kind"], p["value"]) in declared
            assert p["kind"] in ("email", "app_account")
            assert p["kind"] == "email" or p["value"].startswith("venmo:")
    venmo_names = {h["value"]: h["display_name"] for h in scan["handles"]
                   if h["kind"] == "app_account"}
    assert venmo_names["venmo:Jake Morales"] == "Jake Morales"


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_span_money_and_whose_account(tmp_path, cfg, conn):
    scan = payments.scan(venmo(tmp_path), cfg)
    text = "\n".join(payments.report_lines(scan, obs.build_plan(conn, scan)))
    assert "txn: 5 new, 0 changed | 2026-10-15..2026-10-20" in text
    assert "usd: -1,157.25 out, +514.88 in across these rows" in text
    assert "you on venmo: Alex Rivera (the name on every row of the statement)" in text
    assert "counterparties: venmo:Jake Morales (2)" in text
    assert "Venmo times read as UTC" in text
    assert "file VenmoStatement_Oct_2026.csv: 5 completed transaction(s) " \
           "2026-10-15..2026-10-20, @Alex-Rivera-7" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    folder = tmp_path / "money"
    venmo(folder)
    paypal(folder)
    _, first = _apply(conn, payments.scan(folder, cfg))
    assert first["added"] == 9
    plan, again = _apply(conn, payments.scan(folder, cfg))
    assert again["added"] == 0 and again["identical"] == 9
    assert plan["conflicts"] == [] and plan["updates"] == []
    text = "\n".join(payments.report_lines(payments.scan(folder, cfg), plan))
    assert "nothing new — these statements match the database" in text


def test_overlapping_statements_count_the_overlap_once(tmp_path, cfg, conn):
    folder = tmp_path / "money"
    write(folder, "VenmoStatement_Oct_2026.csv", venmo_csv(VENMO_ROWS[:2]))
    write(folder, "VenmoStatement_custom.csv", venmo_csv(VENMO_ROWS[1:3]))
    scan = payments.scan(folder, cfg)
    plan, result = _apply(conn, scan)
    assert result["added"] == 3 and plan["dup_incoming"] == 1


def test_a_later_statement_removes_nothing(tmp_path, cfg, conn):
    _apply(conn, payments.scan(venmo(tmp_path / "a"), cfg))
    later = payments.scan(venmo(tmp_path / "b", VENMO_ROWS[:1]), cfg)
    assert "present_ids" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 5


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    _apply(conn, payments.scan(venmo(tmp_path), cfg))
    for t in ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] == 0, t


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    folder = tmp_path / "money"
    venmo(folder)
    paypal(folder)
    _apply(conn, payments.scan(folder, cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_a_linked_counterparty_attributes_every_payment_with_them(tmp_path, cfg, conn):
    """What lets 'how much did I pay Jake' answer from these rows."""
    from lifecrm.domain import entities
    _apply(conn, payments.scan(venmo(tmp_path), cfg))
    jake = entities.create(conn, "person", "Jake Morales")["id"]
    obs.link_handle(conn, "app_account", "venmo:Jake Morales", jake)
    conn.commit()
    rows = conn.execute(
        "SELECT o.value FROM observations o JOIN observation_participants p "
        "ON p.observation_id = o.id JOIN entity_handles h ON h.kind = p.handle_kind "
        "AND h.value = p.handle_value WHERE h.entity_id = ? AND p.role = 'counterparty' "
        "AND o.stream = 'txn' ORDER BY o.value", (jake,)).fetchall()
    assert [r["value"] for r in rows] == [-150.0, 14.88]
    assert run_checks(conn, cfg)["failures"] == 0


# ---------------------------------------------------------------- the demo sample

ANCHOR = date(2026, 10, 21)


def _demo(tmp_path, cfg, combo):
    return [payments.scan(p, cfg) for p in sample.write_samples(tmp_path, ANCHOR, combo)]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_venmo_statement_tells_the_personas_story(tmp_path, cfg, combo):
    scan = _demo(tmp_path, cfg, combo)[0]
    got = sorted((r["journal_date"], r["value"], _parts(r)["counterparty"])
                 for r in scan["records"])
    day = lambda n: persona.day(ANCHOR, n).isoformat()      # noqa: E731
    assert got == [(day(-9), 106.2, "venmo:Maya Chen"),
                   (day(-2), -150.0, "venmo:Jake Morales"),
                   (day(-1), 14.88, "venmo:Jake Morales"),
                   (day(0), -25.0, "venmo:Sam Okafor")]
    tacos = next(r for r in scan["records"] if r["value"] == 14.88)
    assert "Veracruz" in tacos["title"]
    assert "1 × canceled transaction (only completed ones are imported)" in scan["skipped"]
    assert scan["account"] == "venmo:Alex Rivera"


def test_the_iphone_demo_adds_a_paypal_download_and_android_does_not(tmp_path, cfg):
    assert len(_demo(tmp_path / "a", cfg, "android")) == 1
    venmo_scan, paypal_scan = _demo(tmp_path / "i", cfg, "iphone")
    (gas,) = paypal_scan["records"]
    assert gas["journal_date"] == persona.day(ANCHOR, -14).isoformat()
    assert gas["value"] == 40.0 and gas["unit"] == "usd"
    assert _parts(gas)["counterparty"] == persona.person("mom").email
    assert paypal_scan["account"] == persona.OWNER.email
    assert paypal_scan["facts"]["warnings"] == []


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_demo_sample_applies_cleanly_and_reimports_identical(tmp_path, cfg, conn, combo):
    for scan in _demo(tmp_path / "a", cfg, combo):
        _apply(conn, scan)
    for scan in _demo(tmp_path / "b", cfg, combo):
        plan = obs.build_plan(conn, scan)
        assert plan["new"] == [] and plan["conflicts"] == [] and plan["identical"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_demo_sample_is_deterministic_and_refuses_an_unknown_combo(tmp_path):
    def files(root):
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}
    sample.write_samples(tmp_path / "a", ANCHOR, "iphone")
    sample.write_samples(tmp_path / "b", ANCHOR, "iphone")
    assert files(tmp_path / "a") == files(tmp_path / "b") and files(tmp_path / "a")
    with pytest.raises(ValueError, match="payments has no sample for combo 'pager'"):
        sample.write_samples(tmp_path / "c", ANCHOR, "pager")


# ---------------------------------------------------------------- CLI

def _count(where=""):
    r = runner.invoke(app, ["sql", f"SELECT COUNT(*) c FROM observations {where}", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing_and_apply_then_rerun_is_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    p = venmo(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "payments", str(p)])
    assert r.exit_code == 0, r.output
    assert "txn: 5 new" in r.output and "dry run — nothing written" in r.output
    assert _count() == 0
    r = runner.invoke(app, ["observe", "payments", str(p), "--apply"])
    assert r.exit_code == 0, r.output
    assert "5 added" in r.output
    again = runner.invoke(app, ["observe", "payments", str(p), "--apply"])
    assert "0 added" in again.output and "5 identical skipped" in again.output
    assert _count("WHERE status='active'") == 5


def test_cli_without_a_path_or_setting_says_how_to_give_one(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["observe", "payments"])
    assert r.exit_code == 1 and "payments needs a path" in r.output
