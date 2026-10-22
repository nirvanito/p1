"""Notes connector, Apple Notes: a Mac's NoteStore.sqlite.

Stores are built here with the Core Data tables Notes writes — one
ZICCLOUDSYNCINGOBJECT table holding accounts, folders and notes side by side,
told apart through Z_PRIMARYKEY, and each note's text in ZICNOTEDATA.ZDATA as
gzip-compressed protobuf, encoded by hand below. Two layouts are covered: a
current one (ZTITLE1, ZCREATIONDATE3) and an older one with no entity map,
because the importer must find its columns rather than assume them.

Also here: the protobuf reader on its own, the snapshot of a live store with
writes still in its -wal, and mirroring — a note deleted, moved to Recently
Deleted or marked for deletion leaves the archive; a locked one does not.
"""

import gzip
import json
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from lifecrm.connectors import _protobuf, notes
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks

APPLE_EPOCH = 978307200
STORE_UUID = "0B7C3F1E-2A44-4C55-9E21-6D5F0A1B2C3D"


# ---------------------------------------------------------------- protobuf, by hand

def varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def ld(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def body_blob(text, *, extra_note_fields=b""):
    """NoteStoreProto{2: Document{2: version, 3: Note{2: text, 5: run}}}, gzipped."""
    note = ld(2, text.encode("utf-8")) + extra_note_fields + ld(5, varint(1 << 3) + varint(len(text)))
    document = varint(2 << 3) + varint(0) + ld(3, note)
    return gzip.compress(ld(2, document), mtime=0)


def cd(year, month, day, hour=12, minute=0, second=0, frac=0.0):
    """A Core Data timestamp: seconds since 2001, as Notes stores them (REAL)."""
    return datetime(year, month, day, hour, minute, second,
                    tzinfo=timezone.utc).timestamp() - APPLE_EPOCH + frac


# ---------------------------------------------------------------- stores

CURRENT = """
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZIDENTIFIER VARCHAR, ZMARKEDFORDELETION INTEGER, ZISPASSWORDPROTECTED INTEGER,
    ZISPINNED INTEGER, ZFOLDERTYPE INTEGER, ZFOLDER INTEGER, ZNOTEDATA INTEGER,
    ZOWNER INTEGER, ZCREATIONDATE1 TIMESTAMP, ZCREATIONDATE3 TIMESTAMP,
    ZMODIFICATIONDATE1 TIMESTAMP, ZNAME VARCHAR, ZTITLE1 VARCHAR, ZTITLE2 VARCHAR);
CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZNOTE INTEGER, ZDATA BLOB);
"""

# An older layout: no entity map, ZTITLE / ZCREATIONDATE, notes pointing at
# their data rather than the other way round.
OLD = """
CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZIDENTIFIER VARCHAR, ZMARKEDFORDELETION INTEGER, ZISPASSWORDPROTECTED INTEGER,
    ZFOLDER INTEGER, ZNOTEDATA INTEGER, ZCREATIONDATE TIMESTAMP,
    ZMODIFICATIONDATE TIMESTAMP, ZTITLE VARCHAR);
CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZDATA BLOB);
"""

NOTES = [
    {"id": "N-BLED", "title": "Bled packing list", "folder": "Travel",
     "text": "Bled packing list\nrain jacket, hiking boots",
     "created": cd(2022, 9, 26, 16, 30, 0, 0.75), "modified": cd(2022, 9, 30, 7)},
    {"id": "N-GIFTS", "title": "Gift ideas", "folder": "Notes", "pinned": 1,
     "text": "Gift ideas\n\N{OBJECT REPLACEMENT CHARACTER}\n- ceramics voucher  \n",
     "created": cd(2026, 9, 26, 2, 40), "created1": cd(2020, 1, 1),
     "modified": cd(2026, 10, 13, 2, 30)},
    {"id": "N-LOCKED", "title": "Passport details", "folder": "Notes", "locked": 1,
     "data": b"\x8a\x13ciphertext-not-gzip", "created": cd(2026, 9, 28, 15),
     "modified": cd(2026, 9, 28, 15)},
    {"id": "N-TRASH", "title": "Grocery list", "folder": "Recently Deleted",
     "text": "Grocery list\neggs", "created": cd(2026, 9, 18, 23), "modified": cd(2026, 9, 18, 23)},
    {"id": "N-GONE", "title": "Old idea", "folder": "Notes", "marked": 1,
     "text": "Old idea\nmeh", "created": cd(2026, 1, 2), "modified": cd(2026, 1, 2)},
]


def make_store(path, notes_=None, *, layout="current", metadata=True, wal_to=None):
    """A NoteStore.sqlite. With wal_to=(main, wal) it is written in WAL mode
    and both halves are copied out before any checkpoint — a main file that
    on its own holds no notes at all, as a copied live store can be."""
    notes_ = NOTES if notes_ is None else notes_
    c = sqlite3.connect(path)
    if wal_to:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
    c.executescript(CURRENT if layout == "current" else OLD)
    folders = {"Notes": (2, 0), "Travel": (3, 0), "Recently Deleted": (4, 1)}
    if layout == "current":
        c.executemany("INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, 0)",
                      [(8, "ICAccount"), (11, "ICFolder"), (12, "ICNote"), (19, "ICNoteData")])
        if metadata:
            c.execute("INSERT INTO Z_METADATA VALUES (1, ?, NULL)", (STORE_UUID,))
        c.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZNAME) "
                  "VALUES (1, 8, 'ACCOUNT', 'iCloud')")
        for title, (pk, ftype) in folders.items():
            c.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, "
                      "ZFOLDERTYPE, ZOWNER, ZTITLE2) VALUES (?, 11, ?, ?, 1, ?)",
                      (pk, f"F-{pk}", ftype, title))
    else:
        for title, (pk, _) in folders.items():
            c.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE) "
                      "VALUES (?, 5, ?, ?)", (pk, f"F-{pk}", title))
    for i, n in enumerate(notes_):
        pk, data_pk = 20 + i, 100 + i
        data = n.get("data", body_blob(n["text"]) if "text" in n else None)
        if layout == "current":
            c.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, "
                      "ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZISPINNED, ZFOLDER, "
                      "ZNOTEDATA, ZCREATIONDATE1, ZCREATIONDATE3, ZMODIFICATIONDATE1, "
                      "ZTITLE1) VALUES (?, 12, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (pk, n["id"], n.get("marked", 0), n.get("locked", 0),
                       n.get("pinned", 0), folders[n["folder"]][0], data_pk,
                       n.get("created1"), n["created"], n["modified"], n.get("title")))
            if data is not None or n.get("row_without_data"):
                c.execute("INSERT INTO ZICNOTEDATA (Z_PK, Z_ENT, ZNOTE, ZDATA) "
                          "VALUES (?, 19, ?, ?)", (data_pk, pk, data))
        else:
            c.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, "
                      "ZMARKEDFORDELETION, ZISPASSWORDPROTECTED, ZFOLDER, ZNOTEDATA, "
                      "ZCREATIONDATE, ZMODIFICATIONDATE, ZTITLE) "
                      "VALUES (?, 9, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (pk, n["id"], n.get("marked", 0), n.get("locked", 0),
                       folders[n["folder"]][0], data_pk, n["created"], n["modified"],
                       n.get("title")))
            c.execute("INSERT INTO ZICNOTEDATA (Z_PK, Z_ENT, ZDATA) VALUES (?, 19, ?)",
                      (data_pk, data))
    c.commit()
    if wal_to:
        shutil.copyfile(path, wal_to[0])
        shutil.copyfile(f"{path}-wal", wal_to[1])
    c.close()
    return path


def _by_id(scan):
    return {r["external_id"]: r for r in scan["records"]}


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- protobuf

def test_varints_of_every_width_round_trip():
    for n in (0, 1, 127, 128, 300, 2**32, 2**63):
        assert _protobuf.read_varint(varint(n) + b"tail", 0) == (n, len(varint(n)))


def test_fields_yields_every_wire_type():
    buf = (varint(1 << 3) + varint(150)
           + varint(2 << 3 | 1) + (7).to_bytes(8, "little")
           + ld(3, b"abc")
           + varint(4 << 3 | 5) + (9).to_bytes(4, "little"))
    assert list(_protobuf.fields(buf)) == [(1, 0, 150), (2, 1, 7), (3, 2, b"abc"), (4, 5, 9)]


def test_first_walks_nested_messages_and_skips_what_it_does_not_need():
    inner = varint(1 << 3) + varint(5) + ld(2, b"the text")
    buf = ld(1, b"ignored") + varint(9 << 3 | 5) + b"\x00" * 4 + ld(2, ld(3, inner))
    assert _protobuf.first(buf, 2, 3, 2) == b"the text"
    assert _protobuf.first(buf, 2, 4) is None


@pytest.mark.parametrize("buf, what", [
    (varint(2 << 3 | 2) + varint(50) + b"short", "claims 50 bytes"),
    (b"\x80\x80", "truncated varint"),
    (varint(1 << 3 | 3), "wire type 3"),
    (varint(0 << 3) + varint(1), "field number 0"),
    (b"\xff" * 11, "longer than 10 bytes"),
])
def test_malformed_protobuf_raises_rather_than_reading_as_empty(buf, what):
    with pytest.raises(ValueError, match=what):
        list(_protobuf.fields(buf))


def test_note_text_reads_a_gzipped_note_and_refuses_anything_else():
    assert notes.note_text(body_blob("Hello\nworld")) == "Hello\nworld"
    assert notes.note_text(gzip.compress(ld(2, b""), mtime=0)) == ""   # no text field
    with pytest.raises(ValueError, match="not gzip"):
        notes.note_text(b"plain bytes")
    with pytest.raises(ValueError):
        notes.note_text(gzip.compress(b"\xff" * 11, mtime=0))


# ---------------------------------------------------------------- reading a store

def test_one_observation_per_note_keyed_on_its_identifier(tmp_path, cfg):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg)
    assert scan["source_kind"] == "apple_notes"
    assert set(_by_id(scan)) == {"apple:N-BLED", "apple:N-GIFTS"}
    rec = _by_id(scan)["apple:N-BLED"]
    assert rec["title"] == "Bled packing list"
    assert rec["body"] == "Bled packing list\nrain jacket, hiking boots"
    assert rec["origin_app"] == "apple_notes" and rec["stream"] == "note"
    assert json.loads(rec["value_json"]) == {"source": "apple_notes", "folder": "Travel",
                                             "account": "iCloud"}


def test_core_data_times_drop_the_fraction_and_prefer_the_newer_creation_column(tmp_path, cfg):
    recs = _by_id(notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg))
    assert recs["apple:N-BLED"]["started_at"] == "2022-09-26T16:30:00Z"
    assert recs["apple:N-BLED"]["last_modified_at"] == "2022-09-30T07:00:00Z"
    assert recs["apple:N-BLED"]["journal_date"] == "2022-09-26"
    # ZCREATIONDATE3 is the current column; a stale ZCREATIONDATE1 is ignored
    assert recs["apple:N-GIFTS"]["started_at"] == "2026-09-26T02:40:00Z"
    assert recs["apple:N-GIFTS"]["journal_date"] == "2026-09-25"   # 21:40 CDT


def test_attachment_placeholders_are_stripped_and_counted(tmp_path, cfg):
    rec = _by_id(notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg))["apple:N-GIFTS"]
    assert rec["body"] == "Gift ideas\n\n- ceramics voucher"
    detail = json.loads(rec["value_json"])
    assert detail["attachments"] == 1 and detail["pinned"] is True


def test_deleted_notes_are_skipped_and_count_as_gone(tmp_path, cfg):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg)
    assert "apple:N-TRASH" not in scan["present_ids"]
    assert "apple:N-GONE" not in scan["present_ids"]
    assert "1 × note in Recently Deleted (deleted — not kept)" in scan["skipped"]
    assert "1 × note marked for deletion (deleted — not kept)" in scan["skipped"]
    assert scan["notes_seen"] == 5


def test_a_locked_note_is_counted_and_still_present(tmp_path, cfg):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg)
    assert "apple:N-LOCKED" in scan["present_ids"]
    assert "apple:N-LOCKED" not in _by_id(scan)
    assert "1 × locked note (password-protected — its text is encrypted)" in scan["skipped"]


def test_a_note_without_its_text_or_with_a_corrupt_one_is_present_not_imported(tmp_path, cfg):
    store = make_store(tmp_path / "NoteStore.sqlite", [
        {"id": "N-CLOUD", "title": "Not downloaded", "folder": "Notes",
         "created": cd(2026, 5, 1), "modified": cd(2026, 5, 1)},
        {"id": "N-BAD", "title": "Corrupt", "folder": "Notes",
         "data": gzip.compress(b"\xff" * 11, mtime=0),
         "created": cd(2026, 5, 1), "modified": cd(2026, 5, 1)},
        {"id": "N-OK", "folder": "Notes", "text": "First line is the title\nmore",
         "created": cd(2026, 5, 1), "modified": cd(2026, 5, 1)}])
    scan = notes.scan(store, cfg)
    assert {"apple:N-CLOUD", "apple:N-BAD", "apple:N-OK"} <= scan["present_ids"]
    assert set(_by_id(scan)) == {"apple:N-OK"}
    assert _by_id(scan)["apple:N-OK"]["title"] == "First line is the title"
    joined = " | ".join(scan["skipped"])
    assert "not yet downloaded" in joined and "could not be decoded" in joined


def test_a_corrupt_timestamp_is_a_counted_skip_not_a_traceback(tmp_path, cfg):
    store = make_store(tmp_path / "NoteStore.sqlite", [
        {"id": "N-FAR", "folder": "Notes", "text": "From the year 10^300",
         "created": 1e300, "modified": None},
        {"id": "N-TEXT", "folder": "Notes", "text": "Typed date",
         "created": "yesterday", "modified": cd(2026, 5, 1)}])
    scan = notes.scan(store, cfg)
    assert "1 × note with no usable timestamp" in scan["skipped"]
    assert "apple:N-FAR" in scan["present_ids"]
    # an unreadable creation date falls back to the last edit
    assert _by_id(scan)["apple:N-TEXT"]["started_at"] == "2026-05-01T12:00:00Z"


def test_an_older_store_layout_is_read_by_introspection(tmp_path, cfg):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite", layout="old"), cfg)
    assert set(_by_id(scan)) == {"apple:N-BLED", "apple:N-GIFTS"}
    rec = _by_id(scan)["apple:N-BLED"]
    assert rec["started_at"] == "2022-09-26T16:30:00Z"
    assert json.loads(rec["value_json"])["folder"] == "Travel"
    # No Z_METADATA: the store does not say which it is, so nothing mirrors.
    assert scan["account"] is None


def test_the_account_is_the_stores_own_uuid(tmp_path, cfg):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg)
    assert scan["account"] == f"apple_notes:{STORE_UUID}"
    facts = scan["facts"]
    assert facts["folders"] == {"Notes": 1, "Travel": 1}
    assert facts["oldest_note"] == "2022-09-26"


def test_a_folder_holding_the_store_reads_the_store(tmp_path, cfg):
    container = tmp_path / "group.com.apple.notes"
    container.mkdir()
    make_store(container / "NoteStore.sqlite")
    assert set(_by_id(notes.scan(container, cfg))) == {"apple:N-BLED", "apple:N-GIFTS"}


def test_writes_only_in_the_wal_are_read(tmp_path, cfg):
    """A store copied off a running Mac: main file plus -wal, no -shm."""
    folder = tmp_path / "copied"
    folder.mkdir()
    make_store(tmp_path / "live.sqlite",
               wal_to=(folder / "NoteStore.sqlite", folder / "NoteStore.sqlite-wal"))
    assert not (folder / "NoteStore.sqlite-shm").exists()
    scan = notes.scan(folder / "NoteStore.sqlite", cfg)
    assert set(_by_id(scan)) == {"apple:N-BLED", "apple:N-GIFTS"}


def test_a_store_open_in_another_process_is_snapshotted_not_read_in_place(tmp_path, cfg):
    store = make_store(tmp_path / "NoteStore.sqlite")
    live = sqlite3.connect(store)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZTITLE1='Renamed' "
                     "WHERE ZIDENTIFIER='N-BLED'")
        live.commit()
        scan = notes.scan(store, cfg)
    finally:
        live.close()
    assert _by_id(scan)["apple:N-BLED"]["title"] == "Renamed"


def test_two_scans_of_one_store_are_identical(tmp_path, cfg):
    store = make_store(tmp_path / "NoteStore.sqlite")
    a, b = notes.scan(store, cfg), notes.scan(store, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]


def test_a_database_that_is_not_a_notes_store_fails_naming_its_tables(tmp_path, cfg):
    p = tmp_path / "chat.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    with pytest.raises(DomainError, match=r"not an Apple Notes store .*has: message"):
        notes.scan(p, cfg)


def test_a_file_that_is_not_a_database_is_refused(tmp_path, cfg):
    p = tmp_path / "NoteStore.sqlite"
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    with pytest.raises(DomainError, match="cannot read NoteStore.sqlite as a database"):
        notes.scan(p, cfg)


def test_a_store_it_may_not_open_asks_for_full_disk_access(tmp_path, cfg, monkeypatch):
    """macOS privacy protection reads as 'unable to open database file'."""
    store = make_store(tmp_path / "NoteStore.sqlite")
    real = sqlite3.connect

    def guarded(target, *a, **kw):
        if "mode=ro" in str(target):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **kw)
    monkeypatch.setattr(notes.sqlite3, "connect", guarded)
    with pytest.raises(DomainError, match="Full Disk Access") as e:
        notes.scan(store, cfg)
    assert "\n" not in str(e.value)


def test_the_report_sample_does_not_repeat_the_title_line(tmp_path, cfg, conn):
    scan = notes.scan(make_store(tmp_path / "NoteStore.sqlite"), cfg)
    lines = notes.report_lines(scan, obs.build_plan(conn, scan))
    sample = next(ln for ln in lines if ln.startswith("  note: "))
    assert '"Gift ideas": - ceramics voucher' in sample
    # The body itself keeps the note as Notes holds it, title line included.
    rec = next(r for r in scan["records"] if r["title"] == "Gift ideas")
    assert rec["body"].startswith("Gift ideas\n")


# ---------------------------------------------------------------- apply, mirroring

def test_apply_then_reapply_is_identical_and_doctor_is_green(tmp_path, cfg, conn):
    store = make_store(tmp_path / "NoteStore.sqlite")
    _, first = _apply(conn, notes.scan(store, cfg))
    assert first["added"] == 2
    _, again = _apply(conn, notes.scan(store, cfg))
    assert again["added"] == 0 and again["identical"] == 2
    for table in ("entries", "measurements", "mentions", "entity_aliases"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_note_deleted_on_the_phone_leaves_and_a_locked_one_stays(tmp_path, cfg, conn):
    _apply(conn, notes.scan(make_store(tmp_path / "a.sqlite"), cfg))
    later = [dict(n) for n in NOTES if n["id"] != "N-GIFTS"]           # deleted outright
    for n in later:
        if n["id"] == "N-BLED":
            n["folder"] = "Recently Deleted"                            # moved to the trash
    # Both notes of the account at once is the whole archive: past the
    # mass-removal bar.
    plan, result = _apply(conn, notes.scan(make_store(tmp_path / "b.sqlite", later), cfg),
                          allow_mass_removal=True)
    assert sorted(r["external_id"] for r in plan["removed"]) == ["apple:N-BLED", "apple:N-GIFTS"]
    assert result["removed"] == 2
    text = "\n".join(notes.report_lines(notes.scan(tmp_path / "b.sqlite", cfg), plan))
    assert "remove: 2 no longer in the source (deleted or trashed)" in text


def test_a_recovered_note_returns(tmp_path, cfg, conn):
    _apply(conn, notes.scan(make_store(tmp_path / "a.sqlite"), cfg))
    gone = [n for n in NOTES if n["id"] != "N-BLED"]
    _apply(conn, notes.scan(make_store(tmp_path / "b.sqlite", gone), cfg))
    _, result = _apply(conn, notes.scan(make_store(tmp_path / "c.sqlite"), cfg))
    assert result["added"] == 1


def test_another_macs_store_is_never_touched(tmp_path, cfg, conn):
    _apply(conn, notes.scan(make_store(tmp_path / "a.sqlite"), cfg))
    other = make_store(tmp_path / "b.sqlite", [NOTES[0]])
    c = sqlite3.connect(other)
    c.execute("UPDATE Z_METADATA SET Z_UUID='ANOTHER-MAC'")
    c.commit()
    c.close()
    assert obs.build_plan(conn, notes.scan(other, cfg))["removed"] == []
