"""Sample call history: the persona's calls as a MacBook's CallHistory.storedata.

With the iPhone's calls syncing to the Mac, the Mac's
~/Library/Application Support/CallHistoryDB/CallHistory.storedata holds the
phone's Recents. This writes that Core Data store — Z_METADATA (the store's
identity, which the connector scopes mirroring to), Z_PRIMARYKEY, and
ZCALLRECORD with the columns iOS writes — in a fake home Library, beside the
same Contacts store the Messages sample writes, and returns the store's path
for `observe ios_calls`.

Numbers are spelled the way the phone records them: incoming calls in E.164,
outgoing ones as dialled (ten digits), so the import's normalization shows.

The Windows + iPhone combo has no sample: there the only copy is an iPhone
backup, and an unencrypted one has no call history (docs/calls.md).
"""

from __future__ import annotations

import plistlib
import sqlite3
import uuid
from datetime import date, timezone
from pathlib import Path

from .. import persona
from .mac_messages import write_address_book

APPLE_EPOCH = 978307200
_NS = uuid.UUID("0c2e7a4b-91d3-4f6a-b8e5-5d3c1a9f2e70")
STORE_UUID = str(uuid.uuid5(_NS, "store")).upper()

_SCHEMA = """
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER,
  Z_MAX INTEGER);
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZANSWERED INTEGER, ZCALL_CATEGORY INTEGER, ZCALLTYPE INTEGER,
  ZDISCONNECTED_CAUSE INTEGER, ZFACE_TIME_DATA BLOB, ZHANDLE_TYPE INTEGER,
  ZNUMBER_AVAILABILITY INTEGER, ZORIGINATED INTEGER, ZREAD INTEGER,
  ZJUNKCONFIDENCE INTEGER, ZVERIFICATIONSTATUS INTEGER, ZDATE TIMESTAMP,
  ZDURATION FLOAT, ZADDRESS VARCHAR, ZDEVICE_ID VARCHAR, ZISO_COUNTRY_CODE VARCHAR,
  ZLOCATION VARCHAR, ZNAME VARCHAR, ZSERVICE_PROVIDER VARCHAR, ZUNIQUE_ID VARCHAR,
  ZPARTICIPANT_GROUP_UUIDS BLOB, ZREMOTE_PARTICIPANT_HANDLES BLOB);
"""

_AREA = {"512": "Austin, TX", "210": "San Antonio, TX"}


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo in persona.COMBOS and persona.has_iphone(combo) and not persona.on_mac(combo):
        # Windows + iPhone: the only copy within reach is an iPhone backup, and
        # Apple keeps CallHistory.storedata for encrypted backups only.
        raise ValueError("an unencrypted iPhone backup carries no call history — Apple "
                         "keeps it for encrypted backups, which cannot be read; the "
                         "Windows + iPhone setup has no calls to import (docs/calls.md)")
    if combo != "iphone":
        raise ValueError(f"ios_calls has no sample for combo '{combo}' — iPhone call "
                         f"history belongs to the MacBook + iPhone setup")
    app_support = Path(dest) / "Library" / "Application Support"
    write_address_book(app_support)
    store = app_support / "CallHistoryDB" / "CallHistory.storedata"
    store.parent.mkdir(parents=True, exist_ok=True)
    for stale in (store, store.with_name(store.name + "-wal"),
                  store.with_name(store.name + "-shm")):
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(store)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO Z_PRIMARYKEY VALUES (1, 'CallRecord', 0, ?)",
                     (len(persona.CALLS),))
        conn.execute("INSERT INTO Z_METADATA VALUES (1, ?, ?)",
                     (STORE_UUID, plistlib.dumps({"NSStoreType": "SQLite",
                                                  "NSStoreUUID": STORE_UUID},
                                                 fmt=plistlib.FMT_BINARY)))
        for pk, (offset, hhmm, key, seconds, kind) in enumerate(persona.CALLS, start=1):
            who = persona.person(key)
            when = persona.local_dt(anchor, offset, hhmm).astimezone(timezone.utc)
            out = kind == "out"
            address = who.phone[2:] if out else who.phone      # dialled vs received
            conn.execute(
                "INSERT INTO ZCALLRECORD (Z_PK, Z_ENT, Z_OPT, ZANSWERED, ZCALL_CATEGORY, "
                "ZCALLTYPE, ZDISCONNECTED_CAUSE, ZHANDLE_TYPE, ZNUMBER_AVAILABILITY, "
                "ZORIGINATED, ZREAD, ZDATE, ZDURATION, ZADDRESS, ZISO_COUNTRY_CODE, "
                "ZLOCATION, ZNAME, ZSERVICE_PROVIDER, ZUNIQUE_ID) "
                "VALUES (?, 1, 1, ?, 1, 1, 0, 2, 0, ?, 1, ?, ?, ?, 'us', ?, ?, "
                "'com.apple.Telephony', ?)",
                (pk, int(kind != "missed"), int(out),
                 when.timestamp() - APPLE_EPOCH + 0.25, float(seconds), address,
                 _AREA.get(who.phone[2:5]), who.name,
                 str(uuid.uuid5(_NS, f"call/{offset}/{hhmm}/{key}")).upper()))
        conn.commit()
    finally:
        conn.close()
    return store
