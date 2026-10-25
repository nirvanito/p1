"""The pattern finder in the fronts: off everywhere by default, on with
`[assistant] patterns = "on"` (or `nudges --patterns`), its reported pairs
cited as P# in the same index a brief is checked against."""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import nudges as mod
from lifecrm import patterns, review
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.domain import measurements
from lifecrm.domain.util import DomainError

runner = CliRunner()
TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)


def plant(conn, end=TODAY, n=90, rho=0.75):
    """mood and energy move together over `n` days ending `end`."""
    rng = random.Random(patterns.seed_for("fronts"))
    for k in range(n):
        d = (end - timedelta(days=n - 1 - k)).isoformat()
        x = rng.gauss(0, 1)
        y = rho * x + math.sqrt(1 - rho * rho) * rng.gauss(0, 1)
        measurements.add(conn, journal_date=d, metric="mood",
                         numeric_value=round(min(10.0, max(1.0, 5.5 + 1.5 * x)), 2))
        measurements.add(conn, journal_date=d, metric="energy",
                         numeric_value=round(min(10.0, max(1.0, 5.5 + 1.5 * y)), 2))
    conn.commit()


def test_settings_know_the_pattern_switch():
    from lifecrm.config import Config
    cfg = Config(root=None, data_dir=None, backups_dir=None, inbox_dir=None,
                 inbox_text_dir=None, archive_dir=None, onedrive_backups_dir=None,
                 timezone="America/Chicago", day_rollover_hour=5, whisper={}, ingest={})
    assert mod.settings(cfg)["patterns"] == "off"
    cfg.extra = {"assistant": {"patterns": "on", "pattern_series": ["texts"],
                               "pattern_days": 120}}
    s = mod.settings(cfg)
    assert (s["patterns"], s["pattern_series"], s["pattern_days"]) == ("on", ["texts"], 120)
    cfg.extra = {"assistant": {"patterns": True}}
    assert mod.settings(cfg)["patterns"] == "on"
    for bad in ({"patterns": "maybe"}, {"pattern_series": "texts"}, {"pattern_days": 7}):
        cfg.extra = {"assistant": bad}
        with pytest.raises(DomainError, match=r"\[assistant\]"):
            mod.settings(cfg)


def test_the_nudges_pack_carries_patterns_only_when_asked(conn, cfg):
    plant(conn)
    off = mod.build(conn, cfg, today=TODAY, now=NOW)
    assert off["patterns"]["enabled"] is False and off["patterns"]["items"] == []
    assert not any(k.startswith("P") for k in off["index"])
    assert "off" in off["patterns"]["notes"][0]

    on = mod.build(conn, cfg, today=TODAY, now=NOW, patterns=True)
    block = on["patterns"]
    assert block["enabled"] and block["status"] == "experimental"
    assert block["window"]["days"] == 90 and block["family_size"] == 3    # mood/energy, 3 lags
    [item] = block["items"]
    assert item["id"] == "P1" and (item["x"], item["y"], item["lag"]) == ("energy", "mood", 0)
    assert on["index"]["P1"]["type"] == "pattern" and "co-occurrence, not cause" in item["wording"]
    line = mod.lines(on)["P1"]
    assert line.startswith("pattern energy -> mood lag 0") and str(item["n"]) in line
    r = mod.check(f"Energy went with mood the same day over {item['n']} paired days [P1].", on)
    assert not r["unknown"] and not r["unsupported"]
    assert mod.check("Energy went with mood over 12 paired days [P1].", on)["unsupported"]
    assert on["pack_id"] != off["pack_id"]
    text = "\n".join(mod.render_lines(on))
    assert "Patterns (EXPERIMENTAL" in text and "[P1]" in text
    assert "Patterns (EXPERIMENTAL" not in "\n".join(mod.render_lines(off))

    cfg.extra = {"assistant": {"patterns": "on"}}
    assert mod.build(conn, cfg, today=TODAY, now=NOW)["patterns"]["enabled"] is True
    assert mod.build(conn, cfg, today=TODAY, now=NOW, patterns=False)["patterns"]["enabled"] is False


def test_the_review_pack_has_a_patterns_section_off_and_on(conn, cfg):
    plant(conn, end=date(2026, 10, 18))
    period = review.resolve_period(cfg, week="2026-W42", today=TODAY)
    off = review.build_pack(conn, cfg, period, lens="week")
    assert off["patterns"]["enabled"] is False and "off" in off["patterns"]["note"]
    assert all("patterns" in s for s in review.LENS_SECTIONS.values())
    md = review.render_markdown(off)
    assert "## Patterns (co-occurrence, not cause; EXPERIMENTAL)" in md and "- off" in md

    cfg.extra = {"assistant": {"patterns": "on"}}
    on = review.build_pack(conn, cfg, period, lens="health")
    block = on["patterns"]
    assert block["enabled"] and block["window"]["end"] == "2026-10-18"
    assert block["window"]["days"] == 90 and block["items"]
    pid = block["items"][0]["id"]
    assert pid.startswith("P") and review.index_line(on["index"][pid]).startswith("pattern ")
    assert review.check_citations(f"Energy and mood moved together [{pid}].", on)["cited"] == [pid]
    assert f"- [{pid}] " in review.render_markdown(on)
    # a lens keeps its own series: finance has neither mood nor energy
    finance = review.build_pack(conn, cfg, period, lens="finance")
    assert finance["patterns"]["enabled"] and finance["patterns"]["items"] == []


def test_cli_flag_turns_patterns_on_for_one_run(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    plant(conn)
    conn.close()
    r = runner.invoke(app, ["nudges", "--today", TODAY.isoformat(), "--json"])
    assert json.loads(r.output)["patterns"]["enabled"] is False
    r = runner.invoke(app, ["nudges", "--today", TODAY.isoformat(), "--json", "--patterns"])
    pack = json.loads(r.output)
    assert pack["patterns"]["enabled"] and pack["patterns"]["items"][0]["id"] == "P1"
    r = runner.invoke(app, ["nudges", "--today", TODAY.isoformat(), "--patterns"])
    assert r.exit_code == 0 and "Patterns (EXPERIMENTAL" in r.output and "[P1]" in r.output
