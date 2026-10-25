"""The demo's questions, answered from the persona's whole life.

`build_world` renders lifecrm/demo/persona.py into a fresh database the way
the demo will: the journal as confirmed entries (with closures), and every
other source as observed-tier imports through the real write layer —
`observations.apply_plan` fed scans shaped like each connector's, using the
fixed stream names — plus the AI chat exports from the real generator read
by the real connector, and Health Connect-style health records. Handles are
then linked to people, as the owner would with `handles link`.

Every persona QUESTION must come back with the fact that answers it, cited,
and every deterministic answer must pass its own citation check.
"""

import json
from datetime import date, timedelta

import pytest

from lifecrm import db as db_mod, recall
from lifecrm.config import Config
from lifecrm.connectors import ai_chat as ai_chat_connector
from lifecrm.connectors import base
from lifecrm.demo import persona
from lifecrm.demo.sources import ai_chat as ai_chat_sample
from lifecrm.doctor import run_checks
from lifecrm.domain import (corrections, entities, entries, health, measurements,
                            observations as obs)
from lifecrm.domain.util import canonical_json, now_utc
from lifecrm.importer import import_text

ANCHOR = date(2026, 10, 21)            # a Wednesday
OWNER_PHONE = persona.OWNER.phone
OWNER_LABEL = "you (this phone)"


# ---------------------------------------------------------------- the world

def _utc(dt) -> str:
    return persona.utc_iso(dt)


def _person_handle(key: str) -> dict:
    p = persona.person(key)
    return {"kind": "phone", "value": p.phone, "display_name": p.name}


def _owner_handle() -> dict:
    return {"kind": "phone", "value": OWNER_PHONE, "display_name": OWNER_LABEL}


def _rec(cfg, stream, external_id, started, **kw) -> dict:
    rec = {"stream": stream, "external_id": external_id, "started_at": started,
           "journal_date": base.journal_date_for_iso(started, cfg),
           "ended_at": None, "utc_offset_sec": None, "direction": None,
           "title": None, "body": None, "value": None, "unit": None,
           "value_json": None, "media_path": None, "origin_app": None,
           "last_modified_at": started, "participants": []}
    rec.update(kw)
    return rec


def _apply(conn, scan) -> dict:
    plan = obs.build_plan(conn, scan)
    return obs.apply_plan(conn, scan, plan)


def _scan(connector, records, handles, account=None, source_file=None):
    unique = {(h["kind"], h["value"]): h for h in handles}
    return base.empty_scan(connector, account=account,
                           source_file=source_file or f"{connector}-sample",
                           records=records, handles=list(unique.values()))


def texts_scan(cfg, anchor, combo):
    stream = "imessage" if combo == "iphone" else "android_sms"
    connector = "ios_messages" if combo == "iphone" else "android_sms"
    records, handles = [], [_owner_handle()]
    for thread in persona.THREADS:
        members = [_person_handle(k) for k in thread.members]
        handles += members
        for n, (off, hhmm, sender, text) in enumerate(thread.messages):
            started = _utc(persona.local_dt(anchor, off, hhmm))
            me = sender == "me"
            parts = [{"kind": "phone", "value": OWNER_PHONE if me
                      else persona.person(sender).phone, "role": "sender"}]
            for h in [_owner_handle()] + members:
                if h["value"] != parts[0]["value"]:
                    parts.append({"kind": "phone", "value": h["value"],
                                  "role": "recipient"})
            records.append(_rec(
                cfg, stream, f"{combo}-{thread.key}-{n}", started,
                direction="out" if me else "in", title=thread.name, body=text,
                value_json=canonical_json({"chat": f"chat-{thread.key}",
                                           **({"group": True} if thread.name else {})}),
                origin_app="messages", participants=parts))
    return _scan(connector, records, handles, account=OWNER_PHONE)


def whatsapp_scan(cfg, anchor, combo):
    g = persona.WHATSAPP_GROUP
    rosa = {"kind": "app_account", "value": "whatsapp:Tía Rosa",
            "display_name": "Tía Rosa"}
    handles = [_owner_handle(), _person_handle("mom"), rosa]
    records = []
    for n, (off, hhmm, sender, text) in enumerate(g["messages"]):
        started = _utc(persona.local_dt(anchor, off, hhmm))
        who = {"me": _owner_handle(), "mom": _person_handle("mom")}.get(sender, rosa)
        parts = [{"kind": who["kind"], "value": who["value"], "role": "sender"}]
        parts += [{"kind": h["kind"], "value": h["value"], "role": "recipient"}
                  for h in handles if h["value"] != who["value"]]
        records.append(_rec(cfg, "whatsapp", f"wa-{combo}-{n}", started,
                            direction="out" if sender == "me" else "in",
                            title=g["name"], body=text, origin_app="whatsapp",
                            participants=parts))
    return _scan("whatsapp", records, handles, account=OWNER_PHONE)


def calls_scan(cfg, anchor, combo):
    stream = "ios_call" if combo == "iphone" else "android_call"
    records, handles = [], [_owner_handle()]
    for n, (off, hhmm, key, seconds, kind) in enumerate(persona.CALLS):
        handles.append(_person_handle(key))
        start = persona.local_dt(anchor, off, hhmm)
        out = kind == "out"
        them = persona.person(key).phone
        parts = [{"kind": "phone", "value": OWNER_PHONE if out else them, "role": "sender"},
                 {"kind": "phone", "value": them if out else OWNER_PHONE,
                  "role": "recipient"}]
        records.append(_rec(
            cfg, stream, f"call-{combo}-{n}", _utc(start),
            ended_at=_utc(start + timedelta(seconds=seconds)),
            direction="out" if out else "in", value=seconds, unit="s",
            value_json=canonical_json({"missed": True} if kind == "missed" else {}),
            participants=parts))
    return _scan("calls", records, handles, account=OWNER_PHONE)


def contacts_scan(cfg, anchor, combo):
    records, handles = [], []
    for p in persona.PEOPLE:
        lines = []
        if p.note:
            lines.append(f"Notes: {p.note}")
        if p.city:
            lines.append(f"Address: {p.city}")
        if p.birthday:
            m, d = p.birthday
            lines.append(f"Birthday: {date(2000, m, d):%B} {d}"
                         + (f", {p.birth_year}" if p.birth_year else ""))
        if p.org:
            lines.append(f"Company: {p.org}")
        parts = [{"kind": "phone", "value": p.phone, "role": "participant"}]
        handles.append(_person_handle(p.key))
        if p.email:
            parts.append({"kind": "email", "value": p.email, "role": "participant"})
            handles.append({"kind": "email", "value": p.email, "display_name": p.name})
        # The card's structured fields, as the contacts connector stores them:
        # the birthday (pinned for Maya and Sam, like the demo's vCards).
        detail = {}
        when = persona.birthday_date(p, anchor)
        if when is not None:
            detail["birthday"] = (f"{p.birth_year}-{when.month:02d}-{when.day:02d}"
                                  if p.birth_year else f"--{when.month:02d}-{when.day:02d}")
        records.append(_rec(cfg, "contact_card", f"card-{p.key}",
                            _utc(persona.local_dt(anchor, -60, "09:00")),
                            title=p.name, body="\n".join(lines),
                            value_json=canonical_json(detail) if detail else None,
                            participants=parts))
    return _scan("contacts", records, handles, account=persona.OWNER.email)


def fitness_scan(cfg, anchor, combo):
    """The persona's workouts as a Strong export: a full-history export, so
    the days between the first and the last are declared complete."""
    records = []
    for off, (kind, hhmm, minutes, km) in sorted(persona._WORKOUTS.items()):
        start = persona.local_dt(anchor, off, hhmm)
        detail = {"app": "strong", "exercises": []}
        if km:
            detail["distance_km"] = km
        records.append(_rec(cfg, "workout", f"strong:{-off}", _utc(start),
                            ended_at=_utc(start + timedelta(minutes=minutes)),
                            title=kind.capitalize(), value=float(minutes), unit="min",
                            origin_app="strong", value_json=canonical_json(detail)))
    scan = _scan("fitness", records, [], account="strong")
    scan["coverage"] = [{"stream": "workout", "account": "strong",
                         "start": persona.day(anchor, -persona.WINDOW_DAYS + 1).isoformat(),
                         "end": anchor.isoformat(), "basis": "full-history export"}]
    return scan


def calendar_scan(cfg, anchor, combo):
    records, handles = [], [{"kind": "email", "value": persona.OWNER.email,
                             "display_name": persona.OWNER.name}]
    for ev in persona.CALENDAR:
        occurrences = range(ev.weekly_count or 1)
        for k in occurrences:
            off = ev.offset + 7 * k
            if off > 0:
                break
            start = persona.local_dt(anchor, off, ev.start)
            parts = [{"kind": "email", "value": persona.OWNER.email, "role": "organizer"}]
            for key in ev.attendees:
                p = persona.person(key)
                handles.append({"kind": "email", "value": p.email, "display_name": p.name})
                parts.append({"kind": "email", "value": p.email, "role": "participant"})
            detail = {}
            if ev.place:
                pl = persona.place(ev.place)
                detail["location"] = pl.address or pl.name
            records.append(_rec(
                cfg, "calendar_event", f"{ev.uid}-{k}", _utc(start),
                ended_at=_utc(start + timedelta(minutes=ev.minutes)),
                title=ev.title, body=ev.description,
                value_json=canonical_json(detail), participants=parts))
    return _scan("calendar", records, handles, account=persona.OWNER.email)


def finance_scan(cfg, anchor, combo):
    records = []
    for n, t in enumerate(persona.TRANSACTIONS):
        dt = (persona.trip_dt(t.trip_day or 0, "12:00", anchor) if t.offset is None
              else persona.local_dt(anchor, t.offset, "12:00"))
        records.append(_rec(cfg, "txn", f"txn-{n}", _utc(dt), title=t.merchant,
                            body=t.memo, value=t.amount, unit="USD"))
    return _scan("finance", records, [], account="checking-0042")


def notes_scan(cfg, anchor, combo):
    records = []
    for n in persona.NOTES:
        dt = (persona.trip_dt(n.trip_day or 0, "08:00", anchor) if n.offset is None
              else persona.local_dt(anchor, n.offset, "20:00"))
        records.append(_rec(cfg, "note", f"note-{n.key}", _utc(dt), title=n.title,
                            body=n.body))
    return _scan("notes", records, [])


def photos_scan(cfg, anchor, combo):
    records = []
    for p in persona.PHOTOS:
        pl = persona.place(p.place)
        records.append(_rec(cfg, "photo", f"photo-{p.name}",
                            _utc(persona.photo_dt(p, anchor)),
                            title=f"{p.name}.jpg", body=p.caption,
                            value_json=canonical_json({"lat": pl.lat, "lon": pl.lon})))
    return _scan("photos", records, [])


def _entity_id(conn, ids, key):
    if key in ids:
        return ids[key]
    kind, _, name = key.partition(":")
    ids[key] = entities.create(conn, kind, name)["id"]
    return ids[key]


def journal(conn, cfg, anchor, ids):
    """Each note becomes a source with its transcript, and each entry cites
    the exact sentence it came from — the provenance /log would record."""
    keyed, closes = {}, []
    for jd in persona.JOURNAL:
        if jd.pending:
            continue
        day = persona.day(anchor, jd.offset).isoformat()
        for note in jd.notes:
            src = import_text(conn, cfg, note.text, journal_date=day,
                              recorded_at=_utc(persona.local_dt(anchor, jd.offset,
                                                                note.time)))
            text = conn.execute("SELECT text FROM transcripts WHERE id=?",
                                (src["transcript_id"],)).fetchone()["text"]
            for sentence, e in note.sentences:
                if not e:
                    continue
                occurred = (persona.local_dt(anchor, jd.offset, e["occurred"]).isoformat()
                            if e.get("occurred") else None)
                due = (persona.day(anchor, e["due_offset"]).isoformat()
                       if e.get("due_offset") is not None else None)
                row = entries.create(
                    conn, journal_date=day, kind=e["kind"], title=e["title"],
                    primary_category=e["primary"],
                    secondary_categories=e.get("secondary"), body=sentence,
                    time_of_day=e.get("time_of_day"), occurred_at=occurred,
                    due_at=due, data=e.get("data"))
                start = text.index(sentence)
                entries.add_provenance(conn, entry_id=row["id"],
                                       source_id=src["source_id"],
                                       transcript_id=src["transcript_id"],
                                       char_start=start, char_end=start + len(sentence))
                for key, role in e.get("entities", ()):
                    entries.attach_entity(conn, row["id"], _entity_id(conn, ids, key),
                                          role)
                for metric, value, unit in e.get("measurements", ()):
                    measurements.add(conn, entry_id=row["id"], metric=metric,
                                     numeric_value=value, unit=unit)
                if e.get("key"):
                    keyed[e["key"]] = row["uuid"]
                if e.get("closes"):
                    closes.append(e["closes"])
        for metric, value in (("mood", jd.mood), ("energy", jd.energy),
                              ("sleep_hours", jd.sleep_hours)):
            if value is not None:
                measurements.add(conn, journal_date=day, metric=metric,
                                 numeric_value=value, unit="h" if metric == "sleep_hours" else None)
    for key in closes:
        corrections.close_entry(conn, keyed[key], "done")
    return keyed


def health_records(conn, cfg, anchor, combo):
    origin = "com.sec.android.app.shealth" if combo == "android" else "com.apple.health"
    for n, d in enumerate(persona.health_series(anchor)):
        day = d.date.isoformat()
        health.insert(conn, {
            "metric": "sleep_session", "journal_date": day,
            "started_at": _utc(d.sleep_start), "ended_at": _utc(d.sleep_end),
            "value": round(d.sleep_hours * 60, 1), "unit": "min",
            "origin_app": origin, "external_id": f"sleep{n:04d}" * 4,
            "source_file": f"health-{combo}.zip"})
        noon = persona.local_dt(anchor, (d.date - anchor).days, "12:00")
        health.insert(conn, {
            "metric": "steps", "journal_date": day, "started_at": _utc(noon),
            "value": d.steps, "unit": "count", "origin_app": origin,
            "external_id": f"step{n:04d}" * 4, "source_file": f"health-{combo}.zip"})
        health.insert(conn, {
            "metric": "resting_heart_rate", "journal_date": day, "started_at": _utc(noon),
            "value": d.resting_hr, "unit": "bpm", "origin_app": origin,
            "external_id": f"rhr{n:04d}" * 4, "source_file": f"health-{combo}.zip"})


def build_world(conn, cfg, tmp_path, anchor=ANCHOR, combo="android") -> dict:
    """The persona's life in one database; returns names -> entity ids and
    the import uuids by connector."""
    ids = {p.key: entities.create(conn, "person", p.name, aliases=list(p.aliases))["id"]
           for p in persona.PEOPLE}
    ids |= {pl.key: entities.create(conn, "place", pl.name, aliases=list(pl.aliases))["id"]
            for pl in persona.PLACES}
    journal(conn, cfg, anchor, ids)
    health_records(conn, cfg, anchor, combo)
    imports = {}
    for make in (texts_scan, whatsapp_scan, calls_scan, contacts_scan, calendar_scan,
                 finance_scan, notes_scan, photos_scan, fitness_scan):
        scan = make(cfg, anchor, combo)
        imports[scan["connector"]] = _apply(conn, scan)["import_uuid"]
    for path in ai_chat_sample.write_samples(tmp_path / "ai", anchor, combo):
        scan = ai_chat_connector.scan(path, cfg)
        imports[f"ai_chat:{scan['flavor']}"] = _apply(conn, scan)["import_uuid"]
    # The owner resolves each person's handles once — every message, call and
    # card that ever named them is attributed from then on.
    for p in persona.PEOPLE:
        for kind, value in (("phone", p.phone), ("email", p.email)):
            if conn.execute("SELECT 1 FROM entity_handles WHERE kind=? AND value=?",
                            (kind, value)).fetchone():
                obs.link_handle(conn, kind, value, ids[p.key])
    conn.commit()
    return {"ids": ids, "imports": imports}


def make_cfg(root) -> Config:
    """The conftest `cfg`, for fixtures wider than one test."""
    for d in ("data", "data/staging", "data/transcripts", "backups"):
        (root / d).mkdir(parents=True, exist_ok=True)
    return Config(root=root, data_dir=root / "data", backups_dir=root / "backups",
                  inbox_dir=None, inbox_text_dir=None, archive_dir=None,
                  onedrive_backups_dir=None, timezone="America/Chicago",
                  day_rollover_hour=5, whisper={"model": "small"}, ingest={})


@pytest.fixture(scope="module", params=persona.COMBOS)
def world(request, tmp_path_factory):
    """Built once per combo: every test below only reads it."""
    root = tmp_path_factory.mktemp(f"world-{request.param}")
    cfg = make_cfg(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    info = build_world(conn, cfg, root, combo=request.param)
    yield {**info, "conn": conn, "cfg": cfg, "combo": request.param}
    conn.close()


def ask(world, question):
    bundle = recall.build_evidence(world["conn"], world["cfg"], question, today=ANCHOR)
    text = recall.deterministic_answer(bundle)
    return bundle, text


def _item(bundle, cid):
    for key in ("metrics", "entries", "passages", "observations"):
        for x in bundle.get(key, []):
            if x["id"] == cid:
                return x
    raise KeyError(cid)


def _cited_with(bundle, text, needle):
    """The ids cited in the sentence(s) of `text` that mention `needle`."""
    out = []
    for sentence in recall._sentences(text):
        if needle.lower() in sentence.lower():
            out += [c for c in recall._cited(sentence)]
    return out


# ---------------------------------------------------------------- the generator

@pytest.mark.parametrize("combo", persona.COMBOS)
def test_ai_chat_sample_is_the_vendors_real_shape(tmp_path, cfg, combo):
    claude, chatgpt = ai_chat_sample.write_samples(tmp_path, ANCHOR, combo)
    c = ai_chat_connector.scan(claude, cfg)
    g = ai_chat_connector.scan(chatgpt, cfg)
    assert (c["flavor"], g["flavor"]) == ("claude", "chatgpt")
    assert c["account"] and g["account"] == persona.OWNER.email
    bled = [r for r in c["records"] if r["title"] == "Return trip to Lake Bled"]
    first = min(bled, key=lambda r: r["started_at"])
    assert first["direction"] == "out"
    assert "Lake Bled in Slovenia four years ago" in first["body"]
    assert first["journal_date"] == (ANCHOR - timedelta(days=16)).isoformat()
    assert {r["direction"] for r in bled} == {"in", "out"}
    # ChatGPT's hidden system turn is in the tree but never imported
    assert len(g["records"]) == 2
    assert any("150 g of protein" in r["body"] for r in g["records"])
    assert all(json.loads(r["value_json"])["on_current_path"] for r in g["records"])


def test_ai_chat_sample_is_deterministic_and_rejects_unknown_combos(tmp_path):
    a = ai_chat_sample.write_sample(tmp_path / "a", ANCHOR, "android")
    b = ai_chat_sample.write_sample(tmp_path / "b", ANCHOR, "android")
    assert a.read_bytes() == b.read_bytes()
    other = ai_chat_sample.write_sample(tmp_path / "c", ANCHOR + timedelta(days=1),
                                        "android")
    assert other.read_bytes() != a.read_bytes()
    with pytest.raises(ValueError, match="unknown combo"):
        ai_chat_sample.write_sample(tmp_path, ANCHOR, "blackberry")
    with pytest.raises(ValueError, match="vendor"):
        ai_chat_sample.write_sample(tmp_path, ANCHOR, "iphone", vendor="bard")


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_ai_chat_sample_applies_once_and_keeps_doctor_green(conn, cfg, tmp_path, combo):
    paths = ai_chat_sample.write_samples(tmp_path, ANCHOR, combo)
    for path in paths:
        scan = ai_chat_connector.scan(path, cfg)
        assert _apply(conn, scan)["added"] == len(scan["records"])
    conn.commit()
    for path in paths:
        scan = ai_chat_connector.scan(path, cfg)
        again = _apply(conn, scan)
        assert again["added"] == 0 and again["identical"] == len(scan["records"])
    conn.commit()
    for table in ("entries", "measurements", "mentions", "entity_aliases"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_persona_world_keeps_doctor_green(world):
    assert run_checks(world["conn"], world["cfg"])["failures"] == 0


# ---------------------------------------------------------------- the questions

def test_every_demo_question_has_cited_evidence_and_a_self_consistent_answer(world):
    # This world holds the first sources only; the questions about mail,
    # workouts, nutrition, payments, reading, searches, rides and LinkedIn are
    # answered from those generators' own exports in tests/test_more_ask_demo.py.
    for q in persona.QUESTIONS[:10]:
        bundle, text = ask(world, q)
        assert bundle["has_evidence"], q
        assert bundle["answered"], q
        check = recall.check_citations(text, bundle)
        assert check["cited"], (q, text)
        assert check["unknown"] == [], (q, check)
        assert check["unsupported"] == [], (q, check["unsupported"])


def test_how_did_i_sleep_last_week(world):
    bundle, text = ask(world, "how did I sleep last week")
    sleep = next(m for m in bundle["metrics"] if m["metric"] == "sleep_session")
    assert sleep["days"] == 7
    assert bundle["window"]["label"] == "last week"
    assert f"[{sleep['id']}]" in text and text.startswith("Sleep totalled")
    assert bundle["observations"] == []          # a metric question, not a text search


def test_jakes_favorite_restaurant_comes_from_his_contact_card(world):
    bundle, text = ask(world, "what's Jake's favorite restaurant")
    (jake,) = bundle["entities"]
    assert jake["name"] == "Jake Morales" and jake["via"] == "first name"
    assert "Veracruz All Natural" in text
    card = _item(bundle, jake["cards"][0])
    assert card["stream"] == "contact_card" and card["tier"] == "observed"
    assert jake["cards"][0] in _cited_with(bundle, text, "Veracruz All Natural")


def test_where_jake_is_from(world):
    bundle, text = ask(world, "where is Jake from")
    assert "El Paso" in text
    cited = _cited_with(bundle, text, "El Paso")
    kinds = {_item(bundle, c).get("kind") for c in cited}
    assert "fact" in kinds or "contact card" in kinds


def test_the_lake_in_slovenia(world):
    bundle, text = ask(world, "where is that lake I went to in Slovenia")
    assert "Lake Bled" in text
    hits = [_item(bundle, c) for c in bundle["obs_hits"]]
    assert hits and hits[0]["stream"] == "claude_chat"     # the turn naming both
    assert "Slovenia" in hits[0]["snippet"] and "Bled" in hits[0]["snippet"]


def test_what_i_asked_claude_about_bled(world):
    bundle, text = ask(world, "what did I ask Claude about Bled")
    assert bundle["intent"]["streams"] == ["claude_chat"]
    assert bundle["intent"]["direction"] == "out"
    hits = [_item(bundle, c) for c in bundle["obs_hits"]]
    assert hits and all(h["stream"] == "claude_chat" and h["direction"] == "out"
                        for h in hits)
    assert "you asked Claude" in text
    body = world["conn"].execute("SELECT body FROM observations WHERE uuid=?",
                                 (hits[0]["uuid"],)).fetchone()["body"]
    assert "itinerary" in body


def test_when_i_last_talked_to_mom(world):
    bundle, text = ask(world, "when did I last talk to Mom")
    (mom,) = bundle["entities"]
    assert mom["name"] == "Linda Rivera" and mom["via"] == "alias"
    latest = _item(bundle, mom["last_contact"]["latest"])
    nine_days_ago = (ANCHOR - timedelta(days=9)).isoformat()
    assert latest["date"] == nine_days_ago
    assert latest["kind"] == "call" and latest["direction"] == "out"
    assert text.startswith(f"Last contact with Linda Rivera: {nine_days_ago}, "
                           f"you called Linda Rivera (12 min)")
    # the text and the journal entry from that evening are listed too
    kinds = {_item(bundle, i).get("kind") for i in mom["last_contact"]["items"]}
    assert {"call", "text", "event"} <= kinds


def test_priyas_advice_about_the_deck(world):
    bundle, text = ask(world, "what advice did Priya give me about the deck")
    assert "customer story" in text
    cited = _cited_with(bundle, text, "customer story")
    assert any(c.startswith("E") for c in cited)


def test_how_much_i_spent_at_veracruz(world):
    bundle, text = ask(world, "how much did I spend at Veracruz")
    (spend,) = [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    assert spend["total"] == 150.45 and spend["count"] == 3
    assert spend["span"]["label"] == "all records"     # a named merchant: all of it
    assert text.startswith(f"You spent $150.45 across 3 transaction(s)")
    assert len(spend["transactions"]) == 3
    # scoped to a window, only what falls inside it counts
    bundle, text = ask(world, "how much did I spend at Veracruz this month")
    (spend,) = [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    in_month = [t for t in persona.TRANSACTIONS if t.offset is not None
                and "VERACRUZ" in t.merchant
                and persona.day(ANCHOR, t.offset).month == ANCHOR.month]
    assert spend["total"] == round(-sum(t.amount for t in in_month), 2)


def test_open_tasks(world):
    bundle, text = ask(world, "what are my open tasks")
    titles = {_item(bundle, i)["title"] for i in bundle["loops"]}
    assert titles == {"Get back to three gym sessions a week", "Book flights to Ljubljana",
                      "Visit Mom in San Antonio", "Return trip to Lake Bled next summer"}
    assert "Own the Q4 board pitch deck" not in text      # closed when presented
    assert "Renew passport" not in text
    assert text.startswith("Open tasks and plans:")
    assert bundle["observations"] == []


def test_average_steps_this_month(world):
    bundle, text = ask(world, "how many steps did I average this month")
    (steps,) = [m for m in bundle["metrics"] if m["metric"] == "steps"]
    days = [d for d in persona.health_series(ANCHOR) if d.date.month == ANCHOR.month]
    assert steps["days"] == len(days)
    assert steps["mean"] == round(sum(d.steps for d in days) / len(days))
    assert f"[{steps['id']}]" in text


def test_what_jake_texted_me_about_the_lake(world):
    bundle, text = ask(world, "what did Jake text me about the lake")
    (jake,) = bundle["entities"]
    hits = [_item(bundle, i) for i in jake["hits"]]
    assert hits, text
    assert all(h["direction"] == "in" and h["kind"] == "text" for h in hits)
    assert all(any(p["entity"] == "Jake Morales" and p["role"] == "sender"
                   for p in h["participants"]) for h in hits)
    bodies = " ".join(h["snippet"] for h in hits).lower()
    assert "lake" in bodies
    assert "Jake Morales texted you" in text


def test_answers_through_the_full_pipeline(world):
    result = recall.answer(world["conn"], world["cfg"], "when did I last talk to Mom",
                           use_llm=False, today=ANCHOR)
    assert result["answer_source"] == "deterministic"
    assert result["citations"]["cited"] and not result["citations"]["unsupported"]
    assert any(o["stream"].endswith("_call") for o in result["evidence"]["observations"])
    index = recall.evidence_index(result["evidence"])
    for cid in result["citations"]["cited"]:
        assert index[cid]
