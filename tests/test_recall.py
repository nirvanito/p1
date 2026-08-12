"""The ask layer: deterministic retrieval, honest gaps, checked citations.

The model is never exercised for real here — it is monkeypatched — because
the contract under test is that retrieval is reproducible and that a model's
claims are verified against evidence before a person sees them.
"""

import json
from datetime import date

import pytest
from conftest import cli_project

from lifecrm import recall
from lifecrm.domain import entries, health, measurements, recall as recall_domain
from lifecrm.domain.util import DomainError
from lifecrm.importer import import_text

TODAY = date(2026, 7, 29)          # a Wednesday, fixed so windows are testable


def seed(conn, cfg=None):
    data = [
        ("2026-07-25", 430, 7800, 84.9, 6),
        ("2026-07-26", 465, 11840, 84.7, 8),
        ("2026-07-27", 388, 6420, 84.6, 7),
        ("2026-07-28", 442, 10230, 84.4, 8),
    ]
    for i, (day, sleep_min, steps, kg, mood) in enumerate(data):
        health.insert(conn, {"metric": "sleep_session", "journal_date": day,
                             "started_at": f"{day}T03:00:00Z",
                             "ended_at": f"{day}T11:00:00Z", "value": sleep_min,
                             "unit": "min", "external_id": f"s{i:02d}" * 8,
                             "source_file": "hc.zip"})
        health.insert(conn, {"metric": "steps", "journal_date": day,
                             "started_at": f"{day}T12:00:00Z",
                             "value": steps, "unit": "count",
                             "external_id": f"t{i:02d}" * 8,
                             "source_file": "hc.zip"})
        health.insert(conn, {"metric": "weight", "journal_date": day,
                             "started_at": f"{day}T07:00:00Z", "value": kg,
                             "unit": "kg", "external_id": f"w{i:02d}" * 8,
                             "source_file": "hc.zip"})
        measurements.add(conn, journal_date=day, metric="mood",
                         numeric_value=mood)
    entries.create(conn, journal_date="2026-07-27", kind="event",
                   title="Gym: push day", primary_category="exercise")
    entries.create(conn, journal_date="2026-07-25", kind="event",
                   title="Dinner with Jake", primary_category="social")
    if cfg is not None:
        import_text(conn, cfg, "Slept badly and woke up at four. "
                    "Gym in the evening anyway.", journal_date="2026-07-27")
    conn.commit()


# ---------------------------------------------------------------- windows

@pytest.mark.parametrize("question,start,end,label", [
    ("what happened today", "2026-07-29", "2026-07-29", "today"),
    ("what did I do yesterday", "2026-07-28", "2026-07-28", "yesterday"),
    ("how did I sleep last night", "2026-07-29", "2026-07-29", "last night"),
    ("steps this week", "2026-07-27", "2026-07-29", "this week"),
    ("steps last week", "2026-07-22", "2026-07-28", "last week"),
    ("mood in the last 3 days", "2026-07-27", "2026-07-29", "last 3 days"),
    ("weight this month", "2026-07-01", "2026-07-29", "this month"),
    ("weight last month", "2026-06-01", "2026-06-30", "last month"),
    ("how did I sleep in July", "2026-07-01", "2026-07-31", "July 2026"),
    ("what about june 2025", "2025-06-01", "2025-06-30", "June 2025"),
    ("anything on 2026-07-04", "2026-07-04", "2026-07-04", "2026-07-04"),
    ("between 2026-07-01 and 2026-07-10", "2026-07-01", "2026-07-10",
     "2026-07-01 to 2026-07-10"),
    ("what happened in 2026-05", "2026-05-01", "2026-05-31", "2026-05"),
])
def test_parse_window(cfg, question, start, end, label):
    w = recall.parse_window(question, cfg, today=TODAY)
    assert (w["start"], w["end"], w["label"]) == (start, end, label)
    assert w["explicit"] is True


def test_window_defaults_when_no_time_named(cfg):
    w = recall.parse_window("how is my weight", cfg, today=TODAY,
                            default_days=30)
    assert w["explicit"] is False
    assert (w["start"], w["end"]) == ("2026-06-30", "2026-07-29")


def test_month_name_without_year_picks_the_most_recent(cfg):
    """In July, 'December' means last December — not one five months away."""
    w = recall.parse_window("what happened in December", cfg, today=TODAY)
    assert (w["start"], w["end"]) == ("2025-12-01", "2025-12-31")


# ---------------------------------------------------------------- intent

def test_detect_metrics():
    health_m, journal_m = recall.detect_metrics("how did I sleep last week")
    assert health_m == ["sleep_session"]
    assert "sleep_hours" in journal_m

    health_m, _ = recall.detect_metrics("did I go to the gym")
    assert "exercise_session" in health_m

    # whole-token matching: 'resting' must not be found inside other words
    health_m, _ = recall.detect_metrics("resting heart rate trend")
    assert "resting_heart_rate" in health_m
    assert recall.detect_metrics("I need some rest") == ([], [])


# ---------------------------------------------------------------- FTS safety

@pytest.mark.parametrize("question", [
    'what about "unclosed quote',
    "did I do foo OR (bar",
    "search NEAR/3 things",
    "column:value lookup",
    "wildcard * and -minus",
    "trailing operator AND",
    "unicode café touché",
    "^caret and (parens)",
    "100% of 3.5 things",
])
def test_fts_query_is_injection_proof(conn, question):
    """Any question must be safe to hand to FTS5 MATCH. Raw text is not:
    operators and quotes in a question are search syntax, and a syntax error
    would fail the whole command."""
    match = recall.fts_query(question)
    if match is None:
        return
    conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? "
                 "LIMIT 1", (match,)).fetchall()
    conn.execute("SELECT rowid FROM transcripts_fts WHERE transcripts_fts "
                 "MATCH ? LIMIT 1", (match,)).fetchall()


def test_fts_query_drops_noise_words():
    assert recall.fts_query("how was my week") is None
    assert recall.fts_query("what did I do") is None
    match = recall.fts_query("how was the burrito")
    assert match == '"burrito"'


# ---------------------------------------------------------------- evidence

def test_metric_evidence_carries_stats_and_provenance(conn, cfg):
    seed(conn)
    bundle = recall.build_evidence(conn, cfg, "how did I sleep last week",
                                   today=TODAY)
    (sleep,) = [m for m in bundle["metrics"] if m["metric"] == "sleep_session"]
    assert sleep["id"] == "M1"
    assert sleep["unit"] == "h" and sleep["days"] == 4
    assert sleep["min"] == 6.47 and sleep["min_date"] == "2026-07-27"
    assert sleep["max"] == 7.75 and sleep["max_date"] == "2026-07-26"
    assert sleep["origin"] == "sensor import"
    assert sleep["source_files"] == ["hc.zip"]
    assert [s["date"] for s in sleep["series"]] == [
        "2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28"]


def test_journal_and_sensor_metrics_are_kept_distinct(conn, cfg):
    seed(conn)
    bundle = recall.build_evidence(conn, cfg, "how was my mood and my weight",
                                   today=TODAY)
    origins = {m["metric"]: m["origin"] for m in bundle["metrics"]}
    assert origins["mood"] == "journal"
    assert origins["weight"] == "sensor import"


def test_missing_data_is_reported_not_invented(conn, cfg):
    seed(conn)
    bundle = recall.build_evidence(conn, cfg, "how was my heart rate in June",
                                   today=TODAY)
    assert bundle["metrics"] == []
    assert any("heart rate" in n for n in bundle["no_data"])
    assert "(journal) (journal)" not in " ".join(bundle["no_data"])
    answer = recall.deterministic_answer(bundle)
    assert "No data" in answer or "Nothing in the database" in answer


def test_empty_database_answers_honestly(conn, cfg):
    """Nothing recorded must produce a stated gap, never a fabricated number.
    A recognised metric names what is missing; an unrecognised question falls
    back to the generic answer."""
    known = recall.build_evidence(conn, cfg, "how did I sleep", today=TODAY)
    assert known["has_evidence"] is False and known["metrics"] == []
    assert "No data for: sleep" in recall.deterministic_answer(known)

    vague = recall.build_evidence(conn, cfg, "what happened", today=TODAY)
    assert vague["has_evidence"] is False
    assert "Nothing in the database" in recall.deterministic_answer(vague)


def test_explicit_window_filters_but_default_window_does_not(conn, cfg):
    """A question naming no time should still find older entries — silently
    hiding them behind a default window would look like 'it never happened'."""
    seed(conn)
    old = recall.build_evidence(conn, cfg, "gym", today=date(2026, 12, 25))
    assert [e["title"] for e in old["entries"]] == ["Gym: push day"]
    scoped = recall.build_evidence(conn, cfg, "gym this month",
                                   today=date(2026, 12, 25))
    assert scoped["entries"] == []


def test_entries_and_passages_are_cited(conn, cfg):
    seed(conn, cfg)
    bundle = recall.build_evidence(conn, cfg, "gym", today=TODAY)
    assert bundle["entries"][0]["id"] == "E1"
    assert bundle["entries"][0]["uuid"]
    assert bundle["passages"] and bundle["passages"][0]["id"] == "P1"
    assert bundle["passages"][0]["source_id"]


def test_entity_resolution(conn, cfg):
    from lifecrm.domain import entities as entities_mod
    seed(conn)
    ent = entities_mod.create(conn, "person", "Jake", aliases=["Jakey"])
    row = conn.execute("SELECT id FROM entries WHERE title LIKE 'Dinner%'").fetchone()
    conn.execute("INSERT INTO mentions (entry_id, entity_id, role) "
                 "VALUES (?, ?, 'participant')", (row["id"], ent["id"]))
    conn.commit()
    bundle = recall.build_evidence(conn, cfg, "when did I last see Jake",
                                   today=TODAY)
    assert bundle["entities"][0]["name"] == "Jake"
    assert bundle["entities"][0]["entries"][0]["title"] == "Dinner with Jake"


def test_deterministic_answer_cites_every_metric(conn, cfg):
    seed(conn)
    bundle = recall.build_evidence(conn, cfg, "sleep and steps last week",
                                   today=TODAY)
    answer = recall.deterministic_answer(bundle)
    checked = recall.check_citations(answer, bundle)
    assert set(checked["cited"]) >= {m["id"] for m in bundle["metrics"]}
    assert checked["unknown"] == []


# ---------------------------------------------------------------- citations

def test_check_citations_flags_invented_evidence(conn, cfg):
    seed(conn)
    bundle = recall.build_evidence(conn, cfg, "how did I sleep", today=TODAY)
    good = recall.check_citations("You averaged 7 h [M1].", bundle)
    assert good["cited"] == ["M1"] and good["unknown"] == []
    bad = recall.check_citations("You slept 9 h [M9] and ran [E7].", bundle)
    assert bad["unknown"] == ["E7", "M9"]
    silent = recall.check_citations("You slept fine.", bundle)
    assert silent["uncited"] is True


# ---------------------------------------------------------------- recall_log

def test_recall_log(conn):
    recall_domain.log_question(conn, "how did I sleep", answered=True,
                               answer_summary="7 h average")
    recall_domain.log_question(conn, "what did I spend on coffee",
                               answered=False)
    recall_domain.log_question(conn, "What did I spend on COFFEE",
                               answered=False)
    conn.commit()
    rows = recall_domain.recent(conn)
    assert len(rows) == 3 and rows[0]["question"].startswith("What did I")
    themes = recall_domain.unanswered_themes(conn)
    assert themes[0]["times"] == 2            # case-insensitive grouping
    with pytest.raises(DomainError):
        recall_domain.log_question(conn, "   ", answered=False)


def test_recall_log_truncates_long_summaries(conn):
    recall_domain.log_question(conn, "q", answered=True,
                               answer_summary="x" * 5000)
    conn.commit()
    assert len(recall_domain.recent(conn)[0]["answer_summary"]) <= 500


# ---------------------------------------------------------------- CLI

@pytest.fixture
def cli_root(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lifecrm.cli import app
    from lifecrm import db as db_mod
    from lifecrm.config import load_config

    root = cli_project(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    conn = db_mod.connect(root / "data" / "lifecrm.db")
    seed(conn, load_config(root))
    conn.close()
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": False, "reason": "no local model (test)",
        "model": "test", "models": []})
    return runner, app, root


def test_cli_ask_without_a_model(cli_root):
    runner, app, _ = cli_root
    r = runner.invoke(app, ["ask", "how did I sleep in July"])
    assert r.exit_code == 0, r.output
    assert "Sleep totalled" in r.output
    assert "[M1]" in r.output
    assert "sources:" in r.output
    assert "window: July 2026" in r.output


def test_cli_ask_json_is_the_tool_interface(cli_root):
    runner, app, _ = cli_root
    r = runner.invoke(app, ["ask", "how did I sleep in July", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["answer_source"] == "deterministic"
    assert payload["evidence"]["metrics"][0]["metric"] == "sleep_session"
    assert payload["evidence"]["window"]["label"] == "July 2026"
    assert payload["citations"]["cited"] == ["M1"]
    assert "WINDOW:" in payload["evidence_text"]


def test_cli_ask_logs_questions_and_history(cli_root):
    runner, app, _ = cli_root
    runner.invoke(app, ["ask", "how did I sleep in July"])
    runner.invoke(app, ["ask", "what did I spend on coffee in June"])
    runner.invoke(app, ["ask", "how did I sleep in July", "--no-log"])
    r = runner.invoke(app, ["ask", "--history"])
    assert r.exit_code == 0, r.output
    assert r.output.count("how did I sleep in July") == 1   # --no-log honored
    assert "?" in r.output                                  # unanswered marker


def test_cli_ask_uses_model_and_verifies_its_citations(cli_root, monkeypatch):
    runner, app, _ = cli_root
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:8b", "models": ["fake:8b"]})
    monkeypatch.setattr("lifecrm.llm.answer",
                        lambda cfg, evidence, question, model=None:
                        "You slept about 7 hours a night [M1].")
    r = runner.invoke(app, ["ask", "how did I sleep in July"])
    assert r.exit_code == 0, r.output
    assert "You slept about 7 hours a night [M1]." in r.output
    assert "answer: model:fake:8b" in r.output

    # a model citing evidence that does not exist must be called out
    monkeypatch.setattr("lifecrm.llm.answer",
                        lambda cfg, evidence, question, model=None:
                        "You slept 9 hours [M7] and ran a marathon [E4].")
    r = runner.invoke(app, ["ask", "how did I sleep in July"])
    assert r.exit_code == 0, r.output
    assert "cited evidence that does not exist" in r.output
    assert "E4" in r.output and "M7" in r.output


def test_cli_ask_falls_back_when_the_model_fails(cli_root, monkeypatch):
    runner, app, _ = cli_root
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:8b", "models": ["fake:8b"]})

    def boom(cfg, evidence, question, model=None):
        raise TimeoutError("model timed out")
    monkeypatch.setattr("lifecrm.llm.answer", boom)
    r = runner.invoke(app, ["ask", "how did I sleep in July"])
    assert r.exit_code == 0, r.output
    assert "Sleep totalled" in r.output          # deterministic answer survives
    assert "local model unavailable" in r.output


def test_cli_ask_no_llm_never_probes(cli_root, monkeypatch):
    runner, app, _ = cli_root

    def fail(cfg):
        raise AssertionError("--no-llm must not probe for a model")
    monkeypatch.setattr("lifecrm.llm.status", fail)
    r = runner.invoke(app, ["ask", "how did I sleep in July", "--no-llm"])
    assert r.exit_code == 0, r.output
    assert "Sleep totalled" in r.output


def test_cli_ask_requires_a_question(cli_root):
    runner, app, _ = cli_root
    r = runner.invoke(app, ["ask"])
    assert r.exit_code == 1
    assert "ask what?" in r.output
