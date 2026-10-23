"""The review pack reads the newer sources: email and every messaging app as
contact, logged workouts and personal bests, the nutrient report, payments
between people (each money movement counted once), LinkedIn, and per-day
counts for every stream. Ids stay citable: each one resolves to real rows."""

import json
from datetime import date

from lifecrm import review
from lifecrm.domain import entities, observations as obs
from lifecrm.domain.util import canonical_json

TODAY = date(2026, 8, 5)            # last full week: 2026-07-27 .. 2026-08-02
OWNER = ("phone", "+15125550100")
ME_EM = ("email", "alex@example.com")
JAKE_EM = ("email", "jake@example.com")
MAYA_EM = ("email", "maya@example.com")
PRIYA_EM = ("email", "priya@example.com")
LINDA_EM = ("email", "linda@example.com")
SAM_EM = ("email", "sam@example.com")
ROSA_EM = ("email", "rosa@example.com")
JAKE_IG = ("app_account", "instagram:jake")
ME_IG = ("app_account", "instagram:alex")
MAYA_TG = ("app_account", "telegram:user7")
ME_TG = ("app_account", "telegram:user1")
JAKE_VENMO = ("app_account", "venmo:Jake Morales")
ME_VENMO = ("app_account", "venmo:Alex Rivera")
PRIYA_LI = ("app_account", "linkedin:in/priya-n")
ME_LI = ("app_account", "linkedin:in/alex")
NAMES = {JAKE_EM: "Jake Morales", MAYA_EM: "Maya Chen", PRIYA_EM: "Priya Natarajan",
         LINDA_EM: "Linda Rivera", SAM_EM: "Sam Okafor", ROSA_EM: "Rosa Treviño",
         JAKE_IG: "Jake Morales", MAYA_TG: "Maya Chen", JAKE_VENMO: "Jake Morales",
         PRIYA_LI: "Priya Natarajan"}


def _rec(n, stream, day, hhmm="12:00", direction=None, who=(), **kw):
    rec = {"stream": stream, "external_id": f"m-{n:03d}", "journal_date": day,
           "started_at": f"{day}T{hhmm}:00Z", "direction": direction,
           "participants": [{"kind": h[0], "value": h[1], "role": role}
                            for h, role in who]}
    detail = kw.pop("detail", None)
    if detail is not None:
        rec["value_json"] = canonical_json(detail)
    rec.update(kw)
    return rec


def _apply(conn, connector, account, records):
    handles = sorted({(p["kind"], p["value"]) for r in records
                      for p in r["participants"]})
    scan = {"connector": connector, "account": account, "source_file": connector,
            "source_hash": (connector * 64)[:64], "records": records,
            "handles": [{"kind": k, "value": v, "display_name": NAMES.get((k, v))}
                        for k, v in handles]}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _bench(kg, reps, *more):
    ex = [{"name": "Bench Press (Barbell)", "top_set": {"weight": kg, "unit": "kg",
                                                         "reps": reps, "weight_kg": kg}}]
    for name, w, r in more:
        ex.append({"name": name, "top_set": {"weight": w, "unit": "kg", "reps": r,
                                              "weight_kg": w}})
    return {"app": "strong", "exercises": ex}


def seed(conn):
    ids = {k: entities.create(conn, "person", name)["id"] for k, name in (
        ("me", "Alex Rivera"), ("jake", "Jake Morales"), ("rosa", "Rosa Treviño"),
        ("priya", "Priya Natarajan"))}
    _apply(conn, "meta_messages", ME_IG[1], [
        _rec(1, "instagram_dm", "2026-07-28", direction="in",
             who=((JAKE_IG, "sender"), (ME_IG, "recipient")), body="Bled?"),
    ])
    _apply(conn, "telegram", ME_TG[1], [
        _rec(2, "telegram", "2026-07-29", direction="out",
             who=((ME_TG, "sender"), (MAYA_TG, "recipient")), body="gym at 9"),
    ])
    _apply(conn, "email", ME_EM[1], [
        # Maya is only copied: contact with Jake, not with her
        _rec(3, "email", "2026-07-28", direction="in", title="Bled photos",
             who=((JAKE_EM, "sender"), (ME_EM, "recipient"), (MAYA_EM, "recipient"))),
        _rec(4, "email", "2026-07-30", direction="out", title="Deck v3",
             who=((ME_EM, "sender"), (PRIYA_EM, "recipient"))),
        # nothing said which address is yours: the sender only
        _rec(5, "email", "2026-07-31", title="Dinner Sunday",
             who=((LINDA_EM, "sender"), (ME_EM, "recipient"), (SAM_EM, "recipient"))),
        # Rosa wrote in June; in July she was only copied on Jake's mail
        _rec(6, "email", "2026-06-20", direction="in", title="Recipe",
             who=((ROSA_EM, "sender"), (ME_EM, "recipient"))),
        _rec(7, "email", "2026-07-25", direction="in", title="Party",
             who=((JAKE_EM, "sender"), (ME_EM, "recipient"), (ROSA_EM, "recipient"))),
    ])
    _apply(conn, "fitness", None, [
        _rec(8, "workout", "2026-07-01", title="Push", value=50.0, unit="min",
             origin_app="strong", detail=_bench(80, 5, ("Squat (Barbell)", 100, 5))),
        _rec(9, "workout", "2026-07-22", title="Push", value=50.0, unit="min",
             origin_app="strong", detail=_bench(82.5, 5)),
        _rec(10, "workout", "2026-07-29", title="Push", value=60.0, unit="min",
             origin_app="strong", detail=_bench(85, 5, ("Squat (Barbell)", 100, 5),
                                                ("Deadlift (Barbell)", 120, 5))),
        _rec(11, "workout", "2026-07-31", title="Push", value=45.0, unit="min",
             origin_app="strong", detail=_bench(85, 6)),
    ])
    days = [f"2026-07-{d}" for d in range(27, 32)] + ["2026-08-01", "2026-08-02"]
    _apply(conn, "nutrition", None, [
        _rec(20 + i, "nutrition_day", d, title=f"Nutrition {d}", value=2200.0,
             unit="kcal", origin_app="cronometer",
             detail={"completed": True, "nutrients": {
                 "energy": [2200.0, "kcal"], "vitamin_d": [5.0, "µg"],
                 "fiber": [30.0, "g"], "sodium": [2600.0, "mg"]}})
        for i, d in enumerate(days)])
    _apply(conn, "payments", ME_VENMO[1], [
        _rec(30, "txn", "2026-07-28", direction="out", value=-150.0, unit="usd",
             title="Jake Morales: Bled lakeside place", origin_app="venmo",
             who=((ME_VENMO, "sender"), (JAKE_VENMO, "counterparty")),
             detail={"app": "venmo", "type": "Payment"}),
        _rec(31, "txn", "2026-07-30", direction="in", value=14.88, unit="usd",
             title="Jake Morales: tacos", origin_app="venmo",
             who=((ME_VENMO, "recipient"), (JAKE_VENMO, "counterparty")),
             detail={"app": "venmo", "type": "Payment"}),
        _rec(32, "txn", "2026-07-31", direction="out", value=-100.0, unit="usd",
             title="Standard Transfer to Chase Bank", origin_app="venmo",
             who=((ME_VENMO, "sender"),),
             detail={"app": "venmo", "type": "Standard Transfer", "transfer": True}),
    ])
    _apply(conn, "finance", "checking-1", [
        _rec(33, "txn", "2026-07-28", value=-150.0, unit="usd",
             title="VENMO PAYMENT 1023849"),
        _rec(34, "txn", "2026-07-31", value=100.0, unit="usd", title="VENMO CASHOUT"),
        _rec(35, "txn", "2026-07-29", value=-64.12, unit="usd",
             title="H-E-B #742 AUSTIN TX"),
        # the prior week had no Venmo statement rows: the bank line counts
        _rec(36, "txn", "2026-07-21", value=-20.0, unit="usd",
             title="VENMO PAYMENT 99"),
    ])
    _apply(conn, "linkedin", ME_LI[1], [
        _rec(40, "linkedin_connection", "2026-07-30", title="Priya Natarajan",
             body="VP Product at Acme Robotics", who=((PRIYA_LI, "participant"),),
             detail={"company": "Acme Robotics", "position": "VP Product"}),
        _rec(41, "linkedin_connection", "2026-07-21", title="Old Friend",
             body="Engineer at Initech", detail={"company": "Initech",
                                                 "position": "Engineer"}),
        _rec(42, "linkedin_message", "2026-07-30", "13:40", direction="in",
             who=((PRIYA_LI, "sender"), (ME_LI, "recipient")), body="Congrats"),
        _rec(43, "linkedin_message", "2026-07-31", "09:00", direction="out",
             who=((ME_LI, "sender"), (PRIYA_LI, "recipient")), body="Thank you"),
    ])
    _apply(conn, "spotify", None, [
        _rec(50, "music_play", "2026-07-28", title="Be Sweet", body="Japanese Breakfast",
             value=12.5, unit="min", origin_app="spotify"),
        _rec(51, "music_play", "2026-07-30", title="Bled", body="Slow Traveler",
             value=40.0, unit="min", origin_app="spotify"),
    ])
    for handle, key in ((JAKE_IG, "jake"), (JAKE_EM, "jake"), (ROSA_EM, "rosa"),
                        (JAKE_VENMO, "jake"), (PRIYA_LI, "priya"), (PRIYA_EM, "priya"),
                        (ME_EM, "me"), (ME_IG, "me"), (ME_TG, "me"), (ME_VENMO, "me"),
                        (ME_LI, "me")):
        obs.link_handle(conn, *handle, ids[key])
    conn.commit()
    return ids


def pack_for(conn, cfg, lens="all", **kw):
    return review.build_pack(conn, cfg, review.resolve_period(cfg, today=TODAY, **kw),
                             lens=lens)


def _ids(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "id" and isinstance(v, str):
                found.add(v)
            elif k in ("entries", "events") and isinstance(v, list) \
                    and all(isinstance(x, str) for x in v):
                found.update(v)
            else:
                _ids(v, found)
    elif isinstance(node, list):
        for v in node:
            _ids(v, found)


def test_every_new_id_resolves_and_the_pack_stays_deterministic(conn, cfg):
    seed(conn)
    pack = pack_for(conn, cfg)
    found: set[str] = set()
    _ids({k: v for k, v in pack.items() if k != "index"}, found)
    assert found and found <= set(pack["index"])
    for cid, item in pack["index"].items():
        if item["type"] == "observation_count":
            for u in [item.get("first_uuid"), item.get("last_uuid"),
                      *item.get("uuids", [])]:
                if u:
                    assert conn.execute("SELECT 1 FROM observations WHERE uuid=?",
                                        (u,)).fetchone(), cid
        assert review.index_line(item)
    again = pack_for(conn, cfg)
    assert json.dumps(pack, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert pack["pack_version"] == 2


def test_emails_and_every_messaging_app_count_as_contact_by_the_role_rule(conn, cfg):
    seed(conn)
    people = {p["name"]: p for p in pack_for(conn, cfg)["relationships"]["people"]}
    jake = people["Jake Morales"]
    assert (jake["messages"]["count"], jake["messages"]["streams"]) == (1, ["instagram_dm"])
    assert (jake["emails"]["count"], jake["emails"]["in"]) == (1, 1)
    assert people["Priya Natarajan"]["emails"]["out"] == 1
    # telegram to an unlinked handle is still a message, under its own name
    assert people["Maya Chen"]["messages"]["count"] == 1
    assert people["Maya Chen"]["emails"] is None           # only copied
    # an undirected email is contact with its sender, never its other recipients
    assert people["Linda Rivera"]["emails"]["count"] == 1
    assert "Sam Okafor" not in people
    assert "Alex Rivera" not in people


def test_a_cc_never_refreshes_a_quiet_relationship(conn, cfg):
    seed(conn)
    pack = pack_for(conn, cfg, lens="relationships")
    quiet = {q["name"]: q for q in pack["relationships"]["quiet"]}
    rosa = quiet["Rosa Treviño"]
    # her July Cc is not contact: the June email she sent is the last one
    assert (rosa["last_contact"], rosa["via"]) == ("2026-06-20", "emails")
    assert pack["index"][rosa["id"]]["title"] == "Recipe"


def test_workouts_count_minutes_and_personal_bests(conn, cfg):
    seed(conn)
    w = pack_for(conn, cfg, lens="health")["health"]["workouts"]
    assert (w["count"], w["minutes"], w["prior_count"], w["prior_minutes"]) == \
        (2, 105.0, 1, 50.0)
    assert [s["date"] for s in w["sessions"]] == ["2026-07-29", "2026-07-31"]
    (pb,) = w["personal_bests"]          # squat only tied, deadlift is new
    assert (pb["exercise"], pb["weight"], pb["reps"], pb["date"]) == \
        ("Bench Press (Barbell)", 85, 6, "2026-07-31")
    assert (pb["previous"]["weight"], pb["previous"]["date"]) == (82.5, "2026-07-22")
    pack = pack_for(conn, cfg, lens="health")
    assert pack["index"][pb["previous"]["id"]]["date"] == "2026-07-22"
    text = review.render_markdown(pack)
    assert "Bench Press (Barbell): 85 kg × 6, up from 82.5 kg × 5" in text


def test_nutrition_summary_cites_its_rows_and_says_it_is_not_advice(conn, cfg):
    seed(conn)
    pack = pack_for(conn, cfg, lens="health")
    n = pack["health"]["nutrition"]
    assert "not medical advice" in n["disclaimer"]
    assert n["days_counted"] == 7
    assert [x["key"] for x in n["low"]] == ["vitamin_d"]
    assert [x["key"] for x in n["borderline"]] == ["fiber"]
    assert [x["key"] for x in n["high"]] == ["sodium"]
    assert pack["index"][n["low"][0]["id"]]["count"] == 7
    assert "not medical advice" in review.render_markdown(pack)
    # a period with nothing logged has no nutrition summary at all
    assert pack_for(conn, cfg, lens="health", week="2026-W29")["health"]["nutrition"] \
        is None


def test_payments_by_person_and_each_movement_counted_once(conn, cfg):
    seed(conn)
    fin = pack_for(conn, cfg, lens="finance")["finance"]
    (jake,) = fin["payments"]
    assert (jake["name"], jake["paid"], jake["received"], jake["net"], jake["count"]) \
        == ("Jake Morales", 150.0, 14.88, -135.12, 2)
    # Venmo's own rows count; the bank's VENMO lines and the transfer do not
    assert fin["totals"] == [{"currency": "usd", "spent": 214.12, "received": 14.88,
                              "count": 3, "prior_spent": 20.0, "delta_spent": 194.12}]
    assert [m["merchant"] for m in fin["merchants"]] == ["H-E-B #742 AUSTIN TX"]
    assert fin["excluded"]["funding"]["count"] == 2
    assert fin["excluded"]["transfers"]["count"] == 1
    assert "counted once" in fin["rule"]


def test_career_lists_linkedin_connections_and_messages(conn, cfg):
    seed(conn)
    li = pack_for(conn, cfg, lens="career")["career"]["linkedin"]
    assert [(c["name"], c["position"], c["company"]) for c in li["connections"]] == [
        ("Priya Natarajan", "VP Product", "Acme Robotics")]
    assert (li["connections_total"], li["connections_prior"], li["messages_total"]) == \
        (1, 1, 2)
    assert [(m["name"], m["in"], m["out"]) for m in li["messages"]] == [
        ("Priya Natarajan", 1, 1)]


def test_the_week_counts_every_stream_per_day(conn, cfg):
    seed(conn)
    streams = {s["stream"]: s for s in pack_for(conn, cfg, lens="week")
               ["observations"]["streams"]}
    assert streams["email"]["by_day"] == {"2026-07-28": 1, "2026-07-30": 1,
                                          "2026-07-31": 1}
    assert streams["music_play"]["minutes"] == 52.5
    assert streams["music_play"]["label"] == "listening"
    assert streams["workout"]["by_day"] == {"2026-07-29": 1, "2026-07-31": 1}
    long = pack_for(conn, cfg, lens="week", days=60)["observations"]["streams"]
    assert all("by_day" not in s for s in long)
