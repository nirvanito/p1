"""OFX / QFX / QBO statement reader for the finance connector.

OFX is the one bank format that carries a real transaction id (FITID), which
is why it is worth reading at all when a CSV of the same account exists. It
comes in two dialects and banks still ship both:

  v1 (SGML)   a header block of KEY:VALUE lines, then tags whose leaf
              elements are never closed: `<TRNAMT>-34.50` and a newline.
  v2 (XML)    `<?xml ...?><?OFX ...?>`, then every element closed.

Quicken's QFX and QuickBooks' QBO are the same thing with a few `INTU.*`
tags. One tokenizer reads all of them: a tag followed by text is a leaf, a
tag followed by nothing opens an aggregate, and a closing tag closes the
innermost open aggregate of that name — so a v1 file that closes some leaves
and not others (several banks do) reads the same as one that closes none.

The open question in that rule is an *empty* element: v1's `<MEMO>` with no
text, or v2's `<MEMO></MEMO>`. Treated as an aggregate, it would swallow
every sibling after it. Known leaf names are therefore never opened, and an
unknown empty tag is opened only if the file closes that name somewhere.

This module only parses. Mapping a statement transaction to an observation —
signs, ids, accounts, handles — is `finance.py`'s job.
"""

from __future__ import annotations

import html
import re

from ..domain.util import DomainError

# Aggregates a statement reader needs to see as containers.
_AGGREGATES = frozenset({
    "OFX", "SIGNONMSGSRSV1", "SONRS", "STATUS", "FI",
    "BANKMSGSRSV1", "STMTTRNRS", "STMTRS", "BANKACCTFROM", "BANKACCTTO",
    "BANKTRANLIST", "STMTTRN", "LEDGERBAL", "AVAILBAL", "BALLIST", "BAL",
    "CREDITCARDMSGSRSV1", "CCSTMTTRNRS", "CCSTMTRS", "CCACCTFROM", "CCACCTTO",
    "PAYEE", "CURRENCY", "ORIGCURRENCY", "BANKTRANLISTP", "STMTTRNP",
    "INVSTMTMSGSRSV1", "INVSTMTTRNRS", "INVSTMTRS",
})
# Leaves that may legitimately be empty; never opened as containers.
_LEAVES = frozenset({
    "TRNTYPE", "DTPOSTED", "DTUSER", "DTAVAIL", "TRNAMT", "FITID",
    "CORRECTFITID", "CORRECTACTION", "SRVRTID", "CHECKNUM", "REFNUM", "SIC",
    "PAYEEID", "NAME", "EXTDNAME", "MEMO", "CURDEF", "ACCTID", "BANKID",
    "BRANCHID", "ACCTTYPE", "ACCTKEY", "ORG", "FID", "CURRATE", "CURSYM",
    "DTSTART", "DTEND", "BALAMT", "DTASOF", "CODE", "SEVERITY", "MESSAGE",
    "DTSERVER", "LANGUAGE", "TRNUID", "ADDR1", "ADDR2", "ADDR3", "CITY",
    "STATE", "POSTALCODE", "COUNTRY", "PHONE", "INTU.BID", "INTU.USERID",
})

_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9._]*)\s*(/?)>([^<]*)")
_NOISE = re.compile(r"<\?.*?\?>|<!--.*?-->", re.S)


class Node:
    __slots__ = ("name", "text", "children")

    def __init__(self, name: str, text: str | None = None):
        self.name = name
        self.text = text
        self.children: list[Node] = []

    def child(self, name: str) -> Node | None:
        return next((c for c in self.children if c.name == name), None)

    def value(self, name: str) -> str | None:
        c = self.child(name)
        return c.text if c is not None and c.text else None

    def iter(self, name: str):
        for c in self.children:
            if c.name == name:
                yield c
            yield from c.iter(name)


def looks_like_ofx(raw: bytes) -> bool:
    head = raw[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").upper()
    return head.startswith(b"OFXHEADER") or b"<OFX>" in head or \
        (head.startswith(b"<?XML") and b"OFXHEADER" in head)


def decode(raw: bytes) -> tuple[str, str]:
    """(text, encoding). v1 files declare CHARSET:1252 more often than not;
    UTF-8 is tried first because it fails loudly on 1252 bytes, the reverse
    would not."""
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def tree(text: str, name: str) -> Node:
    start = text.upper().find("<OFX>")
    if start < 0:
        raise DomainError(f"{name}: no <OFX> element — not an OFX/QFX statement "
                          f"(starts: {' '.join(text[:60].split())!r})")
    body = _NOISE.sub("", text[start:])
    tokens = list(_TAG.finditer(body))
    closed = {m.group(2).upper() for m in tokens if m.group(1)}
    root = Node("#root")
    stack = [root]
    for m in tokens:
        closing, tag, self_closing, text_after = (
            m.group(1), m.group(2).upper(), m.group(3), m.group(4))
        if closing:
            # Close the innermost open aggregate of this name. A close for a
            # leaf (v2, or a v1 file that closes some leaves) matches nothing
            # open and is ignored.
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].name == tag:
                    del stack[i:]
                    break
            continue
        value = text_after.strip()
        node = Node(tag, html.unescape(value) if value else None)
        stack[-1].children.append(node)
        if self_closing or value or tag in _LEAVES:
            continue
        if tag in _AGGREGATES or tag in closed:
            stack.append(node)
    return root


# ---------------------------------------------------------------- statements

def _transaction(node: Node) -> dict:
    """Leaf values of one STMTTRN, with PAYEE and the currency aggregates
    flattened. Keys are the OFX element names."""
    out = {c.name: c.text for c in node.children if c.text}
    payee = node.child("PAYEE")
    if payee is not None and payee.value("NAME"):
        out["PAYEE.NAME"] = payee.value("NAME")
    for agg in ("CURRENCY", "ORIGCURRENCY"):
        cur = node.child(agg)
        if cur is not None:
            out[f"{agg}.CURSYM"] = cur.value("CURSYM")
            out[f"{agg}.CURRATE"] = cur.value("CURRATE")
    return out


def statements(raw: bytes, name: str) -> dict:
    """Every bank and card statement in one OFX file.

    {"encoding", "version", "org", "fid", "investment": n,
     "statements": [{"kind": "bank"|"card", "bankid", "acctid", "accttype",
                     "currency", "transactions": [dict], "pending": n}]}
    """
    text, encoding = decode(raw)
    root = tree(text, name)
    version = "2" if text.lstrip("\ufeff \t\r\n")[:5].lower() == "<?xml" else "1"
    fi = next(root.iter("FI"), None)
    out = {"encoding": encoding, "version": version,
           "org": fi.value("ORG") if fi is not None else None,
           "fid": fi.value("FID") if fi is not None else None,
           "investment": sum(1 for _ in root.iter("INVSTMTRS")),
           "statements": []}
    for kind, tag, acct_tag in (("bank", "STMTRS", "BANKACCTFROM"),
                                ("card", "CCSTMTRS", "CCACCTFROM")):
        for st in root.iter(tag):
            acct = st.child(acct_tag) or Node(acct_tag)
            tranlist = st.child("BANKTRANLIST") or Node("BANKTRANLIST")
            pending = st.child("BANKTRANLISTP")
            out["statements"].append({
                "kind": kind,
                "bankid": acct.value("BANKID"),
                "acctid": acct.value("ACCTID"),
                "accttype": acct.value("ACCTTYPE") or ("CREDITCARD" if kind == "card"
                                                       else None),
                "currency": st.value("CURDEF"),
                "transactions": [_transaction(t) for t in tranlist.iter("STMTTRN")],
                "pending": sum(1 for _ in pending.iter("STMTTRNP"))
                if pending is not None else 0,
            })
    if not out["statements"] and not out["investment"]:
        raise DomainError(f"{name}: OFX file with no bank or card statement in it "
                          f"(no STMTRS or CCSTMTRS element)")
    return out


def ofx_date(value: str | None) -> str | None:
    """'YYYYMMDD[HHMMSS[.XXX]][[+-]H:TZ]' -> 'YYYY-MM-DD', the date as the bank
    wrote it. The time and zone are dropped on purpose: banks fill them with
    midnight or noon or a batch time, never when you paid."""
    if not value:
        return None
    m = re.match(r"\s*(\d{4})(\d{2})(\d{2})", value)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
