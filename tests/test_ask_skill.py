"""The local model is off unless configured, and /ask (Claude Code) writes
prose answers instead: its skill file is mirrored, and `ask --check` holds
a written answer to the same per-sentence citation check a model's answer
gets — an invented id, a number the cited evidence lacks, or an answer
citing nothing fails, and the deterministic answer itself passes."""

import json

import pytest
from conftest import PROJECT_ROOT, cli_project
from typer.testing import CliRunner

from lifecrm import llm
from lifecrm.cli import app

runner = CliRunner()
QUESTION = "when did I last talk to Jake"


def test_the_local_model_is_off_unless_turned_on(cfg):
    assert llm.config(cfg)["enabled"] is False
    status = llm.status(cfg)
    assert status["available"] is False and "/ask" in status["reason"]
    cfg.ask = {"enabled": True}
    assert llm.config(cfg)["enabled"] is True


def test_the_example_config_leaves_it_off():
    import tomllib
    raw = tomllib.loads((PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8"))
    assert raw["ask"]["enabled"] is False


def test_the_skill_is_mirrored_and_names_its_commands():
    claude = PROJECT_ROOT / ".claude" / "skills" / "ask" / "SKILL.md"
    agents = PROJECT_ROOT / ".agents" / "skills" / "ask" / "SKILL.md"
    assert claude.read_bytes().replace(b"\r\n", b"\n") == \
        agents.read_bytes().replace(b"\r\n", b"\n")
    text = claude.read_text(encoding="utf-8")
    assert text.startswith("---\nname: ask\n")
    for needle in ("--json --no-llm", "--check", "outside** `data\\`", "observed"):
        assert needle in text, needle


# ---------------------------------------------------------------- ask --check

@pytest.fixture
def project(tmp_path, monkeypatch):
    """A CLI project holding the seeded world of the front-end tests: Jake's
    Instagram message and emails, Priya's LinkedIn thread, ..."""
    from test_more_fronts_web import seed_more
    from lifecrm import db as db_mod
    from lifecrm.config import load_config
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    conn = db_mod.connect(load_config(root).db_path)
    try:
        seed_more(conn)
    finally:
        conn.close()
    return root, tmp_path


def _write(tmp_path, text):
    f = tmp_path / "answer.md"
    f.write_text(text, encoding="utf-8")
    return str(f)


def _deterministic(question):
    done = runner.invoke(app, ["ask", question, "--json", "--no-llm", "--no-log"])
    assert done.exit_code == 0, done.output
    return json.loads(done.stdout)


def test_check_passes_the_deterministic_answer_itself(project):
    root, tmp = project
    res = _deterministic(QUESTION)
    assert res["citations"]["cited"], res["answer"]          # the seed answers it
    done = runner.invoke(app, ["ask", QUESTION, "--check", _write(tmp, res["answer"])])
    assert done.exit_code == 0, done.output
    assert done.stdout.startswith("ok: ")


def test_check_names_an_invented_id(project):
    root, tmp = project
    done = runner.invoke(app, ["ask", QUESTION, "--check",
                               _write(tmp, "Jake emailed you about Bled [O99].")])
    assert done.exit_code == 1
    assert "cites O99, which this question's evidence does not hold" in done.output


def test_check_names_a_date_the_cited_evidence_lacks(project):
    root, tmp = project
    cited = _deterministic(QUESTION)["citations"]["cited"][0]
    done = runner.invoke(app, ["ask", QUESTION, "--check", _write(
        tmp, f"You last talked to Jake on 2026-01-15 [{cited}].")])
    assert done.exit_code == 1 and "unsupported" in done.output


def test_check_refuses_an_answer_citing_nothing(project):
    root, tmp = project
    done = runner.invoke(app, ["ask", QUESTION, "--check",
                               _write(tmp, "You talked to Jake recently.")])
    assert done.exit_code == 1 and "cites no evidence at all" in done.output
    done = runner.invoke(app, ["ask", QUESTION, "--json", "--check",
                               _write(tmp, "You talked to Jake recently.")])
    assert json.loads(done.stdout)["ok"] is False


def test_check_needs_the_question_and_a_readable_file(project):
    root, tmp = project
    done = runner.invoke(app, ["ask", "--check", _write(tmp, "x")])
    assert done.exit_code == 1 and "--check needs the question" in done.output
    done = runner.invoke(app, ["ask", QUESTION, "--check", str(tmp / "missing.md")])
    assert done.exit_code == 1 and "cannot read" in done.output


def test_check_never_logs_the_question(project):
    root, tmp = project
    runner.invoke(app, ["ask", QUESTION, "--check", _write(tmp, "Nothing to say.")])
    from lifecrm import db as db_mod
    from lifecrm.config import load_config
    conn = db_mod.connect(load_config(root).db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recall_log").fetchone()[0] == 0
    finally:
        conn.close()
