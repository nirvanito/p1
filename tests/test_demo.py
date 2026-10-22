"""The demo builder: the persona's journal through the real CLI, into a
throwaway root, never the project's own data.

The builder normally runs each command as a subprocess; here it gets an
in-process runner (same typer app, same arguments) so the whole journal path
runs in seconds.
"""

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.config import PROJECT_ROOT
from lifecrm.demo import journal, persona
from lifecrm.demo.build import DemoBuilder, DemoError, check_target

ANCHOR = date(2026, 10, 21)
runner = CliRunner()


def _in_process(monkeypatch):
    def run(args):
        r = runner.invoke(app, args)
        if r.exception and not isinstance(r.exception, SystemExit):
            raise r.exception
        return r.exit_code, r.stdout, r.stderr
    return run


# ---------------------------------------------------------------- file names

def test_iphone_notes_carry_the_journal_date_and_offset():
    jd = next(j for j in persona.JOURNAL if j.offset == -37)
    name = journal.note_filename(ANCHOR, -37, jd.notes[0], "iphone")
    assert name == "2026-09-14 12.41.08 -0500.txt"


def test_android_notes_carry_the_calendar_date_and_no_offset():
    jd = next(j for j in persona.JOURNAL if j.offset == -37)
    assert journal.note_filename(ANCHOR, -37, jd.notes[1], "android") == \
        "2026-09-14 22.05.51.txt"


def test_pending_notes_are_only_todays():
    pending = journal.note_files(ANCHOR, "android", pending=True)
    assert [n["journal_date"] for n in pending] == ["2026-10-21"]


# ---------------------------------------------------------------- drafts

def test_every_draft_cites_the_exact_sentence():
    known, keyed = {}, {}
    for jday in persona.JOURNAL:
        if jday.pending:
            continue
        ids = list(range(1, len(jday.notes) + 1))
        draft, keys = journal.build_draft(jday, ANCHOR, ids, known, keyed)
        texts = {i: n.text for i, n in zip(ids, jday.notes)}
        sentences = {s for n in jday.notes for s, spec in n.sentences if spec}
        for e in draft["entries"]:
            p = e["provenance"]
            assert texts[p["source_id"]][p["char_start"]:p["char_end"]] in sentences
        assert draft["contract_version"] == 2 and draft["sources"]


# ---------------------------------------------------------------- safety

def test_refuses_the_project_root():
    with pytest.raises(DemoError, match="refusing"):
        check_target(PROJECT_ROOT)


def test_refuses_a_folder_that_is_not_a_demo(tmp_path):
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(DemoError, match="not a demo directory"):
        check_target(tmp_path)


# ---------------------------------------------------------------- end to end

@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_demo_journal_lands_through_the_real_pipeline(tmp_path, monkeypatch, combo):
    monkeypatch.delenv("LIFECRM_ROOT", raising=False)
    root = tmp_path / "demo"
    b = DemoBuilder(root, combo, ANCHOR, echo=lambda *_: None, ask=False,
                    runner=_in_process(monkeypatch))
    summary = b.run()
    assert summary["journal"]["days"] == len([j for j in persona.JOURNAL if not j.pending])
    assert summary["doctor"]["failures"] == 0, summary["doctor"]
    status = summary["status"]
    assert len(status["pending_text"]) == 1                 # today's note waits for /log
    titles = {r["title"] for r in status["open_loops"]}
    assert "Renew passport" not in titles                   # closed across days
    assert "Book flights to Ljubljana" in titles
    assert (root / ".lifecrm-demo").exists()
    assert not (PROJECT_ROOT / "data" / "staging" / "draft-2026-09-11.json").exists()


def test_rebuilding_needs_force(tmp_path, monkeypatch):
    monkeypatch.delenv("LIFECRM_ROOT", raising=False)
    root = tmp_path / "demo"
    run = _in_process(monkeypatch)
    DemoBuilder(root, "android", ANCHOR, echo=lambda *_: None, ask=False, runner=run).run()
    with pytest.raises(DemoError, match="--force"):
        DemoBuilder(root, "android", ANCHOR, echo=lambda *_: None, ask=False,
                    runner=run).run()
