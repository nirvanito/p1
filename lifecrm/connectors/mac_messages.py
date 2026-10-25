"""Mac Messages connector: iMessage, SMS and RCS from the Mac's own chat.db.

With Messages in iCloud on, a Mac holds the same conversations as the iPhone
in ~/Library/Messages/chat.db — the same schema lineage as the phone's sms.db
— and keeps it current without anyone making a backup. For the MacBook +
iPhone setup that makes this the everyday path; the iPhone backup
(`ios_messages`) remains the way in from Windows, and for history older than
what iCloud keeps on the Mac.

This module is thin on purpose. Reading the database — snapshotting it,
decoding attributedBody, tapbacks, thread events, edits, unsends, Recently
Deleted — is ios_messages' machinery, called here unchanged. What differs is
only what is Mac-specific:

- **Its own connector name, so its own mirroring scope.** Mirroring removes
  rows first imported by the same connector and account. Under one name, an
  iPhone backup that has not yet seen today's texts would remove what the Mac
  imported, and a Mac whose iCloud history starts later than the phone's
  would remove the phone's old years. Two names keep each source's deletions
  to its own archive; a message both hold is one record either way, because
  iMessage GUIDs are the same on every device.
- **Attachments are real files here.** A message whose attachment is on disk
  under ~/Library/Messages/Attachments gets that file as media_path (the
  first one, when there are several). value_json keeps the path relative to
  the Messages folder, exactly as the phone's import does, so the same
  message from both compares identical. The one honest difference left is
  media_path itself — the phone's copy has none — and the report folds those
  into a single line instead of a conflict per photo. A row an older reader
  stored with the full path is upgraded in place (`upgrade_stored`, shared
  with ios_messages).
- **Names come from the Mac's Contacts** — every store, one per account
  under AddressBook/Sources plus the local one — snapshotted like the
  database, read by `_abcddb`, and joined by the same rule as the phone's
  address book ("First Last", else nickname, else organization; a number on
  two cards keeps both names). They are display names only: nothing is
  linked to an entity here.
- **Whose Messages it is** comes from the account handles in the database
  itself (destination_caller_id / account on every message). A Mac sends from
  both the phone number and the Apple ID email in comparable numbers, so the
  account is the most-used *phone number* when there is one — a count that
  tips between the two from one import to the next would move the mirroring
  scope with it. The machine's name is recorded when this Mac's own database
  is read.

macOS guards ~/Library/Messages and the Contacts stores: without Full Disk
Access for the app running this, every read fails, and that is reported as
the missing permission (one line, with where to grant it) rather than as a
database that will not open.

Freshness: this connector declares no `stale_after_days`. Its streams are
ios_messages' streams, and doctor measures freshness per stream, so the
`ios_messages_data_age` check already covers texts from either source; a
second check would only repeat it.
"""

from __future__ import annotations

import platform
from pathlib import Path

from .. import platforms
from ..config import Config
from ..domain.util import DomainError
from . import base
from . import ios_messages as im

name = "mac_messages"
streams = im.streams
# The same records as the phone's, so the same stored-format upgrade: a row
# kept with a full Library/Messages/ (or Library/SMS/) attachment path by a
# reader from before 2026-10-22 is rewritten, not reported as a conflict.
upgrade_stored = im.upgrade_stored

ADDRESS_BOOK = "AddressBook-v22.abcddb"
# The same handles as the phone's (its number, its Apple ID), and display
# names are first-wins: one label, whichever source is imported first.
OWNER_LABEL = im.OWNER_LABEL


# ---------------------------------------------------------------- finding chat.db

def _is_live(db: Path) -> bool:
    """Whether `db` is this Mac's own Messages database (not a copy)."""
    try:
        return db.resolve() == platforms.mac_messages_db().resolve()
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """This Mac's ~/Library/Messages/chat.db."""
    db = platforms.mac_messages_db()
    with im.privacy_guard(db):
        if db.exists():
            return db
    if not platforms.is_mac():
        raise DomainError(
            f"mac_messages reads a Mac's own Messages database ({db}) and this is "
            f"not a Mac — import an iPhone backup with 'observe ios_messages', or "
            f"pass a copied chat.db")
    raise DomainError(
        f"no Messages database at {db} — sign in to Messages on this Mac with "
        f"Messages in iCloud on (docs/texts.md), or pass a copied chat.db")


def _resolve(target: Path) -> Path:
    """The chat.db to read: the file itself, or the folder holding it."""
    if not target.is_dir():
        return target
    if (target / "Manifest.db").exists():
        raise DomainError(
            f"{target.name} is an iPhone backup, not a Mac's Messages folder — "
            f"import it with: lifecrm observe ios_messages \"{target}\"")
    if (target / "chat.db").exists():
        return target / "chat.db"
    if (target / "sms.db").exists():
        raise DomainError(
            f"{target.name} holds an iPhone's sms.db, not a Mac's chat.db — "
            f"import it with: lifecrm observe ios_messages \"{target}\"")
    listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
    raise DomainError(f"{target.name} holds no chat.db (has: {listing}) — pass "
                      f"~/Library/Messages or a copied chat.db")


def address_books_under(app_support: Path) -> list[Path]:
    """Every Contacts store under one `Application Support` folder: one per
    account in AddressBook/Sources/, plus the local 'On My Mac' store."""
    base_dir = app_support / "AddressBook"
    found = []
    top = base_dir / ADDRESS_BOOK
    if top.is_file():
        found.append(top)
    sources = base_dir / "Sources"
    if sources.is_dir():
        found.extend(sorted(sources.glob(f"*/{ADDRESS_BOOK}")))
    return found


def _address_books(db: Path) -> tuple[list[Path], bool]:
    """The Contacts stores of the home this chat.db lives in — this Mac's own,
    or those of a copied Library folder — and whether listing them was denied.

    A chat.db sits in <Library>/Messages and Contacts in <Library>/Application
    Support/AddressBook, so the second is found from the first; a lone copied
    chat.db has none and imports without names.
    """
    try:
        if _is_live(db):
            return platforms.mac_addressbook_dbs(), False
        return address_books_under(db.parent.parent / "Application Support"), False
    except PermissionError:
        return [], True


# ---------------------------------------------------------------- scan

def _account(owners: dict[str, int]) -> str | None:
    """The phone number this Mac sends from most, else its most-used handle."""
    phones = {v: n for v, n in owners.items()
              if (base.handle_for(v) or ("",))[0] == "phone"}
    pool = phones or owners
    return max(sorted(pool), key=pool.get) if pool else None


def scan(target: Path, cfg: Config) -> base.Scan:
    with im.privacy_guard(target):
        db = _resolve(target)
        folder = db.parent
        books, books_denied = _address_books(db)
        result, wal_read, books_read = im.read_database(
            db, None, [(b, None) for b in books], cfg,
            attachments_root=folder.resolve())
        source_hash = im._content_hash(db, db.with_name(db.name + "-wal"))
        parts = im.scan_parts(result, folder, wal_read)
        live = _is_live(db)

    owners = result["owners"]
    facts = {
        **parts["facts"],
        "host": (platform.node() or None) if live else None,
        "accounts": [{"handle": "{}:{}".format(*(base.handle_for(v) or ("?", v))),
                      "messages": n}
                     for v, n in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))],
        "attachments": dict(result["attachment_files"]),
        "address_books": len(books),
        "address_books_read": books_read,
        "address_books_denied": books_denied,
    }
    return base.empty_scan(
        name,
        account=_account(owners),
        source_file=db.name,
        source_hash=source_hash,
        records=parts["records"],
        handles=parts["handles"],
        unmapped=parts["unmapped"],
        skipped=parts["skipped"],
        facts=facts,
        coverage=im.coverage_for(parts["facts"], _account(owners), None,
                                 "the Mac's Messages database: its whole store"),
        present_ids=result["present"],
        mirror_streams=list(streams),
        # Keep Messages reaches the Mac through Messages in iCloud: what is
        # older than the store's oldest message aged out, it was not deleted.
        mirror_ages_out=True,
        target_kind="live" if live else "file",
        contacts_read=books_read > 0,
        named_handles=parts["named_handles"],
        messages_seen=result["counts"]["messages"],
        chats=result["counts"]["chats"])


# ---------------------------------------------------------------- report

def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    if scan["target_kind"] == "live":
        what = "this Mac's Messages" + (f", {facts['host']}" if facts.get("host") else "")
    else:
        what = "copied Messages database"
    lines = [f"mac-messages: {scan['source_file']} ({what}; {scan['messages_seen']} "
             f"message row(s), {scan['chats']} chat(s))"]
    lines += im.fact_lines(scan, f"{scan['source_file']}-wal", "Messages")
    others = [a for a in facts.get("accounts", [])
              if a["handle"].split(":", 1)[-1] != scan["account"]]
    if others:
        lines.append("  also sent from: " + ", ".join(
            f"{a['handle']} ({a['messages']})" for a in others[:4]))

    files = facts.get("attachments") or {}
    if files.get("on_disk") or files.get("not_on_disk"):
        lines.append(f"  attachments: {files.get('on_disk', 0)} on this Mac (linked as "
                     f"media_path), {files.get('not_on_disk', 0)} not downloaded "
                     f"(kept in iCloud, or never fetched) — described either way")
    if facts.get("address_books_denied"):
        lines.append(f"  contacts: not readable — {im.FULL_DISK_ACCESS}")
    elif facts.get("address_books"):
        lines.append(f"  contacts: {facts['address_books_read']} of "
                     f"{facts['address_books']} Contacts store(s) read")
    else:
        lines.append("  contacts: no Contacts store beside this database — handles "
                     "come in without names")
    return lines + im.plan_lines(scan, plan, gone="in Messages on this Mac",
                                 source="Messages",
                                 contacts_from="from this Mac's Contacts")
