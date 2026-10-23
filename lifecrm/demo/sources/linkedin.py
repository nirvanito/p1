"""LinkedIn sample: the persona's professional network, as LinkedIn's data
export hands it over.

A zip named the way LinkedIn names the "fast" (Basic) export,
'Basic_LinkedInDataExport_<MM-DD-YYYY>.zip', holding:

  Connections.csv   LinkedIn's "Notes:" preamble about missing emails, then
                    First Name,Last Name,URL,Email Address,Company,Position,
                    Connected On — Priya (VP Product at Acme Robotics), Jake
                    (Software Engineer at Hill Country Software) and Sam
                    (Physical Therapist at Capital City Physio); Sam hides his
                    email, as most connections do
  messages.csv      Priya's note the morning after the board meeting
                    ("Congrats on the board deck") — stored as HTML, the way
                    LinkedIn keeps rich messages — and Alex's reply
  Invitations.csv   the invitation Alex sent Sam (it names Alex's own profile)
  Profile.csv       Alex's profile
  Positions.csv,    two of the files the connector does not read, so the
  Skills.csv        report has something to count

The job titles and Jake's and Sam's employers are this generator's own
story (persona.py gives Priya's employer only); everything else is the
persona's. Priya's contact card (sources/contacts.py, last edited weeks
ago) still says Director of Product: LinkedIn, exported today, has her
promotion — two observed sources of two different moments, each cited as
what it said when. LinkedIn is the same website on both combos, so both get the
same export. Fictional profile URLs throughout.
"""

from __future__ import annotations

import base64
import csv
import io
import zipfile
from datetime import date, timedelta, timezone
from pathlib import Path

from .. import persona

OWNER_URL = "https://www.linkedin.com/in/alex-rivera-pm"

# person key -> (profile slug, company, position, years connected, day shift,
#                shares email)
CONNECTIONS = {
    "priya": ("priya-natarajan-acme", "Acme Robotics", "VP Product", 3, 41, True),
    "jake": ("jakemorales-dev", "Hill Country Software", "Software Engineer", 9, 130, True),
    "sam": ("sam-okafor-pt", "Capital City Physio", "Physical Therapist", 2, 98, False),
}

_NOTE = ("When exporting your connection data, you may notice that some of the email "
         "addresses are missing. You will only see email addresses for connections who "
         "have allowed their connections to see or download their email address using "
         "this setting https://www.linkedin.com/psettings/privacy/email. You can learn "
         "more about this setting here https://www.linkedin.com/help/linkedin/answer/261")

# (anchor offset, local time, sender key, content)
MESSAGES = (
    (-19, "08:40", "priya",
     "<p>Congrats on the board deck, Alex! Leading with the customer story was "
     "exactly right &amp; the board noticed.</p><p>Proud to have you on the team.</p>"),
    (-19, "09:12", "me",
     "Thank you, Priya. Your advice to open with the warehouse story made it."),
)


def profile_url(key: str) -> str:
    if key == "me":
        return OWNER_URL
    return f"https://www.linkedin.com/in/{CONNECTIONS[key][0]}"


def connected_on(key: str, anchor: date) -> date:
    _, _, _, years, shift, _ = CONNECTIONS[key]
    try:
        d = anchor.replace(year=anchor.year - years)
    except ValueError:                       # Feb 29
        d = anchor.replace(year=anchor.year - years, day=28)
    return d - timedelta(days=shift)


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def _connections(anchor: date) -> bytes:
    rows = [["Notes:"], [_NOTE], [],
            ["First Name", "Last Name", "URL", "Email Address", "Company", "Position",
             "Connected On"]]
    # LinkedIn lists the newest connection first.
    for key in sorted(CONNECTIONS, key=lambda k: connected_on(k, anchor), reverse=True):
        p = persona.person(key)
        _, company, position, _, _, shares = CONNECTIONS[key]
        rows.append([p.first, p.last, profile_url(key), p.email if shares else "",
                     company, position, connected_on(key, anchor).strftime("%d %b %Y")])
    return _csv(rows)


def _conversation_id(anchor: date) -> str:
    raw = f"demo-conversation-priya-{anchor.isoformat()}".encode("ascii")
    return "2-" + base64.b64encode(raw).decode("ascii")


def _messages(anchor: date) -> bytes:
    rows = [["CONVERSATION ID", "CONVERSATION TITLE", "FROM", "SENDER PROFILE URL", "TO",
             "RECIPIENT PROFILE URLS", "DATE", "SUBJECT", "CONTENT", "FOLDER"]]
    conv = _conversation_id(anchor)
    for offset, hhmm, sender, content in sorted(MESSAGES, reverse=True):
        other = "priya" if sender == "me" else "me"
        when = persona.local_dt(anchor, offset, hhmm).astimezone(timezone.utc)
        rows.append([conv, "", persona.person(sender).name, profile_url(sender),
                     persona.person(other).name, profile_url(other),
                     when.strftime("%Y-%m-%d %H:%M:%S UTC"), "", content, "INBOX"])
    return _csv(rows)


def _invitations(anchor: date) -> bytes:
    sent = connected_on("sam", anchor) - timedelta(days=1)
    return _csv([
        ["From", "To", "Sent At", "Message", "Direction", "inviterProfileUrl",
         "inviteeProfileUrl"],
        [persona.OWNER.name, persona.person("sam").name,
         f"{sent.month}/{sent.day}/{sent:%y}, 7:02 PM", "Great lifting with you!",
         "OUTGOING", OWNER_URL, profile_url("sam")]])


def _profile() -> bytes:
    me = persona.OWNER
    return _csv([
        ["First Name", "Last Name", "Maiden Name", "Address", "Birth Date", "Headline",
         "Summary", "Industry", "Zip Code", "Geo Location", "Twitter Handles", "Websites",
         "Instant Messengers"],
        [me.first, me.last, "", "", "", f"Product Manager at {me.org}",
         "Building robots that warehouses love.", "Automation Machinery Manufacturing",
         "78702", "Austin, Texas, United States", "", "", ""]])


def _positions(anchor: date) -> bytes:
    since = connected_on("priya", anchor)
    return _csv([
        ["Company Name", "Title", "Description", "Location", "Started On", "Finished On"],
        [persona.OWNER.org, "Product Manager", "", "Austin, Texas, United States",
         since.strftime("%b %Y"), ""]])


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The export zip — what the owner would point `lifecrm observe linkedin` at."""
    if combo not in persona.COMBOS:
        raise ValueError(f"linkedin has no sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    folder = Path(dest)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Basic_LinkedInDataExport_{anchor:%m-%d-%Y}.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 6, 0, 0)
    members = (("Connections.csv", _connections(anchor)),
               ("Invitations.csv", _invitations(anchor)),
               ("messages.csv", _messages(anchor)),
               ("Positions.csv", _positions(anchor)),
               ("Profile.csv", _profile()),
               ("Skills.csv", _csv([["Name"], ["Product Management"], ["Robotics"]])))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, raw in members:
            info = zipfile.ZipInfo(member, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, raw)
    return path
