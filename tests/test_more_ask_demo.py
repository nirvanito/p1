"""`ask` over the newer sources, from the demo generators' own exports.

The world is test_ask_demo's (the journal, texts, calls, cards, calendar,
bank statements, notes, photos, AI chats, health) plus every newer source
exactly as the demo writes it — lifecrm/demo/sources/<name>.write_sample(s)
— read by the real connector (`scan`) and applied through the real write
layer, then each new handle linked to its person the way `lifecrm demo`
does it (`handles link`). Every persona QUESTION must come back answered,
cited, and passing its own citation check; each new kind of question gets
the record that answers it.
"""

import importlib
import json
from datetime import date, timedelta

import pytest

from lifecrm import db as db_mod, nutrients, recall
from lifecrm.config import Config
from lifecrm.connectors import base
from lifecrm.demo import persona
from lifecrm.domain import entities, observations as obs
from lifecrm.domain.util import canonical_json, now_utc

from test_ask_demo import ANCHOR, _apply, _rec, build_world

# The newer sources, in the order the demo imports them (the address book
# first so handles get their names).
NEW_SOURCES = ("email", "meta_messages", "telegram", "google_activity", "fitness",
               "spotify", "reading", "nutrition", "linkedin", "payments", "rides")
NEW_QUESTIONS = persona.QUESTIONS[10:]


def make_cfg(root) -> Config:
    for d in ("data", "data/staging", "data/transcripts", "backups"):
        (root / d).mkdir(parents=True, exist_ok=True)
    me = persona.OWNER
    return Config(root=root, data_dir=root / "data", backups_dir=root / "backups",
                  inbox_dir=None, inbox_text_dir=None, archive_dir=None,
                  onedrive_backups_dir=None, timezone=persona.TIMEZONE,
                  day_rollover_hour=5, whisper={"model": "small"}, ingest={},
                  extra={"owner": {"name": me.name,
                                   "handles": [f"email:{me.email}", f"phone:{me.phone}"]}})


def import_new_sources(conn, cfg, dest, combo) -> dict:
    """Each newer source's demo export, scanned by its connector and applied."""
    done = {}
    for name in NEW_SOURCES:
        sample = importlib.import_module(f"lifecrm.demo.sources.{name}")
        connector = importlib.import_module(f"lifecrm.connectors.{name}")
        out = dest / name
        out.mkdir(parents=True, exist_ok=True)
        many = getattr(sample, "write_samples", None)
        paths = list(many(out, ANCHOR, combo)) if many \
            else [sample.write_sample(out, ANCHOR, combo)]
        for path in paths:
            scan = connector.scan(path, cfg)
            done.setdefault(name, []).append(_apply(conn, scan)["import_uuid"])
    return done


def link_new_handles(conn, ids) -> None:
    """The owner's one-time decisions, as `lifecrm demo` makes them: a
    handle whose phone, email or shown name is a person's is linked to them;
    the owner's own handles to the owner."""
    me = persona.OWNER
    owner_id = entities.create(conn, "person", me.name)["id"]
    by_phone = {p.phone: p.key for p in persona.PEOPLE if p.phone}
    by_email = {p.email.lower(): p.key for p in persona.PEOPLE if p.email}
    names = {}
    for p in persona.PEOPLE:
        for n in (p.name, p.first, *p.aliases):
            names.setdefault(n.lower(), p.key)
    for r in conn.execute("SELECT kind, value, display_name FROM entity_handles "
                          "WHERE entity_id IS NULL AND ignored = 0").fetchall():
        kind, value = r["kind"], r["value"]
        display = (r["display_name"] or "").strip().lower()
        if value.lower() in (me.phone, me.email.lower()) or display in (
                me.name.lower(), me.first.lower()) or display.startswith("you"):
            obs.link_handle(conn, kind, value, owner_id)
            continue
        key = (by_phone.get(value) if kind == "phone" else
               by_email.get(value.lower()) if kind == "email" else None) \
            or names.get(display)
        if key in ids:
            obs.link_handle(conn, kind, value, ids[key])
    conn.commit()


@pytest.fixture(scope="module", params=persona.COMBOS)
def world(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"more-{request.param}")
    cfg = make_cfg(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    info = build_world(conn, cfg, root, combo=request.param)
    info["new"] = import_new_sources(conn, cfg, root / "exports", request.param)
    link_new_handles(conn, info["ids"])
    yield {**info, "conn": conn, "cfg": cfg, "combo": request.param}
    conn.close()


def ask(world, question):
    bundle = recall.build_evidence(world["conn"], world["cfg"], question, today=ANCHOR)
    return bundle, recall.deterministic_answer(bundle)


def item(bundle, cid):
    for key in ("metrics", "entries", "passages", "observations"):
        for x in bundle.get(key, []):
            if x["id"] == cid:
                return x
    raise KeyError(cid)


def metric(bundle, name):
    found = [m for m in bundle["metrics"] if m["metric"] == name]
    assert found, (name, [m["metric"] for m in bundle["metrics"]])
    return found


def self_consistent(bundle, text):
    check = recall.check_citations(text, bundle)
    assert check["cited"] and not check["unknown"], (text, check)
    assert check["unsupported"] == [], check["unsupported"]
    return check


def cited_with(text, needle):
    return [c for s in recall._sentences(text) if needle.lower() in s.lower()
            for c in recall._cited(s)]


# ---------------------------------------------------------------- every question

def test_the_world_imported_every_newer_source(world):
    assert set(world["new"]) == set(NEW_SOURCES)
    streams = {r[0] for r in world["conn"].execute(
        "SELECT DISTINCT stream FROM observations WHERE status='active'")}
    assert {"email", "workout", "food_log", "nutrition_day", "music_play", "highlight",
            "book", "web_search", "video_watch", "ride", "food_order",
            "linkedin_connection", "txn"} <= streams


def test_every_persona_question_is_answered_cited_and_self_consistent(world):
    for q in persona.QUESTIONS:
        bundle, text = ask(world, q)
        assert bundle["has_evidence"] and bundle["answered"], (q, text)
        self_consistent(bundle, text)


def test_the_first_ten_questions_keep_their_answers(world):
    """The newer sources add evidence, never take the old answer away."""
    _, text = ask(world, "what's Jake's favorite restaurant")
    assert "Veracruz All Natural" in text
    _, text = ask(world, "where is Jake from")
    assert "El Paso" in text
    bundle, text = ask(world, "what advice did Priya give me about the deck")
    assert any(c.startswith("E") for c in cited_with(text, "customer story"))
    bundle, text = ask(world, "how much did I spend at Veracruz")
    spend = metric(bundle, "txn_spend")[0]
    assert spend["total"] == 150.45 and spend["spent_count"] == 3
    bundle, text = ask(world, "what are my open tasks")
    assert text.startswith("Open tasks and plans:") and bundle["observations"] == []


# ---------------------------------------------------------------- email

def test_what_priya_advised_over_email_is_her_email_quoted_as_observed(world):
    bundle, text = ask(world, "what advice did Priya give me over email")
    intent = bundle["intent"]
    assert intent["streams"] == ["email"] and intent["direction"] == "in"
    assert not intent["profile"]                  # not her card: what she wrote
    (priya,) = bundle["entities"]
    hits = [item(bundle, i) for i in priya["hits"]]
    assert hits and all(h["stream"] == "email" and h["direction"] == "in" for h in hits)
    assert all(any(p["entity"] == "Priya Natarajan" and p["role"] == "sender"
                   for p in h["participants"]) for h in hits)
    assert 'Priya Natarajan emailed you "Re: Q4 board deck' in text
    assert "(observed, not confirmed)" in text
    assert any(item(bundle, c)["stream"] == "email"
               for c in cited_with(text, "customer story") if c.startswith("O"))
    self_consistent(bundle, text)


def test_what_i_emailed_jake_is_mine_to_him(world):
    bundle, text = ask(world, "what did I email Jake")
    assert bundle["intent"]["direction"] == "out"
    (jake,) = bundle["entities"]
    hits = [item(bundle, i) for i in jake["hits"]]
    assert hits and all(h["direction"] == "out" and h["stream"] == "email" for h in hits)
    assert "you emailed Jake Morales" in text
    self_consistent(bundle, text)


def test_email_counts_as_contact_and_a_whether_question_says_none(world):
    bundle, text = ask(world, "when did I last email Priya")
    latest = item(bundle, bundle["entities"][0]["last_contact"]["latest"])
    assert latest["stream"] == "email" and latest["direction"] == "out"
    assert text.startswith("Last contact with Priya Natarajan:")
    _, text = ask(world, "did Priya email me this week")
    assert text.startswith("Nothing recorded shows an email with Priya Natarajan "
                           "for this week")


def test_jakes_email_is_still_his_address_on_the_card(world):
    bundle, text = ask(world, "what's Jake's email")
    assert "email" not in bundle["intent"]["streams"] and bundle["intent"]["profile"]
    assert persona.person("jake").email in text


def test_a_journal_entry_that_says_emailed_is_email_contact(world):
    assert recall.EMAIL_STREAMS in [s for s, _ in recall._CHANNEL_WORDS]


# ---------------------------------------------------------------- messaging apps

@pytest.mark.parametrize("question, stream, app", [
    ("what did Jake send me on Instagram", "instagram_dm", "Instagram"),
    ("what did Maya say on telegram", "telegram", "Telegram"),
    ("what did Priya say on LinkedIn", "linkedin_message", "LinkedIn"),
])
def test_messages_say_which_app_they_came_over(world, question, stream, app):
    bundle, text = ask(world, question)
    hits = [item(bundle, i) for e in bundle["entities"] for i in e["hits"]]
    assert hits and all(h["stream"] in recall.TEXT_STREAMS for h in hits)
    assert any(h["stream"] == stream for h in hits)
    assert f"messaged you on {app}" in text
    self_consistent(bundle, text)


# ---------------------------------------------------------------- payments

def test_how_much_i_paid_jake_is_the_payments_with_him_as_counterparty(world):
    bundle, text = ask(world, "how much did I pay Jake")
    (m,) = metric(bundle, "txn_person")
    assert (m["paid"], m["paid_count"]) == (150.0, 1)
    assert (m["received"], m["received_count"]) == (14.88, 1)
    assert m["net"] == -135.12 and m["span"]["label"] == recall.ALL_RECORDS
    rows = [item(bundle, i) for i in m["transactions"]]
    assert {r["origin_app"] for r in rows} == {"venmo"}
    assert all(any(p["role"] == "counterparty" and p["entity"] == "Jake Morales"
                   for p in r["participants"]) for r in rows)
    assert text.startswith("You paid Jake Morales $150.00 in 1 payment(s), Jake Morales "
                           "paid you $14.88 in 1 payment(s); net $135.12 from you to "
                           "Jake Morales — payments made, not a debt")
    assert not [x for x in bundle["metrics"] if x["metric"] == "txn_spend"]
    self_consistent(bundle, text)


def test_who_paid_whom_back(world):
    bundle, text = ask(world, "did Maya pay me back")
    (m,) = metric(bundle, "txn_person")
    assert (m["received"], m["paid_count"], m["net"]) == (106.2, 0, 106.2)
    assert "Maya Chen paid you $106.20" in text and "from Maya Chen to you" in text
    self_consistent(bundle, text)
    bundle, text = ask(world, "what do I owe Sam")
    (m,) = metric(bundle, "txn_person")
    assert (m["paid"], m["net"]) == (25.0, -25.0)
    assert "not a debt" in text
    self_consistent(bundle, text)


def test_moms_paypal_gas_money_on_the_iphone(world):
    if world["combo"] != "iphone":
        pytest.skip("PayPal is the iPhone combo's")
    bundle, text = ask(world, "did Mom send me money")
    (m,) = metric(bundle, "txn_person")
    assert m["received"] == 40.0 and m["paid_count"] == 0  # the return was canceled
    assert {item(bundle, i)["origin_app"] for i in m["transactions"]} == {"paypal"}
    self_consistent(bundle, text)


def test_how_much_i_spent_on_venmo_is_the_apps_own_rows(world):
    bundle, text = ask(world, "how much did I spend on Venmo this month")
    assert bundle["intent"]["apps"] == ["venmo"] and not bundle["intent"]["unused"]
    (m,) = metric(bundle, "txn_spend")
    assert {item(bundle, i)["origin_app"] for i in m["transactions"]} == {"venmo"}
    self_consistent(bundle, text)


# ---------------------------------------------------------------- workouts

def test_bench_pr_is_the_heaviest_top_set(world):
    bundle, text = ask(world, "what's my bench PR")
    (m,) = metric(bundle, "workout_best")
    assert m["exercise"] == "Bench Press (Barbell)" and m["best"] == "185 lb × 5"
    assert m["best_date"] == persona.day(ANCHOR, -12).isoformat()
    assert item(bundle, m["best_id"])["stream"] == "workout"
    assert text.startswith("Your best Bench Press (Barbell) on record: 185 lb × 5 on "
                           f"{m['best_date']}")
    assert not [x for x in bundle["metrics"] if x["table"] == "health_records"]
    self_consistent(bundle, text)


def test_best_squat_prefers_the_exercise_named_by_the_word(world):
    bundle, text = ask(world, "what's my best squat")
    (m,) = metric(bundle, "workout_best")
    assert m["exercise"].lower().startswith("squat")
    tops = [json.loads(r[0]) for r in world["conn"].execute(
        "SELECT json_extract(e.value, '$.top_set') FROM observations o, "
        "json_each(o.value_json, '$.exercises') e WHERE o.stream = 'workout' "
        "AND json_extract(e.value, '$.name') = ?", (m["exercise"],))]
    assert m["best_kg"] == max(t["weight_kg"] for t in tops)
    self_consistent(bundle, text)


def test_how_many_workouts_this_month(world):
    bundle, text = ask(world, "how many workouts this month")
    (m,) = metric(bundle, "activity_total")
    first = ANCHOR.replace(day=1).isoformat()
    n, minutes = world["conn"].execute(
        "SELECT COUNT(*), SUM(value) FROM observations WHERE stream = 'workout' "
        "AND status = 'active' AND journal_date BETWEEN ? AND ?",
        (first, ANCHOR.isoformat())).fetchone()
    assert (m["count"], m["sum"]) == (n, minutes) and n > 0
    assert text.startswith(f"Workouts: {n} workout(s)")
    self_consistent(bundle, text)


def test_when_i_last_ran_is_the_newest_run(world):
    bundle, text = ask(world, "when did I last run")
    assert bundle["intent"]["last_of"] and bundle["intent"]["topic_terms"] == ["run"]
    newest = world["conn"].execute(
        "SELECT journal_date FROM observations WHERE stream = 'workout' AND "
        "json_extract(value_json, '$.activity_type') = 'Run' ORDER BY journal_date DESC "
        "LIMIT 1").fetchone()[0]
    first = item(bundle, bundle["obs_hits"][0])
    assert first["date"] == newest and "Run" in (first["snippet"] or "")
    assert text.startswith(f"Last workout on record (observed, not confirmed): {newest}")
    self_consistent(bundle, text)


# ---------------------------------------------------------------- nutrition

def test_what_am_i_low_on_is_the_nutrient_report_cited(world):
    bundle, text = ask(world, "what am I low on")
    assert text.startswith(nutrients.DISCLAIMER)
    report = nutrients.build_report(
        world["conn"], world["cfg"],
        nutrients.resolve_window(world["cfg"], start=bundle["window"]["start"],
                                 end=ANCHOR.isoformat(), today=ANCHOR))
    shown = {m["key"]: m for m in metric(bundle, "nutrient")}
    assert set(shown) == set(report["low"] + report["borderline"] + report["high"]
                             + report["over_on_some_days"])
    assert {"vitamin_d", "magnesium"} <= set(report["low"]) and "sodium" in shown
    by_key = {x["key"]: x for x in report["nutrients"]}
    for key, m in shown.items():
        assert m["average"] == by_key[key]["average"]
        days = [item(bundle, i) for i in m["transactions"]]
        assert days and {d["stream"] for d in days} <= {"nutrition_day", "food_log"}
    over = shown["sodium"]["days_over"]
    assert over and all(o["ids"] for o in over)
    for o in over:
        assert all(f"[{i}]" in text for i in o["ids"])
    assert "Below 70% of the reference intake: Vitamin D" in text
    self_consistent(bundle, text)


def test_how_much_protein_this_week_is_that_nutrient_only(world):
    bundle, text = ask(world, "how much protein did I eat this week")
    assert bundle["intent"]["nutrition"] == {"keys": ["protein"], "flags": False}
    (m,) = metric(bundle, "nutrient")
    assert m["key"] == "protein" and m["days_counted"] >= 1
    assert round(sum(s["value"] for s in m["series"]), 1) == m["in_all"]
    assert not [x for x in bundle["metrics"] if x["metric"] in
                ("nutrition", "total_calories", "calories")]   # not calories burned
    assert text.startswith(nutrients.DISCLAIMER) and "Protein " in text
    self_consistent(bundle, text)


def test_what_i_ate_yesterday_is_the_food_log_and_the_journal(world):
    bundle, text = ask(world, "what did I eat yesterday")
    food = bundle["food"]
    yesterday = (ANCHOR - timedelta(days=1)).isoformat()
    logged = world["conn"].execute(
        "SELECT COUNT(*) FROM observations WHERE stream = 'food_log' AND status = 'active' "
        "AND journal_date = ?", (yesterday,)).fetchone()[0]
    assert len(food["items"]) == logged > 0
    assert all(item(bundle, i)["stream"] == "food_log" for i in food["items"])
    assert [item(bundle, i)["stream"] for i in food["days"]] == ["nutrition_day"]
    assert [item(bundle, i)["title"] for i in food["entries"]] == \
        ["Tacos at Veracruz with Jake"]
    assert f"Food log for yesterday (observed, not confirmed): {yesterday}" in text
    self_consistent(bundle, text)


def test_what_i_had_for_dinner_is_that_meal(world):
    bundle, text = ask(world, "what did I have for dinner yesterday")
    meals = {item(bundle, i)["detail"].get("meal") for i in bundle["food"]["items"]}
    assert meals == {"Dinner"}
    self_consistent(bundle, text)


# ---------------------------------------------------------------- listening, reading

def test_the_podcast_about_slovenia(world):
    bundle, text = ask(world, "what podcast did I listen to about Slovenia")
    assert bundle["intent"]["kinds"] == ["podcast"]
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and all(h["stream"] == "music_play" and h["detail"]["kind"] == "podcast"
                        and "Slovenia" in h["title"] for h in hits)
    assert 'you listened to "Slovenia' in text
    self_consistent(bundle, text)


def test_how_long_i_listened_to_an_artist(world):
    bundle, text = ask(world, "how long did I listen to Japanese Breakfast")
    (m,) = metric(bundle, "activity_total")
    assert m["terms"] == ["japanese", "breakfast"] and not bundle.get("food")
    total = world["conn"].execute(
        "SELECT ROUND(SUM(value), 2) FROM observations WHERE stream = 'music_play' "
        "AND body = 'Japanese Breakfast' AND status = 'active'").fetchone()[0]
    assert m["sum"] == total and m["span"]["label"] == recall.ALL_RECORDS
    self_consistent(bundle, text)


def test_the_highlights_of_a_book_are_listed_in_reading_order(world):
    bundle, text = ask(world, "what did I highlight in The Overstory")
    assert bundle["highlights"]
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    n = world["conn"].execute(
        "SELECT COUNT(*) FROM observations WHERE stream = 'highlight' AND status = 'active' "
        "AND title LIKE 'The Overstory%'").fetchone()[0]
    assert len(hits) == n > 3
    assert all(h["stream"] == "highlight" and h["title"].startswith("The Overstory")
               for h in hits)
    assert [h["started_at"] for h in hits] == sorted(h["started_at"] for h in hits)
    assert all(f"[{h['id']}]" in text for h in hits)
    assert text.startswith('Highlights and notes in "The Overstory')
    self_consistent(bundle, text)


@pytest.mark.parametrize("question, shelf", [
    ("what books did I read this year", "read"),
    ("what's on my reading list", "to-read"),
])
def test_books_by_shelf(world, question, shelf):
    bundle, text = ask(world, question)
    assert bundle["intent"]["shelf"] == shelf
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and all(h["stream"] == "book" and h["detail"]["exclusive_shelf"] == shelf
                        for h in hits)
    if "this year" in question:
        assert all(h["date"] >= f"{ANCHOR.year}-01-01" for h in hits)
    self_consistent(bundle, text)


def test_what_have_i_read_is_not_clipped_to_thirty_days(world):
    bundle, _ = ask(world, "what have I read")
    dates = [item(bundle, i)["date"] for i in bundle["obs_hits"]]
    assert dates and min(dates) < (ANCHOR - timedelta(days=30)).isoformat()


# ---------------------------------------------------------------- searches, videos, rides

def test_what_i_searched_for_about_bled(world):
    bundle, text = ask(world, "what did I search for about Bled")
    assert bundle["intent"]["streams"] == ["web_search"]
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert len(hits) >= 3 and all(h["stream"] == "web_search"
                                  and "bled" in h["title"].lower() for h in hits)
    assert all(f"[{h['id']}]" in text for h in hits)
    assert 'you searched "' in text
    self_consistent(bundle, text)


def test_what_i_watched_on_youtube(world):
    bundle, text = ask(world, "what did I watch on YouTube")
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and all(h["stream"] == "video_watch" for h in hits)
    assert 'you watched "' in text
    self_consistent(bundle, text)


def test_when_i_last_took_an_uber(world):
    bundle, text = ask(world, "when did I last take an Uber")
    assert bundle["intent"]["streams"] == ["ride"] and bundle["intent"]["last_of"]
    assert bundle["intent"]["topic_terms"] == []           # 'take' is how, not what
    newest = world["conn"].execute(
        "SELECT journal_date, title FROM observations WHERE stream = 'ride' "
        "ORDER BY journal_date DESC LIMIT 1").fetchone()
    first = item(bundle, bundle["obs_hits"][0])
    assert (first["date"], first["title"]) == tuple(newest)
    assert text.startswith(f"Last ride on record (observed, not confirmed): {newest[0]}, "
                           f'ride "{newest[1]}"')
    self_consistent(bundle, text)


def test_an_uber_to_the_airport(world):
    bundle, text = ask(world, "Uber to the airport")
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and all(h["stream"] == "ride" and "Airport" in h["title"] for h in hits)
    self_consistent(bundle, text)


def test_uber_fares_are_their_own_line_never_added_to_spending(world):
    bundle, text = ask(world, "how much did I spend on Uber rides")
    fares = metric(bundle, "activity_total")
    assert fares[0]["stream"] == "ride"
    assert "apart from the spending total" in fares[0]["label"]
    assert not [m for m in bundle["metrics"] if m["metric"] == "txn_spend"
                and any(item(bundle, i)["stream"] == "ride" for i in m["transactions"])]
    self_consistent(bundle, text)


def test_an_uber_eats_order_is_a_food_order(world):
    bundle, text = ask(world, "what did I order on Uber Eats")
    assert bundle["intent"]["streams"] == ["food_order"]
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and hits[0]["title"] == "Chipotle"
    self_consistent(bundle, text)


# ---------------------------------------------------------------- LinkedIn

def test_where_priya_works_is_her_linkedin_row_beside_her_card(world):
    bundle, text = ask(world, "where does Priya work")
    (priya,) = bundle["entities"]
    (row,) = [item(bundle, i) for i in priya["work"]]
    assert row["stream"] == "linkedin_connection"
    assert row["snippet"] == "VP Product at Acme Robotics"
    assert text.startswith('LinkedIn lists Priya Natarajan as "VP Product at Acme Robotics"')
    assert priya["cards"] and all(c in text for c in priya["cards"])
    self_consistent(bundle, text)


def test_who_i_know_at_acme(world):
    bundle, text = ask(world, "who do I know at Acme")
    assert bundle["intent"]["know_at"] and bundle["intent"]["topic_terms"] == ["acme"]
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert any(h["stream"] == "linkedin_connection" and h["title"] == "Priya Natarajan"
               for h in hits)
    assert text.startswith('People on record at "Acme"')
    self_consistent(bundle, text)


# ---------------------------------------------------------------- double counting

def test_the_bank_copy_of_a_venmo_payment_and_a_transfer_are_not_spending(world):
    """A bank row titled VENMO on a day the Venmo statement covers is the same
    money as the statement's own row — left out, and counted in the answer.
    One outside that range is all there is and counts; a transfer between
    your own accounts never does."""
    conn, cfg = world["conn"], world["cfg"]
    inside = persona.day(ANCHOR, -2)
    outside = persona.day(ANCHOR, -60)

    def bank(day, title, value, n):
        started = f"{day.isoformat()}T17:00:00Z"
        return _rec(cfg, "txn", f"bank-venmo-{world['combo']}-{n}", started,
                    direction="out", title=title, value=value, unit="usd",
                    origin_app="chase",
                    value_json=canonical_json({"account": "chase:4417"}))

    scan = base.empty_scan("finance", account="chase:9999",
                           source_file="bank-venmo-test.csv",
                           records=[bank(inside, "VENMO PAYMENT 1023849", -150.0, 1),
                                    bank(outside, "VENMO PAYMENT 1000001", -20.0, 2)],
                           handles=[])
    _apply(conn, scan)
    transfer = _rec(cfg, "txn", f"venmo:transfer-{world['combo']}",
                    f"{inside.isoformat()}T18:00:00Z", direction="out",
                    title="Standard Transfer to Chase Bank *8812", value=-300.0, unit="usd",
                    origin_app="venmo",
                    value_json=canonical_json({"app": "venmo", "transfer": True}))
    _apply(conn, base.empty_scan("payments", account="venmo:Alex-Rivera-7",
                                 source_file="venmo-transfer-test.csv",
                                 records=[transfer], handles=[]))
    conn.commit()
    try:
        bundle, text = ask(world, "how much did I spend in the last 90 days")
        (m,) = metric(bundle, "txn_spend")
        titles = {item(bundle, i)["title"] for i in m["transactions"]}
        assert m["bank_copies_dropped"] == 1
        assert "VENMO PAYMENT 1023849" not in titles
        assert "Standard Transfer to Chase Bank *8812" not in titles
        rows = conn.execute(
            "SELECT o.title, o.value FROM observations o JOIN imports i ON i.id = o.import_id "
            "WHERE o.stream='txn' AND o.status='active' AND o.value < 0 "
            "AND o.journal_date BETWEEN ? AND ?",
            (bundle["window"]["start"], bundle["window"]["end"])).fetchall()
        expected = -sum(v for t, v in rows if t not in (
            "VENMO PAYMENT 1023849", "Standard Transfer to Chase Bank *8812"))
        assert m["total"] == round(expected, 2)
        assert any(t == "VENMO PAYMENT 1000001" for t, _ in rows) or \
            outside.isoformat() < bundle["window"]["start"]
        assert "1 bank row(s) titled VENMO or PAYPAL left out" in text
        self_consistent(bundle, text)
    finally:
        for uuid in conn.execute("SELECT uuid FROM imports WHERE source_file IN "
                                 "('bank-venmo-test.csv', 'venmo-transfer-test.csv')"):
            obs.retract_import(conn, uuid[0], actor="test")
        conn.commit()
