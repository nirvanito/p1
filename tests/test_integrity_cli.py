"""The integrity fixes at the surface the user drives: the multi-source
`sources:[{source_id}]` draft shape through the real CLI (brief 2.1 — until
now only the domain layer had seen it), inline offsets checked against the
stored transcript, a --dry-run that refuses what ingest refuses,
`admin discard-run`, and a `transcribe --date` that dates the recording
before anything is written."""

import json
import os
import time

from conftest import golden_draft, sample_text
from typer.testing import CliRunner

from lifecrm import transcribe as tr
from lifecrm.cli import app
from test_cli import _setup, _sql_json
from test_transcribe import FAKE_RESULT, fake_whisper  # noqa: F401  (fixture)

runner = CliRunner()


def _import(root, name, text, date="2026-07-27"):
    p = root / name
    p.write_text(text, encoding="utf-8")
    r = runner.invoke(app, ["import", str(p), "--date", date, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["source_id"]


def _write(root, draft, name="draft.json"):
    p = root / "data" / "staging" / name
    p.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return p


def _ingest(path, *extra):
    return runner.invoke(app, ["ingest", str(path), *extra])


def _diff_for(root, date="2026-07-27"):
    diffs = sorted((root / "data" / "staging").glob(f"diff-{date}-*.json"),
                   key=lambda p: p.stat().st_mtime)
    assert diffs, "ingest produced no diff file"
    return diffs[-1]


MORNING = "Morning standup ran long. Skipped breakfast."
EVENING = "Evening recap. Lunch was a burrito bowl with Dana."


def _entry(title, cat, prov, kind="event"):
    return {"kind": kind, "categories": {"primary": cat}, "title": title,
            "provenance": prov}


# ---------------------------------------------------------------- 2.1 multi-source

def test_a_two_source_day_ingests_and_applies_through_the_cli(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    a = _import(root, "a.txt", MORNING)
    b = _import(root, "b.txt", EVENING)
    draft = {"contract_version": 2, "journal_date": "2026-07-27",
             "sources": [{"source_id": a}, {"source_id": b}],
             "entries": [
                 _entry("Standup ran long", "work",
                        {"source_id": a, "char_start": 0, "char_end": 25}),
                 _entry("Lunch: burrito bowl with Dana", "food",
                        {"source_id": b, "char_start": 15, "char_end": 50})]}
    r = _ingest(_write(root, draft))
    assert r.exit_code == 0, r.output
    assert "(2 source(s))" in r.output
    r = runner.invoke(app, ["apply", str(_diff_for(root))])
    assert r.exit_code == 0, r.output

    prov = {row["title"]: row["source_id"] for row in _sql_json(
        "SELECT e.title, es.source_id FROM entry_sources es "
        "JOIN entries e ON e.id=es.entry_id")}
    assert prov == {"Standup ran long": a, "Lunch: burrito bowl with Dana": b}
    assert _sql_json("SELECT COUNT(*) c FROM extraction_run_sources")[0]["c"] == 2

    uuid = _sql_json("SELECT uuid FROM entries WHERE title LIKE 'Lunch%'")[0]["uuid"]
    snap = json.loads(runner.invoke(app, ["entry", "show", uuid, "--json"]).output)
    assert snap["provenance"][0]["transcript_excerpt"] == EVENING[15:50]
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_a_single_source_id_draft_needs_no_per_entry_source_id(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    a = _import(root, "a.txt", MORNING)
    draft = {"contract_version": 2, "journal_date": "2026-07-27",
             "sources": [{"source_id": a}],
             "entries": [_entry("Standup ran long", "work",
                                {"char_start": 0, "char_end": 25})]}
    assert _ingest(_write(root, draft)).exit_code == 0
    assert runner.invoke(app, ["apply", str(_diff_for(root))]).exit_code == 0
    assert _sql_json("SELECT source_id FROM entry_sources")[0]["source_id"] == a


def test_a_multi_source_draft_is_refused_for_bad_provenance(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    a = _import(root, "a.txt", MORNING)
    b = _import(root, "b.txt", EVENING)
    c = _import(root, "c.txt", "Some other note entirely.")
    draft = {"contract_version": 2, "journal_date": "2026-07-27",
             "sources": [{"source_id": a}, {"source_id": b}],
             "entries": [
                 _entry("Standup ran long", "work",
                        {"source_id": c, "char_start": 0, "char_end": 4}),
                 _entry("Lunch: burrito bowl with Dana", "food",
                        {"source_id": b, "char_start": 15, "char_end": 500}),
                 _entry("No provenance at all", "other", None)]}
    draft["entries"][2].pop("provenance")
    r = _ingest(_write(root, draft))
    assert r.exit_code == 1
    assert f"entries[0].provenance.source_id: {c} is not one of this draft's " \
           f"sources ({a}, {b})" in r.output
    assert f"char_end 500 exceeds transcript length {len(EVENING)}" in r.output
    assert "entries[2].provenance: required in multi-source drafts" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM extraction_runs")[0]["c"] == 0


def test_an_unknown_source_id_is_refused_by_name(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft = {"contract_version": 2, "journal_date": "2026-07-27",
             "sources": [{"source_id": 99}], "entries": []}
    r = _ingest(_write(root, draft))
    assert r.exit_code == 1
    assert "sources[0].source_id: source id=99 does not exist" in r.output


# ---------------------------------------------------------------- 1.6 inline offsets

def _crlf_draft():
    raw = "\r\n  " + sample_text().replace("\n", "\r\n") + "\r\n"
    draft = golden_draft()
    draft["sources"][0]["text"] = raw
    return draft, raw


def test_inline_offsets_are_checked_against_the_stored_normalized_text(
        tmp_path, monkeypatch):
    """The stored transcript is normalized (CRLF -> LF, outer whitespace
    stripped), so it is shorter than the raw draft string; an offset valid
    against the raw string used to pass and then excerpt the wrong passage."""
    root = _setup(tmp_path, monkeypatch)
    draft, raw = _crlf_draft()
    draft["entries"][0]["provenance"] = {"char_start": 0, "char_end": len(raw)}
    r = _ingest(_write(root, draft))
    assert r.exit_code == 1
    assert f"exceeds transcript length {len(sample_text())}" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM sources")[0]["c"] == 0


def test_offsets_valid_against_the_normalized_text_excerpt_exactly(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft, _ = _crlf_draft()
    end = len(sample_text())
    draft["entries"][0]["provenance"] = {"char_start": end - 40, "char_end": end}
    assert _ingest(_write(root, draft)).exit_code == 0
    assert runner.invoke(app, ["apply", str(_diff_for(root))]).exit_code == 0
    uuid = _sql_json("SELECT uuid FROM entries WHERE title LIKE 'Breakfast%'")[0]["uuid"]
    snap = json.loads(runner.invoke(app, ["entry", "show", uuid, "--json"]).output)
    assert snap["provenance"][0]["transcript_excerpt"] == sample_text()[-40:]


# ---------------------------------------------------------------- 3.1 dry run

def test_dry_run_refuses_a_bad_extraction_block_like_ingest_does(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft = golden_draft()
    draft["extraction"] = {"provider": "claude-code", "interface": "foo"}
    r = _ingest(_write(root, draft), "--dry-run")
    assert r.exit_code == 1
    assert "extraction.interface: 'foo' must be claude_skill or manual" in r.output


def test_a_non_object_extraction_block_is_an_error_line_not_a_traceback(
        tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    draft = golden_draft()
    draft["extraction"] = "x"
    r = _ingest(_write(root, draft))
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)
    assert "extraction: must be an object" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM sources")[0]["c"] == 0


def test_a_draft_that_is_not_an_object_is_an_error_line(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    p = root / "data" / "staging" / "list.json"
    p.write_text("[1, 2]", encoding="utf-8")
    r = _ingest(p)
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit)
    assert "draft must be a JSON object" in r.output


# ---------------------------------------------------------------- 3.1 discard-run

def test_discard_run_ends_a_draft_renames_its_diff_and_apply_refuses_it(
        tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    assert _ingest(_write(root, golden_draft())).exit_code == 0
    diff = _diff_for(root)
    prefix = diff.stem.rsplit("-", 1)[1]

    r = runner.invoke(app, ["admin", "discard-run", prefix, "--reason", "wrong day"])
    assert r.exit_code == 0, r.output
    assert "discarded extraction run" in r.output and "2026-07-27" in r.output
    discarded = diff.with_name(diff.name + ".discarded")
    assert not diff.exists() and discarded.exists()
    assert f"{diff.name} -> {discarded.name}" in r.output

    r = runner.invoke(app, ["apply", str(discarded)])
    assert r.exit_code == 1
    assert "was discarded — re-run ingest" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM entries")[0]["c"] == 0
    r = runner.invoke(app, ["admin", "discard-run", prefix])
    assert r.exit_code == 1 and "already discarded" in r.output

    # no longer an open run; its source waits for a fresh extraction, which applies
    status = json.loads(runner.invoke(app, ["status", "--json"]).output)
    assert status["open_extraction_runs"] == []
    (waiting,) = status["unprocessed_sources"]
    redo = golden_draft()
    redo["sources"] = [{"source_id": waiting["source_id"]}]
    assert _ingest(_write(root, redo, "redo.json")).exit_code == 0
    assert runner.invoke(app, ["apply", str(_diff_for(root))]).exit_code == 0
    assert _sql_json("SELECT COUNT(*) c FROM entries")[0]["c"] == 6
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_discard_run_refuses_an_applied_run(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    assert _ingest(_write(root, golden_draft())).exit_code == 0
    diff = _diff_for(root)
    assert runner.invoke(app, ["apply", str(diff)]).exit_code == 0
    r = runner.invoke(app, ["admin", "discard-run", diff.stem.rsplit("-", 1)[1]])
    assert r.exit_code == 1
    assert "was applied — discarding cannot undo that" in r.output


# ---------------------------------------------------------------- 3.1 transcribe --date

def _aged_audio(root, name="2026-07-20 21.30.00.m4a", data=b"fake-audio"):
    p = root / "onedrive" / "inbox" / name
    p.write_bytes(data)
    t = time.time() - 3600
    os.utime(p, (t, t))
    return p


def test_transcribe_date_requires_file(tmp_path, monkeypatch, fake_whisper):  # noqa: F811
    root = _setup(tmp_path, monkeypatch)
    _aged_audio(root)
    r = runner.invoke(app, ["transcribe", "--date", "2026-07-27"])
    assert r.exit_code == 1
    assert "--date sets ONE recording's journal date" in r.output
    assert _sql_json("SELECT COUNT(*) c FROM sources")[0]["c"] == 0


def test_transcribe_date_is_validated_before_whisper_loads(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    audio = _aged_audio(root)

    def must_not_load(cfg, model_name=None):
        raise AssertionError("whisper loaded before the date was checked")
    monkeypatch.setattr(tr, "load_model", must_not_load)
    r = runner.invoke(app, ["transcribe", "--file", str(audio), "--date", "2026-13-01"])
    assert r.exit_code == 1
    assert "--date '2026-13-01' is not a valid YYYY-MM-DD date" in r.output


def test_transcribe_date_dates_the_source_its_audit_row_and_its_copy(
        tmp_path, monkeypatch, fake_whisper):  # noqa: F811
    root = _setup(tmp_path, monkeypatch)
    audio = _aged_audio(root)              # the file name says 2026-07-20
    r = runner.invoke(app, ["transcribe", "--file", str(audio),
                            "--date", "2026-07-27", "--json"])
    assert r.exit_code == 0, r.output
    (res,) = json.loads(r.output)
    assert res["journal_date"] == "2026-07-27"
    assert res["transcript_file"].endswith(f"2026-07-27-src{res['source_id']}.txt")
    assert not list((root / "data" / "transcripts").rglob("2026-07-20-*"))

    src = _sql_json(f"SELECT uuid, journal_date, recorded_at FROM sources "
                    f"WHERE id={res['source_id']}")[0]
    assert src["journal_date"] == "2026-07-27"
    assert src["recorded_at"] == "2026-07-21T02:30:00Z"    # the instant is the file's
    audit = _sql_json(f"SELECT details FROM audit_log WHERE table_name='sources' "
                      f"AND row_uuid='{src['uuid']}'")
    assert [json.loads(a["details"])["journal_date"] for a in audit] == ["2026-07-27"]
    # no stray day row for the date the override replaced
    assert _sql_json("SELECT journal_date FROM days ORDER BY journal_date") == [
        {"journal_date": "2026-07-27"}]
