"""Seeding helpers for the read-side feature tests (tests/test_features_*.py).

One small world, written through the real domain layer: the owner, Jake,
Mom and Priya with linked handles; observed rows built like a connector's
scan; entries with mentions. Every helper returns what a test needs to
assert against.
"""

from __future__ import annotations

from datetime import date

from lifecrm.domain import days as days_mod
from lifecrm.domain import entities, entries, measurements
from lifecrm.domain import observations as obs
from lifecrm.domain.util import canonical_json

TODAY = date(2026, 10, 21)
OWNER = "+15125550100"
JAKE = "+15125550142"
MOM = "+12105550133"
PRIYA = "priya@acme-robotics.example"

_n = [0]


def rec(stream: str, day: str, hhmm: str = "12:00", parts=(), **kw) -> dict:
    _n[0] += 1
    r = {"stream": stream, "external_id": f"fx-{_n[0]:05d}", "journal_date": day,
         "started_at": f"{day}T{hhmm}:00Z", "direction": None,
         "participants": [{"kind": k, "value": v, "role": role} for k, v, role in parts]}
    if "value_json" in kw and isinstance(kw["value_json"], dict):
        kw["value_json"] = canonical_json(kw["value_json"])
    r.update(kw)
    return r


def apply(conn, records: list[dict], *, connector: str = "feature_fixture",
          account: str = OWNER, handles=(), coverage=(), source: str = "fixture",
          last_modified: str | None = None) -> dict:
    """One import of `records` (a connector-shaped scan), applied."""
    if last_modified:
        for r in records:
            r.setdefault("last_modified_at", last_modified)
    scan = {"connector": connector, "account": account, "source_file": source,
            "source_hash": (source * 64)[:64].ljust(64, "0"), "records": records,
            "handles": list(handles), "coverage": list(coverage)}
    plan = obs.build_plan(conn, scan)
    obs.apply_plan(conn, scan, plan)
    return plan


def people(conn) -> dict[str, int]:
    """The owner and three people, each linked to a handle (created on demand
    by a contact card so the handle exists)."""
    cards = [rec("contact_card", "2026-10-01", parts=[("phone", OWNER, "participant")],
                 title="Alex Rivera", value_json={}),
             rec("contact_card", "2026-10-01", parts=[("phone", JAKE, "participant")],
                 title="Jake Morales",
                 value_json={"birthday": "1990-10-30",
                             "phones": [{"label": "mobile", "number": JAKE}]}),
             rec("contact_card", "2026-10-01", parts=[("phone", MOM, "participant")],
                 title="Linda Rivera", value_json={}),
             rec("contact_card", "2026-10-01", parts=[("email", PRIYA, "participant")],
                 title="Priya Natarajan", value_json={})]
    apply(conn, cards, connector="contacts_fixture", source="cards",
          handles=[{"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
                   {"kind": "phone", "value": JAKE, "display_name": "Jake Morales"},
                   {"kind": "phone", "value": MOM, "display_name": "Linda Rivera"},
                   {"kind": "email", "value": PRIYA, "display_name": "Priya Natarajan"}])
    out = {}
    for name, kind, value in (("Alex Rivera", "phone", OWNER), ("Jake Morales", "phone", JAKE),
                              ("Linda Rivera", "phone", MOM),
                              ("Priya Natarajan", "email", PRIYA)):
        out[name] = entities.create(conn, "person", name)["id"]
        obs.link_handle(conn, kind, value, out[name])
    return out


def entry(conn, day: str, kind: str, title: str, category: str = "social", *,
          mentions=(), role: str = "participant", **kw) -> dict:
    e = entries.create(conn, journal_date=day, kind=kind, title=title,
                       primary_category=category, **kw)
    for eid in mentions:
        entries.attach_entity(conn, e["id"], eid, role)
    return e


def summary(conn, day: str, text: str) -> None:
    days_mod.set_summary(conn, day, text)


def mood(conn, day: str, value: float) -> None:
    measurements.add(conn, journal_date=day, metric="mood", numeric_value=value)


def sms(day: str, hhmm: str, who: str, text: str, direction: str = "in") -> dict:
    parts = ([("phone", who, "sender"), ("phone", OWNER, "recipient")] if direction == "in"
             else [("phone", OWNER, "sender"), ("phone", who, "recipient")])
    return rec("android_sms", day, hhmm, parts, direction=direction, body=text,
               value_json={"thread_id": f"t-{who}"})


def txn(day: str, title: str, amount: float, unit: str = "USD") -> dict:
    return rec("txn", day, "12:00", [], title=title, value=-abs(amount), unit=unit,
               direction="out", origin_app="bank")
