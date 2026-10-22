"""iCloud Drive as the inbox (Mac + iPhone).

Two things iCloud does that OneDrive does not: with Optimize Mac Storage it
replaces a file it has evicted with a hidden stub '.<name>.icloud' (older
macOS) or keeps the name and marks the file dataless (SF_DATALESS, newer
macOS); and Finder, exFAT drives and unzipped archives leave AppleDouble
'._<name>' files that carry the real file's suffix and none of its bytes.
Intake must never read a stub or an AppleDouble file as a note, must ask
iCloud for a stub's note (once), and doctor must count stubs — skipped with
the other dotfiles, a note that never came down would read as a quiet day.

The Mac-only parts are exercised by pretending to be a Mac (platforms.SYSTEM)
and recording the `brctl` call instead of making it.
"""

import os
import sys
import time
from types import SimpleNamespace

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import doctor as doctor_mod
from lifecrm import platforms
from lifecrm import transcribe as tr
from lifecrm.cli import app
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks

runner = CliRunner()
OLD = time.time() - 3 * 86400
NOTE = "2026-07-28 08.00.00 -0500.m4a"


def _aged(path, when=OLD):
    os.utime(path, (when, when))
    return path


@pytest.fixture
def brctl(monkeypatch):
    """Pretend to be a Mac; record brctl calls instead of running them."""
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    monkeypatch.setattr(tr, "_DOWNLOAD_REQUESTED", set())
    return calls


# ---------------------------------------------------------------- intake's scan

def test_appledouble_files_are_never_taken_for_recordings(cfg):
    real = cfg.inbox_dir / "2026-07-27 21.30.05 -0500.m4a"
    real.write_bytes(b"audio")
    _aged(real)
    double = cfg.inbox_dir / "._2026-07-27 21.30.05 -0500.m4a"
    double.write_bytes(b"\x00\x05\x16\x07")                 # AppleDouble magic
    _aged(double)
    assert tr.scan_inbox(cfg) == [real]


def test_an_icloud_stub_is_asked_for_once_and_never_returned(cfg, brctl, capsys):
    real = cfg.inbox_dir / "2026-07-27 21.30.05 -0500.m4a"
    real.write_bytes(b"audio")
    _aged(real)
    stub = cfg.inbox_dir / f".{NOTE}.icloud"
    stub.write_bytes(b"bplist00")
    _aged(stub)

    assert tr.scan_inbox(cfg) == [real]
    assert tr.scan_inbox(cfg) == [real]       # intake scans each inbox twice
    assert brctl == [["brctl", "download", str(stub)]]
    err = capsys.readouterr().err
    assert err.count("waiting:") == 1
    assert f"{NOTE} is still in iCloud" in err and "download requested" in err


def test_a_text_note_stub_waits_too(cfg, brctl, capsys):
    stub = cfg.inbox_text_dir / ".2026-07-28 12.00.00 -0500.txt.icloud"
    stub.write_bytes(b"bplist00")
    assert tr.scan_text_inbox(cfg) == []
    assert "2026-07-28 12.00.00 -0500.txt is still in iCloud" in capsys.readouterr().err


def test_a_stub_for_a_file_intake_does_not_read_is_ignored(cfg, brctl, capsys):
    (cfg.inbox_dir / ".holiday.jpg.icloud").write_bytes(b"bplist00")
    assert tr.scan_inbox(cfg) == []
    assert brctl == [] and capsys.readouterr().err == ""


def test_off_a_mac_a_stub_is_reported_without_brctl(cfg, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(tr, "_DOWNLOAD_REQUESTED", set())
    (cfg.inbox_dir / f".{NOTE}.icloud").write_bytes(b"bplist00")
    assert tr.scan_inbox(cfg) == []
    assert calls == []
    err = capsys.readouterr().err
    assert f"{NOTE} is still in iCloud" in err and "download requested" not in err


def test_a_failing_brctl_is_reported_not_raised(cfg, monkeypatch, capsys):
    def broken(*a, **k):
        raise FileNotFoundError("brctl")
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    monkeypatch.setattr(tr.subprocess, "run", broken)
    monkeypatch.setattr(tr, "_DOWNLOAD_REQUESTED", set())
    (cfg.inbox_dir / f".{NOTE}.icloud").write_bytes(b"bplist00")
    assert tr.scan_inbox(cfg) == []
    assert "could not ask for it" in capsys.readouterr().err


def test_intake_with_only_stubs_is_a_quiet_success_that_says_what_waits(
        tmp_path, monkeypatch, brctl):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    (root / "onedrive" / "inbox-text" / ".2026-07-28 12.00.00 -0500.txt.icloud") \
        .write_bytes(b"bplist00")
    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 0, r.output
    assert "nothing to intake" in r.stdout
    assert "2026-07-28 12.00.00 -0500.txt is still in iCloud" in r.stderr


# ---------------------------------------------------------------- placeholders

def _fake_stat(monkeypatch, target, **fields):
    real_stat = os.stat

    def fake(p, *a, **k):
        if str(p) == str(target):
            return SimpleNamespace(st_mtime=OLD, **fields)
        return real_stat(p, *a, **k)
    monkeypatch.setattr(tr.os, "stat", fake)


def test_a_dataless_icloud_file_is_a_placeholder(tmp_path, monkeypatch):
    f = tmp_path / "note.m4a"
    f.write_bytes(b"x")
    _fake_stat(monkeypatch, f, st_flags=tr.SF_DATALESS)
    assert tr.is_placeholder(f)


def test_a_onedrive_recall_file_is_still_a_placeholder(tmp_path, monkeypatch):
    f = tmp_path / "note.m4a"
    f.write_bytes(b"x")
    _fake_stat(monkeypatch, f, st_file_attributes=tr.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    assert tr.is_placeholder(f)


def test_an_ordinary_file_is_not_a_placeholder(tmp_path, monkeypatch):
    f = tmp_path / "note.m4a"
    f.write_bytes(b"x")
    _fake_stat(monkeypatch, f, st_flags=0x1, st_file_attributes=0x20)
    assert not tr.is_placeholder(f)
    assert not tr.is_placeholder(tmp_path / "missing.m4a")


def test_an_unreadable_cloud_file_error_names_no_one_vendor(tmp_path, monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    with pytest.raises(DomainError) as e:
        tr.read_hydrated(tmp_path / "gone.m4a", retries=2)
    assert "cloud file offline or not downloaded yet" in str(e.value)
    assert "OneDrive" not in str(e.value)


# ---------------------------------------------------------------- doctor

def _check(cfg, conn, name):
    return next(c for c in run_checks(conn, cfg)["checks"] if c["check"] == name)


def test_doctor_counts_notes_still_in_icloud(cfg, conn):
    (cfg.inbox_dir / f".{NOTE}.icloud").write_bytes(b"bplist00")
    (cfg.inbox_text_dir / ".2026-07-28 12.00.00 -0500.txt.icloud").write_bytes(b"x")
    (cfg.inbox_dir / ".holiday.jpg.icloud").write_bytes(b"x")     # not a note
    check = _check(cfg, conn, "inbox_stuck")
    assert check["status"] == "warn"
    detail = check["detail"]
    assert "2 note(s) still in iCloud" in detail
    assert "open the VoiceJournal folder in Finder or set it to Keep Downloaded" in detail
    assert f"inbox_dir/{NOTE}" in detail and "holiday" not in detail
    assert _check(cfg, conn, "inbox_unrecognized")["status"] == "ok"


def test_doctor_reports_stubs_and_stuck_files_together(cfg, conn):
    (cfg.inbox_dir / f".{NOTE}.icloud").write_bytes(b"bplist00")
    _aged(cfg.inbox_dir / f".{NOTE}.icloud")
    old = cfg.inbox_dir / "old.m4a"
    old.write_bytes(b"x")
    _aged(old)
    detail = _check(cfg, conn, "inbox_stuck")["detail"]
    assert "1 file(s) waiting over" in detail and "1 note(s) still in iCloud" in detail


def test_doctor_ignores_appledouble_and_finder_icon_litter(cfg, conn):
    assert "icon\r" in doctor_mod._INBOX_LITTER
    (cfg.inbox_dir / "._2026-07-27.m4a").write_bytes(b"\x00\x05\x16\x07")
    if sys.platform != "win32":        # a CR in a file name is illegal on Windows
        (cfg.inbox_dir / "Icon\r").write_bytes(b"")
    assert _check(cfg, conn, "inbox_stuck")["status"] == "ok"
    assert _check(cfg, conn, "inbox_unrecognized")["status"] == "ok"
