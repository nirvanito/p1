"""`lifecrm serve` logs a question's `answered` exactly as `lifecrm ask` does.

recall_log is the build-this-next list, so `answered` follows retrieval's own
verdict (domain/recall.answered): a question that asked for numbers and got
only loose text matches was not answered. The web server used to log
has_evidence instead, so the same question counted as answered in the page
and as unanswered ('?') in `ask --history`.
"""

from __future__ import annotations

import threading
from urllib.parse import quote_plus

from test_web import Client, seed_root
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm.cli import app
from lifecrm.domain import recall as recall_domain

# Asks for numbers (money); the seeded root holds text about tacos with Jake
# but no transactions, so evidence comes back and the question is unanswered.
NUMBERS_FROM_TEXT_ONLY = "how much money for tacos with Jake"
ANSWERED = "how did I sleep in July"


def _logged(cfg) -> list[tuple[str, int]]:
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        return [(r["question"], r["answered"]) for r in conn.execute(
            "SELECT question, answered FROM recall_log ORDER BY id")]
    finally:
        conn.close()


def test_the_shared_rule_prefers_retrievals_verdict_over_any_evidence():
    assert recall_domain.answered({"has_evidence": True, "answered": False}) is False
    assert recall_domain.answered({"has_evidence": True, "answered": True}) is True
    assert recall_domain.answered({"has_evidence": True}) is True
    assert recall_domain.answered({"has_evidence": False}) is False


def test_the_page_and_ask_log_the_same_answered(tmp_path, monkeypatch):
    from lifecrm.web import server as web_server
    cfg = seed_root(tmp_path)
    server = web_server.create_server(cfg, "127.0.0.1", 0, log_questions=True,
                                      use_llm=False, refresh_seconds=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Client(server)
        for q in (NUMBERS_FROM_TEXT_ONLY, ANSWERED):
            r = client.get("/api/ask?q=" + quote_plus(q))
            assert r["logged"] is True
            assert r["evidence"]["has_evidence"] is True, q
    finally:
        server.shutdown()
        server.server_close()

    monkeypatch.setenv("LIFECRM_ROOT", str(cfg.root))
    runner = CliRunner()
    for q in (NUMBERS_FROM_TEXT_ONLY, ANSWERED):
        r = runner.invoke(app, ["ask", q, "--no-llm"])
        assert r.exit_code == 0, r.output

    rows = _logged(cfg)
    expected = [(NUMBERS_FROM_TEXT_ONLY, 0), (ANSWERED, 1)]
    assert rows[:2] == expected, "web"
    assert rows[2:] == expected, "ask"
