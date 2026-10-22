"""`lifecrm configure` and `lifecrm where`: a fresh machine's first two
commands.

Both run on any OS here. A Mac (or a Windows PC) is a fake home directory
plus platforms.SYSTEM, and the timezone sources (/etc/localtime's link,
`tzutil /g`) are replaced by what such a machine would report — the parsing
is what is under test, not this machine's own settings.
"""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.commands import configure as configure_mod
from lifecrm.commands import where as where_mod
from lifecrm.config import load_config

runner = CliRunner()


def _home(monkeypatch, tmp_path, system: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(platforms, "SYSTEM", system)
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    return home


def _root(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LIFECRM_ROOT", str(root))
    return root


def _mac(monkeypatch, tmp_path, zone_link="/var/db/timezone/zoneinfo/America/Denver"):
    home = _home(monkeypatch, tmp_path, "mac")
    real_readlink = configure_mod.os.readlink

    def readlink(p, *a, **k):
        return zone_link if str(p) == "/etc/localtime" else real_readlink(p, *a, **k)
    monkeypatch.setattr(configure_mod.os, "readlink", readlink)
    return home


def _icloud(home: Path) -> Path:
    drive = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    drive.mkdir(parents=True)
    return drive


# ---------------------------------------------------------------- configure

def test_configure_on_a_mac_points_the_journal_at_icloud_drive(tmp_path, monkeypatch):
    home = _mac(monkeypatch, tmp_path)
    drive = _icloud(home)
    root = _root(monkeypatch, tmp_path)

    r = runner.invoke(app, ["configure"])
    assert r.exit_code == 0, r.output
    cfg = load_config(root)
    journal = drive / "VoiceJournal"
    assert cfg.inbox_dir == journal / "inbox"
    assert cfg.inbox_text_dir == journal / "inbox-text"
    assert cfg.archive_dir == journal / "archive"
    assert cfg.onedrive_backups_dir == journal / "backups"
    assert cfg.timezone == "America/Denver"
    for sub in ("inbox", "inbox-text", "archive", "backups"):
        assert (journal / sub).is_dir()
    assert "iCloud Drive" in r.output and "from /etc/localtime" in r.output
    assert "Keep Downloaded" in r.output


def test_configure_keeps_everything_else_the_example_says(tmp_path, monkeypatch):
    _icloud(_mac(monkeypatch, tmp_path))
    root = _root(monkeypatch, tmp_path)
    assert runner.invoke(app, ["configure"]).exit_code == 0
    written = (root / "config.toml").read_text(encoding="utf-8")
    example = tomllib.loads((configure_mod.PROJECT_ROOT / "config.example.toml")
                            .read_text(encoding="utf-8"))
    mine = tomllib.loads(written)
    for section in example:
        if section not in ("paths", "time"):
            assert mine[section] == example[section], section
    assert mine["paths"]["data_dir"] == example["paths"]["data_dir"]
    assert mine["time"]["day_rollover_hour"] == example["time"]["day_rollover_hour"]
    assert "# recordings before 05:00 local count as the previous day" in written
    assert "<you>" not in "".join(l for l in written.splitlines()
                                  if not l.lstrip().startswith("#"))
    assert "\r" not in written


def test_configure_on_windows_points_the_journal_at_onedrive(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "windows")
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    monkeypatch.setenv("OneDrive", str(onedrive))
    monkeypatch.setattr(configure_mod, "_windows_zone_name",
                        lambda: "Pacific Standard Time")
    root = _root(monkeypatch, tmp_path)

    r = runner.invoke(app, ["configure"])
    assert r.exit_code == 0, r.output
    cfg = load_config(root)
    assert cfg.inbox_dir == onedrive / "VoiceJournal" / "inbox"
    assert cfg.timezone == "America/Los_Angeles"
    assert "from Windows' 'Pacific Standard Time'" in r.output
    assert (onedrive / "VoiceJournal" / "inbox-text").is_dir()


def test_configure_never_creates_a_missing_cloud_drive(tmp_path, monkeypatch):
    home = _mac(monkeypatch, tmp_path)          # iCloud Drive not turned on
    root = _root(monkeypatch, tmp_path)
    r = runner.invoke(app, ["configure"])
    assert r.exit_code == 0, r.output
    assert "folders NOT created" in r.output and "iCloud Drive is not turned on" in r.output
    assert not (home / "Library").exists()
    assert (root / "config.toml").exists()     # the config is still written


def test_configure_refuses_to_replace_a_config_without_force(tmp_path, monkeypatch):
    _icloud(_mac(monkeypatch, tmp_path))
    root = _root(monkeypatch, tmp_path)
    (root / "config.toml").write_text("[time]\ntimezone = 'Europe/London'\n",
                                      encoding="utf-8")
    r = runner.invoke(app, ["configure"])
    assert r.exit_code == 1 and "already exists" in r.output
    assert "Europe/London" in (root / "config.toml").read_text(encoding="utf-8")

    r = runner.invoke(app, ["configure", "--force"])
    assert r.exit_code == 0, r.output
    kept = list((root / "data").glob("config.toml.replaced-*"))
    assert len(kept) == 1 and "Europe/London" in kept[0].read_text(encoding="utf-8")
    assert load_config(root).timezone == "America/Denver"


def test_configure_takes_an_explicit_folder_and_zone(tmp_path, monkeypatch):
    _mac(monkeypatch, tmp_path)
    root = _root(monkeypatch, tmp_path)
    (tmp_path / "Dropbox").mkdir()
    journal = tmp_path / "Dropbox" / "VoiceJournal"
    r = runner.invoke(app, ["configure", "--journal-root", str(journal),
                            "--timezone", "Europe/Berlin", "--no-mkdir"])
    assert r.exit_code == 0, r.output
    cfg = load_config(root)
    assert cfg.inbox_dir == journal / "inbox" and cfg.timezone == "Europe/Berlin"
    assert not journal.exists() and "--no-mkdir" in r.output


def test_configure_rejects_an_unknown_zone(tmp_path, monkeypatch):
    _mac(monkeypatch, tmp_path)
    root = _root(monkeypatch, tmp_path)
    r = runner.invoke(app, ["configure", "--timezone", "Mars/Olympus_Mons"])
    assert r.exit_code == 1 and "not a known IANA zone" in r.output
    assert not (root / "config.toml").exists()


def test_a_path_with_an_apostrophe_survives_toml_quoting(tmp_path):
    value = "/Users/o'brien/Library/Mobile Documents/com~apple~CloudDocs/VoiceJournal"
    text = f"[paths]\ninbox_dir = {configure_mod.toml_string(value)}\n"
    assert tomllib.loads(text)["paths"]["inbox_dir"] == value
    win = r"C:\Users\alex\OneDrive\VoiceJournal\inbox"
    assert configure_mod.toml_string(win) == f"'{win}'"


def test_render_rewrites_in_place_and_adds_what_is_missing():
    example = ("# old header\n\n[paths]\n"
               "inbox_dir = 'C:\\x'         # where audio lands\n"
               "# inbox_dir = '~/mac'\n\n[time]\nday_rollover_hour = 5\n")
    out = configure_mod.render(example, {
        "paths": {"inbox_dir": "'/new'", "archive_dir": "'/arc'"},
        "time": {"timezone": "'Asia/Tokyo'"},
        "whisper": {"model": "'small'"},
    }, ["# new header"])
    lines = out.splitlines()
    assert lines[0] == "# new header" and "# old header" not in out
    assert "inbox_dir = '/new'         # where audio lands" in lines   # same column
    assert "# inbox_dir = '~/mac'" in lines                 # the other OS's example
    parsed = tomllib.loads(out)
    assert parsed["paths"] == {"inbox_dir": "/new", "archive_dir": "/arc"}
    assert parsed["time"] == {"day_rollover_hour": 5, "timezone": "Asia/Tokyo"}
    assert parsed["whisper"] == {"model": "small"}


# ---------------------------------------------------------------- timezone

def test_windows_zone_names_map_to_iana():
    assert configure_mod.iana_from_windows("Central Standard Time") == "America/Chicago"
    assert configure_mod.iana_from_windows("W. Europe Standard Time") == "Europe/Berlin"
    assert configure_mod.iana_from_windows("Tokyo Standard Time") == "Asia/Tokyo"
    assert configure_mod.iana_from_windows("Eastern Standard Time_dstoff") \
        == "America/New_York"
    assert configure_mod.iana_from_windows("Middle Earth Standard Time") is None


def test_every_mapped_zone_is_one_this_machine_can_load():
    assert len(configure_mod.WINDOWS_ZONES) >= 40
    for windows, iana in configure_mod.WINDOWS_ZONES.items():
        ZoneInfo(iana)          # raises on a name tzdata does not know


def test_an_unmapped_windows_zone_falls_back_with_a_note(monkeypatch):
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    monkeypatch.setattr(configure_mod, "_windows_zone_name",
                        lambda: "Middle Earth Standard Time")
    zone, note = configure_mod.detect_timezone()
    assert zone == "America/Chicago" and "not in the table" in note


def test_tzutil_output_is_read(monkeypatch):
    monkeypatch.setattr(configure_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="Romance Standard Time\r\n"))
    assert configure_mod._windows_zone_name() == "Romance Standard Time"


@pytest.mark.parametrize("link, zone", [
    ("/var/db/timezone/zoneinfo/America/Chicago", "America/Chicago"),
    ("/usr/share/zoneinfo/Europe/London", "Europe/London"),
    ("/usr/share/zoneinfo/posix/Asia/Tokyo", "Asia/Tokyo"),
    ("../usr/share/zoneinfo/America/Argentina/Buenos_Aires",
     "America/Argentina/Buenos_Aires"),
    ("/etc/somewhere-else", None),
])
def test_the_localtime_link_names_the_zone(link, zone):
    assert configure_mod.zone_from_localtime_link(link) == zone


def test_an_unreadable_localtime_falls_back_with_a_note(tmp_path, monkeypatch):
    _mac(monkeypatch, tmp_path, zone_link="/etc/not-a-zone")
    zone, note = configure_mod.detect_timezone()
    assert zone == "America/Chicago" and "set [time] timezone" in note


# ---------------------------------------------------------------- where

def _mac_with_data(monkeypatch, tmp_path):
    home = _mac(monkeypatch, tmp_path)
    drive = _icloud(home)
    lib = home / "Library"
    (lib / "Messages").mkdir(parents=True)
    (lib / "Messages" / "chat.db").write_bytes(b"")
    ab = lib / "Application Support" / "AddressBook" / "Sources" / "A1B2"
    ab.mkdir(parents=True)
    (ab / "AddressBook-v22.abcddb").write_bytes(b"")
    backup = lib / "Application Support" / "MobileSync" / "Backup" / "00008110-ABC"
    backup.mkdir(parents=True)
    (backup / "Manifest.db").write_bytes(b"")
    downloads = home / "Downloads"
    downloads.mkdir()
    for name in ("export.zip", "sms-20261020120000.xml", "calls-20261020120000.xml",
                 "takeout-20261001T000000Z-001.zip", "WhatsApp Chat - Jake Morales.zip",
                 "Health Connect.zip", "unrelated.pdf"):
        (downloads / name).write_bytes(b"")
    root = _root(monkeypatch, tmp_path)
    assert runner.invoke(app, ["configure"]).exit_code == 0
    inbox = drive / "VoiceJournal" / "inbox"
    (inbox / "2026-10-20 21.30.05 -0500.m4a").write_bytes(b"x")
    (inbox / ".2026-10-21 07.10.00 -0500.m4a.icloud").write_bytes(b"x")
    return home, root


def _groups(output: str) -> dict:
    return {g["title"]: g for g in json.loads(output)["groups"]}


def test_where_reports_what_a_mac_holds_and_what_to_run(tmp_path, monkeypatch):
    home, root = _mac_with_data(monkeypatch, tmp_path)

    def no_subprocess(*a, **k):
        raise AssertionError("where must only look")
    monkeypatch.setattr("subprocess.run", no_subprocess)

    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    report = json.loads(r.output)
    assert report["system"] == "mac" and report["config"] == str(root / "config.toml")
    g = _groups(r.output)

    journal = g["Journal — voice notes and quick notes"]
    inbox = next(i for i in journal["items"] if i["label"] == "inbox_dir")
    assert inbox["state"] == "found"
    assert inbox["detail"] == "1 waiting, 1 still in iCloud (intake asks for them)"

    backups = g["iPhone backups — texts and calls"]["items"]
    assert any(i["state"] == "found" and i["detail"] == "1 backup(s)" for i in backups)
    assert g["Messages on this Mac"]["items"][0]["state"] == "found"
    assert g["Contacts"]["items"][0]["path"].endswith("AddressBook-v22.abcddb")
    assert g["Photos library (metadata only)"]["items"][0]["state"] == "missing"

    health = g["Health exports (Apple Health, Health Connect)"]
    assert {i["label"] for i in health["items"]} == {"Apple Health", "Health Connect"}
    commands = [c["command"] for c in health["commands"]]
    assert f'python -m lifecrm health import "{home / "Downloads" / "export.zip"}"' \
        in commands
    sms = g["SMS Backup & Restore (Android texts and calls)"]
    assert len(sms["items"]) == 2
    assert any("observe android_sms" in c["command"] for c in sms["commands"])
    assert g["Google Takeout"]["items"][0]["label"] == "Google Takeout"
    assert g["WhatsApp chat exports"]["items"]
    assert not g["Calendar exports (.ics)"]["items"]
    assert "none in" in g["Calendar exports (.ics)"]["note"]


def test_where_marks_commands_for_connectors_this_version_lacks(tmp_path, monkeypatch):
    _mac_with_data(monkeypatch, tmp_path)
    monkeypatch.setattr(where_mod, "_known_connectors", lambda: {"ios_messages"})
    r = runner.invoke(app, ["where", "--json"])
    commands = {c["command"]: c["available"]
                for grp in json.loads(r.output)["groups"] for c in grp["commands"]}
    assert commands["python -m lifecrm observe ios_messages"] is True
    assert commands["python -m lifecrm observe mac_messages"] is False
    assert commands["python -m lifecrm intake"] is True
    text = runner.invoke(app, ["where"]).output
    assert "observe mac_messages   (connector not in this version)" in text


def test_where_reports_a_privacy_guarded_place_as_no_access(tmp_path, monkeypatch):
    home, _ = _mac_with_data(monkeypatch, tmp_path)
    guarded = home / "Library" / "Messages" / "chat.db"
    real_exists = Path.exists

    def exists(self, *a, **k):
        if self == guarded:
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_exists(self, *a, **k)
    monkeypatch.setattr(Path, "exists", exists)

    r = runner.invoke(app, ["where"])
    assert r.exit_code == 0, r.output
    assert "no access" in r.output and "Full Disk Access" in r.output
    item = _groups(runner.invoke(app, ["where", "--json"]).output)[
        "Messages on this Mac"]["items"][0]
    assert item["state"] == "no access" and "Full Disk Access" in item["detail"]


def test_where_on_windows_skips_the_mac_only_places(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "windows")
    _root(monkeypatch, tmp_path)
    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    report = json.loads(r.output)
    titles = {g["title"] for g in report["groups"]}
    assert "Messages on this Mac" not in titles and "Contacts" not in titles
    assert report["config"] is None
    journal = _groups(r.output)["Journal — voice notes and quick notes"]
    assert journal["note"] == "no config.toml yet"
    assert journal["commands"][0]["command"] == "python -m lifecrm configure"


def test_where_text_output_names_each_place_once(tmp_path, monkeypatch):
    _mac_with_data(monkeypatch, tmp_path)
    r = runner.invoke(app, ["where"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("this machine: Mac; config: ")
    assert "run: python -m lifecrm observe mac_messages" in r.output
    assert "~" in r.output                      # home shown as ~, paths stay short
