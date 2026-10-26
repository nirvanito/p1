"""The phone shown in File Explorer (lifecrm/phone.py): one more place the
readers look, never relied on. A fake sync root stands in for Windows'
registry; the phone's folders are plain folders here, so what is tested is
the finding, the copying-once, the timeouts and the quiet "not answering".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from conftest import cli_project
from test_collect import home  # noqa: F401  (autouse: a home of its own)
from typer.testing import CliRunner

from lifecrm import collect, phone
from lifecrm.cli import app
from lifecrm.connectors import android_sms, gps_track
from lifecrm.domain.util import DomainError

runner = CliRunner()
OLD = time.time() - 3600


@pytest.fixture
def linked(home, monkeypatch):  # noqa: F811
    """One phone Windows shows in File Explorer, with its storage folder."""
    root = home / "CrossDevice" / "Test Phone"
    storage = root / "storage"
    for sub in ("Recordings/Voice Recorder", "Recordings/Call", "VoiceJournal-text",
                "DCIM/Camera", "Download"):
        (storage / sub).mkdir(parents=True)
    (home / "CrossDevice" / "Old Phone" / "storage").mkdir(parents=True)  # unlinked leftover
    monkeypatch.setattr(phone, "_sync_roots", lambda: [("Test Phone", root)])
    phone.forget()
    return storage


def _away(storage: Path):
    """os.listdir as it behaves when the phone's cloud provider is gone:
    everything under the mount fails, everything else is untouched."""
    real = os.listdir

    def listdir(path="."):
        if str(path).lower().startswith(str(storage).lower()):
            raise PermissionError(f"cloud provider not running: {path}")
        return real(path)
    return listdir


def _note(path: Path, body: bytes = b"\x00" * 64, age: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (age, age))
    return path


# ---------------------------------------------------------------- finding

def test_only_phones_in_the_registry_are_mounts(cfg, linked):
    assert [m.name for m in phone.mounts(cfg)] == ["Test Phone"]
    assert phone.under(cfg, linked / "GPSLogger" / "20261024.gpx").name == "Test Phone"
    assert phone.under(cfg, cfg.root / "elsewhere") is None
    cfg.extra = {**(cfg.extra or {}), "phone": {"read": False}}
    assert phone.mounts(cfg) == [] and phone.status(cfg) == []
    cfg.extra = {**cfg.extra, "phone": {"read": "yes"}}
    with pytest.raises(DomainError, match=r"\[phone\] read must be true or false"):
        phone.mounts(cfg)


def test_nothing_without_a_phone(cfg):
    assert phone.mounts(cfg) == [] and phone.folders(cfg, "GPSLogger") == []
    assert phone.pull(cfg) == [] and phone.status(cfg) == []


def test_the_connectors_look_on_the_phone_too(cfg, linked):
    assert phone.folders(cfg, "GPSLogger", "SMSBackupRestore") == []
    (linked / "GPSLogger").mkdir()
    (linked / "SMSBackupRestore").mkdir()
    assert phone.folders(cfg, *gps_track.FOLDER_NAMES) == [linked / "GPSLogger"]
    assert linked / "GPSLogger" in gps_track.search_folders(cfg)
    assert linked / "SMSBackupRestore" in android_sms.search_places(cfg)
    assert phone.status(cfg) == [{"name": "Test Phone", "root": str(linked),
                                  "reachable": True}]


# ---------------------------------------------------------------- the inboxes

def test_pull_copies_each_note_once_and_keeps_its_time(cfg, linked):
    voice = _note(linked / "Recordings" / "Voice Recorder" / "Voice 001.m4a")
    call = _note(linked / "Recordings" / "Call" / "Call recording Mom_261020_200512.m4a")
    text = _note(linked / "VoiceJournal-text" / "2026-10-20 14.05.33.txt", b"a note")
    _note(linked / "Recordings" / "Voice Recorder" / "still-recording.m4a", age=time.time())
    _note(linked / "Recordings" / "Voice Recorder" / ".Voice 002.m4a.part")
    _note(linked / "Recordings" / "Voice Recorder" / "cover.jpg")

    got = phone.pull(cfg)
    assert [(r["status"], r["file"]) for r in got] == [
        ("pulled", "Voice 001.m4a"), ("pulled", "Call recording Mom_261020_200512.m4a"),
        ("pulled", "2026-10-20 14.05.33.txt")]
    assert (cfg.inbox_dir / "Voice 001.m4a").read_bytes() == voice.read_bytes()
    assert int((cfg.inbox_dir / "Voice 001.m4a").stat().st_mtime) == int(OLD)
    assert (cfg.inbox_dir / call.name).exists()
    assert (cfg.inbox_text_dir / text.name).read_bytes() == b"a note"
    assert not list(cfg.inbox_dir.glob(".*"))                # no half-copied names left
    assert voice.exists() and call.exists() and text.exists()   # copied, never moved

    assert phone.pull(cfg) == []                               # the ledger
    (cfg.inbox_dir / "Voice 001.m4a").unlink()                 # intake archived it
    assert phone.pull(cfg) == []                               # still once
    _note(voice, b"\x00" * 128)                                # re-recorded: a new file
    assert [r["file"] for r in phone.pull(cfg)] == ["Voice 001.m4a"]


def test_a_note_already_in_the_inbox_is_not_copied_over(cfg, linked):
    """The sync app brought it first: the inbox copy wins, and the ledger
    remembers so the phone's copy is never asked for again."""
    _note(linked / "Recordings" / "Voice Recorder" / "Voice 001.m4a", b"phone")
    _note(cfg.inbox_dir / "Voice 001.m4a", b"synced")
    assert phone.pull(cfg) == []
    assert (cfg.inbox_dir / "Voice 001.m4a").read_bytes() == b"synced"


def test_a_phone_that_does_not_answer_is_one_line(cfg, linked, monkeypatch):
    _note(linked / "Recordings" / "Voice Recorder" / "Voice 001.m4a")
    (linked / "GPSLogger").mkdir()

    monkeypatch.setattr(phone.os, "listdir", _away(linked))
    phone.forget()
    (r,) = phone.pull(cfg)
    assert r["status"] == "unreachable" and "same Wi-Fi" in r["reason"]
    assert not list(cfg.inbox_dir.iterdir())
    assert phone.folders(cfg, "GPSLogger") == []
    assert phone.status(cfg)[0]["reachable"] is False


def test_a_listing_that_hangs_times_out(monkeypatch, tmp_path):
    monkeypatch.setattr(phone, "TIMEOUT_SEC", 0.2)
    started = time.perf_counter()
    with pytest.raises(phone.Unreachable, match="no answer from the phone in 0.2 s"):
        phone._within(0.2, time.sleep, 5)
    assert time.perf_counter() - started < 2
    assert phone.listing(tmp_path / "not-there") == []
    with pytest.raises(ZeroDivisionError):                     # other errors pass through
        phone._within(1, lambda: 1 / 0)


# ---------------------------------------------------------------- collect

def test_collect_reads_the_phone_and_skips_it_quietly_when_away(conn, cfg, linked,
                                                               monkeypatch):
    from test_gps_track import two_days
    two_days(linked / "GPSLogger")
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["gps_track"]["hold"] == "first_import"
    assert str(linked / "GPSLogger") in by["gps_track"]["reason"]

    # The phone drops off while its folder is read: skipped, no error row.
    real_scan = gps_track.scan

    def dropped(target, cfg_):
        if phone.under(cfg_, target):
            raise OSError(1231, "The network location cannot be reached")
        return real_scan(target, cfg_)
    monkeypatch.setattr(gps_track, "scan", dropped)
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    gps = by["gps_track"]                                        # skipped: no row recorded
    assert (gps["outcome"], gps["skip"]) == ("skipped", "not found")
    assert "stopped answering" in gps["reason"] and "same Wi-Fi" in gps["reason"]
    assert conn.execute("SELECT COUNT(*) FROM collect_runs WHERE source='gps_track'"
                        ).fetchone()[0] == 1                       # the hold, nothing since
    with pytest.raises(DomainError, match="stopped answering.*same Wi-Fi"):
        collect.approve(conn, cfg, "gps_track")

    # Away entirely: not found anywhere, so nothing to say.
    monkeypatch.setattr(phone.os, "listdir", _away(linked))
    phone.forget()
    gps = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}["gps_track"]
    assert (gps["outcome"], gps["skip"]) == ("skipped", "not found")
    assert "stopped answering" not in gps["reason"]
    data = collect.setup_checklist(conn, cfg, "android")
    assert data["linked"] == [{"name": "Test Phone", "root": str(linked),
                               "reachable": False}]
    assert collect.status(conn, cfg)["phone"][0]["reachable"] is False


def test_the_checklist_names_the_route(conn, cfg, linked):
    data = collect.setup_checklist(conn, cfg, "android")
    assert data["linked"][0]["reachable"] is True
    sms = next(it for it in data["items"] if it["source"] == "android_sms")
    assert any("shown in this computer's File Explorer" in s for s in sms["steps"])


# ---------------------------------------------------------------- the CLI

def test_intake_pulls_then_reads(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    root = tmp_path / "CrossDevice" / "Test Phone"
    _note(root / "storage" / "VoiceJournal-text" / "2026-10-20 14.05.33.txt",
          "Lunch with Sam at the taco place.\n".encode())
    monkeypatch.setattr(phone, "_sync_roots", lambda: [("Test Phone", root)])
    phone.forget()
    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 0, r.output
    assert "pulled 2026-10-20 14.05.33.txt from Test Phone" in r.output
    assert "2026-10-20 14.05.33.txt: source id=" in r.output
    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 0 and "both inboxes empty" in r.output
    r = runner.invoke(app, ["collect", "status"])
    assert "phone: Test Phone shown in File Explorer — reachable" in r.output
    r = runner.invoke(app, ["collect", "setup", "--phone", "android"])
    assert "Test Phone is shown in this computer's File Explorer" in r.output
