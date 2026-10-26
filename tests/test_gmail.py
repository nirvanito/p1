"""Gmail online source (lifecrm/online/gmail.py): IMAP with an app password,
read-only and incremental — and the very same collect flow after it: the
first import asked about, a yes, routine mornings, a quiet one unchanged, a
failed fetch said rather than skipped.

The network is never touched. `gmail._imap` is handed a fake imap.gmail.com
that speaks the protocol to the real imaplib — greeting, CAPABILITY, LOGIN,
LIST with the \\All flag, EXAMINE with UIDVALIDITY and UIDNEXT, UID SEARCH,
UID FETCH with literals, BODYSTRUCTURE and BODY.PEEK sections — so what is
tested is what imaplib really hands the module. Anything that would change
the mailbox (SELECT, STORE, a FETCH without PEEK, ...) is refused and
written down, and every test ends by checking nothing was.
"""

from __future__ import annotations

import base64
import imaplib
import json
import re
import socket
from email import policy
from email.parser import BytesParser

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import collect, online
from lifecrm.cli import app
from lifecrm.connectors import email as em
from lifecrm.domain.util import DomainError
from lifecrm.online import gmail, secrets
from test_collect import home  # noqa: F401  (the autouse home isolation)

runner = CliRunner()

ADDRESS = "alex.rivera@gmail.com"
ALIAS = "alex@work.example"
APP_PASSWORD = "abcdwxyzefghqrst"
TYPED = "abcd wxyz efgh qrst"            # as Google shows it
PRIYA = "priya@acme-robotics.example"
JAKE = "jake.morales@example.com"
THREAD = 1781234567890123456


def letter(subject: str, body: str, *, sender=f"Priya Natarajan <{PRIYA}>",
           to=f"Alex Rivera <{ADDRESS}>", msgid=None, delivered=True, extra=()) -> bytes:
    """One message as Gmail stores it: CRLF, Delivered-To on received mail."""
    msgid = msgid or "<" + re.sub(r"\W+", "-", subject.lower()).strip("-") + "@example.com>"
    lines = ([f"Delivered-To: {ADDRESS}"] if delivered else []) + [
        "Received: by 2002:a05:6a10:8a0e with SMTP id x; Tue, 20 Oct 2026 07:15:01 -0700",
        "Date: Tue, 20 Oct 2026 09:15:00 -0500", f"Message-ID: {msgid}",
        f"Subject: {subject}", f"From: {sender}", f"To: {to}", "MIME-Version: 1.0", *extra]
    if not any(x.lower().startswith("content-type:") for x in extra):
        lines.append('Content-Type: text/plain; charset="UTF-8"')
    text = "\r\n".join(lines) + "\r\n\r\n" + body + "\n"
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


DECK = letter("Q4 deck", "Lead with the customer story, not the TAM slide.\n\nPriya")
REPLY = letter("Re: Q4 deck", "Thanks, will do.", sender=f"Alex Rivera <{ADDRESS}>",
               to=f"Priya Natarajan <{PRIYA}>", delivered=False, msgid="<reply@gmail.com>",
               extra=("In-Reply-To: <q4-deck@example.com>",))
TAXES = letter("Taxes", "The 1099s are in.", sender=f"Sam Okafor <sam@example.com>")


# ---------------------------------------------------------------- the fake server

class FakeGmail:
    """imap.gmail.com, as much of it as the fetch speaks: one account, All
    Mail by its \\All flag (renamed per language), UIDs, labels and thread
    ids, and every command written down."""

    def __init__(self):
        self.password = APP_PASSWORD
        self.all_mail = "[Gmail]/All Mail"
        self.folders = {"INBOX": r"(\HasNoChildren)", "[Gmail]": r"(\HasChildren \Noselect)",
                        self.all_mail: r"(\All \HasNoChildren)",
                        "[Gmail]/Sent Mail": r"(\HasNoChildren \Sent)",
                        "[Gmail]/Trash": r"(\HasNoChildren \Trash)"}
        self.uidvalidity = 11
        self.messages: list[dict] = []
        self.next_uid = 1
        self.log: list[str] = []
        self.refused: list[str] = []
        self.down: Exception | None = None     # what connecting raises
        self.break_after: int | None = None    # FETCHes answered before the line drops
        self.chatty = False                    # flag updates nobody asked for, as
                                               # a server may send them

    def add(self, raw: bytes, labels=(), thrid=None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages.append({"uid": uid, "raw": raw, "labels": list(labels),
                              "thrid": thrid or 1780000000000000000 + uid})
        return uid

    def renumber(self) -> None:
        """What Gmail does, rarely, to a folder: a new UIDVALIDITY, new UIDs."""
        self.uidvalidity += 1
        for i, m in enumerate(self.messages, 101):
            m["uid"] = i
        self.next_uid = 101 + len(self.messages)

    def connect(self, host: str):
        assert host == "imap.gmail.com"
        if self.down is not None:
            raise self.down
        return _Wire(self, host)

    # ------------------------------------------------------------ commands

    def answer(self, line: bytes, session: dict) -> bytes:
        text = line.decode("ascii")
        self.log.append(text)
        if session.get("dead"):
            return b""
        tag, _, rest = text.partition(" ")
        verb, _, args = rest.partition(" ")
        verb = verb.upper()
        if verb == "UID":
            sub, _, args = args.partition(" ")
            verb = "UID " + sub.upper()
        done = f"{tag} OK Success\r\n".encode()
        if verb == "CAPABILITY":
            return (b"* CAPABILITY IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST "
                    b"CHILDREN X-GM-EXT-1 UIDPLUS COMPRESS=DEFLATE ENABLE MOVE CONDSTORE "
                    b"ESEARCH UTF8=ACCEPT LIST-EXTENDED LIST-STATUS LITERAL- SPECIAL-USE\r\n"
                    + done)
        if verb == "LOGIN":
            user, _, password = args.partition(" ")
            if user == ADDRESS and _unquote(password) == self.password:
                session["user"] = user
                return f"{tag} OK {user} authenticated (Success)\r\n".encode()
            return f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)\r\n".encode()
        if verb == "LOGOUT":
            return f"* BYE LOGOUT Requested\r\n{tag} OK 73 good day (Success)\r\n".encode()
        if not session.get("user"):
            return f"{tag} BAD not signed in\r\n".encode()
        if verb == "LIST":
            return "".join(f'* LIST {flags} "/" "{name}"\r\n'
                           for name, flags in self.folders.items()).encode() + done
        if verb == "EXAMINE":
            name = _unquote(args)
            if name != self.all_mail or name not in self.folders:
                return f"{tag} NO [NONEXISTENT] Unknown Mailbox: {name} (Failure)\r\n".encode()
            return (f"* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen)\r\n"
                    f"* OK [PERMANENTFLAGS ()] Flags permitted.\r\n"
                    f"* OK [UIDVALIDITY {self.uidvalidity}] UIDs valid.\r\n"
                    f"* {len(self.messages)} EXISTS\r\n* 0 RECENT\r\n"
                    f"* OK [UIDNEXT {self.next_uid}] Predicted next UID.\r\n"
                    f"* OK [HIGHESTMODSEQ 4242]\r\n"
                    f"{tag} OK [READ-ONLY] {name} selected. (Success)\r\n").encode()
        if verb == "UID SEARCH":
            found = " ".join(str(u) for u in self._search(args))
            return f"* SEARCH{' ' + found if found else ''}\r\n".encode() + done
        if verb == "UID FETCH":
            if self.break_after is not None:
                if self.break_after <= 0:
                    session["dead"] = True
                    return b""                      # the line drops: imaplib reads EOF
                self.break_after -= 1
            uidset, _, items = args.partition(" ")
            return self._fetch(uidset, items.strip("()").split()) + done
        self.refused.append(text)
        return f"{tag} BAD refused by the test: {verb}\r\n".encode()

    def _ranges(self, spec: str) -> list[tuple[int, int]]:
        top = max((m["uid"] for m in self.messages), default=0)
        out = []
        for piece in spec.split(","):
            a, sep, b = piece.partition(":")
            lo = top if a == "*" else int(a)
            hi = lo if not sep else (top if b == "*" else int(b))
            out.append((min(lo, hi), max(lo, hi)))
        return out

    def _search(self, args: str) -> list[int]:
        crit = args.split()
        uids = sorted(m["uid"] for m in self.messages)
        if crit[0].upper() == "ALL":
            return uids
        assert crit[0].upper() == "UID", args
        ranges = self._ranges(crit[1])
        return [u for u in uids if any(lo <= u <= hi for lo, hi in ranges)]

    def _fetch(self, uidset: str, items: list[str]) -> bytes:
        ranges = self._ranges(uidset)
        out = b""
        for seq, m in enumerate(sorted(self.messages, key=lambda m: m["uid"]), 1):
            if not any(lo <= m["uid"] <= hi for lo, hi in ranges):
                continue
            parts = [f"UID {m['uid']}".encode()]
            for item in items:
                part = self._item(m, item)
                if part:
                    parts.append(part)
            out += f"* {seq} FETCH (".encode() + b" ".join(parts) + b")\r\n"
            if self.chatty:
                out += f"* {seq} FETCH (UID {m['uid']} FLAGS (\\Seen))\r\n".encode()
        if self.chatty:
            out += b"* 999 FETCH (UID 99999 FLAGS ())\r\n"
        return out

    def _item(self, m: dict, item: str) -> bytes | None:
        up = item.upper()
        if up == "UID":
            return None
        if up == "RFC822.SIZE":
            return f"RFC822.SIZE {len(m['raw'])}".encode()
        if up == "X-GM-THRID":
            return f"X-GM-THRID {m['thrid']}".encode()
        if up == "X-GM-LABELS":
            return ("X-GM-LABELS (" + " ".join(_quote(x) for x in m["labels"]) + ")").encode()
        if up == "BODYSTRUCTURE":
            return b"BODYSTRUCTURE " + _bodystructure(_parsed(m["raw"]))
        got = re.fullmatch(r"(BODY(?:\.PEEK)?)\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", item, re.I)
        if got is None:
            self.refused.append(f"FETCH {item}")
            return None
        if not got.group(1).upper().endswith(".PEEK"):
            self.refused.append(f"FETCH {item} (would mark the message read)")
        data, key = _section(m["raw"], got.group(2)), f"BODY[{got.group(2)}]"
        if got.group(3) is not None:
            start, n = int(got.group(3)), int(got.group(4))
            data, key = data[start:start + n], key + f"<{start}>"
        return f"{key} {{{len(data)}}}\r\n".encode() + data


class _Wire(imaplib.IMAP4):
    """imaplib's own client, its socket replaced by the fake server."""

    def __init__(self, server: FakeGmail, host: str):
        self.server = server
        self.session: dict = {}
        super().__init__(host, 993)

    def open(self, host="", port=993, timeout=None):
        self.host, self.port, self.sock = host, port, None
        self._in = bytearray(b"* OK Gimap ready for requests from 127.0.0.1\r\n")
        self._out = b""

    def read(self, size):
        data = bytes(self._in[:size])
        del self._in[:size]
        return data

    def readline(self):
        i = self._in.find(b"\n")
        end = len(self._in) if i < 0 else i + 1
        line = bytes(self._in[:end])
        del self._in[:end]
        return line

    def send(self, data):
        self._out += data
        while b"\r\n" in self._out:
            line, self._out = self._out.split(b"\r\n", 1)
            self._in += self.server.answer(line, self.session)

    def shutdown(self):
        pass


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def _parsed(raw: bytes):
    return BytesParser(policy=policy.compat32).parsebytes(raw)


def _head(part) -> bytes:
    return b"".join(f"{k}: {v}\r\n".encode("utf-8", "surrogateescape")
                    for k, v in part.items())


def _body(part) -> bytes:
    if part.get_content_type() == "message/rfc822":
        return part.get_payload()[0].as_bytes()
    return part.get_payload(decode=False).encode("utf-8", "surrogateescape")


def _section(raw: bytes, section: str) -> bytes:
    """BODY[section] the way RFC 3501 numbers a message's parts."""
    head, _, text = raw.partition(b"\r\n\r\n")
    if section == "":
        return raw
    if section.upper() == "HEADER":
        return head + b"\r\n\r\n"
    if section.upper() == "TEXT":
        return text
    *path, last = section.split(".")
    kind = last.upper() if last.upper() in ("MIME", "HEADER") else None
    if kind is None:
        path.append(last)
    part = _parsed(raw)
    for n in path:
        if part.is_multipart():
            part = part.get_payload()[int(n) - 1]
    if kind == "MIME":
        return _head(part) + b"\r\n"
    if kind == "HEADER":
        return _head(part.get_payload()[0]) + b"\r\n"
    return _body(part)


def _plist(pairs) -> str:
    pairs = [(k, v) for k, v in pairs if v]
    return "(" + " ".join(f'"{k.upper()}" "{v}"' for k, v in pairs) + ")" if pairs else "NIL"


def _bodystructure(part) -> bytes:
    if part.is_multipart() and part.get_content_maintype() == "multipart":
        kids = "".join(_bodystructure(p).decode() for p in part.get_payload())
        return (f'({kids} "{part.get_content_subtype().upper()}" '
                f'("BOUNDARY" "{part.get_boundary()}") NIL NIL NIL)').encode()
    main, sub = part.get_content_maintype().upper(), part.get_content_subtype().upper()
    params = _plist((part.get_params() or [])[1:])
    enc = (part.get("Content-Transfer-Encoding") or "7BIT").upper()
    body = _body(part)
    disp = part.get("Content-Disposition")
    if disp:
        dparams = (part.get_params(header="content-disposition") or [])
        dtype = dparams[0][0].upper() if dparams else "ATTACHMENT"
        dstr = f'("{dtype}" {_plist(dparams[1:])})'
    else:
        dstr = "NIL"
    if main == "TEXT":
        return (f'("TEXT" "{sub}" {params} NIL NIL "{enc}" {len(body)} '
                f'{body.count(b"\n")} NIL {dstr} NIL NIL)').encode()
    if (main, sub) == ("MESSAGE", "RFC822"):
        return (f'("MESSAGE" "RFC822" {params} NIL NIL "{enc}" {len(body)} NIL '
                f'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 0 0) 0 NIL {dstr} NIL NIL)').encode()
    return f'("{main}" "{sub}" {params} NIL NIL "{enc}" {len(body)} NIL {dstr} NIL NIL)'.encode()


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def server(monkeypatch):
    s = FakeGmail()
    monkeypatch.setattr(gmail, "_imap", s.connect)
    monkeypatch.setattr(gmail, "_HELD", {})
    yield s
    assert s.refused == []          # never a command that changes the mailbox


def asker(*answers):
    """A stand-in for the terminal: answers in order, every question kept."""
    asked: list[tuple[str, bool]] = []

    def ask(prompt, hide):
        asked.append((prompt, hide))
        return answers[len(asked) - 1]
    ask.asked = asked
    return ask


def _email(result: dict) -> dict:
    return next(r for r in result["sources"] if r["source"] == "email")


def _folders(cfg):
    dest = online.dest_dir(cfg, "gmail")
    return dest / "pending", dest / "archive"


def _uids(folder) -> list[int]:
    return sorted(int(p.stem.split("-")[1]) for p in folder.glob("*.eml"))


def _kept(cfg) -> dict:
    return secrets.load(cfg)["gmail"]


def _nowhere_but_secrets(root, password: str) -> None:
    """The app password is in data/secrets.json and in no other file."""
    for p in root.rglob("*"):
        if p.is_file() and p.name != secrets.FILE:
            assert password.encode() not in p.read_bytes(), p


# ---------------------------------------------------------------- connecting

def test_connecting_signs_in_once_and_keeps_the_address_password_and_all_mail(cfg, server):
    server.folders.pop(server.all_mail)
    server.all_mail = "[Gmail]/Alle Nachrichten"        # a German account
    server.folders[server.all_mail] = r"(\All \HasNoChildren)"
    ask = asker(" Alex.Rivera@Gmail.com ", TYPED)
    online.connect(cfg, "gmail", [], ask)
    assert _kept(cfg) == {"address": ADDRESS, "password": APP_PASSWORD,
                          "mailbox": "[Gmail]/Alle Nachrichten"}
    assert [hide for _, hide in ask.asked] == [False, True]     # the password unseen
    assert not any(" EXAMINE " in c or " SELECT " in c for c in server.log)
    assert set(online.connected(cfg)) == {"gmail"}


def test_a_failed_connect_says_what_to_check_and_keeps_nothing(cfg, server):
    with pytest.raises(DomainError, match="use an app password") as e:
        online.connect(cfg, "gmail", [ADDRESS], asker("wron gpas swor dxyz"))
    assert "wrongpasswordxyz" not in str(e.value)
    server.folders.pop(server.all_mail)                       # hidden from IMAP
    with pytest.raises(DomainError, match="Show in IMAP"):
        online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    server.down = socket.gaierror(11001, "getaddrinfo failed")
    with pytest.raises(DomainError, match="could not reach imap.gmail.com .*getaddrinfo"):
        online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    with pytest.raises(DomainError, match="never put on the command line"):
        online.connect(cfg, "gmail", [ADDRESS, APP_PASSWORD], None)
    with pytest.raises(DomainError, match="typed when asked"):
        online.connect(cfg, "gmail", [ADDRESS], None)
    with pytest.raises(DomainError, match="not an email address"):
        online.connect(cfg, "gmail", ["alex"], asker(TYPED))
    with pytest.raises(DomainError, match="no app password given"):
        online.connect(cfg, "gmail", [ADDRESS], asker("   "))
    assert secrets.load(cfg) == {}


def test_a_new_app_password_carries_on_where_the_fetch_got_to(cfg, server):
    server.add(DECK, [r"\Inbox"])
    online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    assert online.fetch(cfg, online.get("gmail")) is not None
    before = _kept(cfg)
    server.password = "newpasswordabcde"                      # revoked, a new one made
    with pytest.raises(DomainError, match="no longer accepts the app password .* connect "
                                          "gmail alex.rivera@gmail.com"):
        online.fetch(cfg, online.get("gmail"))
    online.connect(cfg, "gmail", [ADDRESS], asker("newp assw orda bcde"))
    after = _kept(cfg)
    assert after["password"] == "newpasswordabcde"
    assert {k: after.get(k) for k in gmail.CURSOR} == {k: before.get(k) for k in gmail.CURSOR}


# ---------------------------------------------------------------- collect

def test_first_import_waits_for_the_yes_then_every_morning_is_routine(conn, cfg, server,
                                                                      tmp_path):
    server.add(DECK, [r"\Inbox", r"\Important"], thrid=THREAD)
    server.add(REPLY, [r"\Sent"], thrid=THREAD)
    server.add(TAXES, ["Caf&AOk-", "Taxes, 2026", "Work/Projects"])
    server.chatty = True
    online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    pending, archive = _folders(cfg)

    result = collect.run(conn, cfg)
    r = _email(result)
    assert (r["outcome"], r["hold"]) == ("held", "first_import"), r
    assert f"email for {ADDRESS}, 3 record(s)" in r["reason"]
    assert APP_PASSWORD not in json.dumps(result)
    assert _uids(pending) == [1, 2, 3]
    assert _email(collect.run(conn, cfg))["hold"] == "first_import"   # asked again
    assert _uids(pending) == [1, 2, 3] and not _uids(archive)

    done = collect.approve(conn, cfg, "email", apply=True)
    assert done["applied"] and done["applied_counts"]["added"] == 3, done
    assert not _uids(pending) and _uids(archive) == [1, 2, 3]      # filed away, kept
    rows = {r["title"]: r for r in conn.execute(
        "SELECT title, direction, value_json, origin_app FROM observations "
        "WHERE stream='email'")}
    assert rows["Q4 deck"]["direction"] == "in"
    assert rows["Re: Q4 deck"]["direction"] == "out"              # the Sent label
    deck = json.loads(rows["Q4 deck"]["value_json"])
    assert deck["labels"] == ["Important", "Inbox"] and deck["thread"] == str(THREAD)
    assert json.loads(rows["Re: Q4 deck"]["value_json"])["thread"] == str(THREAD)
    assert json.loads(rows["Taxes"]["value_json"])["labels"] == [
        "Café", "Taxes, 2026", "Work/Projects"]
    assert {r["origin_app"] for r in rows.values()} == {"gmail"}

    server.add(letter("Climbing Saturday?", "Bouldering at 10?", sender=f"Jake <{JAKE}>"),
               [r"\Inbox"])
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 1, r

    # A morning with only mail sent from an alias and a newsletter: the
    # account is still the one fetched from, so it is routine, not a question.
    server.add(letter("From work", "Sent from the work alias.", sender=f"Alex <{ALIAS}>",
                      to=f"Jake <{JAKE}>", delivered=False), [r"\Sent"])
    server.add(letter("Weekly digest", "News.", sender="News <news@digest.example>",
                      extra=("List-Unsubscribe: <mailto:u@digest.example>",)), [r"\Inbox"])
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 1, r
    assert [a for (a,) in conn.execute(
        "SELECT DISTINCT account FROM imports WHERE connector='email'")] == [ADDRESS]

    r = _email(collect.run(conn, cfg))                             # a quiet morning
    assert (r["outcome"], r["target"]) == ("unchanged", None), r
    assert not _uids(pending) and _uids(archive) == [1, 2, 3, 4, 5, 6]
    assert any(" EXAMINE " in c for c in server.log)
    _nowhere_but_secrets(tmp_path, APP_PASSWORD)


def test_a_new_uidvalidity_starts_over_and_nothing_doubles(conn, cfg, server):
    server.add(DECK, [r"\Inbox"])
    server.add(TAXES, [r"\Inbox"])
    online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    collect.run(conn, cfg)
    assert collect.approve(conn, cfg, "email", apply=True)["applied_counts"]["added"] == 2
    old = server.uidvalidity
    server.renumber()
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] in ("applied", "unchanged") and "added" not in r["counts"], r
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE stream='email'"
                        ).fetchone()[0] == 2
    kept = _kept(cfg)
    assert kept["uidvalidity"] == server.uidvalidity and kept["high"] == 102
    _, archive = _folders(cfg)
    assert sorted(p.name for p in archive.glob("*.eml")) == [
        f"{old}-0000000001.eml", f"{old}-0000000002.eml",
        f"{server.uidvalidity}-0000000101.eml", f"{server.uidvalidity}-0000000102.eml"]


def test_older_mail_comes_in_batches_newest_first_once_the_first_import_is_in(
        conn, cfg, server, monkeypatch):
    monkeypatch.setattr(gmail, "BATCH", 2)
    monkeypatch.setattr(gmail, "CHUNK", 1)
    for i in range(1, 6):
        server.add(letter(f"Note {i}", f"Text {i}."), [r"\Inbox"])
    online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    pending, archive = _folders(cfg)

    assert _email(collect.run(conn, cfg))["hold"] == "first_import"
    assert _uids(pending) == [4, 5]                               # the newest batch
    server.add(letter("Note 6", "Text 6."), [r"\Inbox"])
    assert _email(collect.run(conn, cfg))["hold"] == "first_import"
    assert _uids(pending) == [4, 5, 6]          # new mail comes; older waits for the yes
    assert _kept(cfg)["low"] == 4 and not _kept(cfg).get("settled")

    assert collect.approve(conn, cfg, "email", apply=True)["applied_counts"]["added"] == 3
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 2, r
    assert _uids(archive) == [2, 3, 4, 5, 6] and not _kept(cfg)["complete"]
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 1, r
    assert _kept(cfg)["complete"] and _kept(cfg)["low"] == 1
    r = _email(collect.run(conn, cfg))
    assert (r["outcome"], r["target"]) == ("unchanged", None), r


def test_the_backfill_stops_at_its_byte_budget_and_goes_on_next_run(cfg, server,
                                                                   monkeypatch):
    monkeypatch.setattr(gmail, "CHUNK", 1)
    monkeypatch.setattr(gmail, "BACKFILL_BYTES", 1)
    for i in range(1, 4):
        server.add(letter(f"Note {i}", f"Text {i}."), [r"\Inbox"])
    secrets.put(cfg, "gmail", {"address": ADDRESS, "password": APP_PASSWORD,
                               "mailbox": server.all_mail})
    mod = online.get("gmail")
    lows = []
    for _ in range(4):
        online.fetch(cfg, mod)
        secrets.update(cfg, "gmail", settled=True)     # as each import's settle says
        lows.append((_kept(cfg)["low"], _kept(cfg)["complete"]))
    assert lows == [(3, False), (2, False), (1, False), (1, True)]


def test_a_network_failure_is_said_and_a_broken_fetch_keeps_what_it_got(conn, cfg, server,
                                                                        monkeypatch):
    server.add(DECK, [r"\Inbox"])
    server.add(TAXES, [r"\Inbox"])
    online.connect(cfg, "gmail", [ADDRESS], asker(TYPED))
    server.down = socket.gaierror(11001, "getaddrinfo failed")
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "error" and "could not reach Gmail" in r["reason"], r

    server.down = None
    monkeypatch.setattr(gmail, "CHUNK", 1)
    server.break_after = 2                     # the newest message, then the line drops
    r = _email(collect.run(conn, cfg))
    assert r["outcome"] == "error" and "connection to Gmail broke" in r["reason"], r
    assert "1 message(s) fetched before it are kept" in r["reason"]
    pending, _ = _folders(cfg)
    assert _uids(pending) == [2]
    server.break_after = None
    r = _email(collect.run(conn, cfg))                 # carries on: the first batch done
    assert (r["outcome"], r["hold"]) == ("held", "first_import"), r
    assert "2 record(s)" in r["reason"] and _uids(pending) == [1, 2]


# ---------------------------------------------------------------- what a file holds

def test_labels_are_written_the_way_takeout_writes_them():
    labels = gmail._labels([r"\Inbox", r"\Sent", r"\Draft", b"Caf&AOk-", "Taxes, 2026",
                            'Say "hi"', "&ZeVnLIqe-", r"\Inbox"])
    assert labels == ["Inbox", "Sent", "Drafts", "Café", "Taxes, 2026", 'Say "hi"', "日本語"]
    header = gmail._label_header(labels)
    assert header.isascii()
    assert em._labels({"x-gmail-labels": [header]}) == labels
    assert gmail._label_header(["Inbox", "Important"]) == "Inbox,Important"


def test_a_big_message_keeps_its_text_and_only_names_its_attachments(cfg, server,
                                                                     monkeypatch):
    monkeypatch.setattr(gmail, "MESSAGE_CAP", 4096)
    pdf = bytes(range(256)) * 80
    b64 = base64.encodebytes(pdf).decode()
    body = ("--OUTER\n"
            'Content-Type: multipart/alternative; boundary="INNER"\n\n'
            '--INNER\nContent-Type: text/plain; charset="UTF-8"\n\n'
            "Deck attached. Lead with the customer story.\n"
            '--INNER\nContent-Type: text/html; charset="UTF-8"\n\n'
            "<p>Deck attached. Lead with the customer story.</p>\n"
            "--INNER--\n"
            '--OUTER\nContent-Type: application/pdf; name="Q4 Deck.pdf"\n'
            'Content-Disposition: attachment; filename="Q4 Deck.pdf"\n'
            "Content-Transfer-Encoding: base64\n\n" + b64 +
            "--OUTER\nContent-Type: message/rfc822\nContent-Disposition: attachment\n\n"
            "Subject: Old thread\nFrom: x@example.com\n\nold text\n"
            "--OUTER--")
    big_raw = letter("Q4 deck v3", body, msgid="<deck-v3@example.com>",
                     extra=('Content-Type: multipart/mixed; boundary="OUTER"',))
    small = server.add(DECK, [r"\Inbox"])
    big = server.add(big_raw, [r"\Inbox"], thrid=THREAD)
    secrets.put(cfg, "gmail", {"address": ADDRESS, "password": APP_PASSWORD,
                               "mailbox": server.all_mail})
    dest = online.fetch(cfg, online.get("gmail"))
    files = {int(p.stem.split("-")[1]): p.read_bytes() for p in dest.glob("*.eml")}

    assert files[small] == (f"X-Lifecrm-Account: {ADDRESS}\r\nX-GM-THRID: "
                            f"{1780000000000000000 + small}\r\nX-Gmail-Labels: Inbox\r\n"
                            ).encode() + DECK                    # Gmail's copy, whole
    assert len(big_raw) > 4096 and len(files[big]) < 4096
    assert b64[:300].encode() not in files[big].replace(b"\r\n", b"\n")
    assert any("BODYSTRUCTURE" in c for c in server.log)

    scan = em.scan(dest, cfg)
    rec = next(r for r in scan["records"] if r["external_id"] == "deck-v3@example.com")
    assert rec["body"] == "Deck attached. Lead with the customer story."
    detail = json.loads(rec["value_json"])
    pdf_att, old = detail["attachments"]
    assert (pdf_att["name"], pdf_att["mime"]) == ("Q4 Deck.pdf", "application/pdf")
    assert abs(pdf_att["size"] - len(pdf)) < 100          # estimated from the server's size
    assert (old["name"], old["mime"]) == ("Old thread.eml", "message/rfc822")
    assert old["size"] > 0
    assert detail["thread"] == str(THREAD) and detail["labels"] == ["Inbox"]
    assert scan["account"] == ADDRESS


def test_the_fetched_account_is_yours_and_only_a_first_line_says_so(cfg, tmp_path):
    folder = tmp_path / "eml"
    folder.mkdir()
    mark = f"X-Lifecrm-Account: {ADDRESS}\r\n".encode()
    (folder / "1.eml").write_bytes(mark + b"X-Gmail-Labels: Sent\r\n" + letter(
        "From work", "Sent from the alias.", sender=f"Alex <{ALIAS}>", to=f"Jake <{JAKE}>",
        delivered=False))
    (folder / "2.eml").write_bytes(mark + b"X-Gmail-Labels: Sent\r\n" + REPLY)
    cfg.connectors = {"email": {"addresses": [ALIAS]}}
    scan = em.scan(folder, cfg)
    assert scan["account"] == ADDRESS
    assert {r["title"]: r["direction"] for r in scan["records"]} == {
        "From work": "out", "Re: Q4 deck": "out"}

    # The same line further down came with the message: it says nothing.
    forged = tmp_path / "forged"
    forged.mkdir()
    raw = letter("Hello", "Hi.", extra=("X-Lifecrm-Account: someone@else.example",))
    (forged / "1.eml").write_bytes(raw)
    cfg.connectors = {}
    scan = em.scan(forged, cfg)
    assert scan["account"] == ADDRESS            # Delivered-To, as for any saved mail
    assert "Gmail account" not in (scan["facts"]["you_from"] or "")


# ---------------------------------------------------------------- the CLI

def test_cli_connect_gmail_asks_for_the_app_password_and_never_shows_it(tmp_path,
                                                                       monkeypatch,
                                                                       server):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    server.add(DECK, [r"\Inbox"])
    r = runner.invoke(app, ["connect"])
    assert r.exit_code == 0 and "gmail" in r.output and "apppasswords" in r.output
    r = runner.invoke(app, ["connect", "gmail", ADDRESS], input=TYPED + "\n")
    assert r.exit_code == 0 and "connected: Gmail" in r.output, r.output
    assert APP_PASSWORD not in r.output and TYPED not in r.output
    r = runner.invoke(app, ["connect", "gmail", ADDRESS], input="nope nope nope nope\n")
    assert r.exit_code != 0 and "use an app password" in r.output
    r = runner.invoke(app, ["fetch", "gmail"])
    assert r.exit_code == 0 and "fetched Gmail" in r.output, r.output
    r = runner.invoke(app, ["collect", "approve", "email", "--yes"])
    assert r.exit_code == 0 and "imported" in r.output, r.output
    r = runner.invoke(app, ["fetch", "gmail"])
    assert r.exit_code == 0 and "nothing new from Gmail" in r.output, r.output
    r = runner.invoke(app, ["disconnect", "gmail"])
    assert r.exit_code == 0 and "disconnected: gmail" in r.output
    _nowhere_but_secrets(tmp_path, APP_PASSWORD)
