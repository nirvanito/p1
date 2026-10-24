"""Sample mail archives for the demo, telling the persona's story by email.

  android   a Google Takeout zip with Mail only — what takeout.google.com
            hands over for Gmail: Takeout/Mail/All mail Including Spam and
            Trash.mbox, one mbox with every message, each carrying Gmail's
            X-GM-THRID and X-Gmail-Labels headers, CRLF line ends
  iphone    an Apple Mail "Export Mailbox…" folder from the MacBook: one
            '<mailbox>.mbox' folder per exported mailbox (INBOX, Sent
            Messages, Junk), each holding the 'mbox' file and Apple's
            table_of_contents beside it
  windows-iphone  the Gmail Takeout again — a Windows laptop has no Apple
            Mail, and iCloud.com exports no mail — where the one reply Alex
            sent from Slovenia was written in the iPhone's Mail app (its
            X-Mailer, its quote, "Sent from my iPhone")

All carry the same sixteen messages:

- the Q4 board deck thread: Alex sends Priya the draft, Priya answers with
  the advice the journal quotes ("lead with the customer story, not the TAM
  slide"), Alex replies — Re: subjects, a quoted reply under each answer,
  Priya's '-- ' signature;
- Priya's calendar invitation to the board meeting (kept as an email), and
  her note after it, copied to the product team: the board loved it;
- Ellis Dental's appointment reminder from frontdesk@ellisdental.example and
  Alex's reply confirming it;
- Jake's tips for Lake Bled four years ago, sent on the first day of the trip
  (persona.bled_trip_days) — the pletna boat, Vintgar Gorge, the cream cake
  at the Park Hotel — and Alex's answer from Slovenia, dated in Slovenia's
  zone;
- the airline's confirmation for those flights (HTML only, the total the card
  statement shows);
- photos of Maya's birthday sent to Mom, copied to Maya, and Mom's reply
  "Sent from my iPad";
- a note Alex mailed to themself about booking flights to Ljubljana, and
  Sam's email about the new climbing gym;
- what a real archive also holds and the importer must not keep: a gym
  newsletter (List-Unsubscribe, Precedence: bulk) and a spam message
  (Gmail's Spam label; Apple Mail's Junk mailbox).

On the Mac the note to self sits in both INBOX and Sent Messages — the same
Message-ID twice, as Apple Mail keeps it — and is imported once.

What the persona leaves open — the hour of each message, the subjects, who
writes with which mail app and how that app quotes — is fixed here,
consistent with the texts and the journal: Priya's advice lands at 08:47 the
morning the journal records it (her text follows at 09:02), and the board
note at 16:20, before her text.

Deterministic for (anchor, combo): fixed MIME boundaries, ids derived from
each message's key, fixed zip timestamps and file times.
"""

from __future__ import annotations

import base64
import email.header
import email.utils
import hashlib
import os
import quopri
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

TAKEOUT_MBOX = "Takeout/Mail/All mail Including Spam and Trash.mbox"
EXPORT_FOLDER = "Apple Mail export"
NEWSLETTER = ("Movement Gym", "news@movementgym.example")
SPAM = ("Rewards Center", "prizes@winner-alerts.example")
AIRLINE = ("Air France", "booking@airfrance.example")
PRODUCT_TEAM = ("Product Team", "product-team@acme-robotics.example")
BOOKING_REFERENCE = "K7Q2LM"


@dataclass(frozen=True)
class Mail:
    key: str
    when: tuple                 # (offset, 'HH:MM') at home; ('trip', day, 'HH:MM') in
                                # Slovenia; ('booked', days before the trip, 'HH:MM')
    sender: str                 # person key, 'me', or an outside sender key
    to: tuple[str, ...]
    subject: str
    text: str
    cc: tuple[str, ...] = ()
    reply_to: str | None = None   # the message this answers (and quotes)
    kind: str = "plain"         # plain | alternative | html | invite | newsletter | spam
    attachment: tuple | None = None   # (file name, mime type, size in bytes)
    signature: str | None = None


_OUTSIDE = {"newsletter": NEWSLETTER, "spam": SPAM, "airline": AIRLINE,
            "team": PRODUCT_TEAM}

# Which mail app each writer uses; it decides how a reply quotes and signs.
# Alex writes from Gmail on the Samsung setup and from Apple Mail on the Mac.
_CLIENT = {"priya": "gmail", "jake": "gmail", "sam": "gmail", "mom": "ipad",
           "ellis": "office"}

_PRIYA_SIGNATURE = "Priya Natarajan\nVP Product, Acme Robotics\n+1 512-555-0123"


def _mails(anchor: date) -> tuple[Mail, ...]:
    trip = persona.bled_trip_days(anchor)
    appointment = persona.day(anchor, -12)
    board = persona.day(anchor, -20)
    outbound = trip[0] - timedelta(days=1)
    return (
        Mail("deck_draft", (-28, "17:40"), "me", ("priya",),
             "Q4 board deck \N{EM DASH} draft v3 for review",
             "Hi Priya,\n\nDraft v3 of the Q4 board deck is attached. The market sizing "
             "section is done; I'm least sure about the opening.\n\nWould love your "
             "read before the board meeting.\n\nThanks,\nAlex",
             attachment=("Q4 Board Deck v3.pdf", "application/pdf", 48_212)),
        Mail("priya_advice", (-27, "08:47"), "priya", ("me",),
             "Re: Q4 board deck \N{EM DASH} draft v3 for review",
             "Alex,\n\nRead it last night. My one piece of advice: lead with the customer "
             "story, not the TAM slide. Open on the warehouse pilot \N{EM DASH} what "
             "broke, what we shipped, what changed for them \N{EM DASH} and only then "
             "the three metrics.\n\nPush the market sizing to the appendix. If the "
             "board wants it, they'll ask.\n\nPriya",
             reply_to="deck_draft", signature=_PRIYA_SIGNATURE),
        Mail("deck_thanks", (-27, "09:20"), "me", ("priya",),
             "Re: Q4 board deck \N{EM DASH} draft v3 for review",
             "Makes sense. Rewriting the opening around the warehouse story today.\n\n"
             "Thanks, Priya!\nAlex",
             reply_to="priya_advice"),
        Mail("board_invite", (-30, "11:00"), "priya", ("me",),
             f"Invitation: Board meeting: Q4 deck @ {board:%a %b} {board.day}, "
             f"{board.year} 2pm - 3:30pm (CDT) ({persona.OWNER.email})",
             f"Board meeting: Q4 deck\n{board:%A %b} {board.day}, {board.year} \N{EN DASH} "
             f"2pm \N{EN DASH} 3:30pm (Central Time)\nAcme Robotics office, 500 W 2nd St, "
             f"Austin, TX\n\nQ4 product update to the board.\n\nInvitation from Google "
             f"Calendar",
             kind="invite"),
        Mail("board_loved_it", (-20, "16:20"), "priya", ("me",),
             "Board recap: Q4 deck",
             "Alex,\n\nThe board loved it. Leading with the warehouse customer story was "
             "the right call \N{EM DASH} two members asked for the pilot numbers "
             "afterwards. Nice work.\n\nPriya",
             cc=("team",), signature=_PRIYA_SIGNATURE),
        Mail("dentist_reminder", (-14, "08:30"), "ellis", ("me",),
             f"Appointment reminder: {appointment:%A, %B} {appointment.day} at 10:30 AM",
             f"Hi Alex,\n\nThis is a reminder of your appointment with Dr. Rebecca Ellis "
             f"on {appointment:%A, %B} {appointment.day} at 10:30 AM at Ellis Dental.\n\n"
             f"Please reply to confirm, or call us at (512) 555-0199 to reschedule.\n\n"
             f"Ellis Dental",
             kind="alternative"),
        Mail("dentist_confirm", (-14, "09:12"), "me", ("ellis",),
             f"Re: Appointment reminder: {appointment:%A, %B} {appointment.day} at "
             f"10:30 AM",
             "Confirmed \N{EM DASH} see you then. Thanks!\n\nAlex",
             reply_to="dentist_reminder"),
        Mail("flights_booked", ("booked", 41, "21:14"), "airline", ("me",),
             f"Your booking is confirmed \N{EN DASH} Austin to Ljubljana "
             f"({BOOKING_REFERENCE})",
             _flight_text(outbound, trip[4]), kind="html"),
        # 14:30 in Slovenia is 07:30 at home: the trip's first journal day.
        Mail("jake_tips", ("trip", 0, "14:30"), "jake", ("me",),
             "Bled tips",
             "You get to Bled a day before me, so here's my list from when I went with "
             "my family:\n\n- Take a pletna boat out to the island. Bring euro cash, "
             "they don't take cards.\n- Do Vintgar Gorge early in the morning, before "
             "the tour buses.\n- Get the cream cake (krem\N{LATIN SMALL LETTER S WITH CARON}"
             "nita) at the Park Hotel caf\N{LATIN SMALL LETTER E WITH ACUTE}. "
             "Non-negotiable.\n\nSee you at the lake tomorrow!\nJake"),
        Mail("jake_tips_reply", ("trip", 0, "21:15"), "me", ("jake",),
             "Re: Bled tips",
             "Landed. Pletna boat when you're here, Vintgar Gorge first thing the day "
             "after. Saving the cream cake for when you arrive.\n\nSee you tomorrow!\n"
             "Alex",
             reply_to="jake_tips"),
        Mail("birthday_photos", (-8, "10:05"), "me", ("mom",),
             "Photos from Maya's birthday",
             "Hi Mom,\n\nA few photos from Maya's birthday dinner at Uchi. She loved the "
             "ceramics class voucher.\n\nLove,\nAlex",
             cc=("maya",), attachment=("maya_birthday.jpg", "image/jpeg", 61_440)),
        Mail("mom_reply", (-8, "18:30"), "mom", ("me",),
             "Re: Photos from Maya's birthday",
             "Qu\N{LATIN SMALL LETTER E WITH ACUTE} bonita! Tell Maya happy birthday "
             "again from me. When are you two coming to San Antonio?\n\nLove, Mom",
             cc=("maya",), reply_to="birthday_photos"),
        Mail("flights_note", (-2, "13:10"), "me", ("me",),
             "Flights to Ljubljana",
             "Book before prices go up: AUS to LJU for early June, via Paris or "
             "Frankfurt. Last time was Air France, $1,124.60, booking reference "
             f"{BOOKING_REFERENCE}."),
        Mail("sam_gym", (0, "07:40"), "sam", ("me",),
             "New climbing gym Saturday?",
             "Saw that the new bouldering gym on the east side opens this weekend. "
             "Saturday morning? I'll drive.\n\nSam"),
        Mail("newsletter", (-6, "07:00"), "newsletter", ("me",),
             "This week at Movement: new routes and member yoga",
             "New routes went up in the bouldering cave this week, and member yoga is "
             "back on Thursday evenings.\n\nSee you on the wall,\nThe Movement team",
             kind="newsletter"),
        Mail("spam", (-4, "03:12"), "spam", ("me",),
             "Congratulations! Your $500 gift card is waiting",
             "You have been selected to receive a $500 gift card. Claim it within 24 "
             "hours: verify your account details at the link below.",
             kind="spam"),
    )


def _flight_text(outbound: date, back: date) -> str:
    """The airline's confirmation, as the HTML part's text (rendered below)."""
    arrive = outbound + timedelta(days=1)
    return "\n".join((
        f"Booking reference: {BOOKING_REFERENCE}",
        "Passenger: ALEX RIVERA",
        f"Outbound {outbound:%a %d %b %Y}: Austin (AUS) 17:05 \N{RIGHTWARDS ARROW} Paris "
        f"(CDG) 09:10, then Paris (CDG) 11:25 \N{RIGHTWARDS ARROW} Ljubljana (LJU) 13:15 "
        f"on {arrive:%a %d %b}",
        f"Return {back:%a %d %b %Y}: Ljubljana (LJU) 14:05 \N{RIGHTWARDS ARROW} Paris "
        f"(CDG) 16:00, then Paris (CDG) 18:30 \N{RIGHTWARDS ARROW} Austin (AUS) 22:15",
        "Total paid: USD 1,124.60",
    ))


# ---------------------------------------------------------------- people and times

def _who(key: str) -> tuple[str, str]:
    if key in _OUTSIDE:
        return _OUTSIDE[key]
    p = persona.person(key)
    return (p.org if key == "ellis" else p.name), p.email


def _dt(m: Mail, anchor: date) -> datetime:
    if m.when[0] == "trip":
        return persona.trip_dt(m.when[1], m.when[2], anchor)
    if m.when[0] == "booked":
        d = persona.bled_trip_days(anchor)[0] - timedelta(days=m.when[1])
        hh, mm = (int(x) for x in m.when[2].split(":"))
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(persona.TIMEZONE))
    return persona.local_dt(anchor, m.when[0], m.when[1])


def _digest(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def message_id(m: Mail) -> str:
    """The Message-ID, without brackets: the same on both combos."""
    h = _digest("message", m.key)
    domain = _who(m.sender)[1].split("@", 1)[1]
    return f"{h[:16]}.{h[16:28]}@{domain}"


def _thread_root(m: Mail, by_key: dict) -> Mail:
    while m.reply_to:
        m = by_key[m.reply_to]
    return m


def _references(m: Mail, by_key: dict) -> list[str]:
    chain = []
    cur = m
    while cur.reply_to:
        cur = by_key[cur.reply_to]
        chain.append(message_id(cur))
    return list(reversed(chain))


def _client(m: Mail, combo: str) -> str:
    if m.sender == "me":
        if combo == "windows-iphone":
            # Gmail in the laptop's browser; from Slovenia, the iPhone's Mail.
            return "iphone" if m.when[0] == "trip" else "gmail"
        return "gmail" if combo == "android" else "apple"
    return _CLIENT.get(m.sender, "plain")


# ---------------------------------------------------------------- MIME

def _addr(key: str) -> str:
    display, addr = _who(key)
    return email.utils.formataddr((display, addr))


def _header(text: str) -> str:
    if text.isascii():
        return text
    return email.header.Header(text, "utf-8", maxlinelen=10_000).encode()


def _qp(text: str) -> str:
    return quopri.encodestring(text.encode("utf-8")).decode("ascii")


def _b64(data: bytes) -> str:
    raw = base64.b64encode(data).decode("ascii")
    return "\n".join(raw[i:i + 76] for i in range(0, len(raw), 76))


def _blob(key: str, size: int, head: bytes, tail: bytes) -> bytes:
    out = bytearray(head)
    seed = key.encode("utf-8")
    while len(out) < size - len(tail):
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:size - len(tail)]) + tail


def _attachment_bytes(key: str, mime: str, size: int) -> bytes:
    if mime == "application/pdf":
        return _blob(key, size, b"%PDF-1.4\n", b"\n%%EOF\n")
    return _blob(key, size, b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", b"\xff\xd9")


def _quote(parent: Mail, client: str, anchor: date) -> str:
    """The reply's quote of its parent, as the replier's mail app writes it."""
    when = _dt(parent, anchor)
    display, addr = _who(parent.sender)
    lines = _full_text(parent).split("\n")
    quoted = "\n".join(("> " + ln).rstrip() for ln in lines)
    if client == "gmail":
        # Gmail wraps a long attribution line; the address moves to a second line.
        hour = when.strftime("%I:%M %p").lstrip("0")
        head = f"On {when:%a, %b} {when.day}, {when.year} at {hour} {display} <{addr}>"
        if len(head) + len(" wrote:") > 76:
            return f"{head}\nwrote:\n\n{quoted}"
        return f"{head} wrote:\n\n{quoted}"
    if client in ("apple", "ipad", "iphone"):
        return (f"> On {when:%b} {when.day}, {when.year}, at {when:%H:%M}, {display} "
                f"<{addr}> wrote:\n> \n{quoted}")
    return (f"-----Original Message-----\nFrom: {display} <{addr}>\n"
            f"Sent: {when:%A, %B} {when.day}, {when.year} {when:%I:%M %p}\n"
            f"Subject: {parent.subject}\n\n{_full_text(parent)}")


def _full_text(m: Mail) -> str:
    """What the sender wrote plus their signature — what a reply quotes."""
    return m.text + (f"\n\n-- \n{m.signature}" if m.signature else "")


def _body_text(m: Mail, combo: str, by_key: dict, anchor: date) -> str:
    client = _client(m, combo)
    text = _full_text(m)
    if (client in ("apple", "ipad") and m.sender == "me" and combo == "iphone"
            and m.when[0] == "trip") or client == "iphone":
        text += "\n\nSent from my iPhone"
    if client == "ipad":
        text += "\n\nSent from my iPad"
    if m.reply_to:
        text += "\n\n" + _quote(by_key[m.reply_to], client, anchor)
    return text


def _html_of(text: str) -> str:
    paras = text.split("\n\n")
    return ("<html><head><style>p{margin:0 0 12px}</style></head><body>"
            + "".join("<p>" + _escape(p).replace("\n", "<br>") + "</p>" for p in paras)
            + "</body></html>")


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _flight_html(m: Mail) -> str:
    rows = m.text.split("\n")
    cells = "".join(f"<tr><td>{_escape(r.split(': ', 1)[0])}</td><td>"
                    f"{_escape(r.split(': ', 1)[1])}</td></tr>" for r in rows)
    return ("<!DOCTYPE html><html><head><title>Booking confirmation</title><style>"
            "td{padding:4px 8px}</style><script>var t=1;</script></head><body>"
            "<h1>Your booking is confirmed</h1><p>Thank you for flying with us, "
            "Alex.</p><table>" + cells + "</table><p>Have a pleasant flight.</p>"
            "</body></html>")


def _ics(m: Mail, anchor: date) -> str:
    board = persona.local_dt(anchor, -20, "14:00").astimezone(timezone.utc)
    end = board + timedelta(minutes=90)
    stamp = _dt(m, anchor).astimezone(timezone.utc)
    return "\r\n".join((
        "BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
        "VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"DTSTART:{board:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}",
        f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
        f"ORGANIZER;CN=Priya Natarajan:mailto:{persona.person('priya').email}",
        "UID:demo-board-q4@google.com",
        f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
        f"CN={persona.OWNER.email}:mailto:{persona.OWNER.email}",
        "SUMMARY:Board meeting: Q4 deck", "LOCATION:Acme Robotics office",
        "DESCRIPTION:Q4 product update to the board", "STATUS:CONFIRMED",
        "END:VEVENT", "END:VCALENDAR", ""))


def _boundary(m: Mail, n: int) -> str:
    return f"000000000000{_digest('boundary', m.key, str(n))[:16]}"


def _render(m: Mail, combo: str, by_key: dict, anchor: date) -> tuple[list[str], str]:
    """(headers, body) of one message, '\n' line ends."""
    when = _dt(m, anchor)
    heads = ["MIME-Version: 1.0", f"Date: {email.utils.format_datetime(when)}",
             f"Message-ID: <{message_id(m)}>", f"Subject: {_header(m.subject)}",
             f"From: {_addr(m.sender)}", "To: " + ", ".join(_addr(k) for k in m.to)]
    if m.cc:
        heads.append("Cc: " + ", ".join(_addr(k) for k in m.cc))
    if m.reply_to:
        refs = _references(m, by_key)
        heads += [f"In-Reply-To: <{refs[-1]}>", "References: " + " ".join(f"<{r}>" for r in refs)]
    client = _client(m, combo)
    if client == "apple":
        heads.append("X-Mailer: Apple Mail (2.3826.400.131.1.6)")
    elif client == "ipad":
        heads.append("X-Mailer: iPad Mail (22A3354)")
    elif client == "iphone":
        heads.append("X-Mailer: iPhone Mail (19G82)")     # the iOS of four years back
    if m.kind == "newsletter":
        heads += ["List-Id: Movement Gym News <news.movementgym.example>",
                  "List-Unsubscribe: <mailto:unsubscribe@movementgym.example?subject="
                  "unsubscribe>, <https://movementgym.example/u/7f3a9c>",
                  "Precedence: bulk"]

    text = _body_text(m, combo, by_key, anchor)
    plain = ['Content-Type: text/plain; charset="UTF-8"',
             "Content-Transfer-Encoding: quoted-printable", "", _qp(text)]
    if m.sender == "jake":                         # Gmail picks base64 for some mail
        plain = ['Content-Type: text/plain; charset="UTF-8"',
                 "Content-Transfer-Encoding: base64", "", _b64(text.encode("utf-8"))]

    if m.kind == "html":
        part = ['Content-Type: text/html; charset="UTF-8"',
                "Content-Transfer-Encoding: quoted-printable", "", _qp(_flight_html(m))]
        return heads + part[:2], "\n".join(part[3:])
    if m.kind in ("alternative", "newsletter"):
        b = _boundary(m, 0)
        html = ['Content-Type: text/html; charset="UTF-8"',
                "Content-Transfer-Encoding: quoted-printable", "", _qp(_html_of(text))]
        body = "\n".join([f"--{b}", *plain, f"--{b}", *html, f"--{b}--", ""])
        return heads + [f'Content-Type: multipart/alternative; boundary="{b}"'], body
    if m.kind == "invite":
        outer, inner = _boundary(m, 0), _boundary(m, 1)
        ics = _ics(m, anchor)
        cal = ['Content-Type: text/calendar; charset="UTF-8"; method=REQUEST',
               "Content-Transfer-Encoding: 7bit", "", ics.replace("\r\n", "\n")]
        att = ['Content-Type: application/ics; name="invite.ics"',
               'Content-Disposition: attachment; filename="invite.ics"',
               "Content-Transfer-Encoding: base64", "", _b64(ics.encode("utf-8"))]
        body = "\n".join([f"--{outer}", f'Content-Type: multipart/alternative; '
                          f'boundary="{inner}"', "", f"--{inner}", *plain, f"--{inner}",
                          *cal, f"--{inner}--", "", f"--{outer}", *att, f"--{outer}--", ""])
        return heads + [f'Content-Type: multipart/mixed; boundary="{outer}"'], body
    if m.attachment:
        b = _boundary(m, 0)
        fname, mime, size = m.attachment
        data = _attachment_bytes(m.key, mime, size)
        att = [f'Content-Type: {mime}; name="{fname}"',
               f'Content-Disposition: attachment; filename="{fname}"',
               "Content-Transfer-Encoding: base64", "", _b64(data)]
        body = "\n".join([f"--{b}", *plain, f"--{b}", *att, f"--{b}--", ""])
        return heads + [f'Content-Type: multipart/mixed; boundary="{b}"'], body
    return heads + plain[:2], "\n".join(plain[3:])


def _escape_from(body: str) -> str:
    """mboxrd: a body line that starts with 'From ' (or '>From ') gets one
    more '>', so it never reads as a separator."""
    return re.sub(r"(?m)^(>*From )", r">\1", body)


# ---------------------------------------------------------------- Gmail Takeout

def _gmail_labels(m: Mail) -> str:
    if m.kind == "spam":
        return "Spam,Unread"
    if m.kind == "newsletter":
        return "Inbox,Category Promotions,Unread"
    if m.sender == "me":
        return "Inbox,Sent,Opened" if "me" in m.to else "Sent"
    if m.when[0] in ("trip", "booked"):
        return "Archived,Opened,Category Updates" if m.sender == "airline" \
            else "Archived,Opened"
    return "Inbox,Opened,Important" if m.sender == "priya" else "Inbox,Opened"


def _gmail_id(*parts: str) -> str:
    """A Gmail-shaped 19-digit id (X-GM-THRID, X-GM-MSGID)."""
    return str(1_700_000_000_000_000_000 + int(_digest(*parts)[:15], 16) % 10**17)


def _takeout(dest: Path, anchor: date, combo: str = "android") -> Path:
    mails = _mails(anchor)
    by_key = {m.key: m for m in mails}
    chunks = []
    for m in sorted(mails, key=lambda x: (_dt(x, anchor).astimezone(timezone.utc), x.key)):
        when = _dt(m, anchor).astimezone(timezone.utc)
        heads, body = _render(m, combo, by_key, anchor)
        gmail = [f"X-GM-THRID: {_gmail_id('thread', _thread_root(m, by_key).key)}",
                 f"X-Gmail-Labels: {_gmail_labels(m)}"]
        if m.sender != "me":
            # Google's servers stamp delivery in Pacific time, two seconds later.
            delivered = (when + timedelta(seconds=2)).astimezone(ZoneInfo("America/Los_Angeles"))
            gmail += [f"Delivered-To: {persona.OWNER.email}",
                      f"Received: by 2002:a05:7300:5c0b:b0:13d:9e1f with SMTP id "
                      f"{_digest('smtp', m.key)[:12]}; "
                      f"{email.utils.format_datetime(delivered)}"]
        sep = f"From {_gmail_id('message', m.key)}@xxx {when:%a %b %d %H:%M:%S} +0000 {when:%Y}"
        text = "\n".join([sep, *gmail, *heads, "", _escape_from(body)]).rstrip("\n") + "\n\n"
        chunks.append(text.replace("\n", "\r\n"))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"takeout-{anchor:%Y%m%d}T120000Z-001.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 12, 0, 0)

    def put(zf: zipfile.ZipFile, member: str, data: bytes) -> None:
        info = zipfile.ZipInfo(member, date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, data)

    with zipfile.ZipFile(out, "w") as zf:
        put(zf, "Takeout/archive_browser.html",
            ("<!DOCTYPE html><html><head><title>Your Google data archive</title></head>"
             f"<body><h1>Archive for {persona.OWNER.email}</h1><p>Mail: messages and "
             "attachments in your Gmail account in MBOX format.</p></body></html>"
             ).encode("utf-8"))
        put(zf, TAKEOUT_MBOX, "".join(chunks).encode("utf-8"))
    _touch(out, anchor)
    return out


# ---------------------------------------------------------------- Apple Mail export

def _apple_mailboxes(m: Mail) -> tuple[str, ...]:
    if m.kind == "spam":
        return ("Junk",)
    if m.sender == "me":
        # Mail to yourself is in both: sent, and received.
        return ("INBOX", "Sent Messages") if "me" in m.to else ("Sent Messages",)
    return ("INBOX",)


def _apple_export(dest: Path, anchor: date) -> Path:
    mails = _mails(anchor)
    by_key = {m.key: m for m in mails}
    root = dest / EXPORT_FOLDER
    boxes: dict[str, list[str]] = {}
    for m in sorted(mails, key=lambda x: (_dt(x, anchor).astimezone(timezone.utc), x.key)):
        heads, body = _render(m, "iphone", by_key, anchor)
        local = _dt(m, anchor)
        sep = f"From {_who(m.sender)[1]} {local:%a %b %d %H:%M:%S %Y}"
        text = "\n".join([sep, *heads, "", _escape_from(body)]).rstrip("\n") + "\n\n"
        for box in _apple_mailboxes(m):
            boxes.setdefault(box, []).append(text)
    for box, texts in sorted(boxes.items()):
        folder = root / f"{box}.mbox"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mbox").write_bytes("".join(texts).encode("utf-8"))
        # Apple's index of the export, a binary file the importer leaves alone.
        (folder / "table_of_contents").write_bytes(
            b"\x00\x00\x00\x02" + hashlib.sha256(box.encode()).digest())
        _touch(folder / "mbox", anchor)
        _touch(folder / "table_of_contents", anchor)
    return root


def _touch(path: Path, anchor: date) -> None:
    """A fixed file time (the export's), so two builds read the same."""
    t = datetime(anchor.year, anchor.month, anchor.day, 12, tzinfo=timezone.utc).timestamp()
    os.utime(path, (t, t))


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo == "android":
        return _takeout(Path(dest), anchor)
    if combo == "iphone":
        return _apple_export(Path(dest), anchor)
    if combo == "windows-iphone":
        return _takeout(Path(dest), anchor, combo)
    raise ValueError(f"email has no sample for combo '{combo}' "
                     f"(expected one of {', '.join(persona.COMBOS)})")


def kept_keys() -> tuple[str, ...]:
    """The messages an import keeps: all but the newsletter and the spam."""
    return tuple(m.key for m in _mails(date(2026, 10, 22))
                 if m.kind not in ("newsletter", "spam"))
